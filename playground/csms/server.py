"""The CSMS: WebSocket server, version negotiation, and one guarded connection per station.

    station ──ws──▶ handshake (subprotocol) ──▶ GuardedConnection ──▶ gatekeeper ──▶ ocpp library ──▶ handlers
                                                      ▲                                   │
                                                      └──────────── replies ◀─────────────┘
"""
from __future__ import annotations

import asyncio
import json

import websockets

from ..events import Bus
from ..protocol import frames, schemas, versions
from ..protocol.versions import OcppVersion
from . import gatekeeper
from .handlers import csms_class, handled_actions
from .state import Policy, StationRecord


class GuardedConnection:
    """Wraps the WebSocket the library reads from: logs every frame and runs the gatekeeper first."""

    def __init__(self, ws, record: StationRecord, version: OcppVersion, csms: "Csms", handled: set[str]):
        self.ws, self.rec, self.version, self.csms, self.handled = ws, record, version, csms, handled
        self.actions: dict[str, str] = {}      # message id -> action (to label answers in the log)
        self.received_errors: set[str] = set() # ids of CALLERRORs the station sent us

    def _log(self, raw: str, direction: str) -> dict:
        info = frames.peek(raw)
        if info["kind"] in ("CALL", "SEND") and info["action"]:
            self.actions[info["uid"]] = info["action"]
        elif info["uid"] in self.actions:
            info["action"] = self.actions[info["uid"]]
        if direction == "to_csms" and info["kind"] == "CALLERROR":
            self.received_errors.add(info["uid"])
        self.csms.bus.frame(self.rec.id, direction, self.version.name, info, raw)
        return info

    async def recv(self) -> str:
        while True:
            raw = await self.ws.recv()
            self.rec.seen()                               # any message proves the station is alive (G02.FR.04)
            self._log(raw, "to_csms")
            verdict = gatekeeper.inspect(raw, self.version, self.rec, self.csms.policy, self.handled)
            for text, level, ref in verdict.notes:
                self.csms.bus.note(self.rec.id, text, level, ref)
            if verdict.send:
                self.csms.process_send(self.rec, verdict.send)
            if verdict.reply:
                await self.send(verdict.reply.to_json())
            if verdict.forward:
                return verdict.forward

    async def send(self, raw: str) -> None:
        raw = self.csms.inject_faults(raw, self)
        info = self._log(raw, "to_station")
        if info["kind"] == "CALLRESULT":
            self.rec.sent_results[info["uid"]] = info.get("action") or "?"
            if len(self.rec.sent_results) > 200:
                self.rec.sent_results.pop(next(iter(self.rec.sent_results)))
        await self.ws.send(raw)


class Csms:
    def __init__(self, bus: Bus, policy: Policy | None = None):
        self.bus = bus
        self.policy = policy or Policy()
        self.stations: dict[str, StationRecord] = {}
        self.connections: dict[str, object] = {}       # station id -> live ChargePoint (CSMS end)

    # -- WebSocket handshake -------------------------------------------------------------------
    def select_subprotocol(self, connection, offered):
        """Part 4 §3.2: pick one offered subprotocol, or none (then we close right after the handshake)."""
        version = versions.select(list(offered), self.policy.versions)
        return version.subprotocol if version else None

    async def handle(self, ws) -> None:
        station_id = ws.request.path.rstrip("/").split("/")[-1] or "unknown"
        offered = ws.request.headers.get("Sec-WebSocket-Protocol", "")
        version = versions.BY_SUBPROTOCOL.get(ws.subprotocol or "")
        if version is None:
            self.bus.note(station_id, f"Handshake: station offered [{offered or 'nothing'}], CSMS has "
                                      f"{self.policy.versions} enabled → no common version, connection closed",
                          "warn", "Part 4 §3.2")
            await ws.close(1002, "no supported OCPP version")
            return
        if station_id in self.connections:
            self.bus.note(station_id, "A second connection with the same identity: the old one is replaced", "warn")
            old = self.connections[station_id]
            asyncio.ensure_future(old._connection.ws.close(1000, "replaced"))

        rec = self.stations.setdefault(station_id, StationRecord(id=station_id))
        rec.version, rec.online = version.name, True
        rec.seen()
        rec.seen_ids.clear()
        cls = csms_class(version)
        handled = handled_actions(cls) & schemas.store(version).request_actions
        conn = GuardedConnection(ws, rec, version, self, handled)
        cp = cls(station_id, conn, response_timeout=15)
        cp.csms, cp.rec, cp.version = self, rec, version
        self.connections[station_id] = cp
        self.bus.note(station_id, f"WebSocket open, OCPP {version.name} agreed (offered: {offered}) → Online",
                      ref="Part 4 §3")
        self.bus.changed()
        try:
            await cp.start()
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.connections.get(station_id) is cp:
                del self.connections[station_id]
                rec.online = False
                self.bus.note(station_id, "WebSocket closed → Offline (registration is kept for a reconnect)")
                self.bus.changed()

    # -- 2.1 SEND messages ------------------------------------------------------------------------
    def process_send(self, rec: StationRecord, frame) -> None:
        if frame.action == "NotifyPeriodicEventStream":
            p = frame.payload
            stream = rec.streams.get(p["id"])
            if stream is None:
                self.bus.note(rec.id, f"SEND for stream {p['id']} that was never opened: ignored", "warn")
                return
            for item in p["data"]:
                stream["values"].append({"basetime": p["basetime"], "t": item["t"], "v": item["v"]})
            self.bus.changed()

    # -- fault injection --------------------------------------------------------------------------
    def inject_faults(self, raw: str, conn: GuardedConnection) -> str:
        if not self.policy.fault_bad_heartbeat_response:
            return raw
        info = frames.peek(raw)
        if info["kind"] == "CALLRESULT" and conn.actions.get(info["uid"]) == "Heartbeat":
            data = json.loads(raw)
            data[2].pop("currentTime", None)              # required field removed → schema-invalid response
            self.bus.note(conn.rec.id, "FAULT INJECTED: HeartbeatResponse sent without currentTime", "warn")
            return json.dumps(data, separators=(",", ":"))
        return raw

    # -- misc ----------------------------------------------------------------------------------
    def forget(self) -> None:
        """Forget registrations so the next boot counts as a first boot (keeps live connections)."""
        for sid in [s for s, r in self.stations.items() if not r.online]:
            del self.stations[sid]
        for rec in self.stations.values():
            rec.boots, rec.registration = 0, "Unknown"
            rec.allowed_once.clear()
        self.bus.changed()

    def snapshot(self) -> dict:
        return {sid: rec.snapshot() for sid, rec in self.stations.items()}

    async def serve(self, host: str, port: int):
        return await websockets.serve(
            self.handle, host, port,
            subprotocols=[v.subprotocol for v in versions.ALL],
            select_subprotocol=self.select_subprotocol,
        )
