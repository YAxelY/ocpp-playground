"""Wires everything together: event bus, CSMS, simulated stations and the dashboard."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .csms.server import Csms
from .events import Bus
from .protocol import schemas
from .station.simulator import Station
from .web.server import Dashboard


class Playground:
    def __init__(self, host: str = "localhost", csms_port: int = 9000, ui_port: int = 8000):
        self.host, self.csms_port, self.ui_port = host, csms_port, ui_port
        self.bus = Bus()
        self.csms = Csms(self.bus)
        self.stations: dict[str, Station] = {}
        self.bus.snapshot = self.snapshot
        self.dashboard = Dashboard(self)

    @property
    def csms_url(self) -> str:
        return f"ws://{self.host}:{self.csms_port}/ocpp"

    def add_station(self, offer: list[str], evses: int = 1, station_id: str | None = None) -> Station:
        n = len(self.stations) + 1
        station_id = station_id or f"CS-{n:03d}"
        station = Station(station_id, self.bus, self.csms_url, evses=evses, offer=tuple(offer))
        self.stations[station_id] = station
        self.bus.changed()
        return station

    def snapshot(self) -> dict:
        csms = self.csms.snapshot()
        return {
            "policy": self.csms.policy.to_dict(),
            "stations": [{"id": sid, "station": st.snapshot(), "csms": csms.get(sid)} for sid, st in self.stations.items()],
        }

    async def run(self, autostart: bool = True) -> None:
        csms_server = await self.csms.serve(self.host, self.csms_port)
        ui_server = await self.dashboard.serve(self.host, self.ui_port)
        self.add_station(["ocpp2.1", "ocpp2.0.1"], evses=1)
        self.add_station(["ocpp2.0.1"], evses=2)
        self.add_station(["ocpp1.6"], evses=2)
        print(f"CSMS  {self.csms_url}/<station-id>   (OCPP {', '.join(self.csms.policy.versions)})", flush=True)
        print(f"Dashboard  http://{self.host}:{self.ui_port}", flush=True)
        if autostart:
            await self.stations["CS-001"].power_on()
        try:
            await asyncio.Future()
        finally:
            csms_server.close()
            ui_server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="OCPP 1.6 / 2.0.1 / 2.1 playground: mini CSMS + simulated stations")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--csms-port", type=int, default=9000)
    parser.add_argument("--ui-port", type=int, default=8000)
    parser.add_argument("--no-autostart", action="store_true", help="don't power on CS-001 at start")
    parser.add_argument("--schemas", action="append", default=[], metavar="VERSION=DIR",
                        help="use official OCA schemas, e.g. --schemas 2.0.1=~/OCPP-2.0.1_part3_JSON_schemas")
    args = parser.parse_args()
    for item in args.schemas:
        name, _, directory = item.partition("=")
        schemas.use_directory(name, os.path.expanduser(directory))
        print(f"OCPP {name} schemas: {directory}")
    logging.getLogger("ocpp").setLevel(logging.CRITICAL)      # everything is shown in the dashboard
    try:
        asyncio.run(Playground(args.host, args.csms_port, args.ui_port).run(not args.no_autostart))
    except KeyboardInterrupt:
        print("\nbye")
