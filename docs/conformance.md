# Conformance notes: rule, decision or open question?

The playground tries to be honest about **where each behaviour comes from**. Every row is tagged:

- **OCA**: required or recommended by the OCPP specification (reference given, text paraphrased)
- **Decision**: the spec leaves it open, or allows several options; this is the playground's choice
- **Open**: unclear from the specification; worth asking someone who maintains a real CSMS

References: OCPP 1.6 Edition 2 (+ OCPP-J 1.6, cited "1.6 J"), OCPP 2.0.1 Edition 3/4 and OCPP 2.1 Edition 2, with
Part 2 use cases (`B02.FR.09`…) and Part 4 (OCPP-J, `§`). This is not a certified implementation and is not
affiliated with the Open Charge Alliance.

## Transport and RPC framework

| Behaviour | 1.6 | 2.0.1 | 2.1 | Tag |
|---|---|---|---|---|
| CSMS = WebSocket server, station = client, connection kept open | J §3 | §3 | §3 | OCA |
| Station identity = last URL path segment | J §3.1.1 | §3.1.1 | §3.1.1 | OCA |
| Version via `Sec-WebSocket-Protocol`; with no common version the CSMS completes the handshake without the header, then closes | J §3.2 | §3.2 | §3.2 | OCA |
| The CSMS picks by **its own** preference order (2.1 > 2.0.1 > 1.6) | | | | Decision: §3.2 doesn't say whose preference wins |
| An unknown station id is accepted (no HTTP 404) | | | | Decision: J §3.2 / §3.2 say SHOULD 404; the playground accepts any id so you can add stations |
| One open CALL per direction | J §4.1.1 (SHOULD NOT) | §4.1.1 (SHALL NOT) | §4.1.1 | OCA |
| A SEND may be sent while a CALL is open | — | — | §4.1.1 note | OCA |
| Message ids unique… | per connection (J §4.1.4) | per station identity, every connection (§4.1.4) | same | OCA: the CSMS forgets ids on reconnect only for 1.6 |
| Reused message id → CALLERROR (policy `duplicate_ids`) | GenericError | RpcFrameworkError | RpcFrameworkError | Decision: reuse is forbidden, the error code isn't specified |
| Unreadable message id → CALLERROR with id `"-1"` | GenericError (decision: 1.6 defines nothing) | RpcFrameworkError (§4.2.3) | same | OCA for 2.x, Decision for 1.6 |
| …or drop it silently (policy `unreadable_frames = drop`) | | §4.2.3 also allows dropping | same | OCA |
| Unknown action → NotImplemented; known but unhandled → NotSupported | J §4.2.3 table | §4.3 table | §4.3 | OCA |
| Schema violation → Type / Occurrence / Property-constraint violation, Format(ion)Violation for unknown fields | spelled `OccurenceConstraintViolation`, `FormationViolation` | `OccurrenceConstraintViolation`, `FormatViolation` | same | Decision: the keyword → code mapping isn't listed in any spec |
| Unknown MessageTypeId | ignored (J §4.1.3) | CALLERROR MessageTypeNotSupported (§4.4) | ignored (§4.4) | OCA |
| SEND never answered, even when invalid | — | — | §4.2.4, Part 2 generic FR.07 | OCA |
| Invalid CALLRESULT → CALLRESULTERROR with the result's id | — | — | §4.2.3 | OCA |
| Reconnect with randomised doubling back-off (RetryBackOff* variables) | not specified (decision: same algorithm) | §5.3 | §5.3 | OCA (2.x) |
| No BootNotification on reconnect unless boot data changed | J §5.4 | §5.3 | §5.3 | OCA |

## Registration (1.6 §4.2 · 2.x B01–B04)

| Behaviour | Tag | Reference |
|---|---|---|
| BootNotification after every (re)boot, before anything else | OCA | 1.6 §4.2 · B01.FR.01/03/08 |
| Accepted → heartbeat interval adopted, StatusNotification for every connector (1.6: plus connectorId 0) | OCA | 1.6 §4.2, §3.8 · B01.FR.04/05 |
| Pending → only BootNotification, not before `interval`; connection stays open; CSMS may read / change configuration | OCA | 1.6 §4.2 · B02.FR.01/02/04/06 |
| Messages the CSMS triggered (TriggerMessage; NotifyReport after GetBaseReport) are allowed while not Accepted | OCA | 1.6 §4.2 · B02.FR.02 |
| 1.6 Pending: the CSMS must not send RemoteStart/StopTransaction (the playground refuses to send them) | OCA | 1.6 §4.2 |
| 2.x Pending: RequestStartTransaction answered Rejected by the station | OCA | B02.FR.05 |
| Rejected → station silent until the interval; CSMS initiates nothing | OCA | 1.6 §4.2 · B03.FR.02/03 |
| 2.x: a non-boot request from a Pending / Rejected station → CALLERROR SecurityError | OCA | B01.FR.10 · B02.FR.09 · B03.FR.07 |
| 1.6: the same SecurityError reply | Decision | 1.6 only says the station SHALL NOT send; the CSMS reaction isn't specified |
| A station that never booted on this connection sends a request → SecurityError | Decision | it breaks B01.FR.08 / 1.6 §4.2; the reaction isn't specified |
| `interval = 0` with a status other than Accepted → the station waits 10 s | Decision | 1.6 §4.2 · B02.FR.07 · B03.FR.05: "choose your own" |
| First boot Pending, then Accepted (default policy) | Decision | who decides registration is a deployment choice |

## Availability and liveness

| Behaviour | Tag | Reference |
|---|---|---|
| Any message proves the station alive; Heartbeat after an idle interval; any message resets the timer | OCA | 1.6 §4.6 · G02.FR.02/04/05 |
| "Silent" when nothing arrived for more than 2 × the heartbeat interval | Decision | neither 1.6 nor 2.x defines when the CSMS concludes "offline" |
| 2.x: after an offline period > OfflineThreshold report all connectors, otherwise only the changed ones | OCA | B04.FR.01/02 |
| 1.6: after reconnecting, report connectors whose status changed while offline | OCA | 1.6 §4.9 (rule 1) |
| 2.x statuses: Available / Occupied / Reserved / Unavailable / Faulted | OCA | G01 |
| 1.6 statuses: Available / Preparing / Charging / SuspendedEV / SuspendedEVSE / Finishing / Reserved / Unavailable / Faulted, plus a mandatory `errorCode` | OCA | 1.6 §4.9 |
| ChangeAvailability while a transaction runs → Scheduled, applied when it ends | OCA | 1.6 §5.2 · G03 |

## Authorization and transactions

| Behaviour | Tag | Reference |
|---|---|---|
| 2.x: transaction id generated by the station; `seqNo` increments per event; the CSMS flags gaps | OCA (reaction: Decision) | 2.0.1 Part 0 §2.8.2 |
| 1.6: transaction id assigned by the CSMS in StartTransaction.conf | OCA | 1.6 §4.8 |
| 2.x: cable first, card first, remote start first, remote stop, stop by the same card | OCA | E02, E03, F02, F03, E07 |
| 1.6: RemoteStartTransaction behaves as if the card was presented (AuthorizeRemoteTxRequests = false) | OCA | 1.6 §5.11 |
| 1.6: StopTransactionOnEVSideDisconnect = true (unplugging stops the session) | Decision | 1.6 §4.10 makes it configurable |
| 1.6: StartTransaction answered with an invalid idTag → the station stops the transaction (DeAuthorized) | Decision | 1.6 has a configuration key for this behaviour |
| Events while offline are queued and sent later in order (2.x with `offline: true`) | OCA | E04, E12 · 1.6 §3.5 |
| Unknown cards accepted locally while offline | Decision | C15 / 1.6 §3.5 make this configurable |
| Authorize may reveal Blocked / Expired, or only Accepted / Invalid (policy) | Decision | security trade-off |

## Things noticed in the specifications

These came up while building the playground. They're observations, not claims that a specification is wrong.

1. **OCPP-J 1.6 §4.2.2 example vs the 1.6 schema.** The example BootNotification answer uses `heartbeatInterval`; the
   schema and the core spec (1.6 §6.4) call the field `interval`.
2. **OCPP-J 1.6 spells two error codes unusually**: `FormationViolation` and `OccurenceConstraintViolation`. 2.0.1
   renamed them `FormatViolation` and `OccurrenceConstraintViolation`. The playground uses each version's spelling.
3. **2.1 Part 4 §4.2.4 SEND example vs the 2.1 schema.** The `NotifyPeriodicEventStream` example has no `basetime` and
   uses timestamps for `t`; the schema requires `basetime` and defines `t` as a number (offset). The playground follows
   the schema.
4. **Unknown MessageTypeId: ignore (1.6) → answer MessageTypeNotSupported (2.0.1) → ignore again (2.1).**
5. **Message-id uniqueness widened** from "per connection" (1.6) to "per station identity, across connections" (2.0.1).
6. **Block membership.** Heartbeat (G02) and StatusNotification (G01) are often listed with the provisioning messages but
   belong to the *Availability* block; only BootNotification is in *Provisioning*.
7. **2.0.1 → 2.1 is not structurally identical even for the core messages.** The requests of BootNotification,
   Heartbeat and StatusNotification are unchanged, but `BootNotificationResponse.statusInfo.additionalInfo` grew from 512
   to 1024 characters, and 83 schemas changed in total (mostly lengths and new enum values). `idToken.type` stopped being
   an enum: it's a string whose values are listed in an appendix. See [ocpp-versions-compared.md](ocpp-versions-compared.md).
8. **The `ocpp` library's schemas differ slightly from the official files** (2 real differences, 11 extra 1.6
   security-extension messages): see [schema-provenance.md](schema-provenance.md).

## Open questions

- When should a CSMS consider a station offline: connection closed only, or also after heartbeat silence, and with
  what margin?
- Which error code for a reused message id, an id longer than 36 characters, and (in 1.6) an unreadable id?
- `PropertyConstraintViolation` or `Format(ion)Violation` for a bad enum value? Implementations differ (the `ocpp`
  library uses FormatViolation).
- How should a CSMS treat requests from a connected station that never sent a BootNotification on this connection?
