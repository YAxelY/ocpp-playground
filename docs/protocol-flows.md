# Protocol flows implemented

Every flow below runs in the playground and is covered by an end-to-end test
([tests/test_end_to_end.py](../tests/test_end_to_end.py)). References point to the OCA specification: **Part 2**
use cases (e.g. `B02.FR.09`) and **Part 4** (OCPP-J) sections. The text is paraphrased, not copied.

## 1. Connect and boot: Pending, then Accepted (B01, B02)

```mermaid
sequenceDiagram
    autonumber
    participant CS as Charging Station
    participant CSMS
    CS->>CSMS: WebSocket handshake (offers ocpp2.1, ocpp2.0.1)
    CSMS-->>CS: agreed: ocpp2.1
    CS->>CSMS: [2] BootNotification (reason PowerUp)
    CSMS-->>CS: [3] status Pending, interval 5
    Note over CS: B02.FR.02: only BootNotification may be sent now<br/>B02.FR.04: not before the interval
    opt CSMS uses the Pending time (B02.FR.01)
        CSMS->>CS: [2] GetBaseReport
        CS-->>CSMS: [3] Accepted
        CS->>CSMS: [2] NotifyReport (allowed: the CSMS asked for it)
        CSMS-->>CS: [3]
    end
    CS->>CSMS: [2] BootNotification
    CSMS-->>CS: [3] status Accepted, interval 10
    loop every connector (B01.FR.05)
        CS->>CSMS: [2] StatusNotification (Available)
        CSMS-->>CS: [3]
    end
    loop every interval without other traffic (G02)
        CS->>CSMS: [2] Heartbeat
        CSMS-->>CS: [3] currentTime
    end
```

**If a station that isn't Accepted sends anything else**, the CSMS answers `CALLERROR SecurityError`
(B01.FR.10 · B02.FR.09 · B03.FR.07). Try it: put the CSMS on *Pending*, then press *Send something wrong → Heartbeat now*.

## 2. Registration as seen by the station

```mermaid
stateDiagram-v2
    [*] --> Off
    Off --> Connecting: power on
    Connecting --> Booting: handshake OK
    Connecting --> Backoff: refused / no network
    Booting --> Pending: status Pending
    Booting --> Rejected: status Rejected
    Booting --> Accepted: status Accepted
    Pending --> Booting: after interval (or TriggerMessage)
    Rejected --> Booting: after interval (B03.FR.06)
    Accepted --> Backoff: connection lost
    Backoff --> Connecting: back-off elapsed (Part 4 §5.4)
    Connecting --> Accepted: reconnect, no new boot (Part 4 §5.4)
    Accepted --> Booting: Reset / reboot
    Accepted --> Off: power off
```

## 3. Connector status (G01) and availability (G03)

```mermaid
stateDiagram-v2
    [*] --> Available
    Available --> Occupied: cable plugged in (G01.FR.03)
    Occupied --> Available: cable unplugged (G01.FR.04)
    Available --> Unavailable: ChangeAvailability Inoperative
    Unavailable --> Available: ChangeAvailability Operative
    Occupied --> Occupied: ChangeAvailability while charging → Scheduled
    Available --> Faulted: fault
    Occupied --> Faulted: fault (ends the transaction)
    Faulted --> Available: fault cleared
```

## 4. Transaction: cable first, then card (E02, E07)

The **station** generates the transaction id (Part 0 §2.8.2), and `seqNo` increases by one per event so the CSMS can
detect gaps.

```mermaid
sequenceDiagram
    participant EV as Driver / EV
    participant CS as Charging Station
    participant CSMS
    EV->>CS: plug in cable
    CS->>CSMS: StatusNotification Occupied
    CS->>CSMS: TransactionEvent Started (CablePluggedIn, seqNo 0, EVConnected)
    EV->>CS: present card AXEL-0001
    CS->>CSMS: Authorize (idToken)
    CSMS-->>CS: idTokenInfo Accepted
    CS->>CSMS: TransactionEvent Updated (Authorized, Charging, idToken)
    loop every TxUpdatedInterval
        CS->>CSMS: TransactionEvent Updated (MeterValuePeriodic, Energy.Active.Import.Register)
    end
    EV->>CS: present the same card again
    CS->>CSMS: TransactionEvent Ended (StopAuthorized, stoppedReason Local)
    EV->>CS: unplug
    CS->>CSMS: StatusNotification Available
```

Other start and stop paths in the simulator:

| Path | Trigger | Use case |
|---|---|---|
| Card first, cable later | `Started` with Authorized, chargingState Idle, then `Updated` with CablePluggedIn | E03 |
| Remote start, cable later | RequestStartTransaction → `Started` with RemoteStart and remoteStartId | F02 |
| Remote start, cable already in | RequestStartTransaction → `Updated` with RemoteStart | F01 |
| Remote stop | RequestStopTransaction → `Ended` with RemoteStop, stoppedReason Remote | F03 |
| Cable pulled while charging | `Ended` with EVCommunicationLost, stoppedReason EVDisconnected | E09 |
| Fault while charging | `Ended` with AbnormalCondition, stoppedReason Other | — |
| Reset while charging | `Ended` with ResetCommand, stoppedReason ImmediateReset | B12 |
| Pending station receives RequestStartTransaction | answers `Rejected` | B02.FR.05 |

## 4b. The same session in OCPP 1.6 (1.6 §4.1, §4.7–4.10)

In 1.6 a session is three different messages, the **CSMS** assigns the transaction id, and the connector goes through
more statuses (1.6 §4.9: *Occupied* was split into Preparing, Charging, SuspendedEV/EVSE and Finishing).

```mermaid
sequenceDiagram
    participant EV as Driver / EV
    participant CP as Charge point (1.6)
    participant CSMS
    EV->>CP: plug in cable
    CP->>CSMS: StatusNotification connector 1 · Preparing · errorCode NoError
    EV->>CP: present card AXEL-0001
    CP->>CSMS: Authorize (idTag)
    CSMS-->>CP: idTagInfo Accepted
    CP->>CSMS: StartTransaction (connectorId 1, idTag, meterStart, timestamp)
    CSMS-->>CP: transactionId 1001 + idTagInfo
    Note over CSMS: the CSMS chose the id (1.6 §4.8)
    CP->>CSMS: StatusNotification · Charging
    loop every MeterValueSampleInterval
        CP->>CSMS: MeterValues (connectorId 1, transactionId 1001)
    end
    EV->>CP: same card again
    CP->>CSMS: StopTransaction (transactionId 1001, meterStop, reason Local)
    CP->>CSMS: StatusNotification · Finishing
    EV->>CP: unplug
    CP->>CSMS: StatusNotification · Available
```

```mermaid
stateDiagram-v2
    [*] --> Available
    Available --> Preparing: cable in, or card / RemoteStart first
    Preparing --> Charging: authorized + cable → StartTransaction
    Charging --> Finishing: StopTransaction (card, RemoteStop)
    Charging --> Available: cable pulled → StopTransaction EVDisconnected
    Finishing --> Available: cable out
    Preparing --> Available: cable out / card withdrawn
    Available --> Unavailable: ChangeAvailability Inoperative
    Unavailable --> Available: ChangeAvailability Operative
    Charging --> Faulted: fault → StopTransaction Other
    Faulted --> Available: repaired
```

At boot a 1.6 charge point also reports **connectorId 0** (the charge point itself, 1.6 §3.8), so the CSMS sees
connectors `0`, `1`, `2`… A transaction started while offline is queued; its id only exists once the queued
StartTransaction reaches the CSMS after the reconnect.

## 5. Connection loss, back-off and offline behaviour (Part 4 §5.4, B04, E04/E12)

```mermaid
sequenceDiagram
    participant CS as Charging Station
    participant CSMS
    Note over CS,CSMS: Accepted, charging
    CS--xCSMS: network lost
    Note over CSMS: WebSocket closed → Offline<br/>(registration kept)
    Note over CS: keeps charging (B04)<br/>TransactionEvents queued with offline=true
    loop attempt n
        Note over CS: wait = RetryBackOffWaitMinimum × 2^min(n, RepeatTimes)<br/>+ random(0..RetryBackOffRandomRange)
        CS--xCSMS: connect fails
    end
    CS->>CSMS: reconnect (no BootNotification: boot data unchanged)
    alt offline longer than OfflineThreshold (B04.FR.01)
        CS->>CSMS: StatusNotification for every connector
    else shorter (B04.FR.02)
        CS->>CSMS: StatusNotification only for changed connectors
    end
    CS->>CSMS: queued TransactionEvents (offline=true), in order
```

## 6. OCPP 2.1: periodic event stream with SEND (N11 scenario 2)

```mermaid
sequenceDiagram
    participant CS as Charging Station (2.1)
    participant CSMS
    CS->>CSMS: [2] OpenPeriodicEventStream (id 1, interval 3, values 3)
    CSMS-->>CS: [3] status Accepted
    loop every 3 values
        CS-)CSMS: [6] SEND NotifyPeriodicEventStream {id, pending, basetime, data:[{t, v}…]}
        Note over CSMS: never answered (2.1 Part 4 §4.2.4)<br/>may cross an open CALL
    end
    CS->>CSMS: [2] ClosePeriodicEventStream
    CSMS-->>CS: [3]
```

The same `[6, …]` frame sent on a **2.0.1** connection gets `CALLERROR MessageTypeNotSupported`, because 2.0.1 only knows
types 2, 3 and 4 (2.0.1 Part 4 §4.4).

## 7. OCPP 2.1: CALLRESULTERROR

Turn on *Fault injection → HeartbeatResponse without currentTime*. The CSMS then sends a schema-invalid answer, and:

```mermaid
sequenceDiagram
    participant CS21 as Station on 2.1
    participant CS201 as Station on 2.0.1
    participant CSMS
    CS21->>CSMS: [2] Heartbeat
    CSMS-->>CS21: [3] {} (currentTime missing)
    CS21->>CSMS: [5] CALLRESULTERROR OccurrenceConstraintViolation
    CS201->>CSMS: [2] Heartbeat
    CSMS-->>CS201: [3] {} (currentTime missing)
    Note over CS201: 2.0.1 has no way to report a bad CALLRESULT
```

## 8. CSMS → station requests

The dashboard buttons are generic; each one sends the message of the negotiated version:

| Button | 2.0.1 / 2.1 | 1.6 |
|---|---|---|
| read heartbeat | GetVariables(OCPPCommCtrlr.HeartbeatInterval) | GetConfiguration(HeartbeatInterval) |
| set heartbeat | SetVariables | ChangeConfiguration |
| full report | GetBaseReport → NotifyReport later | GetConfiguration() → values in the answer |
| reset | Reset(Immediate) | Reset(Soft) |
| change availability | ChangeAvailability(operationalStatus, evse) | ChangeAvailability(connectorId, type) |
| remote start / stop | RequestStart/StopTransaction | RemoteStart/StopTransaction (not sent while Pending, 1.6 §4.2) |
| unlock | UnlockConnector(evseId, connectorId) | UnlockConnector(connectorId) |

2.x details:

| Dashboard button | Request | Station behaviour |
|---|---|---|
| GetVariables | `GetVariables(OCPPCommCtrlr.HeartbeatInterval)` | Accepted + value, or UnknownComponent / UnknownVariable (B06) |
| SetVariables | `SetVariables(OCPPCommCtrlr.HeartbeatInterval)` | Accepted, or Rejected for read-only (B05). The heartbeat loop picks up the new value |
| GetBaseReport | `GetBaseReport(FullInventory)` | Accepted, then `NotifyReport` with all variables. Allowed while Pending (B07, B02.FR.02) |
| Reset | `Reset(Immediate)` | Accepted, ends transactions, reconnects with BootNotification reason RemoteReset (B11/B12) |
| TriggerMessage | `TriggerMessage(StatusNotification / Heartbeat / BootNotification / MeterValues)` | Accepted and sends it, or NotImplemented (MeterValues) (F06) |
| ChangeAvailability | `ChangeAvailability(Inoperative / Operative)` | Accepted → StatusNotification Unavailable / Available; Scheduled if a transaction runs (G03/G04) |
| RequestStartTransaction | `RequestStartTransaction(EVSE 1, FLEET-0042)` | F01/F02, Rejected while Pending (B02.FR.05) |
| RequestStopTransaction | `RequestStopTransaction(transactionId)` | F03 |

While a station is **Rejected**, the CSMS refuses to send it anything (B03.FR.03).
