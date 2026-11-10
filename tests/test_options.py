"""Optional / configurable behaviour (playground/station/options.py and the CSMS policy), end to end."""
from playground.csms import commands
from playground.station.options import BY_KEY, parse

from tests.test_end_to_end import Base, until


def payloads(test, sid, action, kind="CALL"):
    return [f["payload"] for f in test.frames(sid, kind, action)]


class StatusReporting(Base):
    async def test_2_1_reports_connector_status_with_notify_event_by_default(self):
        await self.accepted("CS-N", offer=("ocpp2.1",))
        events = payloads(self, "CS-N", "NotifyEvent")
        self.assertTrue(events)
        data = events[0]["eventData"][0]
        self.assertEqual((data["component"]["name"], data["variable"]["name"], data["actualValue"]),
                         ("Connector", "AvailabilityState", "Available"))
        self.assertFalse(payloads(self, "CS-N", "StatusNotification"))
        self.assertEqual(self.csms.stations["CS-N"].connectors, {"1/1": "Available"})

    async def test_2_1_deprecated_status_notification_still_works(self):
        st = self.station("CS-S", offer=("ocpp2.1",))
        st.set_option("StatusReport21", "StatusNotification")
        await st.power_on()
        await until(lambda: self.csms.stations.get("CS-S") and self.csms.stations["CS-S"].connectors)
        self.assertTrue(payloads(self, "CS-S", "StatusNotification"))
        self.assertFalse(payloads(self, "CS-S", "NotifyEvent"))
        self.assertTrue(any("deprecated" in (e.get("text") or "") for e in self.bus.history if e["type"] == "note"))

    async def test_2_0_1_has_no_notify_event_status_and_sends_the_boot_security_event(self):
        await self.accepted("CS-201", offer=("ocpp2.0.1",))
        self.assertTrue(payloads(self, "CS-201", "StatusNotification"))
        await until(lambda: payloads(self, "CS-201", "SecurityEventNotification"))
        self.assertEqual(payloads(self, "CS-201", "SecurityEventNotification")[0]["type"], "StartupOfTheDevice")

    async def test_1_6_minimum_status_duration_skips_short_statuses(self):
        st = self.station("CS-M", offer=("ocpp1.6",))
        st.set_option("MinimumStatusDuration", 1)
        await st.power_on()
        await until(lambda: st.accepted and self.csms.stations["CS-M"].connectors.get("1") == "Available")
        await st.plug(1)
        await st.unplug(1)                                    # Preparing for a moment only
        await until(lambda: any("MinimumStatusDuration" in (e.get("text") or "") for e in self.bus.history
                                if e["type"] == "note"), timeout=4)
        self.assertNotIn("Preparing", [p["status"] for p in payloads(self, "CS-M", "StatusNotification")])


class Transactions(Base):
    async def test_power_path_closed_start_point(self):
        st = self.station("CS-P")
        st.set_option("TxCtrlr.TxStartPoint", "PowerPathClosed")
        await st.power_on()
        await until(lambda: st.accepted)
        await st.plug(1)
        self.assertIsNone(st.evses[1].tx)                     # cable alone does not start it
        await st.present_token(1, "AXEL-0001")
        await until(lambda: payloads(self, "CS-P", "TransactionEvent"))
        started = payloads(self, "CS-P", "TransactionEvent")[0]
        self.assertEqual((started["eventType"], started["triggerReason"]), ("Started", "Authorized"))
        self.assertEqual(started["meterValue"][0]["sampledValue"][0]["context"], "Transaction.Begin")

    async def test_stop_point_ev_connected_keeps_the_transaction_after_the_card(self):
        st = self.station("CS-E")
        st.set_option("TxCtrlr.TxStopPoint", "EVConnected")
        await st.power_on()
        await until(lambda: st.accepted)
        await st.plug(1)
        await st.present_token(1, "AXEL-0001")
        await st.present_token(1, "AXEL-0001")                # card again: authorization ends, not the transaction
        self.assertIsNotNone(st.evses[1].tx)
        await st.unplug(1)
        await until(lambda: [p for p in payloads(self, "CS-E", "TransactionEvent") if p["eventType"] == "Ended"])
        events = payloads(self, "CS-E", "TransactionEvent")
        self.assertIn("StopAuthorized", [p["triggerReason"] for p in events if p["eventType"] == "Updated"])
        ended = [p for p in events if p["eventType"] == "Ended"][0]
        self.assertEqual(ended["transactionInfo"]["stoppedReason"], "EVDisconnected")

    async def test_ev_pause_reports_suspended_ev(self):
        st = await self.accepted("CS-Z")
        await st.plug(1)
        await st.present_token(1, "AXEL-0001")
        await st.ev_pause(1, True)
        await until(lambda: [p for p in payloads(self, "CS-Z", "TransactionEvent")
                             if p["triggerReason"] == "ChargingStateChanged"])
        changed = [p for p in payloads(self, "CS-Z", "TransactionEvent") if p["triggerReason"] == "ChargingStateChanged"]
        self.assertEqual(changed[0]["transactionInfo"]["chargingState"], "SuspendedEV")
        self.assertFalse(st.evses[1].tx.charging)

    async def test_reset_on_idle_is_scheduled_during_a_transaction(self):
        st = await self.accepted("CS-R", offer=("ocpp2.0.1",))
        await st.plug(1)
        await st.present_token(1, "AXEL-0001")
        r = await commands.run(self.csms, "CS-R", "reset", {"type": "OnIdle"})
        self.assertEqual(r["result"]["status"], "Scheduled")
        self.assertEqual(st.pending_reset, "OnIdle")


class Authorization(Base):
    async def test_offline_unknown_card_refused_when_not_allowed(self):
        st = await self.accepted("CS-U")
        st.set_option("AuthCtrlr.OfflineTxForUnknownIdEnabled", False)
        await st.drop_network()
        await until(lambda: not st.link)
        self.assertEqual(await st.authorize("UNKNOWN-99"), "Invalid")

    async def test_cache_and_local_pre_authorize(self):
        st = await self.accepted("CS-C")
        self.assertEqual(await st.authorize("AXEL-0001"), "Accepted")      # Authorize, then cached
        st.set_option("AuthCtrlr.LocalPreAuthorize", True)
        before = len(payloads(self, "CS-C", "Authorize"))
        self.assertEqual(await st.authorize("AXEL-0001"), "Accepted")
        self.assertEqual(len(payloads(self, "CS-C", "Authorize")), before)  # no Authorize this time
        r = await commands.run(self.csms, "CS-C", "clear_cache", {})
        self.assertEqual(r["result"]["status"], "Accepted")
        self.assertEqual(st.auth_cache, {})

    async def test_authorize_remote_start(self):
        st = await self.accepted("CS-AR")
        st.set_option("AuthCtrlr.AuthorizeRemoteStart", True)
        await commands.run(self.csms, "CS-AR", "request_start", {"connector": 1, "token": "FLEET-0042"})
        await until(lambda: payloads(self, "CS-AR", "Authorize"))
        await until(lambda: st.evses[1].auth_token == "FLEET-0042")

    async def test_cache_expiry_from_the_csms(self):
        st = await self.accepted("CS-X")
        self.csms.policy.update({"cache_expiry_minutes": 30})
        await st.authorize("AXEL-0001")
        answer = payloads(self, "CS-X", "Authorize", "CALLRESULT")[0]
        self.assertIn("cacheExpiryDateTime", answer["idTokenInfo"])

    async def test_1_6_invalid_id_without_stop_suspends_the_energy(self):
        st = await self.accepted("CS-I16", offer=("ocpp1.6",), connectors=2)
        st.set_option("TxCtrlr.StopTxOnInvalidId", False)
        await st.drop_network()
        await until(lambda: not st.link)
        await st.plug(1)
        await st.present_token(1, "BLOCKED-01")               # unknown offline → accepted locally
        await st.restore_network()
        await until(lambda: self.csms.stations["CS-I16"].connectors.get("1") == "SuspendedEVSE", timeout=12)
        self.assertIsNotNone(st.evses[1].tx)


class ConfigurationAccess(Base):
    async def test_options_are_device_model_variables(self):
        st = await self.accepted("CS-V", offer=("ocpp2.0.1",))
        r = await commands.run(self.csms, "CS-V", "set_heartbeat", {"value": "7"})
        self.assertTrue(r["ok"])
        self.assertEqual(st.set_variable("TxCtrlr.TxStartPoint", "Nonsense"), "Rejected")
        self.assertEqual(st.set_variable("TxCtrlr.TxStartPoint", "EVConnected"), "Accepted")
        self.assertEqual(st.set_variable("AuthCtrlr.LocalPreAuthorize", "true"), "Accepted")
        self.assertIs(st.opt("AuthCtrlr.LocalPreAuthorize"), True)

    async def test_1_6_configuration_keys_map_to_the_same_options(self):
        st = await self.accepted("CS-K16", offer=("ocpp1.6",), connectors=2)
        self.assertEqual(st.config_16("LocalPreAuthorize"), ("false", False))
        cp = self.csms.connections["CS-K16"]
        resp = await cp.call(cp.version.call.ChangeConfiguration(key="LocalPreAuthorize", value="true"))
        self.assertEqual(resp.status, "Accepted")
        self.assertIs(st.opt("AuthCtrlr.LocalPreAuthorize"), True)

    async def test_clock_is_set_from_the_csms(self):
        st = self.station("CS-CL")
        st.set_option("ClockErrorAtStart", -3600)
        await st.power_on()
        await until(lambda: st.accepted)
        self.assertLess(abs(st.clock_offset), 5)
        st2 = self.station("CS-CL2")
        st2.set_option("ClockErrorAtStart", -3600)
        st2.set_option("ClockCtrlr.TimeSource", "RealTimeClock")
        await st2.power_on()
        await until(lambda: st2.accepted)
        self.assertEqual(st2.clock_offset, -3600)

    async def test_clock_aligned_meter_values_when_idle(self):
        st = self.station("CS-AL", offer=("ocpp2.0.1",))
        st.set_option("AlignedDataCtrlr.Interval", 1)
        await st.power_on()
        await until(lambda: payloads(self, "CS-AL", "MeterValues"), timeout=6)
        value = payloads(self, "CS-AL", "MeterValues")[0]["meterValue"][0]["sampledValue"][0]
        self.assertEqual(value["context"], "Sample.Clock")

    def test_parse_rejects_bad_values(self):
        self.assertRaises(ValueError, parse, BY_KEY["TxCtrlr.TxStopPoint"], "")
        self.assertRaises(ValueError, parse, BY_KEY["AuthCacheCtrlr.Enabled"], "yes")
        self.assertEqual(parse(BY_KEY["SampledDataCtrlr.TxUpdatedMeasurands"], "SoC, Voltage"), "SoC,Voltage")


class Registration(Base):
    async def test_pending_then_trigger_boot(self):
        self.csms.policy.update({"pending_for": 2, "retry_interval": 30, "pending_actions": ["trigger_boot"],
                                 "pending_action_delay": 0})
        st = self.station("CS-PT")
        await st.power_on()
        await until(lambda: self.frames("CS-PT", "CALL", "TriggerMessage"), timeout=6)
        await until(lambda: st.registration == "Accepted")            # long before the 30 s retry

    async def test_pending_actions_read_and_configure_the_station(self):
        self.csms.policy.update({"pending_for": 30, "pending_action_delay": 0,
                                 "pending_actions": ["get_variables", "get_base_report", "set_variables"]})
        st = self.station("CS-PA", offer=("ocpp2.0.1",))
        await st.power_on()
        rec = lambda: self.csms.stations["CS-PA"]
        await until(lambda: self.frames("CS-PA", "CALL", "SetVariables"), timeout=8)
        self.assertEqual(st.registration, "Pending")
        for action in ("GetVariables", "GetBaseReport", "NotifyReport"):
            self.assertTrue(self.frames("CS-PA", "CALL", action), action)
        self.assertEqual(rec().variables.get("ChargingStation.Model"), "Playground-1")

    async def test_pending_then_rejected(self):
        self.csms.policy.update({"registration_mode": "pending_then_reject", "pending_for": 1})
        st = self.station("CS-PR")
        await st.power_on()
        await until(lambda: st.registration == "Pending")
        await until(lambda: st.registration == "Rejected", timeout=8)
        statuses = [p["status"] for p in payloads(self, "CS-PR", "BootNotification", "CALLRESULT")]
        self.assertEqual((statuses[0], statuses[-1]), ("Pending", "Rejected"))

    async def test_pending_forever(self):
        self.csms.policy.update({"registration_mode": "pending"})
        st = self.station("CS-PP")
        await st.power_on()
        await until(lambda: len(self.frames("CS-PP", "CALL", "BootNotification")) >= 3)
        self.assertEqual(st.registration, "Pending")
