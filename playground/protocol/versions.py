"""Supported OCPP versions and everything that differs between them at the RPC-framework level.

| | 1.6 | 2.0.1 | 2.1 |
|---|---|---|---|
| subprotocol | ocpp1.6 | ocpp2.0.1 | ocpp2.1 |
| frame types | 2, 3, 4 | 2, 3, 4 | 2, 3, 4, 5, 6 |
| unknown MessageTypeId | ignore (J §4.1.3) | CALLERROR MessageTypeNotSupported (§4.4) | ignore (§4.4) |
| unreadable message id | not specified | CALLERROR with id "-1" (§4.2.3) | same as 2.0.1 |
| message-id uniqueness | per WebSocket connection (J §4.1.4) | per station identity, all connections (§4.1.4) | same as 2.0.1 |
| schema file names | Action.json / ActionResponse.json | ActionRequest.json / ActionResponse.json | same as 2.0.1 |
| error codes | J §4.2.3 table (incl. the spelling "FormationViolation", "OccurenceConstraintViolation") | Part 4 §4.3 | Part 4 §4.3 |
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from functools import cached_property

from .frames import MessageType

# Logical error names used by the code → the exact string each version defines.
_CODES_2X = {
    "rpc": "RpcFrameworkError", "format": "FormatViolation", "occurrence": "OccurrenceConstraintViolation",
    "property": "PropertyConstraintViolation", "type": "TypeConstraintViolation", "protocol": "ProtocolError",
    "not_implemented": "NotImplemented", "not_supported": "NotSupported", "security": "SecurityError",
    "unknown_type": "MessageTypeNotSupported", "generic": "GenericError", "internal": "InternalError",
}
_CODES_16 = {
    **_CODES_2X,
    "rpc": "GenericError",                          # 1.6 has no RpcFrameworkError (PLAYGROUND DECISION)
    "format": "FormationViolation",                 # sic, as spelled in OCPP-J 1.6 §4.2.3
    "occurrence": "OccurenceConstraintViolation",   # sic
    "unknown_type": None,                           # 1.6 has no MessageTypeNotSupported
}


@dataclass(frozen=True)
class OcppVersion:
    name: str
    subprotocol: str
    frame_types: frozenset
    answers_unknown_type: bool          # answer an unknown MessageTypeId with an error (only 2.0.1)
    defines_unreadable_id: bool         # spec defines the "-1" reply (2.0.1, 2.1)
    ids_unique_per_identity: bool       # 2.x: across connections; 1.6: per connection
    request_suffix: str                 # schema file name suffix for requests
    lib_package: str
    codes: dict = field(default_factory=dict, compare=False, hash=False)

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
    def library_schema_dir(self) -> str:
        return os.path.join(os.path.dirname(self._pkg.__file__), "schemas")

    @property
    def is_2x(self) -> bool:
        return self.name != "1.6"

    def code(self, logical: str) -> str | None:
        return self.codes[logical]

    def ref(self, section: str) -> str:
        """Cite a transport-spec section in this version's document: '1.6 J §4.2.3' / '2.1 Part 4 §4.4'."""
        return f"{self.name} {'OCPP-J' if self.name == '1.6' else 'Part 4'} {section}"

    def __str__(self) -> str:
        return f"OCPP {self.name}"


V16 = OcppVersion(
    name="1.6", subprotocol="ocpp1.6",
    frame_types=frozenset({MessageType.CALL, MessageType.CALLRESULT, MessageType.CALLERROR}),
    answers_unknown_type=False, defines_unreadable_id=False, ids_unique_per_identity=False,
    request_suffix="", lib_package="ocpp.v16", codes=_CODES_16,
)
V201 = OcppVersion(
    name="2.0.1", subprotocol="ocpp2.0.1",
    frame_types=frozenset({MessageType.CALL, MessageType.CALLRESULT, MessageType.CALLERROR}),
    answers_unknown_type=True, defines_unreadable_id=True, ids_unique_per_identity=True,
    request_suffix="Request", lib_package="ocpp.v201", codes=_CODES_2X,
)
V21 = OcppVersion(
    name="2.1", subprotocol="ocpp2.1",
    frame_types=frozenset(MessageType),
    answers_unknown_type=False, defines_unreadable_id=True, ids_unique_per_identity=True,
    request_suffix="Request", lib_package="ocpp.v21", codes=_CODES_2X,
)

ALL: tuple[OcppVersion, ...] = (V21, V201, V16)
BY_NAME = {v.name: v for v in ALL}
BY_SUBPROTOCOL = {v.subprotocol: v for v in ALL}

# Subprotocols that exist (IANA-registered) but that this playground never speaks.
KNOWN_BUT_UNSUPPORTED = ("ocpp2.0", "ocpp1.5", "ocpp1.2")


def select(offered: list[str], enabled: list[str]) -> OcppVersion | None:
    """Server-side negotiation: the first version, in the *server's* preference order, that the client offered."""
    for name in enabled:
        version = BY_NAME.get(name)
        if version and version.subprotocol in offered:
            return version
    return None
