# Experiments: predict first, then click

Start `python -m playground` and open http://localhost:8000. Before you click, write down what you expect, then compare it
with the sequence diagram. **Click any arrow** to see who sent it to whom, what it means and what it answers.
New to sequence diagrams? Open the **📖 How to read the diagram** tab first.

## 1. First boot (any version)
CS-001 powers on by itself.
- **Predict:** how many BootNotifications? Which arrows share a message id?
- **Look for:** ⏳ Pending, a second boot ✔ Accepted, one StatusNotification per connector, then Heartbeats.
- **Then:** *Forget registrations* in the CSMS panel, power CS-001 off and on. Why is it Pending again?

## 2. Three versions, one CSMS
Power on **CS-002** (speaks only 2.0.1) and **CS-003** (speaks only 1.6).
- **Predict:** which version does each get? What is different in their BootNotification payloads?
- **Look for:** the version chip on every arrow; CS-003 also reports **connector 0** (the charge point itself).
- **Then:** untick 1.6 in the CSMS panel and power-cycle CS-003: refused at the handshake. Add a "2.0.1 + 1.6"
  station and untick 2.0.1: it falls back to 1.6.

## 3. Nothing before Accepted
Set *BootNotification answer* to **Rejected** and power on a station.
- **Predict:** what does it do after each answer? What if you press *Send something wrong → Heartbeat now*?
- **Look for:** CALLERROR `SecurityError` with the reference `B03.FR.07` (2.x). Do it on CS-003: same reply, but the
  note says it's a playground decision. 1.6 only forbids the station from sending, it doesn't say how the CSMS reacts.
- **Then:** while Pending, use *Requests the CSMS can send → full report*. Why is the station allowed to answer? On 1.6,
  try *RemoteStartTransaction* while Pending.

## 4. A charging session in 2.x vs 1.6
Same steps on CS-001 (2.1) and CS-003 (1.6): *Plug in car*, *Present card* (AXEL-0001), wait, *Present card* again,
*Unplug*. Watch the pictures: the car, the ⚡, the connector colour.
- **Predict:** 2.x sends TransactionEvent Started → Updated → Ended. What does 1.6 send? Who creates the transaction id?
- **Look for (1.6):** Preparing → StartTransaction → **CSMS answers transactionId 1001** → Charging → MeterValues →
  StopTransaction → Finishing → Available.
- **Then:** present the card *before* plugging in. Try BLOCKED-01, and set *Card check detail* to "only Accepted /
  Invalid". Why might hiding "Blocked" matter for security?

## 5. Losing the network mid-session
Start charging, then *✂ Cut network*. Plug in / present cards on another connector, wait, then *📶 Restore*.
- **Predict:** what does the station think during the outage, and what does the CSMS think? (Open *What each side
  believes*.)
- **Look for:** "reconnect attempt n in x s" (growing back-off), queued messages on the station's screen, then after the
  reconnect: no BootNotification, StatusNotifications, and the queued messages in order. On 1.6, the
  queued StartTransaction gets its id only now.

## 6. Protocol mistakes, three ways
Open *🧨 Send something wrong* on CS-002 (2.0.1), CS-001 (2.1) and CS-003 (1.6). Predict each answer first:

| Button | 1.6 | 2.0.1 | 2.1 |
|---|---|---|---|
| Unknown action | ? | ? | ? |
| Missing field | ? | ? | ? |
| Bad enum value | ? | ? | ? |
| Malformed JSON / Unreadable id | ? | ? | ? |
| MessageTypeId 7 | ? | ? | ? |
| SEND frame [6] | ? | ? | ? |

(Answers: [conformance.md](conformance.md). Watch the spelling of 1.6's error codes.)

## 7. OCPP 2.1 only
On CS-001: *📈 Open voltage stream*.
- **Look for:** dotted 📣 SEND arrows that are never answered, and the line chart filling on the CSMS side.
- **Then:** turn on *Fault injection*. A 2.1 station answers the broken Heartbeat answer with a ⛔ CALLRESULTERROR; a
  2.0.1 or 1.6 station can't. (A charging station sends no Heartbeat, because its other messages keep resetting the
  timer, so try an idle one.)

## 8. Compare schemas
Open **🧩 Compare schemas**, pick 1.6 ↔ 2.0.1.
- Look at BootNotification: which fields moved into `chargingStation`, which new field is required?
- Look at StartTransaction: what replaced it? Untick "only differences" to see every field.
- Switch to 2.0.1 ↔ 2.1: what changed in TransactionEvent and in the BootNotification *response*?

## 9. Wireshark
Capture on the loopback interface with `tcp.port == 9000`, follow a WebSocket stream, and find the handshake
(`Sec-WebSocket-Protocol`) and the same JSON frames the dashboard shows.
