// Jarvis UI: orb + prompt + voice + settings, talking to the local server.
import { Orb } from "/ui/orb3d.js";
import { createMap } from "/ui/map.js";
import { createPhone, REMOTE } from "/ui/phone.js";

const TOKEN = document.querySelector('meta[name="jarvis-token"]').content;
const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const r = await fetch(path, {
    ...opts,
    headers: { "content-type": "application/json", "x-jarvis-token": TOKEN, ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
};

const orb = new Orb($("orb"));
// opened from a phone (e.g. over Tailscale): its own mic, speaker and GPS - see phone.js
const phone = REMOTE ? createPhone({ api, token: TOKEN, orb, toast: (...a) => toast(...a),
  onText: (text) => send({ type: "ask", text, source: "phone" }) }) : null;
if (REMOTE) document.body.classList.add("remote");
new ResizeObserver(() => orb.resize()).observe($("orb"));   // it shrinks into the corner in model mode
let ws = null;
let settings = null;
let voiceStatus = "off";
let busy = false;
let replyText = "";
let replyTimer = null;

// ------------------------------------------------------------------ helpers
function escapeHtml(s) { return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function renderMd(s) {
  const parts = s.split(/```(\w*)\n?([\s\S]*?)(?:```|$)/g);
  let html = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 3 === 0) {
      html += escapeHtml(parts[i])
        .replace(/!\[([^\]]*)\]\((\/api\/(?:media|camera\/snap)\/[\w.\-]+)\)/g,
          (m, alt, url) => `<img class="md-img" src="${url}?token=${encodeURIComponent(TOKEN)}" alt="${alt}">`)
        .replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
        .replace(/\n/g, "<br>");
    } else if (i % 3 === 2) html += `<pre><code>${escapeHtml(parts[i])}</code></pre>`;
  }
  return html;
}
function setStatus(text, cls = "") { const el = $("status"); el.textContent = text; el.className = "status " + cls; }
function toast(text, level = "info", ms = 4500) {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
  return el;
}
const downloads = {};
function downloadToast(label, progress) {
  let el = downloads[label];
  if (!el) {
    el = downloads[label] = document.createElement("div");
    el.className = "toast";
    el.innerHTML = `<span></span><div class="bar"><i></i></div>`;
    $("toasts").appendChild(el);
  }
  el.querySelector("span").textContent = `Downloading ${label}… ${Math.round(progress * 100)}%`;
  el.querySelector("i").style.width = `${progress * 100}%`;
  if (progress >= 1) { setTimeout(() => el.remove(), 1200); delete downloads[label]; }
}
function showReply(show) {
  $("reply").classList.toggle("show", show);
  clearTimeout(replyTimer);
  if (show && !busy) replyTimer = setTimeout(() => $("reply").classList.remove("show"), 9000 + replyText.length * 45);
}
function setBusy(b) {
  busy = b;
  $("btnStop").classList.toggle("hidden", !b);
  $("btnSend").classList.toggle("hidden", b);
  if (!b) showReply(!!replyText);
}

// ------------------------------------------------------------------ activity panel
const activity = new Map();
function activityStart(ev) {
  const li = document.createElement("li");
  li.className = "run" + (ev.fast ? " fast" : "");
  const args = Object.values(ev.args || {}).map((v) => (typeof v === "string" ? v : JSON.stringify(v))).join(", ");
  li.innerHTML = `<span class="name"></span><span class="detail"></span><span class="ms"></span>`;
  li.querySelector(".name").textContent = ev.name;
  li.querySelector(".detail").textContent = args;
  $("activity").prepend(li);
  activity.set(ev.id, li);
  while ($("activity").children.length > 6) $("activity").lastChild.remove();
}
function activityEnd(ev) {
  const li = activity.get(ev.id);
  if (!li) return;
  li.className = (ev.ok ? "ok" : "fail") + (li.classList.contains("fast") ? " fast" : "");
  li.querySelector(".detail").textContent = ev.summary || "";
  li.querySelector(".ms").textContent = ev.ms != null ? `${ev.ms} ms` : "";
  if (ev.verified === false) li.querySelector(".detail").textContent = "⚠ not verified · " + (ev.summary || "");
  setTimeout(() => { li.style.opacity = "0"; li.style.transition = "opacity .6s"; setTimeout(() => li.remove(), 700); }, 7000);
}

// ------------------------------------------------------------------ tool calls in the transcript
// A slim row above each reply: one chip per tool call - a status dot, the tool, how long it took. Hover for the
// arguments and what came back. Live while Jarvis works (pulsing dot), saved with the reply for the History panel.
const toolLabel = (n) => n.replace(/^habit_/, "habit · ").replace(/_/g, " ");
function toolChip(t) {
  const c = document.createElement("span");
  c.className = `tc ${t.ok === undefined ? "run" : t.ok ? "ok" : "fail"}`;
  c.innerHTML = `<i></i><b></b><small></small>`;
  c.querySelector("b").textContent = toolLabel(t.name);
  toolChipSet(c, t);
  return c;
}
function toolChipSet(c, t) {
  if (t.ok !== undefined) c.className = `tc ${t.ok ? "ok" : "fail"}`;
  c.querySelector("small").textContent = t.ms != null && t.ok !== undefined ? (t.ms >= 1000 ? `${(t.ms / 1000).toFixed(1)}s` : `${t.ms}ms`) : "";
  c.title = [t.args, t.summary && `→ ${t.summary}`].filter(Boolean).join("\n");
}
function toolRow(tools) {
  const row = document.createElement("div");
  row.className = "tools";
  for (const t of tools || []) row.append(toolChip(t));
  return row;
}
// one live turn shown in both the conversation panel and the History panel
const liveTurn = { chips: new Map(), args: new Map() };
function turnToolStart(ev) {
  const t = { name: ev.name, args: Object.entries(ev.args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ") };
  liveTurn.args.set(ev.id, t.args);
  const chips = [];
  for (const host of [convo.toolHost(), historyLive.toolHost()]) {
    if (!host) continue;
    const c = toolChip(t); host.append(c); chips.push(c);
  }
  liveTurn.chips.set(ev.id, chips);
}
function turnToolEnd(ev) {
  for (const c of liveTurn.chips.get(ev.id) || []) toolChipSet(c, { ...ev, args: liveTurn.args.get(ev.id) });
  liveTurn.chips.delete(ev.id);
}

// ------------------------------------------------------------------ websocket
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => setStatus("");
  ws.onclose = () => { setStatus("Reconnecting…", "warn"); setTimeout(connect, 1000); };
  ws.onmessage = (m) => handle(JSON.parse(m.data));
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function handle(ev) {
  switch (ev.type) {
    case "hello":
      voiceStatus = ev.voice_status;
      updateMic();
      if (!ev.model || (!ev.key_set && !ev.local)) openSettings(true);
      model.restore();
      break;
    case "model": model.show(ev); break;
    case "job": if (!$("history").classList.contains("hidden")) jobsview.render(); break;
    case "job_done":
      toast(`Background job finished: ${ev.task.slice(0, 80)}`, "info", 9000);
      convo.add("assistant", `**Background job finished** - ${ev.task}

${ev.result}`);
      if ("Notification" in window && Notification.permission === "granted") new Notification("Jarvis - job finished", { body: ev.task.slice(0, 120) });
      if (!$("history").classList.contains("hidden")) jobsview.render();
      break;
    case "media": {
      const md = `
![${ev.alt || "picture"}](${ev.url})
`;
      replyText += md; $("reply").innerHTML = renderMd(replyText); showReply(true); convo.stream(replyText);
      break;
    }
    case "study": studyview.open(ev.deck); break;
    case "proactive":
      // Jarvis speaking up on his own: show it like a reply, keep it in the conversation, notify if hidden
      replyText = ev.text; $("reply").innerHTML = renderMd(ev.text); showReply(true);
      convo.add("assistant", ev.text);
      if (document.hidden && "Notification" in window && Notification.permission === "granted") new Notification("Jarvis", { body: ev.text });
      break;
    case "camera": if (ev.action !== "watch_done") camview.open(ev.cam); break;
    case "map": { const { type, ...m } = ev; mapview.show({ ...m, units: settings && settings.nav_units, voice: settings ? settings.nav_voice : true }); break; }
    case "geo": mapview.geo(ev); break;
    case "ui_open": openPanel(ev.panel); break;
    case "settings_changed":
      api("/api/settings").then((d) => { settings = d.settings; if (!$("settings").classList.contains("hidden")) openSettings(); }).catch(() => {});
      break;
    case "camera_frame": camview.frame(ev); break;
    case "orb":
      orb.setState(ev.state);
      $("btnMic").classList.toggle("live", ev.state === "listening");
      break;
    case "level":
      if ((ev.source === "mic" && orb.state === "listening") || ev.source === "tts") orb.setLevel(ev.value);
      break;
    case "user":
      replyText = ""; $("reply").innerHTML = ""; showReply(false); setBusy(true);
      convo.add("user", ev.text);
      historyLive.user(ev.text);
      if (ev.source === "voice") { $("transcript").textContent = `“${ev.text}”`; $("transcript").classList.add("show"); }
      break;
    case "transcript":
      if (ev.text) { $("transcript").textContent = `“${ev.text}”`; $("transcript").classList.add("show"); }
      break;
    case "ack":
      if (!replyText) { $("reply").innerHTML = `<span class="muted">${escapeHtml(ev.text)}</span>`; showReply(true); }
      break;
    case "delta":
      if (phone) phone.feed(ev.text);
      replyText += ev.text;
      convo.stream(replyText);
      historyLive.stream(replyText);
      $("reply").innerHTML = renderMd(replyText);
      $("reply").scrollTop = $("reply").scrollHeight;
      showReply(true);
      break;
    case "delta_break":
      replyText += "\n";
      break;
    case "reply":
      if (ev.text && !replyText.trim()) { replyText = ev.text; $("reply").innerHTML = renderMd(replyText); convo.stream(replyText); }
      historyLive.stream(replyText);
      break;
    case "done":
      setBusy(false);
      convo.end();
      historyLive.end();
      if (phone) phone.finish();
      setTimeout(() => $("transcript").classList.remove("show"), 2500);
      break;
    case "cancelled":
      setBusy(false);
      break;
    case "tool_start": activityStart(ev); turnToolStart(ev); break;
    case "tool_end": activityEnd(ev); turnToolEnd(ev); break;
    case "plan": toast(`${ev.title}: ${ev.steps.length} steps`); break;
    case "confirm": openConfirm(ev); break;
    case "confirm_closed": if ($("confirm").dataset.id === ev.id) $("confirm").classList.add("hidden"); break;
    case "notice": toast(ev.text, ev.level || "info", ev.level === "error" ? 8000 : 4500); break;
    case "alert":
      toast(`⏰ ${ev.text}`, "warn", 15000);
      if ("Notification" in window && Notification.permission === "granted") new Notification("Jarvis", { body: ev.text });
      break;
    case "download": downloadToast(ev.label, ev.progress); break;
    case "voice_status":
      voiceStatus = ev.status;
      updateMic();
      if (ev.status === "loading") setStatus("Loading voice models…");
      else if (ev.status === "ready") setStatus(`Voice ready · ${ev.stt || ""} on ${ev.stt_device || ""}`);
      else if (ev.status === "error") { setStatus("Voice unavailable", "err"); toast(`Voice: ${ev.error}`, "error", 10000); }
      else if (ev.status === "unavailable") setStatus("Voice packages not installed", "warn");
      setTimeout(() => { if ($("status").textContent.startsWith("Voice ready")) setStatus(""); }, 5000);
      break;
    case "listening":
      $("btnMic").classList.toggle("live", ev.on);
      break;
    case "barge_in": orb.setLevel(0); break;
  }
}

function updateMic() {
  const ready = voiceStatus === "ready";
  $("btnMic").classList.toggle("off", !ready);
  $("btnMic").title = ready ? (REMOTE ? "Tap to talk" : "Talk (or say “Hey Jarvis”)") : `Voice: ${voiceStatus}`;
}

// ------------------------------------------------------------------ prompt + mic
$("prompt").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  promptHistory.unshift(text); histIdx = -1;
  if (phone) phone.typed();
  send({ type: "ask", text, source: phone ? "phone" : "text" });
  $("input").value = "";
});
const promptHistory = [];
let histIdx = -1;
$("input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowUp" && promptHistory.length) { histIdx = Math.min(promptHistory.length - 1, histIdx + 1); $("input").value = promptHistory[histIdx]; e.preventDefault(); }
  if (e.key === "ArrowDown") { histIdx = Math.max(-1, histIdx - 1); $("input").value = histIdx >= 0 ? promptHistory[histIdx] : ""; e.preventDefault(); }
});
$("input").addEventListener("input", () => { if ($("input").value.length === 1) send({ type: "interrupt" }); });
$("btnStop").onclick = () => { if (phone) phone.stop(); send({ type: "cancel" }); };
const talk = () => (phone ? phone.listen() : send({ type: "ptt" }));
$("btnMic").onclick = talk;
$("orb").addEventListener("click", () => { if (!orb.consumeDrag()) talk(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("confirm").classList.contains("hidden")) return answerConfirm(false);
    if (!$("settings").classList.contains("hidden")) return closeDrawer("settings");
    if (!$("history").classList.contains("hidden")) return closeDrawer("history");
    send({ type: "cancel" });
  }
  if (e.key === "/" && document.activeElement !== $("input") && !e.target.closest("input,textarea,select")) {
    e.preventDefault(); $("input").focus();
  }
});
window.jarvis = { pushToTalk: () => send({ type: "ptt" }) };

// ------------------------------------------------------------------ confirmation
function openConfirm(ev) {
  $("confirm").dataset.id = ev.id;
  $("confirmText").textContent = ev.summary;
  $("confirmArgs").textContent = JSON.stringify(ev.args, null, 2);
  $("confirm").classList.remove("hidden");
  $("confirmYes").focus();
}
function answerConfirm(approved) {
  send({ type: "confirm", id: $("confirm").dataset.id, approved });
  $("confirm").classList.add("hidden");
}
$("confirmYes").onclick = () => answerConfirm(true);
$("confirmNo").onclick = () => answerConfirm(false);

// ------------------------------------------------------------------ drawers
function closeDrawer(id) { $(id).classList.add("hidden"); }
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => closeDrawer(b.dataset.close)));
$("btnSettings").onclick = () => ($("settings").classList.contains("hidden") ? openSettings() : closeDrawer("settings"));
$("btnHistory").onclick = async () => {
  if (!$("history").classList.contains("hidden")) return closeDrawer("history");
  const { messages } = await api("/api/history?limit=100");
  jobsview.render();
  const list = $("historyList");
  list.replaceChildren();
  for (const m of messages) list.append(historyMsg(m.role, m.content, m.ts, (m.meta || {}).tools));
  if (!messages.length) list.innerHTML = `<p class="muted">No conversations yet.</p>`;
  $("history").classList.remove("hidden");
  list.scrollTop = list.scrollHeight;
};
function historyMsg(role, text, ts, tools) {
  const d = document.createElement("div");
  d.className = `msg ${role}`;
  const t = document.createElement("time");
  t.textContent = new Date((ts || Date.now() / 1000) * 1000).toLocaleString();
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text || "";
  d.append(t);
  if (role === "assistant") d.append(toolRow(tools));
  d.append(body);
  return d;
}
const historyLive = (() => {
  let cur = null;
  const open = () => !$("history").classList.contains("hidden");
  const list = () => $("historyList");
  function ensure() {
    if (!cur && open()) { list().querySelector("p.muted")?.remove(); cur = historyMsg("assistant", "", null, []); list().append(cur); }
    return cur;
  }
  const scroll = () => { const l = list(); l.scrollTop = l.scrollHeight; };
  return {
    user(text) { if (!open()) return; cur = null; list().querySelector("p.muted")?.remove(); list().append(historyMsg("user", text)); scroll(); },
    toolHost() { const c = ensure(); if (c) scroll(); return c && c.querySelector(".tools"); },
    stream(text) { const c = ensure(); if (c) { c.querySelector(".body").textContent = text; scroll(); } },
    end() { cur = null; },
  };
})();
$("btnClearHistory").onclick = async () => {
  if (!confirm("Clear all conversation history?")) return;
  await api("/api/history", { method: "DELETE" });
  $("historyList").innerHTML = `<p class="muted">History cleared.</p>`;
};

// ------------------------------------------------------------------ settings
let allModels = [];
async function openSettings(onboarding = false) {
  const data = await api("/api/settings");
  settings = data.settings;
  fillSettings(data);
  $("onboarding").classList.toggle("hidden", !(onboarding || !data.key_set));
  $("settings").classList.remove("hidden");
  loadTools();
  loadCameras();
  loadUndo();
  loadDevices();
  if (data.key_set || settings.provider === "ollama") loadModels(false);
}

function fillSettings(data) {
  const s = data.settings;
  const prov = $("sProvider");
  prov.replaceChildren(...Object.entries(data.presets).map(([k, v]) => new Option(v.label, k)));
  prov.value = s.provider;
  $("sBaseUrl").value = s.base_url;
  $("sKey").value = "";
  $("sKey").placeholder = data.presets[s.provider]?.key_hint || "";
  $("keyState").textContent = data.key_set ? `· saved (${data.key_masked}) in your OS keychain` : "· not set";
  if (s.model && ![...$("sModel").options].some((o) => o.value === s.model)) $("sModel").add(new Option(s.model, s.model));
  $("sModel").value = s.model;
  $("sTemp").value = s.temperature; $("tempOut").textContent = s.temperature;
  $("sVoice").checked = s.voice_enabled;
  $("sWake").checked = s.wake_word;
  $("sEngine").value = s.voice_engine;
  const vp = data.voice_profile, hw = data.hardware;
  $("voiceInfo").textContent = `${hw.gpu_name ? `GPU: ${hw.gpu_name} (${hw.vram_gb} GB)` : "No NVIDIA GPU detected"}${hw.cuda ? " · CUDA ready" : ""}` +
    ` → speech: Whisper ${vp.stt_model} on ${vp.stt_device}, voice: Kokoro` +
    (data.voice.status !== "off" ? ` · status: ${data.voice.status}${data.voice.error ? " — " + data.voice.error : ""}` : "");
  const voices = data.voice.voices.length ? data.voice.voices : [s.tts_voice];
  $("sTtsVoice").replaceChildren(...voices.map((v) => new Option(voiceLabel(v), v)));
  $("sTtsVoice").value = s.tts_voice;
  $("sSpeed").value = s.tts_speed; $("speedOut").textContent = `${s.tts_speed}×`;
  $("sHotkey").value = s.hotkey;
  $("sFast").checked = s.fast_path;
  $("sShell").checked = s.shell_enabled;
  $("sName").value = s.user_name;
  $("sProactive").checked = s.proactive;
  $("sNavVoice").checked = s.nav_voice; $("sNavUnits").value = s.nav_units || "imperial";
  loadLocation();
  $("sHeartbeat").value = String(s.heartbeat_min || 0);
  $("sQuietStart").value = s.quiet_start; $("sQuietEnd").value = s.quiet_end;
  $("sBriefing").value = s.briefing_time || ""; $("sDiary").value = s.diary_time || "";
  loadProactive();
  $("sVault").value = s.vault_path || "";
  $("sKeeper").checked = s.memory_keeper;
  $("sLearn").checked = s.learn;
  loadVault();
  $("sPersonality").value = s.personality;
  $("about").textContent = `Jarvis v${data.version} · MIT · github.com/yash2031233/jarvis-ai-api`;
}
function voiceLabel(v) {
  if (v.startsWith("pocket:")) {
    const n = v.slice(7).split("_")[0];
    return `${n[0].toUpperCase() + n.slice(1)} — Pocket (British, streaming)`;
  }
  const accent = { a: "American", b: "British" }[v[0]] || "";
  const sex = { f: "female", m: "male" }[v[1]] || "";
  const name = v.split("_")[1] || v;
  return `${name[0].toUpperCase() + name.slice(1)} — ${accent} ${sex}`.trim();
}

async function save(changes) {
  try {
    const data = await api("/api/settings", { method: "POST", body: changes });
    settings = data.settings;
    return data;
  } catch (e) { toast(`Couldn't save: ${e.message}`, "error"); }
}

$("sProvider").onchange = async () => {
  const data = await save({ provider: $("sProvider").value });
  if (data) { fillSettings(data); allModels = []; $("sModel").replaceChildren(new Option("— load models —", "")); }
};
$("sBaseUrl").onchange = () => save({ base_url: $("sBaseUrl").value.trim() });
$("btnSaveKey").onclick = async () => {
  const key = $("sKey").value.trim();
  try {
    const r = await api("/api/key", { method: "POST", body: { key } });
    $("keyState").textContent = r.key_set ? `· saved (${r.key_masked}) in your OS keychain` : "· not set";
    $("sKey").value = "";
    toast(r.key_set ? "API key saved securely." : "API key removed.");
    if (r.key_set) { await testConnection(); await loadModels(true); }
  } catch (e) { toast(e.message, "error"); }
};
$("sKey").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnSaveKey").click(); });
async function testConnection() {
  $("connResult").textContent = "Testing…";
  const r = await api("/api/test-connection", { method: "POST" });
  $("connResult").textContent = r.ok ? `✓ Connected · ${r.models} models · ${r.latency_ms} ms` : `✗ ${r.error}`;
  $("connResult").className = r.ok ? "" : "risk-high";
  return r.ok;
}
$("btnTestConn").onclick = testConnection;

async function loadModels(pickDefault) {
  $("btnLoadModels").disabled = true;
  try {
    const r = await api("/api/models");
    if (r.error) { toast(r.error, "error"); return; }
    allModels = r.models;
    renderModels();
    if (pickDefault && !settings.model && allModels.length) {
      const pref = ["moonshotai/kimi-k2-instruct", "meta/llama-3.3-70b-instruct", "qwen/qwen2.5-72b-instruct",
        "meta/llama-3.1-70b-instruct", "mistralai/mistral-large"];
      const choice = pref.find((m) => allModels.includes(m)) || allModels.find((m) => /instruct|chat/i.test(m)) || allModels[0];
      $("sModel").value = choice;
      await save({ model: choice });
      toast(`Model set to ${choice}. You can change it any time.`);
      testModel();
    }
  } finally { $("btnLoadModels").disabled = false; }
}
function renderModels() {
  const f = $("sModelFilter").value.toLowerCase();
  const list = allModels.filter((m) => !f || m.toLowerCase().includes(f));
  const sel = $("sModel");
  sel.replaceChildren(...list.map((m) => new Option(m, m)));
  if (settings.model && !list.includes(settings.model)) sel.prepend(new Option(settings.model, settings.model));
  sel.value = settings.model || list[0] || "";
}
$("sModelFilter").oninput = renderModels;
$("btnLoadModels").onclick = () => loadModels(false);
$("sModel").onchange = async () => { await save({ model: $("sModel").value }); $("modelResult").textContent = ""; testModel(); };
async function testModel() {
  const model = $("sModel").value;
  if (!model) return;
  $("modelResult").textContent = "Testing…";
  const r = await api("/api/test-model", { method: "POST", body: { model } });
  if (r.error) { $("modelResult").textContent = `✗ ${r.error}`; return; }
  $("modelResult").textContent = r.native ? `✓ Native tool calling · ${r.latency_ms} ms`
    : r.works ? "✓ Works via text tool-calling fallback" : "⚠ This model may struggle with tools";
}
$("btnTestModel").onclick = testModel;
$("sTemp").oninput = () => ($("tempOut").textContent = $("sTemp").value);
$("sTemp").onchange = () => save({ temperature: +$("sTemp").value });
$("sVoice").onchange = () => save({ voice_enabled: $("sVoice").checked });
$("sWake").onchange = () => { save({ wake_word: $("sWake").checked }); toast("Restart Jarvis to apply wake-word changes."); };
$("sEngine").onchange = () => { save({ voice_engine: $("sEngine").value }); toast("Restart Jarvis to switch the voice engine."); };
$("sTtsVoice").onchange = () => save({ tts_voice: $("sTtsVoice").value });
$("sSpeed").oninput = () => ($("speedOut").textContent = `${$("sSpeed").value}×`);
$("sSpeed").onchange = () => save({ tts_speed: +$("sSpeed").value });
$("sMic").onchange = () => { save({ mic_device: $("sMic").value === "" ? null : +$("sMic").value }); toast("Restart Jarvis to switch microphones."); };
$("sHotkey").onchange = () => { save({ hotkey: $("sHotkey").value.trim() }); toast("Restart Jarvis to apply the new hotkey."); };
$("sFast").onchange = () => save({ fast_path: $("sFast").checked });
$("sShell").onchange = () => save({ shell_enabled: $("sShell").checked });
$("sName").onchange = () => save({ user_name: $("sName").value.trim() });
$("sPersonality").onchange = () => save({ personality: $("sPersonality").value });

async function loadTools() {
  const { tools } = await api("/api/tools");
  const tb = $("permTable");
  tb.replaceChildren();
  for (const t of tools) {
    const tr = document.createElement("tr");
    const sel = document.createElement("select");
    for (const v of ["auto", "ask", "off"]) sel.add(new Option(v, v));
    sel.value = t.permission;
    sel.setAttribute("aria-label", `Permission for ${t.name}`);
    sel.onchange = async () => {
      const perms = { ...(settings.tool_permissions || {}), [t.name]: sel.value };
      await save({ tool_permissions: perms });
    };
    const name = document.createElement("td"); name.textContent = t.name; name.title = t.description;
    const risk = document.createElement("td"); risk.textContent = t.risk; risk.className = `risk-${t.risk}`;
    const ctl = document.createElement("td"); ctl.append(sel);
    tr.append(name, risk, ctl);
    tb.append(tr);
  }
}
async function loadUndo() {
  const { entries } = await api("/api/undo");
  $("undoList").replaceChildren(...entries.map((e) => {
    const li = document.createElement("li");
    li.textContent = `${e.undone ? "↶ " : ""}${e.what}`;
    return li;
  }));
  if (!entries.length) $("undoList").innerHTML = "<li>No changes yet.</li>";
}
$("btnUndo").onclick = async () => {
  const r = await api("/api/undo", { method: "POST" });
  toast(r.undone.length ? `Undid: ${r.undone.join(", ")}` : "Nothing to undo.");
  loadUndo();
};
async function loadDevices() {
  const { inputs } = await api("/api/devices");
  const sel = $("sMic");
  sel.replaceChildren(new Option("Default / auto", ""), ...inputs.map((d) => new Option(d.name, d.index)));
  sel.value = settings.mic_device ?? "";
}

// ------------------------------------------------------------------ window controls (pywebview)
window.addEventListener("pywebviewready", () => {
  document.body.classList.add("desktop");
  $("btnMin").onclick = () => window.pywebview.api.minimize();
  $("btnClose").onclick = () => window.pywebview.api.close();
});
if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {});

// ------------------------------------------------------------------ conversation panel (beside the 3D model)
const convo = (() => {
  const log = $("convoLog");
  let cur = null;
  function add(role, text, tools) {
    const d = document.createElement("div");
    d.className = `msg ${role}`;
    if (role === "assistant") d.append(toolRow(tools));
    const body = document.createElement("div");
    body.className = "body";
    body.innerHTML = renderMd(text || "");
    d.append(body);
    log.append(d);
    while (log.children.length > 80) log.firstChild.remove();
    log.scrollTop = log.scrollHeight;
    return d;
  }
  return {
    add(role, text) { cur = null; add(role, text); },
    stream(text) { if (!cur) cur = add("assistant", ""); cur.querySelector(".body").innerHTML = renderMd(text); log.scrollTop = log.scrollHeight; },
    toolHost() { if (!cur) cur = add("assistant", ""); return cur.querySelector(".tools"); },
    end() { cur = null; },
    async prefill() {
      if (log.children.length) return;
      try {
        const { messages } = await api("/api/history?limit=20");
        for (const m of messages) add(m.role === "user" ? "user" : "assistant", m.content || "", (m.meta || {}).tools);
      } catch { /* history is optional */ }
    },
  };
})();

// ------------------------------------------------------------------ 3D model mode (print3d)
// The print3d tool tells every screen when a part is being designed, is ready, or failed. The part takes the
// middle, the orb shrinks into the corner, the conversation runs beside it (body[data-mode="model"] in the CSS).
const model = (() => {
  const host = $("mv3d"), status = $("mvStatus"), nameEl = $("mvName"), sizeEl = $("mvSize");
  const verSel = $("mvVersion"), issuesEl = $("mvIssues");
  let view = null, loading = null, shownUrl = null, shownPart = null, current = null;
  let bed = [220, 220, 220];

  async function ensureView() {
    if (view) return view;
    if (!loading) {
      loading = import("/ui/model.js").then(({ ModelView }) => {
        view = new ModelView(host, { token: TOKEN, bed });
        return view;
      });
    }
    return loading;
  }
  function enter() {
    if (document.body.dataset.mode === "camera") camview.close();
    if (document.body.dataset.mode === "study") studyview.close();
    if (document.body.dataset.mode === "map") mapview.close();
    if (document.body.dataset.mode !== "model") { document.body.dataset.mode = "model"; convo.prefill(); }
  }
  function exit() {
    if (document.body.dataset.mode === "model") delete document.body.dataset.mode;
    api("/api/cad/close", { method: "POST" }).catch(() => {});
  }
  function setStatus(kind, title, detail) {
    status.className = `mvStatus ${kind || ""}`;
    status.innerHTML = kind ? `<div class="card"><div class="t">${escapeHtml(title)}</div>${detail ? `<div class="d">${escapeHtml(detail)}</div>` : ""}</div>` : "";
  }
  function showSize(x, y, z) {
    const big = x > bed[0] || y > bed[1] || z > bed[2];
    sizeEl.innerHTML = `${(+x).toFixed(1)} × ${(+y).toFixed(1)} × ${(+z).toFixed(1)} mm${big ? ' <span class="warn">· bigger than the bed</span>' : ""}`;
  }
  function showVersions(m) {
    const vs = m.versions || [];
    verSel.parentElement.classList.toggle("hidden", vs.length < 2);
    verSel.replaceChildren(...vs.slice().reverse().map((v) => new Option(`v${v}${v === vs[vs.length - 1] ? " (latest)" : ""}`, v)));
    if (m.version) verSel.value = m.version;
  }
  async function show(m) {
    enter();
    current = m;
    nameEl.textContent = m.name || (m.part || "").replace(/_/g, " ");
    if (m.size_mm && m.size_mm.length === 3) showSize(...m.size_mm);
    issuesEl.replaceChildren(...(m.issues || []).map((t) => Object.assign(document.createElement("li"), { textContent: t })));
    if (m.versions) showVersions(m);
    let v;
    try { v = await ensureView(); } catch (e) { setStatus("error", "3D viewer unavailable", String(e && e.message || e)); return; }
    if (m.status === "building") {
      v.dim(true);
      setStatus("building", shownUrl && shownPart === m.part ? "Updating the part" : "Designing", m.text || "");
      return;
    }
    if (m.status === "error") { v.dim(false); setStatus("error", "OpenSCAD couldn't build it", m.text || ""); return; }
    if (m.url && m.url !== shownUrl) {
      try {
        const size = await v.load(m.url, m.part === shownPart);   // same part = a tweak: keep the angle
        shownUrl = m.url; shownPart = m.part;
        showSize(size.x, size.y, size.z);                          // measured from the mesh itself
      } catch (e) { setStatus("error", "Couldn't load the model", String(e && e.message || e)); return; }
    }
    v.dim(false);
    setStatus("");
  }
  $("modelView").querySelector(".mvBar").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || !view) return;
    if (b.dataset.view) {
      view.setView(b.dataset.view);
      b.parentElement.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    }
    if (b.dataset.flag) b.classList.toggle("on", view.toggle(b.dataset.flag));
  });
  verSel.addEventListener("change", () => {
    if (current) api("/api/cad/show", { method: "POST", body: { part: current.part, version: +verSel.value } }).catch((e) => toast(e.message, "error"));
  });
  $("mvClose").addEventListener("click", exit);
  return {
    show, exit,
    async restore() {
      try {
        const s = await api("/api/cad/state");
        if (s.open && (s.url || s.status === "building")) show(s);
      } catch { /* nothing to restore */ }
    },
  };
})();

// ------------------------------------------------------------------ camera mode
// Opens by itself when Jarvis uses a camera (look / watch / read...), or from the camera button.
// Live feed in the middle; every frame he looked at, with what he made of it, down the side.
const camview = (() => {
  const live = $("camLive"), pick = $("camPick"), feed = $("camFeed"), msg = $("camMsg");
  let current = null;
  async function fillPicker(sel) {
    const { cameras } = await api("/api/camera/list");
    pick.replaceChildren(...cameras.map((c) => new Option(c.name + (c.kind === "ip" ? " (IP)" : ""), c.id)));
    const id = sel || (cameras.find((c) => c.default) || cameras[0] || {}).id;
    if (id) pick.value = id;
    return id;
  }
  function start(id) {
    current = id;
    msg.textContent = "Connecting…";
    live.onload = () => { msg.textContent = ""; };
    live.onerror = () => { msg.textContent = "No picture from this camera. Check it in Settings → Cameras."; };
    live.src = `/api/camera/${encodeURIComponent(id)}/stream?token=${encodeURIComponent(TOKEN)}&t=${Date.now()}`;
  }
  async function open(id) {
    if (document.body.dataset.mode === "model") model.exit();
    if (document.body.dataset.mode === "study") studyview.close();
    if (document.body.dataset.mode === "map") mapview.close();
    document.body.dataset.mode = "camera";
    const cid = await fillPicker(id || current).catch(() => id);
    if (cid && (cid !== current || !live.src)) start(cid);
  }
  function close() {
    if (document.body.dataset.mode === "camera") delete document.body.dataset.mode;
    live.removeAttribute("src");   // stops the stream; the camera is released after ~20 s
    current = null;
  }
  pick.addEventListener("change", () => start(pick.value));
  $("camClose").addEventListener("click", close);
  $("btnCamera").addEventListener("click", () => (document.body.dataset.mode === "camera" ? close() : open()));
  return {
    open, close,
    frame(ev) {
      const li = document.createElement("li");
      const img = document.createElement("img");
      img.src = `${ev.img}?token=${encodeURIComponent(TOKEN)}`;
      img.alt = ev.text || "camera frame";
      const p = document.createElement("p");
      p.textContent = ev.text || "";
      const t = document.createElement("time");
      t.textContent = `${ev.name} · ${new Date(ev.t * 1000).toLocaleTimeString()}`;
      li.append(img, p, t);
      feed.prepend(li);
      while (feed.children.length > 30) feed.lastChild.remove();
    },
  };
})();

// ------------------------------------------------------------------ study mode
const studyview = (() => {
  const decksEl = $("studyDecks"), card = $("studyCard"), title = $("studyTitle"), meta = $("studyMeta");
  let deck = null, queue = [], cur = null;
  async function open(name) {
    if (document.body.dataset.mode === "model") model.exit();
    if (document.body.dataset.mode === "camera") camview.close();
    if (document.body.dataset.mode === "map") mapview.close();
    document.body.dataset.mode = "study";
    if (name) return practise(name);
    showDecks();
  }
  function close() { if (document.body.dataset.mode === "study") delete document.body.dataset.mode; }
  async function showDecks() {
    deck = null;
    card.classList.add("hidden"); decksEl.classList.remove("hidden"); $("studyBack").classList.add("hidden");
    title.textContent = "Study"; meta.textContent = "";
    const { decks } = await api("/api/study");
    decksEl.replaceChildren(...(decks.length ? decks.map((d) => {
      const li = document.createElement("li");
      li.innerHTML = `<b>${escapeHtml(d.deck)}</b><span class="${d.due ? "due" : "muted"}">${d.due} due</span> · ${d.cards} cards · ${d.mastered} mastered`;
      li.onclick = () => practise(d.deck);
      return li;
    }) : [Object.assign(document.createElement("li"), { className: "empty",
      textContent: "No decks yet. Ask Jarvis: “make me flashcards on …” (a topic, your notes, a file or a photo)." })]));
  }
  async function practise(name) {
    const d = await api(`/api/study/deck/${encodeURIComponent(name)}`);
    deck = d.deck;
    queue = d.due.map((id) => d.cards.find((c) => c.id === id));
    if (!queue.length) queue = d.cards.slice().sort((a, b) => (a.box || 1) - (b.box || 1)).slice(0, 10);  // nothing due: free practice
    decksEl.classList.add("hidden"); card.classList.remove("hidden"); $("studyBack").classList.remove("hidden");
    title.textContent = deck;
    next();
  }
  function next() {
    cur = queue.shift();
    ["scFlip", "scAnswer", "scGrade", "scNext"].forEach((id) => $(id).classList.add("hidden"));
    $("scChoices").replaceChildren();
    if (!cur) { $("scQ").textContent = "All done for now. Missed cards come back sooner."; meta.textContent = ""; return; }
    meta.textContent = `${queue.length + 1} left · box ${cur.box || 1}`;
    $("scQ").textContent = cur.q;
    const choices = (cur.choices || []).slice().sort(() => Math.random() - 0.5);
    if (choices.length >= 2) {
      for (const ch of choices) {
        const b = Object.assign(document.createElement("button"), { type: "button", textContent: ch });
        b.onclick = () => answer(ch === cur.a, b);
        $("scChoices").append(b);
      }
    } else $("scFlip").classList.remove("hidden");
  }
  function reveal() {
    $("scAnswer").innerHTML = `<b>${escapeHtml(cur.a)}</b>${cur.why ? `<p>${escapeHtml(cur.why)}</p>` : ""}`;
    $("scAnswer").classList.remove("hidden");
  }
  async function answer(right, btn) {
    $("scChoices").querySelectorAll("button").forEach((b) => { b.disabled = true; if (b.textContent === cur.a) b.classList.add("right"); });
    if (btn && !right) btn.classList.add("wrong");
    reveal();
    await grade(right);
    $("scNext").classList.remove("hidden");
  }
  async function grade(right) {
    try { await api("/api/study/grade", { method: "POST", body: { deck, card_id: cur.id, right } }); }
    catch (e) { toast(e.message, "error"); }
    if (!right) queue.push(cur);  // see it again this session
  }
  $("scShow").onclick = () => { $("scFlip").classList.add("hidden"); reveal(); $("scGrade").classList.remove("hidden"); };
  $("scGot").onclick = async () => { await grade(true); next(); };
  $("scMissed").onclick = async () => { await grade(false); next(); };
  $("scNextBtn").onclick = next;
  $("studyBack").onclick = showDecks;
  $("studyClose").onclick = close;
  $("btnStudy").onclick = () => (document.body.dataset.mode === "study" ? close() : open());
  return { open, close, refresh() { if (document.body.dataset.mode === "study" && !deck) showDecks(); } };
})();

// ------------------------------------------------------------------ background jobs (History drawer)
const jobsview = (() => {
  const list = $("jobsList");
  async function render() {
    let jobs = [];
    try { ({ jobs } = await api("/api/jobs")); } catch { return; }
    const live = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
    $("jobsCount").textContent = jobs.length ? `· ${live ? `${live} running` : `${jobs.length}`}` : "";
    list.replaceChildren(...(jobs.length ? jobs.slice(0, 15).map((j) => {
      const li = document.createElement("li");
      const tools = [...new Set(j.tools || [])].slice(-4).join(", ");
      li.innerHTML = `<div class="top"><span class="task"></span><span class="st ${j.status}">${j.status}</span></div>
        <div class="sub">${j.seconds != null ? `${j.seconds}s` : ""}${tools ? ` · ${escapeHtml(tools)}` : ""}${j.error ? ` · ${escapeHtml(j.error)}` : ""}</div>`;
      li.querySelector(".task").textContent = j.task;
      li.title = j.task;
      if (j.status === "running" || j.status === "queued") {
        const b = Object.assign(document.createElement("button"), { className: "btn subtle", type: "button", textContent: "Cancel" });
        b.onclick = async (e) => { e.stopPropagation(); await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); render(); };
        li.querySelector(".top").append(b);
      }
      li.onclick = async () => {
        const open = li.querySelector(".result");
        if (open) return open.remove();
        if (j.status !== "done") return;
        const full = await api(`/api/jobs/${j.id}`);
        const div = document.createElement("div");
        div.className = "result"; div.innerHTML = renderMd(full.result || "");
        li.append(div);
      };
      return li;
    }) : [Object.assign(document.createElement("li"), { className: "empty",
      textContent: "No background jobs. Ask Jarvis to do something big “in the background”." })]));
  }
  return { render };
})();

// ------------------------------------------------------------------ settings: speaking up
async function loadProactive() {
  try {
    const p = await api("/api/proactive");
    const q = p.quiet_until * 1000 > Date.now() ? `Quiet until ${new Date(p.quiet_until * 1000).toLocaleTimeString()} · ` : "";
    $("proactiveInfo").textContent = `${q}${p.engagement.engaged} welcomed, ${p.engagement.ignored} ignored (2 weeks)`;
  } catch { /* optional */ }
}
$("sProactive").addEventListener("change", () => save({ proactive: $("sProactive").checked }));

// ------------------------------------------------------------------ Settings → Location (Telegram link, maps key)
async function loadLocation() {
  try {
    const t = await api("/api/telegram");
    const m = await api("/api/maps-key");
    $("tgToken").placeholder = t.token_set ? "•••••••• (saved)" : "123456:ABC-…";
    $("gmKey").placeholder = m.set ? "•••••••• (saved)" : "AIza…";
    let st = !t.token_set ? "No bot yet." : t.error ? `⚠ ${t.error}` : t.bot ? `${t.bot} connected` : "Connecting…";
    if (t.token_set && t.paired) st += " · paired ✓" + (t.last_fix ? ` · last location ${new Date(t.last_fix * 1000).toLocaleTimeString()}` : "");
    if (t.pair_code) st += ` · send the bot: ${t.pair_code}`;
    else if (t.token_set && !t.paired) st += " · not paired - press Pair";
    $("tgStatus").textContent = st;
    $("btnTgPair").textContent = t.paired ? "Re-pair" : "Pair";
    $("btnTgPair").disabled = !t.token_set;
  } catch { /* optional */ }
}
// an empty box means "nothing typed", not "delete": the saved token / pairing stay unless you confirm removing them
$("btnTgSave").addEventListener("click", async () => {
  const tok = $("tgToken").value.trim();
  if (!tok) {
    const t = await api("/api/telegram");
    if (!t.token_set) return toast("Paste the token from @BotFather first.");
    if (!confirm("Remove the saved Telegram bot (and its pairing)?")) return;
  } else if (!/^\d+:[\w-]{20,}$/.test(tok)) {
    return toast("That doesn't look like a bot token (it looks like 123456789:ABC…).", "warn");
  }
  const t = await api("/api/telegram", { method: "POST", body: { token: tok } });
  $("tgToken").value = "";
  if (tok) toast(t.error ? `Telegram: ${t.error}` : `Saved ✓ ${t.bot || ""} - now press Pair.`, t.error ? "error" : "info", 7000);
  loadLocation();
});
$("btnTgPair").addEventListener("click", async () => {
  const t = await api("/api/telegram", { method: "POST", body: { pair: true } });
  if (t.pair_code) toast(`Send ${t.pair_code} to ${t.bot || "your bot"} on Telegram (valid 10 minutes).`, "info", 12000);
  loadLocation();
  const poll = setInterval(async () => { await loadLocation(); if (!$("tgStatus").textContent.includes("send the bot")) clearInterval(poll); }, 3000);
  setTimeout(() => clearInterval(poll), 600000);
});
$("btnGmSave").addEventListener("click", async () => {
  const key = $("gmKey").value.trim();
  if (!key) {
    const m = await api("/api/maps-key");
    if (!m.set) return toast("Paste a Google Maps API key first.");
    if (!confirm("Remove the saved Google Maps key (routes go back to OpenStreetMap)?")) return;
  }
  await api("/api/maps-key", { method: "POST", body: { key } });
  $("gmKey").value = ""; if (key) toast("Google Maps key saved ✓");
  loadLocation();
});
$("sNavVoice").addEventListener("change", () => save({ nav_voice: $("sNavVoice").checked }));
$("sNavUnits").addEventListener("change", () => save({ nav_units: $("sNavUnits").value }));

// ------------------------------------------------------------------ map mode (location tool, navigation)
const mapview = createMap({
  api,
  speak: phone ? (text, urgent) => phone.say(text, { urgent }) : null,   // on the phone: directions from its speaker
  onOpen() {
    if (phone) phone.gpsOn();
    if (document.body.dataset.mode === "model") model.exit();
    if (document.body.dataset.mode === "camera") camview.close();
    if (document.body.dataset.mode === "study") studyview.close();
    document.body.dataset.mode = "map";
  },
  onClose() { if (document.body.dataset.mode === "map") delete document.body.dataset.mode; },
});
$("btnMap").addEventListener("click", () => (mapview.isOpen ? mapview.close() : mapview.open()));

// ------------------------------------------------------------------ the show_panel tool: Jarvis opens a screen
function openPanel(p) {
  const mode = document.body.dataset.mode;
  const closeModes = () => {
    if (mode === "model") model.exit();
    if (mode === "camera") camview.close();
    if (mode === "study") studyview.close();
    if (mode === "map") mapview.close();
  };
  if (p === "close") { closeModes(); closeDrawer("history"); closeDrawer("settings"); return; }
  if (p === "history" || p === "jobs") { if ($("history").classList.contains("hidden")) $("btnHistory").click(); return; }
  if (p === "settings") { openSettings(); return; }
  if (p === "map") { if (!mapview.isOpen) mapview.open(); return; }
  if (p === "cameras") { if (mode !== "camera") camview.open(); return; }
  if (p === "study") { if (mode !== "study") $("btnStudy").click(); return; }
  if (p === "model") { closeModes(); model.restore(); }
}
$("sHeartbeat").addEventListener("change", () => save({ heartbeat_min: +$("sHeartbeat").value }));
$("sQuietStart").addEventListener("change", () => save({ quiet_start: $("sQuietStart").value || "22:30" }));
$("sQuietEnd").addEventListener("change", () => save({ quiet_end: $("sQuietEnd").value || "07:00" }));
$("sBriefing").addEventListener("change", () => save({ briefing_time: $("sBriefing").value || "" }));
$("sDiary").addEventListener("change", () => save({ diary_time: $("sDiary").value || "" }));
$("btnQuiet2h").addEventListener("click", async () => {
  await api("/api/proactive/pause", { method: "POST", body: { minutes: 120 } });
  toast("Jarvis will stay quiet for 2 hours."); loadProactive();
});

// ------------------------------------------------------------------ settings: memory vault
async function loadVault() {
  try {
    const v = await api("/api/vault");
    const facts = v.notes.reduce((a, n) => a + n.facts, 0);
    $("vaultInfo").textContent = `${v.path} · ${v.notes.length} notes · ${facts} facts` +
      (v.keeper.last_run ? ` · last filed ${new Date(v.keeper.last_run * 1000).toLocaleString()}` : "");
  } catch (e) { $("vaultInfo").textContent = e.message; }
}
$("sVault").addEventListener("change", async () => { await save({ vault_path: $("sVault").value.trim() }); loadVault(); });
$("sKeeper").addEventListener("change", () => save({ memory_keeper: $("sKeeper").checked }));
$("sLearn").addEventListener("change", () => save({ learn: $("sLearn").checked }));
$("btnVaultOpen").addEventListener("click", () => api("/api/vault/open", { method: "POST" }));
$("btnKeepNow").addEventListener("click", async () => {
  const b = $("btnKeepNow"); b.disabled = true; b.textContent = "Remembering…";
  try {
    const r = await api("/api/vault/keep-now", { method: "POST" });
    toast(r.filed.length ? `Remembered: ${r.filed.map((f) => f.fact).join("; ")}` : "Nothing new worth remembering.");
    loadVault();
  } catch (e) { toast(e.message, "error"); }
  finally { b.disabled = false; b.textContent = "Remember now"; }
});

// ------------------------------------------------------------------ settings: cameras
async function loadCameras() {
  const list = $("camList");
  try {
    const { cameras } = await api("/api/camera/list");
    list.replaceChildren(...cameras.map((c) => {
      const li = document.createElement("li");
      li.className = c.default ? "default" : "";
      const what = document.createElement("div");
      what.className = "what";
      what.innerHTML = `${escapeHtml(c.name)}${c.default ? ' <span class="muted small">· default</span>' : ""}<small>${
        c.kind === "ip" ? escapeHtml(c.address || "") : `Direct · device ${c.device}`}${c.auto ? " · auto-detected" : ""}</small>`;
      const test = Object.assign(document.createElement("button"), { className: "btn subtle", type: "button", textContent: "Test" });
      test.onclick = () => testCamera(c.id, test);
      li.append(what, test);
      if (!c.default) {
        const def = Object.assign(document.createElement("button"), { className: "btn subtle", type: "button", textContent: "Default" });
        def.onclick = async () => { await api("/api/camera/default", { method: "POST", body: { id: c.id } }); loadCameras(); };
        li.append(def);
      }
      {
        const rm = Object.assign(document.createElement("button"), { className: "btn subtle", type: "button", textContent: "Remove" });
        rm.onclick = async () => { await api("/api/camera/remove", { method: "POST", body: { id: c.id } }); loadCameras(); };
        li.append(rm);
      }
      return li;
    }));
  } catch (e) { list.innerHTML = `<li>${escapeHtml(e.message)}</li>`; }
}
async function testCamera(id, btn) {
  const img = $("camTestImg");
  btn.disabled = true; btn.textContent = "…";
  try {
    const r = await fetch(`/api/camera/${encodeURIComponent(id)}/snapshot?t=${Date.now()}`, { headers: { "x-jarvis-token": TOKEN } });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    img.src = URL.createObjectURL(await r.blob());
    img.classList.remove("hidden");
    toast("Camera works.");
  } catch (e) { toast(`Camera test failed: ${e.message}`, "error", 8000); img.classList.add("hidden"); }
  finally { btn.disabled = false; btn.textContent = "Test"; }
}
$("camKind").addEventListener("change", () => {
  const ip = $("camKind").value === "ip";
  $("camUrlRow").classList.toggle("hidden", !ip);
  $("camDevRow").classList.toggle("hidden", ip);
});
$("btnCamDetect").addEventListener("click", async () => {
  const b = $("btnCamDetect");
  b.disabled = true; b.textContent = "Detecting…";
  try {
    const { devices } = await api("/api/camera/devices");
    $("camDevice").replaceChildren(...(devices.length ? devices.map((d) => new Option(`${d.name}${d.in_use_by_jarvis ? " (in use)" : ""}`, d.device))
      : [new Option("No cameras found", "0")]));
    toast(devices.length ? `Found ${devices.length} camera(s).` : "No directly connected cameras found.");
  } finally { b.disabled = false; b.textContent = "Detect"; }
});
$("btnCamAdd").addEventListener("click", async () => {
  const kind = $("camKind").value;
  const body = { name: $("camName").value.trim() || (kind === "ip" ? "IP camera" : "Webcam"), kind,
    device: +$("camDevice").value, url: $("camUrl").value.trim() };
  $("camAddMsg").textContent = "Adding…";
  try {
    const c = await api("/api/camera/add", { method: "POST", body });
    $("camUrl").value = ""; $("camName").value = "";
    $("camAddMsg").textContent = "";
    await loadCameras();
    const btn = [...$("camList").querySelectorAll("li")].find((li) => li.textContent.includes(c.name))?.querySelector("button");
    if (btn) testCamera(c.id, btn);
  } catch (e) { $("camAddMsg").textContent = e.message; }
});

connect();
