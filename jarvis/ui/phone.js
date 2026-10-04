// The phone app: this page opened from another device (a phone over Tailscale, added to the home screen).
// There the PC's mic and speakers are no use, so:
//   * the mic button records on the phone and Jarvis's Whisper (on the PC) transcribes it - tap to talk, it stops by
//     itself when you stop speaking (or tap again)
//   * replies are played on the phone, sentence by sentence as they stream, in Jarvis's own voice (the PC renders the
//     audio; the browser's voice if the PC has no voice loaded)
//   * spoken directions while navigating come out of the phone too
//   * the phone's GPS keeps Jarvis's location fresh while the app is open (routes, ETAs, "where am I")
export const REMOTE = !["127.0.0.1", "localhost", "[::1]", "::1"].includes(location.hostname);

export function createPhone({ api, token, orb, toast, onText }) {
  let ctx = null, playing = null, queue = [], busy = false, gen = 0;
  let speakReplies = false, buf = "";

  // ---- audio out. Jarvis's voice plays through a normal media player, not Web Audio: iPhones mute Web Audio when
  // the ring/silent switch is on, media keeps playing. iOS only lets a page play sound after a tap, so the first tap
  // "unlocks" the player with a moment of silence.
  const player = new Audio();
  player.playsInline = true;
  player.preload = "auto";
  const SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
  let unlocked = false;
  function audio() {                                   // the mic meter still uses Web Audio (input only)
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    return ctx;
  }
  function unlock() {
    audio();
    if (unlocked) return;
    unlocked = true;
    player.src = SILENCE;
    player.play().catch(() => { unlocked = false; });
  }
  for (const ev of ["touchend", "click"]) addEventListener(ev, unlock, { passive: true });

  async function fetchVoice(text) {
    const r = await fetch("/api/tts", { method: "POST", body: JSON.stringify({ text }),
      headers: { "content-type": "application/json", "x-jarvis-token": token } });
    if (!r.ok) throw new Error("no voice");
    return URL.createObjectURL(await r.blob());
  }
  function browserVoice(text) {
    return new Promise((res) => {
      if (!("speechSynthesis" in window)) return res();
      const u = new SpeechSynthesisUtterance(text);
      u.onend = u.onerror = () => res();
      speechSynthesis.speak(u);
    });
  }
  function playBuffer(url) {
    return new Promise((res) => {
      let raf = 0, done = false;
      const t0 = performance.now();
      const pulse = () => { const t = (performance.now() - t0) / 1000;      // the orb breathes with the speech
        orb.setLevel(0.35 + 0.25 * Math.abs(Math.sin(t * 7.3)) + 0.15 * Math.abs(Math.sin(t * 3.1))); raf = requestAnimationFrame(pulse); };
      const end = () => { if (done) return; done = true; cancelAnimationFrame(raf); orb.setLevel(0); playing = null;
        URL.revokeObjectURL(url); res(); };
      playing = { stop: () => { player.pause(); end(); } };
      player.onended = end;
      player.onerror = end;
      player.src = url;
      orb.setState("speaking"); pulse();
      player.play().catch(end);
    });
  }
  // Sentences are fetched ahead (so there's no gap between them) but played strictly in order.
  function say(text, { urgent = false } = {}) {
    text = String(text || "").replace(/```[\s\S]*?```/g, " ").replace(/[*_#`>|]/g, "").replace(/https?:\/\/\S+/g, "the link").trim();
    if (!text) return;
    if (urgent) stop();
    const my = gen;
    queue.push({ text, audio: fetchVoice(text).catch(() => null), my });
    pump();
  }
  async function pump() {
    if (busy) return;
    busy = true;
    while (queue.length) {
      const item = queue.shift();
      if (item.my !== gen) continue;
      const b = await item.audio;
      if (item.my !== gen) continue;
      if (b) await playBuffer(b); else await browserVoice(item.text);
    }
    busy = false;
    orb.setState("idle");
  }
  function stop() {
    gen++; queue = []; buf = "";
    if (playing) playing.stop();
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  }

  // replies stream in as text deltas: speak each sentence as soon as it's complete
  function feed(delta) {
    if (!speakReplies) return;
    buf += delta;
    for (;;) {
      if ((buf.match(/```/g) || []).length % 2) return;            // inside a code block: wait for its end
      const m = /^([\s\S]+?[.!?…:;])(\s+)/.exec(buf);
      if (!m) {
        if (buf.length > 180 && buf.indexOf(",", 70) > 0) { const cut = buf.indexOf(",", 70) + 1; say(buf.slice(0, cut)); buf = buf.slice(cut); continue; }
        return;
      }
      say(m[1]); buf = buf.slice(m[0].length);
    }
  }
  function finish() { if (speakReplies && buf.trim()) say(buf); buf = ""; }

  // ---- the mic: record on the phone, transcribe on the PC
  let rec = null;
  async function listen() {
    if (rec) return rec.finish();                                     // tap again = done talking
    stop();
    unlock();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch {
      toast("Allow the microphone for Jarvis (iPhone: Settings → Safari → Microphone, or the prompt).", "warn", 8000);
      return;
    }
    const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg"].find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
    const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    // end of speech: quiet for 1.1 s after talking, or 20 s total, or nothing said within 7 s
    const c = audio(), srcNode = c.createMediaStreamSource(stream), an = c.createAnalyser();
    an.fftSize = 1024; srcNode.connect(an);
    const data = new Float32Array(an.fftSize);
    const t0 = performance.now();
    let spoke = false, quietSince = 0, raf = 0, done = false;
    const finishRec = () => { if (done) return; done = true; cancelAnimationFrame(raf); if (mr.state !== "inactive") mr.stop(); };
    const tick = () => {
      an.getFloatTimeDomainData(data);
      let s = 0; for (const v of data) s += v * v;
      const rms = Math.sqrt(s / data.length), now = performance.now();
      orb.setLevel(Math.min(1, rms * 8));
      if (rms > 0.035) { spoke = true; quietSince = 0; } else if (spoke && !quietSince) quietSince = now;
      if ((spoke && quietSince && now - quietSince > 1100) || now - t0 > 20000 || (!spoke && now - t0 > 7000)) return finishRec();
      raf = requestAnimationFrame(tick);
    };
    rec = { finish: finishRec };
    orb.setState("listening");
    document.body.classList.add("recording");
    mr.onstop = async () => {
      rec = null;
      stream.getTracks().forEach((t) => t.stop());
      try { srcNode.disconnect(); } catch { /* fine */ }
      document.body.classList.remove("recording");
      orb.setLevel(0);
      if (!spoke) { orb.setState("idle"); return; }
      orb.setState("thinking");
      const blob = new Blob(chunks, { type: mr.mimeType || mime || "audio/webm" });
      try {
        const r = await fetch("/api/voice/transcribe", { method: "POST", body: blob,
          headers: { "content-type": blob.type, "x-jarvis-token": token } });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || "transcription failed");
        if (!j.text) { orb.setState("idle"); toast("Didn't catch that."); return; }
        speakReplies = true;                                          // asked by voice: answer out loud
        onText(j.text);
      } catch (e) { orb.setState("idle"); toast(`Voice: ${e.message}`, "error", 6000); }
    };
    mr.start(250);
    raf = requestAnimationFrame(tick);
  }

  // ---- the phone's GPS -> Jarvis, while the app is open (and the map asks for every fix while navigating)
  let watch = null, last = null;
  function gpsOn() {
    if (watch != null || !navigator.geolocation) return;
    watch = navigator.geolocation.watchPosition((g) => {
      const c = g.coords, now = Date.now();
      const moved = last ? Math.hypot((c.latitude - last.lat) * 111000, (c.longitude - last.lon) * 111000 * Math.cos(c.latitude * Math.PI / 180)) : 1e9;
      if (last && moved < 15 && now - last.t < 60000) return;
      last = { lat: c.latitude, lon: c.longitude, t: now };
      api("/api/geo/fix", { method: "POST", body: { lat: c.latitude, lon: c.longitude, acc: c.accuracy, heading: c.heading } }).catch(() => {});
    }, () => { watch = null; }, { enableHighAccuracy: true, maximumAge: 15000, timeout: 30000 });
  }
  function gpsOff() { if (watch != null) navigator.geolocation.clearWatch(watch); watch = null; }
  document.addEventListener("visibilitychange", () => (document.hidden ? gpsOff() : gpsOn()));
  // start right away when location was allowed before; otherwise the first map open / mic tap asks
  try {
    navigator.permissions?.query({ name: "geolocation" }).then((p) => { if (p.state === "granted") gpsOn(); }).catch(() => {});
  } catch { /* old browser */ }

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

  // ---- notifications on this phone (asked from a tap: Settings → Phone notifications)
  async function enablePush() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      throw new Error(/iPhone|iPad/.test(navigator.userAgent) ? "On iPhone: add Jarvis to the Home Screen first and open it from there (iOS 16.4+)." : "This browser can't do notifications.");
    }
    if ((await Notification.requestPermission()) !== "granted") throw new Error("Notifications weren't allowed.");
    const reg = await navigator.serviceWorker.ready;
    const { key } = await api("/api/push");
    const raw = Uint8Array.from(atob(key.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((key.length + 3) % 4)), (c) => c.charCodeAt(0));
    const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: raw });
    return api("/api/push/subscribe", { method: "POST", body: sub.toJSON() });
  }

  return {
    listen, say, stop, feed, finish, gpsOn, enablePush,
    typed() { speakReplies = false; stop(); },             // typed questions get a typed answer (no surprise audio)
    get speaking() { return busy; },
  };
}
