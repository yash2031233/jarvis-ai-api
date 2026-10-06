// The Jarvis orb in real 3D (WebGL2, no dependencies).
//
// Same design as orb.js: the 2D generator's lines are *lifted* into 3D (onto spheres, tilted
// rings, an axis through the core…) and the result is honest 3D: every line lies on a real,
// round sphere (nested ones fill the inside) and x/y are left as drawn. (They used to be
// pre-scaled so the front view matched the 2D design exactly - which made the back of the orb
// up to a quarter too big, and turning it looked like seeing it through a lens.) The camera is
// far away, so the front view still looks like the design.
//
// Quality: lines are proper screen-space polylines (mitered joins, round-ish caps,
// analytic anti-aliasing, constant pixel width) and the glow is the same three Gaussian
// passes as the 2D version, done as a GPU blur pyramid. Falls back to the 2D renderer when
// WebGL2 is unavailable.

import { ORB_CONFIG, Orb2D, LAYER_ORDER, brightnessField, buildLayers, heat } from "./orb.js";

export { ORB_CONFIG };

const DCAM = 7600;            // camera distance in design px (gentle perspective: the front view stays the design)
const D2R = Math.PI / 180;

function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ------------------------------------------------------------------ lifting 2D → 3D
// Returns, per layer, a function (x, y, polyRand) → Z (toward the viewer, design px).
// Coordinates are relative to the orb center; y points down.
function liftFunctions(cfg) {
  const [CX, CY] = cfg.center;
  // A line lies on a ROUND sphere around (cx, cy): radius somewhere between just enough to hold the line and R
  // (p.depth = where in that range). Squashing one sphere by a factor instead made lens-like streaks when turned.
  const sphere = (cx, cy, R) => (x, y, p) => {
    if (p.rho === undefined) {
      const rm = Math.max(...(p.local || [[x, y]]).map(([qx, qy]) => Math.hypot(qx - cx, qy - cy)));
      p.rho = rm >= R ? rm : rm + (R - rm) * p.depth;
    }
    const r2 = (x - cx) ** 2 + (y - cy) ** 2;
    return p.sign * Math.sqrt(Math.max(0, p.rho * p.rho - r2));
  };
  const tiltedCircle = (cx, cy) => (x, y, p) => {
    // a ring that looks circular at rest but sits in a tilted plane: z = r·sin(tilt)·cos(θ-θ0)
    const dx = x - cx, dy = y - cy;
    const r = Math.hypot(dx, dy), th = Math.atan2(dy, dx);
    return r * Math.sin(p.tilt) * Math.cos(th - p.phase);
  };
  const core = { cx: cfg.core.cx - CX, cy: cfg.core.cy - CY };
  const ring = cfg.ring;
  const sinA = Math.sqrt(Math.max(0, 1 - (ring.b / ring.a) ** 2));
  const s0 = cfg.streak.from, s1 = cfg.streak.to;
  const sdx = s1[0] - s0[0], sdy = s1[1] - s0[1], slen2 = sdx * sdx + sdy * sdy;
  return {
    outer: sphere(0, 0, 820),
    arcs: tiltedCircle(0, 0),
    surface: sphere(0, 0, 660),
    core: (x, y, p) => (p.ringLike ? tiltedCircle(core.cx, core.cy)(x, y, p) : sphere(core.cx, core.cy, cfg.core.r * 1.04)(x, y, p)),
    heart: (x, y, p) => (p.ringLike ? tiltedCircle(core.cx, core.cy)(x, y, p) : sphere(core.cx, core.cy, cfg.heart.r * 1.05)(x, y, p)),
    ring: (x, y) => {
      // the ellipse is a circle of radius a in a plane tilted by acos(b/a); upper arc = front
      const dy = y - (ring.cy - CY);
      return -(dy / ring.b) * ring.a * sinA;
    },
    streak: (x, y) => {
      const u = ((x + CX - s0[0]) * sdx + (y + CY - s0[1]) * sdy) / slen2;
      return (u - 0.5) * 2 * 300; // an axis piercing the orb, top-left end behind
    },
    comet: (x, y) => 120 + 0.25 * (x - (cfg.comet.from[0] - CX)),
  };
}

function polyParams(layer, rng, cfg, pts) {
  const p = { sign: 1, depth: 1, tilt: 0, phase: 0, ringLike: false };
  switch (layer) {
    // front and back filled alike, and lines spread through the depth rather than all on the outside of a shell:
    // turned sideways, the orb used to show a hollow slab through the middle and a thin back half
    case "outer": p.sign = rng() < 0.5 ? 1 : -1; p.depth = 0.85 + rng() * 0.15; break;
    case "surface": p.sign = rng() < 0.5 ? 1 : -1; p.depth = 0.35 + rng() * 0.65; break;
    case "arcs": p.tilt = (rng() * 2 - 1) * 32 * D2R; p.phase = rng() * Math.PI * 2; break;
    case "heart":
    case "core": {
      // the smooth concentric rings become gyroscope-like tilted circles; chains sit on a sphere
      const [CX, CY] = cfg.center;
      const cx = cfg.core.cx - CX, cy = cfg.core.cy - CY;
      const r0 = Math.hypot(pts[0][0] - cx, pts[0][1] - cy);
      const r1 = Math.hypot(pts[pts.length - 1][0] - cx, pts[pts.length - 1][1] - cy);
      p.ringLike = pts.length > 60 && Math.abs(r0 - r1) < 25;
      p.tilt = (rng() * 2 - 1) * (layer === "heart" ? 80 : 55) * D2R; p.phase = rng() * Math.PI * 2;
      p.sign = rng() < 0.5 ? 1 : -1; p.depth = Math.sqrt(rng());   // nested spheres all through: a solid ball
      break;
    }
  }
  return p;
}

// ------------------------------------------------------------------ real 3D chains
// The lace shell, the inner ball and the nucleus are grown in 3D instead of lifted from the flat drawing: a flat
// drawing lifted onto spheres bunches up front and back and leaves the sides thin, and its lines turn into streaks
// edge-on (it looked refracted when turned). Each 2D chain gets a 3D twin of the same length and brightness, zig-
// zagging over a real sphere through a point spread evenly in all directions - the same look from every side.
const SOLID = { outer: "shell", surface: "shell", core: "ball", heart: "ball" };

function isRingLike(cfg, pts) {
  const [CX, CY] = cfg.center;
  const cx = cfg.core.cx - CX + CX, cy = cfg.core.cy - CY + CY;
  const r0 = Math.hypot(pts[0][0] - cx, pts[0][1] - cy);
  const r1 = Math.hypot(pts[pts.length - 1][0] - cx, pts[pts.length - 1][1] - cy);
  return pts.length > 60 && Math.abs(r0 - r1) < 25;
}

const v3 = {
  add: (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]],
  mul: (a, k) => [a[0] * k, a[1] * k, a[2] * k],
  dot: (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2],
  cross: (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]],
  norm: (a) => { const l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / l, a[1] / l, a[2] / l]; },
};

function randomUnit(rng) {
  const z = rng() * 2 - 1, t = rng() * Math.PI * 2, r = Math.sqrt(1 - z * z);
  return [r * Math.cos(t), r * Math.sin(t), z];
}

// one zig-zag chain of `steps` bonds over the sphere (center c, radius rad), starting at direction n0
function chain3d(rng, cfg, c, rad, n0, steps, swirl) {
  const D2 = Math.PI / 180, zig = cfg.zig * D2;
  let n = v3.norm(n0);
  let d = swirl && Math.abs(n[2]) < 0.97 ? v3.norm(v3.cross([0, 0, 1], n)) : v3.norm(v3.cross(n, randomUnit(rng)));
  if (rng() < 0.5) d = v3.mul(d, -1);
  let pos = v3.mul(n, rad), side = rng() < 0.5 ? 1 : -1;
  const pts = [v3.add(c, pos)];
  for (let i = 0; i < steps; i++) {
    if (rng() < 0.06) {                       // an occasional turn, like the 2D chains
      const th = (rng() < 0.5 ? 1 : -1) * 60 * D2;
      d = v3.add(v3.mul(d, Math.cos(th)), v3.mul(v3.cross(n, d), Math.sin(th)));
    }
    const ang = side * zig + (rng() - 0.5) * 0.08;
    side = -side;
    const step = v3.add(v3.mul(d, Math.cos(ang)), v3.mul(v3.cross(n, d), Math.sin(ang)));
    pos = v3.mul(v3.norm(v3.add(pos, v3.mul(step, cfg.seg * (0.85 + rng() * 0.3)))), rad);
    n = v3.norm(pos);
    d = v3.norm(v3.add(d, v3.mul(n, -v3.dot(d, n))));   // keep heading along the sphere
    pts.push(v3.add(c, pos));
  }
  return pts;
}

// ------------------------------------------------------------------ mesh building
// Per vertex: pos(3) prev(3) next(3) side(1) color(3) = 13 floats; 2 vertices per point.
const STRIDE = 13;

function buildMeshes(cfg) {
  const geo = buildLayers(cfg);
  const lifts = liftFunctions(cfg);
  const rng = mulberry32(cfg.seed ^ 0x9e3779b9);
  const [CX, CY] = cfg.center;
  const [cr, cg, cb] = cfg.color;
  const meshes = {};
  const sparkPaths = [];

  for (const name of LAYER_ORDER) {
    const layer = geo[name];
    const verts = [];
    const idx = [];
    let n = 0;
    const lift = lifts[name];

    const addPoly = (pts2d, p, color, widthScale, closed) => {
      // to orb-centered coordinates and lift into 3D (x/y as drawn: no perspective pre-scaling - see the top)
      p.local = pts2d.map(([x, y]) => [x - CX, y - CY]);
      return pushPoly(p.local.map(([lx, ly]) => [lx, ly, lift(lx, ly, p)]), color, widthScale, closed);
    };
    const pushPoly = (P, color, widthScale, closed) => {
      const m = P.length;
      for (let i = 0; i < m; i++) {
        const prev = i > 0 ? P[i - 1] : closed ? P[m - 2] : P[i];
        const next = i < m - 1 ? P[i + 1] : closed ? P[1] : P[i];
        for (const side of [-widthScale, widthScale]) {
          verts.push(P[i][0], P[i][1], P[i][2], prev[0], prev[1], prev[2], next[0], next[1], next[2], side,
            color[0], color[1], color[2]);
        }
      }
      for (let i = 0; i < m - 1; i++) {
        const a = n + i * 2;
        idx.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
      }
      n += m * 2;
      return P;
    };

    const solid = SOLID[name];
    const coreC = [cfg.core.cx - CX, cfg.core.cy - CY, 0];
    for (const ln of layer.lines) {
      const mid = ln.pts[Math.floor(ln.pts.length / 2)];
      const a = Math.min(1, cfg.lineAlpha * ln.a);
      if (solid && !isRingLike(cfg, ln.pts)) {
        // its 3D twin: same length and brightness, over a real sphere, facing any direction
        const R = { outer: 790, surface: 660, core: cfg.core.r, heart: cfg.heart.r }[name];
        const c = name === "surface" || name === "outer" ? [0, 0, 0] : coreC;
        const rad = solid === "shell" ? R * (0.93 + rng() * 0.07) : R * Math.cbrt(0.04 + rng() * 0.96);
        const P = chain3d(rng, cfg, c, rad, randomUnit(rng), Math.max(2, ln.pts.length - 1), name === "core");
        const m = P[Math.floor(P.length / 2)];
        const hc = heat(cfg, brightnessField(cfg, m[0] + CX, m[1] + CY));
        pushPoly(P, [hc[0] / 255 * a, hc[1] / 255 * a, hc[2] / 255 * a], 1, false);
        if (rng() < 0.3) {                    // a dot (atom) at the chain's end, flat toward the viewer
          const e = P[P.length - 1], r = 1.8 + rng();
          const dot = [];
          for (let i = 0; i <= 12; i++) { const t = (i / 12) * Math.PI * 2; dot.push([e[0] + Math.cos(t) * r, e[1] + Math.sin(t) * r, e[2]]); }
          pushPoly(dot, [hc[0] / 255 * a, hc[1] / 255 * a, hc[2] / 255 * a], 0.9, true);
        }
        continue;
      }
      const k = brightnessField(cfg, mid[0], mid[1]);
      const hc = heat(cfg, k), color = [hc[0] / 255 * a, hc[1] / 255 * a, hc[2] / 255 * a];
      const p = polyParams(name, rng, cfg, ln.pts);
      const P = addPoly(ln.pts, p, color, 1, false);
      if ((name === "ring" || name === "core") && ln.pts.length > 12) sparkPaths.push({ layer: name, pts: P });
    }
    for (const d of solid ? [] : layer.dots) {
      const k = brightnessField(cfg, d.x, d.y);
      const a = cfg.lineAlpha * d.a;
      const hc = heat(cfg, k), color = [hc[0] / 255 * a, hc[1] / 255 * a, hc[2] / 255 * a];
      const pts = [];
      for (let i = 0; i <= 12; i++) {
        const t = (i / 12) * Math.PI * 2;
        pts.push([d.x + Math.cos(t) * d.r, d.y + Math.sin(t) * d.r]);
      }
      // a dot shares the depth of whatever it sits on
      const p = polyParams(name === "core" ? "surface" : name, rng, cfg, pts);
      addPoly(pts, p, color, 0.9, true);
    }
    meshes[name] = { verts: new Float32Array(verts), idx: new Uint32Array(idx) };
  }
  return { meshes, sparkPaths };
}

// ------------------------------------------------------------------ shaders
const LINE_VS = `#version 300 es
precision highp float;
in vec3 aPos; in vec3 aPrev; in vec3 aNext; in float aSide; in vec3 aColor;
uniform mat3 uRot; uniform vec3 uPivot; uniform mat3 uRotG;
uniform vec2 uRes; uniform vec2 uOffset; uniform float uFit, uScale, uDcam, uHalfW, uIntensity;
out vec3 vColor; out float vDist; out float vHalf;
vec2 proj(vec3 p) {
  vec3 q = uRotG * (uRot * (p - uPivot) + uPivot);
  float k = uDcam / max(1.0, uDcam - q.z);
  return uRes * 0.5 + (uOffset + q.xy * k) * uFit * uScale;
}
void main() {
  vec2 c = proj(aPos), a = proj(aPrev), b = proj(aNext);
  vec2 d1 = c - a, d2 = b - c;
  float l1 = length(d1), l2 = length(d2);
  vec2 t1 = l1 > 1e-4 ? d1 / l1 : (l2 > 1e-4 ? d2 / l2 : vec2(1.0, 0.0));
  vec2 t2 = l2 > 1e-4 ? d2 / l2 : t1;
  vec2 tsum = t1 + t2;
  vec2 tang = length(tsum) > 1e-3 ? normalize(tsum) : t1;
  vec2 n = vec2(-tang.y, tang.x);
  float miter = 1.0 / max(0.4, abs(dot(n, vec2(-t1.y, t1.x))));
  float side = sign(aSide);
  float halfW = uHalfW * abs(aSide);
  float hw = max(halfW, 0.5) + 1.0;              // extrusion incl. 1px anti-aliasing margin
  vec2 pos = c + n * side * hw * min(miter, 2.5);
  if (l1 < 1e-4) pos -= t2 * hw * 0.8;            // caps at polyline ends
  if (l2 < 1e-4) pos += t1 * hw * 0.8;
  vDist = side * hw;
  vHalf = max(halfW, 0.5);
  float thin = halfW < 0.5 ? halfW * 2.0 : 1.0;  // sub-pixel lines: fade instead of thinning
  vColor = aColor * uIntensity * thin;
  vec2 clip = pos / uRes * 2.0 - 1.0;
  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
}`;

const LINE_FS = `#version 300 es
precision highp float;
in vec3 vColor; in float vDist; in float vHalf;
out vec4 o;
void main() {
  float cov = clamp(vHalf + 0.5 - abs(vDist), 0.0, 1.0);
  o = vec4(vColor * cov, cov);
}`;

const QUAD_VS = `#version 300 es
in vec2 aXY; out vec2 vUV;
void main() { vUV = aXY * 0.5 + 0.5; gl_Position = vec4(aXY, 0.0, 1.0); }`;

const DOWN_FS = `#version 300 es
precision highp float;
in vec2 vUV; uniform sampler2D uTex; out vec4 o;
void main() { o = texture(uTex, vUV); }`;   // bilinear tap at the 2x2 center = box filter

const BLUR_FS = `#version 300 es
precision highp float;
in vec2 vUV; uniform sampler2D uTex; uniform vec2 uDir; uniform float uW[25]; uniform int uR;
out vec4 o;
void main() {
  vec4 s = texture(uTex, vUV) * uW[0];
  for (int i = 1; i <= 24; i++) {
    if (i > uR) break;
    vec2 off = uDir * float(i);
    s += (texture(uTex, vUV + off) + texture(uTex, vUV - off)) * uW[i];
  }
  o = s;
}`;

const COMP_FS = `#version 300 es
precision highp float;
in vec2 vUV;
uniform sampler2D uScene, uG0, uG1, uG2;
uniform vec3 uGA; uniform vec3 uBoost; uniform vec3 uBg;
uniform vec2 uRes; uniform vec2 uGradC; uniform float uGradR, uGradA; uniform bool uBackground;
out vec4 o;
void main() {
  vec3 glow = (texture(uG0, vUV).rgb * uGA.x + texture(uG1, vUV).rgb * uGA.y + texture(uG2, vUV).rgb * uGA.z) * uBoost;
  vec3 orb = texture(uScene, vUV).rgb + glow;
  vec2 cpx = vec2(vUV.x, 1.0 - vUV.y) * uRes;
  float cr = length(cpx - uGradC) / (uGradR * 0.42);
  orb += vec3(1.0, 0.6, 0.28) * (0.16 + 0.5 * uGradA) * exp(-cr * cr * 4.0);
  vec3 base = vec3(0.0);
  if (uBackground) {
    vec2 px = vec2(vUV.x, 1.0 - vUV.y) * uRes;
    float f = clamp(length(px - uGradC) / uGradR, 0.0, 1.0);
    float a = uGradA * (1.0 - f);
    base = mix(uBg, vec3(60.0, 28.0, 8.0) / 255.0, a);
  }
  vec3 c = min(base + orb, vec3(1.0));
  o = vec4(c, uBackground ? 1.0 : max(c.r, max(c.g, c.b)));
}`;

const SPARK_VS = `#version 300 es
precision highp float;
in vec3 aPos;
uniform mat3 uRot; uniform vec3 uPivot; uniform mat3 uRotG;
uniform vec2 uRes; uniform vec2 uOffset; uniform float uFit, uScale, uDcam;
void main() {
  vec3 q = uRotG * (uRot * (aPos - uPivot) + uPivot);
  float k = uDcam / max(1.0, uDcam - q.z);
  vec2 px = uRes * 0.5 + (uOffset + q.xy * k) * uFit * uScale;
  vec2 clip = px / uRes * 2.0 - 1.0;
  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
  gl_PointSize = 28.0 * uFit * uScale * k;
}`;

const SPARK_FS = `#version 300 es
precision highp float;
out vec4 o;
void main() {
  float d = length(gl_PointCoord - 0.5) * 2.0;
  if (d > 1.0) discard;
  vec3 c = mix(vec3(1.0, 0.94, 0.84), vec3(1.0, 0.59, 0.24), d) * 0.95 * (1.0 - d);
  o = vec4(c, 1.0 - d);
}`;

function compile(gl, vs, fs) {
  const mk = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  };
  const p = gl.createProgram();
  gl.attachShader(p, mk(gl.VERTEX_SHADER, vs));
  gl.attachShader(p, mk(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
  const u = {};
  const nu = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
  for (let i = 0; i < nu; i++) {
    const info = gl.getActiveUniform(p, i);
    const name = info.name.replace(/\[0\]$/, "");
    u[name] = gl.getUniformLocation(p, info.name);
  }
  return { p, u };
}

function rotYX(yaw, pitch) {
  const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
  // R = Rx(pitch) * Ry(yaw), column-major for GLSL mat3
  return new Float32Array([cy, sp * sy, -cp * sy, 0, cp, sp, sy, -sp * cy, cp * cy]);
}
function rotZ(a) {
  const c = Math.cos(a), s = Math.sin(a);
  return new Float32Array([c, s, 0, -s, c, 0, 0, 0, 1]);
}

function gaussian(sigma) {
  const r = Math.min(24, Math.max(1, Math.ceil(sigma * 3)));
  const w = new Float32Array(25);
  let sum = 0;
  for (let i = 0; i <= r; i++) { w[i] = Math.exp(-(i * i) / (2 * sigma * sigma)); sum += i ? 2 * w[i] : w[i]; }
  for (let i = 0; i <= r; i++) w[i] /= sum;
  return { w, r };
}

// ------------------------------------------------------------------ renderer
class Orb3D {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.cfg = Object.assign({}, ORB_CONFIG, opts.config || {});
    this.state = "idle";
    this.level = 0;
    this.targetLevel = 0;
    this.energy = 0;
    this.t = 0;
    this.frozen = !!opts.frozen;
    this.pixelSize = opts.pixelSize || 0;
    this.background = opts.background !== false;
    this.sparks = [];
    this.drag = { active: false, yaw: 0, pitch: 0, vy: 0, vp: 0, moved: false, dist: 0, x: 0, y: 0 };
    this._wasDrag = false;
    const gl = canvas.getContext("webgl2", { antialias: false, alpha: !this.background, premultipliedAlpha: false,
      preserveDrawingBuffer: true });
    if (!gl) throw new Error("WebGL2 unavailable");
    this.gl = gl;
    this._init();
    this._last = performance.now();
    if (!opts.noAutoResize) window.addEventListener("resize", () => this.resize());
    if (!this.frozen) this._bindDrag();
    canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); this._lost = true; });
    canvas.addEventListener("webglcontextrestored", () => { this._lost = false; this._init(); });
    this.resize();
    if (!this.frozen) requestAnimationFrame((t) => this._frame(t));
  }

  _init() {
    const gl = this.gl;
    this.prog = {
      line: compile(gl, LINE_VS, LINE_FS),
      down: compile(gl, QUAD_VS, DOWN_FS),
      blur: compile(gl, QUAD_VS, BLUR_FS),
      comp: compile(gl, QUAD_VS, COMP_FS),
      spark: compile(gl, SPARK_VS, SPARK_FS),
    };
    // fullscreen triangle
    this.quad = gl.createVertexArray();
    gl.bindVertexArray(this.quad);
    const qb = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, qb);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    for (const k of ["down", "blur", "comp"]) {
      const loc = gl.getAttribLocation(this.prog[k].p, "aXY");
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    }
    // meshes
    const { meshes, sparkPaths } = buildMeshes(this.cfg);
    this.sparkPaths = sparkPaths;
    this.layers = {};
    const P = this.prog.line.p;
    const attrs = [["aPos", 3, 0], ["aPrev", 3, 3], ["aNext", 3, 6], ["aSide", 1, 9], ["aColor", 3, 10]];
    for (const name of LAYER_ORDER) {
      const m = meshes[name];
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      const vb = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, vb);
      gl.bufferData(gl.ARRAY_BUFFER, m.verts, gl.STATIC_DRAW);
      for (const [nm, size, off] of attrs) {
        const loc = gl.getAttribLocation(P, nm);
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc, size, gl.FLOAT, false, STRIDE * 4, off * 4);
      }
      const ib = gl.createBuffer();
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, m.idx, gl.STATIC_DRAW);
      this.layers[name] = { vao, count: m.idx.length };
    }
    // sparks
    this.sparkVao = gl.createVertexArray();
    gl.bindVertexArray(this.sparkVao);
    this.sparkBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.sparkBuf);
    const sl = gl.getAttribLocation(this.prog.spark.p, "aPos");
    gl.enableVertexAttribArray(sl);
    gl.vertexAttribPointer(sl, 3, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    this.targets = null;
  }

  // ---- render targets: scene + 2x pyramid (levels 1..5) + per-glow ping/pong
  _targets(W, H) {
    const gl = this.gl;
    if (this.targets && this.targets.W === W && this.targets.H === H) return this.targets;
    if (this.targets) for (const t of this.targets.all) { gl.deleteTexture(t.tex); gl.deleteFramebuffer(t.fb); }
    const all = [];
    const mk = (w, h) => {
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      const fb = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      const t = { tex, fb, w, h };
      all.push(t);
      return t;
    };
    const scene = mk(W, H);
    const pyr = [scene];
    for (let l = 1; l <= 5; l++) pyr.push(mk(Math.max(1, W >> l), Math.max(1, H >> l)));
    const glows = [0, 1, 2].map(() => ({ a: null, b: null }));
    this.targets = { W, H, scene, pyr, glows, all, mk };
    return this.targets;
  }

  _glowTarget(i, w, h) {
    const T = this.targets;
    const g = T.glows[i];
    if (!g.a || g.a.w !== w || g.a.h !== h) {
      g.a = T.mk(w, h);
      g.b = T.mk(w, h);
    }
    return g;
  }

  resize() {
    if (this.pixelSize) {
      this.canvas.width = this.canvas.height = this.pixelSize;
    } else {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = this.canvas.getBoundingClientRect();
      this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
      this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    }
    if (this.frozen) this.render(0);
  }

  setState(s) { this.state = s; }
  setLevel(v) { this.targetLevel = Math.max(0, Math.min(1, v)); }
  /** true if the last pointer interaction was a drag (so the app can ignore the click) */
  consumeDrag() { const d = this._wasDrag; this._wasDrag = false; return d; }

  _bindDrag() {
    const c = this.canvas, d = this.drag;
    c.addEventListener("pointerdown", (e) => {
      d.active = true; d.moved = false; d.dist = 0; d.x = e.clientX; d.y = e.clientY;
      c.setPointerCapture(e.pointerId);
    });
    c.addEventListener("pointermove", (e) => {
      if (!d.active) return;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      d.x = e.clientX; d.y = e.clientY;
      d.dist += Math.abs(dx) + Math.abs(dy);
      if (d.dist > 5) d.moved = true;
      d.vy = dx * 0.006; d.vp = dy * 0.006;
      d.yaw += d.vy;
      d.pitch = Math.max(-1.3, Math.min(1.3, d.pitch + d.vp));
    });
    const up = (e) => {
      if (!d.active) return;
      d.active = false;
      this._wasDrag = d.moved;
      try { c.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    };
    c.addEventListener("pointerup", up);
    c.addEventListener("pointercancel", up);
  }

  _frame(now) {
    const dt = Math.min(0.05, (now - this._last) / 1000);
    this._last = now;
    this.t += dt;
    this.level += (this.targetLevel - this.level) * Math.min(1, dt * 18);
    this.targetLevel *= Math.pow(0.02, dt);
    const want = { idle: 0, listening: 0.55, thinking: 1, working: 0.85, speaking: 0.5, error: 0.2 }[this.state] ?? 0;
    this.energy += (want - this.energy) * Math.min(1, dt * 2.5);
    const d = this.drag;
    if (!d.active) {
      // a little inertia, then ease back to the reference pose
      d.yaw += d.vy; d.pitch += d.vp;
      d.vy *= Math.pow(0.02, dt); d.vp *= Math.pow(0.02, dt);
      const k = 1 - Math.pow(0.25, dt);
      d.yaw -= d.yaw * k; d.pitch -= d.pitch * k;
    }
    if (!this._lost) this.render(dt);
    requestAnimationFrame((t) => this._frame(t));
  }

  render(dt) {
    const gl = this.gl, cfg = this.cfg;
    const W = this.canvas.width, H = this.canvas.height;
    const T = this._targets(W, H);
    const t = this.t, e = this.energy, lv = this.level, st = this.state;
    const fit = Math.min(W, H) / cfg.size;
    const [CX, CY] = cfg.center;
    const breathe = 1 + 0.012 * Math.sin(t * 0.9);
    const pulse = 1 + (st === "listening" ? 0.035 : 0.02) * lv;
    const scale = breathe * pulse;

    // global 3D pose: gentle sway (more when busy) + user drag
    const sway = 1 + e * 0.6;
    const yaw = (this.frozen ? 0 : 0.16 * Math.sin(t * 0.21 * sway) + 0.05 * Math.sin(t * 0.53)) + this.drag.yaw;
    const pitch = (this.frozen ? 0 : 0.06 * Math.sin(t * 0.17 * sway + 1.0)) + this.drag.pitch;
    const RG = rotYX(yaw, pitch);

    const spin = {
      outer: 0.02 * Math.sin(t * 0.11) + e * 0.05 * Math.sin(t * 0.6),
      arcs: t * (0.012 + e * 0.12),
      surface: 0.015 * Math.sin(t * 0.13 + 1),
      core: -t * (0.03 + e * 0.35),
      heart: t * (0.08 + e * 0.6),
      ring: 0.004 * Math.sin(t * 0.3),
      comet: 0.01 * Math.sin(t * 0.25),
      streak: 0,
    };
    const glowK = {
      outer: 1 + (st === "listening" ? 0.35 * lv : 0),
      arcs: 1 + 0.25 * e,
      surface: 1,
      core: 1 + 0.35 * e + (st === "speaking" ? 0.5 * lv : 0),
      heart: 1.0 + 0.4 * e + (st === "speaking" ? 0.9 * lv : 0) + 0.08 * Math.sin(t * 1.6),
      ring: 1 + (st === "speaking" ? 0.45 * lv : 0) + 0.15 * e,
      comet: 1,
      streak: 1 + (st === "working" ? 0.4 + 0.3 * Math.sin(t * 8) : 0),
    };
    const base = (st === "error" ? 0.55 + 0.3 * Math.abs(Math.sin(t * 20)) : 1) * (this.frozen ? 1 : 0.97 + 0.03 * Math.sin(t * 0.9));
    const offset = [CX - cfg.size / 2, CY - cfg.size / 2];
    const corePivot = [cfg.core.cx - CX, cfg.core.cy - CY, 0];

    // ---- 1. lines → scene (additive, clamped like the 2D 'lighter' compositing)
    gl.bindFramebuffer(gl.FRAMEBUFFER, T.scene.fb);
    gl.viewport(0, 0, W, H);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    const L = this.prog.line;
    gl.useProgram(L.p);
    gl.uniform2f(L.u.uRes, W, H);
    gl.uniform2f(L.u.uOffset, offset[0], offset[1]);
    gl.uniform1f(L.u.uFit, fit);
    gl.uniform1f(L.u.uScale, scale);
    gl.uniform1f(L.u.uDcam, DCAM);
    gl.uniform1f(L.u.uHalfW, cfg.lineWidth * fit * scale * 0.5);
    gl.uniformMatrix3fv(L.u.uRotG, false, RG);
    for (const name of LAYER_ORDER) {
      const k = glowK[name];
      const intensity = base * (Math.min(1, k) + Math.max(0, (k - 1) * 0.8));
      gl.uniformMatrix3fv(L.u.uRot, false, rotZ(spin[name] || 0));
      const pv = name === "core" || name === "heart" ? corePivot : [0, 0, 0];
      gl.uniform3f(L.u.uPivot, pv[0], pv[1], pv[2]);
      gl.uniform1f(L.u.uIntensity, Math.min(1, intensity));
      gl.bindVertexArray(this.layers[name].vao);
      gl.drawElements(gl.TRIANGLES, this.layers[name].count, gl.UNSIGNED_INT, 0);
    }

    // ---- sparks while thinking / working
    if (e > 0.3 && this.sparkPaths.length && dt) {
      if (Math.random() < e * 0.6) {
        const sp = this.sparkPaths[Math.floor(Math.random() * this.sparkPaths.length)];
        this.sparks.push({ sp, u: 0, v: 0.6 + Math.random() * 0.8 });
      }
    }
    if (this.sparks.length) {
      const S = this.prog.spark;
      gl.useProgram(S.p);
      gl.uniform2f(S.u.uRes, W, H);
      gl.uniform2f(S.u.uOffset, offset[0], offset[1]);
      gl.uniform1f(S.u.uFit, fit);
      gl.uniform1f(S.u.uScale, scale);
      gl.uniform1f(S.u.uDcam, DCAM);
      gl.uniformMatrix3fv(S.u.uRotG, false, RG);
      gl.bindVertexArray(this.sparkVao);
      for (const name of ["ring", "core"]) {
        const pts = [];
        for (const s of this.sparks) {
          if (s.sp.layer !== name) continue;
          s.u += s.v * (dt || 0.016);
          const p = s.sp.pts[Math.min(s.sp.pts.length - 1, Math.floor(s.u * s.sp.pts.length))];
          pts.push(p[0], p[1], p[2]);
        }
        if (!pts.length) continue;
        gl.uniformMatrix3fv(S.u.uRot, false, rotZ(spin[name]));
        const pv = name === "core" ? corePivot : [0, 0, 0];
        gl.uniform3f(S.u.uPivot, pv[0], pv[1], pv[2]);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.sparkBuf);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pts), gl.DYNAMIC_DRAW);
        gl.drawArrays(gl.POINTS, 0, pts.length / 3);
      }
      this.sparks = this.sparks.filter((s) => s.u < 1).slice(-40);
    }
    gl.disable(gl.BLEND);

    // ---- 2. glow: box-filtered pyramid, then separable Gaussian at a suitable level
    gl.bindVertexArray(this.quad);
    const sigmas = cfg.glow.map(([b]) => b * fit * scale);
    const levels = sigmas.map((s) => Math.max(0, Math.min(5, Math.ceil(Math.log2(Math.max(1, s / 3))))));
    const maxL = Math.max(...levels);
    const Dn = this.prog.down;
    gl.useProgram(Dn.p);
    for (let l = 1; l <= maxL; l++) {
      const dst = T.pyr[l];
      gl.bindFramebuffer(gl.FRAMEBUFFER, dst.fb);
      gl.viewport(0, 0, dst.w, dst.h);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, T.pyr[l - 1].tex);
      gl.uniform1i(Dn.u.uTex, 0);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    const B = this.prog.blur;
    gl.useProgram(B.p);
    const glowTex = [];
    for (let i = 0; i < 3; i++) {
      const src = T.pyr[levels[i]];
      const g = this._glowTarget(i, src.w, src.h);
      const { w, r } = gaussian(Math.max(0.6, sigmas[i] / (1 << levels[i])));
      gl.uniform1fv(B.u.uW, w);
      gl.uniform1i(B.u.uR, r);
      gl.uniform1i(B.u.uTex, 0);
      gl.viewport(0, 0, src.w, src.h);
      gl.bindFramebuffer(gl.FRAMEBUFFER, g.a.fb);
      gl.bindTexture(gl.TEXTURE_2D, src.tex);
      gl.uniform2f(B.u.uDir, 1 / src.w, 0);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.bindFramebuffer(gl.FRAMEBUFFER, g.b.fb);
      gl.bindTexture(gl.TEXTURE_2D, g.a.tex);
      gl.uniform2f(B.u.uDir, 0, 1 / src.h);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      glowTex.push(g.b.tex);
    }

    // ---- 3. composite to screen
    const C = this.prog.comp;
    gl.useProgram(C.p);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, W, H);
    const texs = [T.scene.tex, ...glowTex];
    ["uScene", "uG0", "uG1", "uG2"].forEach((nm, i) => {
      gl.activeTexture(gl.TEXTURE0 + i);
      gl.bindTexture(gl.TEXTURE_2D, texs[i]);
      gl.uniform1i(C.u[nm], i);
    });
    gl.activeTexture(gl.TEXTURE0);
    gl.uniform3f(C.u.uGA, cfg.glow[0][1], cfg.glow[1][1], cfg.glow[2][1]);
    gl.uniform3f(C.u.uBoost, ...cfg.glowColorBoost);
    gl.uniform3f(C.u.uBg, 5 / 255, 3 / 255, 2 / 255);
    gl.uniform2f(C.u.uRes, W, H);
    gl.uniform2f(C.u.uGradC, W / 2 + offset[0] * fit, H / 2 + offset[1] * fit);
    gl.uniform1f(C.u.uGradR, cfg.size * 0.48 * fit);
    gl.uniform1f(C.u.uGradA, 0.18 + 0.12 * e);
    gl.uniform1i(C.u.uBackground, this.background ? 1 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindVertexArray(null);
  }
}

/** The orb: real 3D (WebGL2) when available, otherwise the 2D canvas renderer. */
export class Orb {
  constructor(canvas, opts = {}) {
    if (!opts.force2d) {
      try {
        const o = new Orb3D(canvas, opts);
        o.mode = "3d";
        return o;
      } catch (e) {
        console.warn("3D orb unavailable, using 2D:", e.message);
      }
    }
    const o = new Orb2D(canvas, opts);
    o.mode = "2d";
    o.consumeDrag = () => false;
    return o;
  }
}
