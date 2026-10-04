// The Hub: everything at a glance (ported from Jarvis v1's hub) - this computer, GPU, network, Jarvis, weather,
// location, 3D printer, robot car, background jobs and timers. Refreshes every 3 s while it's open.
export function createHub({ api, onOpen, onClose, openMap }) {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let timer = 0;
  const bar = (pct, warn = 85) => `<div class="hbar${pct >= warn ? " hot" : ""}"><i style="width:${Math.max(0, Math.min(100, pct))}%"></i></div>`;
  const card = (title, body, extra = "") => `<div class="hcard ${extra}"><div class="hk">${title}</div>${body}</div>`;
  const big = (v, unit = "") => `<div class="hbig">${v}<small>${unit}</small></div>`;

  function render(d) {
    const s = d.system, n = d.network, j = d.jarvis, cards = [];
    cards.push(card("Computer", `${big(Math.round(s.cpu), "% CPU")}${bar(s.cpu)}
      <div class="hrow"><span>RAM</span><b>${s.ram_used_gb} / ${s.ram_gb} GB</b></div>${bar(100 * s.ram_used_gb / s.ram_gb)}
      <div class="hrow"><span>Disk free</span><b>${s.disk_free_gb} GB</b></div>
      ${s.battery ? `<div class="hrow"><span>Battery</span><b>${s.battery.pct}%${s.battery.plugged ? " ⚡" : ""}</b></div>` : ""}
      <div class="hrow"><span>Up</span><b>${s.uptime_h} h</b></div>`));
    for (const g of Array.isArray(s.gpus) ? s.gpus : []) {
      cards.push(card("GPU", `${big(g.util, "%")}${bar(g.util)}<div class="hrow"><span>${esc(g.name)}</span></div>
        <div class="hrow"><span>VRAM</span><b>${g.vram_used_gb} / ${g.vram_gb} GB</b></div>${bar(100 * g.vram_used_gb / g.vram_gb, 92)}
        <div class="hrow"><span>Temp</span><b>${g.temp_c}°C</b></div>`));
    }
    cards.push(card("Network", `<div class="hrow"><span>Internet</span><b class="${n.internet_ms == null ? "bad" : "ok"}">${n.internet_ms == null ? "offline" : n.internet_ms + " ms"}</b></div>
      <div class="hrow"><span>This PC</span><b>${esc(n.local_ip || "–")}</b></div>
      <div class="hrow"><span>Tailscale</span><b class="${n.tailscale ? "ok" : "dim"}">${n.tailscale ? "on · " + esc(n.tailscale) : "off"}</b></div>`));
    cards.push(card("Jarvis", `<div class="hrow"><span>Model</span><b>${esc(j.model || "not set")}</b></div>
      <div class="hrow"><span>Provider</span><b>${esc(j.provider)}</b></div>
      <div class="hrow"><span>Voice</span><b class="${j.voice === "ready" ? "ok" : "dim"}">${esc(j.voice)}</b></div>
      <div class="hrow"><span>Tools</span><b>${j.tools}</b></div><div class="hrow"><span>Running</span><b>${j.uptime_min} min</b></div>`));
    const w = d.weather;
    if (w && !w.error) cards.push(card("Weather", `${big(Math.round(w.now), "°" + w.units)}<div class="hrow"><span>${esc(w.conditions)}</span><b>${Math.round(w.low)}–${Math.round(w.high)}°</b></div>
      <div class="hrow"><span>Rain</span><b>${w.rain_chance ?? 0}%</b></div><div class="hrow"><span>${esc(w.place)}</span></div>`));
    const l = d.location;
    cards.push(card("Location", l ? `<div class="hrow"><span>Last fix</span><b>${esc(l.age)}</b></div><div class="hrow"><span>From</span><b>${esc(l.source)}</b></div>
      <button class="btn subtle hbtn" data-act="map">Open map</button>` : `<div class="hrow"><span class="dim">No location yet - Settings → Location</span></div>`, "tap"));
    const p = d.printer;
    if (p) cards.push(card("3D printer", !p.online ? `<div class="hrow"><span class="dim">offline${p.error ? " · " + esc(p.error) : ""}</span></div>`
      : `${big(p.progress_pct ?? 0, "%")}${bar(p.progress_pct ?? 0, 101)}<div class="hrow"><span>${esc(p.state || "")}</span><b>${esc(p.file || "")}</b></div>
         ${p.minutes_left ? `<div class="hrow"><span>Left</span><b>${p.minutes_left} min</b></div>` : ""}
         <div class="hrow"><span>Nozzle / bed</span><b>${Math.round(p.nozzle_c ?? 0)}° / ${Math.round(p.bed_c ?? 0)}°</b></div>
         ${(p.slots || []).length ? `<div class="hslots">${p.slots.map((s) => `<i title="${esc(s.material || "")}" style="background:${esc(s.color || "#333")}"></i>`).join("")}</div>` : ""}`));
    const c = d.car;
    if (c) cards.push(card("Robot car", c.online ? `<div class="hrow"><span>Battery</span><b>${c.power === "battery" ? (c.battery_v ?? 0).toFixed(1) + " V" : "USB"}</b></div>
      <div class="hrow"><span>Clear ahead</span><b>${c.distance_cm > 0 ? Math.round(c.distance_cm) + " cm" : "> 3 m"}</b></div>` : `<div class="hrow"><span class="dim">offline</span></div>`));
    cards.push(card("Background jobs", d.jobs.length ? d.jobs.map((x) => `<div class="hrow"><span>${esc(x.task)}</span><b>${esc(x.status)}</b></div>`).join("") : `<div class="hrow"><span class="dim">none running</span></div>`));
    cards.push(card("Timers", d.timers.length ? d.timers.map((x) => `<div class="hrow"><span>${esc(x.label)}</span><b>${esc(x.remaining)}</b></div>`).join("") : `<div class="hrow"><span class="dim">none</span></div>`));
    $("hubGrid").innerHTML = cards.join("");
    $("hubGrid").querySelectorAll("[data-act=map]").forEach((b) => (b.onclick = () => { close(); openMap(); }));
    $("hubTime").textContent = new Date(d.time * 1000).toLocaleTimeString();
  }
  async function tick() {
    if (document.body.dataset.mode !== "hub") { clearInterval(timer); timer = 0; return; }   // another screen took over
    try { render(await api("/api/hub")); } catch (e) { $("hubTime").textContent = `offline (${e.message})`; }
  }
  function open() { onOpen(); document.body.dataset.mode = "hub"; tick(); clearInterval(timer); timer = setInterval(tick, 3000); }
  function close() { clearInterval(timer); timer = 0; if (document.body.dataset.mode === "hub") onClose(); }
  $("hubClose").addEventListener("click", close);
  return { open, close, get isOpen() { return document.body.dataset.mode === "hub"; } };
}
