# Architecture

The playground is one Python process with three roles: a **CSMS** (the backend), any number of simulated **charging
stations**, and a **dashboard**. The stations and the CSMS talk over **real WebSocket connections** (OCPP-J), exactly as
they would across the internet. Capture the traffic with Wireshark on the loopback interface (`tcp.port == 9000`).

Three protocol versions run side by side: **OCPP 1.6**, **2.0.1** and **2.1**. The version of each connection is
negotiated in the WebSocket handshake, and everything after that (vocabulary, error codes, schemas, rules) follows it.

## 1. System view

```mermaid
flowchart LR
    subgraph Browser
        UI["Dashboard<br/>Live · How to read · Compare schemas"]
    end
    subgraph Process["python -m playground"]
        WEB["web.server.Dashboard<br/>HTTP + WebSocket API :8000"]
        BUS(("events.Bus"))
        subgraph CSMSBOX["CSMS :9000"]
            SRV["csms.server.Csms<br/>speaks 2.1 · 2.0.1 · 1.6"]
        end
        subgraph Stations["Simulated stations"]
            S1["CS-001<br/>offers 2.1, 2.0.1"]
            S2["CS-002<br/>offers 2.0.1"]
            S3["CS-003<br/>offers 1.6"]
        end
    end
    UI <-->|"ws /ws : commands ↑ events ↓"| WEB
    S1 -->|"ws://…/ocpp/CS-001 · ocpp2.1"| SRV
    S2 -->|"ocpp2.0.1"| SRV
    S3 -->|"ocpp1.6"| SRV
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
    station["<b>station</b><br/>simulator · behaviour · handlers · model"]
    protocol["<b>protocol</b><br/>frames · versions · schemas"]
    events["<b>events</b><br/>Bus"]
    tools["<b>tools</b><br/>schema_report · verify_schemas"]
    lib[["ocpp library<br/>v16 / v201 / v21: call, call_result, ChargePoint, JSON schemas"]]
    ws[["websockets library"]]

    app --> web & csms & station
    web --> csms & protocol & events
    csms --> protocol & events & lib & ws
    station --> protocol & events & lib & ws
    tools --> protocol
    protocol --> lib
```

| Package | File | Responsibility |
|---|---|---|
| `protocol` | `frames.py` | Parse/serialise the five OCPP-J frame types; detect malformed frames (logical error names) |
| | `versions.py` | `OcppVersion` for **1.6, 2.0.1, 2.1**: subprotocol, frame types, error-code vocabulary, rules that differ, library modules, negotiation |
| | `schemas.py` | Load schemas (library or official folder), validate payloads, map JSON-schema keywords to each version's error codes, compare any two versions, 1.6 → 2.x replacements |
| `csms` | `server.py` | WebSocket server, subprotocol negotiation, `GuardedConnection` (logging + gatekeeper + fault injection) |
| | `gatekeeper.py` | **All protocol rules** applied before the library, per version |
| | `handlers.py` | `CsmsHandlers2x` (TransactionEvent, NotifyReport, 2.1 streams) and `CsmsHandlers16` (StartTransaction with CSMS-assigned ids, MeterValues, StopTransaction) |
| | `commands.py` | CSMS → station requests under one generic name each (e.g. `read_heartbeat` = GetVariables in 2.x, GetConfiguration in 1.6) |
| | `state.py` | `Policy` (the knobs) and `StationRecord` / `Transaction` (what the CSMS knows) |
| `station` | `simulator.py` | Version-independent life cycle: connect, boot, reports, heartbeats, offline queue, back-off, 2.1 streams, edge cases |
| | `behaviour.py` | How physical events become messages: `Behaviour2x` (TransactionEvent) vs `Behaviour16` (Start/MeterValues/StopTransaction) |
| | `handlers.py` | How the station answers CSMS requests, per family (`StationHandlers2x`, `StationHandlers16`) |
| | `model.py` | EVSEs/connectors, transactions, status vocabularies (`status_2x`, `status_16`), configuration store |
| `web` | `server.py`, `static/` | Static files + WebSocket API; the dashboard draws stations, the sequence diagram and the schema comparison |
| `events` | `events.py` | Pub/sub bus: frames, notes (with spec reference), throttled state snapshots |
| `tools` | `schema_report.py` | Generates [ocpp-versions-compared.md](ocpp-versions-compared.md) |
| | `verify_schemas.py` | Compares the library's schemas with an official OCA folder ([schema-provenance.md](schema-provenance.md)) |

## 3. What changes with the version

```mermaid
classDiagram
    class OcppVersion {
        name
        subprotocol
        frame_types
        answers_unknown_type
        defines_unreadable_id
        ids_unique_per_identity
        request_suffix
        codes
        ChargePoint
        call
        call_result
    }
    class V16 {
        ocpp1.6
        frames 2 3 4
        FormationViolation
        ids unique per connection
    }
    class V201 {
        ocpp2.0.1
        frames 2 3 4
        MessageTypeNotSupported
        unreadable id gets minus one
    }
    class V21 {
        ocpp2.1
        frames 2 3 4 5 6
        unknown types ignored
    }
    OcppVersion <|-- V16
    OcppVersion <|-- V201
    OcppVersion <|-- V21
```

Code never writes an error code as a literal. It asks `version.code("format")`, which returns `FormationViolation`
on 1.6 and `FormatViolation` on 2.x, exactly as each specification spells it.

Handlers are written once per *family* and combined with the library's `ChargePoint` for the negotiated version:

```mermaid
classDiagram
    class CsmsCommon {
        decide_registration()
        token_status()
        tx()
    }
    class CsmsHandlers2x {
        BootNotification
        TransactionEvent
        NotifyReport
        OpenPeriodicEventStream
    }
    class CsmsHandlers16 {
        BootNotification
        StartTransaction
        MeterValues
        StopTransaction
    }
    CsmsCommon <|-- CsmsHandlers2x
    CsmsCommon <|-- CsmsHandlers16
    CsmsHandlers2x <|-- CsmsConnection_2_1
    CsmsHandlers2x <|-- CsmsConnection_2_0_1
    CsmsHandlers16 <|-- CsmsConnection_1_6
```

```mermaid
sequenceDiagram
    participant CS as Station (offers ocpp2.1, ocpp2.0.1)
    participant CSMS as CSMS (enabled: 2.1 > 2.0.1 > 1.6)
    CS->>CSMS: GET /ocpp/CS-001 · Sec-WebSocket-Protocol: ocpp2.1, ocpp2.0.1
    Note over CSMS: versions.select(offered, enabled):<br/>first enabled version the station offered
    CSMS-->>CS: 101 Switching Protocols · Sec-WebSocket-Protocol: ocpp2.1
    Note over CS,CSMS: both sides now use the 2.1 classes, schemas, error codes and rules
```

## 4. Why a gatekeeper in front of the `ocpp` library?

The [`ocpp`](https://github.com/mobilityhouse/ocpp) library handles framing, routing and call/response correlation
well. But it only knows message types 2, 3 and 4, and it makes some error-handling choices of its own:

| Situation | `ocpp` library (as used here) | Playground (gatekeeper) | Reference |
|---|---|---|---|
| Unknown action | `NotSupported` (1.6, 2.0.1), **no answer** (2.1) | `NotImplemented` ("action not known") | 1.6 J §4.2.3 · 2.x §4.3 |
| Known action without handler | `NotImplemented` | `NotSupported` ("recognised but not supported") | same tables |
| Unreadable frame | logs, no answer | CALLERROR with id `"-1"` (or drop, configurable) | 2.x §4.2.3 |
| Enum violation | `FormatViolation` | `PropertyConstraintViolation` | error-code tables |
| Missing required field | `ProtocolError` | `OccurrenceConstraintViolation` (2.x) / `OccurenceConstraintViolation` (1.6) | error-code tables |
| MessageTypeId 5 / 6 | not supported | per version: 2.0.1 → MessageTypeNotSupported, 1.6 → ignored, 2.1 → handled | 2.0.1 §4.4 · 1.6 J §4.1.3 |
| Request before Accepted | answered normally | CALLERROR `SecurityError` | B01.FR.10 · B02.FR.09 · B03.FR.07 (2.x) |

Every incoming frame goes through the gatekeeper first. Only what passes reaches the library:

```mermaid
flowchart TD
    IN([raw text from WebSocket]) --> P{"parse()<br/>valid OCPP-J?"}
    P -- "no, id unreadable" --> U{"policy:<br/>unreadable_frames"}
    U -- answer --> R1["CALLERROR id -1<br/>RpcFrameworkError (2.x)<br/>GenericError (1.6, decision)"]
    U -- drop --> D1([drop + note])
    P -- "no, id readable" --> M["CALLERROR (CALL) /<br/>CALLRESULTERROR (2.1 result)"]
    P -- "unknown MessageTypeId" --> V{"version?"}
    P -- yes --> T{"frame type allowed<br/>in this version?"}
    T -- no --> V
    V -- 2.0.1 --> R2["CALLERROR<br/>MessageTypeNotSupported"]
    V -- "1.6 / 2.1" --> D2([ignore + note])
    T -- "CALLRESULT / CALLERROR" --> F([forward to library])
    T -- "CALLRESULTERROR (2.1)" --> N([note: station rejected our answer])
    T -- "SEND (2.1)" --> SV{"schema valid?"}
    SV -- yes --> PS([process, never answer])
    SV -- no --> D3([drop silently])
    T -- CALL --> DUP{"message id<br/>reused?"}
    DUP -- "yes + policy reject" --> R3["CALLERROR"]
    DUP -- no --> A{"action known<br/>in this version?"}
    A -- no --> R4["CALLERROR NotImplemented"]
    A -- yes --> REG{"BootNotification<br/>or station Accepted?"}
    REG -- no --> TR{"triggered by<br/>the CSMS?"}
    TR -- no --> R5["CALLERROR SecurityError"]
    TR -- yes --> SC
    REG -- yes --> SC{"payload matches<br/>the version's schema?"}
    SC -- no --> R6["CALLERROR<br/>Type / Occurrence / Property /<br/>Format(ion) violation"]
    SC -- yes --> H{"handler<br/>implemented?"}
    H -- no --> R7["CALLERROR NotSupported"]
    H -- yes --> F
```

`gatekeeper.inspect()` is a pure function of *(raw text, version, station record, policy, handled actions)*, so every
branch is unit-tested for each version without a network ([tests/test_gatekeeper.py](../tests/test_gatekeeper.py)).

## 5. Station: one life cycle, two vocabularies

```mermaid
flowchart LR
    BTN["Dashboard button<br/>plug · card · unplug · fault"] --> SIM["Station<br/>(simulator.py)"]
    SIM --> B{"negotiated<br/>version"}
    B -- "2.0.1 / 2.1" --> B2["Behaviour2x<br/>StatusNotification Occupied<br/>TransactionEvent Started / Updated / Ended"]
    B -- "1.6" --> B16["Behaviour16<br/>StatusNotification Preparing / Charging / Finishing<br/>StartTransaction / MeterValues / StopTransaction"]
    B2 & B16 --> SEND{"connected and<br/>Accepted?"}
    SEND -- yes --> CSMS[(CSMS)]
    SEND -- no --> Q["offline queue<br/>(flushed in order after reconnect)"]
```

The life cycle (connect, boot until Accepted, report connectors, heartbeats, back-off, reconnect) is the same for
every version. Only the messages differ, so they live in `behaviour.py`. A 1.6 transaction queued while offline gets its
`transactionId` from the CSMS only when the queued StartTransaction is finally sent; the later MeterValues and
StopTransaction read that id at send time.

## 6. State: two points of view

The dashboard always shows **what the station thinks** next to **what the CSMS thinks**. Most interesting situations
live in the gap between the two (after a network drop the station still believes it's Accepted, while the CSMS shows it
Offline).

```mermaid
classDiagram
    class Station {
        version
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
        auth_token
        finishing
    }
    class TxState {
        id
        server_id
        seq
        token
        energy_wh
    }
    class StationRecord {
        version
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

## 7. Dashboard

```mermaid
sequenceDiagram
    participant B as Browser
    participant D as Dashboard (web)
    participant Bus
    participant C as CSMS / Stations
    B->>D: open ws /ws
    D->>B: hello {history, state, meta (schema catalogue, command names per version)}
    C->>Bus: frame / note
    Bus->>D: event
    D->>B: event (drawn as an arrow or a note)
    C->>Bus: changed()
    Note over Bus: snapshots coalesced, at most every 100 ms
    Bus->>D: state
    D->>B: state (station pictures and cards redrawn)
    B->>D: {"cmd":"station","action":"plug","evse":1}
    D->>C: Station.plug(1)
    B->>D: {"cmd":"schema_compare","a":"1.6","b":"2.0.1",…}
    D->>B: field-by-field comparison + raw schemas
```

| Tab | What it shows |
|---|---|
| 🔴 Live | One picture per station (screen, connectors, cars on the cables, live link to the CSMS), controls, and the sequence diagram. Click an arrow for who sent it to whom, what it means, what it answers, every field and the raw frame |
| 📖 How to read the diagram | An annotated example sequence and the five kinds of arrow |
| 🧩 Compare schemas | Any two of 1.6 / 2.0.1 / 2.1, field by field, with replacements and the raw schemas |
