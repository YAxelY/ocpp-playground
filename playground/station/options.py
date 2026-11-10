"""Optional and configurable station behaviour, one entry per choice the specifications leave open.

Each option says where it lives in each version:

  dm      the OCPP 2.0.1 / 2.1 Device Model variable ("Component.Variable"); the CSMS can read/write it
          with GetVariables / SetVariables and it appears in NotifyReport.
  key16   the OCPP 1.6 configuration key (§9); readable/writable with GetConfiguration / ChangeConfiguration.
  neither an implementation choice: the spec allows several behaviours (MAY / SHOULD / "or") but defines
          no variable for it. Only the dashboard can change it.

Defaults keep the playground's earlier behaviour unless the specification prefers something else
(2.1 connector status is now reported with NotifyEvent, the preferred method of 2.1 G01).
"""
from __future__ import annotations

from dataclasses import dataclass, field

MEASURANDS = ("Energy.Active.Import.Register", "Power.Active.Import", "Current.Import", "Voltage", "SoC")
TX_POINTS = ("EVConnected", "Authorized", "PowerPathClosed", "EnergyTransfer")
ALL = ("1.6", "2.0.1", "2.1")
X2 = ("2.0.1", "2.1")


@dataclass(frozen=True)
class Option:
    key: str                      # dm name for Device Model variables, else a short id
    label: str
    kind: str                     # bool | int | choice | members
    default: object
    group: str
    help: str
    versions: tuple = ALL
    dm: bool = False              # a 2.0.1/2.1 Device Model variable
    key16: str | None = None      # the 1.6 configuration key
    choices: tuple = ()
    ref16: str = ""
    ref2x: str = ""
    labels: dict = field(default_factory=dict)   # choice value -> text

    def applies(self, version: str) -> bool:
        return version in self.versions

    def name_for(self, version: str) -> str:
        if version == "1.6":
            return self.key16 or "(no 1.6 configuration key)"
        return self.key if self.dm else "(no Device Model variable)"


OPTIONS: tuple[Option, ...] = (
    # ------------------------------------------------------------------ B / G: boot, status, heartbeat
    Option("StatusReport21", "How connector status is reported (2.1)", "choice", "NotifyEvent", "Status & heartbeat",
           "2.1 prefers NotifyEvent(Connector.AvailabilityState); StatusNotification still works but is deprecated. "
           "'both' sends both, like the certification tool does when it tests a CSMS.",
           versions=("2.1",), choices=("NotifyEvent", "StatusNotification", "both"),
           ref2x="2.1 B01.FR.05 · G01 (alternative scenario) · G01.FR.03-07",
           labels={"NotifyEvent": "NotifyEvent AvailabilityState (preferred)",
                   "StatusNotification": "StatusNotification (deprecated in 2.1)", "both": "both messages"}),
    Option("StatusReportEvseAndStation", "Also report EVSE and ChargingStation AvailabilityState (2.1)", "bool", False,
           "Status & heartbeat", "G01 step 1a: the NotifyEvent may also carry the state of the EVSE and of the whole station.",
           versions=("2.1",), ref2x="2.1 G01 scenario 1a"),
    Option("ReportConnectorZero", "Report connectorId 0 (the whole charge point) after boot", "bool", True,
           "Status & heartbeat", "1.6 lets the charge point report its own status with connectorId 0 "
           "(only Available, Unavailable or Faulted).", versions=("1.6",), ref16="1.6 §4.9 · §3.8"),
    Option("MinimumStatusDuration", "MinimumStatusDuration (s)", "int", 0, "Status & heartbeat",
           "Skip StatusNotifications for statuses that lasted less than this many seconds (e.g. a short SuspendedEV).",
           versions=("1.6",), key16="MinimumStatusDuration", ref16="1.6 §4.9 · §9.1.20"),
    Option("HeartbeatResetOnTraffic", "Any message resets the heartbeat timer", "bool", True, "Status & heartbeat",
           "On: no Heartbeat while other messages flow. Off: a Heartbeat exactly every interval.",
           ref16="1.6 §4.6 (MAY skip)", ref2x="G02.FR.05 (RECOMMENDED) · G02.FR.07"),
    Option("ClockCtrlr.TimeSource", "Clock source", "choice", "Heartbeat", "Status & heartbeat",
           "With Heartbeat the station sets its clock from currentTime in BootNotification/Heartbeat answers; with "
           "RealTimeClock it trusts its own clock (see 'clock error at power-on').",
           dm=True, choices=("Heartbeat", "RealTimeClock"), ref16="1.6 §4.6 (RECOMMENDED to sync)",
           ref2x="B01.FR.06 · G02.FR.06 · ClockCtrlr.TimeSource"),
    Option("ClockErrorAtStart", "Clock error at power-on (s)", "int", 0, "Status & heartbeat",
           "Simulates a station whose clock is wrong after a power cut (e.g. -3600). Every timestamp it sends uses its own clock.",
           ref16="1.6 §4.6", ref2x="B01.FR.06"),
    Option("BootWaitWhenZero", "Own wait when the boot interval is 0 (s)", "int", 10, "Status & heartbeat",
           "Pending/Rejected with interval 0: the station picks its own wait, without flooding the CSMS.",
           ref16="1.6 §4.2", ref2x="B02.FR.06 · B03.FR.05"),
    Option("ResetReportUnavailable", "Report connectors Unavailable before a reset", "bool", False, "Status & heartbeat",
           "B11.FR.03: before rebooting, the station MAY send NotifyEvent(AvailabilityState = Unavailable) for its connectors.",
           versions=X2, ref2x="B11.FR.03 / B11.FR.08"),
    # ------------------------------------------------------------------ C: authorization
    Option("AuthCacheCtrlr.Enabled", "Authorization cache", "bool", True, "Authorization",
           "Remember the answers about cards (from Authorize, TransactionEvent / StartTransaction answers).",
           dm=True, key16="AuthorizationCacheEnabled", ref16="1.6 §3.4 · §9.1.2", ref2x="C10 · AuthCacheCtrlr.Enabled"),
    Option("AuthCtrlr.LocalPreAuthorize", "Local pre-authorize (online)", "bool", False, "Authorization",
           "Online: start at once for a card the cache knows as Accepted, without waiting for Authorize.",
           dm=True, key16="LocalPreAuthorize", ref16="1.6 §3.4 · §9.1.13", ref2x="C12 · AuthCtrlr.LocalPreAuthorize"),
    Option("AuthCtrlr.LocalAuthorizeOffline", "Local authorize offline", "bool", True, "Authorization",
           "Offline: accept a card the cache knows as Accepted.",
           dm=True, key16="LocalAuthorizeOffline", ref16="1.6 §3.5 · §9.1.12", ref2x="C12 · AuthCtrlr.LocalAuthorizeOffline"),
    Option("AuthCtrlr.OfflineTxForUnknownIdEnabled", "Offline: accept unknown cards", "bool", True, "Authorization",
           "Offline: also accept cards the station has never seen. The CSMS checks them afterwards.",
           dm=True, key16="AllowOfflineTxForUnknownId", ref16="1.6 §3.5 · §9.1.1", ref2x="C15 · AuthCtrlr.OfflineTxForUnknownIdEnabled"),
    Option("AuthCtrlr.AuthorizeRemoteStart", "Authorize remote starts", "bool", False, "Authorization",
           "On: a remote start (RequestStartTransaction / RemoteStartTransaction) is first checked with an Authorize, like a card.",
           dm=True, key16="AuthorizeRemoteTxRequests", ref16="1.6 §5.11 · §9.1.3", ref2x="F01/F02 · AuthCtrlr.AuthorizeRemoteStart"),
    # ------------------------------------------------------------------ E: transactions
    Option("TxCtrlr.TxStartPoint", "Transaction start point", "members", "EVConnected,Authorized", "Transactions",
           "Which event(s) start a transaction: cable connected, card accepted, both (PowerPathClosed) or energy flowing.",
           versions=X2, dm=True, choices=TX_POINTS, ref2x="E01 · Part 2 E.1.1 / §2.6.6.1"),
    Option("TxCtrlr.TxStopPoint", "Transaction stop point", "members", "EVConnected,Authorized", "Transactions",
           "Which event(s) end it: cable removed, authorization ended, either (PowerPathClosed) or energy stopping.",
           versions=X2, dm=True, choices=TX_POINTS, ref2x="E06 · Part 2 §2.6.6.2"),
    Option("TxCtrlr.EVConnectionTimeOut", "Cable timeout after authorization (s)", "int", 60, "Transactions",
           "Card accepted but no cable within this time: the authorization (and a started transaction) is cancelled.",
           dm=True, key16="ConnectionTimeOut", ref16="1.6 §4.9 (B1) · §9.1.6", ref2x="C01.FR.26 · E03.FR.05"),
    Option("TxCtrlr.StopTxOnInvalidId", "Stop on invalid id", "bool", True, "Transactions",
           "The CSMS says the card of a running transaction is not Accepted: stop it (on) or only stop the energy (off).",
           dm=True, key16="StopTransactionOnInvalidId", ref16="1.6 §3.4 · §9.1.24", ref2x="E05.FR.02/09/10"),
    Option("TxCtrlr.StopTxOnEVSideDisconnect", "Stop when the cable is unplugged from the car", "bool", True,
           "Transactions", "Off (1.6): unplugging only suspends the transaction (SuspendedEV) until it is stopped by card "
           "or remotely. In 2.x the TxStopPoint decides, so this stays on.",
           dm=True, key16="StopTransactionOnEVSideDisconnect", ref16="1.6 §9.1.23", ref2x="TxCtrlr.StopTxOnEVSideDisconnect"),
    # ------------------------------------------------------------------ J: meter values
    Option("SampledDataCtrlr.TxUpdatedMeasurands", "Measurands sent while charging", "members",
           "Energy.Active.Import.Register", "Meter values", "What is sampled every TxUpdatedInterval / MeterValueSampleInterval.",
           dm=True, key16="MeterValuesSampledData", choices=MEASURANDS, ref16="1.6 §9.1.17", ref2x="J02 · SampledDataCtrlr"),
    Option("SampledDataCtrlr.TxStartedMeasurands", "Measurands at transaction start", "members",
           "Energy.Active.Import.Register", "Meter values", "Sent with context Transaction.Begin in TransactionEvent Started.",
           versions=X2, dm=True, choices=MEASURANDS, ref2x="E01.FR.09 · J02"),
    Option("SampledDataCtrlr.TxEndedMeasurands", "Measurands at transaction end", "members",
           "Energy.Active.Import.Register", "Meter values",
           "2.x: in TransactionEvent Ended (Transaction.End). 1.6: in StopTransaction.transactionData.",
           dm=True, key16="StopTxnSampledData", choices=MEASURANDS, ref16="1.6 §9.1.27", ref2x="E06 · J02"),
    Option("AlignedDataCtrlr.Interval", "Clock-aligned interval (s, 0 = off)", "int", 0, "Meter values",
           "Also send readings at round clock times (every N s), with context Sample.Clock.",
           dm=True, key16="ClockAlignedDataInterval", ref16="1.6 §4.7 · §9.1.5", ref2x="J01.FR.14 · J02"),
    Option("AlignedDataCtrlr.Measurands", "Clock-aligned measurands", "members", "Energy.Active.Import.Register",
           "Meter values", "What is sent at each aligned time.", dm=True, key16="MeterValuesAlignedData",
           choices=MEASURANDS, ref16="1.6 §9.1.15", ref2x="AlignedDataCtrlr.Measurands"),
    Option("AlignedDataCtrlr.SendDuringIdle", "Clock-aligned only when idle", "bool", False, "Meter values",
           "On: no clock-aligned values while a transaction is running on that EVSE.",
           versions=X2, dm=True, ref2x="J01.FR.19 · AlignedDataCtrlr.SendDuringIdle"),
    # ------------------------------------------------------------------ errors
    Option("FaultAsNotifyEvent", "Also report a fault as NotifyEvent (EVSE.Problem)", "bool", False, "Faults",
           "2.x describes problems with Device Model events in addition to the Faulted status.",
           versions=X2, ref2x="G01 · N07 (NotifyEvent)"),
)

BY_KEY = {o.key: o for o in OPTIONS}
BY_KEY16 = {o.key16: o for o in OPTIONS if o.key16}


def defaults() -> dict:
    return {o.key: o.default for o in OPTIONS}


def parse(option: Option, value):
    """Turn a value from the dashboard / SetVariables / ChangeConfiguration into the stored type, or raise ValueError."""
    if option.kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text not in ("true", "false"):
            raise ValueError("not a boolean")
        return text == "true"
    if option.kind == "int":
        number = int(value)
        if number < 0 and option.key != "ClockErrorAtStart":
            raise ValueError("negative")
        return number
    if option.kind == "choice":
        if str(value) not in option.choices:
            raise ValueError("not an allowed value")
        return str(value)
    items = [v.strip() for v in str(value).split(",") if v.strip()]
    if not items or any(v not in option.choices for v in items):
        raise ValueError("not an allowed member")
    return ",".join(items)


def text(value) -> str:
    """How a value is written in OCPP (booleans are lower case)."""
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def catalogue() -> list[dict]:
    """For the dashboard."""
    return [{"key": o.key, "label": o.label, "kind": o.kind, "default": o.default, "group": o.group, "help": o.help,
             "versions": list(o.versions), "dm": o.dm, "key16": o.key16, "choices": list(o.choices),
             "ref16": o.ref16, "ref2x": o.ref2x, "labels": o.labels} for o in OPTIONS]
