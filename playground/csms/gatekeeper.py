"""Gatekeeper: every incoming frame passes here *before* the `ocpp` library sees it.

The library only understands CALL / CALLRESULT / CALLERROR and makes its own choices for errors.
The gatekeeper applies the protocol rules explicitly, per negotiated version (1.6, 2.0.1, 2.1),
with a spec reference for each, and decides one of:

  forward  → hand the raw frame to the library (handler dispatch / call correlation)
  reply    → answer immediately with this frame (usually a CALLERROR) and don't forward
  send     → a valid 2.1 SEND message to process; never answered
  nothing  → drop (with a note explaining why)

It's a pure function of (raw text, version, station record, policy, handled actions), so it can be
unit-tested without any network.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..protocol import schemas
from ..protocol.frames import Frame, FrameError, MessageType, error_frame, parse
from ..protocol.versions import OcppVersion
from .state import Policy, StationRecord

BOOT = "BootNotification"


@dataclass
class Verdict:
    forward: str | None = None
    reply: Frame | None = None
    send: Frame | None = None
    notes: list = field(default_factory=list)       # (text, level, ref)
    frame: Frame | None = None

    def note(self, text: str, level: str = "info", ref: str | None = None) -> "Verdict":
        self.notes.append((text, level, ref))
        return self


def inspect(raw: str, version: OcppVersion, record: StationRecord, policy: Policy,
            handled_actions: set[str]) -> Verdict:
    store = schemas.store(version)
    try:
        frame = parse(raw)
    except FrameError as e:
        return _broken_frame(e, version, policy)

    v = Verdict(frame=frame)
    if frame.type not in version.frame_types:                     # 5 and 6 exist only in 2.1
        return _unknown_type(frame.message_id, int(frame.type), version, v)

    if frame.type is MessageType.CALL:
        return _call(raw, frame, version, store, record, policy, handled_actions, v)

    if frame.type in (MessageType.CALLRESULT, MessageType.CALLERROR):
        v.forward = raw                                            # the library correlates answers by id
        return v

    if frame.type is MessageType.CALLRESULTERROR:
        action = record.sent_results.get(frame.message_id, "?")
        return v.note(f"Station could not process our CALLRESULT for {action}: {frame.error_code} "
                      f"({frame.error_description})", "warn", version.ref("§4.2.3"))

    # SEND (2.1)
    if frame.action not in store.send_actions:
        return v.note(f"SEND with unknown action '{frame.action}' dropped: a SEND is never answered", "warn",
                      version.ref("§4.2.4"))
    violation = store.validate(frame.action, "send", frame.payload)
    if violation:
        return v.note(f"SEND {frame.action} breaks its schema ({violation.keyword} at {violation.path or 'root'}): "
                      "dropped silently, a SEND is never answered", "warn", version.ref("§4.2.4"))
    v.send = frame
    return v


# ---------------------------------------------------------------------------------------------
def _broken_frame(e: FrameError, version: OcppVersion, policy: Policy) -> Verdict:
    v = Verdict()
    if e.code == "unknown_type":
        return _unknown_type(e.message_id, e.type_id, version, v)
    if e.message_id is None:
        if policy.unreadable_frames == "drop":
            return v.note(f"Unreadable frame ({e.description}) dropped (policy: drop)", "warn", version.ref("§4.2.3"))
        v.reply = error_frame(None, version.code("rpc"), e.description)
        if version.defines_unreadable_id:
            return v.note(f"Unreadable frame ({e.description}) → CALLERROR with MessageId \"-1\"", "warn",
                          version.ref("§4.2.3"))
        return v.note(f"Unreadable frame ({e.description}) → CALLERROR \"-1\" {version.code('rpc')}: OCPP 1.6 defines "
                      "no id for this case, so this reply is a playground decision", "warn", "decision")
    if e.type_id == MessageType.CALL:
        v.reply = error_frame(e.message_id, version.code(e.code), e.description)
        return v.note(f"Malformed CALL → CALLERROR {version.code(e.code)}", "warn", version.ref("§4.2.3"))
    if e.type_id == MessageType.CALLRESULT and MessageType.CALLRESULTERROR in version.frame_types:
        v.reply = error_frame(e.message_id, version.code(e.code), e.description, result_error=True)
        return v.note(f"Malformed CALLRESULT → CALLRESULTERROR {version.code(e.code)}", "warn", version.ref("§4.2.3"))
    return v.note(f"Malformed frame ({e.description}) dropped: no error frame exists to answer it", "warn")


def _unknown_type(message_id, type_id, version: OcppVersion, v: Verdict) -> Verdict:
    if version.answers_unknown_type:
        v.reply = error_frame(message_id, version.code("unknown_type"),
                              f"MessageTypeId {type_id} is not supported by OCPP {version.name}")
        return v.note(f"MessageTypeId {type_id} unknown in OCPP {version.name} → CALLERROR MessageTypeNotSupported",
                      "warn", version.ref("§4.4"))
    section = "§4.1.3" if version.name == "1.6" else "§4.4"
    return v.note(f"MessageTypeId {type_id} unknown in OCPP {version.name} → payload ignored, no answer", "warn",
                  version.ref(section))


def _call(raw: str, frame: Frame, version: OcppVersion, store, record: StationRecord, policy: Policy,
          handled: set[str], v: Verdict) -> Verdict:
    action = frame.action

    if frame.message_id in record.seen_ids and policy.duplicate_ids == "reject":
        v.reply = error_frame(frame.message_id, version.code("rpc"), "MessageId already used by this sender")
        scope = "this charging station identity" if version.ids_unique_per_identity else "this connection"
        return v.note(f"MessageId {frame.message_id[:12]}… reused on {scope} → CALLERROR {version.code('rpc')} "
                      "(policy: reject)", "warn", version.ref("§4.1.4"))
    record.seen_ids.append(frame.message_id)

    if action not in store.request_actions:
        v.reply = error_frame(frame.message_id, version.code("not_implemented"),
                              f"Action {action} is not known by the receiver")
        return v.note(f"Unknown action '{action}' → CALLERROR NotImplemented", "warn",
                      version.ref("§4.2.3 table" if version.name == "1.6" else "§4.3"))

    if action != BOOT and record.registration != "Accepted":
        if action in record.allowed_once:
            record.allowed_once.discard(action)
            v.note(f"{action} allowed although not Accepted: the CSMS asked for it", "info",
                   "1.6 §4.2" if version.name == "1.6" else "B02.FR.02")
        else:
            v.reply = error_frame(frame.message_id, version.code("security"),
                                  f"Charging station is not accepted (registration: {record.registration})")
            if version.name == "1.6":
                ref = "1.6 §4.2 (station rule) · reply = decision"
            else:
                ref = {"Rejected": "B03.FR.07", "Pending": "B02.FR.09"}.get(record.registration, "B01.FR.08 / B01.FR.10")
            return v.note(f"{action} from a station that isn't Accepted ({record.registration}) → "
                          "CALLERROR SecurityError", "warn", ref)

    violation = store.validate(action, "request", frame.payload)
    if violation:
        v.reply = error_frame(frame.message_id, violation.code,
                              f"{store.schema_name(action, 'request')}: {violation.message}"[:255], violation.details())
        return v.note(f"{action} payload invalid ({violation.keyword} at {violation.path or 'root'}) → "
                      f"CALLERROR {violation.code}", "warn", version.ref("§4.2.3" if version.name == "1.6" else "§4.3"))

    if action not in handled:
        v.reply = error_frame(frame.message_id, version.code("not_supported"), f"{action} is recognised but not supported")
        return v.note(f"{action} is valid OCPP {version.name} but not implemented here → CALLERROR NotSupported",
                      "info", version.ref("§4.2.3 table" if version.name == "1.6" else "§4.3"))

    v.forward = raw
    return v
