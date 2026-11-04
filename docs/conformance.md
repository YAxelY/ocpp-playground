# Conformance notes: rule, decision or open question?

The playground tries to be honest about **where each behaviour comes from**. Every row is tagged:

- **OCA**: required or recommended by the OCPP specification (reference given, text paraphrased)
- **Decision**: the spec leaves it open (or allows several options); this is the playground's choice
- **Open**: unclear from the specification; worth asking someone who maintains a real CSMS

References: OCPP 2.0.1 Edition 3 (2024-05-06) and OCPP 2.1 Edition 2 (2025-12-03), Part 2 (use cases, `XNN.FR.NN`)
and Part 4 (OCPP-J, `§`). This is not a certified implementation, and not affiliated with the Open Charge Alliance.

## Transport and RPC framework (Part 4)

| Behaviour | Tag | Reference |
|---|---|---|
| CSMS is the WebSocket server, the station the client, and the connection stays open | OCA | §3, §3.1 |
| Station identity is the last URL path segment | OCA | §3.1.1 |
| Version via `Sec-WebSocket-Protocol`. With no common version, the CSMS completes the handshake without the header, then closes | OCA | §3.1.2, §3.2 |
| The CSMS chooses by **its own** preference order among the offered versions (2.1 > 2.0.1) | Decision | §3.2 doesn't say whose preference wins |
| An unknown station id is accepted (no HTTP 404) | Decision | §3.2 says SHOULD 404. The playground accepts any id so you can add stations freely |
| One open CALL per direction (the library's call lock) | OCA | §4.1.1 |
| A SEND may be sent while a CALL is open | OCA (2.1) | 2.1 §4.1.1 note |
| Unreadable message id → CALLERROR with id `"-1"`, code RpcFrameworkError | OCA | §4.2.3 |
| …or drop it silently (policy `unreadable_frames = drop`) | OCA | §4.2.3 also allows dropping invalid / non-schema messages |
| Message id longer than 36 characters → CALLERROR RpcFrameworkError | Decision | §4.1.4 gives the limit. The error code isn't specified |
| Reused message id → CALLERROR RpcFrameworkError (policy `duplicate_ids`) | Decision | §4.1.4 forbids reuse by the sender; §4.2.3 lists "same id already being handled" as a CALLERROR case without a code |
| Unknown action → `NotImplemented`; known but unhandled → `NotSupported` | OCA | §4.3 table descriptions |
| Schema violations → Type / Occurrence / PropertyConstraintViolation, FormatViolation for extra fields | Decision | §4.3 describes the codes; which JSON-schema keyword maps to which code isn't listed |
| Unknown MessageTypeId on 2.0.1 → CALLERROR MessageTypeNotSupported | OCA | 2.0.1 §4.4 |
| Unknown MessageTypeId on 2.1 → ignored, no answer | OCA | 2.1 §4.4 (changed from 2.0.1) |
| SEND is never answered, even when invalid | OCA | 2.1 §4.2.4, Part 2 generic FR.07 |
| An invalid CALLRESULT on 2.1 → CALLRESULTERROR with the result's id | OCA | 2.1 §4.2.3 |
| Reconnect with randomised, doubling back-off (RetryBackOff* variables) | OCA | §5.3, §8 |
| No BootNotification on reconnect unless boot data changed | OCA | §5.3 |

## Provisioning and availability (Part 2, blocks B and G)

| Behaviour | Tag | Reference |
|---|---|---|
| Station sends BootNotification after every (re)boot, before anything else | OCA | B01.FR.01, FR.03, FR.08 |
| Accepted → heartbeat interval adopted, StatusNotification for every connector | OCA | B01.FR.04, FR.05 |
| Pending → only BootNotification, not earlier than `interval`; connection stays open | OCA | B02.FR.02, FR.04, FR.06 |
| Pending → CSMS may GetVariables / SetVariables / GetBaseReport, and the station answers | OCA | B02.FR.01 |
| Messages the CSMS triggered (TriggerMessage, NotifyReport after GetBaseReport) are allowed while not Accepted | OCA | B02.FR.02 |
| Pending → RequestStartTransaction answered Rejected | OCA | B02.FR.05 |
| Rejected → station silent until the interval; CSMS initiates nothing | OCA | B03.FR.02, FR.03 |
| Non-boot request from a Pending / Rejected station → CALLERROR SecurityError | OCA | B01.FR.10, B02.FR.09, B03.FR.07 |
| Non-boot request from a station that never booted on this CSMS → SecurityError | Decision | the station breaks B01.FR.08; the CSMS reaction isn't specified for that case |
| `interval = 0` with a status other than Accepted → the station picks 10 s | Decision | B02.FR.07 / B03.FR.05 say "choose your own" |
| First boot Pending, then Accepted (default policy) | Decision | who decides registration is a deployment choice |
| Any message proves the station alive; Heartbeat after an idle interval; any message resets the timer | OCA | G02.FR.02, FR.04, FR.05 |
| "Silent" when nothing arrived for more than 2 × heartbeat interval | Decision | G02 says the CSMS concludes "Offline" without defining when |
| After an offline period longer than OfflineThreshold, report all connectors; otherwise only the changed ones | OCA | B04.FR.01, FR.02 |
| Cable in → Occupied; cable out → Available | OCA | G01.FR.03, FR.04 |
| ChangeAvailability while a transaction runs → Scheduled, applied when it ends | OCA | G03 |

## Authorization and transactions (blocks C, E, F)

| Behaviour | Tag | Reference |
|---|---|---|
| Transaction id generated by the station; `seqNo` increments per event | OCA | Part 0 §2.8.2 |
| Cable first, card first, remote start first, remote stop, stop by the same card | OCA | E02, E03, F02, F03, E07 |
| TxStartPoint = TxStopPoint = EVConnected,Authorized (as read-only variables) | Decision | E01/E06 make these configurable |
| Events that happen while offline are queued and sent later with `offline: true` | OCA | E04, E12 |
| Unknown tokens accepted locally while offline | Decision | C15 makes this configurable |
| Authorize may reveal Blocked / Expired, or only Accepted / Invalid (policy) | Decision | security trade-off: detailed statuses reveal that a tag exists |
| CSMS flags `seqNo` gaps | Decision | purpose of seqNo per Part 0 §2.8.2; the reaction is up to the CSMS |

## Things noticed in the specifications

These came up while building the playground. They're observations, not claims that the specification is wrong.

1. **2.1 Part 4 §4.2.4 SEND example vs the schema.** The `NotifyPeriodicEventStream` example in the text has no
   `basetime` field and uses timestamps for `t`. The 2.1 JSON schema requires `basetime` and defines `t` as a **number**
   (offset from basetime). The playground follows the schema.
2. **Unknown MessageTypeId changed between versions.** 2.0.1 §4.4 asks for a `MessageTypeNotSupported` CALLERROR;
   2.1 §4.4 only says to ignore the payload. Both rules are implemented, per negotiated version.
3. **Block membership.** Heartbeat (G02) and StatusNotification (G01) are often listed with "provisioning messages",
   but they belong to the *Availability* block. Only BootNotification is in *Provisioning*, although the B01 sequence
   uses all three.
4. **`idToken.type` is no longer an enum in 2.1.** It's a string (max 20 characters) whose values are listed in an
   appendix (`IdTokenEnumStringType`), so new token types don't require a schema change. See
   [ocpp-2.0.1-vs-2.1.md](ocpp-2.0.1-vs-2.1.md).
5. **The core provisioning messages are structurally identical in 2.0.1 and 2.1**: BootNotification, Heartbeat and
   StatusNotification requests and responses. Most of 2.1's changes are in transactions, tariffs, DER control and the
   new messages.

## Open questions

- When should a CSMS consider a station offline: connection closed only, or also after a heartbeat silence? With what
  margin over the heartbeat interval?
- Should a reused message id be refused, and with which error code?
- Is `PropertyConstraintViolation` or `FormatViolation` the expected code for a bad enum value? Implementations differ
  (the `ocpp` library uses FormatViolation).
- How should a CSMS treat requests from a connected station that never sent a BootNotification on this connection
  *and* isn't known (B01.FR.08 is a station obligation)?
