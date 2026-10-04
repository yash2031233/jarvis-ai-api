// The Jarvis orb — procedural amber "molecular circuit" sphere.
//
// Everything is generated in the reference image's own coordinate space (1920×1920),
// baked once into per-layer textures (sharp lines + glow), then composited every frame
// with cheap transforms. That keeps it pixel-faithful to the reference AND 60 fps.
//
// Tunable parameters live in ORB_CONFIG; the seed makes the orb identical on every launch.

export const ORB_CONFIG = {
  seed: 7331030,
  size: 1920,
  center: [1010, 962],
  color: [246, 176, 122],       // line color (additive)
  lineAlpha: 0.52,
  lineWidth: 2.0,
  glow: [[3, 0.32], [12, 0.16], [40, 0.09]], // [blur px, alpha] passes
  glowColorBoost: [1.0, 0.72, 0.42],
  hotspot: { x: 1265, y: 290, r: 250, gain: 0.55 }, // whitening region (top-right rim)
  seg: 9.5,                    // bond length
  zig: 30,                     // zig-zag half angle (deg)
  outer: {
    center: [1003, 975], squash: [1, 1.04], chains: 585, tiers: 5, blockDeg: 9, gap: 0.45,
    len: [5, 18], zig: 24, rung: 0.06, guides: 9,
    // [fromDeg, toDeg, innerR, outerR, density]  (0° = right, 90° = down, -90° = up)
    profile: [
      [-200, -150, 581, 666, 0.25],   // upper-left (thin)
      [-150, -105, 611, 736, 0.85],   // top-left
      [-105, -45, 616, 741, 1.0],     // top
      [-45, 15, 566, 676, 0.95],      // right-top → right
      [15, 60, 571, 706, 0.9],        // right-bottom
      [60, 120, 636, 786, 1.0],       // bottom
      [120, 160, 621, 746, 0.55],     // lower-left
    ],
  },
  arcs: { count: 34 },
  surface: { chains: 686 },
  core: { cx: 962, cy: 978, r: 330, rings: 60, chains: 480 },
  ring: { cx: 960, cy: 992, a: 655, b: 176, tilt: -0.4, chains: 44 },
  streak: { from: [788, 672], to: [1240, 1402], lines: 16 },
  comet: { from: [1305, 1132], to: [1575, 1035], chains: 26 },
};

// ------------------------------------------------------------------ utilities
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const D2R = Math.PI / 180;

class Layer {
  constructor(name) { this.name = name; this.lines = []; this.dots = []; }
  line(pts, a = 1) { if (pts.length > 1) this.lines.push({ pts, a }); }
  dot(x, y, r, a = 1) { this.dots.push({ x, y, r, a }); }
}

// Molecular chain generator: zig-zag bonds following a guide direction field,
// with branches, occasional hexagon rings and dot terminals.
class Chemist {
  constructor(rng, cfg) { this.r = rng; this.cfg = cfg; }
  rand(a, b) { return a + (b - a) * this.r(); }
  pick(arr) { return arr[Math.floor(this.r() * arr.length)]; }

  chain(layer, x, y, opts) {
    const o = Object.assign({
      steps: 12, seg: this.cfg.seg, zig: this.cfg.zig, dir: null, // dir(x,y) -> angle (rad)
      turn: 0.06, branch: 0.07, hex: 0.0, dotEnd: 0.45, dotBranch: 0.6, alpha: 1,
      keep: null, wobble: 0.08, maxBranchDepth: 1,
    }, opts);
    this._chain(layer, x, y, o, 0, this.r() * Math.PI * 2);
  }

  _chain(layer, x, y, o, depth, fallbackAngle) {
    const pts = [[x, y]];
    let base = o.dir ? o.dir(x, y) : fallbackAngle;
    let bias = 0;
    let side = this.r() < 0.5 ? 1 : -1;
    let cx = x, cy = y;
    const steps = Math.max(2, Math.round(o.steps * this.rand(0.6, 1.4)));
    for (let i = 0; i < steps; i++) {
      if (o.dir) base = o.dir(cx, cy) + bias;
      if (this.r() < o.turn) bias += (this.r() < 0.5 ? 1 : -1) * 60 * D2R * (this.r() < 0.7 ? 1 : 0.5);
      bias *= 0.92;
      const ang = base + side * o.zig * D2R + (this.r() - 0.5) * o.wobble;
      side = -side;
      const len = o.seg * this.rand(0.85, 1.15);
      const nx = cx + Math.cos(ang) * len, ny = cy + Math.sin(ang) * len;
      if (o.keep && !o.keep(nx, ny)) break;
      cx = nx; cy = ny;
      pts.push([cx, cy]);
      if (depth < o.maxBranchDepth && this.r() < o.branch) {
        const bdir = ang + (this.r() < 0.5 ? 1 : -1) * (60 + this.rand(-10, 10)) * D2R;
        const sub = Object.assign({}, o, { steps: Math.max(2, o.steps * 0.25), dir: null, turn: 0.05 });
        const end = this._branch(layer, cx, cy, bdir, sub);
        if (end && this.r() < o.dotBranch) layer.dot(end[0], end[1], this.rand(1.8, 2.7), o.alpha);
      }
      if (this.r() < o.hex) this._hexagon(layer, cx, cy, ang, o);
    }
    layer.line(pts, o.alpha);
    if (this.r() < o.dotEnd) layer.dot(cx, cy, this.rand(1.8, 2.8), o.alpha);
    return [cx, cy];
  }

  _branch(layer, x, y, dir, o) {
    const pts = [[x, y]];
    let cx = x, cy = y, side = this.r() < 0.5 ? 1 : -1;
    const n = 1 + Math.floor(this.r() * Math.max(1, o.steps));
    for (let i = 0; i < n; i++) {
      const ang = dir + side * o.zig * D2R;
      side = -side;
      cx += Math.cos(ang) * o.seg; cy += Math.sin(ang) * o.seg;
      if (o.keep && !o.keep(cx, cy)) break;
      pts.push([cx, cy]);
    }
    layer.line(pts, o.alpha);
    return pts.length > 1 ? [cx, cy] : null;
  }

  _hexagon(layer, x, y, ang, o) {
    // benzene ring hanging off the current atom
    const s = o.seg;
    const start = ang + (this.r() < 0.5 ? 1 : -1) * 60 * D2R;
    const pts = [[x, y]];
    let cx = x, cy = y, a = start;
    for (let i = 0; i < 6; i++) {
      cx += Math.cos(a) * s; cy += Math.sin(a) * s;
      pts.push([cx, cy]);
      a += 60 * D2R;
    }
    layer.line(pts, o.alpha);
  }
}

// ------------------------------------------------------------------ geometry builders
function buildLayers(cfg) {
  const rng = mulberry32(cfg.seed);
  const chem = new Chemist(rng, cfg);
  const [CX, CY] = cfg.center;
  const L = {};

  // ---- 1. outer fragmented shell: a thick C-shaped band (top → right → bottom) of short
  //         wavy chains + radial rungs, broken into blocks; thin/absent on the left.
  {
    const lay = (L.outer = new Layer("outer"));
    const o = cfg.outer;
    const [ox, oy] = o.center;
    // band inner/outer radius as a function of screen angle (deg, 0 = right, 90 = down)
    const bandAt = (deg) => {
      for (const k of o.profile) if (deg >= k[0] && deg < k[1]) return k;
      return null;
    };
    const blocks = new Map(); // fragmentation: (angle bin, tier) -> on/off
    const blockOn = (deg, tier) => {
      const key = Math.floor((deg + 360) / o.blockDeg) * 10 + tier;
      if (!blocks.has(key)) blocks.set(key, rng() > o.gap);
      return blocks.get(key);
    };
    const polar = (deg, rad) => [ox + Math.cos(deg * D2R) * rad * o.squash[0], oy + Math.sin(deg * D2R) * rad * o.squash[1]];
    let made = 0, guard = 0;
    while (made < o.chains && guard++ < o.chains * 20) {
      const deg = chem.rand(-200, 160);
      const band = bandAt(deg);
      if (!band) continue;
      const [, , rin, rout, dens] = band;
      if (rng() > dens) continue;
      const u = rng();
      const rad = rin + (rout - rin) * u;
      const tier = Math.min(o.tiers - 1, Math.floor(u * o.tiers));
      if (!blockOn(deg, tier)) continue;
      made++;
      const dirSign = rng() < 0.5 ? 1 : -1;
      const n = Math.round(chem.rand(o.len[0], o.len[1]));
      const pts = [];
      let d = deg, r = rad, side = 1;
      const zigR = cfg.seg * Math.sin(o.zig * D2R);
      for (let k = 0; k < n; k++) {
        const [x, y] = polar(d, r + side * zigR * chem.rand(0.5, 1.0));
        pts.push([x, y]);
        side = -side;
        d += dirSign * (cfg.seg * Math.cos(o.zig * D2R) / r) / D2R * chem.rand(0.8, 1.2);
        if (rng() < 0.08) r += chem.rand(-1, 1) * cfg.seg;               // step to a neighbouring lane
        if (rng() < o.rung && k > 1) {                                   // radial rung (ladder look)
          const rungLen = Math.round(chem.rand(2, 5));
          const rp = [[x, y]];
          let rr = r, s2 = 1;
          const dirR = rng() < 0.5 ? 1 : -1;
          for (let j = 0; j < rungLen; j++) {
            rr += dirR * cfg.seg * Math.cos(o.zig * D2R);
            rp.push(polar(d + s2 * 0.25, rr));
            s2 = -s2;
          }
          lay.line(rp, 0.85);
          if (rng() < 0.35) lay.dot(rp[rp.length - 1][0], rp[rp.length - 1][1], chem.rand(1.8, 2.6), 0.9);
        }
      }
      lay.line(pts, chem.rand(0.55, 0.9));
      if (rng() < 0.3) lay.dot(pts[pts.length - 1][0], pts[pts.length - 1][1], chem.rand(1.8, 2.8), 0.9);
    }
    // a few long smooth arcs riding along the band (the faint guide lines in the reference)
    for (let i = 0; i < o.guides; i++) {
      const deg0 = chem.rand(-190, 100), span = chem.rand(25, 70);
      const band = bandAt(deg0);
      if (!band) continue;
      const rad = chem.rand(band[2], band[3]);
      const pts = [];
      for (let d = deg0; d < deg0 + span; d += 0.6) pts.push(polar(d, rad));
      lay.line(pts, chem.rand(0.5, 0.9));
    }
  }

  // ---- 2. smooth concentric arcs (thin, slightly offset)
  {
    const lay = (L.arcs = new Layer("arcs"));
    const groups = [
      { from: 200, to: 300, r: [612, 668], n: 9 },   // top
      { from: 300, to: 395, r: [618, 672], n: 8 },   // right
      { from: 108, to: 168, r: [604, 652], n: 6 },   // lower-left
      { from: 168, to: 214, r: [608, 640], n: 4 },   // left
      { from: 20, to: 85, r: [606, 640], n: 4 },     // lower-right
    ];
    for (const g of groups) {
      for (let i = 0; i < g.n; i++) {
        const r = chem.rand(g.r[0], g.r[1]);
        const a0 = chem.rand(g.from, g.from + (g.to - g.from) * 0.4) * D2R;
        const a1 = chem.rand(g.from + (g.to - g.from) * 0.6, g.to) * D2R;
        const ox = chem.rand(-14, 14), oy = chem.rand(-10, 10);
        const pts = [];
        for (let a = a0; a <= a1; a += 0.01) pts.push([CX + ox + Math.cos(a) * r, CY + oy + Math.sin(a) * r]);
        lay.line(pts, chem.rand(0.55, 1));
      }
    }
  }

  // ---- 3. sparse molecular chains on the sphere surface
  {
    const lay = (L.surface = new Layer("surface"));
    const Rin = cfg.core.r * 1.02, Rout = 640;
    const inside = (x, y) => {
      const d = Math.hypot(x - CX, y - CY);
      const dc = Math.hypot(x - cfg.core.cx, y - cfg.core.cy);
      return d < Rout && dc > Rin * 0.97;
    };
    for (let i = 0; i < cfg.surface.chains; i++) {
      let x, y, tries = 0;
      do {
        const a = rng() * Math.PI * 2;
        const rr = Math.sqrt(chem.rand(0.08, 1)) * Rout;
        x = CX + Math.cos(a) * rr; y = CY + Math.sin(a) * rr; tries++;
      } while (!inside(x, y) && tries < 20);
      if (!inside(x, y)) continue;
      const d = Math.hypot(x - CX, y - CY) / Rout;
      const axis = rng() < 0.55 ? (rng() < 0.5 ? Math.PI / 2 : -Math.PI / 2) : (rng() < 0.5 ? 0 : Math.PI);
      const tangential = rng() < 0.3;
      chem.chain(lay, x, y, {
        steps: chem.rand(6, 22),
        dir: tangential ? (px, py) => Math.atan2(py - CY, px - CX) + Math.PI / 2 : () => axis,
        turn: 0.12, branch: 0.12, hex: 0.01, dotEnd: 0.32, dotBranch: 0.4, keep: inside,
        alpha: 0.75 + 0.25 * d,
      });
    }
  }

  // ---- 4. inner core: concentric rings + dense honeycomb chains (tunnel look)
  {
    const lay = (L.core = new Layer("core"));
    const { cx, cy, r, rings, chains } = cfg.core;
    for (let i = 0; i < rings; i++) {
      const rr = chem.rand(r * 0.2, r * 1.0);
      const ox = chem.rand(-8, 8), oy = chem.rand(-6, 6);
      let a = rng() * Math.PI * 2;
      const span = chem.rand(2.4, 6.28);
      const pts = [];
      for (let t = 0; t < span; t += 0.012) pts.push([cx + ox + Math.cos(a + t) * rr, cy + oy + Math.sin(a + t) * rr]);
      lay.line(pts, chem.rand(0.28, 0.6));
    }
    // edge rings (the bright rim of the core)
    for (let i = 0; i < 3; i++) {
      const rr = r * chem.rand(0.94, 1.03);
      const a = rng() * Math.PI * 2, span = chem.rand(2.5, 5.5);
      const pts = [];
      for (let t = 0; t < span; t += 0.01) pts.push([cx + Math.cos(a + t) * rr, cy + Math.sin(a + t) * rr * 0.995]);
      lay.line(pts, 0.55);
    }
    const inCore = (x, y) => {
      const d = Math.hypot(x - cx, y - cy);
      return d < r * 1.01 && d > 14;
    };
    for (let i = 0; i < chains; i++) {
      const a = rng() * Math.PI * 2;
      const rr = Math.sqrt(chem.rand(0.02, 1)) * r;
      const x = cx + Math.cos(a) * rr, y = cy + Math.sin(a) * rr;
      chem.chain(lay, x, y, {
        steps: chem.rand(5, 20), seg: cfg.seg * 0.92,
        dir: (px, py) => Math.atan2(py - cy, px - cx) + Math.PI / 2,
        turn: 0.16, branch: 0.1, hex: 0.08, dotEnd: 0.05, dotBranch: 0.05, keep: inCore,
        alpha: 0.62,
      });
    }
  }

  // ---- 5. equatorial ring (Saturn-like, extends past the sphere)
  {
    const lay = (L.ring = new Layer("ring"));
    const { cx, cy, a, b, tilt, chains } = cfg.ring;
    const ct = Math.cos(tilt * D2R), st = Math.sin(tilt * D2R);
    const ell = (t, da, db) => {
      const x = (a + da) * Math.cos(t), y = -(b + db) * Math.sin(t);
      return [cx + x * ct - y * st, cy + x * st + y * ct];
    };
    for (let i = 0; i < chains; i++) {
      // most chains cover the middle; some reach the frayed tips
      const t0 = chem.rand(0.02, 2.2), t1 = Math.min(Math.PI - 0.02, t0 + chem.rand(0.5, 2.6));
      const da = chem.rand(-30, 22), db = chem.rand(-30, 42);
      const pts = [];
      let side = 1;
      const amp = cfg.seg * Math.sin(cfg.zig * D2R) * chem.rand(0.5, 1.0);
      for (let t = t0; t < t1; t += cfg.seg / (a * 0.9) * chem.rand(0.8, 1.1)) {
        const [x, y] = ell(t, da, db);
        // normal offset for zig-zag
        const [x2, y2] = ell(t + 0.001, da, db);
        const nx = -(y2 - y), ny = x2 - x, nl = Math.hypot(nx, ny) || 1;
        pts.push([x + (nx / nl) * amp * side, y + (ny / nl) * amp * side]);
        side = -side;
      }
      lay.line(pts, chem.rand(0.42, 0.75));
      if (rng() < 0.25 && pts.length) lay.dot(pts[0][0], pts[0][1], chem.rand(2.2, 3));
    }
    // frayed tips: short chains peeling off the ends
    for (const [t, dirSign] of [[Math.PI - 0.03, -1], [0.04, 1]]) {
      for (let i = 0; i < 6; i++) {
        const [x, y] = ell(t + chem.rand(-0.05, 0.08) * -dirSign, chem.rand(-20, 10), chem.rand(-20, 20));
        chem.chain(lay, x, y, { steps: chem.rand(3, 9), dir: () => (dirSign < 0 ? Math.PI : 0) + chem.rand(-0.5, 0.5),
          turn: 0.2, branch: 0.1, dotEnd: 0.3, alpha: 0.6 });
      }
    }
  }

  // ---- 6. diagonal axis streak
  {
    const lay = (L.streak = new Layer("streak"));
    const { from, to, lines } = cfg.streak;
    const dx = to[0] - from[0], dy = to[1] - from[1], len = Math.hypot(dx, dy);
    const nx = -dy / len, ny = dx / len;
    for (let i = 0; i < lines; i++) {
      const off = chem.rand(-11, 11), skew = chem.rand(-8, 8);
      const s0 = chem.rand(0, 0.06), s1 = chem.rand(0.93, 1);
      const pts = [];
      for (let s = s0; s <= s1; s += 0.01) {
        const o = off + skew * (s - 0.5);
        pts.push([from[0] + dx * s + nx * o, from[1] + dy * s + ny * o]);
      }
      lay.line(pts, chem.rand(0.7, 1));
    }
  }

  // ---- 7. comet fragment (right side, just below the equator)
  {
    const lay = (L.comet = new Layer("comet"));
    const { from, to, chains } = cfg.comet;
    const ang = Math.atan2(to[1] - from[1], to[0] - from[0]);
    const len = Math.hypot(to[0] - from[0], to[1] - from[1]);
    for (let i = 0; i < chains; i++) {
      const s = chem.rand(0, 0.75);
      const spread = (1 - s) * 4 + s * 26;
      const x = from[0] + Math.cos(ang) * len * s + chem.rand(-spread, spread) * Math.sin(ang);
      const y = from[1] + Math.sin(ang) * len * s - chem.rand(-spread, spread) * Math.cos(ang);
      chem.chain(lay, x, y, { steps: chem.rand(4, 14), dir: () => ang + chem.rand(-0.12, 0.12), turn: 0.05,
        branch: 0.08, dotEnd: 0.4, alpha: 0.9 });
    }
  }
  return L;
}

// ------------------------------------------------------------------ baking
function brightnessField(cfg, x, y) {
  const h = cfg.hotspot;
  const d = Math.hypot(x - h.x, y - h.y) / h.r;
  return 1 + h.gain * Math.max(0, 1 - d * d);
}

function bakeLayer(layer, cfg, scale) {
  const S = Math.ceil(cfg.size * scale);
  const sharp = document.createElement("canvas");
  sharp.width = sharp.height = S;
  const g = sharp.getContext("2d");
  g.scale(scale, scale);
  g.lineJoin = "round"; g.lineCap = "round";
  g.globalCompositeOperation = "lighter";
  const [r, gg, b] = cfg.color;
  g.lineWidth = cfg.lineWidth;
  for (const ln of layer.lines) {
    const mid = ln.pts[Math.floor(ln.pts.length / 2)];
    const k = brightnessField(cfg, mid[0], mid[1]);
    const cr = Math.min(255, r * k), cg = Math.min(255, gg * k), cb = Math.min(255, b * k * 1.08);
    g.strokeStyle = `rgba(${cr | 0},${cg | 0},${cb | 0},${Math.min(1, cfg.lineAlpha * ln.a)})`;
    g.beginPath();
    g.moveTo(ln.pts[0][0], ln.pts[0][1]);
    for (let i = 1; i < ln.pts.length; i++) g.lineTo(ln.pts[i][0], ln.pts[i][1]);
    g.stroke();
  }
  g.lineWidth = cfg.lineWidth * 0.9;
  for (const d of layer.dots) {
    const k = brightnessField(cfg, d.x, d.y);
    g.strokeStyle = `rgba(${Math.min(255, r * k) | 0},${Math.min(255, gg * k) | 0},${Math.min(255, b * k) | 0},${cfg.lineAlpha * d.a})`;
    g.beginPath();
    g.arc(d.x, d.y, d.r, 0, Math.PI * 2);
    g.stroke();
  }
  // glow passes (blurred, warmer copies)
  const out = document.createElement("canvas");
  out.width = out.height = S;
  const o = out.getContext("2d");
  o.globalCompositeOperation = "lighter";
  const tint = document.createElement("canvas");
  tint.width = tint.height = S;
  const tg = tint.getContext("2d");
  tg.drawImage(sharp, 0, 0);
  tg.globalCompositeOperation = "multiply";
  const [br, bg, bb] = cfg.glowColorBoost;
  tg.fillStyle = `rgb(${(255 * br) | 0},${(255 * bg) | 0},${(255 * bb) | 0})`;
  tg.fillRect(0, 0, S, S);
  tg.globalCompositeOperation = "destination-in";
  tg.drawImage(sharp, 0, 0);
  for (const [blur, alpha] of cfg.glow) {
    o.filter = `blur(${blur * scale}px)`;
    o.globalAlpha = alpha;
    o.drawImage(tint, 0, 0);
  }
  o.filter = "none";
  o.globalAlpha = 1;
  o.drawImage(sharp, 0, 0);
  return out;
}

// ------------------------------------------------------------------ renderer
const LAYER_ORDER = ["outer", "arcs", "surface", "core", "ring", "comet", "streak"];

export class Orb {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.cfg = Object.assign({}, ORB_CONFIG, opts.config || {});
    this.state = "idle";
    this.level = 0;        // smoothed audio level (mic or tts)
    this.targetLevel = 0;
    this.energy = 0;       // smoothed "activity" for state transitions
    this.t = 0;
    this.frozen = !!opts.frozen;
    this.pixelSize = opts.pixelSize || 0;  // fixed backing size (comparison harness)
    this.background = opts.background !== false;
    this.textures = null;
    this.sparks = [];
    this._last = performance.now();
    this._resize = this.resize.bind(this);
    if (!opts.noAutoResize) window.addEventListener("resize", this._resize);
    this.resize();
    this.build();
    if (!this.frozen) requestAnimationFrame(this._frame.bind(this));
  }

  resize() {
    if (this.pixelSize) {
      this.canvas.width = this.canvas.height = this.pixelSize;
      if (this.frozen && this.textures) this.render(0);
      return;
    }
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    if (this.textures && this._bakedFor && Math.abs(this._bakedFor - this._texScale()) > 0.15) this.build();
    if (this.frozen) this.render(0);
  }

  _texScale() {
    const px = Math.min(this.canvas.width, this.canvas.height);
    return Math.min(1, Math.max(0.35, px / this.cfg.size * 1.05));
  }

  build() {
    const geo = buildLayers(this.cfg);
    this.geometry = geo;
    const scale = this.pixelSize ? Math.min(1, this.pixelSize / this.cfg.size) : this._texScale();
    this._bakedFor = scale;
    this.textures = {};
    for (const k of LAYER_ORDER) this.textures[k] = bakeLayer(geo[k], this.cfg, scale);
    // spark paths: long lines from the ring and core
    this.sparkPaths = [...geo.ring.lines, ...geo.core.lines.filter((l) => l.pts.length > 12)].map((l) => l.pts);
    if (this.frozen) this.render(0);
  }

  setState(state) { this.state = state; }
  setLevel(v) { this.targetLevel = Math.max(0, Math.min(1, v)); }

  _frame(now) {
    const dt = Math.min(0.05, (now - this._last) / 1000);
    this._last = now;
    this.t += dt;
    this.level += (this.targetLevel - this.level) * Math.min(1, dt * 18);
    this.targetLevel *= Math.pow(0.02, dt); // decay if no updates arrive
    const want = { idle: 0, listening: 0.55, thinking: 1, working: 0.85, speaking: 0.5, error: 0.2 }[this.state] ?? 0;
    this.energy += (want - this.energy) * Math.min(1, dt * 2.5);
    this.render(dt);
    requestAnimationFrame(this._frame.bind(this));
  }

  render(dt) {
    const { ctx, canvas, cfg } = this;
    const W = canvas.width, H = canvas.height;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    if (this.background) {
      ctx.fillStyle = "#050302";
      ctx.fillRect(0, 0, W, H);
    } else {
      ctx.clearRect(0, 0, W, H);
    }
    if (!this.textures) return;

    // fit the 1920 design space into the canvas (orb fills ~ the shorter side)
    const fit = Math.min(W, H) / cfg.size;
    const [CX, CY] = cfg.center;
    const t = this.t, e = this.energy, lv = this.level;
    const st = this.state;
    const breathe = 1 + 0.012 * Math.sin(t * 0.9);
    const pulse = 1 + (st === "listening" ? 0.035 : 0.02) * lv;

    // per-layer motion (radians) — small oscillations keep the reference composition
    const spin = {
      outer: 0.02 * Math.sin(t * 0.11) + e * 0.05 * Math.sin(t * 0.6),
      arcs: t * (0.012 + e * 0.12),
      surface: 0.015 * Math.sin(t * 0.13 + 1) - e * t * 0.0,
      core: -t * (0.03 + e * 0.35),
      ring: 0.004 * Math.sin(t * 0.3),
      comet: 0.01 * Math.sin(t * 0.25),
      streak: 0,
    };
    const glow = {
      outer: 1 + (st === "listening" ? 0.35 * lv : 0),
      arcs: 1 + 0.25 * e,
      surface: 1,
      core: 1 + 0.35 * e + (st === "speaking" ? 0.5 * lv : 0),
      ring: 1 + (st === "speaking" ? 0.45 * lv : 0) + 0.15 * e,
      comet: 1,
      streak: 1 + (st === "working" ? 0.4 + 0.3 * Math.sin(t * 8) : 0),
    };
    const base = (st === "error" ? 0.55 + 0.3 * Math.abs(Math.sin(t * 20)) : 1) * (0.97 + 0.03 * Math.sin(t * 0.9));

    // subtle warm backdrop glow
    if (this.background) {
      const gx = W / 2 + (CX - cfg.size / 2) * fit, gy = H / 2 + (CY - cfg.size / 2) * fit;
      const grad = ctx.createRadialGradient(gx, gy, 0, gx, gy, cfg.size * 0.48 * fit);
      grad.addColorStop(0, `rgba(60,28,8,${0.18 + 0.12 * e})`);
      grad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, W, H);
    }

    ctx.globalCompositeOperation = "lighter";
    for (const k of LAYER_ORDER) {
      const tex = this.textures[k];
      const s = this._bakedFor;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.translate(W / 2, H / 2);
      ctx.scale(fit * breathe * pulse, fit * breathe * pulse);
      ctx.translate(CX - cfg.size / 2, CY - cfg.size / 2);
      // rotate around the layer's own center
      const pivot = k === "core" ? [cfg.core.cx, cfg.core.cy] : [CX, CY];
      ctx.translate(pivot[0] - CX, pivot[1] - CY);
      ctx.rotate(spin[k] || 0);
      ctx.translate(-pivot[0], -pivot[1]);
      ctx.globalAlpha = Math.min(1, base * (glow[k] || 1));
      ctx.drawImage(tex, 0, 0, tex.width, tex.height, 0, 0, tex.width / s, tex.height / s);
      if ((glow[k] || 1) > 1.05) { // extra additive pass for "hot" layers
        ctx.globalAlpha = Math.min(1, (glow[k] - 1) * 0.8);
        ctx.drawImage(tex, 0, 0, tex.width, tex.height, 0, 0, tex.width / s, tex.height / s);
      }
    }

    // sparks traveling along traces while thinking / working
    if (e > 0.3 && this.sparkPaths && this.sparkPaths.length) {
      if (Math.random() < e * 0.6) {
        const p = this.sparkPaths[Math.floor(Math.random() * this.sparkPaths.length)];
        this.sparks.push({ p, u: 0, v: 0.6 + Math.random() * 0.8 });
      }
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.translate(W / 2, H / 2);
      ctx.scale(fit * breathe, fit * breathe);
      ctx.translate(-cfg.size / 2, -cfg.size / 2);
      ctx.globalAlpha = 1;
      for (const sp of this.sparks) {
        sp.u += sp.v * (dt || 0.016);
        const i = Math.min(sp.p.length - 1, Math.floor(sp.u * sp.p.length));
        const [x, y] = sp.p[i];
        const g = ctx.createRadialGradient(x, y, 0, x, y, 14);
        g.addColorStop(0, "rgba(255,240,215,0.95)");
        g.addColorStop(1, "rgba(255,150,60,0)");
        ctx.fillStyle = g;
        ctx.fillRect(x - 14, y - 14, 28, 28);
      }
      this.sparks = this.sparks.filter((s) => s.u < 1);
      if (this.sparks.length > 40) this.sparks.splice(0, this.sparks.length - 40);
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
  }
}
