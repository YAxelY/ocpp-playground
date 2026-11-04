"""Dashboard: serves the static web page and a WebSocket API (/ws) for live events and commands.

Client → server messages (JSON):
  {"cmd": "station", "id": "CS-001", "action": "plug", "evse": 1}       button on a station card
  {"cmd": "csms", "id": "CS-001", "command": "reset", "args": {...}}      CSMS-initiated request
  {"cmd": "policy", ...fields}                                            change CSMS behaviour
  {"cmd": "add_station", "offer": ["ocpp2.1"], "evses": 1}
  {"cmd": "schema_diff", "name": "TransactionEventRequest"}               2.0.1 ↔ 2.1 comparison
  {"cmd": "forget"} · {"cmd": "clear_log"}
Server → client: {"type": "hello" | "frame" | "note" | "state" | "schema_diff" | "schema_list", ...}
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from http import HTTPStatus
from pathlib import Path

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from ..csms import commands
from ..protocol import V21, V201, schemas

STATIC = Path(__file__).parent / "static"

STATION_ACTIONS = {
    "power_on": lambda st, m: st.power_on(),
    "power_off": lambda st, m: st.power_off(),
    "drop_network": lambda st, m: st.drop_network(),
    "restore_network": lambda st, m: st.restore_network(),
    "plug": lambda st, m: st.plug(int(m["evse"])),
    "unplug": lambda st, m: st.unplug(int(m["evse"])),
    "token": lambda st, m: st.present_token(int(m["evse"]), m.get("token") or "AXEL-0001"),
    "fault": lambda st, m: st.set_fault(int(m["evse"]), True),
    "clear_fault": lambda st, m: st.set_fault(int(m["evse"]), False),
    "open_stream": lambda st, m: st.open_stream(),
    "close_stream": lambda st, m: st.close_stream(),
    "edge": lambda st, m: st.edge_case(m["name"]),
}


class Dashboard:
    def __init__(self, playground):
        self.pg = playground

    # -- HTTP -------------------------------------------------------------------------------------
    def process_request(self, connection, request):
        path = request.path.split("?")[0]
        if path == "/ws":
            return None                                            # let the WebSocket upgrade happen
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        file = (STATIC / name).resolve()
        if STATIC.resolve() not in file.parents or not file.is_file():
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        body = file.read_bytes()
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        headers = Headers([("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
        return Response(HTTPStatus.OK.value, "OK", headers, body)

    async def serve(self, host: str, port: int):
        return await websockets.serve(self.handle, host, port, process_request=self.process_request)

    # -- WebSocket API ----------------------------------------------------------------------------
    async def handle(self, ws) -> None:
        bus = self.pg.bus
        queue = bus.subscribe()
        try:
            await ws.send(json.dumps({"type": "hello", "history": list(bus.history), **self.pg.snapshot(),
                                      "meta": self.meta()}))
            pusher = asyncio.create_task(self._push(ws, queue))
            try:
                async for text in ws:
                    msg = json.loads(text)
                    task = asyncio.create_task(self.dispatch(msg, queue))
                    task.add_done_callback(lambda t, m=msg: self._report(t, m))
            finally:
                pusher.cancel()
        except websockets.ConnectionClosed:
            pass
        finally:
            bus.unsubscribe(queue)

    async def _push(self, ws, queue) -> None:
        while True:
            await ws.send(json.dumps(await queue.get(), default=str))

    def _report(self, task: asyncio.Task, msg: dict) -> None:
        if not task.cancelled() and task.exception():
            e = task.exception()
            self.pg.bus.note(msg.get("id", "playground"), f"'{msg.get('action') or msg.get('command') or msg.get('cmd')}' "
                                                          f"failed: {type(e).__name__}: {e}", "error")

    async def dispatch(self, msg: dict, queue) -> None:
        pg, cmd = self.pg, msg.get("cmd")
        if cmd == "station":
            station = pg.stations[msg["id"]]
            if msg["action"] == "boot_on_reconnect":
                station.boot_on_reconnect = bool(msg.get("value"))
            else:
                await STATION_ACTIONS[msg["action"]](station, msg)
        elif cmd == "csms":
            await commands.run(pg.csms, msg["id"], msg["command"], msg.get("args") or {})
        elif cmd == "policy":
            pg.csms.policy.update({k: v for k, v in msg.items() if k != "cmd"})
            pg.bus.note("CSMS", "Policy changed: " + ", ".join(f"{k}={v}" for k, v in msg.items() if k != "cmd"))
        elif cmd == "add_station":
            pg.add_station(msg.get("offer") or ["ocpp2.1", "ocpp2.0.1"], int(msg.get("evses", 1)))
        elif cmd == "forget":
            pg.csms.forget()
            pg.bus.note("CSMS", "Registrations forgotten: the next BootNotification counts as a first boot")
        elif cmd == "clear_log":
            pg.bus.history.clear()
        elif cmd == "schema_diff":
            d = schemas.diff(schemas.store(V201), schemas.store(V21), msg["name"])
            d["old"] = schemas.store(V201).load(msg["name"]) if d["only_in"] != "2.1" else None
            d["new"] = schemas.store(V21).load(msg["name"]) if d["only_in"] != "2.0.1" else None
            queue.put_nowait({"type": "schema_diff", **d})
            return
        pg.bus.changed()

    _meta: dict | None = None

    @classmethod
    def meta(cls) -> dict:
        if cls._meta is None:
            a, b = schemas.store(V201), schemas.store(V21)
            files = sorted(set(f[:-5] for f in os.listdir(a.dir) if f.endswith(".json")) |
                           set(f[:-5] for f in os.listdir(b.dir) if f.endswith(".json")))
            status = {}
            for name in files:
                d = schemas.diff(a, b, name)
                status[name] = f"only {d['only_in']}" if d["only_in"] else "same" if schemas.is_unchanged(d) else "changed"
            cls._meta = {"schemas": files, "schema_status": status, "commands": sorted(commands.BUILDERS)}
        return cls._meta
