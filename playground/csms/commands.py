"""CSMS-initiated requests (CSMS → Charging Station), triggered from the dashboard.

Each command builds the request with the `call` module of the version the station negotiated,
sends it through the library (which waits for the answer with the same message id) and reports
the outcome. If a 2.1 station's CALLRESULT is schema-invalid, the CSMS answers it with a
CALLRESULTERROR (2.1 Part 4 §4.2.3).
"""
from __future__ import annotations

import asyncio
import dataclasses
import itertools
import uuid

from ocpp.exceptions import OCPPError

from ..protocol import schemas
from ..protocol.frames import MessageType, error_frame

_request_ids = itertools.count(1)

# What each command sends. f(call_module, **args) -> payload object
BUILDERS = {
    "get_variables": lambda c, component="OCPPCommCtrlr", variable="HeartbeatInterval", **_: c.GetVariables(
        get_variable_data=[{"component": {"name": component}, "variable": {"name": variable}}]),
    "set_variables": lambda c, component="OCPPCommCtrlr", variable="HeartbeatInterval", value="20", **_: c.SetVariables(
        set_variable_data=[{"attribute_value": str(value), "component": {"name": component},
                            "variable": {"name": variable}}]),
    "get_base_report": lambda c, report_base="FullInventory", **_: c.GetBaseReport(
        request_id=next(_request_ids), report_base=report_base),
    "change_availability": lambda c, operational_status="Inoperative", evse_id=0, **_: c.ChangeAvailability(
        operational_status=operational_status, **({"evse": {"id": int(evse_id)}} if int(evse_id) else {})),
    "reset": lambda c, type="Immediate", **_: c.Reset(type=type),
    "request_start": lambda c, evse_id=1, token="AXEL-0001", **_: c.RequestStartTransaction(
        id_token={"id_token": token, "type": "ISO14443"}, remote_start_id=next(_request_ids), evse_id=int(evse_id)),
    "request_stop": lambda c, transaction_id="", **_: c.RequestStopTransaction(transaction_id=transaction_id),
    "trigger": lambda c, message="StatusNotification", evse_id=0, **_: c.TriggerMessage(
        requested_message=message, **({"evse": {"id": int(evse_id)}} if int(evse_id) else {})),
}

# Requests a station is allowed to send *because* the CSMS asked (B02.FR.02 exception list).
TRIGGERED = {"get_base_report": lambda a: "NotifyReport", "trigger": lambda a: a.get("message", "StatusNotification")}


async def run(csms, station_id: str, name: str, args: dict) -> dict:
    cp = csms.connections.get(station_id)
    rec = csms.stations.get(station_id)
    if cp is None or rec is None:
        csms.bus.note(station_id, f"Can't send {name}: station is not connected", "warn")
        return {"ok": False}
    if rec.registration == "Rejected":
        csms.bus.note(station_id, f"{name} not sent: the CSMS SHALL NOT initiate messages to a Rejected station",
                      "warn", "B03.FR.03")
        return {"ok": False}
    if name not in BUILDERS:
        raise ValueError(f"unknown command {name}")

    payload = BUILDERS[name](cp.version.call, **args)
    if name in TRIGGERED and rec.registration != "Accepted":
        rec.allowed_once.add(TRIGGERED[name](args))
    uid = str(uuid.uuid4())
    try:
        response = await cp.call(payload, suppress=False, unique_id=uid)
    except asyncio.TimeoutError:
        csms.bus.note(station_id, f"{name}: no answer in time (the CALL timed out)", "warn", "Part 4 §4.1.1")
        return {"ok": False}
    except OCPPError as e:
        # Either the station answered with a CALLERROR, or its CALLRESULT failed our schema check.
        answered_with_error = uid in cp._connection.received_errors
        code = e.code if answered_with_error else schemas.code_for_library_error(e)
        if not answered_with_error and MessageType.CALLRESULTERROR in cp.version.frame_types:
            cause = str((e.details or {}).get("cause", e.description))[:255]
            await cp._connection.send(error_frame(uid, code, cause, result_error=True).to_json())
            csms.bus.note(station_id, f"{name}: station's CALLRESULT is invalid → CALLRESULTERROR {code}",
                          "warn", "2.1 Part 4 §4.2.3")
        elif not answered_with_error:
            csms.bus.note(station_id, f"{name}: station's CALLRESULT is invalid ({code}); OCPP 2.0.1 has no "
                                      "CALLRESULTERROR to report it", "warn")
        else:
            csms.bus.note(station_id, f"{name}: station answered CALLERROR {code}", "warn")
        return {"ok": False, "error": code}
    result = dataclasses.asdict(response) if dataclasses.is_dataclass(response) else response
    csms.bus.note(station_id, f"{name} → {_short(result)}")
    return {"ok": True, "result": result}


def _short(result: dict) -> str:
    clean = {k: v for k, v in result.items() if v not in (None, [], {}) and k != "custom_data"}
    text = str(clean)
    return text if len(text) < 160 else text[:157] + "…"
