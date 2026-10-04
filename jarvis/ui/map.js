// Map workspace (ported from Jarvis v1): your live position on a dark OpenStreetMap map. Leaflet loads the first time
// the map opens. Draws what the location tool sends (me / route / nearby / places / trip) and follows live GPS fixes:
// with a route on screen, every fix re-measures what's left of it and counts the ETA down.
//
// Navigation ("exactly like Google Maps, with the voice and auto-following"): when a route arrives it starts
// turn-by-turn by itself. Position comes from this device's GPS when it has one (about one fix a second; laptops use
// Wi-Fi location), else from the phone's live location shared with the Telegram bot. The dot glides between fixes and
// snaps onto the road; the map turns heading-up and follows, the dot in the lower part of the screen, zooming with
// speed. Jarvis speaks the turns in his own voice ("In half a mile, turn right onto Route 9" ... "Turn right onto
// Route 9"), how far to go after each one, re-routes when you leave the route, and says when you've arrived.
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";   // keyless; darkened in CSS
const LEAFLET = "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/";

export function createMap({ api, onOpen, onClose }) {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let L = null, map = null, layers = {}, state = {}, route = null, followMe = true;
  let units = "imperial", voiceOn = true;

  function loadLeaflet() {
    if (window.L) return Promise.resolve(window.L);
    return new Promise((ok, bad) => {
      const css = document.createElement("link");
      css.rel = "stylesheet"; css.href = LEAFLET + "leaflet.css";
      document.head.appendChild(css);
      const js = document.createElement("script");
      js.src = LEAFLET + "leaflet.js"; js.onload = () => ok(window.L); js.onerror = () => bad(new Error("map library didn't load (offline?)"));
      document.head.appendChild(js);
    });
  }

  async function ensure() {
    if (map) return map;
    L = await loadLeaflet();
    // no tile fade-in: the follow-cam moves the map every frame, which keeps restarting the fade (tiles stayed invisible)
    map = L.map("leaf", { zoomControl: false, attributionControl: true, worldCopyJump: true, fadeAnimation: false });
    const tl = L.tileLayer(TILES, { maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(map);
    // Navigating, the map is a tilted 3D layer with a colour filter on its tiles, and the browser doesn't repaint it
    // when new tiles arrive - they load but the road stays black until something changes inside the tile layer. A
    // class flip isn't enough (and never fires when the tiles were cached before navigation started), so each batch of
    // arriving tiles - and the start of navigation - briefly adds an empty element to the tile pane: a real repaint.
    tl.on("tileload load", () => { if (document.body.classList.contains("navOn")) repaint(); });
    L.control.zoom({ position: "bottomleft" }).addTo(map);
    for (const k of ["trail", "route", "places", "reminders", "nearby", "me"]) layers[k] = L.layerGroup().addTo(map);
    map.setView([30, 0], 2);
    map.on("dragstart", () => { followMe = false; $("#mapMe").classList.add("off"); });
    new ResizeObserver(() => map.invalidateSize()).observe($("#leaf"));
    return map;
  }

  let rp = 0;
  function repaint() {                       // at most ~4 times a second
    if (rp || !map) return;
    rp = setTimeout(() => {
      rp = 0;
      const pane = map.getPane("tilePane"), d = document.createElement("i");
      pane.appendChild(d);
      requestAnimationFrame(() => requestAnimationFrame(() => d.remove()));
    }, 250);
  }

  // ---- helpers ----------------------------------------------------------------------------------------
  const R = 6371000, rad = (d) => d * Math.PI / 180;
  function dist(a, b) {
    const h = Math.sin(rad(b[0] - a[0]) / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(rad(b[1] - a[1]) / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }
  const fmtDist = (m) => (units === "metric" || m < 950 ? (m < 950 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(1)} km`) : `${(m / 1609.34).toFixed(1)} mi`);
  const fmtClock = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const ago = (s) => (s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`);
  const MODE = { drive: "Drive", walk: "Walk", bike: "Bike" };
  function dur(secs) {                       // [big number, unit] - "14 min", or "3:49 hr" for long trips
    const m = Math.round(secs / 60);
    return m < 90 ? [String(Math.max(m, secs > 30 ? 1 : 0)), "min"] : [`${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`, "hr"];
  }
  function arrow(t) {
    t = t.toLowerCase();
    if (t.startsWith("arrive")) return "⚑";
    if (t.includes("u-turn")) return "⤺";
    if (t.includes("roundabout")) return "⟳";
    if (t.includes("sharp left") || t.includes("left")) return t.includes("slight") ? "↖" : "↰";
    if (t.includes("sharp right") || t.includes("right")) return t.includes("slight") ? "↗" : "↱";
    return "↑";
  }
  function pin(label, cls) {
    return L.divIcon({ className: `pin ${cls || ""}`, html: `<span class="dot"></span>${label ? `<span class="lbl">${esc(label)}</span>` : ""}`,
      iconSize: [14, 14], iconAnchor: [7, 7] });
  }

  // ---- drawing ---------------------------------------------------------------------------------------------
  function drawMe(p) {
    if (nav.on) return;                                      // navigating: the car marker is the dot
    layers.me.clearLayers();
    if (!p) return;
    const ll = [p.lat, p.lon];
    if (p.acc) L.circle(ll, { radius: p.acc, className: "acc", weight: 1 }).addTo(layers.me);
    const head = typeof p.heading === "number" ? `<i class="cone" style="transform:rotate(${p.heading}deg)"></i>` : "";
    L.marker(ll, { icon: L.divIcon({ className: "me", html: `<div class="me-dot">${head}<b></b></div>`, iconSize: [24, 24], iconAnchor: [12, 12] }),
      zIndexOffset: 1000, interactive: false }).addTo(layers.me);
  }
  function drawTrail(t) {
    layers.trail.clearLayers();
    if (t && t.length > 1) L.polyline(t, { className: "trail", weight: 3 }).addTo(layers.trail);
  }
  function drawPlaces(places, rems) {
    layers.places.clearLayers(); layers.reminders.clearLayers();
    for (const p of places || []) L.marker([p.lat, p.lon], { icon: pin(p.name, "place") }).bindTooltip(esc(p.address || p.name)).addTo(layers.places);
    for (const r of rems || []) {
      L.circle([r.lat, r.lon], { radius: r.radius_m || 150, className: "geofence", weight: 2 })
        .bindTooltip(`⏰ ${esc(r.text)} (${r.on === "leave" ? "leaving" : "arriving"} ${esc(r.place)})`).addTo(layers.reminders);
    }
  }
  function drawRoute(r) {
    layers.route.clearLayers();
    route = r ? { ...r, cum: [] } : null;
    if (!r) return;
    const g = r.geometry;
    let acc = 0;                                           // running distance along the line, for live ETA
    route.cum = g.map((pt, i) => (acc += i ? dist(g[i - 1], pt) : 0));
    route.total = acc || r.dist_m;
    // where each turn sits along the line, so the "next turn" panel can follow the live position
    const near = (at) => { let bi = 0, bd = Infinity; g.forEach((pt, i) => { const d = dist(at, pt); if (d < bd) { bd = d; bi = i; } }); return bi; };
    route.turns = (r.steps || []).filter((st) => st.at && !/^head out/i.test(st.text)).map((st) => ({ ...st, idx: near(st.at) }));
    L.polyline(g, { className: "route-glow", weight: 16 }).addTo(layers.route);
    L.polyline(g, { className: "route-core", weight: 5 }).addTo(layers.route);
    L.polyline(g, { className: "route-flow", weight: 5, dashArray: "2 22" }).addTo(layers.route);
    if (r.to) L.marker([r.to.lat, r.to.lon], { icon: pin(r.to.name, "dest") }).addTo(layers.route);
  }
  function drawNearby(nb) {
    layers.nearby.clearLayers();
    for (const [i, h] of ((nb && nb.results) || []).entries()) {
      L.marker([h.lat, h.lon], { icon: pin(`${i + 1}. ${h.name}`, "hit") }).bindTooltip(esc(h.address || "")).addTo(layers.nearby);
    }
  }

  // ---- the card in the corner ---------------------------------------------------------------------------------
  function card(s) {
    const el = $("#mapCard");
    const me = s.me;
    const liveTag = me && me.approx ? '<span class="gps off">approximate (IP)</span>'
      : me && me.fresh && me.live ? '<span class="gps live">LIVE GPS</span>'
      : me ? `<span class="gps">last fix ${ago(me.age_s ?? 0)}</span>` : '<span class="gps off">no GPS yet</span>';
    if (s.view === "route" && route) {
      const r = route, st = r.steps || [];
      const next = st.find((x) => !/^head out/i.test(x.text)) || st[0];
      el.innerHTML = `
        <div class="kicker">${MODE[r.mode] || "Route"} · ${liveTag}${r.live_traffic ? " · traffic" : ""}</div>
        <div class="dest">${esc(r.to ? r.to.name : "Destination")}</div>
        <div class="eta"><b id="etaMin">${dur(r.secs)[0]}</b><span id="etaUnit">${dur(r.secs)[1]}</span>
          <div class="etaSide"><div>arrive <b id="etaClock">${esc(r.eta)}</b></div><div id="etaDist">${fmtDist(r.dist_m)}</div></div></div>
        <div class="bar"><i id="etaBar" style="width:0%"></i></div>
        ${next ? `<div class="next"><span class="arr" id="nextArr">${arrow(next.text)}</span><div><div id="nextText">${esc(next.text)}</div><small id="nextIn">in ${fmtDist(next.dist_m)}</small></div></div>` : ""}
        <button class="navGo" id="navGo">${nav.on ? "Navigating…" : "▶ Start navigation"}</button>
        <details class="steps"><summary>All ${st.length} steps</summary><ol>${st.map((x) => `<li><span class="arr">${arrow(x.text)}</span>${esc(x.text)}<small>${fmtDist(x.dist_m)}</small></li>`).join("")}</ol></details>`;
      $("#navGo").onclick = () => nav.start();
    } else if (s.view === "nearby" && s.nearby) {
      const res = s.nearby.results || [];
      el.innerHTML = `<div class="kicker">Nearby · ${liveTag}</div><div class="dest">${esc(s.nearby.what)}</div>
        <ol class="hits">${res.map((h, i) => `<li data-i="${i}"><b>${i + 1}</b><div><div>${esc(h.name)}</div><small>${h.dist_m != null ? fmtDist(h.dist_m) : ""}${h.address ? " · " + esc(h.address) : ""}${h.hours ? " · " + esc(h.hours) : ""}</small></div></li>`).join("") || "<li>Nothing found.</li>"}</ol>`;
      el.querySelectorAll(".hits li[data-i]").forEach((li) => li.onclick = () => { const h = res[+li.dataset.i]; followMe = false; map.flyTo([h.lat, h.lon], 17); });
    } else if (s.view === "places") {
      const pl = s.places || [], rm = s.reminders || [];
      el.innerHTML = `<div class="kicker">Places · ${liveTag}</div>
        <ol class="hits">${pl.map((p) => `<li><b>★</b><div><div>${esc(p.name)}</div><small>${esc(p.address || "")}</small></div></li>`).join("") || "<li><small>No saved places yet - say “save this as home”.</small></li>"}</ol>
        ${rm.length ? `<div class="kicker" style="margin-top:10px">Reminders</div><ol class="hits">${rm.map((r) => `<li><b>⏰</b><div><div>${esc(r.text)}</div><small>${r.on === "leave" ? "leaving" : "at"} ${esc(r.place)}</small></div></li>`).join("")}</ol>` : ""}`;
    } else {
      el.innerHTML = `<div class="kicker">${s.view === "trip" ? "Today's trail" : "You are here"} · ${liveTag}</div>
        <div class="dest">${esc(s.address || (me ? `${me.lat.toFixed(5)}, ${me.lon.toFixed(5)}` : "Waiting for your location"))}</div>
        ${me && !me.approx ? "" : `<small class="hint">For real GPS: share your live location with your Jarvis Telegram bot (Settings → Location), or allow location access on this device.</small>`}`;
    }
  }

  // Live ETA: find where the newest fix sits on the route and scale the duration by what's left.
  function progress(p) {
    if (!route || !route.geometry.length) return;
    const here = [p.lat, p.lon];
    let best = 0, bestD = Infinity;
    route.geometry.forEach((pt, i) => { const d = dist(here, pt); if (d < bestD) { bestD = d; best = i; } });
    const left = Math.max(0, route.total - route.cum[best]);
    const secs = route.secs * (left / route.total);
    const m = $("#etaMin"), c = $("#etaClock"), d = $("#etaDist"), b = $("#etaBar");
    const [big, unit] = dur(secs);
    if (m) m.textContent = big;
    const u = $("#etaUnit"); if (u) u.textContent = unit;
    if (c) c.textContent = fmtClock(new Date(Date.now() + secs * 1000));
    if (d) d.textContent = fmtDist(left);
    if (b) b.style.width = `${Math.min(100, 100 * (1 - left / route.total))}%`;
    const turn = (route.turns || []).find((t) => t.idx > best);
    if (turn && $("#nextText")) {
      $("#nextText").textContent = turn.text;
      $("#nextArr").textContent = arrow(turn.text);
      $("#nextIn").textContent = `in ${fmtDist(Math.max(0, route.cum[turn.idx] - route.cum[best]))}`;
    }
  }

  function frame(s) {
    const pts = [];
    if (s.view === "route" && route) pts.push(...route.geometry);
    else if (s.view === "nearby" && s.nearby) pts.push(...(s.nearby.results || []).map((h) => [h.lat, h.lon]));
    else if (s.view === "trip" && s.trail && s.trail.length) pts.push(...s.trail);
    else if (s.focus) pts.push([s.focus.lat, s.focus.lon]);
    else if (s.view === "places") pts.push(...(s.places || []).map((p) => [p.lat, p.lon]));
    if (s.me) pts.push([s.me.lat, s.me.lon]);
    if (pts.length > 1) map.fitBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 17 });
    else if (pts.length === 1) map.setView(pts[0], s.view === "me" ? (s.me && s.me.approx ? 11 : 16) : 15);
  }

  async function show(s, { autostart = true } = {}) {
    onOpen();
    try { await ensure(); } catch (e) { $("#mapCard").innerHTML = `<div class="dest">${esc(e.message)}</div>`; return; }
    if (s.units) units = s.units;
    if (s.voice !== undefined) voiceOn = s.voice;
    state = { ...state, ...s };
    if (s.view !== "route") state.route = s.route;
    followMe = true;
    $("#mapMe").classList.remove("off");
    drawMe(state.me);
    drawTrail(state.trail);
    drawPlaces(state.places, state.reminders);
    drawRoute(state.view === "route" ? state.route : null);
    drawNearby(state.view === "nearby" ? state.nearby : null);
    card(state);
    setTimeout(() => { map.invalidateSize(); frame(state); if (state.me && route) progress(state.me); }, 60);
    if (state.view === "route" && route) { if (autostart) nav.start(); } else nav.stop(true);
    if (!state.me || !state.me.fresh || state.me.approx) locateOnce();
  }

  async function open() {
    try { await show(await api("/api/geo/state"), { autostart: false }); } catch (e) { /* offline */ }
  }
  function close() {
    nav.stop(true);
    onClose();
    api("/api/geo/close", { method: "POST" }).catch(() => {});
  }

  // this device's own position (laptops: Wi-Fi location; phones: GPS) - asked once when the map opens without a fix
  let locating = false;
  function locateOnce() {
    if (locating || !navigator.geolocation) return;
    locating = true;
    navigator.geolocation.getCurrentPosition((g) => {
      locating = false;
      const c = g.coords, f = { lat: c.latitude, lon: c.longitude, heading: c.heading, speed: c.speed, acc: c.accuracy };
      api("/api/geo/fix", { method: "POST", body: f }).catch(() => {});
      geo({ ...f, live: true });
    }, () => { locating = false; }, { enableHighAccuracy: true, maximumAge: 30000, timeout: 15000 });
  }

  function geo(e) {
    if (e.reminder_fired) { toast(e.text); return; }
    if (!map || typeof e.lat !== "number") return;
    if (nav.on && nav.localFresh() && e.src !== "device") return;  // navigating on this device's GPS: the phone's fixes are older news
    if (nav.on) { nav.fix({ lat: e.lat, lon: e.lon, heading: e.heading, speed: null, acc: e.acc }); return; }
    const p = { ...e, age_s: 0, fresh: true };
    const first = !state.me || state.me.approx;
    state.me = p;
    state.trail = [...(state.trail || []), [p.lat, p.lon]].slice(-2000);
    drawMe(p);
    drawTrail(state.trail);
    progress(p);
    const tag = document.querySelector("#mapCard .gps");
    if (tag) { tag.className = "gps live"; tag.textContent = "LIVE GPS"; }
    if (first && (state.view || "me") === "me") { card(state); frame(state); }
    else if (followMe) map.panTo([p.lat, p.lon], { animate: true });
  }

  function toast(text) {
    const t = document.createElement("div");
    t.className = "mapToast"; t.textContent = text;
    $("#mapView").appendChild(t);
    setTimeout(() => t.remove(), 9000);
  }

  $("#mapMe").addEventListener("click", () => {
    if (!map || !state.me) return locateOnce();
    followMe = true; $("#mapMe").classList.remove("off");
    map.flyTo([state.me.lat, state.me.lon], Math.max(map.getZoom(), 16));
  });
  $("#mapFit").addEventListener("click", () => { if (map) frame(state); });
  $("#mapClose").addEventListener("click", close);

  // =============================================================================================================
  // Navigation: follow-cam + voice, like Google Maps
  // =============================================================================================================
  const nav = (() => {
    const N = { on: false, watch: null, lastLocal: 0, pos: null, from: null, to: null, t0: 0, dur: 1000, heading: 0, hdgGoal: 0,
                speed: 0, raf: 0, said: new Set(), afterTurn: -1, off: 0, rerouteAt: 0, arrived: false, muted: false,
                lock: null, lastFixAt: 0, wantZoom: 17 };
    const body = document.body;
    try { N.muted = localStorage.getItem("jarvisNavMute") === "1"; } catch { /* private */ }
    // ---- distances the way people read them out (miles/feet, or km/m)
    const ft = (m) => m * 3.28084, mi = (m) => m / 1609.34;
    function say_dist(m) {
      if (units === "metric") {
        if (m < 950) return `${Math.max(50, Math.round(m / 50) * 50)} metres`;
        const k = m / 1000;
        return `${k < 10 ? k.toFixed(1).replace(/\.0$/, "") : Math.round(k)} kilometres`;
      }
      if (m < 160) { const f = Math.max(50, Math.round(ft(m) / 50) * 50); return `${f} feet`; }
      const x = mi(m);
      if (x < 0.35) return "a quarter mile"; if (x < 0.65) return "half a mile"; if (x < 0.9) return "three quarters of a mile";
      if (x < 1.15) return "1 mile";
      return `${x < 10 ? x.toFixed(1).replace(/\.0$/, "") : Math.round(x)} miles`;
    }
    function show_dist(m) {
      if (units === "metric") return m < 950 ? `${Math.max(50, Math.round(m / 50) * 50)} m` : `${(m / 1000).toFixed(1)} km`;
      return m < 160 ? `${Math.max(50, Math.round(ft(m) / 50) * 50)} ft` : `${mi(m) < 10 ? mi(m).toFixed(1) : Math.round(mi(m))} mi`;
    }
    const lower = (t) => t.charAt(0).toLowerCase() + t.slice(1);
    // how a step sounds out loud: the router's "Arrive - destination on the right" becomes natural speech
    const spoken = (t) => { const a = /^arrive(?:.*on the (left|right))?/i.exec(t);
      return a ? (a[1] ? `your destination will be on the ${a[1].toLowerCase()}` : "you'll arrive at your destination") : lower(t); };
    // ---- the voice: Jarvis's own (the app's speech), only ever ONE copy of the app speaking, each line once per 20 s,
    // and a line that's gone stale (the turn it was about has passed) is dropped instead of read late.
    const tabId = Math.random().toString(36).slice(2);
    let owner = true, bc = null;
    try { bc = new BroadcastChannel("jarvis-nav"); bc.onmessage = (e) => { if (e.data && e.data.nav && e.data.nav !== tabId) owner = false; }; } catch { /* old browser */ }
    const recent = new Map();
    function speak(text, opt = {}) {
      if (N.muted || !voiceOn || !text || !owner) return;
      const now = Date.now();
      if (now - (recent.get(text) || 0) < 20000) return;
      recent.set(text, now);
      api("/api/geo/say", { method: "POST", body: { text, urgent: !!opt.urgent } }).then((r) => {
        if (r && r.spoken) return;
        if (!("speechSynthesis" in window)) return;           // no voice in the app: the browser's own
        if (opt.urgent) speechSynthesis.cancel();
        speechSynthesis.speak(new SpeechSynthesisUtterance(text));
      }).catch(() => {});
    }
    // ---- geometry: snap a point onto the route
    function snap(p) {
      const g = route.geometry, cosLat = Math.cos(rad(p.lat));
      let best = { d: Infinity, i: 0, pt: [p.lat, p.lon], along: 0 };
      for (let i = 1; i < g.length; i++) {
        const a = g[i - 1], b = g[i];
        const ax = a[1] * cosLat, ay = a[0], bx = b[1] * cosLat, by = b[0], px = p.lon * cosLat, py = p.lat;
        const vx = bx - ax, vy = by - ay, L2 = vx * vx + vy * vy || 1e-12;
        const k = Math.max(0, Math.min(1, ((px - ax) * vx + (py - ay) * vy) / L2));
        const q = [ay + vy * k, (ax + vx * k) / cosLat], d = dist([p.lat, p.lon], q);
        if (d < best.d) best = { d, i: i - 1, pt: q, along: route.cum[i - 1] + dist(a, q) };
      }
      return best;
    }
    const bearing = (a, b) => { const y = Math.sin(rad(b[1] - a[1])) * Math.cos(rad(b[0])), x = Math.cos(rad(a[0])) * Math.sin(rad(b[0])) - Math.sin(rad(a[0])) * Math.cos(rad(b[0])) * Math.cos(rad(b[1] - a[1]));
      return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360; };

    // ---- the screens: a big turn banner on top, the trip bar at the bottom
    function ui() {
      if ($("#navTop")) return;
      const mv = $("#mapView");
      mv.insertAdjacentHTML("beforeend", `<div id="navTop" class="navTop"><span class="navArr">↑</span><div><b class="navDist"></b><div class="navText"></div></div><div class="navThen"></div></div>
        <div id="navBot" class="navBot"><div class="navEta"><b class="navMin">–</b><span>min</span></div><div class="navMeta"><span class="navLeft"></span><span class="navClock"></span></div>
          <button class="navMute" aria-label="Mute directions"></button><button class="navExit">Exit</button></div>`);
      $(".navExit").onclick = () => stop();
      $(".navMute").onclick = () => { N.muted = !N.muted; try { localStorage.setItem("jarvisNavMute", N.muted ? "1" : "0"); } catch { /* fine */ } paintMute(); if (!N.muted) speak("Voice guidance on."); };
      paintMute();
    }
    function paintMute() { const b = $(".navMute"); if (b) { b.textContent = N.muted ? "🔇" : "🔊"; b.classList.toggle("off", N.muted); } }

    function start() {
      if (!route) return;
      ui();
      const key = `${route.total | 0}:${route.geometry.length}:${route.to ? route.to.name : ""}`;
      const fresh = !N.on, same = key === N.routeKey;
      N.routeKey = key;
      if (!same) { N.arrived = false; N.said = new Set(); N.off = 0; N.afterTurn = -1; }
      N.on = true;
      owner = true; try { bc && bc.postMessage({ nav: tabId }); } catch { /* fine */ }
      body.classList.add("navOn");
      persp();
      if (map) { map.options.zoomSnap = 0; layers.me.clearLayers(); }
      followMe = true;
      if (map) { map.dragging.disable(); map.touchZoom.disable(); map.doubleClickZoom.disable(); }
      setTimeout(() => map && map.invalidateSize(), 50);
      if (navigator.geolocation && N.watch == null) {
        N.watch = navigator.geolocation.watchPosition((g) => {
          const c = g.coords; N.lastLocal = Date.now();
          const f = { lat: c.latitude, lon: c.longitude, heading: c.heading, speed: c.speed, acc: c.accuracy };
          fix(f);
          api("/api/geo/fix", { method: "POST", body: f }).catch(() => {});
        }, () => { /* no GPS here: the phone's live location (Telegram) still drives it */ }, { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 });
      }
      try { if (navigator.wakeLock && !N.lock) navigator.wakeLock.request("screen").then((l) => { N.lock = l; }).catch(() => {}); } catch { /* fine */ }
      if (fresh) {                                   // the intro first; the turns are the turn logic's job
        const mins = Math.round(route.secs / 60);
        speak(`Starting route to ${route.to ? route.to.name : "your destination"}. ${mins < 90 ? mins + " minutes" : Math.floor(mins / 60) + " hours " + (mins % 60) + " minutes"}, arriving at ${route.eta}.`);
      }
      if (state.me) fix({ lat: state.me.lat, lon: state.me.lon, heading: state.me.heading, speed: 0 });
      if (!N.raf) N.raf = requestAnimationFrame(tick);
      repaint(); setTimeout(repaint, 900); setTimeout(repaint, 2500);
      const go = $("#navGo"); if (go) go.textContent = "Navigating…";
    }
    function stop(quiet) {
      if (!N.on) return;
      N.on = false;
      body.classList.remove("navOn", "navLand");
      if (N.watch != null && navigator.geolocation) navigator.geolocation.clearWatch(N.watch);
      N.watch = null;
      if (N.lock) { N.lock.release().catch(() => {}); N.lock = null; }
      cancelAnimationFrame(N.raf); N.raf = 0;
      const lf = $("#leaf"); if (lf) lf.style.transform = "";
      N.routeKey = null;
      if (map) { map.options.zoomSnap = 1; map.setZoom(Math.round(map.getZoom()), { animate: false }); }
      if (map) { map.dragging.enable(); map.touchZoom.enable(); map.doubleClickZoom.enable(); setTimeout(() => { map.invalidateSize(); frame(state); drawMe(state.me); }, 80); }
      if (!quiet) speak("Navigation ended.", { urgent: true });
      const go = $("#navGo"); if (go) go.textContent = "▶ Start navigation";
    }
    const localFresh = () => Date.now() - N.lastLocal < 5000;

    // a new fix: glide there over the time since the last one, and work out what to say
    function fix(f) {
      if (!route) return;
      const now = performance.now();
      const raw = { lat: f.lat, lon: f.lon };
      const sn = snap(raw);
      const onRoad = sn.d < (route.mode === "walk" ? 25 : 40);
      const target = onRoad ? sn.pt : [raw.lat, raw.lon];
      // heading: the device's, else the road's direction, else the way we moved
      let h = typeof f.heading === "number" && !Number.isNaN(f.heading) && (f.speed == null || f.speed > 1) ? f.heading : null;
      if (h == null && onRoad) { const g = route.geometry, i = Math.min(sn.i + 1, g.length - 1); h = bearing(g[sn.i], g[i]); }
      if (h == null && N.pos) { const moved = dist(N.pos, target); if (moved > 3) h = bearing(N.pos, target); }
      if (h != null) N.hdgGoal = h;
      if (typeof f.speed === "number" && f.speed >= 0) N.speed = f.speed;
      else if (N.to && N.lastFixAt) N.speed = dist(N.to, target) / Math.max(0.5, (now - N.lastFixAt) / 1000);
      N.from = N.pos || target; N.to = target; N.t0 = now; N.dur = Math.min(2500, Math.max(400, N.lastFixAt ? now - N.lastFixAt : 800)); N.lastFixAt = now;
      N.wantZoom = route.mode === "walk" ? 18 : N.speed > 25 ? 16 : N.speed > 14 ? 16.7 : 17.3;
      state.me = { ...(state.me || {}), lat: raw.lat, lon: raw.lon, fresh: true, live: true, age_s: 0, approx: false };
      guide(sn, onRoad);
    }

    // turn-by-turn: what's next, how far, and when to say it
    function guide(sn, onRoad) {
      const left = Math.max(0, route.total - sn.along);
      N.along = sn.along; fasterCheck();
      // arrived?
      if (!N.arrived && left < (route.mode === "walk" ? 15 : 35)) {
        N.arrived = true;
        const last = (route.steps || []).slice(-1)[0], side = last && /on the (left|right)/i.exec(last.text);
        speak(`You have arrived at ${route.to ? route.to.name : "your destination"}.${side ? ` It's on the ${side[1].toLowerCase()}.` : ""}`, { urgent: true });
        paint(null, 0, left);
        setTimeout(() => stop(true), 12000);
        return;
      }
      // off the route: say so, get a new one
      if (!onRoad) {
        if (++N.off >= 3 && Date.now() - N.rerouteAt > 20000) { N.rerouteAt = Date.now(); N.off = 0; reroute(); }
      } else N.off = 0;
      const turns = route.turns || [];
      const ti = turns.findIndex((t) => route.cum[t.idx] > sn.along + 3);
      const turn = ti >= 0 ? turns[ti] : null, toTurn = turn ? route.cum[turn.idx] - sn.along : left;
      paint(turn, toTurn, left, turns[ti + 1]);
      if (!turn || N.arrived) return;
      const drive = route.mode !== "walk", key = (k) => `${ti}:${k}`;
      const far = drive ? (N.speed > 22 ? 1600 : 800) : 160, near = drive ? (N.speed > 22 ? 400 : 160) : 30;
      const instr = turn.text;
      // just finished a turn and the next one is a long way off: "continue for ..."
      if (N.afterTurn !== ti && N.said.has(`${ti - 1}:now`) && toTurn > far * 1.5) {
        N.afterTurn = ti;
        const road = /onto (.+)$/i.exec(turns[ti - 1] ? turns[ti - 1].text : "");
        speak(`Continue${road ? " on " + road[1] : ""} for ${say_dist(toTurn)}.`);
      }
      const isArrive = /^arrive/i.test(instr);
      if (toTurn <= far && toTurn > near * 1.6 && !N.said.has(key("far"))) { N.said.add(key("far")); speak(`In ${say_dist(toTurn)}, ${spoken(instr)}.`, { urgent: true }); }
      else if (toTurn <= near && !N.said.has(key("now"))) {
        N.said.add(key("now")); N.said.add(key("far"));
        if (isArrive) return;                            // the arrival line itself comes when you get there
        const then = turns[ti + 1], soon = then && route.cum[then.idx] - route.cum[turn.idx] < (drive ? 200 : 40);
        speak(`${instr}.${soon ? ` Then ${spoken(then.text)}.` : ""}`, { urgent: true });
      }
    }
    function paint(turn, toTurn, left, then) {
      const a = $(".navArr"), d = $(".navDist"), tx = $(".navText"), th = $(".navThen");
      if (!a) return;
      if (turn) { a.textContent = arrow(turn.text); d.textContent = show_dist(toTurn); tx.textContent = turn.text; }
      else { a.textContent = "⚑"; d.textContent = N.arrived ? "Arrived" : show_dist(left); tx.textContent = route.to ? route.to.name : "Destination"; }
      th.innerHTML = then ? `Then <span>${arrow(then.text)}</span>` : "";
      th.style.display = then && toTurn < 400 ? "" : "none";
      const secs = route.secs * (left / (route.total || 1));
      const [big] = dur(secs);
      $(".navMin").textContent = big; $(".navMin").nextElementSibling.textContent = secs >= 5400 ? "hr" : "min";
      $(".navLeft").textContent = show_dist(left);
      $(".navClock").textContent = fmtClock(new Date(Date.now() + secs * 1000));
      progress({ lat: N.to ? N.to[0] : 0, lon: N.to ? N.to[1] : 0 });
    }
    // every 3 minutes with live traffic: is there a faster way now? Switch if it saves 2+ minutes (and 8%+).
    async function fasterCheck() {
      if (!route || !route.live_traffic || N.arrived || Date.now() - (N.checkAt || 0) < 180000) return;
      N.checkAt = Date.now();
      try {
        const me = state.me, left = route.secs * (Math.max(0, route.total - (N.along || 0)) / (route.total || 1));
        const r = await api("/api/geo/reroute", { method: "POST", body: { lat: me.lat, lon: me.lon, to: route.to, mode: route.mode, check: true } });
        if (!r.route || !N.on) return;
        const save = left - r.route.secs;
        if (save >= 120 && save >= left * 0.08) {
          state.route = r.route; drawRoute(r.route); N.said = new Set(); N.afterTurn = -1; N.routeKey = `${route.total | 0}:${route.geometry.length}:${route.to ? route.to.name : ""}`;
          speak(`Found a faster route. It saves ${Math.round(save / 60)} minutes.`);
          api("/api/geo/reroute", { method: "POST", body: { lat: me.lat, lon: me.lon, to: route.to, mode: route.mode } }).catch(() => {});
        }
      } catch { /* keep this one */ }
    }
    async function reroute() {
      speak("Rerouting.", { urgent: true });
      try {
        const me = state.me;
        const r = await api("/api/geo/reroute", { method: "POST", body: { lat: me.lat, lon: me.lon, to: route.to, mode: route.mode } });
        if (!r.route) return;
        state.route = r.route; drawRoute(r.route); N.said = new Set(); N.afterTurn = -1;
      } catch { /* keep the old one */ }
    }

    // ---- the view, like Google Maps' driving mode (without the 3D buildings): the map lies back in perspective,
    // turns heading-up around the car, and the car - a chevron flat on the road - sits centred low on the screen.
    // The map layer is sized from the screen and the tilt so it always reaches past every edge at any heading.
    const TILT = 44;
    function persp() {
      const mv = $("#mapView"), lf = $("#leaf");
      if (!mv || !lf) return;
      const W = innerWidth, H = innerHeight, P = Math.max(700, H * 1.25), th = rad(TILT);
      // landscape and short (a phone in a car mount): the directions sit in a column on the left, the car right of centre
      const land = W > H && H < 560, CAR_X = land ? 0.66 : 0.5, CAR_Y = land ? 0.68 : 0.70, half = W * Math.max(CAR_X, 1 - CAR_X);
      const up = H * CAR_Y - H * 0.12;                                // must reach this far up (the horizon band covers the rest)
      const ahead = up * P / Math.max(1, P * Math.cos(th) - up * Math.sin(th));   // map distance that lands there
      const side = (half + 40) * (P + ahead * Math.sin(th)) / P;           // half-width needed at that distance
      const back = H * (1 - CAR_Y) + 60;
      const Rr = Math.max(Math.hypot(ahead, side), Math.hypot(back, half + 40)) + 80;
      const S = Math.ceil(Rr * 2);
      mv.style.setProperty("--navS", S + "px"); mv.style.setProperty("--navP", P + "px"); mv.style.setProperty("--navY", CAR_Y * 100 + "%"); mv.style.setProperty("--navX", CAR_X * 100 + "%");
      body.classList.toggle("navLand", land);
      if (!$(".navSky")) mv.insertAdjacentHTML("beforeend", `<div class="navSky"></div><div class="navMe"><svg viewBox="0 0 40 48" aria-hidden="true"><path d="M20 3 L37 44 L20 34 L3 44 Z"/></svg></div>`);
      if (map) map.invalidateSize({ pan: false });
    }
    addEventListener("resize", () => { if (N.on) persp(); });

    // every frame: glide the car, turn the map heading-up, tilt it, keep the car centred low on the screen
    function tick(now) {
      N.raf = requestAnimationFrame(tick);
      if (!N.on || !map || !N.to) return;
      const k = Math.min(1.25, (now - N.t0) / N.dur);             // a little past the fix = dead reckoning
      const from = N.from || N.to;
      N.pos = [from[0] + (N.to[0] - from[0]) * k, from[1] + (N.to[1] - from[1]) * k];
      const dh = ((N.hdgGoal - N.heading + 540) % 360) - 180; N.heading = (N.heading + dh * 0.08 + 360) % 360;
      if (!followMe || body.dataset.mode !== "map") return;
      const z = map.getZoom() + (N.wantZoom - map.getZoom()) * 0.05;
      map.setView(N.pos, z, { animate: false });                        // the layer's centre IS the car's spot on screen
      const lf = $("#leaf"); if (lf) lf.style.transform = `rotateX(${TILT}deg) rotate(${-N.heading}deg)`;
    }
    return { start, stop, fix, localFresh, get on() { return N.on; } };
  })();

  return { show, open, close, geo, nav, get isOpen() { return document.body.dataset.mode === "map"; } };
}
