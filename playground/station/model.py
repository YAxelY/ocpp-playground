"""The simulated station's hardware model: EVSEs/connectors, transactions and configuration."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TxState:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])   # local id (2.x: sent to the CSMS)
    server_id: int | None = None      # 1.6: the transactionId the CSMS assigned in StartTransaction.conf
    seq: int = -1                     # 2.x seqNo; the first event gets 0
    token: str | None = None
    charging: bool = False
    energy_wh: float = 0.0
    remote_start_id: int | None = None
    started: float = field(default_factory=time.time)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    @property
    def shown_id(self) -> str:
        return str(self.server_id) if self.server_id is not None else self.id


@dataclass
class Evse:
    """One EVSE with one connector (2.x: evseId N / connectorId 1; 1.6: connectorId N)."""
    id: int
    status: str = "Available"         # last status reported (version-specific vocabulary)
    operative: bool = True
    cable: bool = False
    fault: bool = False
    tx: TxState | None = None
    auth_token: str | None = None     # authorized, waiting for the cable (1.6 Preparing)
    finishing: bool = False           # 1.6: transaction stopped, cable still in
    changed_while_offline: bool = False
    pending_operative: bool | None = None    # ChangeAvailability "Scheduled" until the transaction ends


def status_2x(e: Evse) -> str:
    """ConnectorStatusEnumType (2.0.1/2.1): Available, Occupied, Reserved, Unavailable, Faulted."""
    if e.fault:
        return "Faulted"
    if not e.operative:
        return "Unavailable"
    return "Occupied" if e.cable else "Available"


def status_16(e: Evse) -> str:
    """ChargePointStatus (1.6 §4.9): Available, Preparing, Charging, SuspendedEV/EVSE, Finishing, Reserved, Unavailable, Faulted."""
    if e.fault:
        return "Faulted"
    if not e.operative and not e.tx:
        return "Unavailable"
    if e.tx and e.tx.charging:
        return "Charging"
    if e.finishing and e.cable:
        return "Finishing"
    if e.cable or e.auth_token or e.tx:
        return "Preparing"
    return "Available"


# Device model (2.x) and configuration keys (1.6) share one store: "Component.Variable" -> [value, mutability].
def default_variables(heartbeat: int = 10) -> dict:
    return {
        "OCPPCommCtrlr.HeartbeatInterval": [heartbeat, "ReadWrite"],
        "OCPPCommCtrlr.OfflineThreshold": [20, "ReadWrite"],
        "OCPPCommCtrlr.RetryBackOffWaitMinimum": [2, "ReadWrite"],
        "OCPPCommCtrlr.RetryBackOffRandomRange": [2, "ReadWrite"],
        "OCPPCommCtrlr.RetryBackOffRepeatTimes": [3, "ReadWrite"],
        "SampledDataCtrlr.TxUpdatedInterval": [5, "ReadWrite"],
        "TxCtrlr.TxStartPoint": ["EVConnected,Authorized", "ReadOnly"],
        "TxCtrlr.TxStopPoint": ["EVConnected,Authorized", "ReadOnly"],
        "ChargingStation.Model": ["Playground-1", "ReadOnly"],
        "ChargingStation.VendorName": ["OCPP Playground", "ReadOnly"],
    }


# 1.6 configuration keys (1.6 §9 Standard Configuration Key Names) → where the value lives.
# A string target is a variable key; a tuple is a fixed read-only value.
CONFIG_16 = {
    "HeartbeatInterval": "OCPPCommCtrlr.HeartbeatInterval",
    "MeterValueSampleInterval": "SampledDataCtrlr.TxUpdatedInterval",
    "ConnectionTimeOut": ("60",),
    "AuthorizeRemoteTxRequests": ("false",),
    "LocalPreAuthorize": ("false",),
    "StopTransactionOnEVSideDisconnect": ("true",),
    "SupportedFeatureProfiles": ("Core,RemoteTrigger",),
    "MeterValuesSampledData": ("Energy.Active.Import.Register",),
}
