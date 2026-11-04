"use strict";
// OCPP Playground dashboard: talks to the Python side over ws://<host>/ws (see playground/web/server.py).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let ws, meta = {}, state = { stations: [], policy: {} }, events = [], filter = "all";
const ui = { open: new Set(), values: {} };          // <details> kept open + input values, across re-renders

// ---------------------------------------------------------------- connection
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => ($("#conn").textContent = "● live");
  ws.onclose = () => { $("#conn").textContent = "disconnected, retrying…"; setTimeout(connect, 1500); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "hello") { meta = m.meta; events = m.history; setState(m); renderLog(); renderSchemaList(); openFromHash(); }
    else if (m.type === "state") setState(m);
    else if (m.type === "schema_diff") renderDiff(m);
    else { events.push(m); if (events.length > 1500) events.shift(); appendEvent(m); }
  };
}
const send = (o) => ws && ws.readyState === 1 && ws.send(JSON.stringify(o));
const act = (id, action, extra = {}) => send({ cmd: "station", id, action, ...extra });
const csms = (id, command, args = {}) => send({ cmd: "csms", id, command, args });
const val = (key, fallback) => ui.values[key] ?? fallback;

// ---------------------------------------------------------------- state → cards
function setState(m) {
  state = { stations: m.stations, policy: m.policy };
  const typing = document.activeElement && ["INPUT", "SELECT"].includes(document.activeElement.tagName)
    && document.activeElement.closest("#stations, #policy");
  if (!typing) { renderPolicy(); renderStations(); }
  renderFilters();
}

const badge = (cls, text) => `<span class="badge ${cls}">${esc(text)}</span>`;
const verBadge = (v) => v ? badge(`ver ${v === "2.1" ? "v21" : "v201"}`, `OCPP ${v}`) : badge("idle", "no version");
function regBadge(r) {
  return ({ Accepted: badge("ok", "✔ Accepted"), Pending: badge("warn", "⏳ Pending"), Rejected: badge("bad", "✖ Rejected") })[r]
    || badge("idle", r === "—" || !r ? "— none yet" : "? " + r);
}
function connBadge(s) {
  return ({ Available: badge("ok", "○ Available"), Occupied: badge("warn", "🔌 Occupied"), Faulted: badge("bad", "⚠ Faulted"),
            Unavailable: badge("idle", "⛔ Unavailable"), Reserved: badge("warn", "◆ Reserved") })[s] || badge("idle", s);
}
function connectivity(c) {
  if (!c) return badge("idle", "never connected");
  if (!c.online) return badge("bad", "✖ Offline");
  const ago = c.last_seen_ms ? Math.round((Date.now() - c.last_seen_ms) / 1000) : null;
  if (ago !== null && ago > 2 * state.policy.heartbeat_interval)
    return badge("warn", `⚠ Silent ${ago} s`) + ` <span class="sub">(playground rule: > 2 × heartbeat)</span>`;
  return badge("ok", "▲ Online") + ` <span class="sub ago" data-ls="${c.last_seen_ms}">last msg ${ago ?? "?"} s ago</span>`;
}
const isSilent = (c) => c && c.online && c.last_seen_ms && (Date.now() - c.last_seen_ms) / 1000 > 2 * state.policy.heartbeat_interval;

function captureUi(root) {
  $$("details[data-key]", root).forEach((d) => d.open ? ui.open.add(d.dataset.key) : ui.open.delete(d.dataset.key));
  $$("[data-val]", root).forEach((el) => (ui.values[el.dataset.val] = el.type === "checkbox" ? el.checked : el.value));
}
function restoreUi(root) {
  $$("details[data-key]", root).forEach((d) => (d.open = ui.open.has(d.dataset.key)));
  $$("[data-val]", root).forEach((el) => {
    if (!(el.dataset.val in ui.values)) return;
    if (el.type === "checkbox") el.checked = ui.values[el.dataset.val]; else el.value = ui.values[el.dataset.val];
  });
}
document.addEventListener("change", (e) => e.target.dataset?.val && (ui.values[e.target.dataset.val] =
  e.target.type === "checkbox" ? e.target.checked : e.target.value));
document.addEventListener("toggle", (e) => e.target.dataset?.key &&
  (e.target.open ? ui.open.add(e.target.dataset.key) : ui.open.delete(e.target.dataset.key)), true);

// ---------------------------------------------------------------- CSMS policy panel
function renderPolicy() {
  const p = state.policy, box = $("#policy");
  captureUi(box);
  const sel = (key, opts) => `<select data-pol="${key}">${opts.map(([v, t]) => `<option value="${v}" ${p[key] === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
  box.innerHTML = `
    <h2>CSMS settings <span class="sub">(playground decisions, not OCPP rules)</span></h2>
    <details data-key="policy" open>
      <summary>show / hide</summary>
      <div class="row"><span class="lbl">Versions enabled</span>
        <label><input type="checkbox" data-ver="2.1" ${p.versions.includes("2.1") ? "checked" : ""}> 2.1</label>
        <label><input type="checkbox" data-ver="2.0.1" ${p.versions.includes("2.0.1") ? "checked" : ""}> 2.0.1</label>
        <span class="sub">(preference: ${esc(p.versions.join(" > "))})</span></div>
      <div class="row"><span class="lbl">BootNotification answer</span>${sel("registration_mode", [["pending_then_accept", "Pending first, then Accepted"], ["accept", "Accepted"], ["reject", "Rejected"]])}</div>
      <div class="row"><span class="lbl">Intervals (s)</span> heartbeat <input type="number" min="1" data-pol="heartbeat_interval" value="${p.heartbeat_interval}">
        retry <input type="number" min="0" data-pol="retry_interval" value="${p.retry_interval}"></div>
      <div class="row"><span class="lbl">Unreadable frame</span>${sel("unreadable_frames", [["answer", "answer CALLERROR with id \"-1\""], ["drop", "drop silently"]])}</div>
      <div class="row"><span class="lbl">Reused message id</span>${sel("duplicate_ids", [["reject", "reject (RpcFrameworkError)"], ["allow", "answer normally"]])}</div>
      <div class="row"><span class="lbl">Authorize detail</span>${sel("authorize_detail", [["full", "full status (Blocked, Expired…)"], ["accepted_or_invalid", "Accepted / Invalid only"]])}</div>
      <div class="row"><span class="lbl">Fault injection</span><label><input type="checkbox" data-pol="fault_bad_heartbeat_response" ${p.fault_bad_heartbeat_response ? "checked" : ""}>
        HeartbeatResponse without currentTime</label></div>
      <div class="row"><span class="lbl">Tokens</span><span class="mono">${Object.entries(p.tokens).map(([k, v]) => `${esc(k)}=${esc(v)}`).join(" · ")}</span></div>
      <div class="row"><button id="forget">Forget registrations</button></div>
    </details>`;
  restoreUi(box);
  $$("[data-pol]", box).forEach((el) => (el.onchange = () => {
    const v = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value;
    send({ cmd: "policy", [el.dataset.pol]: v });
  }));
  $$("[data-ver]", box).forEach((el) => (el.onchange = () => {
    const versions = ["2.1", "2.0.1"].filter((v) => $(`[data-ver="${v}"]`, box).checked);
    send({ cmd: "policy", versions });
  }));
  $("#forget", box).onclick = () => send({ cmd: "forget" });
}

// ---------------------------------------------------------------- station cards
const TOKENS = ["AXEL-0001", "FLEET-0042", "BLOCKED-01", "EXPIRED-01", "UNKNOWN-99"];
const EDGES = [
  ["heartbeat_now", "Heartbeat now", "a Heartbeat whatever the registration (try it while Pending)"],
  ["unknown_action", "Unknown action", "CALL MakeCoffee → NotImplemented"],
  ["unsupported_action", "Valid but unsupported", "SecurityEventNotification → NotSupported"],
  ["invalid_payload", "Bad enum value", "connectorStatus \"Exploded\""],
  ["wrong_type", "Wrong type", "evseId as a string"],
  ["missing_field", "Missing field", "BootNotification without model"],
  ["malformed", "Malformed JSON", "frame cut off"],
  ["unreadable_id", "Unreadable id", "MessageId is a number"],
  ["long_id", "Id > 36 chars", ""],
  ["duplicate_id", "Same id twice", ""],
  ["unknown_type", "MessageTypeId 7", "unknown frame type"],
  ["send_frame", "SEND frame [6]", "unknown in 2.0.1; valid but unopened stream in 2.1"],
];

function evseHtml(id, n, e, live, accepted) {
  const k = `${id}-e${n}`, tx = e.tx;
  const txLine = tx ? `<div class="tx">tx ${esc(tx.id)} · seq ${tx.seq} · ${(tx.energy_wh / 1000).toFixed(3)} kWh · ${tx.charging ? "⚡ charging" : "waiting"}${tx.token ? " · " + esc(tx.token) : ""}</div>` : "";
  return `<div class="evse"><div class="row" style="margin-top:0"><b>EVSE ${n}</b> ${connBadge(e.status)}
      ${!e.operative ? badge("idle", "inoperative") : ""}</div>${txLine}
    <div class="row">
      <button ${e.cable || e.fault || !e.operative ? "disabled" : ""} onclick="act('${id}','plug',{evse:${n}})">Plug in</button>
      <button ${!e.cable ? "disabled" : ""} onclick="act('${id}','unplug',{evse:${n}})">Unplug</button>
      <select data-val="${k}-tok">${TOKENS.map((t) => `<option>${t}</option>`).join("")}</select>
      <button onclick="act('${id}','token',{evse:${n},token:val('${k}-tok','AXEL-0001')})">Present card</button>
      ${e.fault ? `<button onclick="act('${id}','clear_fault',{evse:${n}})">Clear fault</button>`
                : `<button onclick="act('${id}','fault',{evse:${n}})">Fault</button>`}
    </div></div>`;
}

function sparkline(values) {
  if (!values || values.length < 2) return `<span class="sub">waiting for SEND frames…</span>`;
  const nums = values.map((v) => +v.v), min = Math.min(...nums) - 0.5, max = Math.max(...nums) + 0.5;
  const pts = nums.map((v, i) => `${(i / (nums.length - 1)) * 300},${36 - ((v - min) / (max - min)) * 32 - 2}`).join(" ");
  return `<svg class="spark" viewBox="0 0 300 36" preserveAspectRatio="none" aria-label="voltage stream">
    <polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts}"/></svg>
    <span class="sub">${nums.length} values · last ${esc(values[values.length - 1].v)} V</span>`;
}

function stationHtml({ id, station: s, csms: c }) {
  const live = s.link, accepted = live && s.registration === "Accepted", v21 = s.version === "2.1";
  const k = id;
  const txs = (c?.transactions || []).map((t) => `<div class="tx">${esc(t.id)} · ${t.state} · ${(t.energy_wh / 1000).toFixed(3)} kWh · seq ${t.last_seq}${t.offline_events ? ` · ${t.offline_events} offline evt` : ""}${t.stopped_reason ? " · " + t.stopped_reason : ""}</div>`).join("") || `<span class="sub">none</span>`;
  const streams = Object.entries(c?.streams || {}).map(([sid, st]) => `<div>stream ${sid}: ${sparkline(st.values)}</div>`).join("");
  return `<div class="card">
    <h2>${esc(id)} ${verBadge(s.version)} ${badge(s.power ? "ok" : "idle", s.power ? "⏻ on" : "⏻ off")}
      <span class="sub">offers ${s.offer.map((o) => esc(o)).join(", ")}</span></h2>
    <div class="sub">doing: ${esc(s.activity)}</div>
    <div class="grid2">
      <div class="view"><h3>Station thinks</h3><div class="kv">
        <span>Registration</span><span>${regBadge(s.registration)}</span>
        <span>Link</span><span>${live ? badge("ok", "▲ connected") : badge("idle", "✖ not connected")} ${s.network_up ? "" : badge("bad", "network down")}</span>
        <span>Heartbeat</span><span>${s.variables["OCPPCommCtrlr.HeartbeatInterval"]} s</span>
        ${s.queued ? `<span>Queued</span><span>${badge("warn", s.queued + " TransactionEvent(s)")}</span>` : ""}
      </div></div>
      <div class="view"><h3>CSMS thinks</h3><div class="kv">
        <span>Registration</span><span>${c ? regBadge(c.registration) : badge("idle", "unknown station")}</span>
        <span>Connectivity</span><span>${connectivity(c)}</span>
        ${c ? `<span>Connectors</span><span>${Object.entries(c.connectors).map(([n, st]) => `${n} ${connBadge(st)}`).join(" ") || "—"}</span>
               <span>Boots</span><span>${c.boots}</span>` : ""}
      </div></div>
    </div>
    <div class="row"><span class="lbl">Power</span>
      <button ${s.power ? "disabled" : ""} onclick="act('${id}','power_on')">Power on</button>
      <button ${!s.power ? "disabled" : ""} onclick="act('${id}','power_off')">Power off</button>
      <span class="lbl">Network</span>
      <button ${!s.network_up ? "disabled" : ""} onclick="act('${id}','drop_network')">Drop</button>
      <button ${s.network_up ? "disabled" : ""} onclick="act('${id}','restore_network')">Restore</button>
      <label class="sub"><input type="checkbox" ${s.boot_on_reconnect ? "checked" : ""} onchange="act('${id}','boot_on_reconnect',{value:this.checked})"> boot on reconnect</label>
    </div>
    ${Object.entries(s.evses).map(([n, e]) => evseHtml(id, n, e, live, accepted)).join("")}
    ${v21 ? `<div class="row"><span class="lbl">2.1 stream</span>
      <button ${!accepted ? "disabled" : ""} onclick="act('${id}','open_stream')">Open voltage stream</button>
      <button ${!s.streams.length ? "disabled" : ""} onclick="act('${id}','close_stream')">Close</button></div>${streams}` : ""}
    <details data-key="${k}-tx"><summary>Transactions seen by the CSMS</summary>${txs}</details>
    <details data-key="${k}-cmd"><summary>CSMS → station requests</summary>
      <div class="row"><button ${!live ? "disabled" : ""} onclick="csms('${id}','get_variables')">GetVariables HeartbeatInterval</button>
        <input type="number" min="1" data-val="${k}-hb" value="20">
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','set_variables',{value:val('${k}-hb','20')})">SetVariables</button></div>
      <div class="row"><button ${!live ? "disabled" : ""} onclick="csms('${id}','get_base_report')">GetBaseReport</button>
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','reset')">Reset</button>
        <select data-val="${k}-trig"><option>StatusNotification</option><option>Heartbeat</option><option>BootNotification</option><option>MeterValues</option></select>
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','trigger',{message:val('${k}-trig','StatusNotification')})">TriggerMessage</button></div>
      <div class="row"><select data-val="${k}-av"><option>Inoperative</option><option>Operative</option></select>
        <button ${!live ? "disabled" : ""} onclick="csms('${id}','change_availability',{operational_status:val('${k}-av','Inoperative'),evse_id:0})">ChangeAvailability (all)</button></div>
      <div class="row"><button ${!live ? "disabled" : ""} onclick="csms('${id}','request_start',{evse_id:1,token:'FLEET-0042'})">RequestStartTransaction (EVSE 1, FLEET-0042)</button>
        <button ${!live || !Object.values(s.evses).some((e) => e.tx) ? "disabled" : ""}
          onclick="csms('${id}','request_stop',{transaction_id:'${Object.values(s.evses).find((e) => e.tx)?.tx.id || ""}'})">RequestStopTransaction</button></div>
      ${c && Object.keys(c.variables).length ? `<h3>Variables reported to the CSMS</h3><div class="mono">${Object.entries(c.variables).map(([kk, vv]) => `${esc(kk)} = ${esc(vv)}`).join("<br>")}</div>` : ""}
    </details>
    <details data-key="${k}-edge"><summary>Send something wrong (protocol edge cases)</summary>
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
  $$(".ago").forEach((el) => (el.textContent = `last msg ${Math.round((Date.now() - +el.dataset.ls) / 1000)} s ago`));
  const key = state.stations.map((s) => (isSilent(s.csms) ? 1 : 0)).join("");
  if (key !== silentKey) { silentKey = key; renderStations(); }
}, 1000);

// ---------------------------------------------------------------- sequence diagram
function renderFilters() {
  const ids = ["all", ...state.stations.map((s) => s.id), "CSMS"];
  $("#filters").innerHTML = ids.map((id) => `<button class="chip ${filter === id ? "on" : ""}" onclick="filter='${id}';renderFilters();renderLog()">${id === "all" ? "All" : esc(id)}</button>`).join(" ");
}
const NOISE = new Set(["Heartbeat"]);
function shown(e) {
  if (filter !== "all" && e.station !== filter) return false;
  if ($("#hideNoise").checked && e.type === "frame") {
    if (NOISE.has(e.action)) return false;
    if (e.action === "TransactionEvent" && e.kind === "CALL" && e.payload?.triggerReason === "MeterValuePeriodic") return false;
    if (e.action === "TransactionEvent" && e.kind === "CALLRESULT") return false;
  }
  return true;
}
const hhmmss = (t) => new Date(t).toTimeString().slice(0, 8);
function summary(e) {
  if (e.problem) return e.problem + " · " + e.raw;
  const s = JSON.stringify(e.payload ?? {});
  return s.length > 140 ? s.slice(0, 137) + "…" : s;
}
function eventHtml(e) {
  if (e.type === "note")
    return `<div class="note ${e.level}"><b>${esc(e.station)}</b> · ${esc(e.text)}${e.ref ? `<span class="ref">${esc(e.ref)}</span>` : ""}</div>`;
  const kindClass = e.kind.startsWith("TYPE") ? "k-TYPE" : `k-${e.kind}`;
  const label = e.kind === "CALLERROR" || e.kind === "CALLRESULTERROR" ? `${esc(e.action || "?")} ✖ ${esc(e.error_code)}`
    : e.kind === "SEND" ? `${esc(e.action)} (no answer)` : esc(e.action || e.kind);
  const vchip = e.version ? `<span class="vchip ${e.version === "2.1" ? "v21" : "v201"}">${esc(e.version)}</span>` : "";
  return `<div class="ev" onclick="toggleRaw(this)" data-raw="${esc(e.raw)}">
    <span class="time">${hhmmss(e.t)}</span><span class="sid">${esc(e.station)}</span>
    <div class="arrow ${e.dir} ${kindClass}"><div class="line"></div>
      <span class="lab"><b>${label}</b> <span class="kind">${esc(e.kind)}</span>${vchip} <span class="sub">#${esc(String(e.uid).slice(0, 8))}</span></span>
      <span class="sum">${esc(summary(e))}</span></div>
    <span class="who">${e.dir === "to_csms" ? "→ CSMS" : "→ station"}</span></div>`;
}
function toggleRaw(row) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains("raw")) { next.remove(); return; }
  let txt = row.dataset.raw;
  try { txt = JSON.stringify(JSON.parse(txt), null, 2); } catch { txt += "\n\n(not valid JSON)"; }
  row.insertAdjacentHTML("afterend", `<pre class="raw">${esc(txt)}</pre>`);
}
const atBottom = () => { const l = $("#log"); return l.scrollHeight - l.scrollTop - l.clientHeight < 80; };
function renderLog() { $("#log").innerHTML = events.filter(shown).map(eventHtml).join(""); $("#log").scrollTop = $("#log").scrollHeight; }
function appendEvent(e) {
  if (!shown(e)) return;
  const stick = atBottom();
  $("#log").insertAdjacentHTML("beforeend", eventHtml(e));
  if (stick) $("#log").scrollTop = $("#log").scrollHeight;
}

// ---------------------------------------------------------------- schema tab
function renderSchemaList() {
  const f = ($("#schemaFilter").value || "").toLowerCase(), only = $("#onlyChanged").checked;
  const st = meta.schema_status || {};
  $("#schemaList").innerHTML = (meta.schemas || [])
    .filter((n) => n.toLowerCase().includes(f) && (!only || st[n] !== "same"))
    .map((n) => {
      const s = st[n], b = s === "same" ? badge("idle", "same") : s === "changed" ? badge("warn", "changed") : badge(s === "only 2.1" ? "ok" : "bad", s);
      return `<div class="sitem" data-name="${esc(n)}"><span>${esc(n)}</span>${b}</div>`;
    }).join("");
  $$(".sitem").forEach((el) => (el.onclick = () => { $$(".sitem.on").forEach((x) => x.classList.remove("on")); el.classList.add("on"); send({ cmd: "schema_diff", name: el.dataset.name }); }));
}
function renderDiff(d) {
  const list = (items, cls, fmt = (x) => esc(x)) => items.length ? `<ul>${items.map((i) => `<li class="${cls}">${fmt(i)}</li>`).join("")}</ul>` : "";
  let body;
  if (d.only_in) body = `<p>${badge(d.only_in === "2.1" ? "ok" : "bad", "only in OCPP " + d.only_in)}</p>`;
  else if (!["added", "removed", "required_changed", "enum_added", "enum_removed", "type_changed"].some((k) => d[k].length))
    body = `<p>${badge("idle", "identical structure")} Same fields, required flags, types and enum values in 2.0.1 and 2.1.</p>`;
  else body = `
    ${d.added.length ? "<h3>Added in 2.1</h3>" + list(d.added, "add", (x) => "+ " + esc(x)) : ""}
    ${d.removed.length ? "<h3>Removed in 2.1</h3>" + list(d.removed, "rem", (x) => "− " + esc(x)) : ""}
    ${d.required_changed.length ? "<h3>Required flag changed</h3>" + list(d.required_changed, "chg", (x) => `${esc(x.field)}: ${x.from ? "required" : "optional"} → ${x.to ? "required" : "optional"}`) : ""}
    ${d.type_changed.length ? "<h3>Type changed</h3>" + list(d.type_changed, "chg", (x) => `${esc(x.field)}: ${esc(x.from)} → ${esc(x.to)}`) : ""}
    ${d.enum_added.length ? "<h3>Enum values added</h3>" + list(d.enum_added, "add", (x) => `${esc(x.field)}: + ${x.values.map(esc).join(", ")}`) : ""}
    ${d.enum_removed.length ? "<h3>Enum values removed</h3>" + list(d.enum_removed, "rem", (x) => `${esc(x.field)}: − ${x.values.map(esc).join(", ")}`) : ""}`;
  $("#schemaDiff").innerHTML = `<h2>${esc(d.name)}</h2><div class="diff">${body}</div>
    <details><summary>Raw JSON schemas side by side</summary><div class="cols">
      <div><h3>OCPP 2.0.1</h3><pre>${esc(d.old ? JSON.stringify(d.old, null, 2) : "—")}</pre></div>
      <div><h3>OCPP 2.1</h3><pre>${esc(d.new ? JSON.stringify(d.new, null, 2) : "—")}</pre></div></div></details>`;
}

// ---------------------------------------------------------------- wiring
function showTab(tab) {
  $$(".tab").forEach((x) => x.classList.toggle("on", x.dataset.tab === tab));
  ["liveTab", "schemaTab"].forEach((id) => $("#" + id).classList.toggle("hidden", id !== tab));
}
// Deep links: #schemas or #schemas/TransactionEventRequest
function openFromHash() {
  const [tab, name] = location.hash.slice(1).split("/");
  if (tab === "schemas") { showTab("schemaTab"); if (name) send({ cmd: "schema_diff", name }); }
}
$$(".tab").forEach((t) => (t.onclick = () => {
  showTab(t.dataset.tab);
  history.replaceState(null, "", t.dataset.tab === "schemaTab" ? "#schemas" : "#");
}));
$("#schemaFilter").oninput = renderSchemaList;
$("#onlyChanged").onchange = renderSchemaList;
$("#hideNoise").onchange = renderLog;
$("#clear").onclick = () => { send({ cmd: "clear_log" }); events = []; renderLog(); };
$("#addStation").onclick = () => send({ cmd: "add_station", offer: $("#newOffer").value.split(","), evses: +$("#newEvses").value || 1 });
connect();
