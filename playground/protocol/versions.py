"""Supported OCPP versions and what differs between them at the RPC-framework level."""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from functools import cached_property

from .frames import MessageType


@dataclass(frozen=True)
class OcppVersion:
    name: str                    # "2.0.1"
    subprotocol: str             # WebSocket subprotocol, Part 4 §3.1.2
    frame_types: frozenset       # MessageTypes this version knows
    answers_unknown_type: bool   # 2.0.1 Part 4 §4.4: answer MessageTypeNotSupported. 2.1 Part 4 §4.4: ignore
    lib_package: str             # the `ocpp` library sub-package for this version

    @cached_property
    def _pkg(self):
        return importlib.import_module(self.lib_package)

    @property
    def ChargePoint(self):
        return self._pkg.ChargePoint

    @cached_property
    def call(self):
        return importlib.import_module(self.lib_package + ".call")

    @cached_property
    def call_result(self):
        return importlib.import_module(self.lib_package + ".call_result")

    @property
    def schema_dir(self) -> str:
        return os.path.join(os.path.dirname(self._pkg.__file__), "schemas")

    def __str__(self) -> str:
        return f"OCPP {self.name}"


V201 = OcppVersion(
    name="2.0.1", subprotocol="ocpp2.0.1",
    frame_types=frozenset({MessageType.CALL, MessageType.CALLRESULT, MessageType.CALLERROR}),
    answers_unknown_type=True, lib_package="ocpp.v201",
)
V21 = OcppVersion(
    name="2.1", subprotocol="ocpp2.1",
    frame_types=frozenset(MessageType),
    answers_unknown_type=False, lib_package="ocpp.v21",
)

ALL: tuple[OcppVersion, ...] = (V21, V201)
BY_NAME = {v.name: v for v in ALL}
BY_SUBPROTOCOL = {v.subprotocol: v for v in ALL}

# Subprotocols that exist (IANA-registered) but that this playground never speaks.
KNOWN_BUT_UNSUPPORTED = ("ocpp1.6", "ocpp2.0", "ocpp1.5", "ocpp1.2")


def select(offered: list[str], enabled: list[str]) -> OcppVersion | None:
    """Server-side negotiation: the first version, in the *server's* preference order, that the client offered.

    `enabled` is a list of version names, e.g. ["2.1", "2.0.1"].
    """
    for name in enabled:
        version = BY_NAME.get(name)
        if version and version.subprotocol in offered:
            return version
    return None
