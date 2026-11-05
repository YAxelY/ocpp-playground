"""End-to-end: a real CSMS and real simulated stations talking over WebSockets on a free port."""
import asyncio
import socket
import time
import unittest

from playground.csms import commands
from playground.csms.server import Csms
from playground.events import Bus
from playground.station.simulator import Station


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


async def until(predicate, timeout=8.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(step)
    raise AssertionError("condition not reached in time")


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = Bus(history=5000)
        self.csms = Csms(self.bus)
        self.csms.policy.update({"heartbeat_interval": 2, "retry_interval": 1})
        port = free_port()
        self.url = f"ws://localhost:{port}/ocpp"
        self.server = await self.csms.serve("localhost", port)
        self.stations = []

    async def asyncTearDown(self):
        for st in self.stations:
            await st.power_off()
        self.server.close()
        await self.server.wait_closed()

    def station(self, sid, offer=("ocpp2.1", "ocpp2.0.1"), evses=1):
        st = Station(sid, self.bus, self.url, evses=evses, offer=offer, heartbeat=2)
        st.variables["OCPPCommCtrlr.RetryBackOffWaitMinimum"][0] = 0.3
        st.variables["OCPPCommCtrlr.RetryBackOffRandomRange"][0] = 0.1
        st.variables["SampledDataCtrlr.TxUpdatedInterval"][0] = 1
        self.stations.append(st)
        return st

    async def accepted(self, sid, offer=("ocpp2.1", "ocpp2.0.1"), evses=1, connectors=None):
        st = self.station(sid, offer, evses)
        await st.power_on()
        await until(lambda: st.registration == "Accepted" and st.link)
        await until(lambda: len(self.csms.stations[sid].connectors) == (connectors or evses))
        return st

    def frames(self, sid, kind=None, action=None):
        return [e for e in self.bus.history if e["type"] == "frame" and e["station"] == sid
                and (kind is None or e["kind"] == kind) and (action is None or e.get("action") == action)]

    def errors(self, sid):
        return [e["payload"]["errorCode"] for e in self.frames(sid, "CALLERROR")]


class EndToEnd2x(Base):
    async def test_pending_then_accepted_and_version_negotiation(self):
        st21 = await self.accepted("CS-21")
        st201 = await self.accepted("CS-201", offer=("ocpp2.0.1",))
        self.assertEqual((st21.version.name, st201.version.name), ("2.1", "2.0.1"))
        self.assertEqual(len(self.frames("CS-21", "CALL", "BootNotification")), 2)      # Pending, then Accepted
        self.assertEqual(self.csms.stations["CS-21"].connectors, {"1/1": "Available"})

    async def test_version_refused_at_handshake(self):
        self.csms.policy.update({"versions": ["2.1", "2.0.1"]})
        st = self.station("CS-16", offer=("ocpp1.6",))
        await st.power_on()
        await until(lambda: any(e["type"] == "note" and e["station"] == "CS-16" and "no common version" in e["text"]
                                for e in self.bus.history))
        self.assertFalse(st.link)

    async def test_security_error_while_pending(self):
        self.csms.policy.update({"retry_interval": 5})
        st = self.station("CS-P")
        await st.power_on()
        await until(lambda: st.registration == "Pending")
        await st.edge_case("heartbeat_now")
        await until(lambda: "SecurityError" in self.errors("CS-P"))

    async def test_protocol_errors(self):
        st = await self.accepted("CS-E", offer=("ocpp2.0.1",))
        for name in ("unknown_action", "unsupported_action", "malformed", "unknown_type", "send_frame", "invalid_payload"):
            await st.edge_case(name)
        await until(lambda: {"NotImplemented", "NotSupported", "RpcFrameworkError", "MessageTypeNotSupported",
                             "PropertyConstraintViolation"} <= set(self.errors("CS-E")))
        self.assertTrue([e for e in self.frames("CS-E", "CALLERROR") if e["uid"] == "-1"])

    async def test_transaction_cable_first(self):
        st = await self.accepted("CS-T")
        await st.plug(1)
        await st.present_token(1, "AXEL-0001")
        await until(lambda: st.evses[1].tx and st.evses[1].tx.energy_wh > 3)
        tx_id = st.evses[1].tx.id
        await st.present_token(1, "AXEL-0001")                          # same card → stop
        rec_tx = self.csms.stations["CS-T"].transactions[tx_id]
        await until(lambda: rec_tx.state == "Ended")
        self.assertEqual((rec_tx.stopped_reason, rec_tx.seq_gaps), ("Local", 0))
        self.assertGreater(rec_tx.energy_wh, 0)

    async def test_blocked_token_hidden_by_policy(self):
        st = await self.accepted("CS-A")
        self.assertEqual(await st.authorize("BLOCKED-01"), "Blocked")
        self.csms.policy.update({"authorize_detail": "accepted_or_invalid"})
        self.assertEqual(await st.authorize("BLOCKED-01"), "Invalid")

    async def test_offline_transaction_events_are_queued(self):
        st = await self.accepted("CS-O")
        await st.drop_network()
        await until(lambda: not st.link)
        await st.plug(1)
        await st.present_token(1, "AXEL-0001")
        self.assertGreaterEqual(len(st.tx_queue), 2)
        tx_id = st.evses[1].tx.id
        await st.restore_network()
        await until(lambda: st.link and not st.tx_queue, timeout=12)
        transactions = self.csms.stations["CS-O"].transactions
        await until(lambda: tx_id in transactions and transactions[tx_id].offline_events >= 2)

    async def test_csms_commands(self):
        st = await self.accepted("CS-C", offer=("ocpp2.0.1",))
        self.assertTrue((await commands.run(self.csms, "CS-C", "set_heartbeat", {"value": "7"}))["ok"])
        self.assertEqual(st.var("OCPPCommCtrlr.HeartbeatInterval"), 7)
        r = await commands.run(self.csms, "CS-C", "read_heartbeat", {})
        self.assertEqual(r["result"]["get_variable_result"][0]["attribute_value"], "7")
        r = await commands.run(self.csms, "CS-C", "request_start", {"connector": 1, "token": "FLEET-0042"})
        self.assertEqual(r["result"]["status"], "Accepted")
        await until(lambda: st.evses[1].tx is not None)
        boots = self.csms.stations["CS-C"].boots
        await commands.run(self.csms, "CS-C", "reset", {})
        await until(lambda: self.csms.stations["CS-C"].boots > boots and st.registration == "Accepted")

    async def test_full_report_while_pending(self):
        self.csms.policy.update({"retry_interval": 5})
        st = self.station("CS-R")
        await st.power_on()
        await until(lambda: st.registration == "Pending")
        self.assertTrue((await commands.run(self.csms, "CS-R", "full_report", {}))["ok"])
        await until(lambda: "OCPPCommCtrlr.HeartbeatInterval" in self.csms.stations["CS-R"].variables)

    async def test_periodic_event_stream_2_1(self):
        st = await self.accepted("CS-S")
        await st.open_stream()
        rec = self.csms.stations["CS-S"]
        await until(lambda: any(len(s["values"]) >= 3 for s in rec.streams.values()), timeout=10)
        sends = {f["uid"] for f in self.frames("CS-S", "SEND")}
        self.assertTrue(sends)
        self.assertFalse([e for e in self.frames("CS-S") if e["dir"] == "to_station" and e["uid"] in sends])

    async def test_invalid_response_gives_callresulterror_in_2_1(self):
        await self.accepted("CS-F")
        self.csms.policy.update({"fault_bad_heartbeat_response": True})
        await until(lambda: self.frames("CS-F", "CALLRESULTERROR"), timeout=6)
        self.assertEqual(self.frames("CS-F", "CALLRESULTERROR")[0]["error_code"], "OccurrenceConstraintViolation")


class EndToEnd16(Base):
    async def test_boot_and_connector_zero(self):
        st = await self.accepted("CS-16", offer=("ocpp1.6",), evses=2, connectors=3)
        self.assertEqual(st.version.name, "1.6")
        self.assertEqual(self.csms.stations["CS-16"].connectors, {"0": "Available", "1": "Available", "2": "Available"})

    async def test_transaction_ids_come_from_the_csms(self):
        st = await self.accepted("CS-T16", offer=("ocpp1.6",), connectors=2)
        rec = self.csms.stations["CS-T16"]
        await st.plug(1)
        await until(lambda: rec.connectors.get("1") == "Preparing")
        await st.present_token(1, "AXEL-0001")
        await until(lambda: st.evses[1].tx and st.evses[1].tx.server_id is not None)
        server_id = st.evses[1].tx.server_id
        self.assertGreaterEqual(server_id, 1001)                         # assigned by the CSMS
        await until(lambda: rec.connectors.get("1") == "Charging")
        await until(lambda: self.frames("CS-T16", "CALL", "MeterValues"))
        await st.present_token(1, "AXEL-0001")                          # same card → StopTransaction
        await until(lambda: rec.transactions[str(server_id)].state == "Ended")
        await until(lambda: rec.connectors.get("1") == "Finishing")
        await st.unplug(1)
        await until(lambda: rec.connectors.get("1") == "Available")

    async def test_1_6_error_codes_and_unknown_type(self):
        st = await self.accepted("CS-E16", offer=("ocpp1.6",), connectors=2)
        for name in ("missing_field", "invalid_payload", "unknown_action", "unknown_type", "send_frame"):
            await st.edge_case(name)
        await until(lambda: {"OccurenceConstraintViolation", "PropertyConstraintViolation", "NotImplemented"}
                    <= set(self.errors("CS-E16")))
        self.assertNotIn("MessageTypeNotSupported", self.errors("CS-E16"))     # 1.6 ignores unknown types

    async def test_1_6_commands(self):
        st = await self.accepted("CS-C16", offer=("ocpp1.6",), connectors=2)
        self.assertTrue((await commands.run(self.csms, "CS-C16", "set_heartbeat", {"value": "9"}))["ok"])
        self.assertEqual(st.var("OCPPCommCtrlr.HeartbeatInterval"), 9)
        await commands.run(self.csms, "CS-C16", "full_report", {})
        self.assertEqual(self.csms.stations["CS-C16"].variables.get("HeartbeatInterval"), "9")
        r = await commands.run(self.csms, "CS-C16", "request_start", {"connector": 1, "token": "FLEET-0042"})
        self.assertEqual(r["result"]["status"], "Accepted")
        await st.plug(1)
        await until(lambda: st.evses[1].tx and st.evses[1].tx.server_id)
        r = await commands.run(self.csms, "CS-C16", "request_stop", {"transaction_id": st.evses[1].tx.server_id})
        self.assertEqual(r["result"]["status"], "Accepted")
        await until(lambda: st.evses[1].tx is None)

    async def test_1_6_offline_start_gets_id_after_reconnect(self):
        st = await self.accepted("CS-O16", offer=("ocpp1.6",), connectors=2)
        await st.drop_network()
        await until(lambda: not st.link)
        await st.plug(1)
        await st.present_token(1, "AXEL-0001")
        tx = st.evses[1].tx
        self.assertIsNone(tx.server_id)                                  # nobody to assign one yet
        await st.restore_network()
        await until(lambda: tx.server_id is not None, timeout=12)
