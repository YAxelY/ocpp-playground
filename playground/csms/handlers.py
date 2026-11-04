"""CSMS-side message handlers (requests that a charging station sends to the CSMS).

One mixin serves every OCPP version: `self.R` is the version's `call_result` module, chosen by
`csms_class(version)`. The gatekeeper has already validated each request, so handlers only
contain business logic.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from ocpp.routing import on

from ..protocol.versions import OcppVersion
from .state import Transaction


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class CsmsHandlers:
    R = None          # call_result module of the negotiated version (set by csms_class)
    csms = None       # the Csms instance (set by the server)
    rec = None        # StationRecord of this connection

    # -- B Provisioning ------------------------------------------------------------------------
    @on("BootNotification")
    def on_boot_notification(self, charging_station, reason, **_):
        rec, policy = self.rec, self.csms.policy
        rec.boots += 1
        rec.model = charging_station.get("model", "")
        rec.vendor = charging_station.get("vendor_name", "")
        rec.firmware = charging_station.get("firmware_version", "")
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
        self.csms.bus.note(rec.id, f"BootNotification (reason {reason}) → {status}, interval {interval} s",
                           ref={"Accepted": "B01", "Pending": "B02", "Rejected": "B03"}[status])
        self.csms.bus.changed()
        return self.R.BootNotification(current_time=now_iso(), interval=interval, status=status)

    @on("NotifyReport")
    def on_notify_report(self, request_id, generated_at, seq_no, report_data=None, tbc=False, **_):
        for item in report_data or []:
            comp, var = item["component"]["name"], item["variable"]["name"]
            attrs = item.get("variable_attribute") or [{}]
            self.rec.variables[f"{comp}.{var}"] = attrs[0].get("value")
        self.csms.bus.changed()
        return self.R.NotifyReport()

    # -- G Availability ------------------------------------------------------------------------
    @on("Heartbeat")
    def on_heartbeat(self, **_):
        return self.R.Heartbeat(current_time=now_iso())

    @on("StatusNotification")
    def on_status_notification(self, timestamp, connector_status, evse_id, connector_id, **_):
        self.rec.connectors[f"{evse_id}/{connector_id}"] = connector_status
        self.csms.bus.changed()
        return self.R.StatusNotification()

    # -- C Authorization -----------------------------------------------------------------------
    def _token_status(self, token: str) -> str:
        status = self.csms.policy.tokens.get(token, "Invalid")
        if self.csms.policy.authorize_detail == "accepted_or_invalid" and status != "Accepted":
            status = "Invalid"          # don't reveal that a tag exists but is blocked/expired
        return status

    @on("Authorize")
    def on_authorize(self, id_token, **_):
        status = self._token_status(id_token["id_token"])
        return self.R.Authorize(id_token_info={"status": status})

    # -- E Transactions ------------------------------------------------------------------------
    @on("TransactionEvent")
    def on_transaction_event(self, event_type, timestamp, trigger_reason, seq_no, transaction_info,
                             id_token=None, evse=None, meter_value=None, offline=False, **_):
        tx_id = transaction_info["transaction_id"]
        tx = self.rec.transactions.get(tx_id)
        if tx is None:
            tx = self.rec.transactions[tx_id] = Transaction(id=tx_id, started_at=timestamp)
        if tx.last_seq >= 0 and seq_no != tx.last_seq + 1:
            tx.seq_gaps += 1
            self.csms.bus.note(self.rec.id, f"Transaction {tx_id}: seqNo jumped {tx.last_seq} → {seq_no}",
                               "warn", "Part 0 §2.8.2")
        tx.last_seq = seq_no
        tx.state = event_type
        tx.charging_state = transaction_info.get("charging_state", tx.charging_state)
        if evse:
            tx.evse_id = evse.get("id", tx.evse_id)
        if offline:
            tx.offline_events += 1
        for mv in meter_value or []:
            for sv in mv.get("sampled_value", []):
                if sv.get("measurand", "Energy.Active.Import.Register") == "Energy.Active.Import.Register":
                    tx.energy_wh = float(sv["value"])
        if event_type == "Ended":
            tx.ended_at = timestamp
            tx.stopped_reason = transaction_info.get("stopped_reason", "Local")
        response = {}
        if id_token:
            tx.id_token = id_token["id_token"]
            response["id_token_info"] = {"status": self._token_status(id_token["id_token"])}
        self.csms.bus.changed()
        return self.R.TransactionEvent(**response)

    # -- N Diagnostics / P Data transfer ---------------------------------------------------------
    @on("NotifyEvent")
    def on_notify_event(self, generated_at, seq_no, event_data, **_):
        for ev in event_data:
            self.csms.bus.note(self.rec.id, f"NotifyEvent: {ev['component']['name']}.{ev['variable']['name']} = "
                                            f"{ev.get('actual_value')} ({ev.get('trigger')})")
        return self.R.NotifyEvent()

    @on("DataTransfer")
    def on_data_transfer(self, vendor_id, **_):
        # Part 0 §4: implementations should at least be able to reject unknown DataTransfer.
        return self.R.DataTransfer(status="UnknownVendorId")

    # -- 2.1: periodic event streams (N11/N12) ---------------------------------------------------
    @on("OpenPeriodicEventStream")
    def on_open_stream(self, constant_stream_data, **_):
        sid = constant_stream_data["id"]
        self.rec.streams[sid] = {"meta": constant_stream_data, "values": deque(maxlen=300)}
        self.csms.bus.changed()
        return self.R.OpenPeriodicEventStream(status="Accepted")

    @on("ClosePeriodicEventStream")
    def on_close_stream(self, id, **_):
        self.rec.streams.pop(id, None)
        self.csms.bus.changed()
        return self.R.ClosePeriodicEventStream()


def csms_class(version: OcppVersion):
    """A ChargePoint class for the CSMS end of a connection speaking `version`."""
    return type(f"CsmsConnection_{version.name.replace('.', '_')}", (CsmsHandlers, version.ChargePoint),
                {"R": version.call_result})


def handled_actions(cls) -> set[str]:
    return {getattr(getattr(cls, name), "_on_action") for name in dir(cls)
            if hasattr(getattr(cls, name, None), "_on_action")}
