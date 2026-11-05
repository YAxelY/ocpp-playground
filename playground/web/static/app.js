"use strict";
// OCPP Playground dashboard. Talks to the Python side over ws://<host>/ws (see playground/web/server.py).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let ws, meta = {}, state = { stations: [], policy: {} }, events = [], filter = "all", selected = null;
const ui = { open: new Set(), values: {} };

// ======================================================================= connection
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => ($("#conn").textContent = "● live");
  ws.onclose = () => { $("#conn").textContent = "disconnected, retrying…"; setTimeout(connect, 1500); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "hello") { meta = m.meta; events = m.history; setState(m); renderLog(); initSchemas(); openFromHash(); }
    else if (m.type === "state") setState(m);
    else if (m.type === "schema_compare") renderCompare(m);
    else {
      events.push(m);
      if (events.length > 2000) { events.splice(0, 500); closeExplain(); renderLog(); }   // indices change: redraw
      else appendEvent(m);
    }
  };
}
const send = (o) => ws && ws.readyState === 1 && ws.send(JSON.stringify(o));
const act = (id, action, extra = {}) => send({ cmd: "station", id, action, ...extra });
const csms = (id, command, args = {}) => send({ cmd: "csms", id, command, args });
const val = (key, fallback) => ui.values[key] ?? fallback;

// ======================================================================= helpers
const badge = (cls, text) => `<span class="badge ${cls}">${esc(text)}</span>`;
const vclass = (v) => (v === "2.1" ? "v21" : v === "1.6" ? "v16" : "v201");
const verBadge = (v) => (v ? badge(`ver ${vclass(v)}`, `OCPP ${v}`) : badge("idle", "no version yet"));
function regBadge(r) {
  return ({ Accepted: badge("ok", "✔ Accepted"), Pending: badge("warn", "⏳ Pending"), Rejected: badge("bad", "✖ Rejected") })[r]
    || badge("idle", r === "—" || !r ? "— not registered" : "? " + r);
}
const STATUS_PIC = { Available: "🟢", Preparing: "🟠", Occupied: "🟠", Charging: "⚡", SuspendedEV: "⏸", SuspendedEVSE: "⏸",
                     Finishing: "🔵", Reserved: "🔖", Unavailable: "⛔", Faulted: "⚠️" };
function connBadge(s) {
  const base = String(s).split(" ")[0];
  const cls = { Available: "ok", Faulted: "bad", Unavailable: "idle" }[base] || "warn";
  return badge(cls, `${STATUS_PIC[base] || ""} ${s}`);
}
const silentAfter = () => 2 * (state.policy.heartbeat_interval || 10);
function connectivity(c) {
  if (!c) return badge("idle", "never connected");
  if (!c.online) return badge("bad", "✖ Offline");
  const ago = c.last_seen_ms ? Math.round((Date.now() - c.last_seen_ms) / 1000) : null;
  if (ago !== null && ago > silentAfter()) return badge("warn", `⚠ Silent ${ago} s`);
  return badge("ok", "▲ Online") + ` <span class="sub ago" data-ls="${c.last_seen_ms}">${ago ?? "?"} s ago</span>`;
}
const isSilent = (c) => c && c.online && c.last_seen_ms && (Date.now() - c.last_seen_ms) / 1000 > silentAfter();

function captureUi(root) {
  $$("details[data-key]", root).forEach((d) => (d.open ? ui.open.add(d.dataset.key) : ui.open.delete(d.dataset.key)));
  $$("[data-val]", root).forEach((el) => (ui.values[el.dataset.val] = el.type === "checkbox" ? el.checked : el.value));
}
function restoreUi(root) {
  $$("details[data-key]", root).forEach((d) => (d.open = ui.open.has(d.dataset.key)));
  $$("[data-val]", root).forEach((el) => {
    if (el.dataset.val in ui.values) el.type === "checkbox" ? (el.checked = ui.values[el.dataset.val]) : (el.value = ui.values[el.dataset.val]);
  });
}
document.addEventListener("change", (e) => e.target.dataset?.val &&
  (ui.values[e.target.dataset.val] = e.target.type === "checkbox" ? e.target.checked : e.target.value));
document.addEventListener("toggle", (e) => e.target.dataset?.key &&
  (e.target.open ? ui.open.add(e.target.dataset.key) : ui.open.delete(e.target.dataset.key)), true);

function setState(m) {
  state = { stations: m.stations, policy: m.policy };
  const typing = document.activeElement && ["INPUT", "SELECT"].includes(document.activeElement.tagName)
    && document.activeElement.closest("#stations, #policy");
  if (!typing) { renderPolicy(); renderStations(); }
  renderFilters();
}

// ======================================================================= CSMS settings
function renderPolicy() {
  const p = state.policy, box = $("#policy");
  captureUi(box);
  const sel = (key, opts) => `<select data-pol="${key}">${opts.map(([v, t]) => `<option value="${v}" ${p[key] === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
  const online = state.stations.filter((s) => s.csms?.online).length;
  box.innerHTML = `
    <h2>🖥️ CSMS <span class="sub">${online} station(s) connected · ws://${esc(location.hostname)}:9000/ocpp/&lt;id&gt;</span></h2>
    <div class="row"><span class="lbl">speaks</span>
      ${["2.1", "2.0.1", "1.6"].map((v) => `<label><input type="checkbox" data-ver="${v}" ${p.versions.includes(v) ? "checked" : ""}> ${badge(`ver ${vclass(v)}`, v)}</label>`).join(" ")}
      <span class="sub">preference ${esc(p.versions.join(" > ")) || "none: every station is refused"}</span></div>
    <details data-key="policy"><summary>CSMS behaviour (playground choices, not OCPP rules)</summary>
      <div class="row"><span class="lbl">BootNotification answer</span>${sel("registration_mode", [["pending_then_accept", "⏳ Pending first, then ✔ Accepted"], ["accept", "✔ Accepted"], ["reject", "✖ Rejected"]])}</div>
      <div class="row"><span class="lbl">Intervals (s)</span> heartbeat <input type="number" min="1" data-pol="heartbeat_interval" value="${p.heartbeat_interval}">
        retry <input type="number" min="0" data-pol="retry_interval" value="${p.retry_interval}"></div>
      <div class="row"><span class="lbl">Unreadable frame</span>${sel("unreadable_frames", [["answer", "answer with id \"-1\""], ["drop", "drop silently"]])}</div>
      <div class="row"><span class="lbl">Reused message id</span>${sel("duplicate_ids", [["reject", "refuse it"], ["allow", "answer normally"]])}</div>
      <div class="row"><span class="lbl">Card check detail</span>${sel("authorize_detail", [["full", "full status (Blocked, Expired…)"], ["accepted_or_invalid", "only Accepted / Invalid"]])}</div>
      <div class="row"><span class="lbl">Fault injection</span><label><input type="checkbox" data-pol="fault_bad_heartbeat_response" ${p.fault_bad_heartbeat_response ? "checked" : ""}>
        broken Heartbeat answer (no currentTime)</label></div>
      <div class="row"><span class="lbl">Cards known</span><span class="mono">${Object.entries(p.tokens).map(([k, v]) => `💳 ${esc(k)}=${esc(v)}`).join(" · ")}</span></div>
      <div class="row"><button id="forget">Forget registrations</button></div>
    </details>`;
  restoreUi(box);
  $$("[data-pol]", box).forEach((el) => (el.onchange = () =>
    send({ cmd: "policy", [el.dataset.pol]: el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value })));
  $$("[data-ver]", box).forEach((el) => (el.onchange = () =>
    send({ cmd: "policy", versions: ["2.1", "2.0.1", "1.6"].filter((v) => $(`[data-ver="${v}"]`, box).checked) })));
  $("#forget", box).onclick = () => send({ cmd: "forget" });
}

// ======================================================================= station picture (SVG)
function scene(s, c) {
  const evs = Object.entries(s.evses), n = evs.length;
  const reg = s.registration === "—" ? "not registered" : s.registration;
  const linkCls = s.link ? "link-up" : !s.power ? "" : !s.network_up ? "link-down" : "link-wait";
  const linkLabel = s.link ? `ocpp${s.version} · ${reg}` : !s.power ? "switched off" : !s.network_up ? "✂ network down" : "reconnecting…";
  const csmsReg = c ? c.registration : "unknown";
  let socks = "", cars = "";
  evs.forEach(([no, e], i) => {
    const cx = 18 + (150 * (i + 1)) / (n + 1), cy = 124;
    socks += `<circle class="sock st-${esc(e.status)}" cx="${cx}" cy="${cy}" r="12"><title>connector ${no}: ${esc(e.status)}</title></circle>
      <text x="${cx}" y="${cy + 4}" text-anchor="middle" fill="#fff" font-size="11" font-weight="700">${no}</text>`;
    if (e.cable) {
      const carX = 200 + i * 78;
      cars += `<path class="cable" d="M${cx},${cy + 10} C${cx},178 ${carX - 30},178 ${carX - 2},162"/>
        <text x="${carX}" y="172" font-size="34">🚗</text>
        ${e.tx && e.tx.charging ? `<text class="bolt" x="${carX + 38}" y="150" font-size="20">⚡</text>` : ""}
        ${e.tx ? `<text class="lab" x="${carX + 2}" y="190">${(e.tx.energy_wh / 1000).toFixed(2)} kWh</text>` : ""}`;
    } else if (e.auth_token) {
      cars += `<text x="${200 + i * 78}" y="168" font-size="22">💳</text>`;
    }
  });
  return `<svg class="scene" viewBox="0 0 560 196" role="img" aria-label="${esc(s.id)}: ${esc(linkLabel)}">
    <g opacity="${s.power ? 1 : 0.45}">
      <rect class="cab" x="18" y="10" width="150" height="150" rx="14"/>
      <rect class="screen" x="30" y="22" width="126" height="54" rx="6"/>
      <text class="screen-ink" x="38" y="38">${esc(s.power ? reg : "off")}</text>
      <text class="screen-ink" x="38" y="53">${esc(s.link ? "OCPP " + s.version : s.power ? "offline" : "")}</text>
      <text class="screen-ink" x="38" y="68">${s.queued.length ? esc(s.queued.length + " msg queued") : ""}</text>
      <text class="lab" x="93" y="96" text-anchor="middle">${esc(s.offer.map((o) => o.replace("ocpp", "")).join(" · "))}</text>
      ${socks}
    </g>
    ${cars}
    ${linkCls ? `<line class="${linkCls}" x1="170" y1="48" x2="420" y2="48"/>` : ""}
    <text class="lab" x="295" y="38" text-anchor="middle">${esc(linkLabel)}</text>
    ${s.link ? `<text class="lab" x="295" y="64" text-anchor="middle">WebSocket ${esc(s.id)}</text>` : ""}
    <rect class="server" x="420" y="14" width="124" height="74" rx="10"/>
    <line x1="432" y1="36" x2="532" y2="36" stroke="currentColor" opacity=".25"/><line x1="432" y1="52" x2="532" y2="52" stroke="currentColor" opacity=".25"/>
    <text class="big" x="482" y="30" text-anchor="middle">🖥️ CSMS</text>
    <text class="lab" x="482" y="70" text-anchor="middle">sees: ${esc(csmsReg)}</text>
    <text class="lab" x="482" y="84" text-anchor="middle">${esc(c ? (c.online ? "online" : "offline") : "never seen")}</text>
  </svg>`;
}

// ======================================================================= station cards
const TOKENS = ["AXEL-0001", "FLEET-0042", "BLOCKED-01", "EXPIRED-01", "UNKNOWN-99"];
const EDGES = [
  ["heartbeat_now", "Heartbeat now", "a Heartbeat whatever the registration (try it while Pending)"],
  ["unknown_action", "Unknown action", "CALL MakeCoffee"], ["unsupported_action", "Valid but unsupported", "a real OCPP action this CSMS doesn't implement"],
  ["invalid_payload", "Bad enum value", "status \"Exploded\""], ["wrong_type", "Wrong type", "connectorId as text"],
  ["missing_field", "Missing field", "BootNotification without model"], ["malformed", "Malformed JSON", "frame cut off"],
  ["unreadable_id", "Unreadable id", "MessageId is a number"], ["long_id", "Id > 36 chars", ""],
  ["duplicate_id", "Same id twice", ""], ["unknown_type", "MessageTypeId 7", "a frame type nobody knows"],
  ["send_frame", "SEND frame [6]", "exists only in 2.1"],
];
const vname = (s) => s.version || (s.family === "1.6" ? "1.6" : "2.0.1");
const cmdLabel = (s, c) => meta.commands?.[vname(s)]?.[c] || c;

function connectorHtml(id, s, no, e) {
  const k = `${id}-e${no}`, tx = e.tx;
  const txLine = tx ? `<div class="tx">tx ${esc(tx.id)}${tx.seq >= 0 ? " · seq " + tx.seq : ""} · ${(tx.energy_wh / 1000).toFixed(3)} kWh · ${tx.charging ? "⚡ charging" : "waiting"}${tx.token ? " · 💳 " + esc(tx.token) : ""}</div>` : "";
  return `<div class="conn"><div class="row" style="margin-top:0"><b>${s.family === "1.6" ? "Connector" : "EVSE"} ${no}</b> ${connBadge(e.status)}
      ${!e.operative ? badge("idle", "inoperative") : ""}</div>${txLine}
    <div class="row">
      <button ${e.cable || e.fault || !e.operative ? "disabled" : ""} onclick="act('${id}','plug',{evse:${no}})">🔌 Plug in car</button>
      <button ${!e.cable ? "disabled" : ""} onclick="act('${id}','unplug',{evse:${no}})">⏏ Unplug</button>
      <select data-val="${k}-tok" aria-label="card">${TOKENS.map((t) => `<option>${t}</option>`).join("")}</select>
      <button onclick="act('${id}','token',{evse:${no},token:val('${k}-tok','AXEL-0001')})">💳 Present card</button>
      ${e.fault ? `<button onclick="act('${id}','clear_fault',{evse:${no}})">🔧 Repair</button>`
                : `<button onclick="act('${id}','fault',{evse:${no}})">⚠️ Fault</button>`}
    </div></div>`;
}

function sparkline(values) {
  if (!values || values.length < 2) return `<span class="sub">waiting for SEND frames…</span>`;
  const nums = values.map((v) => +v.v), min = Math.min(...nums) - 0.5, max = Math.max(...nums) + 0.5;
  const pts = nums.map((v, i) => `${(i / (nums.length - 1)) * 300},${34 - ((v - min) / (max - min)) * 30 - 2}`).join(" ");
  return `<svg class="spark" viewBox="0 0 300 34" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts}"/></svg>
    <span class="sub">${nums.length} values received by the CSMS · last ${esc(values[values.length - 1].v)} V</span>`;
}

function stationHtml({ id, station: s, csms: c }) {
  const live = s.link, accepted = live && s.registration === "Accepted", v = vname(s);
  const anyTx = Object.values(s.evses).find((e) => e.tx);
  const txs = (c?.transactions || []).map((t) => `<div class="tx">${esc(t.id)} · ${t.state} · ${(t.energy_wh / 1000).toFixed(3)} kWh${t.last_seq >= 0 ? " · seq " + t.last_seq : ""}${t.offline_events ? ` · ${t.offline_events} offline` : ""}${t.stopped_reason ? " · " + t.stopped_reason : ""}</div>`).join("") || `<span class="sub">none yet</span>`;
  const streams = Object.entries(c?.streams || {}).map(([sid, st]) => `<div>📈 stream ${sid}: ${sparkline(st.values)}</div>`).join("");
  return `<div class="card">
    <h2>${esc(id)} ${verBadge(s.version)} ${badge(s.power ? "ok" : "idle", s.power ? "⏻ on" : "⏻ off")}
      <span class="sub">${esc(s.activity)}</span></h2>
    ${scene(s, c)}
    <div class="row"><button ${s.power ? "disabled" : ""} onclick="act('${id}','power_on')">⏻ Power on</button>
      <button ${!s.power ? "disabled" : ""} onclick="act('${id}','power_off')">Power off</button>
      <button ${!s.network_up ? "disabled" : ""} onclick="act('${id}','drop_network')">✂ Cut network</button>
      <button ${s.network_up ? "disabled" : ""} onclick="act('${id}','restore_network')">📶 Restore</button>
      <label class="sub"><input type="checkbox" ${s.boot_on_reconnect ? "checked" : ""} onchange="act('${id}','boot_on_reconnect',{value:this.checked})"> boot again on reconnect</label></div>
    ${Object.entries(s.evses).map(([no, e]) => connectorHtml(id, s, no, e)).join("")}
    ${v === "2.1" ? `<div class="row"><span class="lbl">2.1 stream</span>
      <button ${!accepted ? "disabled" : ""} onclick="act('${id}','open_stream')">📈 Open voltage stream</button>
      <button ${!s.streams.length ? "disabled" : ""} onclick="act('${id}','close_stream')">Close</button></div>${streams}` : ""}
    <details data-key="${id}-views"><summary>👁️ What each side believes</summary><div class="views">
      <div class="view"><h3>🔌 Station thinks</h3><div class="kv">
        <span>Registration</span><span>${regBadge(s.registration)}</span>
        <span>Link</span><span>${live ? badge("ok", "▲ connected") : badge("idle", "✖ not connected")}</span>
        <span>Heartbeat</span><span>every ${s.heartbeat} s</span>
        <span>Queued</span><span>${s.queued.length ? esc(s.queued.join(", ")) : "nothing"}</span></div></div>
      <div class="view"><h3>🖥️ CSMS thinks</h3><div class="kv">
        <span>Registration</span><span>${c ? regBadge(c.registration) : badge("idle", "unknown")}</span>
        <span>Connectivity</span><span>${connectivity(c)}</span>
        <span>Connectors</span><span>${c && Object.keys(c.connectors).length ? Object.entries(c.connectors).map(([k, st]) => `${esc(k)} ${connBadge(st)}`).join(" ") : "—"}</span>
        ${c ? `<span>Boots</span><span>${c.boots}</span>` : ""}</div></div></div></details>
    <details data-key="${id}-tx"><summary>🧾 Transactions recorded by the CSMS</summary>${txs}</details>
    <details data-key="${id}-cmd"><summary>🖥️→🔌 Requests the CSMS can send (OCPP ${esc(v)} names)</summary>
      <div class="row"><button ${!live ? "disabled" : ""} onclick="csms('${id}','read_heartbeat')">${cmdLabel(s, "read_heartbeat")} (heartbeat)</button>
        <input type="number" min="1" data-val="${id}-hb" value="20" aria-label="new heartbeat interval">
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','set_heartbeat',{value:val('${id}-hb','20')})">${cmdLabel(s, "set_heartbeat")}</button></div>
      <div class="row"><button ${!live ? "disabled" : ""} onclick="csms('${id}','full_report')">${cmdLabel(s, "full_report")} (everything)</button>
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','reset')">${cmdLabel(s, "reset")}</button>
        <select data-val="${id}-trig"><option>StatusNotification</option><option>Heartbeat</option><option>BootNotification</option><option>MeterValues</option></select>
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','trigger',{message:val('${id}-trig','StatusNotification')})">${cmdLabel(s, "trigger")}</button></div>
      <div class="row"><select data-val="${id}-av"><option>Inoperative</option><option>Operative</option></select>
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','change_availability',{operational_status:val('${id}-av','Inoperative'),connector:0})">${cmdLabel(s, "change_availability")} (all)</button>
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','unlock',{connector:1})">${cmdLabel(s, "unlock")} 1</button></div>
      <div class="row"><button ${!live ? "disabled" : ""} onclick="csms('${id}','request_start',{connector:1,token:'FLEET-0042'})">${cmdLabel(s, "request_start")} (1, FLEET-0042)</button>
        <button ${!live || !anyTx ? "disabled" : ""} onclick="csms('${id}','request_stop',{transaction_id:'${anyTx?.tx.id || ""}'})">${cmdLabel(s, "request_stop")}</button></div>
      ${c && Object.keys(c.variables).length ? `<h3>What the CSMS learned about the station</h3><div class="mono">${Object.entries(c.variables).map(([kk, vv]) => `${esc(kk)} = ${esc(vv)}`).join("<br>")}</div>` : ""}
    </details>
    <details data-key="${id}-edge"><summary>🧨 Send something wrong (protocol edge cases)</summary>
      <div class="row">${EDGES.map(([n, label, title]) => `<button ${!live ? "disabled" : ""} title="${esc(title)}" onclick="act('${id}','edge',{name:'${n}'})">${esc(label)}</button>`).join("")}</div>
    </details>
  </div>`;
}

function renderStations() {
  const box = $("#stations");
  captureUi(box);
  box.innerHTML = state.stations.map(stationHtml).join("");
  restoreUi(box);
}
let silentKey = "";
setInterval(() => {
  $$(".ago").forEach((el) => (el.textContent = `${Math.round((Date.now() - +el.dataset.ls) / 1000)} s ago`));
  const key = state.stations.map((s) => (isSilent(s.csms) ? 1 : 0)).join("");
  if (key !== silentKey) { silentKey = key; renderStations(); }
}, 1000);

// ======================================================================= sequence diagram
const KIND = {
  CALL: ["📨", "asks"], CALLRESULT: ["✅", "answers"], CALLERROR: ["❌", "refuses"],
  CALLRESULTERROR: ["⛔", "rejects the answer"], SEND: ["📣", "tells (no answer)"], INVALID: ["⚠️", "sends something invalid"],
};
const kindOf = (e) => (e.kind.startsWith("TYPE") ? "INVALID" : e.kind);

function renderFilters() {
  const ids = ["all", ...state.stations.map((s) => s.id), "CSMS"];
  $("#filters").innerHTML = ids.map((id) => `<button class="chip ${filter === id ? "on" : ""}" onclick="filter='${id}';renderFilters();renderLog()">${id === "all" ? "All" : esc(id)}</button>`).join(" ");
}
function shown(e) {
  if (filter !== "all" && e.station !== filter) return false;
  if ($("#hideNoise").checked && e.type === "frame") {
    if (e.action === "Heartbeat" || e.action === "MeterValues") return false;
    if (e.action === "TransactionEvent" && (e.kind === "CALLRESULT" || e.payload?.triggerReason === "MeterValuePeriodic")) return false;
  }
  return true;
}
const hhmmss = (t) => new Date(t).toTimeString().slice(0, 8);
function summary(e) {
  if (e.problem) return e.problem + " · " + e.raw;
  const s = JSON.stringify(e.payload ?? {});
  return s.length > 150 ? s.slice(0, 147) + "…" : s;
}
function eventHtml(e, i) {
  if (e.type === "note")
    return `<div class="note ${e.level}"><b>${esc(e.station)}</b> · ${esc(e.text)}${e.ref ? `<span class="ref">${esc(e.ref)}</span>` : ""}</div>`;
  const k = kindOf(e), [pic, verb] = KIND[k];
  const label = k === "CALLERROR" || k === "CALLRESULTERROR" ? `${esc(e.action || "?")} · ${esc(e.error_code)}`
    : k === "CALLRESULT" ? `${esc(e.action || "?")}` : esc(e.action || e.kind);
  const vchip = e.version ? `<span class="vchip ${vclass(e.version)}">${esc(e.version)}</span>` : "";
  const sender = e.dir === "to_csms" ? e.station : "CSMS", receiver = e.dir === "to_csms" ? "CSMS" : e.station;
  return `<div class="ev" data-i="${i}" data-pair="${esc(e.station)}|${esc(e.uid)}" onclick="explain(${i})"
      onmouseenter="pair(this,true)" onmouseleave="pair(this,false)" title="${esc(sender)} ${verb} ${esc(receiver)}">
    <span class="time">${hhmmss(e.t)}</span>
    <div class="arrow ${e.dir} k-${k}"><div class="line"></div>
      <span class="lab">${pic} <b>${label}</b> <span class="kind">${esc(e.kind)}</span>${vchip} <span class="sub">${esc(e.station)} #${esc(String(e.uid).slice(0, 6))}</span></span>
      <span class="sum">${esc(summary(e))}</span></div>
    <span class="who">${e.dir === "to_csms" ? "🖥️" : "🔌"}</span></div>`;
}
function renderLog() {
  $("#log").innerHTML = events.map((e, i) => (shown(e) ? eventHtml(e, i) : "")).join("");
  $("#log").scrollTop = $("#log").scrollHeight;
}
function appendEvent(e) {
  if (!shown(e)) return;
  const l = $("#log"), stick = l.scrollHeight - l.scrollTop - l.clientHeight < 80;
  l.insertAdjacentHTML("beforeend", eventHtml(e, events.length - 1));
  if (stick) l.scrollTop = l.scrollHeight;
}
function pair(row, on) {
  $$(`.ev[data-pair="${CSS.escape(row.dataset.pair)}"]`).forEach((r) => r.classList.toggle("pair", on));
}

// ======================================================================= explanations
const A = (meaning, from, refs) => ({ meaning, from, refs });
const ACTIONS = {
  BootNotification: A("“I just started (or rebooted). Here is who I am: vendor, model…” The answer decides whether the station is Accepted, Pending or Rejected, and gives the heartbeat interval.", "station", { "1.6": "1.6 §4.2", "2": "B01–B03" }),
  Heartbeat: A("“I'm still here.” Sent when the station has been quiet for the heartbeat interval. The answer carries the CSMS clock.", "station", { "1.6": "1.6 §4.6", "2": "G02" }),
  StatusNotification: A("“One of my connectors changed state” (plugged, charging, faulted…).", "station", { "1.6": "1.6 §4.9", "2": "G01" }),
  Authorize: A("“Is this card / badge allowed to charge?” The answer says Accepted, Invalid, Blocked…", "station", { "1.6": "1.6 §4.1", "2": "C01" }),
  TransactionEvent: A("2.x: everything about a charging session in one message. eventType Started → Updated (meter values) → Ended.", "station", { "2": "E01–E15" }),
  StartTransaction: A("1.6: “a charging session started on connector N with this card.” The CSMS answers with the transactionId it assigns.", "station", { "1.6": "1.6 §4.8" }),
  StopTransaction: A("1.6: “the session with this transactionId stopped, final meter value, reason.”", "station", { "1.6": "1.6 §4.10" }),
  MeterValues: A("Energy / power measurements. In 1.6 the transaction meter values travel here; in 2.x they're inside TransactionEvent.", "station", { "1.6": "1.6 §4.7", "2": "J01" }),
  NotifyReport: A("2.x: the station sends its configuration (the device model) because the CSMS asked with GetBaseReport.", "station", { "2": "B07" }),
  NotifyEvent: A("2.x: something worth reporting happened on a component.", "station", { "2": "N07" }),
  OpenPeriodicEventStream: A("2.1: “I'm opening a stream of frequent values (here: voltage); I'll send them with SEND messages.”", "station", { "2": "N11" }),
  ClosePeriodicEventStream: A("2.1: “I'm closing that stream.”", "station", { "2": "N11" }),
  NotifyPeriodicEventStream: A("2.1: a batch of stream values. Sent as SEND: the CSMS never answers it.", "station", { "2": "N11 · 2.1 Part 4 §4.2.4" }),
  GetVariables: A("2.x: the CSMS reads configuration values (Component.Variable).", "csms", { "2": "B06" }),
  SetVariables: A("2.x: the CSMS changes configuration values.", "csms", { "2": "B05" }),
  GetBaseReport: A("2.x: the CSMS asks for the whole configuration; it arrives later in NotifyReport messages.", "csms", { "2": "B07" }),
  GetConfiguration: A("1.6: the CSMS reads configuration keys; the values come back in the answer.", "csms", { "1.6": "1.6 §5.8" }),
  ChangeConfiguration: A("1.6: the CSMS changes one configuration key.", "csms", { "1.6": "1.6 §5.3" }),
  Reset: A("The CSMS asks the station to reboot (it will send a new BootNotification).", "csms", { "1.6": "1.6 §5.14", "2": "B11–B12" }),
  TriggerMessage: A("The CSMS asks the station to send a given message now (e.g. a StatusNotification).", "csms", { "1.6": "1.6 §5.17", "2": "F06" }),
  ChangeAvailability: A("The CSMS makes a connector (or the whole station) operative or inoperative.", "csms", { "1.6": "1.6 §5.2", "2": "G03–G04" }),
  RequestStartTransaction: A("2.x: the CSMS asks the station to *try* to start charging for a card (e.g. from a phone app).", "csms", { "2": "F01–F02" }),
  RequestStopTransaction: A("2.x: the CSMS asks the station to stop a session.", "csms", { "2": "F03" }),
  RemoteStartTransaction: A("1.6: the CSMS asks the station to start charging for a card (e.g. from a phone app).", "csms", { "1.6": "1.6 §5.11" }),
  RemoteStopTransaction: A("1.6: the CSMS asks the station to stop the session with this transactionId.", "csms", { "1.6": "1.6 §5.12" }),
  UnlockConnector: A("The CSMS asks the station to release the cable lock.", "csms", { "1.6": "1.6 §5.18", "2": "F05" }),
  DataTransfer: A("A vendor-specific message outside the standard.", "either", { "1.6": "1.6 §4.3", "2": "P01–P02" }),
  SecurityEventNotification: A("2.x: the station reports a security event.", "station", { "2": "A04" }),
  SignCertificate: A("1.6 security extension: the station asks for a signed certificate.", "station", { "1.6": "1.6 security white paper" }),
  MakeCoffee: A("Not an OCPP message at all: a deliberate mistake to see the CSMS refuse an unknown action.", "station", {}),
};
const KIND_TEXT = {
  CALL: "a request: the receiver must answer with a CALLRESULT or a CALLERROR carrying the same id.",
  CALLRESULT: "the normal answer to a request. It has no action name: the id tells which request it answers.",
  CALLERROR: "an error answer: the request was malformed, not allowed, or couldn't be processed.",
  CALLRESULTERROR: "OCPP 2.1: “I received your answer but it was broken.”",
  SEND: "OCPP 2.1: a one-way message. Nobody answers it.",
  INVALID: "this isn't valid OCPP-J (broken JSON, unknown frame type, unreadable id…). See what the receiver did with it.",
};

function flat(obj, prefix = "", out = []) {
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    for (const [k, v] of Object.entries(obj)) flat(v, prefix ? `${prefix}.${k}` : k, out);
  } else if (Array.isArray(obj)) {
    obj.forEach((v, i) => flat(v, `${prefix}[${i}]`, out));
  } else out.push([prefix, obj]);
  return out;
}

function explain(i) {
  const e = events[i];
  selected = i;
  $$(".ev.sel").forEach((r) => r.classList.remove("sel"));
  $(`.ev[data-i="${i}"]`)?.classList.add("sel");
  const k = kindOf(e), [pic, verb] = KIND[k];
  const fromStation = e.dir === "to_csms";
  const sender = fromStation ? e.station : "CSMS", receiver = fromStation ? "CSMS" : e.station;
  const action = e.action && ACTIONS[e.action];
  const vkey = e.version === "1.6" ? "1.6" : "2";
  const partners = events.map((x, j) => [x, j]).filter(([x, j]) => j !== i && x.type === "frame" && x.station === e.station && x.uid === e.uid);
  let pairLine = "";
  if (k === "CALL") {
    pairLine = partners.length
      ? partners.map(([x, j]) => `↳ answered at ${hhmmss(x.t)} with <span class="linkto" onclick="explain(${j})">${KIND[kindOf(x)][0]} ${esc(x.kind)}${x.error_code ? " " + esc(x.error_code) : ""}</span>`).join("<br>")
      : "↳ no answer (yet). An unanswered CALL eventually times out.";
  } else if (k === "CALLRESULT" || k === "CALLERROR" || k === "CALLRESULTERROR") {
    pairLine = partners.length
      ? partners.map(([x, j]) => `↰ this answers <span class="linkto" onclick="explain(${j})">${KIND[kindOf(x)][0]} ${esc(x.action || x.kind)}</span> sent at ${hhmmss(x.t)}`).join("<br>")
      : `↰ answers a request with id ${esc(e.uid)}${e.uid === "-1" ? " (\"-1\" = the request's id couldn't be read)" : ""}`;
  } else if (k === "SEND") pairLine = "↳ no answer will come: that's the point of SEND.";
  const fields = e.payload && !e.problem ? flat(e.payload) : [];
  const fieldRows = fields.length ? `<table class="fields">${fields.map(([p, v]) => `<tr><td>${esc(p)}</td><td>${esc(JSON.stringify(v))}</td></tr>`).join("")}</table>` : `<p class="sub">no payload fields</p>`;
  let raw = e.raw;
  try { raw = JSON.stringify(JSON.parse(raw), null, 2); } catch { raw += "\n\n(not valid JSON)"; }
  const box = $("#explain");
  box.classList.remove("hidden");
  box.innerHTML = `
    <h3>${pic} ${esc(e.action || e.kind)} <span class="badge idle">${esc(e.kind)}</span>
      ${e.version ? badge(`ver ${vclass(e.version)}`, "OCPP " + e.version) : ""}
      <button style="margin-left:auto" onclick="closeExplain()">✕</button></h3>
    <div class="who-box"><div class="actor"><span class="pic">${fromStation ? "🔌" : "🖥️"}</span>${esc(sender)}<div class="sub">sender</div></div>
      <div><span class="envelope">${pic}</span><div class="sub">${esc(verb)} →</div></div>
      <div class="actor"><span class="pic">${fromStation ? "🖥️" : "🔌"}</span>${esc(receiver)}<div class="sub">receiver</div></div></div>
    <p><b>${esc(e.kind)}</b>: ${esc(KIND_TEXT[k])}</p>
    ${action ? `<p><b>${esc(e.action)}</b>: ${esc(action.meaning)} ${action.refs[vkey] ? `<span class="ref">${esc(action.refs[vkey])}</span>` : ""}</p>` : ""}
    ${e.error_code ? `<p><b>Error code ${esc(e.error_code)}</b>: ${esc(e.payload?.description || "")}</p>` : ""}
    ${e.problem ? `<p><b>Problem:</b> ${esc(e.problem)}</p>` : ""}
    <p>${pairLine}</p>
    <h3 class="sub">Fields</h3>${fieldRows}
    <details><summary>Raw frame (exactly what travelled on the WebSocket)</summary><pre class="raw">${esc(raw)}</pre></details>`;
}
function closeExplain() { $("#explain").classList.add("hidden"); $$(".ev.sel").forEach((r) => r.classList.remove("sel")); selected = null; }

// ======================================================================= schema comparison
let current = null;
function initSchemas() {
  const opts = (sel) => meta.versions.slice().reverse().map((v) => `<option ${v === sel ? "selected" : ""}>${v}</option>`).join("");
  $("#verA").innerHTML = opts(ui.values.verA || "1.6");
  $("#verB").innerHTML = opts(ui.values.verB || "2.0.1");
  renderSchemaList();
}
function pairKey() {
  const a = $("#verA").value, b = $("#verB").value, order = ["1.6", "2.0.1", "2.1"];
  return order.indexOf(a) <= order.indexOf(b) ? [a, b, `${a}|${b}`] : [a, b, `${b}|${a}`];
}
function statusFor(item, a, b) {
  if (a === b) return item.in[a] ? "same" : "absent";
  const [, , key] = pairKey();
  let st = item.status[key] || "absent";
  if (st.startsWith("only")) st = `only ${item.in[a] ? a : b}`;
  return st;
}
const STATUS_BADGE = { same: ["idle", "same"], changed: ["warn", "changed"], renamed: ["warn", "replaced"] };
function renderSchemaList() {
  if (!meta.catalogue) return;
  const [a, b] = pairKey(), f = ($("#schemaFilter").value || "").toLowerCase(), only = $("#onlyDiff").checked;
  const kinds = $$(".kindf").filter((x) => x.checked).map((x) => x.value);
  const counts = {};
  const items = meta.catalogue.items.filter((it) => {
    const st = statusFor(it, a, b);
    if (st === "absent" || !kinds.includes(it.kind)) return false;
    counts[st] = (counts[st] || 0) + 1;
    return it.action.toLowerCase().includes(f) && (!only || st !== "same");
  });
  $("#schemaCounts").innerHTML = Object.entries(counts).map(([k, v]) => `${esc(k)}: <b>${v}</b>`).join(" · ") +
    ` <span class="sub">(source: ${esc(Object.entries(meta.catalogue.sources).map(([v, s]) => `${v} ${s}`).join(", "))})</span>`;
  $("#schemaList").innerHTML = items.map((it) => {
    const st = statusFor(it, a, b), [cls, txt] = STATUS_BADGE[st] || (st.startsWith("only") ? [st.endsWith(b) ? "ok" : "bad", st] : ["idle", st]);
    const on = current && current.action === it.action && current.kind === it.kind ? "on" : "";
    const ext = it.ext16 && (a === "1.6" || b === "1.6") ? ` <span class="k" title="from the OCPP 1.6 security white paper, not in the 1.6 Edition 2 schemas">🔐 1.6 sec. ext.</span>` : "";
    return `<div class="sitem ${on}" data-a="${esc(it.action)}" data-k="${it.kind}"><span>${esc(it.action)} <span class="k">${it.kind}</span>${ext}</span>${badge(cls, txt)}</div>`;
  }).join("") || `<p class="sub">nothing matches</p>`;
  $$(".sitem").forEach((el) => (el.onclick = () => {
    current = { action: el.dataset.a, kind: el.dataset.k };
    $$(".sitem.on").forEach((x) => x.classList.remove("on")); el.classList.add("on");
    send({ cmd: "schema_compare", a, b, action: current.action, kind: current.kind });
  }));
}
function cell(f, other) {
  if (!f) return `<span class="sub">—</span>`;
  const bits = [`<span class="t">${esc(f.type || "?")}</span>`];
  if (f.required) bits.push(`<span class="req">required</span>`);
  if (f.maxLength) bits.push(`≤ ${f.maxLength} chars`);
  if (f.format) bits.push(esc(f.format));
  let en = "";
  if (f.enum) {
    const o = new Set(other?.enum || []);
    en = `<div class="enum">${f.enum.map((x) => `<span class="${other && other.enum && !o.has(x) ? "diff" : ""}">${esc(x)}</span>`).join("")}</div>`;
  }
  return `<div class="cell">${bits.join(" · ")}${en}</div>`;
}
function renderCompare(c) {
  const box = $("#schemaDetail"), name = `${c.action} (${c.kind})`;
  const avail = (v, ok) => (ok ? badge(`ver ${vclass(v)}`, `${v} ✔`) : badge("idle", `${v}: not in this version`));
  const summary = c.in_a && c.in_b
    ? (c.status === "same" ? `<p>${badge("idle", "identical structure")} Same fields, required flags, types, lengths and allowed values.</p>`
      : `<p>${c.added.length ? badge("ok", `+${c.added.length} new`) : ""} ${c.removed.length ? badge("bad", `−${c.removed.length} gone`) : ""} ${c.changed ? badge("warn", `${c.changed} changed`) : ""}</p>`)
    : "";
  const rows = c.rows.filter((r) => !$("#onlyDiff").checked || r.change !== "same" || !(c.in_a && c.in_b));
  box.innerHTML = `
    <h2>${esc(name)}</h2>
    <div class="row">${avail(c.a, c.in_a)} ${avail(c.b, c.in_b)}</div>
    ${c.replaced ? `<p>🔁 <b>${esc(c.replaced)}</b></p>` : ""}
    ${c.ext16 ? `<p>🔐 In 1.6 this message comes from the <b>OCPP 1.6 security white paper</b> (an extension): it is not part of the 1.6 Edition 2 schema set.</p>` : ""}
    ${summary}
    ${rows.length ? `<table class="ftable"><colgroup><col style="width:34%"><col style="width:33%"><col style="width:33%"></colgroup>
      <tr><th>field</th><th>OCPP ${esc(c.a)}</th><th>OCPP ${esc(c.b)}</th></tr>
      ${rows.map((r) => `<tr class="${r.change}"><td>${esc(r.path)}</td><td>${cell(r.a, r.b)}</td><td>${cell(r.b, r.a)}</td></tr>`).join("")}
    </table>` : `<p class="sub">no fields to show${$("#onlyDiff").checked ? " (untick “only differences” to see all)" : ""}</p>`}
    <div class="rawtabs">${c.schema_a ? `<button class="chip on" data-raw="a">raw ${esc(c.a)} schema</button>` : ""}${c.schema_b ? `<button class="chip ${c.schema_a ? "" : "on"}" data-raw="b">raw ${esc(c.b)} schema</button>` : ""}</div>
    <pre class="rawbox" id="rawbox"></pre>`;
  const show = (which) => {
    $("#rawbox").textContent = JSON.stringify(which === "a" ? c.schema_a : c.schema_b, null, 2);
    $$("[data-raw]", box).forEach((b) => b.classList.toggle("on", b.dataset.raw === which));
  };
  $$("[data-raw]", box).forEach((b) => (b.onclick = () => show(b.dataset.raw)));
  if (c.schema_a || c.schema_b) show(c.schema_a ? "a" : "b");
  else $("#rawbox").remove();
}

// ======================================================================= wiring
function showTab(tab) {
  $$(".tab").forEach((x) => x.classList.toggle("on", x.dataset.tab === tab));
  ["liveTab", "guideTab", "schemaTab"].forEach((id) => $("#" + id).classList.toggle("hidden", id !== tab));
}
function openFromHash() {   // deep links: #guide, #schemas, #schemas/1.6/2.0.1/BootNotification/request
  const [tab, a, b, action, kind] = location.hash.slice(1).split("/");
  if (tab === "guide") showTab("guideTab");
  if (tab === "schemas") {
    showTab("schemaTab");
    if (a && b) { $("#verA").value = a; $("#verB").value = b; renderSchemaList(); }
    if (action) { current = { action, kind: kind || "request" }; send({ cmd: "schema_compare", a, b, action, kind: kind || "request" }); renderSchemaList(); }
  }
}
$$(".tab").forEach((t) => (t.onclick = () => {
  showTab(t.dataset.tab);
  history.replaceState(null, "", { liveTab: "#", guideTab: "#guide", schemaTab: "#schemas" }[t.dataset.tab]);
}));
["#verA", "#verB"].forEach((s) => ($(s).onchange = () => { ui.values[s.slice(1)] = $(s).value; renderSchemaList();
  if (current) { const [a, b] = pairKey(); send({ cmd: "schema_compare", a, b, ...current }); } }));
$("#schemaFilter").oninput = renderSchemaList;
$("#onlyDiff").onchange = () => { renderSchemaList(); if (current) { const [a, b] = pairKey(); send({ cmd: "schema_compare", a, b, ...current }); } };
$$(".kindf").forEach((x) => (x.onchange = renderSchemaList));
$("#hideNoise").onchange = renderLog;
$("#clear").onclick = () => { send({ cmd: "clear_log" }); events = []; renderLog(); closeExplain(); };
$("#addStation").onclick = () => send({ cmd: "add_station", offer: $("#newOffer").value.split(","), evses: +$("#newEvses").value || 1 });
connect();
