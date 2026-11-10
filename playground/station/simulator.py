"""Simulated charging station (WebSocket client), for OCPP 1.6, 2.0.1 and 2.1.

Life cycle (identical for every version):
  power on ─▶ connect, offering its subprotocols ─▶ BootNotification until Accepted
          ─▶ StatusNotification per connector ─▶ flush the offline transaction queue
          ─▶ Heartbeat / meter values / (2.1) event streams, until the connection drops
  connection lost ─▶ back-off with randomisation ─▶ reconnect without a new BootNotification
          ─▶ report connectors (all if offline > OfflineThreshold, else only the changed ones)

What the station *says* depends on the negotiated version: see behaviour.py (Behaviour2x / Behaviour16).
Behaviour marked PLAYGROUND DECISION is a simplification, not an OCPP rule.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from datetime import datetime, timezone

import websockets
from ocpp.exceptions import OCPPError

from ..events import Bus
from ..protocol import schemas
from ..protocol.frames import FrameError, MessageType, error_frame, parse
from ..protocol.versions import BY_SUBPROTOCOL, OcppVersion
from .behaviour import Behaviour2x as Behaviour2xEvent
from .behaviour import behaviour_for
from .handlers import station_class
from .model import CONFIG_16, Evse, default_variables
from .options import BY_KEY, OPTIONS
from .options import parse as parse_option
from .options import text as option_text

POWER_W = 11000          # simulated charging power


def now_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StationConnection:
    """Client WebSocket wrapper: remembers received CALLERROR ids, intercepts 2.1 CALLRESULTERROR."""

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
                self.station.bus.note(self.station.id, f"Station got CALLRESULTERROR {frame.error_code}: the CSMS "
                                                       "couldn't process one of its answers", "warn", "2.1 Part 4 §4.2.3")
                continue
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
        # Until the first handshake, assume the preferred offered version (decides the vocabulary).
        self.version: OcppVersion = BY_SUBPROTOCOL[self.offer[0]]
        self.behaviour = behaviour_for(self.version, self)
        self.registration = "—"            # what the CSMS last told *us*
        self.need_boot = True
        self.boot_reason = "PowerUp"
        self.boot_on_reconnect = False
        self.evses = {i: Evse(i) for i in range(1, evses + 1)}
        self.variables = default_variables(heartbeat)
        self.activity = "switched off"
        self.attempt = 0
        self.offline_since: float | None = None
        self.last_sent = 0.0               # any message resets the heartbeat timer (G02.FR.05 / 1.6 §4.6)
        self.last_heartbeat = 0.0
        self.tx_queue: list = []           # (label, coroutine function) waiting for the connection
        self.streams: dict[int, dict] = {} # 2.1 periodic event streams
        self.choices = {o.key: o.default for o in OPTIONS if not o.dm}   # implementation choices (no OCPP variable)
        self.auth_cache: dict[str, str] = {}   # idToken -> last status the CSMS gave (C10 / 1.6 §3.4)
        self.clock_offset = 0.0                # station clock minus real time (seconds)
        self.security_event: str | None = None # A04: StartupOfTheDevice / ResetOrReboot, sent once Accepted (2.x)
        self.pending_reset: str | None = None  # 2.x Reset OnIdle, done when the last transaction ends
        self.cp = None
        self.ws = None
        self._task: asyncio.Task | None = None
        self._kick = asyncio.Event()
        self._boot_now = asyncio.Event()
        self._stream_ids = iter(range(1, 10_000))
        for e in self.evses.values():
            e.status = self.behaviour.status_of(e)

    # ==================================================================== state for the dashboard
    def var(self, key: str):
        return self.variables[key][0]

    def opt(self, key: str):
        """The current value of an optional behaviour (Device Model variable or implementation choice)."""
        return self.variables[key][0] if key in self.variables else self.choices[key]

    def set_option(self, key: str, value) -> str:
        option = BY_KEY.get(key)
        if option is None:
            return "UnknownVariable"
        try:
            parsed = parse_option(option, value)
        except (TypeError, ValueError):
            return "Rejected"
        if option.dm:
            self.variables[key][0] = parsed
        else:
            self.choices[key] = parsed
        self.bus.changed()
        return "Accepted"

    def set_variable(self, key: str, value) -> str:
        entry = self.variables.get(key)
        if entry is None:
            return "UnknownVariable"
        if entry[1] != "ReadWrite":
            return "Rejected"
        if key in BY_KEY:
            return self.set_option(key, value)
        try:
            entry[0] = int(value) if isinstance(entry[0], int) else str(value)
        except ValueError:
            return "Rejected"
        self.bus.changed()
        return "Accepted"

    # -- the station's own clock (B01.FR.06 / G02.FR.06 / 1.6 §4.6) ------------------------------
    def clock(self) -> float:
        return time.time() + self.clock_offset

    def now_iso(self) -> str:
        return now_iso(datetime.fromtimestamp(self.clock(), timezone.utc))

    def sync_clock(self, current_time, source: str) -> None:
        if self.opt("ClockCtrlr.TimeSource") != "Heartbeat" or not current_time:
            return
        try:
            csms_time = datetime.fromisoformat(str(current_time).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return
        offset = csms_time - time.time()
        if abs(offset - self.clock_offset) > 2:
            self.bus.note(self.id, f"Clock set from {source}.currentTime: corrected by {offset - self.clock_offset:+.0f} s",
                          ref="1.6 §4.6" if self.version.name == "1.6" else "B01.FR.06 · G02.FR.06")
        self.clock_offset = offset

    def cache(self, token: str, status: str) -> None:
        if self.opt("AuthCacheCtrlr.Enabled"):
            self.auth_cache[token] = status
            self.bus.changed()

    def config_16(self, key: str):
        """(value, readonly) of a 1.6 configuration key, or None if unknown."""
        if key == "NumberOfConnectors":
            return str(len(self.evses)), True
        target = CONFIG_16.get(key)
        if target is None:
            return None
        if isinstance(target, tuple):
            return target[0], True
        if target in self.variables:
            return option_text(self.var(target)), self.variables[target][1] != "ReadWrite"
        return option_text(self.choices[target]), False

    @property
    def accepted(self) -> bool:
        return self.link and self.registration == "Accepted"

    def snapshot(self) -> dict:
        return {
            "power": self.power, "network_up": self.network_up, "link": self.link,
            "version": self.version.name if self.link else None, "family": self.behaviour.family,
            "offer": list(self.offer), "registration": self.registration, "boot_on_reconnect": self.boot_on_reconnect,
            "activity": self.activity, "attempt": self.attempt, "queued": [label for label, _ in self.tx_queue],
            "heartbeat": self.var("OCPPCommCtrlr.HeartbeatInterval"), "streams": list(self.streams),
            "options": {o.key: self.opt(o.key) for o in OPTIONS}, "auth_cache": dict(self.auth_cache),
            "clock_offset": round(self.clock_offset), "pending_reset": self.pending_reset,
            "evses": {str(i): {"status": e.status, "operative": e.operative, "cable": e.cable, "fault": e.fault,
                               "auth_token": e.auth_token, "ev_suspended": e.ev_suspended,
                               "evse_suspended": e.evse_suspended,
                               "tx": None if e.tx is None else {"id": e.tx.shown_id, "token": e.tx.token,
                                                                "charging": e.tx.charging,
                                                                "energy_wh": round(e.tx.energy_wh, 1), "seq": e.tx.seq}}
                      for i, e in self.evses.items()},
        }

    def _set(self, activity: str) -> None:
        self.activity = activity
        self.bus.changed()

    # ==================================================================== power & network
    async def power_on(self) -> None:
        if self.power:
            return
        self.power, self.need_boot, self.boot_reason, self.registration = True, True, "PowerUp", "—"
        self.attempt = 0
        self.clock_offset = float(self.opt("ClockErrorAtStart"))   # a power cut may leave the clock wrong
        self.security_event = "StartupOfTheDevice"
        self._task = asyncio.create_task(self._lifecycle())

    async def power_off(self) -> None:
        self.power = False
        await self._stop_task()
        for e in self.evses.values():
            if e.tx:
                self.bus.note(self.id, f"Power lost during transaction {e.tx.shown_id}: it's gone (simulator)")
                e.tx = None
            e.auth_token, e.auth_since, e.finishing, e.ev_suspended, e.evse_suspended = None, None, False, False, False
            e.status = self.behaviour.status_of(e)
        self.tx_queue.clear()
        self.pending_reset = None
        self.registration, self.streams = "—", {}
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

    async def reboot(self, reason: str, stop_reason: str | None = None) -> None:
        if self.version.is_2x and self.opt("ResetReportUnavailable") and self.accepted and self.cp:
            for e in self.evses.values():                        # B11.FR.03: MAY report Unavailable first
                e.status = "Unavailable"
            await self.call(self.cp, Behaviour2xEvent(self).availability_event(list(self.evses.values())))
            if e.tx:
                await self.behaviour.reset_stop(e, *([stop_reason] if stop_reason else []))
        self.security_event, self.pending_reset = "ResetOrReboot", None
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
        """2.x Part 4 §5.4: minimum + random, doubled after each failure up to RepeatTimes."""
        base = self.var("OCPPCommCtrlr.RetryBackOffWaitMinimum") * 2 ** min(
            self.attempt, self.var("OCPPCommCtrlr.RetryBackOffRepeatTimes"))
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
            except Exception as e:
                self.bus.note(self.id, f"Station error: {type(e).__name__}: {e}", "error")
            self.link, self.ws, self.cp = False, None, None
            self.bus.changed()
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
                self.bus.note(self.id, f"Station offered {list(self.offer)}, the CSMS agreed on none → closed", "warn",
                              "§3.2")
                await ws.wait_closed()
                return False
            if version is not self.version:
                self.version, self.behaviour = version, behaviour_for(version, self)
                for e in self.evses.values():
                    e.status = self.behaviour.status_of(e)
            self.ws, self.link = ws, True
            cp = station_class(version)(self.id, StationConnection(ws, self), response_timeout=10)
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
            await self._boot(cp, self.boot_reason if self.need_boot else "Unknown")
            self.need_boot, self.boot_reason = False, "PowerUp"
            await self.report(list(self.evses.values()), include_station=True, force=True)   # B01.FR.05
            if self.version.is_2x and self.security_event:                  # A04.FR.01: critical security event
                await self.call(cp, self.behaviour.security_event(self.security_event))
                self.security_event = None
        else:
            self.bus.note(self.id, "Reconnected without a new BootNotification: boot data unchanged",
                          ref=self.version.ref("§5.4"))
            threshold = self.var("OCPPCommCtrlr.OfflineThreshold")
            if self.version.is_2x and offline_for is not None and offline_for > threshold:
                self.bus.note(self.id, f"Offline {offline_for:.0f} s > OfflineThreshold {threshold} s → report all "
                                       "connectors", ref="B04.FR.01")
                await self.report(list(self.evses.values()), force=True)
            else:
                changed = [e for e in self.evses.values() if e.changed_while_offline]
                ref = "1.6 §4.9" if self.version.name == "1.6" else "B04.FR.02"
                self.bus.note(self.id, f"Offline {offline_for or 0:.0f} s → report the {len(changed)} connector(s) "
                                       "whose status changed", ref=ref)
                await self.report(changed, force=True)
        await self._flush_queue(cp)
        await asyncio.gather(self._heartbeat_loop(cp), self._meter_loop(), self._stream_loop(), self._aligned_loop())

    # ==================================================================== sending helpers
    async def call(self, cp, payload, **kwargs):
        """Send a CALL; returns the response, or None if the CSMS answered with an error."""
        uid = str(uuid.uuid4())
        self.last_sent = time.time()
        try:
            return await cp.call(payload, suppress=False, unique_id=uid, **kwargs)
        except OCPPError as e:
            name = payload.__class__.__name__
            if uid in cp._connection.received_errors:
                self.bus.note(self.id, f"CSMS answered {name} with CALLERROR {e.code}", "warn")
                return None
            code = schemas.code_for_library_error(e, self.version)
            if MessageType.CALLRESULTERROR in self.version.frame_types:
                cause = str((e.details or {}).get("cause", e.description))[:255]
                await cp._connection.send(error_frame(uid, code, cause, result_error=True).to_json())
                self.bus.note(self.id, f"CSMS's answer to {name} is schema-invalid → station sends CALLRESULTERROR {code}",
                              "warn", self.version.ref("§4.2.3"))
            else:
                self.bus.note(self.id, f"CSMS's answer to {name} is schema-invalid ({code}); OCPP {self.version.name} "
                                       "has no CALLRESULTERROR to report it", "warn")
            return None

    async def send_tx(self, send, label: str, quiet: bool = False) -> None:
        """Send a transaction-related message now, or queue it until connected and Accepted (E04/E12, 1.6 §3.5)."""
        if self.accepted and self.cp:
            await send(self.cp, False)
        else:
            self.tx_queue.append((label, send))
            if not quiet:
                self.bus.note(self.id, f"{label} queued (offline / not accepted): sent after the reconnect",
                              ref="1.6 §3.5" if self.version.name == "1.6" else "E04 / E12")
        self.bus.changed()

    async def _flush_queue(self, cp) -> None:
        # Remove a message only once it has been sent: if the connection drops mid-flush, it stays
        # queued for the next reconnect (at-least-once delivery, like OCPP's transaction-message retries).
        while self.tx_queue:
            label, send = self.tx_queue[0]
            await send(cp, True)
            self.tx_queue.pop(0)
            self.bus.changed()

    async def raw(self, text: str) -> None:
        if not self.ws:
            raise RuntimeError("not connected")
        self.last_sent = time.time()
        await self.ws.send(text)

    async def authorize(self, token: str) -> str:
        """Check a card: authorization cache, local pre-authorize, offline rules, else an Authorize request."""
        v16 = self.version.name == "1.6"
        cached = self.auth_cache.get(token) if self.opt("AuthCacheCtrlr.Enabled") else None
        if not self.accepted:
            if cached == "Accepted" and self.opt("AuthCtrlr.LocalAuthorizeOffline"):
                self.bus.note(self.id, f"Offline: card {token} is Accepted in the authorization cache → accepted locally",
                              ref="1.6 §3.5 (LocalAuthorizeOffline)" if v16 else "C12 · LocalAuthorizeOffline")
                return "Accepted"
            if cached is None and self.opt("AuthCtrlr.OfflineTxForUnknownIdEnabled"):
                self.bus.note(self.id, f"Offline: card {token} unknown, unknown cards allowed offline → accepted", "warn",
                              "1.6 §3.5 (AllowOfflineTxForUnknownId)" if v16 else "C15 · OfflineTxForUnknownIdEnabled")
                return "Accepted"
            self.bus.note(self.id, f"Offline: card {token} {'is ' + cached + ' in the cache' if cached else 'unknown'} "
                                   "→ refused locally", "warn", "1.6 §3.5" if v16 else "C12 / C15")
            return cached or "Invalid"
        if cached == "Accepted" and self.opt("AuthCtrlr.LocalPreAuthorize"):
            self.bus.note(self.id, f"Card {token} Accepted in the cache, LocalPreAuthorize = true → start without "
                                   "Authorize", ref="1.6 §3.4" if v16 else "C12 · LocalPreAuthorize")
            return "Accepted"
        resp = await self.call(self.cp, self.behaviour.authorize_payload(token))
        status = self.behaviour.authorize_status(resp) if resp else "Invalid"
        if resp:
            self.cache(token, status)
        if status != "Accepted":
            self.bus.note(self.id, f"Card {token}: {status} → no charging", "warn")
        return status

    # ==================================================================== boot & reports
    async def _boot(self, cp, reason: str) -> None:
        while True:
            self._set("sending BootNotification")
            resp = await self.call(cp, self.behaviour.boot_payload(reason))
            if resp is None:
                wait = self.opt("BootWaitWhenZero")
            else:
                self.registration = resp.status
                self.sync_clock(resp.current_time, "BootNotificationResponse")
                self.bus.changed()
                if resp.status == "Accepted":
                    if resp.interval > 0:
                        self.variables["OCPPCommCtrlr.HeartbeatInterval"][0] = resp.interval
                    return
                wait = resp.interval if resp.interval > 0 else self.opt("BootWaitWhenZero")   # B02.FR.06 / B03.FR.05
            self._set(f"{self.registration}: next BootNotification in {wait} s")
            try:
                await asyncio.wait_for(self._boot_now.wait(), wait)      # TriggerMessage may ask earlier
                self._boot_now.clear()
                reason = "Triggered"
            except asyncio.TimeoutError:
                pass

    async def report(self, evses: list[Evse], include_station: bool = False, force: bool = False) -> None:
        """Recompute statuses; report each one that changed (or all, with force).
        2.0.1: StatusNotification · 2.1: NotifyEvent AvailabilityState and/or StatusNotification · 1.6: StatusNotification,
        delayed by MinimumStatusDuration if set (§4.9)."""
        to_send = []
        for e in evses:
            new = self.behaviour.status_of(e)
            if force or new != e.status or e.changed_while_offline:
                to_send.append(e)
            e.status = new
        if not self.accepted:
            for e in to_send:
                e.changed_while_offline = True
            self.bus.changed()
            return
        delay = self.opt("MinimumStatusDuration") if self.version.name == "1.6" and not force else 0
        if delay:
            for e in to_send:
                asyncio.ensure_future(self._report_after(e, e.status, delay))
            self.bus.changed()
            return
        for payload in self.behaviour.status_payloads(to_send, include_station):
            await self.call(self.cp, payload)
        for e in to_send:
            e.changed_while_offline = False
        self.bus.changed()

    async def _report_after(self, e: Evse, status: str, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.behaviour.status_of(e) != status:
            self.bus.note(self.id, f"Connector {e.id}: {status} lasted < MinimumStatusDuration ({delay} s) → not reported",
                          ref="1.6 §4.9")
            return
        if self.accepted and self.cp:
            for payload in self.behaviour.status_payloads([e]):
                await self.call(self.cp, payload)

    async def send_report(self, cp, request_id: int) -> None:
        data = [{"component": {"name": k.split(".")[0]}, "variable": {"name": k.split(".")[1]},
                 "variable_attribute": [{"value": option_text(v[0]), "mutability": v[1]}]} for k, v in self.variables.items()]
        await self.call(cp, self.version.call.NotifyReport(request_id=request_id, generated_at=now_iso(), seq_no=0,
                                                           report_data=data, tbc=False))

    async def triggered(self, message: str, evse_id: int | None) -> None:
        C = self.version.call
        if message == "BootNotification":
            if self.registration == "Accepted":
                await self._boot(self.cp, "Triggered")
            else:
                self._boot_now.set()
        elif message == "Heartbeat":
            await self.heartbeat(self.cp)
        elif message == "StatusNotification":                    # the requested message, also on 2.1
            targets = [self.evses[evse_id]] if evse_id in self.evses else list(self.evses.values())
            for payload in self.behaviour.status_payloads(targets, mode="StatusNotification"):
                await self.call(self.cp, payload)
        elif message == "MeterValues":
            for e in ([self.evses[evse_id]] if evse_id in self.evses else self.evses.values()):
                await self.behaviour.triggered_meter(e)
        elif message == "TransactionEvent":
            for e in ([self.evses[evse_id]] if evse_id in self.evses else self.evses.values()):
                await self.behaviour.triggered_tx(e)

    async def after_transaction(self, e: Evse) -> None:
        if e.pending_operative is not None:                 # a "Scheduled" ChangeAvailability
            e.operative, e.pending_operative = e.pending_operative, None
            await self.report([e])
        if self.pending_reset and not any(x.tx for x in self.evses.values()):
            self.bus.note(self.id, "Last transaction ended → the scheduled reset (OnIdle) happens now", ref="B12.FR.01 / B12.FR.03")
            asyncio.ensure_future(self.reboot("RemoteReset"))

    # ==================================================================== periodic loops
    async def heartbeat(self, cp) -> None:
        self.last_heartbeat = time.time()
        resp = await self.call(cp, self.version.call.Heartbeat())
        if resp is not None:
            self.sync_clock(resp.current_time, "HeartbeatResponse")

    async def _heartbeat_loop(self, cp) -> None:
        self.last_heartbeat = time.time()
        while True:
            interval = self.var("OCPPCommCtrlr.HeartbeatInterval")
            self._set(f"{self.registration} · OCPP {self.version.name} · Heartbeat every {interval} s")
            # 1.6 §4.6 / G02.FR.05: any message may reset the timer; otherwise a Heartbeat every interval
            since = self.last_sent if self.opt("HeartbeatResetOnTraffic") else self.last_heartbeat
            await asyncio.sleep(max(0.2, min(1.0, since + interval - time.time())))
            since = self.last_sent if self.opt("HeartbeatResetOnTraffic") else self.last_heartbeat
            if time.time() >= since + self.var("OCPPCommCtrlr.HeartbeatInterval"):
                await self.heartbeat(cp)

    async def _meter_loop(self) -> None:
        last: dict[int, float] = {}
        while True:
            await asyncio.sleep(1)
            for e in self.evses.values():
                if e.tx and e.tx.charging:
                    e.tx.energy_wh += POWER_W / 3600
                    if time.time() - last.get(e.id, 0) >= self.var("SampledDataCtrlr.TxUpdatedInterval"):
                        last[e.id] = time.time()
                        await self.behaviour.meter(e)
                timeout = self.opt("TxCtrlr.EVConnectionTimeOut")
                if e.auth_token and not e.cable and e.auth_since and timeout and self.clock() - e.auth_since >= timeout:
                    await self.behaviour.timeout(e)
            self.bus.changed()

    async def _aligned_loop(self) -> None:
        """Clock-aligned meter values at round times of the station clock (1.6 §4.7, J01.FR.14)."""
        done = None
        while True:
            await asyncio.sleep(0.5)
            interval = self.opt("AlignedDataCtrlr.Interval")
            if not interval:
                continue
            slot = int(self.clock() // interval)
            if done is None:
                done = slot
            if slot != done:
                done = slot
                for e in self.evses.values():
                    await self.behaviour.aligned(e)

    async def _stream_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            for sid, s in list(self.streams.items()):
                s["buffer"].append((time.time(), f"{230 + random.uniform(-2.5, 2.5):.1f}"))
                if len(s["buffer"]) >= s["values"]:
                    base = s["buffer"][0][0]
                    payload = {"id": sid, "pending": 0, "basetime": now_iso(datetime.fromtimestamp(base, timezone.utc)),
                               "data": [{"t": round(t - base, 3), "v": v} for t, v in s["buffer"]]}
                    s["buffer"].clear()
                    await self.raw(json.dumps([6, str(uuid.uuid4()), "NotifyPeriodicEventStream", payload]))

    # ==================================================================== buttons on the station card
    async def plug(self, evse_id: int) -> None:
        e = self.evses[evse_id]
        if e.cable or e.fault or not e.operative:
            self.bus.note(self.id, f"Connector {evse_id}: can't plug in now ({e.status})", "warn")
            return
        await self.behaviour.plug(e)                     # the behaviour records the cable change

    async def unplug(self, evse_id: int) -> None:
        e = self.evses[evse_id]
        if e.cable:
            await self.behaviour.unplug(e)

    async def present_token(self, evse_id: int, token: str) -> None:
        await self.behaviour.token(self.evses[evse_id], token)

    async def set_fault(self, evse_id: int, fault: bool) -> None:
        e = self.evses[evse_id]
        e.fault = fault
        if fault:
            await self.behaviour.fault(e)
        else:
            await self.behaviour.repaired(e)

    async def ev_pause(self, evse_id: int, paused: bool) -> None:
        e = self.evses[evse_id]
        if not e.cable:
            self.bus.note(self.id, f"EVSE {evse_id}: no car connected", "warn")
            return
        await self.behaviour.ev_pause(e, paused)

    async def open_stream(self) -> None:
        if not self.link or self.version.name != "2.1":
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
    def edge_frames(self, uid: str) -> dict:
        v16 = self.version.name == "1.6"
        status_ok = ({"connectorId": 1, "errorCode": "NoError", "status": "Available"} if v16 else
                     {"timestamp": now_iso(), "connectorStatus": "Available", "evseId": 1, "connectorId": 1})
        return {
            "heartbeat_now": [2, uid, "Heartbeat", {}],
            "unknown_action": [2, uid, "MakeCoffee", {}],
            "unsupported_action": ([2, uid, "SignCertificate", {"csr": "x"}] if v16 else
                                   [2, uid, "LogStatusNotification", {"status": "Idle"}]),
            "malformed": '[2, "broken-1", "Heartbeat", {',
            "unreadable_id": [2, 12345, "Heartbeat", {}],
            "long_id": [2, "x" * 40, "Heartbeat", {}],
            "unknown_type": [7, uid, "Heartbeat", {}],
            "send_frame": [6, uid, "NotifyPeriodicEventStream",
                           {"id": 999, "pending": 0, "basetime": now_iso(), "data": [{"t": 0, "v": "230.0"}]}],
            "invalid_payload": [2, uid, "StatusNotification",
                                {**status_ok, ("status" if v16 else "connectorStatus"): "Exploded"}],
            "missing_field": [2, uid, "BootNotification", {"chargePointVendor": "x"} if v16 else
                              {"reason": "PowerUp", "chargingStation": {"vendorName": "x"}}],
            "wrong_type": [2, uid, "StatusNotification", {**status_ok, "connectorId": "1"}],
        }

    async def edge_case(self, name: str) -> None:
        uid = str(uuid.uuid4())
        if name == "duplicate_id":
            await self.raw(json.dumps([2, uid, "Heartbeat", {}]))
            await self.raw(json.dumps([2, uid, "Heartbeat", {}]))
            return
        frames = self.edge_frames(uid)
        if name not in frames:
            raise ValueError(f"unknown edge case {name}")
        frame = frames[name]
        await self.raw(frame if isinstance(frame, str) else json.dumps(frame))
