// Jarvis UI: orb + prompt + voice + settings, talking to the local server.
import { Orb } from "/ui/orb3d.js";

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
      html += escapeHtml(parts[i]).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
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

// ------------------------------------------------------------------ websocket
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
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
      if (!ev.key_set || !ev.model) openSettings(true);
      break;
    case "orb":
      orb.setState(ev.state);
      $("btnMic").classList.toggle("live", ev.state === "listening");
      break;
    case "level":
      if ((ev.source === "mic" && orb.state === "listening") || ev.source === "tts") orb.setLevel(ev.value);
      break;
    case "user":
      replyText = ""; $("reply").innerHTML = ""; showReply(false); setBusy(true);
      if (ev.source === "voice") { $("transcript").textContent = `“${ev.text}”`; $("transcript").classList.add("show"); }
      break;
    case "transcript":
      if (ev.text) { $("transcript").textContent = `“${ev.text}”`; $("transcript").classList.add("show"); }
      break;
    case "ack":
      if (!replyText) { $("reply").innerHTML = `<span class="muted">${escapeHtml(ev.text)}</span>`; showReply(true); }
      break;
    case "delta":
      replyText += ev.text;
      $("reply").innerHTML = renderMd(replyText);
      $("reply").scrollTop = $("reply").scrollHeight;
      showReply(true);
      break;
    case "delta_break":
      replyText += "\n";
      break;
    case "reply":
      if (ev.text && !replyText.trim()) { replyText = ev.text; $("reply").innerHTML = renderMd(replyText); }
      break;
    case "done":
      setBusy(false);
      setTimeout(() => $("transcript").classList.remove("show"), 2500);
      break;
    case "cancelled":
      setBusy(false);
      break;
    case "tool_start": activityStart(ev); break;
    case "tool_end": activityEnd(ev); break;
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
  $("btnMic").title = ready ? "Talk (or say “Hey Jarvis”)" : `Voice: ${voiceStatus}`;
}

// ------------------------------------------------------------------ prompt + mic
$("prompt").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  promptHistory.unshift(text); histIdx = -1;
  send({ type: "ask", text });
  $("input").value = "";
});
const promptHistory = [];
let histIdx = -1;
$("input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowUp" && promptHistory.length) { histIdx = Math.min(promptHistory.length - 1, histIdx + 1); $("input").value = promptHistory[histIdx]; e.preventDefault(); }
  if (e.key === "ArrowDown") { histIdx = Math.max(-1, histIdx - 1); $("input").value = histIdx >= 0 ? promptHistory[histIdx] : ""; e.preventDefault(); }
});
$("input").addEventListener("input", () => { if ($("input").value.length === 1) send({ type: "interrupt" }); });
$("btnStop").onclick = () => send({ type: "cancel" });
$("btnMic").onclick = () => send({ type: "ptt" });
$("orb").addEventListener("click", () => { if (!orb.consumeDrag()) send({ type: "ptt" }); });
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
  const list = $("historyList");
  list.replaceChildren();
  for (const m of messages) {
    const d = document.createElement("div");
    d.className = `msg ${m.role}`;
    const t = document.createElement("time");
    t.textContent = new Date(m.ts * 1000).toLocaleString();
    d.append(t, document.createTextNode(m.content || ""));
    list.append(d);
  }
  if (!messages.length) list.innerHTML = `<p class="muted">No conversations yet.</p>`;
  $("history").classList.remove("hidden");
  list.scrollTop = list.scrollHeight;
};
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
  $("sPersonality").value = s.personality;
  $("about").textContent = `Jarvis v${data.version} · MIT · github.com/yash2031233/jarvis-ai-api`;
}
function voiceLabel(v) {
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

connect();
