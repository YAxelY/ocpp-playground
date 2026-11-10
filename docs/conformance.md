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
| Reconnect with randomised doubling back-off (RetryBackOff* variables) | not specified (decision: same algorithm) | §5.4 | §5.4 | OCA (2.x) |
| No BootNotification on reconnect unless boot data changed | J §5.4 | §5.4 | §5.4 | OCA |

## Registration (1.6 §4.2 · 2.x B01–B04)

| Behaviour | Tag | Reference |
|---|---|---|
| BootNotification after every (re)boot, before anything else | OCA | 1.6 §4.2 · B01.FR.01/03/08 |
| Accepted → heartbeat interval adopted, then every connector's status: 1.6 / 2.0.1 StatusNotification (1.6: plus connectorId 0, optional); 2.1 NotifyEvent `Connector.AvailabilityState` (preferred, default) or the deprecated StatusNotification (option) | OCA | 1.6 §4.2, §3.8 · B01.FR.04/05 · 2.1 B01.FR.05, G01 |
| 2.x: SecurityEventNotification `StartupOfTheDevice` after a power-up, `ResetOrReboot` after a reset, once Accepted | OCA | A04.FR.01 (critical security events, Part 2 appendix) |
| Pending → only BootNotification, not before `interval`; connection stays open; CSMS may read / change configuration | OCA | 1.6 §4.2 · B02.FR.01/02/04/06 |
| Messages the CSMS triggered (TriggerMessage; NotifyReport after GetBaseReport) are allowed while not Accepted | OCA | 1.6 §4.2 · B02.FR.02 |
| 1.6 Pending: the CSMS must not send RemoteStart/StopTransaction (the playground refuses to send them) | OCA | 1.6 §4.2 |
| 2.x Pending: RequestStartTransaction answered Rejected by the station | OCA | B02.FR.05 |
| Rejected → station silent until the interval; CSMS initiates nothing | OCA | 1.6 §4.2 · B03.FR.02/03 |
| 2.x: a non-boot request from a Pending / Rejected station → CALLERROR SecurityError | OCA | B01.FR.10 · B02.FR.09 · B03.FR.07 |
| 1.6: the same SecurityError reply | Decision | 1.6 only says the station SHALL NOT send; the CSMS reaction isn't specified |
| A station that never booted on this connection sends a request → SecurityError | Decision | it breaks B01.FR.08 / 1.6 §4.2; the reaction isn't specified |
| `interval = 0` with a status other than Accepted → the station waits its own time (option, default 10 s) | OCA (value: option) | 1.6 §4.2 · B02.FR.06 · B03.FR.05: "choose your own" |
| The CSMS's answer (dashboard): Accepted, Rejected, Pending forever, or Pending for N seconds then Accepted / Rejected (default: Pending 20 s, then Accepted). Pending → Rejected is allowed: each BootNotificationResponse is the CSMS's decision and B03 describes Rejected as "not (yet) accepted" | Decision (all allowed) | 1.6 §4.2 · B01 / B02 / B03 |
| While Pending the CSMS (options, default all on): reads settings (GetVariables / GetConfiguration), asks for the full report (GetBaseReport → NotifyReport), sets the heartbeat (SetVariables / ChangeConfiguration), and when the Pending time is over asks for a new BootNotification (TriggerMessage) instead of waiting for the retry | OCA (MAY) | B02.FR.01/02/04 · B05–B07 · 1.6 §4.2, §5.17 |

## Availability and liveness

| Behaviour | Tag | Reference |
|---|---|---|
| Any message proves the station alive; Heartbeat after an idle interval; any message resets the timer (option: strict interval) | OCA (MAY / RECOMMENDED) | 1.6 §4.6 · G02.FR.02/04/05 |
| The station sets its clock from `currentTime` (option `ClockCtrlr.TimeSource`; a wrong clock at power-on can be simulated) | OCA (RECOMMENDED) | 1.6 §4.6 · B01.FR.06 · G02.FR.06 |
| "Silent" when nothing arrived for more than 2 × the heartbeat interval | Decision | neither 1.6 nor 2.x defines when the CSMS concludes "offline" |
| 2.x: after an offline period > OfflineThreshold report all connectors, otherwise only the changed ones | OCA | B04.FR.01/02 |
| 1.6: after reconnecting, report connectors whose status changed while offline | OCA | 1.6 §4.9 (rule 1) |
| 2.x statuses: Available / Occupied / Reserved / Unavailable / Faulted (2.1: carried by NotifyEvent AvailabilityState, optionally also for the EVSE and the whole station) | OCA | G01 · 2.1 G01 1a |
| 1.6 statuses: Available / Preparing / Charging / SuspendedEV / SuspendedEVSE / Finishing / Reserved / Unavailable / Faulted, plus a mandatory `errorCode` | OCA | 1.6 §4.9 |
| ChangeAvailability while a transaction runs → Scheduled, applied when it ends | OCA | 1.6 §5.2 · G03 |

## Authorization and transactions

| Behaviour | Tag | Reference |
|---|---|---|
| 2.x: transaction id generated by the station; `seqNo` increments per event; the CSMS flags gaps | OCA (reaction: Decision) | 2.0.1 Part 0 §2.8.2 |
| 1.6: transaction id assigned by the CSMS in StartTransaction.conf | OCA | 1.6 §4.8 |
| 2.x: a transaction starts / ends on the events listed in `TxCtrlr.TxStartPoint` / `TxStopPoint` (EVConnected, Authorized, PowerPathClosed, EnergyTransfer) | OCA | E01 · E.1.1 · §2.6.6 |
| 2.x: cable first, card first, remote start first, remote stop, stop by the same card, car pauses (SuspendedEV), EVConnectionTimeOut | OCA | E02, E03, F02, F03, E07, C01.FR.26, E03.FR.05 |
| RemoteStart / RequestStart behaves as if the card was presented; checked with Authorize first when `AuthorizeRemoteTxRequests` / `AuthCtrlr.AuthorizeRemoteStart` = true | OCA (option) | 1.6 §5.11 · F01/F02 |
| 1.6: `StopTransactionOnEVSideDisconnect` true stops the transaction; false only suspends it (SuspendedEV) | OCA (option) | 1.6 §9.1.23 |
| Invalid id in StartTransaction.conf / TransactionEventResponse → stop (DeAuthorized) or only stop the energy (SuspendedEVSE), per `StopTransactionOnInvalidId` / `TxCtrlr.StopTxOnInvalidId` | OCA (option) | 1.6 §3.4 · E05.FR.02/09/10 |
| Events while offline are queued and sent later in order (2.x with `offline: true`) | OCA | E04, E12 · 1.6 §3.5 |
| Offline: cached Accepted cards (`LocalAuthorizeOffline`) and unknown cards (`AllowOfflineTxForUnknownId` / `OfflineTxForUnknownIdEnabled`) accepted locally; online `LocalPreAuthorize` skips Authorize for cached cards | OCA (options) | 1.6 §3.4, §3.5 · C10, C12, C15 |
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

## Optional behaviour you can switch

Every choice the specifications leave open for the use cases the playground simulates. Each one is in the
dashboard (station card → ⚙️ *Optional behaviour*). When OCPP defines a Device Model variable (2.x) or a
configuration key (1.6) for it, the CSMS can also read and change it (GetVariables / SetVariables,
GetConfiguration / ChangeConfiguration) and it appears in NotifyReport. Generated by
`python -m playground.tools.options_table` from `playground/station/options.py`.

| Option | Versions | 2.0.1 / 2.1 name | 1.6 key | Default | Reference |
|---|---|---|---|---|---|
| How connector status is reported (2.1) | 2.1 | implementation choice | — | `NotifyEvent` | 2.1 B01.FR.05 · G01 (alternative scenario) · G01.FR.03-07 |
| Also report EVSE and ChargingStation AvailabilityState (2.1) | 2.1 | implementation choice | — | `false` | 2.1 G01 scenario 1a |
| Report connectorId 0 (the whole charge point) after boot | 1.6 | — | implementation choice | `true` | 1.6 §4.9 · §3.8 |
| MinimumStatusDuration (s) | 1.6 | — | `MinimumStatusDuration` | `0` | 1.6 §4.9 · §9.1.20 |
| Any message resets the heartbeat timer | 1.6, 2.0.1, 2.1 | implementation choice | implementation choice | `true` | G02.FR.05 (RECOMMENDED) · G02.FR.07 · 1.6 §4.6 (MAY skip) |
| Clock source | 1.6, 2.0.1, 2.1 | `ClockCtrlr.TimeSource` | implementation choice | `Heartbeat` | B01.FR.06 · G02.FR.06 · ClockCtrlr.TimeSource · 1.6 §4.6 (RECOMMENDED to sync) |
| Clock error at power-on (s) | 1.6, 2.0.1, 2.1 | implementation choice | implementation choice | `0` | B01.FR.06 · 1.6 §4.6 |
| Own wait when the boot interval is 0 (s) | 1.6, 2.0.1, 2.1 | implementation choice | implementation choice | `10` | B02.FR.06 · B03.FR.05 · 1.6 §4.2 |
| Report connectors Unavailable before a reset | 2.0.1, 2.1 | implementation choice | — | `false` | B11.FR.03 / B11.FR.08 |
| Authorization cache | 1.6, 2.0.1, 2.1 | `AuthCacheCtrlr.Enabled` | `AuthorizationCacheEnabled` | `true` | C10 · AuthCacheCtrlr.Enabled · 1.6 §3.4 · §9.1.2 |
| Local pre-authorize (online) | 1.6, 2.0.1, 2.1 | `AuthCtrlr.LocalPreAuthorize` | `LocalPreAuthorize` | `false` | C12 · AuthCtrlr.LocalPreAuthorize · 1.6 §3.4 · §9.1.13 |
| Local authorize offline | 1.6, 2.0.1, 2.1 | `AuthCtrlr.LocalAuthorizeOffline` | `LocalAuthorizeOffline` | `true` | C12 · AuthCtrlr.LocalAuthorizeOffline · 1.6 §3.5 · §9.1.12 |
| Offline: accept unknown cards | 1.6, 2.0.1, 2.1 | `AuthCtrlr.OfflineTxForUnknownIdEnabled` | `AllowOfflineTxForUnknownId` | `true` | C15 · AuthCtrlr.OfflineTxForUnknownIdEnabled · 1.6 §3.5 · §9.1.1 |
| Authorize remote starts | 1.6, 2.0.1, 2.1 | `AuthCtrlr.AuthorizeRemoteStart` | `AuthorizeRemoteTxRequests` | `false` | F01/F02 · AuthCtrlr.AuthorizeRemoteStart · 1.6 §5.11 · §9.1.3 |
| Transaction start point | 2.0.1, 2.1 | `TxCtrlr.TxStartPoint` | — | `EVConnected,Authorized` | E01 · Part 2 E.1.1 / §2.6.6.1 |
| Transaction stop point | 2.0.1, 2.1 | `TxCtrlr.TxStopPoint` | — | `EVConnected,Authorized` | E06 · Part 2 §2.6.6.2 |
| Cable timeout after authorization (s) | 1.6, 2.0.1, 2.1 | `TxCtrlr.EVConnectionTimeOut` | `ConnectionTimeOut` | `60` | C01.FR.26 · E03.FR.05 · 1.6 §4.9 (B1) · §9.1.6 |
| Stop on invalid id | 1.6, 2.0.1, 2.1 | `TxCtrlr.StopTxOnInvalidId` | `StopTransactionOnInvalidId` | `true` | E05.FR.02/09/10 · 1.6 §3.4 · §9.1.24 |
| Stop when the cable is unplugged from the car | 1.6, 2.0.1, 2.1 | `TxCtrlr.StopTxOnEVSideDisconnect` | `StopTransactionOnEVSideDisconnect` | `true` | TxCtrlr.StopTxOnEVSideDisconnect · 1.6 §9.1.23 |
| Measurands sent while charging | 1.6, 2.0.1, 2.1 | `SampledDataCtrlr.TxUpdatedMeasurands` | `MeterValuesSampledData` | `Energy.Active.Import.Register` | J02 · SampledDataCtrlr · 1.6 §9.1.17 |
| Measurands at transaction start | 2.0.1, 2.1 | `SampledDataCtrlr.TxStartedMeasurands` | — | `Energy.Active.Import.Register` | E01.FR.09 · J02 |
| Measurands at transaction end | 1.6, 2.0.1, 2.1 | `SampledDataCtrlr.TxEndedMeasurands` | `StopTxnSampledData` | `Energy.Active.Import.Register` | E06 · J02 · 1.6 §9.1.27 |
| Clock-aligned interval (s, 0 = off) | 1.6, 2.0.1, 2.1 | `AlignedDataCtrlr.Interval` | `ClockAlignedDataInterval` | `0` | J01.FR.14 · J02 · 1.6 §4.7 · §9.1.5 |
| Clock-aligned measurands | 1.6, 2.0.1, 2.1 | `AlignedDataCtrlr.Measurands` | `MeterValuesAlignedData` | `Energy.Active.Import.Register` | AlignedDataCtrlr.Measurands · 1.6 §9.1.15 |
| Clock-aligned only when idle | 2.0.1, 2.1 | `AlignedDataCtrlr.SendDuringIdle` | — | `false` | J01.FR.19 · AlignedDataCtrlr.SendDuringIdle |
| Also report a fault as NotifyEvent (EVSE.Problem) | 2.0.1, 2.1 | implementation choice | — | `false` | G01 · N07 (NotifyEvent) |

CSMS side (CSMS card → ⚙️ *Optional CSMS behaviour*):

| Option | Default | Reference |
|---|---|---|
| Registration answer and Pending duration; what the CSMS does while Pending (see *Registration* above) | Pending 20 s → Accepted, all actions | B01–B03 · B02.FR.01 · 1.6 §4.2 |
| After Accepted: ask for a full report | off | B07 · 1.6 §5.8 |
| Cache expiry in card answers (`idTokenInfo.cacheExpiryDateTime` / `idTagInfo.expiryDate`) | not sent | C10 · 1.6 §7.27 |
| Reset type sent: Immediate / OnIdle (2.x), Soft / Hard (1.6); OnIdle during a transaction is answered Scheduled | Immediate / Soft | B11, B12.FR.01 · 1.6 §5.14 |

### Not simulated (yet)

Listed so nobody mistakes the absence for a rule: TxStartPoint `ParkingBayOccupancy` and `DataSigned` (no bay
sensor, no signed meter), Local Authorization List (C13/C14, 1.6 SendLocalList), reservations (H), smart charging
(K), `MaxEnergyOnInvalidId`, `MessageAttempts` / `TransactionMessageAttempts` retries after a CALLERROR (E13),
signed meter values, ISO 15118, firmware / log uploads, security profiles (A00) and 2.1-only domains
(bidirectional, DER, tariffs).
