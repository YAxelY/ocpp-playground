"""Event bus: everything the dashboard shows (frames, notes, state snapshots) goes through here."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable


def now_ms() -> int:
    return int(time.time() * 1000)


class Bus:
    def __init__(self, history: int = 1000):
        self.history: deque = deque(maxlen=history)
        self.subscribers: set[asyncio.Queue] = set()
        self.snapshot: Callable[[], dict] = lambda: {}
        self._dirty = False

    # -- publishing ---------------------------------------------------------------------------
    def _publish(self, event: dict, keep: bool = True) -> None:
        if keep:
            self.history.append(event)
        for q in list(self.subscribers):
            q.put_nowait(event)

    def frame(self, station: str, direction: str, version: str | None, info: dict, raw: str) -> None:
        """A frame on the wire. direction: 'to_csms' or 'to_station'."""
        self._publish({"type": "frame", "t": now_ms(), "station": station, "dir": direction,
                       "version": version, "raw": raw, **info})

    def note(self, station: str, text: str, level: str = "info", ref: str | None = None) -> None:
        """An explanation shown between the frames. `ref` = spec reference, e.g. 'B02.FR.09'."""
        self._publish({"type": "note", "t": now_ms(), "station": station, "text": text, "level": level, "ref": ref})

    def changed(self) -> None:
        """State changed: send one fresh snapshot (coalesced to at most one per 100 ms)."""
        if self._dirty:
            return
        self._dirty = True
        try:
            asyncio.get_running_loop().call_later(0.1, self._flush)
        except RuntimeError:          # no loop (unit tests)
            self._dirty = False

    def _flush(self) -> None:
        self._dirty = False
        self._publish({"type": "state", **self.snapshot()}, keep=False)

    def send_to(self, queue: asyncio.Queue, event: dict) -> None:
        queue.put_nowait(event)

    # -- subscribing --------------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
