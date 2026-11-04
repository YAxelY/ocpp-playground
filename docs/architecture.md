# Architecture

The playground is one Python process with three roles: a **CSMS** (the backend), any number of simulated **charging
stations**, and a **dashboard**. The stations and the CSMS talk to each other over **real WebSocket connections**
(OCPP-J), exactly as they would across the internet. You can capture that traffic with Wireshark on the loopback
interface (`tcp.port == 9000`).

## 1. System view

```mermaid
flowchart LR
    subgraph Browser
        UI["Dashboard<br/>index.html · app.js"]
    end
    subgraph Process["python -m playground"]
        WEB["web.server.Dashboard<br/>HTTP + WebSocket API :8000"]
        BUS(("events.Bus"))
        subgraph CSMSBOX["CSMS :9000"]
            SRV["csms.server.Csms"]
        end
        subgraph Stations["Simulated stations"]
            S1["Station CS-001<br/>offers 2.1, 2.0.1"]
            S2["Station CS-002<br/>offers 2.0.1"]
            S3["Station CS-003<br/>offers 1.6"]
        end
    end
    UI <-->|"ws /ws : commands ↑ events ↓"| WEB
    S1 -->|"OCPP-J ws://…/ocpp/CS-001"| SRV
    S2 -->|"OCPP-J"| SRV
    S3 -.->|"refused at handshake"| SRV
    SRV -- "frames, notes, state" --> BUS
    Stations -- "notes, state" --> BUS
    BUS --> WEB
    WEB -- "buttons" --> Stations
    WEB -- "policy, CSMS requests" --> SRV
```

## 2. Layers and packages

Dependencies point downwards only. `protocol` knows nothing about networking or state, so it's reusable and easy to
unit-test.

```mermaid
flowchart TB
    web["<b>web</b><br/>server.py · static/"]
    app["<b>app</b><br/>wiring, CLI"]
    csms["<b>csms</b><br/>server · gatekeeper · handlers · commands · state"]
    station["<b>station</b><br/>simulator · handlers · model"]
    protocol["<b>protocol</b><br/>frames · versions · schemas"]
    events["<b>events</b><br/>Bus"]
    lib[["ocpp library<br/>call / call_result / ChargePoint / JSON schemas"]]
    ws[["websockets library"]]

    app --> web & csms & station
    web --> csms & protocol & events
    csms --> protocol & events & lib & ws
    station --> protocol & events & lib & ws
    protocol --> lib
```

| Package | Files | Responsibility |
|---|---|---|
| `protocol` | `frames.py` | Parse/serialise the five OCPP-J frame types; detect malformed frames and pick the RPC error code |
| | `versions.py` | `OcppVersion` objects for 2.0.1 and 2.1: subprotocol, allowed frame types, library modules, negotiation |
| | `schemas.py` | Load the official JSON schemas, validate payloads, map JSON-schema keywords to RPC error codes, diff two versions |
| `csms` | `server.py` | WebSocket server, subprotocol negotiation, `GuardedConnection` (logging + gatekeeper + fault injection) |
| | `gatekeeper.py` | **All protocol rules** applied before the library: frame validity, version frame types, registration gate, schema, NotImplemented / NotSupported |
| | `handlers.py` | Business logic for each station → CSMS request (one mixin, any version) |
| | `commands.py` | CSMS → station requests (GetVariables, Reset, RequestStartTransaction, …), CALLRESULTERROR for bad answers |
| | `state.py` | `Policy` (the knobs) and `StationRecord` / `Transaction` (what the CSMS knows) |
| `station` | `simulator.py` | Station life cycle: connect, boot, reports, heartbeats, transactions, offline queue, back-off, 2.1 streams, edge cases |
| | `handlers.py` | How the station answers CSMS requests (Get/SetVariables, ChangeAvailability, Reset, TriggerMessage, …) |
| | `model.py` | EVSEs, transactions, device-model variables |
| `web` | `server.py`, `static/` | Static files + WebSocket API; the dashboard renders state and draws the sequence diagram |
| `events` | `events.py` | Pub/sub bus: frames, notes (with spec reference), throttled state snapshots |
| `tools` | `schema_report.py` | Generates [ocpp-2.0.1-vs-2.1.md](ocpp-2.0.1-vs-2.1.md) |

## 3. Why a gatekeeper in front of the `ocpp` library?

The [`ocpp`](https://github.com/mobilityhouse/ocpp) library handles framing, routing and call/response correlation
well. But it only knows message types 2, 3 and 4, and it makes some error-handling choices of its own:

| Situation | `ocpp` library (as used here) | Playground (gatekeeper) | Reference |
|---|---|---|---|
| Unknown action | `NotSupported` (2.0.1), **no answer** (2.1) | `NotImplemented` ("action not known") | Part 4 §4.3 |
| Known action without handler | `NotImplemented` | `NotSupported` ("recognised but not supported") | Part 4 §4.3 |
| Unreadable frame | logs, no answer | CALLERROR with id `"-1"` (or drop, configurable) | Part 4 §4.2.3 |
| Enum violation | `FormatViolation` | `PropertyConstraintViolation` | Part 4 §4.3 |
| Missing required field | `ProtocolError` | `OccurrenceConstraintViolation` | Part 4 §4.3 |
| MessageTypeId 5 / 6 | not supported | per version: 2.0.1 → MessageTypeNotSupported, 2.1 → handled | Part 4 §4.1.3, §4.4 |
| Request before Accepted | answered normally | CALLERROR `SecurityError` | B01.FR.10, B02.FR.09, B03.FR.07 |

So every incoming frame goes through the gatekeeper first. Only what passes reaches the library:

```mermaid
flowchart TD
    IN([raw text from WebSocket]) --> P{"parse()<br/>valid OCPP-J?"}
    P -- "no, id unreadable" --> U{"policy:<br/>unreadable_frames"}
    U -- answer --> R1["CALLERROR id -1<br/>RpcFrameworkError"]
    U -- drop --> D1([drop + note])
    P -- "no, id readable" --> M["CALLERROR (CALL) /<br/>CALLRESULTERROR (2.1 result)"]
    P -- "unknown MessageTypeId" --> V{"version?"}
    P -- yes --> T{"frame type allowed<br/>in this version?"}
    T -- no --> V
    V -- 2.0.1 --> R2["CALLERROR<br/>MessageTypeNotSupported"]
    V -- 2.1 --> D2([ignore + note])
    T -- "CALLRESULT / CALLERROR" --> F([forward to library])
    T -- "CALLRESULTERROR (2.1)" --> N([note: station rejected our answer])
    T -- "SEND (2.1)" --> SV{"schema valid?"}
    SV -- yes --> PS([process, never answer])
    SV -- no --> D3([drop silently])
    T -- CALL --> DUP{"message id<br/>reused?"}
    DUP -- "yes + policy reject" --> R3["CALLERROR RpcFrameworkError"]
    DUP -- no --> A{"action known<br/>in this version?"}
    A -- no --> R4["CALLERROR NotImplemented"]
    A -- yes --> REG{"BootNotification<br/>or station Accepted?"}
    REG -- no --> TR{"triggered by<br/>the CSMS?"}
    TR -- no --> R5["CALLERROR SecurityError"]
    TR -- yes --> SC
    REG -- yes --> SC{"payload matches<br/>JSON schema?"}
    SC -- no --> R6["CALLERROR<br/>Type/Occurrence/Property-<br/>ConstraintViolation or FormatViolation"]
    SC -- yes --> H{"handler<br/>implemented?"}
    H -- no --> R7["CALLERROR NotSupported"]
    H -- yes --> F
```

`gatekeeper.inspect()` is a pure function of *(raw text, version, station record, policy, handled actions)*, so all of
these branches are unit-tested without a network ([tests/test_gatekeeper.py](../tests/test_gatekeeper.py)).

## 4. Versions: one code base, two protocols

```mermaid
classDiagram
    class OcppVersion {
        name
        subprotocol
        frame_types
        answers_unknown_type
        ChargePoint
        call
        call_result
        schema_dir
    }
    class CsmsHandlers {
        R
        on_boot_notification()
        on_heartbeat()
        on_status_notification()
        on_authorize()
        on_transaction_event()
        on_open_stream()
    }
    class StationHandlers {
        on_get_variables()
        on_set_variables()
        on_change_availability()
        on_reset()
        on_request_start()
        on_trigger()
    }
    CsmsHandlers <|-- CsmsConnection_2_1
    CsmsHandlers <|-- CsmsConnection_2_0_1
    StationHandlers <|-- StationConnection_2_1
    StationHandlers <|-- StationConnection_2_0_1
    OcppVersion ..> CsmsConnection_2_1 : csms_class(V21)
    OcppVersion ..> StationConnection_2_0_1 : station_class(V201)
```

The handler logic is written once as a mixin. `csms_class(version)` and `station_class(version)` combine it with the
library's `ChargePoint` for that version, and set `R` to the matching `call_result` module. The version is picked
during the WebSocket handshake:

```mermaid
sequenceDiagram
    participant CS as Station (offers ocpp2.1, ocpp2.0.1)
    participant CSMS as CSMS (enabled: 2.1 > 2.0.1)
    CS->>CSMS: GET /ocpp/CS-001 · Sec-WebSocket-Protocol: ocpp2.1, ocpp2.0.1
    Note over CSMS: versions.select(offered, enabled):<br/>first enabled version the station offered
    CSMS-->>CS: 101 Switching Protocols · Sec-WebSocket-Protocol: ocpp2.1
    Note over CS,CSMS: both sides now use the 2.1 classes and schemas
```

## 5. State: two points of view

The dashboard always shows **what the station thinks** next to **what the CSMS thinks**. Most interesting bugs live in
the gap between the two (e.g. after a network drop the station still believes it's Accepted, while the CSMS shows it
Offline).

```mermaid
classDiagram
    class Station {
        registration
        link
        network_up
        variables
        tx_queue
        streams
    }
    class Evse {
        status
        operative
        cable
        fault
    }
    class TxState {
        id
        seq
        token
        energy_wh
    }
    class StationRecord {
        registration
        boots
        online
        last_seen
        connectors
        allowed_once
        seen_ids
    }
    class Transaction {
        state
        charging_state
        last_seq
        seq_gaps
        energy_wh
        offline_events
    }
    class Policy {
        versions
        registration_mode
        heartbeat_interval
        retry_interval
        unreadable_frames
        duplicate_ids
        authorize_detail
        tokens
    }
    Station "1" *-- "1..n" Evse
    Evse "1" *-- "0..1" TxState
    StationRecord "1" *-- "*" Transaction
    Csms "1" *-- "*" StationRecord
    Csms "1" *-- "1" Policy
```

## 6. Events to the dashboard

```mermaid
sequenceDiagram
    participant B as Browser
    participant D as Dashboard (web)
    participant Bus
    participant C as CSMS / Stations
    B->>D: open ws /ws
    D->>B: hello {history, state, meta (schema list)}
    C->>Bus: frame / note
    Bus->>D: event
    D->>B: event (drawn as an arrow or a note)
    C->>Bus: changed()
    Note over Bus: snapshots coalesced, at most every 100 ms
    Bus->>D: state
    D->>B: state (cards re-rendered)
    B->>D: {"cmd":"station","action":"plug","evse":1}
    D->>C: Station.plug(1)
```
