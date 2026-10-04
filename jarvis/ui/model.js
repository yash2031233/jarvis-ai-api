// 3D part viewer — what OpenSCAD's preview window does, inside the page (ported from Jarvis v1).
// Z is up (OpenSCAD's convention), the part sits on a to-scale printer bed (bed_mm setting), and there are the usual
// views, edges / wireframe, perspective / orthographic, bed and a turntable. three.js is bundled (works offline).
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const PART_COLOR = 0xf2b84a;                       // warm gold (amber theme)
const DIRS = {                                     // where the camera sits, looking at the part
  iso: [0.85, -1.25, 0.95], front: [0, -1, 0], back: [0, 1, 0], left: [-1, 0, 0], right: [1, 0, 0],
  top: [0, -0.0001, 1], bottom: [0, -0.0001, -1],
};

export class ModelView {
  constructor(host, { token = "", bed = [220, 220, 220] } = {}) {
    this.host = host;
    this.bedSize = bed;
    THREE.Object3D.DEFAULT_UP.set(0, 0, 1);
    const r = (this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }));
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    r.setClearColor(0x000000, 0);
    host.appendChild(r.domElement);

    this.scene = new THREE.Scene();
    this.persp = new THREE.PerspectiveCamera(35, 1, 0.5, 8000);
    this.ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, -8000, 8000);
    this.persp.up.set(0, 0, 1);
    this.ortho.up.set(0, 0, 1);
    this.camera = this.persp;
    this.persp.position.set(160, -220, 180);

    this.controls = new OrbitControls(this.camera, r.domElement);
    Object.assign(this.controls, { enableDamping: true, dampingFactor: 0.09, screenSpacePanning: true,
      autoRotateSpeed: 2.2, zoomSpeed: 1.1 });
    // Until someone grabs the part, keep it framed as the viewport changes size (layout settling,
    // window resizing, panels moving). Once they've moved the camera, leave it where they put it.
    this.autoFit = "iso";
    this.controls.addEventListener("start", () => { this.autoFit = null; });

    // light: soft sky/ground plus a headlight riding on the camera, so every face reads from every angle
    this.scene.add(new THREE.HemisphereLight(0xfff1e0, 0x2a1a0e, 1.05));
    this.head = new THREE.DirectionalLight(0xffffff, 1.6);
    this.scene.add(this.head, this.head.target);
    const fill = new THREE.DirectionalLight(0xffc890, 0.35);
    fill.position.set(-200, 150, -120);
    this.scene.add(fill);

    this.bed = this.makeBed();
    this.scene.add(this.bed);

    this.material = new THREE.MeshStandardMaterial({ color: PART_COLOR, roughness: 0.55, metalness: 0.04,
      transparent: true, opacity: 1, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
    this.edgeMat = new THREE.LineBasicMaterial({ color: 0x1a0f04, transparent: true, opacity: 0.55 });
    this.mesh = null;
    this.edges = null;
    this.size = new THREE.Vector3(40, 40, 20);
    this.loader = new STLLoader();
    if (token) this.loader.setRequestHeader({ "x-jarvis-token": token });
    this.tween = null;
    this.flags = { edges: true, wire: false, ortho: false, spin: false, bed: true };

    new ResizeObserver(() => this.resize()).observe(host);
    this.resize();
    const loop = () => { requestAnimationFrame(loop); this.frame(); };
    loop();
  }

  makeBed() {
    const [BX, BY] = this.bedSize;
    const g = new THREE.Group();
    const plate = new THREE.Mesh(new THREE.PlaneGeometry(BX, BY),
      new THREE.MeshBasicMaterial({ color: 0x140b05, transparent: true, opacity: 0.6, depthWrite: false }));
    plate.position.z = -0.05;
    g.add(plate);
    const lines = [];
    for (let v = -BY / 2; v <= BY / 2 + 0.01; v += 10) lines.push(-BX / 2, v, 0, BX / 2, v, 0);
    for (let v = -BX / 2; v <= BX / 2 + 0.01; v += 10) lines.push(v, -BY / 2, 0, v, BY / 2, 0);
    const grid = new THREE.BufferGeometry();
    grid.setAttribute("position", new THREE.Float32BufferAttribute(lines, 3));
    g.add(new THREE.LineSegments(grid, new THREE.LineBasicMaterial({ color: 0x6a4320, transparent: true, opacity: 0.35 })));
    const rim = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(
      [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y]) => new THREE.Vector3(x * BX / 2, y * BY / 2, 0))),
      new THREE.LineBasicMaterial({ color: 0xffb26b, transparent: true, opacity: 0.75 }));
    g.add(rim);
    // X / Y / Z at the front-left corner, like OpenSCAD's axes
    const o = new THREE.Vector3(-BX / 2, -BY / 2, 0);
    for (const [dir, color] of [[[1, 0, 0], 0xff5a5a], [[0, 1, 0], 0x57e389], [[0, 0, 1], 0x5aa9ff]]) {
      g.add(new THREE.ArrowHelper(new THREE.Vector3(...dir), o, 22, color, 5, 3));
    }
    return g;
  }

  resize() {
    const w = this.host.clientWidth || 1, h = this.host.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.persp.aspect = w / h;
    this.persp.updateProjectionMatrix();
    this.fitOrtho();
    if (this.autoFit && this.mesh) this.setView(this.autoFit, false);
  }

  radius() { return Math.max(this.size.length() / 2, 8); }

  fitOrtho() {
    const w = this.host.clientWidth || 1, h = this.host.clientHeight || 1;
    const half = this.radius() * 1.25;
    Object.assign(this.ortho, { left: -half * w / h, right: half * w / h, top: half, bottom: -half });
    this.ortho.updateProjectionMatrix();
  }

  async load(url, keepView) {
    const geo = await new Promise((ok, bad) => this.loader.load(url, ok, undefined, bad));
    geo.computeBoundingBox();
    const bb = geo.boundingBox, c = new THREE.Vector3();
    bb.getCenter(c);
    geo.translate(-c.x, -c.y, -bb.min.z);              // centred on the bed, resting on it
    this.size = bb.getSize(new THREE.Vector3());
    if (this.mesh) { this.scene.remove(this.mesh, this.edges); this.mesh.geometry.dispose(); this.edges.geometry.dispose(); }
    this.mesh = new THREE.Mesh(geo, this.material);
    this.edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 28), this.edgeMat);
    this.scene.add(this.mesh, this.edges);
    this.apply();
    this.resize();                                      // measure the viewport now, not whenever it last changed
    if (!keepView) { this.autoFit = "iso"; this.setView("iso", false); }
    return this.size;
  }

  clear() {
    if (this.mesh) { this.scene.remove(this.mesh, this.edges); this.mesh = null; this.edges = null; }
  }

  dim(on) { this.material.opacity = on ? 0.28 : 1; this.edgeMat.opacity = on ? 0.15 : 0.55; }

  setView(name, animate = true) {
    const d = new THREE.Vector3(...(DIRS[name] || DIRS.iso)).normalize();
    const target = new THREE.Vector3(0, 0, this.size.z / 2);
    // fit whichever way is tighter: a tall narrow viewport has less room across than up
    const vHalf = THREE.MathUtils.degToRad(this.persp.fov / 2);
    const hHalf = Math.atan(Math.tan(vHalf) * this.persp.aspect);
    const dist = (this.radius() / Math.sin(Math.min(vHalf, hHalf))) * 1.22;
    const to = target.clone().addScaledVector(d, dist);
    if (this.flags.ortho) { this.ortho.zoom = 1; this.ortho.updateProjectionMatrix(); }
    if (animate) this.autoFit = name;                   // a chosen view stays framed until the user moves
    if (!animate) { this.camera.position.copy(to); this.controls.target.copy(target); this.controls.update(); return; }
    this.tween = { t0: performance.now(), dur: 520, fromP: this.camera.position.clone(), toP: to,
      fromT: this.controls.target.clone(), toT: target };
  }

  toggle(flag) {
    this.flags[flag] = !this.flags[flag];
    if (flag === "ortho") {
      const next = this.flags.ortho ? this.ortho : this.persp;
      next.position.copy(this.camera.position);
      next.quaternion.copy(this.camera.quaternion);
      this.camera = next;
      this.controls.object = next;
      this.fitOrtho();
      this.controls.update();
    }
    this.apply();
    return this.flags[flag];
  }

  apply() {
    this.material.wireframe = this.flags.wire;
    if (this.edges) this.edges.visible = this.flags.edges && !this.flags.wire;
    this.bed.visible = this.flags.bed;
    this.controls.autoRotate = this.flags.spin;
  }

  frame() {
    if (!this.host.offsetParent) return;                 // hidden: don't burn the GPU
    if (this.tween) {
      const k = Math.min(1, (performance.now() - this.tween.t0) / this.tween.dur);
      const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
      this.camera.position.lerpVectors(this.tween.fromP, this.tween.toP, e);
      this.controls.target.lerpVectors(this.tween.fromT, this.tween.toT, e);
      if (k >= 1) this.tween = null;
    }
    this.controls.update();
    this.head.position.copy(this.camera.position);
    this.head.target.position.copy(this.controls.target);
    this.renderer.render(this.scene, this.camera);
  }
}
