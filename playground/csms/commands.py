"""CSMS-initiated requests (CSMS → charging station), triggered from the dashboard.

Commands have one generic name and a builder per protocol family, because the messages differ:

| command | 2.0.1 / 2.1 | 1.6 |
|---|---|---|
| read_heartbeat | GetVariables(OCPPCommCtrlr.HeartbeatInterval) | GetConfiguration(HeartbeatInterval) |
| read_variables | GetVariables(model, vendor, heartbeat, TxStartPoint, …) | GetConfiguration(a few keys) |
| set_heartbeat | SetVariables(…) | ChangeConfiguration(HeartbeatInterval) |
| full_report | GetBaseReport(FullInventory) → NotifyReport | GetConfiguration() (all keys, in the answer) |
| reset | Reset(Immediate / OnIdle) | Reset(Soft / Hard) |
| clear_cache | ClearCache | ClearCache |
| trigger | TriggerMessage | TriggerMessage |
| change_availability | ChangeAvailability(operationalStatus, evse) | ChangeAvailability(connectorId, type) |
| request_start | RequestStartTransaction | RemoteStartTransaction |
| request_stop | RequestStopTransaction | RemoteStopTransaction |
| unlock | UnlockConnector(evseId, connectorId) | UnlockConnector(connectorId) |

If a 2.1 station's CALLRESULT is schema-invalid, the CSMS answers it with a CALLRESULTERROR (2.1 Part 4 §4.2.3).
"""
from __future__ import annotations

import asyncio
import dataclasses
import itertools
import uuid

from ocpp.exceptions import OCPPError

from ..protocol import schemas
from ..protocol.frames import MessageType, error_frame

_ids = itertools.count(1)

# What "read_variables" asks for (a CSMS typically checks identity and key settings while a station is Pending).
READ_SET = [("ChargingStation", "Model"), ("ChargingStation", "VendorName"), ("OCPPCommCtrlr", "HeartbeatInterval"),
            ("TxCtrlr", "TxStartPoint"), ("AuthCtrlr", "LocalAuthorizeOffline")]
READ_SET_16 = ["HeartbeatInterval", "MeterValueSampleInterval", "LocalAuthorizeOffline", "AuthorizeRemoteTxRequests"]

BUILDERS_2X = {
    "read_heartbeat": lambda c, **a: c.GetVariables(get_variable_data=[
        {"component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}}]),
    "read_variables": lambda c, **a: c.GetVariables(get_variable_data=[
        {"component": {"name": comp}, "variable": {"name": var}} for comp, var in READ_SET]),
    "set_heartbeat": lambda c, value="20", **a: c.SetVariables(set_variable_data=[
        {"attribute_value": str(value), "component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}}]),
    "full_report": lambda c, **a: c.GetBaseReport(request_id=next(_ids), report_base="FullInventory"),
    "reset": lambda c, type="Immediate", **a: c.Reset(type=type if type in ("Immediate", "OnIdle") else "Immediate"),
    "clear_cache": lambda c, **a: c.ClearCache(),
    "trigger": lambda c, message="StatusNotification", **a: c.TriggerMessage(requested_message=message),
    "change_availability": lambda c, operational_status="Inoperative", connector=0, **a: c.ChangeAvailability(
        operational_status=operational_status, **({"evse": {"id": int(connector)}} if int(connector) else {})),
    "request_start": lambda c, connector=1, token="AXEL-0001", **a: c.RequestStartTransaction(
        id_token={"id_token": token, "type": "ISO14443"}, remote_start_id=next(_ids), evse_id=int(connector)),
    "request_stop": lambda c, transaction_id="", **a: c.RequestStopTransaction(transaction_id=str(transaction_id)),
    "unlock": lambda c, connector=1, **a: c.UnlockConnector(evse_id=int(connector), connector_id=1),
}
BUILDERS_16 = {
    "read_heartbeat": lambda c, **a: c.GetConfiguration(key=["HeartbeatInterval"]),
    "read_variables": lambda c, **a: c.GetConfiguration(key=READ_SET_16),
    "set_heartbeat": lambda c, value="20", **a: c.ChangeConfiguration(key="HeartbeatInterval", value=str(value)),
    "full_report": lambda c, **a: c.GetConfiguration(),
    "reset": lambda c, type="Soft", **a: c.Reset(type=type if type in ("Soft", "Hard") else "Soft"),
    "clear_cache": lambda c, **a: c.ClearCache(),
    "trigger": lambda c, message="StatusNotification", **a: c.TriggerMessage(requested_message=message),
    "change_availability": lambda c, operational_status="Inoperative", connector=0, **a: c.ChangeAvailability(
        connector_id=int(connector), type=operational_status),
    "request_start": lambda c, connector=1, token="AXEL-0001", **a: c.RemoteStartTransaction(
        id_tag=token, connector_id=int(connector)),
    "request_stop": lambda c, transaction_id="0", **a: c.RemoteStopTransaction(transaction_id=int(transaction_id)),
    "unlock": lambda c, connector=1, **a: c.UnlockConnector(connector_id=int(connector)),
}
COMMANDS = sorted(BUILDERS_2X)

# Messages a station may send *because* the CSMS asked, even before Accepted (B02.FR.02 / 1.6 §4.2).
TRIGGERED = {"full_report": lambda v, a: "NotifyReport" if v.is_2x else None,
             "trigger": lambda v, a: a.get("message", "StatusNotification")}


def message_name(version, command: str) -> str:
    """Which OCPP message a command sends on this version (for the UI labels)."""
    builders = BUILDERS_2X if version.is_2x else BUILDERS_16
    return builders[command](version.call).__class__.__name__


async def run(csms, station_id: str, name: str, args: dict) -> dict:
    cp = csms.connections.get(station_id)
    rec = csms.stations.get(station_id)
    if cp is None or rec is None:
        csms.bus.note(station_id, f"Can't send {name}: station is not connected", "warn")
        return {"ok": False}
    version = cp.version
    if rec.registration == "Rejected":
        csms.bus.note(station_id, f"{name} not sent: the CSMS must not initiate messages to a Rejected station", "warn",
                      "1.6 §4.2" if version.name == "1.6" else "B03.FR.03")
        return {"ok": False}
    if version.name == "1.6" and rec.registration == "Pending" and name in ("request_start", "request_stop"):
        csms.bus.note(station_id, "RemoteStart/StopTransaction are not allowed while Pending (OCPP 1.6): not sent",
                      "warn", "1.6 §4.2")
        return {"ok": False}

    payload = (BUILDERS_2X if version.is_2x else BUILDERS_16)[name](version.call, **args)
    triggered = TRIGGERED.get(name, lambda v, a: None)(version, args)
    if triggered and rec.registration != "Accepted":
        rec.allowed_once.add(triggered)
    uid = str(uuid.uuid4())
    try:
        response = await cp.call(payload, suppress=False, unique_id=uid)
    except asyncio.TimeoutError:
        csms.bus.note(station_id, f"{payload.__class__.__name__}: no answer in time (the CALL timed out)", "warn",
                      version.ref("§4.1.1"))
        return {"ok": False}
    except OCPPError as e:
        answered_with_error = uid in cp._connection.received_errors
        code = e.code if answered_with_error else schemas.code_for_library_error(e, version)
        if not answered_with_error and MessageType.CALLRESULTERROR in version.frame_types:
            cause = str((e.details or {}).get("cause", e.description))[:255]
            await cp._connection.send(error_frame(uid, code, cause, result_error=True).to_json())
            csms.bus.note(station_id, f"{payload.__class__.__name__}: the station's CALLRESULT is invalid → "
                                      f"CALLRESULTERROR {code}", "warn", version.ref("§4.2.3"))
        elif not answered_with_error:
            csms.bus.note(station_id, f"{payload.__class__.__name__}: the station's CALLRESULT is invalid ({code}); "
                                      f"OCPP {version.name} has no CALLRESULTERROR to report it", "warn")
        else:
            csms.bus.note(station_id, f"{payload.__class__.__name__}: station answered CALLERROR {code}", "warn")
        return {"ok": False, "error": code}

    result = dataclasses.asdict(response) if dataclasses.is_dataclass(response) else response
    if version.is_2x and name in ("read_heartbeat", "read_variables"):          # keep what we learned
        for item in result.get("get_variable_result") or []:
            if item.get("attribute_value") is not None:
                rec.variables[f"{item['component']['name']}.{item['variable']['name']}"] = item["attribute_value"]
        csms.bus.changed()
    if version.name == "1.6" and name in ("full_report", "read_heartbeat", "read_variables"):
        for item in result.get("configuration_key") or []:          # 1.6 returns the values in the answer
            rec.variables[item["key"]] = item.get("value")
        csms.bus.changed()
    csms.bus.note(station_id, f"{payload.__class__.__name__} → {_short(result)}")
    return {"ok": True, "result": result}


def _short(result: dict) -> str:
    clean = {k: v for k, v in result.items() if v not in (None, [], {}) and k != "custom_data"}
    text = str(clean)
    return text if len(text) < 160 else text[:157] + "…"
