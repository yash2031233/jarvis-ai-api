# UI Spec — The Jarvis Orb

The orb **is** the UI. Minimal screen: orb, prompt box, voice control, settings button. Nothing else.

**Fidelity requirement:** the rendered orb must match the reference image so closely that in a
side-by-side comparison you cannot tell which is the original.
The image is the source of truth; the text below supports it, it doesn't replace it.

![Orb reference](reference/orb-reference.webp)

---

## 1. Screen layout
```
┌──────────────────────────────────────────────┐
│                                         ⚙    │  settings (top-right, dim until hover)
│                                              │
│                  ( ORB )                     │  centered, ~70% of viewport height
│                                              │
│                                              │
│      ┌──────────────────────────────┐ 🎙     │  prompt box + mic button
│      │ Ask Jarvis…                  │        │  thin amber outline, transparent fill
│      └──────────────────────────────┘        │
└──────────────────────────────────────────────┘
```
- Background: near-black with a faint warm cast (`#070403` → `#000` radial falloff)
- Replies: short text fades in under/over the orb, then fades out; full history in a hidden drawer
- Settings: slide-in panel (API key, base URL, model, voice, hotkey, permissions)
- No chrome: frameless window, draggable from empty space

## 2. Reference analysis (what must be reproduced)

### Palette
| Role | Approx. color |
|---|---|
| Background | `#050302` – `#0a0605` |
| Base trace | amber `#E8A35A` |
| Bright trace / overlaps | `#FFC98A` |
| Hot spots (top-right rim, core streak) | near-white `#FFF1DC` |
| Dim/far traces | `#7A4A22` at low alpha |

### Line language
- **Every structure is made of thin (~1–1.5 px at 1080p) PCB-style traces**: short segments that
  walk in 0°/45°/90° turns, like circuit board routing or "chemical structure" squiggles.
- Many traces end in **tiny dot terminals** (vias).
- Traces are **additive**: overlaps get brighter, dense areas almost bloom to white.
- Soft **bloom/glow** on all lines (tight radius, moderate strength), no hard outer haze.

### Layers (outside → in)
1. **Outer fragmented shell** (r ≈ 1.05–1.15)
   - Broken arcs with gaps; **dense bands of traces at top and bottom** (polar caps),
     sparser on the left/right sides.
   - Edges are ragged — trace bundles fray out at their ends.
   - Brightest region: **top-right rim**, washing toward white.
2. **Concentric arc shell** (r ≈ 0.85–0.95)
   - Several smooth, thin, near-concentric arcs (not full circles), slightly offset from each other.
3. **Sphere surface traces** (r ≈ 0.8)
   - Sparse PCB traces scattered over the sphere, more transparent toward the center (back-facing feel).
4. **Inner core** (r ≈ 0.42)
   - Dense **honeycomb/hexagonal mesh** combined with **many concentric rings** — reads like a
     tunnel/vortex looking into the center. Densest and warmest region.
5. **Equatorial ring** (Saturn-like)
   - Thick band of dense traces, **slightly tilted (~3–5°)**, extending **beyond** the sphere
     on left and right, tapering and fraying at the tips. Brighter where it crosses the core.
6. **Diagonal axis streak**
   - A thin bundle of parallel bright lines from upper-left (~11 o'clock, inside core edge)
     to lower-right (~5 o'clock, inside sphere), crossing through the core. One of the brightest elements.
7. **Secondary streak**
   - A short, dense, comet-like trace bundle on the right side just below the equator, pointing inward.

### Composition
- Orb is centered, roughly circular silhouette with the ring breaking it horizontally.
- Visual weight: core + ring + diagonal streak are the focal points; outer shell frames them.

## 3. Rendering approach
- **Three.js (WebGL2)** in the web UI.
- Traces generated procedurally:
  - Random walks on a (θ, φ) grid with 45°-quantized turns, per layer with its own density map
    (e.g. polar bias for the outer shell, band mask for the ring) → projected onto the layer radius.
  - Core: hex grid + concentric rings, projected onto an inner sphere/tunnel.
  - Streaks: bundles of jittered parallel polylines.
- Rendered as `LineSegments2` (fat lines) or instanced quads for consistent pixel width,
  **additive blending**, depth test off.
- Post-processing: `UnrealBloomPass` (tight), slight tone mapping to push overlaps to white.
- **Seeded RNG** so the orb looks the same every launch (tuned seed checked into the repo).
- Performance target: 60 fps on integrated GPUs (cap trace count; LOD for low-end).

## 4. Motion & states
Idle should look like the still reference with subtle life — never a different design.

| State | Behavior |
|---|---|
| **Idle** | Very slow independent rotation per layer, gentle "breathing" brightness (±5%) |
| **Listening** | Outer shell and ring pulse with mic amplitude; brightness up |
| **Thinking** | Layers rotate faster in opposing directions; traces "flow" (dash offset animation) toward the core |
| **Speaking** | Core and ring pulse with TTS audio amplitude |
| **Working (tools)** | Diagonal streak brightens; small sparks travel along traces |
| **Error** | Brief dim + flicker, then back to idle |

## 5. Fidelity process (how "indistinguishable" is achieved)
1. Build a **comparison harness** page: reference vs. render side-by-side, plus an overlay
   slider and a difference view.
2. Freeze the orb (idle, t=0) at the same resolution and framing as the reference.
3. Tune layer by layer: silhouette → layer radii → trace density/shape → color → bloom.
4. Measure with an image-similarity score (SSIM / perceptual diff) and iterate until the score
   plateaus and a blind A/B can't reliably pick the original.
5. Lock the seed + parameters into `ui/orb/config.json`.

## 6. Controls
- **Prompt box:** Enter to send, ↑ for history, Esc to cancel a running task.
- **Mic button:** click/hold for push-to-talk; shows wake-word status.
- **Settings ⚙:** API key (masked, keychain), test connection, provider/base URL, model picker,
  voice, hotkey, tool permissions.
- Clicking the orb = start listening.
