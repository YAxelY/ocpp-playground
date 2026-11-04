"""Simulated charging station (WebSocket client).

Life cycle:
  power on ─▶ connect (offers its subprotocols) ─▶ BootNotification until Accepted
          ─▶ StatusNotification per connector ─▶ flush offline transaction queue
          ─▶ Heartbeat / meter values / (2.1) event streams, until the connection drops
  connection lost ─▶ back-off with randomisation (Part 4 §5.3) ─▶ reconnect without a new
          BootNotification ─▶ report connectors (B04: all if offline > OfflineThreshold, else changed ones)

Behaviour marked PLAYGROUND DECISION is a simplification, not an OCPP rule.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

import websockets
from ocpp.exceptions import OCPPError

from ..events import Bus
from ..protocol import schemas
from ..protocol.frames import FrameError, MessageType, error_frame, parse
from ..protocol.versions import BY_SUBPROTOCOL, OcppVersion
from .handlers import station_class
from .model import Evse, TxState, default_variables

POWER_W = 11000          # simulated charging power


def now_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StationConnection:
    """Thin wrapper around the client WebSocket: remembers received CALLERROR ids, intercepts 2.1 frames."""

    def __init__(self, ws, station: "Station"):
        self.ws, self.station = ws, station
        self.received_errors: set[str] = set()

    async def recv(self) -> str:
        while True:
            raw = await self.ws.recv()
            try:
                frame = parse(raw)
            except FrameError:
                return raw
            if frame.type is MessageType.CALLERROR:
                self.received_errors.add(frame.message_id)
            if frame.type is MessageType.CALLRESULTERROR:
                self.station.bus.note(self.station.id, f"Station got CALLRESULTERROR {frame.error_code} for one of its "
                                                       "answers", "warn", "2.1 Part 4 §4.2.3")
                continue          # the library doesn't know type 5
            return raw

    async def send(self, raw: str) -> None:
        await self.ws.send(raw)


class Station:
    def __init__(self, station_id: str, bus: Bus, url: str, evses: int = 1,
                 offer: tuple[str, ...] = ("ocpp2.1", "ocpp2.0.1"), heartbeat: int = 10):
        self.id, self.bus, self.url, self.offer = station_id, bus, url.rstrip("/"), tuple(offer)
        self.power = False
        self.network_up = True
        self.link = False
        self.version: OcppVersion | None = None
        self.registration = "—"            # what the CSMS last told *us*
        self.need_boot = True
        self.boot_reason = "PowerUp"
        self.boot_on_reconnect = False
        self.evses = {i: Evse(i) for i in range(1, evses + 1)}
        self.variables = default_variables(heartbeat)
        self.activity = "switched off"
        self.attempt = 0
        self.offline_since: float | None = None
        self.last_sent = 0.0               # G02.FR.05: any message resets the heartbeat timer
        self.tx_queue: list[dict] = []     # TransactionEvents that happened while offline (E04/E12)
        self.streams: dict[int, dict] = {} # 2.1 periodic event streams we opened
        self.cp = None
        self.ws = None
        self._task: asyncio.Task | None = None
        self._kick = asyncio.Event()       # skip the back-off wait (power on, reboot)
        self._boot_now = asyncio.Event()   # TriggerMessage(BootNotification) while Pending (B02.FR.04)
        self._stream_ids = iter(range(1, 10_000))

    # ==================================================================== dashboard view
    def var(self, key: str):
        return self.variables[key][0]

    def snapshot(self) -> dict:
        return {
            "power": self.power, "network_up": self.network_up, "link": self.link,
            "version": self.version.name if self.version else None, "offer": list(self.offer),
            "registration": self.registration, "boot_on_reconnect": self.boot_on_reconnect,
            "activity": self.activity, "attempt": self.attempt, "queued": len(self.tx_queue),
            "variables": {k: v[0] for k, v in self.variables.items()},
            "streams": list(self.streams),
            "evses": {str(i): {"status": e.status, "operative": e.operative, "cable": e.cable, "fault": e.fault,
                               "tx": None if e.tx is None else {"id": e.tx.id, "token": e.tx.token,
                                                                "charging": e.tx.charging,
                                                                "energy_wh": round(e.tx.energy_wh, 1),
                                                                "seq": e.tx.seq}}
                      for i, e in self.evses.items()},
        }

    def _set(self, activity: str) -> None:
        self.activity = activity
        self.bus.changed()

    # ==================================================================== power & network buttons
    async def power_on(self) -> None:
        if self.power:
            return
        self.power, self.need_boot, self.boot_reason, self.registration = True, True, "PowerUp", "—"
        self.attempt = 0
        self._task = asyncio.create_task(self._lifecycle())

    async def power_off(self) -> None:
        self.power = False
        await self._stop_task()
        for e in self.evses.values():
            if e.tx:
                self.bus.note(self.id, f"Power lost during transaction {e.tx.id}: it's gone (simulator)")
                e.tx = None
        self.registration, self.version, self.streams = "—", None, {}
        self._set("switched off")

    async def drop_network(self) -> None:
        self.network_up = False
        if self.ws:
            await self.ws.close(1001, "network lost")
        self.bus.note(self.id, "Network down: the station keeps running and keeps trying to reconnect")
        self.bus.changed()

    async def restore_network(self) -> None:
        self.network_up = True
        self.bus.note(self.id, "Network back: the next reconnect attempt (after its back-off) will succeed")
        self.bus.changed()

    async def reboot(self, cp, reason: str) -> None:
        for e in self.evses.values():
            if e.tx:
                await self.end_transaction(cp, e, "ResetCommand", "ImmediateReset")
        self.need_boot, self.boot_reason, self.registration = True, reason, "—"
        self.attempt = 0
        self._kick.set()
        if self.ws:
            await self.ws.close(1000, "reboot")

    async def _stop_task(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.link, self.ws, self.cp = False, None, None

    # ==================================================================== connection life cycle
    def _backoff(self) -> float:
        """Part 4 §5.3: minimum + random, doubled after each failure up to RepeatTimes, then constant."""
        base = self.var("OCPPCommCtrlr.RetryBackOffWaitMinimum") * 2 ** min(self.attempt,
                                                                           self.var("OCPPCommCtrlr.RetryBackOffRepeatTimes"))
        return base + random.uniform(0, self.var("OCPPCommCtrlr.RetryBackOffRandomRange"))

    async def _lifecycle(self) -> None:
        while self.power:
            connected = False
            try:
                if not self.network_up:
                    raise ConnectionError("network down")
                connected = await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, websockets.InvalidHandshake, asyncio.TimeoutError) as e:
                self.bus.note(self.id, f"Connect attempt {self.attempt + 1} failed: {e}", "warn")
            except websockets.ConnectionClosed:
                pass
            except Exception as e:                       # keep the simulator alive, show the problem
                self.bus.note(self.id, f"Station error: {type(e).__name__}: {e}", "error")
            self.link, self.ws, self.cp = False, None, None
            if not self.power:
                break
            if self.offline_since is None:
                self.offline_since = time.time()
            if connected:
                self.attempt = 0
            if self._kick.is_set():
                self._kick.clear()
                continue
            wait = self._backoff()
            self.attempt += 1
            self._set(f"offline · reconnect attempt {self.attempt} in {wait:.1f} s")
            try:
                await asyncio.wait_for(self._kick.wait(), wait)
                self._kick.clear()
            except asyncio.TimeoutError:
                pass

    async def _session(self) -> bool:
        self._set("connecting…")
        async with websockets.connect(f"{self.url}/{self.id}", subprotocols=list(self.offer), open_timeout=5) as ws:
            version = BY_SUBPROTOCOL.get(ws.subprotocol or "")
            if version is None:
                self.bus.note(self.id, f"Station offered {list(self.offer)}, the CSMS agreed on none → closed",
                              "warn", "Part 4 §3.2")
                await ws.wait_closed()
                return False
            self.version, self.ws, self.link = version, ws, True
            cls = station_class(version)
            cp = cls(self.id, StationConnection(ws, self), response_timeout=10)
            cp.station = self
            self.cp = cp
            self.bus.changed()
            listener = asyncio.create_task(cp.start())
            life = asyncio.create_task(self._life(cp))
            try:
                done, _ = await asyncio.wait({listener, life}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                listener.cancel()
                life.cancel()
            return True

    async def _life(self, cp) -> None:
        offline_for = time.time() - self.offline_since if self.offline_since else None
        self.offline_since = None
        if self.need_boot or self.registration != "Accepted" or self.boot_on_reconnect:
            reason = self.boot_reason if self.need_boot else "Unknown"
            await self._boot(cp, reason)
            self.need_boot, self.boot_reason = False, "PowerUp"
            await self.report_status(cp, list(self.evses.values()))          # B01.FR.05
        else:
            self.bus.note(self.id, "Reconnected without a new BootNotification: boot data unchanged",
                          ref="Part 4 §5.3")
            threshold = self.var("OCPPCommCtrlr.OfflineThreshold")
            if offline_for is not None and offline_for > threshold:
                self.bus.note(self.id, f"Offline {offline_for:.0f} s > OfflineThreshold {threshold} s → report all "
                                       "connectors", ref="B04.FR.01")
                await self.report_status(cp, list(self.evses.values()))
            else:
                changed = [e for e in self.evses.values() if e.changed_while_offline]
                self.bus.note(self.id, f"Offline {offline_for or 0:.0f} s ≤ OfflineThreshold → report only "
                                       f"{len(changed)} changed connector(s)", ref="B04.FR.02")
                await self.report_status(cp, changed)
        await self._flush_queue(cp)
        await asyncio.gather(self._heartbeat_loop(cp), self._meter_loop(cp), self._stream_loop(cp))

    # ==================================================================== sending helpers
    async def call(self, cp, payload, **kwargs):
        """Send a CALL; returns the response, or None if the CSMS answered with an error."""
        uid = str(uuid.uuid4())
        self.last_sent = time.time()
        try:
            return await cp.call(payload, suppress=False, unique_id=uid, **kwargs)
        except OCPPError as e:
            if uid in cp._connection.received_errors:
                self.bus.note(self.id, f"CSMS answered {payload.__class__.__name__} with CALLERROR {e.code}", "warn")
            elif self.version and MessageType.CALLRESULTERROR in self.version.frame_types:
                code = schemas.code_for_library_error(e)
                cause = str((e.details or {}).get("cause", e.description))[:255]
                await cp._connection.send(error_frame(uid, code, cause, result_error=True).to_json())
                self.bus.note(self.id, f"CSMS's answer to {payload.__class__.__name__} is schema-invalid → station "
                                       f"sends CALLRESULTERROR {code}", "warn", "2.1 Part 4 §4.2.3")
            else:
                self.bus.note(self.id, f"CSMS's answer to {payload.__class__.__name__} is schema-invalid "
                                       f"({schemas.code_for_library_error(e)}); OCPP 2.0.1 has no CALLRESULTERROR "
                                       "to report it", "warn")
            return None

    async def raw(self, text: str) -> None:
        if not self.ws:
            raise RuntimeError("not connected")
        self.last_sent = time.time()
        await self.ws.send(text)

    # ==================================================================== boot & reports
    async def _boot(self, cp, reason: str) -> None:
        C = self.version.call
        while True:
            self._set("sending BootNotification")
            resp = await self.call(cp, C.BootNotification(
                charging_station={"model": self.var("ChargingStation.Model"),
                                  "vendor_name": self.var("ChargingStation.VendorName"),
                                  "firmware_version": "1.0.0"}, reason=reason))
            if resp is None:
                wait = 10
            else:
                self.registration = resp.status
                self.bus.changed()
                if resp.status == "Accepted":
                    if resp.interval > 0:                          # B01.FR.04
                        self.variables["OCPPCommCtrlr.HeartbeatInterval"][0] = resp.interval
                    return
                # B02.FR.07 / B03.FR.05: interval 0 → choose our own. PLAYGROUND DECISION: 10 s
                wait = resp.interval if resp.interval > 0 else 10
            self._set(f"{self.registration}: next BootNotification in {wait} s")
            try:                                                   # B02.FR.04: TriggerMessage may ask earlier
                await asyncio.wait_for(self._boot_now.wait(), wait)
                self._boot_now.clear()
                reason = "Triggered"
            except asyncio.TimeoutError:
                pass

    async def report_status(self, cp, evses: list[Evse]) -> None:
        if not (self.link and self.registration == "Accepted"):
            for e in evses:
                e.status = e.computed_status()
                e.changed_while_offline = True
            self.bus.changed()
            return
        for e in evses:
            e.status = e.computed_status()
            e.changed_while_offline = False
            await self.call(cp, self.version.call.StatusNotification(
                timestamp=now_iso(), connector_status=e.status, evse_id=e.id, connector_id=1))
        self.bus.changed()

    async def send_report(self, cp, request_id: int) -> None:
        data = [{"component": {"name": k.split(".")[0]}, "variable": {"name": k.split(".")[1]},
                 "variable_attribute": [{"value": str(v[0]), "mutability": v[1]}]} for k, v in self.variables.items()]
        await self.call(cp, self.version.call.NotifyReport(request_id=request_id, generated_at=now_iso(), seq_no=0,
                                                           report_data=data, tbc=False))

    async def triggered(self, cp, message: str, evse_id: int | None) -> None:
        if message == "BootNotification":
            if self.registration == "Accepted":
                await self._boot(cp, "Triggered")
            else:
                self._boot_now.set()
        elif message == "Heartbeat":
            await self.call(cp, self.version.call.Heartbeat())
        elif message == "StatusNotification":
            targets = [self.evses[evse_id]] if evse_id in self.evses else list(self.evses.values())
            for e in targets:                    # a triggered report is allowed even if not Accepted
                await self.call(cp, self.version.call.StatusNotification(
                    timestamp=now_iso(), connector_status=e.computed_status(), evse_id=e.id, connector_id=1))

    # ==================================================================== periodic loops
    async def _heartbeat_loop(self, cp) -> None:
        while True:
            interval = self.var("OCPPCommCtrlr.HeartbeatInterval")
            self._set(f"{self.registration} · OCPP {self.version.name} · Heartbeat every {interval} s")
            due = self.last_sent + interval
            await asyncio.sleep(max(0.2, min(1.0, due - time.time())))
            if time.time() >= self.last_sent + self.var("OCPPCommCtrlr.HeartbeatInterval"):
                await self.call(cp, self.version.call.Heartbeat())

    async def _meter_loop(self, cp) -> None:
        last_sent: dict[int, float] = {}
        while True:
            await asyncio.sleep(1)
            for e in self.evses.values():
                if e.tx and e.tx.charging:
                    e.tx.energy_wh += POWER_W / 3600
                    if time.time() - last_sent.get(e.id, 0) >= self.var("SampledDataCtrlr.TxUpdatedInterval"):
                        last_sent[e.id] = time.time()
                        await self._tx_event(cp, e, "Updated", "MeterValuePeriodic", "Charging")
            self.bus.changed()

    async def _stream_loop(self, cp) -> None:
        while True:
            await asyncio.sleep(1)
            for sid, s in list(self.streams.items()):
                s["buffer"].append((time.time(), f"{230 + random.uniform(-2.5, 2.5):.1f}"))
                if len(s["buffer"]) >= s["values"]:
                    base = s["buffer"][0][0]
                    payload = {"id": sid, "pending": 0,
                               "basetime": now_iso(datetime.fromtimestamp(base, timezone.utc)),
                               "data": [{"t": round(t - base, 3), "v": v} for t, v in s["buffer"]]}
                    s["buffer"].clear()
                    # SEND (2.1): no answer expected, may be sent while a CALL is open (2.1 Part 4 §4.1.1 note)
                    await self.raw(json.dumps([6, str(uuid.uuid4()), "NotifyPeriodicEventStream", payload]))

    # ==================================================================== transactions (E02/E03/E07/E09)
    async def _tx_event(self, cp, evse: Evse, event_type: str, trigger: str, charging_state: str,
                        token: str | None = None, stopped_reason: str | None = None) -> None:
        tx = evse.tx
        info = {"transaction_id": tx.id, "charging_state": charging_state}
        if stopped_reason:
            info["stopped_reason"] = stopped_reason
        if tx.remote_start_id is not None and trigger == "RemoteStart":
            info["remote_start_id"] = tx.remote_start_id          # links the event to RequestStartTransaction
        kwargs = dict(event_type=event_type, timestamp=now_iso(), trigger_reason=trigger, seq_no=tx.next_seq(),
                      transaction_info=info, evse={"id": evse.id, "connector_id": 1},
                      meter_value=[{"timestamp": now_iso(), "sampled_value": [{
                          "value": round(tx.energy_wh, 1), "measurand": "Energy.Active.Import.Register",
                          "unit_of_measure": {"unit": "Wh"}}]}])
        if token:
            kwargs["id_token"] = {"id_token": token, "type": "ISO14443"}
        if not (self.link and self.registration == "Accepted" and cp):
            kwargs["offline"] = True                                 # E04: happened while offline
            self.tx_queue.append(kwargs)
            self.bus.note(self.id, f"TransactionEvent {event_type} queued (offline), will be sent later",
                          ref="E04 / E12")
            self.bus.changed()
            return
        resp = await self.call(cp, self.version.call.TransactionEvent(**kwargs))
        if resp is not None and resp.id_token_info and resp.id_token_info.get("status") not in (None, "Accepted"):
            self.bus.note(self.id, f"CSMS says token {token} is {resp.id_token_info['status']}", "warn")
        self.bus.changed()

    async def _flush_queue(self, cp) -> None:
        while self.tx_queue:
            kwargs = self.tx_queue.pop(0)
            await self.call(cp, self.version.call.TransactionEvent(**kwargs))
        self.bus.changed()

    async def authorize(self, cp, token: str) -> str:
        if not (self.link and self.registration == "Accepted"):
            # PLAYGROUND DECISION: accept unknown tokens while offline (C15 makes this configurable)
            self.bus.note(self.id, f"Offline: token {token} accepted locally", "warn", "C15")
            return "Accepted"
        resp = await self.call(cp, self.version.call.Authorize(id_token={"id_token": token, "type": "ISO14443"}))
        return resp.id_token_info["status"] if resp else "Invalid"

    async def plug(self, evse_id: int) -> None:
        cp, e = self.cp, self.evses[evse_id]
        if e.cable or e.fault or not e.operative:
            self.bus.note(self.id, f"EVSE {evse_id}: can't plug in now ({e.computed_status()})", "warn")
            return
        e.cable = True
        await self.report_status(cp, [e])
        if e.tx is None:                                         # E02: cable first
            e.tx = TxState()
            await self._tx_event(cp, e, "Started", "CablePluggedIn", "EVConnected")
        else:                                                    # remote start / token came first
            e.tx.charging = bool(e.tx.token)
            await self._tx_event(cp, e, "Updated", "CablePluggedIn", "Charging" if e.tx.charging else "EVConnected")

    async def present_token(self, evse_id: int, token: str) -> None:
        cp, e = self.cp, self.evses[evse_id]
        if e.tx and e.tx.token and e.tx.token == token:          # E07: same token again → stop
            await self.end_transaction(cp, e, "StopAuthorized", "Local", token=token)
            return
        if e.tx and e.tx.token:
            self.bus.note(self.id, f"EVSE {evse_id}: another token is charging here", "warn")
            return
        status = await self.authorize(cp, token)
        if status != "Accepted":
            self.bus.note(self.id, f"Token {token}: {status} → no charging", "warn", "C01")
            return
        if e.tx is None:                                         # E03: token first, wait for the cable
            e.tx = TxState(token=token)
            await self._tx_event(cp, e, "Started", "Authorized", "Idle", token=token)
        else:
            e.tx.token, e.tx.charging = token, True
            await self._tx_event(cp, e, "Updated", "Authorized", "Charging", token=token)

    async def remote_start(self, cp, e: Evse, token: str, remote_start_id: int) -> None:
        if e.tx is None:                                         # F02: remote start first
            e.tx = TxState(token=token, remote_start_id=remote_start_id)
            await self._tx_event(cp, e, "Started", "RemoteStart", "Idle", token=token)
        else:                                                    # F01: cable already plugged
            e.tx.token, e.tx.charging, e.tx.remote_start_id = token, True, remote_start_id
            await self._tx_event(cp, e, "Updated", "RemoteStart", "Charging", token=token)

    async def end_transaction(self, cp, e: Evse, trigger: str, reason: str, token: str | None = None) -> None:
        if e.tx is None:
            return
        e.tx.charging = False
        await self._tx_event(cp, e, "Ended", trigger, "EVConnected" if e.cable else "Idle", token=token,
                             stopped_reason=reason)
        e.tx = None
        if e.pending_operative is not None:                      # Scheduled ChangeAvailability
            e.operative, e.pending_operative = e.pending_operative, None
            await self.report_status(cp, [e])

    async def unplug(self, evse_id: int) -> None:
        cp, e = self.cp, self.evses[evse_id]
        if not e.cable:
            return
        if e.tx:                                                 # E09: cable unplugged on EV side → stop
            await self.end_transaction(cp, e, "EVCommunicationLost", "EVDisconnected")
        e.cable = False
        await self.report_status(cp, [e])

    async def set_fault(self, evse_id: int, fault: bool) -> None:
        cp, e = self.cp, self.evses[evse_id]
        e.fault = fault
        if fault and e.tx:
            await self.end_transaction(cp, e, "AbnormalCondition", "Other")
        await self.report_status(cp, [e])

    # ==================================================================== 2.1 event streams (N11/N12)
    async def open_stream(self) -> None:
        if not self.version or self.version.name != "2.1":
            self.bus.note(self.id, "Periodic event streams only exist in OCPP 2.1", "warn")
            return
        sid = next(self._stream_ids)
        resp = await self.call(self.cp, self.version.call.OpenPeriodicEventStream(constant_stream_data={
            "id": sid, "variable_monitoring_id": 1, "params": {"interval": 3, "values": 3}}))
        if resp and resp.status == "Accepted":
            self.streams[sid] = {"values": 3, "buffer": []}
            self.bus.changed()

    async def close_stream(self) -> None:
        for sid in list(self.streams):
            await self.call(self.cp, self.version.call.ClosePeriodicEventStream(id=sid))
            del self.streams[sid]
        self.bus.changed()

    # ==================================================================== deliberate protocol mistakes
    async def edge_case(self, name: str) -> None:
        uid = str(uuid.uuid4())
        frames = {
            "heartbeat_now": [2, uid, "Heartbeat", {}],
            "unknown_action": [2, uid, "MakeCoffee", {}],
            "unsupported_action": [2, uid, "SecurityEventNotification",
                                   {"type": "SettingSystemTime", "timestamp": now_iso()}],
            "duplicate_id": None,
            "malformed": '[2, "broken-1", "Heartbeat", {',
            "unreadable_id": [2, 12345, "Heartbeat", {}],
            "long_id": [2, "x" * 40, "Heartbeat", {}],
            "unknown_type": [7, uid, "Heartbeat", {}],
            "send_frame": [6, uid, "NotifyPeriodicEventStream",
                           {"id": 999, "pending": 0, "basetime": now_iso(), "data": [{"t": 0, "v": "230.0"}]}],
            "invalid_payload": [2, uid, "StatusNotification",
                                {"timestamp": now_iso(), "connectorStatus": "Exploded", "evseId": 1, "connectorId": 1}],
            "missing_field": [2, uid, "BootNotification", {"reason": "PowerUp", "chargingStation": {"vendorName": "x"}}],
            "wrong_type": [2, uid, "StatusNotification",
                           {"timestamp": now_iso(), "connectorStatus": "Available", "evseId": "1", "connectorId": 1}],
        }
        if name not in frames:
            raise ValueError(f"unknown edge case {name}")
        if name == "duplicate_id":
            await self.raw(json.dumps([2, uid, "Heartbeat", {}]))
            await self.raw(json.dumps([2, uid, "Heartbeat", {}]))
            return
        frame = frames[name]
        await self.raw(frame if isinstance(frame, str) else json.dumps(frame))
