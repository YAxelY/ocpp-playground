"""OCPP-J RPC frames: parsing and serialising the JSON arrays that travel over the WebSocket.

    CALL             [2, "<id>", "<Action>", {payload}]                       request
    CALLRESULT       [3, "<id>", {payload}]                                   answer
    CALLERROR        [4, "<id>", "<errorCode>", "<description>", {details}]   error answer to a CALL
    CALLRESULTERROR  [5, "<id>", "<errorCode>", "<description>", {details}]   (2.1) error answer to a CALLRESULT
    SEND             [6, "<id>", "<Action>", {payload}]                       (2.1) unconfirmed, never answered

Sources: OCPP 2.0.1 Part 4 §4 · OCPP 2.1 Part 4 §4 (paraphrased).

This module is version-agnostic: it knows all five frame types. Which ones a connection
accepts is decided by `versions.OcppVersion.frame_types`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum

MAX_MESSAGE_ID_LENGTH = 36          # Part 4 §4.1.4: string[36], room for a UUID
UNREADABLE_ID = "-1"                # Part 4 §4.2.3: id to use when the message id can't be read


class MessageType(IntEnum):
    CALL = 2
    CALLRESULT = 3
    CALLERROR = 4
    CALLRESULTERROR = 5   # new in 2.1
    SEND = 6              # new in 2.1


# Number of array elements each frame type must have.
_LENGTH = {
    MessageType.CALL: 4,
    MessageType.CALLRESULT: 3,
    MessageType.CALLERROR: 5,
    MessageType.CALLRESULTERROR: 5,
    MessageType.SEND: 4,
}


@dataclass
class Frame:
    type: MessageType
    message_id: str
    action: str | None = None              # CALL and SEND only
    payload: dict = field(default_factory=dict)
    error_code: str | None = None          # CALLERROR and CALLRESULTERROR only
    error_description: str = ""
    error_details: dict = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.type in (MessageType.CALLERROR, MessageType.CALLRESULTERROR)

    def to_list(self) -> list:
        if self.type in (MessageType.CALL, MessageType.SEND):
            return [int(self.type), self.message_id, self.action, self.payload]
        if self.type is MessageType.CALLRESULT:
            return [int(self.type), self.message_id, self.payload]
        return [int(self.type), self.message_id, self.error_code, self.error_description[:255], self.error_details]

    def to_json(self) -> str:
        return json.dumps(self.to_list(), separators=(",", ":"))


class FrameError(Exception):
    """A text that can't be turned into a valid `Frame`.

    `code` is the RPC Framework error code that best describes the problem.
    `message_id` is None when the id itself couldn't be read (then "-1" is used in a reply).
    `type_id` is the MessageTypeId if it could be read (useful to decide how to react).
    """

    def __init__(self, code: str, description: str, message_id: str | None = None, type_id: object = None):
        super().__init__(f"{code}: {description}")
        self.code, self.description, self.message_id, self.type_id = code, description, message_id, type_id


def parse(raw: str) -> Frame:
    """Parse one OCPP-J text frame. Raises FrameError for anything that isn't a well-formed frame."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise FrameError("RpcFrameworkError", "frame is not valid JSON")
    if not isinstance(data, list) or not data:
        raise FrameError("RpcFrameworkError", "frame is not a non-empty JSON array")

    type_id = data[0]
    message_id = data[1] if len(data) > 1 and isinstance(data[1], str) else None
    if message_id is None:
        raise FrameError("RpcFrameworkError", "MessageId could not be read", None, type_id)
    if not message_id or len(message_id) > MAX_MESSAGE_ID_LENGTH:
        raise FrameError("RpcFrameworkError", f"MessageId must be 1..{MAX_MESSAGE_ID_LENGTH} characters", message_id, type_id)

    if not isinstance(type_id, int) or isinstance(type_id, bool) or type_id not in MessageType._value2member_map_:
        raise FrameError("MessageTypeNotSupported", f"MessageTypeId {type_id!r} is unknown", message_id, type_id)
    mtype = MessageType(type_id)

    if len(data) != _LENGTH[mtype]:
        raise FrameError("ProtocolError", f"{mtype.name} must have {_LENGTH[mtype]} elements, got {len(data)}", message_id, type_id)

    if mtype in (MessageType.CALL, MessageType.SEND):
        action, payload = data[2], data[3]
        if not isinstance(action, str) or not action:
            raise FrameError("ProtocolError", "Action must be a non-empty string", message_id, type_id)
        if not isinstance(payload, dict):
            raise FrameError("ProtocolError", "payload must be a JSON object", message_id, type_id)
        return Frame(mtype, message_id, action=action, payload=payload)

    if mtype is MessageType.CALLRESULT:
        if not isinstance(data[2], dict):
            raise FrameError("ProtocolError", "payload must be a JSON object", message_id, type_id)
        return Frame(mtype, message_id, payload=data[2])

    code, description, details = data[2], data[3], data[4]
    if not isinstance(code, str) or not isinstance(description, str) or not isinstance(details, dict):
        raise FrameError("ProtocolError", "error frame needs (string, string, object)", message_id, type_id)
    return Frame(mtype, message_id, error_code=code, error_description=description, error_details=details)


def error_frame(message_id: str | None, code: str, description: str = "", details: dict | None = None,
                *, result_error: bool = False) -> Frame:
    """Build a CALLERROR (or, with result_error=True, a 2.1 CALLRESULTERROR)."""
    mtype = MessageType.CALLRESULTERROR if result_error else MessageType.CALLERROR
    return Frame(mtype, message_id or UNREADABLE_ID, error_code=code,
                 error_description=description, error_details=details or {})


def peek(raw: str) -> dict:
    """Best-effort description of any text frame, valid or not (for logging and the dashboard)."""
    try:
        frame = parse(raw)
        return {"kind": frame.type.name, "uid": frame.message_id, "action": frame.action,
                "error_code": frame.error_code, "payload": frame.payload if not frame.is_error else
                {"errorCode": frame.error_code, "description": frame.error_description}}
    except FrameError as e:
        kind = "INVALID"
        if e.code == "MessageTypeNotSupported":
            kind = f"TYPE {e.type_id}"
        return {"kind": kind, "uid": e.message_id or "?", "action": None, "error_code": None,
                "payload": None, "problem": e.description}
