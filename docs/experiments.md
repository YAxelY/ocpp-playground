# Experiments: predict first, then click

Each experiment starts from a fresh `python -m playground`. Write down what you expect **before** you click, then
compare it with the sequence diagram. Click any arrow to see the raw frame.

## 1. First boot
CS-001 powers on by itself.
- **Predict:** how many BootNotifications will it send? Which frames share a message id?
- **Look for:** `Pending`, then a second boot with `Accepted`, one StatusNotification per connector, then Heartbeats.
- **Then:** *Forget registrations* + *Power off/on*. Why is it Pending again?

## 2. Version negotiation
Power on **CS-002** (offers only 2.0.1) and **CS-003** (offers only 1.6).
- **Predict:** which version does each get? What does the CSMS send to CS-003?
- **Then:** in *CSMS settings*, untick 2.0.1 and power-cycle CS-002. Then untick 2.1 instead and add a "2.1 only" station.

## 3. Nothing before Accepted
Set *BootNotification answer* to **Rejected** and power on a new station.
- **Predict:** what does the station do after each answer? What happens if you press *Send something wrong → Heartbeat now*?
- **Look for:** CALLERROR `SecurityError` with the reference `B03.FR.07`. Switch back to *Pending first* and repeat:
  you get the same error with `B02.FR.09`.
- **Then:** while a station is Pending, use *CSMS → station requests → GetBaseReport*. Why is its NotifyReport allowed?

## 4. A charging session
On CS-001: *Plug in*, *Present card* (AXEL-0001), wait, *Present card* again, *Unplug*.
- **Predict:** the sequence of TransactionEvent `eventType` / `triggerReason` / `seqNo`.
- **Then:** try BLOCKED-01, and switch *Authorize detail* to "Accepted / Invalid only". What changes, and why could that
  matter for security?
- **Then:** *RequestStartTransaction* from the CSMS **before** plugging in. Compare with plugging in first.

## 5. Losing the network mid-session
Start charging, then *Drop* the network. Plug/present cards on another EVSE, wait, then *Restore*.
- **Predict:** what does the station think, and what does the CSMS think, during the outage?
- **Look for:** "reconnect attempt n in x s" (growing back-off), *Queued* TransactionEvents, and after the reconnect:
  no BootNotification, StatusNotifications (all of them or only the changed ones?), then the queued events with
  `offline: true`.
- **Then:** tick *boot on reconnect* and compare. Stay offline longer than `OfflineThreshold` (20 s, see
  *GetBaseReport*) and compare the StatusNotifications after the reconnect.

## 6. Protocol mistakes
Open *Send something wrong* on a **2.0.1** station, then on a **2.1** station. Predict each answer first:

| Button | 2.0.1 | 2.1 |
|---|---|---|
| Unknown action | ? | ? |
| Valid but unsupported | ? | ? |
| Bad enum value / Wrong type / Missing field | ? | ? |
| Malformed JSON | ? | ? |
| Unreadable id | ? | ? |
| Id > 36 chars | ? | ? |
| Same id twice | ? | ? |
| MessageTypeId 7 | ? | ? |
| SEND frame [6] | ? | ? |

(Answers: [conformance.md](conformance.md). The two last rows differ between versions.)

## 7. OCPP 2.1 only
On a 2.1 station: *Open voltage stream*.
- **Look for:** dotted `SEND` arrows with no answer, and the sparkline filling on the CSMS side.
- **Then:** turn on *Fault injection → HeartbeatResponse without currentTime*. Watch a 2.1 station answer with a
  `CALLRESULTERROR` (purple), and a 2.0.1 station that can't. (A charging station sends no Heartbeat because its
  TransactionEvents keep resetting the timer, so try it on an idle one.)

## 8. Schemas
Open the *Schemas 2.0.1 ↔ 2.1* tab, tick *only changed / new*. Which messages used by experiments 1–4 changed in 2.1?
Look at `TransactionEventRequest` and `AuthorizeRequest`.

## 9. Wireshark
Capture on the loopback interface with the filter `tcp.port == 9000`, then follow a WebSocket stream. Find the
handshake with `Sec-WebSocket-Protocol`, and the same JSON frames the dashboard shows.
