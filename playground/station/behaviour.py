"""How the station's physical events become OCPP messages: one class per protocol family.

    physical event      Behaviour2x (2.0.1 / 2.1)                         Behaviour16 (1.6)
    ──────────────      ─────────────────────────────                     ──────────────────
    cable plugged in    status Occupied · TransactionEvent if EVConnected  StatusNotification Preparing
                        is a TxStartPoint (else Updated if a tx runs)       (StartTransaction once authorized)
    card presented      Authorize (unless cached) · Started/Updated         Authorize → StartTransaction (CSMS gives the id)
    car pauses/resumes  Updated ChargingStateChanged (SuspendedEV)          StatusNotification SuspendedEV / Charging
    while charging      Updated MeterValuePeriodic (TxUpdatedMeasurands)    MeterValues (MeterValuesSampledData)
    same card again     Ended StopAuthorized if Authorized ∈ TxStopPoint    StopTransaction (Local) → Finishing
    cable unplugged     Ended EVCommunicationLost if EVConnected ∈ stop     StopTransaction (EVDisconnected), or SuspendedEV
                                                                            if StopTransactionOnEVSideDisconnect = false

2.x transactions start and end on the events listed in TxCtrlr.TxStartPoint / TxStopPoint (Part 2 E.1.1,
§2.6.6): EVConnected, Authorized, PowerPathClosed (= both) and EnergyTransfer (energy is flowing).

Every send goes through `Station.send_tx()` / `Station.call()`: if the station is offline (or not
Accepted), transaction messages are queued and sent in order after the reconnect.
"""
from __future__ import annotations

import asyncio
import itertools

from .model import Evse, TxState, status_16, status_2x

UNITS = {"Energy.Active.Import.Register": "Wh", "Power.Active.Import": "W", "Current.Import": "A",
         "Voltage": "V", "SoC": "Percent"}


def sample(e: Evse, measurand: str) -> float:
    """Simulated readings: 11 kW three-phase charging at 230 V / 16 A, SoC rising from 20 %."""
    tx, flowing = e.tx, bool(e.tx and e.tx.charging)
    energy = tx.energy_wh if tx else 0.0
    return {"Energy.Active.Import.Register": round(energy, 1), "Power.Active.Import": 11000.0 if flowing else 0.0,
            "Current.Import": 16.0 if flowing else 0.0, "Voltage": 230.0,
            "SoC": round(min(100.0, 20 + energy / 600), 1)}[measurand]


def members(value: str) -> list[str]:
    return [v for v in str(value).split(",") if v]


class Behaviour2x:
    family = "2.x"
    status_of = staticmethod(status_2x)
    _event_ids = itertools.count(1)

    def __init__(self, station):
        self.st = station

    @property
    def C(self):
        return self.st.version.call

    # -- boot & status reports ---------------------------------------------------------------------
    def boot_payload(self, reason: str):
        st = self.st
        return self.C.BootNotification(charging_station={
            "model": st.var("ChargingStation.Model"), "vendor_name": st.var("ChargingStation.VendorName"),
            "firmware_version": "1.0.0"}, reason=reason)

    def status_mode(self) -> str:
        """2.0.1: StatusNotification. 2.1: NotifyEvent AvailabilityState (preferred) or the deprecated StatusNotification."""
        return self.st.opt("StatusReport21") if self.st.version.name == "2.1" else "StatusNotification"

    def status_payloads(self, evses: list[Evse], include_station: bool = False, mode: str | None = None):
        mode, st, out = mode or self.status_mode(), self.st, []
        if mode in ("StatusNotification", "both"):
            out += [self.C.StatusNotification(timestamp=st.now_iso(), connector_status=e.status, evse_id=e.id,
                                              connector_id=1) for e in evses]
        if mode in ("NotifyEvent", "both") and (evses or include_station):
            out.append(self.availability_event(evses, include_station))
        return out

    def _event_data(self, component: dict, variable: str, value: str, trigger: str = "Delta") -> dict:
        return {"event_id": next(self._event_ids), "timestamp": self.st.now_iso(), "trigger": trigger,
                "actual_value": value, "component": component, "variable": {"name": variable},
                "event_notification_type": "HardWiredNotification"}

    def availability_event(self, evses: list[Evse], include_station: bool = False):
        """2.1 G01 / B01.FR.05: NotifyEvent(Connector.AvailabilityState), optionally EVSE + ChargingStation (G01 1a)."""
        data = [self._event_data({"name": "Connector", "evse": {"id": e.id, "connector_id": 1}}, "AvailabilityState",
                                 e.status) for e in evses]
        if self.st.opt("StatusReportEvseAndStation"):
            data += [self._event_data({"name": "EVSE", "evse": {"id": e.id}}, "AvailabilityState", e.status)
                     for e in evses]
            include_station = True
        if include_station and self.st.opt("StatusReportEvseAndStation"):
            all_off = all(not e.operative for e in self.st.evses.values())
            data.append(self._event_data({"name": "ChargingStation"}, "AvailabilityState",
                                         "Unavailable" if all_off else "Available"))
        return self.C.NotifyEvent(generated_at=self.st.now_iso(), seq_no=0, event_data=data, tbc=False)

    def problem_event(self, e: Evse, problem: bool):
        return self.C.NotifyEvent(generated_at=self.st.now_iso(), seq_no=0, tbc=False, event_data=[
            self._event_data({"name": "EVSE", "evse": {"id": e.id}}, "Problem", "true" if problem else "false")])

    def security_event(self, kind: str):
        return self.C.SecurityEventNotification(type=kind, timestamp=self.st.now_iso())

    def authorize_payload(self, token: str):
        return self.C.Authorize(id_token={"id_token": token, "type": "ISO14443"})

    @staticmethod
    def authorize_status(resp) -> str:
        return resp.id_token_info["status"]

    # -- meter values ---------------------------------------------------------------------------
    def meter_values(self, e: Evse, measurands: str, context: str) -> list:
        sampled = []
        for m in members(measurands):
            sv = {"value": sample(e, m), "measurand": m, "context": context, "unit_of_measure": {"unit": UNITS[m]}}
            if m == "SoC":
                sv["location"] = "EV"
            sampled.append(sv)
        return [{"timestamp": self.st.now_iso(), "sampled_value": sampled}] if sampled else []

    # -- the transaction state machine -------------------------------------------------------------
    @staticmethod
    def conditions(e: Evse) -> dict:
        authorized = e.auth_token is not None
        return {"EVConnected": e.cable, "Authorized": authorized, "PowerPathClosed": e.cable and authorized,
                "EnergyTransfer": e.cable and authorized and not e.ev_suspended and not e.evse_suspended
                                  and e.operative and not e.fault}

    @staticmethod
    def charging_state(e: Evse) -> str:
        """ChargingStateEnumType: Charging, EVConnected, SuspendedEV, SuspendedEVSE, Idle."""
        if not e.cable:
            return "Idle"
        if e.auth_token is None:
            return "EVConnected"
        if e.evse_suspended:
            return "SuspendedEVSE"
        return "SuspendedEV" if e.ev_suspended else "Charging"

    def start_points(self) -> set:
        return set(members(self.st.opt("TxCtrlr.TxStartPoint")))

    def stop_points(self) -> set:
        points = set(members(self.st.opt("TxCtrlr.TxStopPoint")))
        if "PowerPathClosed" in points:           # §2.6.6.2: exactly the same as EVConnected, Authorized
            points |= {"EVConnected", "Authorized"}
        return points

    async def change(self, e: Evse, mutate, trigger: str, stopped_reason: str = "Local", token: str | None = None,
                     remote_id: int | None = None) -> None:
        """Apply one physical / authorization change, then start, update or end the transaction as configured."""
        before, old_state = self.conditions(e), self.charging_state(e)
        mutate()
        after, new_state = self.conditions(e), self.charging_state(e)
        if e.tx:
            e.tx.charging = after["EnergyTransfer"]
        await self.st.report([e])
        if e.tx is None:
            started = [p for p in self.start_points() if after[p] and not before[p]]
            if started:
                e.tx = TxState(token=e.auth_token, remote_start_id=remote_id, charging=after["EnergyTransfer"])
                self.st.bus.note(self.st.id, f"EVSE {e.id}: {', '.join(started)} is a TxStartPoint → transaction "
                                             f"{e.tx.id} starts", ref="E01 · TxCtrlr.TxStartPoint")
                await self.event(e, "Started", trigger, new_state, token=e.auth_token,
                                 meter=self.meter_values(e, self.st.opt("SampledDataCtrlr.TxStartedMeasurands"),
                                                         "Transaction.Begin"))
            return
        ended = [p for p in self.stop_points() if before[p] and not after[p]]
        if ended:
            self.st.bus.note(self.st.id, f"EVSE {e.id}: {', '.join(ended)} ended and is a TxStopPoint → transaction "
                                         f"{e.tx.id} ends", ref="E06 · TxCtrlr.TxStopPoint")
            await self.end(e, trigger, stopped_reason, token=token)
            return
        if remote_id is not None:
            e.tx.remote_start_id = remote_id
        if before != after or old_state != new_state or token:
            await self.event(e, "Updated", trigger, new_state, token=token)

    async def event(self, e: Evse, event_type: str, trigger: str, charging_state: str, token: str | None = None,
                    stopped_reason: str | None = None, meter: list | None = None, quiet: bool = False) -> None:
        """Build the TransactionEvent now (seqNo, meter values), send now or queue it with offline=true (E04)."""
        tx, st = e.tx, self.st
        info = {"transaction_id": tx.id, "charging_state": charging_state}
        if stopped_reason:
            info["stopped_reason"] = stopped_reason
        if tx.remote_start_id is not None and trigger == "RemoteStart":
            info["remote_start_id"] = tx.remote_start_id
        kwargs = dict(event_type=event_type, timestamp=st.now_iso(), trigger_reason=trigger, seq_no=tx.next_seq(),
                      transaction_info=info, evse={"id": e.id, "connector_id": 1})
        if meter:
            kwargs["meter_value"] = meter
        if token:
            kwargs["id_token"] = {"id_token": token, "type": "ISO14443"}
        version = st.version

        async def send(cp, offline: bool):
            if offline:
                kwargs["offline"] = True
            resp = await st.call(cp, version.call.TransactionEvent(**kwargs))
            info_back = resp.id_token_info if resp is not None else None
            if info_back and token:
                st.cache(token, info_back["status"])
                if info_back["status"] != "Accepted" and e.tx is tx and tx.token == token:
                    asyncio.ensure_future(self.invalid_id(e, info_back["status"]))
        await st.send_tx(send, f"TransactionEvent {event_type}", quiet=quiet)

    async def end(self, e: Evse, trigger: str, reason: str, token: str | None = None) -> None:
        if e.tx is None:
            return
        e.tx.charging = False
        meter = self.meter_values(e, self.st.opt("SampledDataCtrlr.TxEndedMeasurands"), "Transaction.End")
        e.auth_token, e.auth_since, e.evse_suspended = None, None, False
        await self.event(e, "Ended", trigger, self.charging_state(e), token=token, stopped_reason=reason, meter=meter)
        e.tx = None
        await self.st.report([e])
        await self.st.after_transaction(e)

    # -- physical events and commands ---------------------------------------------------------------
    async def plug(self, e: Evse) -> None:
        await self.change(e, lambda: setattr(e, "cable", True), "CablePluggedIn")

    async def unplug(self, e: Evse) -> None:
        def mutate():
            e.cable, e.ev_suspended = False, False
            if e.tx is None or "EVConnected" in self.stop_points():
                e.auth_token, e.auth_since = None, None          # the session is over
        await self.change(e, mutate, "EVCommunicationLost", "EVDisconnected")

    async def token(self, e: Evse, token: str) -> None:
        if e.auth_token == token:                                     # same card again: end the authorization (E07)
            await self.change(e, lambda: setattr(e, "auth_token", None), "StopAuthorized", "Local", token=token)
            return
        if e.auth_token:
            self.st.bus.note(self.st.id, f"EVSE {e.id}: another card is charging here", "warn")
            return
        if await self.st.authorize(token) != "Accepted":
            return
        await self._authorize(e, token, "Authorized")

    async def _authorize(self, e: Evse, token: str, trigger: str, remote_id: int | None = None) -> None:
        def mutate():
            e.auth_token, e.auth_since = token, (None if e.cable else self.st.clock())
        await self.change(e, mutate, trigger, token=token, remote_id=remote_id)

    async def remote_start(self, e: Evse, token: str, remote_id: int) -> None:
        if self.st.opt("AuthCtrlr.AuthorizeRemoteStart"):
            self.st.bus.note(self.st.id, "AuthorizeRemoteStart = true → check the token first", ref="F01/F02")
            if await self.st.authorize(token) != "Accepted":
                return
        await self._authorize(e, token, "RemoteStart", remote_id)

    async def remote_stop(self, e: Evse) -> None:
        await self.end(e, "RemoteStop", "Remote")

    async def ev_pause(self, e: Evse, paused: bool) -> None:
        await self.change(e, lambda: setattr(e, "ev_suspended", paused), "ChargingStateChanged",
                          "Other")

    async def timeout(self, e: Evse) -> None:
        """C01.FR.26 / E03.FR.05: card accepted but no cable within EVConnectionTimeOut."""
        self.st.bus.note(self.st.id, f"EVSE {e.id}: no cable within EVConnectionTimeOut → authorization cancelled",
                         "warn", "C01.FR.26 · E03.FR.05")
        if e.tx:
            await self.end(e, "EVConnectTimeout", "Timeout")
        else:
            e.auth_token, e.auth_since = None, None
            await self.st.report([e])

    async def invalid_id(self, e: Evse, status: str) -> None:
        """E05: the CSMS says the token of a running transaction is not Accepted."""
        if not self.st.opt("TxCtrlr.StopTxOnInvalidId"):
            self.st.bus.note(self.st.id, f"Token {status}, StopTxOnInvalidId = false → energy stops, transaction "
                                         "stays (SuspendedEVSE)", "warn", "E05.FR.02")
            await self.change(e, lambda: setattr(e, "evse_suspended", True), "ChargingStateChanged")
        elif self.stop_points() & {"Authorized", "PowerPathClosed", "EnergyTransfer"}:
            self.st.bus.note(self.st.id, f"Token {status} → transaction ends (DeAuthorized)", "warn", "E05.FR.10")
            await self.end(e, "Deauthorized", "DeAuthorized")
        else:
            self.st.bus.note(self.st.id, f"Token {status} → deauthorized, transaction continues until the TxStopPoint",
                             "warn", "E05.FR.09")
            await self.change(e, lambda: setattr(e, "auth_token", None), "Deauthorized")

    async def fault(self, e: Evse) -> None:
        if e.tx:
            await self.end(e, "AbnormalCondition", "Other")
        await self.st.report([e])
        if self.st.opt("FaultAsNotifyEvent") and self.st.accepted:
            await self.st.call(self.st.cp, self.problem_event(e, True))

    async def repaired(self, e: Evse) -> None:
        await self.st.report([e])
        if self.st.opt("FaultAsNotifyEvent") and self.st.accepted:
            await self.st.call(self.st.cp, self.problem_event(e, False))

    async def reset_stop(self, e: Evse, reason: str = "ImmediateReset") -> None:
        await self.end(e, "ResetCommand", reason)

    async def meter(self, e: Evse) -> None:
        await self.event(e, "Updated", "MeterValuePeriodic", self.charging_state(e), quiet=True,
                         meter=self.meter_values(e, self.st.opt("SampledDataCtrlr.TxUpdatedMeasurands"), "Sample.Periodic"))

    async def aligned(self, e: Evse) -> None:
        """J01.FR.14 / J02: clock-aligned values: in the transaction (MeterValueClock) or as MeterValues when idle."""
        measurands = self.st.opt("AlignedDataCtrlr.Measurands")
        if e.tx:
            if self.st.opt("AlignedDataCtrlr.SendDuringIdle"):
                return                                      # J01.FR.19
            await self.event(e, "Updated", "MeterValueClock", self.charging_state(e), quiet=True,
                             meter=self.meter_values(e, measurands, "Sample.Clock"))
        elif self.st.accepted:
            await self.st.call(self.st.cp, self.C.MeterValues(evse_id=e.id,
                                                              meter_value=self.meter_values(e, measurands, "Sample.Clock")))

    async def triggered_meter(self, e: Evse) -> None:
        await self.st.call(self.st.cp, self.C.MeterValues(evse_id=e.id, meter_value=self.meter_values(
            e, self.st.opt("SampledDataCtrlr.TxUpdatedMeasurands"), "Trigger")))

    async def triggered_tx(self, e: Evse) -> None:
        if e.tx:
            await self.event(e, "Updated", "Trigger", self.charging_state(e),
                             meter=self.meter_values(e, self.st.opt("SampledDataCtrlr.TxUpdatedMeasurands"), "Trigger"))

    def find_tx(self, transaction_id) -> Evse | None:
        return next((e for e in self.st.evses.values() if e.tx and e.tx.id == str(transaction_id)), None)


class Behaviour16:
    family = "1.6"
    status_of = staticmethod(status_16)

    def __init__(self, station):
        self.st = station

    @property
    def C(self):
        return self.st.version.call

    def boot_payload(self, reason: str):
        st = self.st                                            # 1.6 has no boot reason field
        return self.C.BootNotification(charge_point_model=st.var("ChargingStation.Model"),
                                       charge_point_vendor=st.var("ChargingStation.VendorName"),
                                       firmware_version="1.0.0")

    def status_payloads(self, evses: list[Evse], include_station: bool = False, mode: str | None = None):
        out = []
        if include_station and self.st.opt("ReportConnectorZero"):   # 1.6 §3.8: connectorId 0 = the charge point
            all_off = all(not e.operative for e in self.st.evses.values())
            out.append(self.C.StatusNotification(connector_id=0, error_code="NoError", timestamp=self.st.now_iso(),
                                                 status="Unavailable" if all_off else "Available"))
        for e in evses:
            out.append(self.C.StatusNotification(connector_id=e.id, status=e.status, timestamp=self.st.now_iso(),
                                                 error_code="OtherError" if e.fault else "NoError",
                                                 **({"info": "simulated fault"} if e.fault else {})))
        return out

    def authorize_payload(self, token: str):
        return self.C.Authorize(id_tag=token)

    @staticmethod
    def authorize_status(resp) -> str:
        return resp.id_tag_info["status"]

    def meter_values(self, e: Evse, measurands: str, context: str) -> list:
        sampled = []
        for m in members(measurands):
            sv = {"value": f"{sample(e, m):g}", "context": context, "measurand": m, "unit": UNITS[m]}
            if m == "SoC":
                sv["location"] = "EV"
            sampled.append(sv)
        return [{"timestamp": self.st.now_iso(), "sampled_value": sampled}] if sampled else []

    async def _start(self, e: Evse, token: str, remote_id: int | None = None) -> None:
        """StartTransaction: the CSMS's answer carries the transactionId (1.6 §4.8)."""
        tx = e.tx = TxState(token=token, charging=not e.ev_suspended, remote_start_id=remote_id)
        tx.begin = self.meter_values(e, self.st.opt("SampledDataCtrlr.TxEndedMeasurands"), "Transaction.Begin")
        e.auth_token, e.auth_since, e.finishing = None, None, False
        await self.st.report([e])                               # Charging
        version, st = self.st.version, self.st

        async def send(cp, offline: bool):
            resp = await st.call(cp, version.call.StartTransaction(
                connector_id=e.id, id_tag=token, meter_start=int(tx.energy_wh), timestamp=st.now_iso()))
            if resp is not None:
                tx.server_id = resp.transaction_id
                st.cache(token, resp.id_tag_info["status"])
                if resp.id_tag_info["status"] != "Accepted" and e.tx is tx:
                    await self.invalid_id(e, resp.id_tag_info["status"])
        await st.send_tx(send, "StartTransaction")

    async def invalid_id(self, e: Evse, status: str) -> None:
        if self.st.opt("TxCtrlr.StopTxOnInvalidId"):
            self.st.bus.note(self.st.id, f"StartTransaction: idTag {status}, StopTransactionOnInvalidId = true → stop",
                             "warn", "1.6 §3.4 · §9.1.24")
            await self.end(e, "DeAuthorized")
        else:
            self.st.bus.note(self.st.id, f"StartTransaction: idTag {status}, StopTransactionOnInvalidId = false → only "
                                         "the energy stops (SuspendedEVSE)", "warn", "1.6 §3.4 · §9.1.24")
            e.evse_suspended, e.tx.charging = True, False
            await self.st.report([e])

    async def plug(self, e: Evse) -> None:
        e.cable, e.finishing = True, False
        if e.tx:                                                # unplugged on the EV side earlier: resume
            e.tx.charging = not (e.ev_suspended or e.evse_suspended)
            await self.st.report([e])
        elif e.auth_token:                                      # card / remote start came first
            await self._start(e, e.auth_token)
        else:
            await self.st.report([e])                           # Preparing

    async def token(self, e: Evse, token: str) -> None:
        if e.tx and e.tx.token == token:                        # same card again → stop
            await self.end(e, "Local", token=token)
            return
        if e.auth_token == token:                               # authorized, no cable yet: cancel (B1)
            e.auth_token, e.auth_since = None, None
            await self.st.report([e])
            return
        if e.tx or e.auth_token:
            self.st.bus.note(self.st.id, f"Connector {e.id}: another card is in use here", "warn")
            return
        if await self.st.authorize(token) != "Accepted":
            return
        await self._authorized(e, token)

    async def _authorized(self, e: Evse, token: str, remote_id: int | None = None) -> None:
        if e.cable:
            await self._start(e, token, remote_id)
        else:
            e.auth_token, e.auth_since = token, self.st.clock()  # Preparing, waiting for the cable
            await self.st.report([e])

    async def remote_start(self, e: Evse, token: str, remote_id: int) -> None:
        # 1.6 §5.11: behave as if the idTag was presented; authorize first only if AuthorizeRemoteTxRequests = true
        if self.st.opt("AuthCtrlr.AuthorizeRemoteStart"):
            if await self.st.authorize(token) != "Accepted":
                return
        await self._authorized(e, token, remote_id)

    async def remote_stop(self, e: Evse) -> None:
        await self.end(e, "Remote")

    async def ev_pause(self, e: Evse, paused: bool) -> None:
        e.ev_suspended = paused
        if e.tx:
            e.tx.charging = not paused and e.cable and not e.evse_suspended
        await self.st.report([e])                               # SuspendedEV ↔ Charging (C4 / D3)

    async def timeout(self, e: Evse) -> None:
        self.st.bus.note(self.st.id, f"Connector {e.id}: no cable within ConnectionTimeOut → back to Available", "warn",
                         "1.6 §4.9 (B1) · §9.1.6")
        e.auth_token, e.auth_since = None, None
        await self.st.report([e])

    async def end(self, e: Evse, reason: str, token: str | None = None) -> None:
        tx = e.tx
        if tx is None:
            return
        tx.charging = False
        data = (tx.begin or []) + self.meter_values(e, self.st.opt("SampledDataCtrlr.TxEndedMeasurands"), "Transaction.End")
        e.tx, e.finishing, e.ev_suspended, e.evse_suspended = None, e.cable, False, False
        await self.st.report([e])                               # Finishing (cable in) or Available
        version, st = self.st.version, self.st

        async def send(cp, offline: bool):
            if tx.server_id is None:
                st.bus.note(st.id, "StopTransaction without a transactionId (StartTransaction unanswered)", "warn")
                return
            resp = await st.call(cp, version.call.StopTransaction(
                meter_stop=int(tx.energy_wh), timestamp=st.now_iso(), transaction_id=tx.server_id, reason=reason,
                **({"id_tag": token} if token else {}), **({"transaction_data": data} if data else {})))
            if resp is not None and token and resp.id_tag_info:
                st.cache(token, resp.id_tag_info["status"])
        await st.send_tx(send, f"StopTransaction ({reason})")
        await st.after_transaction(e)

    async def unplug(self, e: Evse) -> None:
        e.cable = False
        if e.tx and not self.st.opt("TxCtrlr.StopTxOnEVSideDisconnect"):
            e.tx.charging = False
            self.st.bus.note(self.st.id, f"Connector {e.id}: unplugged on the EV side, StopTransactionOnEVSideDisconnect"
                                         " = false → transaction suspended", "info", "1.6 §9.1.23")
            await self.st.report([e])                           # SuspendedEV
            return
        e.auth_token, e.auth_since, e.ev_suspended = None, None, False
        if e.tx:
            await self.end(e, "EVDisconnected")
        e.finishing = False
        await self.st.report([e])                               # Available

    async def fault(self, e: Evse) -> None:
        if e.tx:
            await self.end(e, "Other")
        await self.st.report([e])

    async def repaired(self, e: Evse) -> None:
        await self.st.report([e])

    async def reset_stop(self, e: Evse, reason: str = "SoftReset") -> None:
        await self.end(e, reason)

    async def meter(self, e: Evse) -> None:
        tx, version, st = e.tx, self.st.version, self.st
        values = self.meter_values(e, st.opt("SampledDataCtrlr.TxUpdatedMeasurands"), "Sample.Periodic")

        async def send(cp, offline: bool):
            if tx.server_id is not None and values:
                await st.call(cp, version.call.MeterValues(connector_id=e.id, transaction_id=tx.server_id,
                                                           meter_value=values))
        await st.send_tx(send, "MeterValues", quiet=True)

    async def aligned(self, e: Evse) -> None:
        """1.6 §4.7: clock-aligned MeterValues (Sample.Clock), with the transactionId while one runs."""
        values = self.meter_values(e, self.st.opt("AlignedDataCtrlr.Measurands"), "Sample.Clock")
        if not values or not self.st.accepted:
            return
        tx_id = e.tx.server_id if e.tx else None
        await self.st.call(self.st.cp, self.C.MeterValues(connector_id=e.id, meter_value=values,
                                                          **({"transaction_id": tx_id} if tx_id is not None else {})))

    async def triggered_meter(self, e: Evse) -> None:
        values = self.meter_values(e, self.st.opt("SampledDataCtrlr.TxUpdatedMeasurands"), "Trigger")
        tx_id = e.tx.server_id if e.tx else None
        await self.st.call(self.st.cp, self.C.MeterValues(connector_id=e.id, meter_value=values,
                                                          **({"transaction_id": tx_id} if tx_id is not None else {})))

    async def triggered_tx(self, e: Evse) -> None:
        return None

    def find_tx(self, transaction_id) -> Evse | None:
        return next((e for e in self.st.evses.values() if e.tx and e.tx.server_id == int(transaction_id)), None)


def behaviour_for(version, station):
    return (Behaviour2x if version.is_2x else Behaviour16)(station)
