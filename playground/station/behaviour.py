"""How the station's physical events become OCPP messages: one class per protocol family.

    physical event      Behaviour2x (2.0.1 / 2.1)                    Behaviour16 (1.6)
    ──────────────      ───────────────────────────                   ──────────────────
    cable plugged in    StatusNotification Occupied                   StatusNotification Preparing
                        TransactionEvent Started (CablePluggedIn)     (StartTransaction once authorized)
    card presented      Authorize → TransactionEvent Updated          Authorize → StartTransaction (CSMS returns the id)
    while charging      TransactionEvent Updated (MeterValuePeriodic) MeterValues(transactionId)
    same card again     TransactionEvent Ended (StopAuthorized)       StopTransaction → Finishing
    cable unplugged     TransactionEvent Ended (EVCommunicationLost)  StopTransaction (EVDisconnected) → Available

Every send goes through `Station.send_tx()` / `Station.call()`: if the station is offline (or not
Accepted), transaction messages are queued and sent in order after the reconnect.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .model import Evse, TxState, status_16, status_2x


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Behaviour2x:
    family = "2.x"
    status_of = staticmethod(status_2x)

    def __init__(self, station):
        self.st = station

    @property
    def C(self):
        return self.st.version.call

    # -- boot & reports -----------------------------------------------------------------------
    def boot_payload(self, reason: str):
        st = self.st
        return self.C.BootNotification(charging_station={
            "model": st.var("ChargingStation.Model"), "vendor_name": st.var("ChargingStation.VendorName"),
            "firmware_version": "1.0.0"}, reason=reason)

    def status_payloads(self, evses: list[Evse], include_station: bool = False):
        return [self.C.StatusNotification(timestamp=now_iso(), connector_status=e.status, evse_id=e.id, connector_id=1)
                for e in evses]

    def authorize_payload(self, token: str):
        return self.C.Authorize(id_token={"id_token": token, "type": "ISO14443"})

    @staticmethod
    def authorize_status(resp) -> str:
        return resp.id_token_info["status"]

    # -- transactions ---------------------------------------------------------------------------
    def _event(self, e: Evse, event_type: str, trigger: str, charging_state: str, token: str | None = None,
               stopped_reason: str | None = None, offline: bool = False) -> dict:
        tx = e.tx
        info = {"transaction_id": tx.id, "charging_state": charging_state}
        if stopped_reason:
            info["stopped_reason"] = stopped_reason
        if tx.remote_start_id is not None and trigger == "RemoteStart":
            info["remote_start_id"] = tx.remote_start_id
        kwargs = dict(event_type=event_type, timestamp=now_iso(), trigger_reason=trigger, seq_no=tx.next_seq(),
                      transaction_info=info, evse={"id": e.id, "connector_id": 1},
                      meter_value=[{"timestamp": now_iso(), "sampled_value": [{
                          "value": round(tx.energy_wh, 1), "measurand": "Energy.Active.Import.Register",
                          "unit_of_measure": {"unit": "Wh"}}]}])
        if token:
            kwargs["id_token"] = {"id_token": token, "type": "ISO14443"}
        return kwargs

    async def event(self, e: Evse, *args, quiet: bool = False, **kwargs) -> None:
        """Build the TransactionEvent now (seqNo, meter value), send now or queue it with offline=true (E04)."""
        payload_kwargs = self._event(e, *args, **kwargs)
        version = self.st.version

        async def send(cp, offline: bool):
            if offline:
                payload_kwargs["offline"] = True
            resp = await self.st.call(cp, version.call.TransactionEvent(**payload_kwargs))
            if resp is not None and resp.id_token_info and resp.id_token_info.get("status") != "Accepted":
                self.st.bus.note(self.st.id, f"CSMS: token is {resp.id_token_info['status']}", "warn")
        await self.st.send_tx(send, f"TransactionEvent {payload_kwargs['event_type']}", quiet=quiet)

    async def plug(self, e: Evse) -> None:
        await self.st.report([e])
        if e.tx is None:                                        # E02: cable first
            e.tx = TxState()
            await self.event(e, "Started", "CablePluggedIn", "EVConnected")
        else:                                                   # card / remote start came first
            e.tx.charging = bool(e.tx.token)
            await self.event(e, "Updated", "CablePluggedIn", "Charging" if e.tx.charging else "EVConnected")

    async def token(self, e: Evse, token: str) -> None:
        if e.tx and e.tx.token == token:                        # E07: same card again → stop
            await self.end(e, "StopAuthorized", "Local", token=token)
            return
        if e.tx and e.tx.token:
            self.st.bus.note(self.st.id, f"EVSE {e.id}: another card is charging here", "warn")
            return
        if await self.st.authorize(token) != "Accepted":
            return
        if e.tx is None:                                        # E03: card first, wait for the cable
            e.tx = TxState(token=token)
            await self.event(e, "Started", "Authorized", "Idle", token=token)
        else:
            e.tx.token, e.tx.charging = token, True
            await self.event(e, "Updated", "Authorized", "Charging", token=token)

    async def remote_start(self, e: Evse, token: str, remote_id: int) -> None:
        if e.tx is None:                                        # F02: remote start first
            e.tx = TxState(token=token, remote_start_id=remote_id)
            await self.event(e, "Started", "RemoteStart", "Idle", token=token)
        else:                                                   # F01: cable already in
            e.tx.token, e.tx.charging, e.tx.remote_start_id = token, True, remote_id
            await self.event(e, "Updated", "RemoteStart", "Charging", token=token)

    async def end(self, e: Evse, trigger: str, reason: str, token: str | None = None) -> None:
        if e.tx is None:
            return
        e.tx.charging = False
        await self.event(e, "Ended", trigger, "EVConnected" if e.cable else "Idle", token=token, stopped_reason=reason)
        e.tx = None
        await self.st.after_transaction(e)

    async def unplug(self, e: Evse) -> None:
        if e.tx:                                                # E09
            await self.end(e, "EVCommunicationLost", "EVDisconnected")
        await self.st.report([e])

    async def fault(self, e: Evse) -> None:
        if e.tx:
            await self.end(e, "AbnormalCondition", "Other")
        await self.st.report([e])

    async def reset_stop(self, e: Evse) -> None:
        await self.end(e, "ResetCommand", "ImmediateReset")

    async def meter(self, e: Evse) -> None:
        await self.event(e, "Updated", "MeterValuePeriodic", "Charging", quiet=True)

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

    def status_payloads(self, evses: list[Evse], include_station: bool = False):
        out = []
        if include_station:                                     # 1.6 §3.8: connectorId 0 = the charge point itself
            out.append(self.C.StatusNotification(connector_id=0, error_code="NoError", status="Available",
                                                 timestamp=now_iso()))
        for e in evses:
            out.append(self.C.StatusNotification(connector_id=e.id, status=e.status, timestamp=now_iso(),
                                                 error_code="OtherError" if e.fault else "NoError",
                                                 **({"info": "simulated fault"} if e.fault else {})))
        return out

    def authorize_payload(self, token: str):
        return self.C.Authorize(id_tag=token)

    @staticmethod
    def authorize_status(resp) -> str:
        return resp.id_tag_info["status"]

    def _meter(self, tx: TxState) -> list:
        return [{"timestamp": now_iso(), "sampled_value": [{"value": str(int(tx.energy_wh)), "context": "Sample.Periodic",
                                                            "measurand": "Energy.Active.Import.Register", "unit": "Wh"}]}]

    async def _start(self, e: Evse, token: str, remote_id: int | None = None) -> None:
        """StartTransaction: the CSMS's answer carries the transactionId (1.6 §4.8)."""
        tx = e.tx = TxState(token=token, charging=True, remote_start_id=remote_id)
        e.auth_token, e.finishing = None, False
        await self.st.report([e])                               # Charging
        version = self.st.version

        async def send(cp, offline: bool):
            resp = await self.st.call(cp, version.call.StartTransaction(
                connector_id=e.id, id_tag=token, meter_start=int(tx.energy_wh), timestamp=now_iso()))
            if resp is not None:
                tx.server_id = resp.transaction_id
                if resp.id_tag_info["status"] != "Accepted":   # PLAYGROUND DECISION: stop on an invalid id
                    self.st.bus.note(self.st.id, f"StartTransaction: idTag {resp.id_tag_info['status']} → stop",
                                     "warn", "1.6 §4.8")
                    if e.tx is tx:
                        await self.end(e, "DeAuthorized")
        await self.st.send_tx(send, "StartTransaction")

    async def plug(self, e: Evse) -> None:
        e.finishing = False
        if e.auth_token:                                        # card / remote start came first
            await self._start(e, e.auth_token)
        else:
            await self.st.report([e])                           # Preparing

    async def token(self, e: Evse, token: str) -> None:
        if e.tx and e.tx.token == token:                        # same card again → stop
            await self.end(e, "Local", token=token)
            return
        if e.tx:
            self.st.bus.note(self.st.id, f"Connector {e.id}: another card is charging here", "warn")
            return
        if await self.st.authorize(token) != "Accepted":
            return
        if e.cable:
            await self._start(e, token)
        else:
            e.auth_token = token                                # Preparing, waiting for the cable
            await self.st.report([e])

    async def remote_start(self, e: Evse, token: str, remote_id: int) -> None:
        # 1.6 §5.11: behave as if the idTag was presented (AuthorizeRemoteTxRequests = false: no Authorize)
        if e.cable:
            await self._start(e, token, remote_id)
        else:
            e.auth_token = token
            await self.st.report([e])

    async def end(self, e: Evse, reason: str, token: str | None = None) -> None:
        tx = e.tx
        if tx is None:
            return
        tx.charging = False
        e.tx, e.finishing = None, e.cable
        await self.st.report([e])                               # Finishing (cable in) or Available
        version = self.st.version

        async def send(cp, offline: bool):
            if tx.server_id is None:
                self.st.bus.note(self.st.id, "StopTransaction without a transactionId (StartTransaction unanswered)",
                                 "warn")
                return
            await self.st.call(cp, version.call.StopTransaction(
                meter_stop=int(tx.energy_wh), timestamp=now_iso(), transaction_id=tx.server_id, reason=reason,
                **({"id_tag": token} if token else {})))
        await self.st.send_tx(send, f"StopTransaction ({reason})")
        await self.st.after_transaction(e)

    async def unplug(self, e: Evse) -> None:
        e.auth_token = None
        if e.tx:                                                # StopTransactionOnEVSideDisconnect = true
            await self.end(e, "EVDisconnected")
        e.finishing = False
        await self.st.report([e])                               # Available

    async def fault(self, e: Evse) -> None:
        if e.tx:
            await self.end(e, "Other")
        await self.st.report([e])

    async def reset_stop(self, e: Evse) -> None:
        await self.end(e, "SoftReset")

    async def meter(self, e: Evse) -> None:
        tx, version = e.tx, self.st.version

        async def send(cp, offline: bool):
            if tx.server_id is not None:
                await self.st.call(cp, version.call.MeterValues(connector_id=e.id, transaction_id=tx.server_id,
                                                                meter_value=self._meter(tx)))
        await self.st.send_tx(send, "MeterValues", quiet=True)

    def find_tx(self, transaction_id) -> Evse | None:
        return next((e for e in self.st.evses.values() if e.tx and e.tx.server_id == int(transaction_id)), None)


def behaviour_for(version, station):
    return (Behaviour2x if version.is_2x else Behaviour16)(station)
