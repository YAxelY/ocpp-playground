"""CSMS-side message handlers: requests a charging station sends to the CSMS.

    CsmsCommon      registration decision, token check, transaction bookkeeping (version-independent)
    CsmsHandlers2x  OCPP 2.0.1 / 2.1 messages (TransactionEvent, NotifyReport, 2.1 streams…)
    CsmsHandlers16  OCPP 1.6 messages (StartTransaction / MeterValues / StopTransaction…)

`csms_class(version)` combines the right mixin with the library's ChargePoint for that version.
`self.R` is the version's call_result module. The gatekeeper has already validated each request.
"""
from __future__ import annotations

import itertools
from collections import deque
from datetime import datetime, timezone

from ocpp.routing import on

from ..protocol.versions import OcppVersion
from .state import Transaction

_tx_ids_16 = itertools.count(1001)        # 1.6: the CSMS generates transaction ids (integers)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class CsmsCommon:
    R = None
    csms = None
    rec = None

    def decide_registration(self, model: str, vendor: str, firmware: str, reason: str | None):
        rec, policy = self.rec, self.csms.policy
        rec.boots += 1
        rec.model, rec.vendor, rec.firmware = model, vendor, firmware or ""
        if policy.registration_mode == "accept":
            status = "Accepted"
        elif policy.registration_mode == "reject":
            status = "Rejected"
        else:                                   # first boot we've seen → Pending, then Accepted
            status = "Pending" if rec.boots == 1 else "Accepted"
        rec.registration = status
        if status == "Accepted":
            rec.allowed_once.clear()
        interval = policy.heartbeat_interval if status == "Accepted" else policy.retry_interval
        ref = "1.6 §4.2" if self.version.name == "1.6" else {"Accepted": "B01", "Pending": "B02", "Rejected": "B03"}[status]
        self.csms.bus.note(rec.id, f"BootNotification{f' (reason {reason})' if reason else ''} → {status}, "
                                   f"interval {interval} s", ref=ref)
        self.csms.bus.changed()
        return status, interval

    def token_status(self, token: str) -> str:
        status = self.csms.policy.tokens.get(token, "Invalid")
        if self.csms.policy.authorize_detail == "accepted_or_invalid" and status != "Accepted":
            status = "Invalid"
        return status

    def tx(self, tx_id: str, **init) -> Transaction:
        tx = self.rec.transactions.get(tx_id)
        if tx is None:
            tx = self.rec.transactions[tx_id] = Transaction(id=tx_id, **init)
        return tx


class CsmsHandlers2x(CsmsCommon):
    @on("BootNotification")
    def on_boot_notification(self, charging_station, reason, **_):
        status, interval = self.decide_registration(charging_station.get("model", ""),
                                                    charging_station.get("vendor_name", ""),
                                                    charging_station.get("firmware_version", ""), reason)
        return self.R.BootNotification(current_time=now_iso(), interval=interval, status=status)

    @on("Heartbeat")
    def on_heartbeat(self, **_):
        return self.R.Heartbeat(current_time=now_iso())

    @on("StatusNotification")
    def on_status_notification(self, timestamp, connector_status, evse_id, connector_id, **_):
        self.rec.connectors[f"{evse_id}/{connector_id}"] = connector_status
        self.csms.bus.changed()
        return self.R.StatusNotification()

    @on("Authorize")
    def on_authorize(self, id_token, **_):
        return self.R.Authorize(id_token_info={"status": self.token_status(id_token["id_token"])})

    @on("TransactionEvent")
    def on_transaction_event(self, event_type, timestamp, trigger_reason, seq_no, transaction_info,
                             id_token=None, evse=None, meter_value=None, offline=False, **_):
        tx = self.tx(transaction_info["transaction_id"], started_at=timestamp)
        if tx.last_seq >= 0 and seq_no != tx.last_seq + 1:
            tx.seq_gaps += 1
            self.csms.bus.note(self.rec.id, f"Transaction {tx.id}: seqNo jumped {tx.last_seq} → {seq_no}", "warn",
                               "2.0.1 Part 0 §2.8.2")
        tx.last_seq, tx.state = seq_no, event_type
        tx.charging_state = transaction_info.get("charging_state", tx.charging_state)
        if evse:
            tx.evse_id = evse.get("id", tx.evse_id)
        tx.offline_events += bool(offline)
        for mv in meter_value or []:
            for sv in mv.get("sampled_value", []):
                if sv.get("measurand", "Energy.Active.Import.Register") == "Energy.Active.Import.Register":
                    tx.energy_wh = float(sv["value"])
        if event_type == "Ended":
            tx.ended_at, tx.stopped_reason = timestamp, transaction_info.get("stopped_reason", "Local")
        response = {}
        if id_token:
            tx.id_token = id_token["id_token"]
            response["id_token_info"] = {"status": self.token_status(id_token["id_token"])}
        self.csms.bus.changed()
        return self.R.TransactionEvent(**response)

    @on("NotifyReport")
    def on_notify_report(self, request_id, generated_at, seq_no, report_data=None, tbc=False, **_):
        for item in report_data or []:
            attrs = item.get("variable_attribute") or [{}]
            self.rec.variables[f"{item['component']['name']}.{item['variable']['name']}"] = attrs[0].get("value")
        self.csms.bus.changed()
        return self.R.NotifyReport()

    @on("NotifyEvent")
    def on_notify_event(self, generated_at, seq_no, event_data, **_):
        for ev in event_data:
            self.csms.bus.note(self.rec.id, f"NotifyEvent {ev['component']['name']}.{ev['variable']['name']} = "
                                            f"{ev.get('actual_value')} ({ev.get('trigger')})")
        return self.R.NotifyEvent()

    @on("DataTransfer")
    def on_data_transfer(self, vendor_id, **_):
        return self.R.DataTransfer(status="UnknownVendorId")       # 2.0.1 Part 0 §4: be able to reject

    # 2.1 only (the gatekeeper answers NotImplemented to these on 2.0.1: they don't exist there)
    @on("OpenPeriodicEventStream")
    def on_open_stream(self, constant_stream_data, **_):
        self.rec.streams[constant_stream_data["id"]] = {"meta": constant_stream_data, "values": deque(maxlen=300)}
        self.csms.bus.changed()
        return self.R.OpenPeriodicEventStream(status="Accepted")

    @on("ClosePeriodicEventStream")
    def on_close_stream(self, id, **_):
        self.rec.streams.pop(id, None)
        self.csms.bus.changed()
        return self.R.ClosePeriodicEventStream()


class CsmsHandlers16(CsmsCommon):
    @on("BootNotification")
    def on_boot_notification(self, charge_point_vendor, charge_point_model, firmware_version=None, **_):
        status, interval = self.decide_registration(charge_point_model, charge_point_vendor, firmware_version, None)
        return self.R.BootNotification(current_time=now_iso(), interval=interval, status=status)

    @on("Heartbeat")
    def on_heartbeat(self, **_):
        return self.R.Heartbeat(current_time=now_iso())

    @on("StatusNotification")
    def on_status_notification(self, connector_id, error_code, status, **_):
        self.rec.connectors[str(connector_id)] = status if error_code == "NoError" else f"{status} ({error_code})"
        self.csms.bus.changed()
        return self.R.StatusNotification()

    @on("Authorize")
    def on_authorize(self, id_tag, **_):
        return self.R.Authorize(id_tag_info={"status": self.token_status(id_tag)})

    @on("StartTransaction")
    def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **_):
        tx_id = next(_tx_ids_16)                    # 1.6 §4.8: the Central System assigns the id
        tx = self.tx(str(tx_id), started_at=timestamp, evse_id=connector_id)
        tx.state, tx.charging_state, tx.id_token, tx.energy_wh = "Started", "Charging", id_tag, float(meter_start)
        status = self.token_status(id_tag)
        self.csms.bus.note(self.rec.id, f"StartTransaction → CSMS assigns transactionId {tx_id}", ref="1.6 §4.8")
        self.csms.bus.changed()
        return self.R.StartTransaction(transaction_id=tx_id, id_tag_info={"status": status})

    @on("MeterValues")
    def on_meter_values(self, connector_id, meter_value, transaction_id=None, **_):
        if transaction_id is not None and str(transaction_id) in self.rec.transactions:
            tx = self.rec.transactions[str(transaction_id)]
            tx.state = "Updated"
            for mv in meter_value:
                for sv in mv.get("sampled_value", []):
                    if sv.get("measurand", "Energy.Active.Import.Register") == "Energy.Active.Import.Register":
                        tx.energy_wh = float(sv["value"])
            self.csms.bus.changed()
        return self.R.MeterValues()

    @on("StopTransaction")
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, reason=None, id_tag=None, **_):
        tx = self.tx(str(transaction_id), started_at=None)
        tx.state, tx.charging_state, tx.energy_wh = "Ended", "Finishing", float(meter_stop)
        tx.ended_at, tx.stopped_reason = timestamp, reason or "Local"     # 1.6 §4.10: missing reason = Local
        self.csms.bus.changed()
        return self.R.StopTransaction(**({"id_tag_info": {"status": self.token_status(id_tag)}} if id_tag else {}))

    @on("DataTransfer")
    def on_data_transfer(self, vendor_id, **_):
        return self.R.DataTransfer(status="UnknownVendorId")

    @on("DiagnosticsStatusNotification")
    def on_diagnostics_status(self, status, **_):
        return self.R.DiagnosticsStatusNotification()

    @on("FirmwareStatusNotification")
    def on_firmware_status(self, status, **_):
        return self.R.FirmwareStatusNotification()


def csms_class(version: OcppVersion):
    """A ChargePoint class for the CSMS end of a connection speaking `version`."""
    mixin = CsmsHandlers2x if version.is_2x else CsmsHandlers16
    return type(f"CsmsConnection_{version.name.replace('.', '_')}", (mixin, version.ChargePoint),
                {"R": version.call_result})


def handled_actions(cls) -> set[str]:
    return {getattr(getattr(cls, name), "_on_action") for name in dir(cls)
            if hasattr(getattr(cls, name, None), "_on_action")}
