// Hand control (ported from Jarvis v1's holo mode). The webcam tracks both hands - MediaPipe hand landmarks, run in
// this page, nothing leaves the computer - and draws them as glowing wireframes over the app. Then:
//   pinch the orb                 grab Jarvis and spin him
//   pinch-drag on the 3D part     turn it; two hands pinching on it: pull apart / together to zoom
//   quick pinch (air tap)         press whatever's under your fingers (buttons, cards, list items)
//   pinch-drag elsewhere          scroll the list under your fingers
//   open palm, swipe              close the screen that's open (or open the conversation history)
//   open palm, held still         stop: cuts Jarvis off mid-sentence / stops what he's doing
// Turn it on with the hand button in the top bar, by asking Jarvis, or press Esc to turn it off.
const VISION = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14";
const MODEL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task";
const EDGES = [[0, 1], [1, 2], [2, 3], [3, 4], [0, 5], [5, 6], [6, 7], [7, 8], [5, 9], [9, 10], [10, 11], [11, 12], [9, 13],
  [13, 14], [14, 15], [15, 16], [13, 17], [17, 18], [18, 19], [19, 20], [0, 17]];

export function createHolo({ toast, stop: stopJarvis }) {
  const body = document.body;
  const H = { on: false, starting: false };
  let video, stream, landmarker, cv, ctx, raf = 0, lastVideoT = -1;
  const hands = [];
  let two = null;

  async function start() {
    if (H.on || H.starting) return;
    H.starting = true;
    toast("Starting hand control…");
    try {
      video = Object.assign(document.createElement("video"), { playsInline: true, muted: true, autoplay: true });
      for (let i = 0; i < 6 && !stream; i++) {        // Jarvis's own camera use lets the webcam go within a moment
        try { stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, facingMode: "user" } }); }
        catch (e) { if (i === 5) throw e; await new Promise((r) => setTimeout(r, 700)); }
      }
      video.srcObject = stream; await video.play();
      const { FilesetResolver, HandLandmarker } = await import(`${VISION}/vision_bundle.mjs`);
      const files = await FilesetResolver.forVisionTasks(`${VISION}/wasm`);
      landmarker = await HandLandmarker.createFromOptions(files, {
        baseOptions: { modelAssetPath: MODEL, delegate: "GPU" }, runningMode: "VIDEO", numHands: 2,
        minHandDetectionConfidence: 0.6, minHandPresenceConfidence: 0.6, minTrackingConfidence: 0.5 });
      cv = document.createElement("canvas"); cv.id = "holo"; body.appendChild(cv); ctx = cv.getContext("2d");
      size(); addEventListener("resize", size);
      H.on = true; body.classList.add("holoOn");
      toast("Hand control on · pinch = grab / tap · pinch-drag = scroll · palm swipe = close · palm still = stop · Esc to exit", "info", 7000);
      loop();
    } catch (e) {
      stop();
      toast(`Hand control couldn't start: ${e.message || e}`, "error", 6000);
    } finally { H.starting = false; }
  }
  function stop() {
    H.on = false; body.classList.remove("holoOn");
    cancelAnimationFrame(raf);
    for (const h of hands) release(h);
    hands.length = 0;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    if (landmarker) { try { landmarker.close(); } catch { /* gone */ } landmarker = null; }
    if (cv) { cv.remove(); cv = null; }
    document.querySelectorAll(".holoHover").forEach((e) => e.classList.remove("holoHover"));
  }
  const toggle = () => (H.on ? (stop(), toast("Hand control off")) : start());
  addEventListener("keydown", (e) => { if (e.key === "Escape" && H.on) toggle(); });
  function size() { if (!cv) return; const d = Math.min(devicePixelRatio || 1, 1.5); cv.width = innerWidth * d; cv.height = innerHeight * d; ctx.setTransform(d, 0, 0, d, 0, 0); }

  // the camera faces you: mirror x; the middle 80% of the frame covers the whole screen so the edges are reachable
  const toScreen = (p) => [Math.min(1, Math.max(0, (1 - p.x - 0.1) / 0.8)) * innerWidth, Math.min(1, Math.max(0, (p.y - 0.1) / 0.8)) * innerHeight];
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const extended = (lm, tip, pip) => dist(lm[tip], lm[0]) > dist(lm[pip], lm[0]) * 1.12;

  function loop() {
    raf = requestAnimationFrame(loop);
    if (!landmarker || video.readyState < 2 || video.currentTime === lastVideoT) return;
    lastVideoT = video.currentTime;
    process(landmarker.detectForVideo(video, performance.now()).landmarks || []);
  }
  function process(found) {
    const next = found.map((lm) => {               // keep each pinch with its own hand (nearest wrist)
      const w = toScreen(lm[0]);
      let best = null, bd = 1e9;
      for (const h of hands) { const d = Math.hypot(h.wrist[0] - w[0], h.wrist[1] - w[1]); if (d < bd && !h._taken) { bd = d; best = h; } }
      if (best && bd < innerWidth * 0.25) { best._taken = true; return update(best, lm); }
      return update({ id: Math.random(), pinch: false, pts: null, cursor: null, wrist: w, hist: [] }, lm);
    });
    for (const h of hands) { if (!next.includes(h)) release(h); }
    hands.length = 0; next.forEach((h) => { h._taken = false; hands.push(h); });
    twoHands();
    draw();
  }
  function update(h, lm) {
    const t = performance.now(), raw = lm.map(toScreen);
    h.pts = h.pts ? h.pts.map((p, i) => [p[0] + (raw[i][0] - p[0]) * 0.55, p[1] + (raw[i][1] - p[1]) * 0.55]) : raw;
    h.wrist = h.pts[0];
    const scale = dist(lm[0], lm[9]) || 0.1, pinchD = dist(lm[4], lm[8]) / scale, was = h.pinch;
    h.pinch = was ? pinchD < 0.5 : pinchD < 0.32;                  // hysteresis: no flicker at the threshold
    h.cursor = [(h.pts[4][0] + h.pts[8][0]) / 2, (h.pts[4][1] + h.pts[8][1]) / 2];
    h.palm = !h.pinch && [[8, 6], [12, 10], [16, 14], [20, 18]].every(([a, b]) => extended(lm, a, b));
    h.hist.push([t, h.cursor[0], h.cursor[1]]); h.hist = h.hist.filter((q) => t - q[0] < 350);
    if (!was && h.pinch) pinchStart(h);
    else if (was && h.pinch) pinchMove(h);
    else if (was && !h.pinch) pinchEnd(h);
    else hover(h);
    palmGestures(h, t);
    return h;
  }

  const orbCanvas = () => document.getElementById("orb");
  const underCursor = (h) => document.elementFromPoint(h.cursor[0], h.cursor[1]);
  function ptr(type, h, target) {
    target.dispatchEvent(new PointerEvent(type, { clientX: h.cursor[0], clientY: h.cursor[1], pointerId: 700 + (h.id * 100 | 0),
      pointerType: "touch", bubbles: true, isPrimary: true, button: 0, buttons: type === "pointerup" ? 0 : 1 }));
  }
  function pinchStart(h) {
    const el = underCursor(h);
    h.t0 = performance.now(); h.moved = 0; h.target = el; h.mode = "tap";
    if (el && el === orbCanvas()) { h.mode = "orb"; ptr("pointerdown", h, el); }
    else if (el && el.closest && el.closest("#mv3d") && window.JarvisModelView) h.mode = "model";
    else if (el) h.scroller = scrollerOf(el);
    flash(h.cursor, h.mode === "orb" ? 26 : 16);
  }
  function pinchMove(h) {
    const prev = h.last || h.cursor; h.last = h.cursor.slice();
    const dx = h.cursor[0] - prev[0], dy = h.cursor[1] - prev[1];
    h.moved += Math.hypot(dx, dy);
    if (h.mode === "orb") ptr("pointermove", h, orbCanvas());
    else if (h.mode === "model" && !two) orbit((r, th, ph) => [r, th - dx * 0.008, ph - dy * 0.008]);
    else if (h.moved > 30 && h.scroller) { h.mode = "scroll"; h.scroller.scrollBy({ top: -dy * 1.6, left: -dx * 1.6 }); }
  }
  function pinchEnd(h) {
    if (h.mode === "orb") { ptr("pointerup", h, orbCanvas()); }
    else if (h.mode === "tap" && performance.now() - h.t0 < 450 && h.moved < 40) tap(h);
    h.mode = null; h.last = null; h.scroller = null;
  }
  function release(h) { if (h.pinch && h.mode === "orb") ptr("pointerup", h, orbCanvas()); h.pinch = false; }
  function tap(h) {
    const el = h.target && (h.target.closest("button, a, [role=button], .hits li, .tc, input, textarea, select, label, summary") || h.target);
    if (!el || el === body || el === document.documentElement || el === orbCanvas()) return;
    flash(h.cursor, 34, true);
    if (el.matches("input, textarea")) el.focus(); else el.click();
  }
  function scrollerOf(el) {
    for (let e = el; e && e !== body; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (/(auto|scroll)/.test(cs.overflowY + cs.overflowX) && (e.scrollHeight > e.clientHeight + 4 || e.scrollWidth > e.clientWidth + 4)) return e;
    }
    return null;
  }
  let hovered = null;
  function hover(h) {
    const el = underCursor(h), c = el && el.closest && el.closest("button, a, .hits li, summary");
    if (c !== hovered) { hovered && hovered.classList.remove("holoHover"); hovered = c; c && c.classList.add("holoHover"); }
  }
  function orbit(fn) {
    const v = window.JarvisModelView; if (!v || !v.camera || !v.controls) return;
    const c = v.camera.position, t = v.controls.target;
    const ox = c.x - t.x, oy = c.y - t.y, oz = c.z - t.z;
    let r = Math.hypot(ox, oy, oz), th = Math.atan2(ox, oz), ph = Math.acos(Math.max(-1, Math.min(1, oy / r)));
    [r, th, ph] = fn(r, th, ph);
    ph = Math.max(0.05, Math.min(Math.PI - 0.05, ph));
    c.set(t.x + r * Math.sin(ph) * Math.sin(th), t.y + r * Math.cos(ph), t.z + r * Math.sin(ph) * Math.cos(th));
    v.camera.lookAt(t); v.controls.update(); v.autoFit = null;
  }
  function twoHands() {
    const p = hands.filter((h) => h.pinch);
    if (p.length < 2) { two = null; return; }
    const d = Math.hypot(p[0].cursor[0] - p[1].cursor[0], p[0].cursor[1] - p[1].cursor[1]);
    if (!two) { two = { dPrev: d }; return; }
    if (body.dataset.mode === "model") { const k = two.dPrev / d; orbit((r, th, ph) => [Math.max(5, r * k), th, ph]); two.dPrev = d; }
  }
  function palmGestures(h, t) {
    if (!h.palm) { h.palmSince = 0; return; }
    const hs = h.hist;
    if (hs.length > 3 && !h.swiped) {
      const [t0, x0] = hs[0], [t1, x1] = hs[hs.length - 1], v = (x1 - x0) / Math.max(1, t1 - t0) * 1000;
      if (Math.abs(v) > innerWidth * 1.6) { h.swiped = t; swipe(v > 0 ? 1 : -1); }
    }
    if (h.swiped && t - h.swiped > 900) h.swiped = 0;
    h.palmSince = h.palmSince || t;
    const still = hs.length > 3 && Math.hypot(hs[hs.length - 1][1] - hs[0][1], hs[hs.length - 1][2] - hs[0][2]) < 25;
    if (!still) h.palmSince = t;
    if (t - h.palmSince > 900 && !h.stopped) { h.stopped = true; stopJarvis(); toast("✋ Stopped"); setTimeout(() => { h.stopped = false; }, 2500); }
  }
  function swipe(dir) {
    flash([dir > 0 ? innerWidth * 0.8 : innerWidth * 0.2, innerHeight / 2], 60, true);
    const close = document.querySelector("body[data-mode='hub'] #hubClose, body[data-mode='map'] #mapClose, " +
      "body[data-mode='camera'] #camClose, body[data-mode='model'] #mvClose, body[data-mode='study'] #studyClose");
    if (close) close.click();
    else if (!document.getElementById("history").classList.contains("hidden")) document.querySelector("[data-close=history]").click();
    else document.getElementById("btnHistory").click();
  }

  const flashes = [];
  function flash(p, r, strong) { flashes.push({ x: p[0], y: p[1], r, t: performance.now(), strong }); }
  function draw() {
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    ctx.globalCompositeOperation = "lighter"; ctx.lineCap = "round";
    const c = (a) => `rgba(255,178,107,${a})`;
    for (const h of hands) {
      const P = h.pts;
      for (const [w, a] of [[9, 0.12], [2.2, 0.85]]) {
        ctx.strokeStyle = c(a); ctx.lineWidth = w; ctx.beginPath();
        for (const [i, j] of EDGES) { ctx.moveTo(P[i][0], P[i][1]); ctx.lineTo(P[j][0], P[j][1]); }
        ctx.stroke();
      }
      P.forEach(([x, y], i) => {
        const tip = [4, 8, 12, 16, 20].includes(i);
        ctx.fillStyle = c(tip ? 0.95 : 0.6); ctx.beginPath(); ctx.arc(x, y, tip ? 4.5 : 3, 0, 6.283); ctx.fill();
        if (tip) { ctx.fillStyle = c(0.12); ctx.beginPath(); ctx.arc(x, y, 14, 0, 6.283); ctx.fill(); }
      });
      const [x, y] = h.cursor, k = h.pinch ? 1 : 0.35;
      ctx.strokeStyle = c(0.9); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, h.pinch ? 10 : 18, 0, 6.283 * k); ctx.stroke();
      if (h.pinch) { ctx.fillStyle = c(0.35); ctx.beginPath(); ctx.arc(x, y, 10, 0, 6.283); ctx.fill(); }
      if (h.palm) { ctx.strokeStyle = c(0.35); ctx.setLineDash([4, 6]); ctx.beginPath(); ctx.arc(P[9][0], P[9][1], 46, 0, 6.283); ctx.stroke(); ctx.setLineDash([]); }
    }
    const p = hands.filter((h) => h.pinch);
    if (p.length === 2) {
      const [a, b] = [p[0].cursor, p[1].cursor], g = ctx.createLinearGradient(a[0], a[1], b[0], b[1]);
      g.addColorStop(0, c(0.9)); g.addColorStop(0.5, c(0.25)); g.addColorStop(1, c(0.9));
      ctx.strokeStyle = g; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(a[0], a[1]);
      for (let i = 1; i < 12; i++) { const f = i / 12; ctx.lineTo(a[0] + (b[0] - a[0]) * f + (Math.random() - 0.5) * 10, a[1] + (b[1] - a[1]) * f + (Math.random() - 0.5) * 10); }
      ctx.lineTo(b[0], b[1]); ctx.stroke();
    }
    const now = performance.now();
    for (let i = flashes.length - 1; i >= 0; i--) {
      const f = flashes[i], age = (now - f.t) / 450;
      if (age > 1) { flashes.splice(i, 1); continue; }
      ctx.strokeStyle = c((1 - age) * (f.strong ? 0.9 : 0.6)); ctx.lineWidth = f.strong ? 3 : 2;
      ctx.beginPath(); ctx.arc(f.x, f.y, f.r * (1 + age * 1.8), 0, 6.283); ctx.stroke();
    }
    ctx.globalCompositeOperation = "source-over";
  }

  // for testing without a camera: feed landmark sets straight in (the same path the camera's go through)
  const feed = (lms) => { if (!cv) { cv = document.createElement("canvas"); cv.id = "holo"; body.appendChild(cv); ctx = cv.getContext("2d"); size(); }
    process(lms); return hands.map((h) => ({ pinch: h.pinch, palm: h.palm, mode: h.mode, cursor: h.cursor.map(Math.round) })); };
  return { start, stop, toggle, feed, get on() { return H.on; } };
}
