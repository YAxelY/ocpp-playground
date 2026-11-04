"""Gatekeeper: every incoming frame passes here *before* the `ocpp` library sees it.

The library only understands CALL / CALLRESULT / CALLERROR and makes its own choices for
errors. The gatekeeper applies the protocol rules explicitly, with a spec reference for each,
and decides one of:

  forward  → hand the raw frame to the library (normal handler dispatch / call correlation)
  reply    → answer immediately with this frame (usually a CALLERROR) and don't forward
  send     → a valid 2.1 SEND message to process; never answered
  nothing  → drop (with a note explaining why)

It's a pure function of (raw text, version, station record, policy, handled actions), so it can
be unit-tested without any network.
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
    frame: Frame | None = None                       # parsed frame, when parsing worked

    def note(self, text: str, level: str = "info", ref: str | None = None) -> "Verdict":
        self.notes.append((text, level, ref))
        return self


def inspect(raw: str, version: OcppVersion, record: StationRecord, policy: Policy,
            handled_actions: set[str]) -> Verdict:
    store = schemas.store(version)

    # 1. Is it a well-formed frame at all? ---------------------------------------------------
    try:
        frame = parse(raw)
    except FrameError as e:
        return _broken_frame(e, version, policy)

    v = Verdict(frame=frame)

    # 2. Does this OCPP version know this frame type? (5 and 6 only exist in 2.1) ------------
    if frame.type not in version.frame_types:
        return _unknown_type(frame.message_id, int(frame.type), version, policy, v)

    # 3. Dispatch per frame type -------------------------------------------------------------
    if frame.type is MessageType.CALL:
        return _call(raw, frame, version, store, record, policy, handled_actions, v)

    if frame.type in (MessageType.CALLRESULT, MessageType.CALLERROR):
        v.forward = raw          # answers to our own CALLs: the library correlates them by id
        return v

    if frame.type is MessageType.CALLRESULTERROR:
        action = record.sent_results.get(frame.message_id, "?")
        return v.note(f"Station could not process our CALLRESULT for {action}: {frame.error_code} "
                      f"({frame.error_description})", "warn", "2.1 Part 4 §4.2.3")

    # SEND (2.1)
    if frame.action not in store.send_actions:
        return v.note(f"SEND with unknown action '{frame.action}' dropped. A SEND is never answered",
                      "warn", "2.1 Part 4 §4.2.4")
    violation = store.validate(frame.action, "send", frame.payload)
    if violation:
        return v.note(f"SEND {frame.action} violates its schema ({violation.code} at "
                      f"{violation.path or 'root'}): dropped silently, a SEND is never answered", "warn",
                      "2.1 Part 4 §4.2.4")
    v.send = frame
    return v


# ---------------------------------------------------------------------------------------------
def _broken_frame(e: FrameError, version: OcppVersion, policy: Policy) -> Verdict:
    v = Verdict()
    if e.code == "MessageTypeNotSupported":
        return _unknown_type(e.message_id, e.type_id, version, policy, v)
    if e.message_id is None:
        if policy.unreadable_frames == "drop":
            return v.note(f"Unreadable frame ({e.description}) dropped (policy: drop)", "warn", "Part 4 §4.2.3")
        v.reply = error_frame(None, "RpcFrameworkError", e.description)
        return v.note(f"Unreadable frame ({e.description}) → CALLERROR with MessageId \"-1\"", "warn",
                      "Part 4 §4.2.3")
    if e.type_id == MessageType.CALL:
        v.reply = error_frame(e.message_id, e.code, e.description)
        return v.note(f"Malformed CALL → CALLERROR {e.code}", "warn", "Part 4 §4.2.3")
    if e.type_id == MessageType.CALLRESULT and MessageType.CALLRESULTERROR in version.frame_types:
        v.reply = error_frame(e.message_id, e.code, e.description, result_error=True)
        return v.note(f"Malformed CALLRESULT → CALLRESULTERROR {e.code}", "warn", "2.1 Part 4 §4.2.3")
    return v.note(f"Malformed frame ({e.description}) dropped: no error frame exists to answer it", "warn")


def _unknown_type(message_id, type_id, version: OcppVersion, policy: Policy, v: Verdict) -> Verdict:
    if version.answers_unknown_type:
        v.reply = error_frame(message_id, "MessageTypeNotSupported",
                              f"MessageTypeId {type_id} is not supported by OCPP {version.name}")
        return v.note(f"MessageTypeId {type_id} unknown in OCPP {version.name} → CALLERROR MessageTypeNotSupported",
                      "warn", "2.0.1 Part 4 §4.4")
    return v.note(f"MessageTypeId {type_id} unknown in OCPP {version.name} → payload ignored, no answer",
                  "warn", "2.1 Part 4 §4.4")


def _call(raw: str, frame: Frame, version: OcppVersion, store, record: StationRecord, policy: Policy,
          handled: set[str], v: Verdict) -> Verdict:
    action = frame.action

    # message id reuse
    if frame.message_id in record.seen_ids and policy.duplicate_ids == "reject":
        v.reply = error_frame(frame.message_id, "RpcFrameworkError", "MessageId already used by this sender")
        return v.note(f"MessageId {frame.message_id[:12]}… reused → CALLERROR (policy: reject)", "warn",
                      "Part 4 §4.1.4")
    record.seen_ids.append(frame.message_id)

    # action must exist in this OCPP version
    if action not in store.request_actions:
        v.reply = error_frame(frame.message_id, "NotImplemented", f"Action {action} is not known by the receiver")
        return v.note(f"Unknown action '{action}' → CALLERROR NotImplemented", "warn", "Part 4 §4.3")

    # registration gate: nothing but BootNotification until Accepted
    if action != BOOT and record.registration != "Accepted":
        if action in record.allowed_once:
            record.allowed_once.discard(action)
            v.note(f"{action} allowed although not Accepted: the CSMS asked for it", "info", "B02.FR.02")
        else:
            v.reply = error_frame(frame.message_id, "SecurityError",
                                  f"Station is not accepted (registration: {record.registration})")
            # Unknown = connected but never booted with us: the station breaks B01.FR.08; answering
            # SecurityError like B01.FR.10 is a PLAYGROUND DECISION for that case.
            ref = {"Rejected": "B03.FR.07", "Pending": "B02.FR.09"}.get(record.registration, "B01.FR.08 / B01.FR.10")
            return v.note(f"{action} from a station that isn't Accepted ({record.registration}) → "
                          "CALLERROR SecurityError", "warn", ref)

    # payload must match the official schema
    violation = store.validate(action, "request", frame.payload)
    if violation:
        v.reply = error_frame(frame.message_id, violation.code, f"{action}Request: {violation.message}"[:255],
                              violation.details())
        return v.note(f"{action} payload invalid ({violation.keyword} at {violation.path or 'root'}) → "
                      f"CALLERROR {violation.code}", "warn", "Part 4 §4.1.7, §4.3")

    # known to OCPP, but does this CSMS implement it?
    if action not in handled:
        v.reply = error_frame(frame.message_id, "NotSupported", f"{action} is recognised but not supported")
        return v.note(f"{action} is valid OCPP but not implemented here → CALLERROR NotSupported", "info",
                      "Part 4 §4.3")

    v.forward = raw
    return v
