"""Diagrams: Jarvis draws math and science visuals - graphs of equations and inequalities, number lines,
geometry figures, charts, and flowcharts - as a picture in the chat. (Ported from Jarvis v1.)

Drawn with matplotlib; equations parsed with sympy (so '2x', 'x^2', 'sqrt(x)', 'sin(x)' all work), never eval'd.
Pictures go to <data>/media/ (the app serves them from there). Dark (the app's look) by default; style='paper'
gives white-background versions for printing or homework.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from .. import config
from .registry import ToolError, tool

OUT = config.DATA_DIR / "media"
THEMES = {
    "dark": {"bg": "#0b0603", "fg": "#f6dcc0", "axis": "#a07c5c", "grid": "#2a1a0e", "fill_alpha": 0.18,
             "colors": ["#ffb26b", "#5fd6ff", "#ff7a9a", "#8dff9e", "#c9a2ff", "#ffe2a0"]},
    "paper": {"bg": "#ffffff", "fg": "#111111", "axis": "#333333", "grid": "#dddddd", "fill_alpha": 0.15,
              "colors": ["#1f6feb", "#d97706", "#db2777", "#16a34a", "#7c3aed", "#ea580c"]},
}


def _plt(style):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t = THEMES.get(style, THEMES["dark"])
    plt.rcParams.update({"figure.facecolor": t["bg"], "axes.facecolor": t["bg"], "savefig.facecolor": t["bg"],
                         "text.color": t["fg"], "axes.labelcolor": t["fg"], "axes.edgecolor": t["axis"],
                         "xtick.color": t["axis"], "ytick.color": t["axis"], "font.size": 12, "axes.titleweight": "bold"})
    return plt, t


def _save(fig, plt, kind):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    fig.savefig(p, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return p


# ---- maths parsing (sympy, never eval) ------------------------------------------------------------------------------
REL = re.compile(r"(<=|>=|≤|≥|<|>|=)")


_FUNCS = {"sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh", "sqrt", "log", "ln", "exp",
          "abs", "Abs", "floor", "ceiling", "ceil", "pi", "e", "x", "y"}
_SAFE_CHARS = re.compile(r"^[0-9a-zA-Z\s.+\-*/^()]*$")


def _safe(expr: str) -> str:
    """sympy's parse_expr evaluates Python under the hood, so only plain maths may reach it:
    digits, x, y, + - * / ^ ( ) . and known function names - no quotes, underscores, brackets or other names."""
    if not _SAFE_CHARS.match(expr):
        raise ValueError(f"'{expr}' has characters that aren't maths")
    for word in re.findall(r"[A-Za-z]+", expr):
        # implicit multiplication like 2x or xy is fine; any other word must be a known function
        if word not in _FUNCS and not re.fullmatch(r"[xy]+", word):
            raise ValueError(f"unknown name '{word}' in '{expr}' (use x, y, numbers and sin/cos/tan/sqrt/log/exp/abs)")
    return expr


def _sym(expr: str):
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application, convert_xor
    import sympy as sp
    expr = expr.replace("π", "pi").replace("√", "sqrt").replace("−", "-").replace("×", "*").replace("÷", "/")
    expr = _safe(expr.replace("ln(", "log("))
    x, y = sp.symbols("x y")
    return parse_expr(expr, local_dict={"x": x, "y": y, "e": sp.E, "pi": sp.pi},
                      transformations=standard_transformations + (implicit_multiplication_application, convert_xor))


def _split(eq: str):
    parts = REL.split(eq.replace(" ", ""))
    if len(parts) != 3:
        raise ValueError(f"couldn't read '{eq}' - write it like y=2x+1, y<=x^2, x^2+y^2=25 or x=3")
    rel = {"≤": "<=", "≥": ">="}.get(parts[1], parts[1])
    return parts[0], rel, parts[2]


def _items(s: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;\n]", s or "") if p.strip()]


def _points(s: str):
    """'A(1,2); B(-3, 4); (0,5)' -> [(label, x, y)]"""
    out = []
    for m in re.finditer(r"([A-Za-z][\w']*)?\s*\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)", s or ""):
        out.append((m.group(1) or "", float(m.group(2)), float(m.group(3))))
    return out


def _range(s: str, default):
    try:
        a, b = [float(v) for v in re.split(r"[,:\s]+", s.strip()) if v]
        return (a, b) if a < b else default
    except Exception:
        return default


# ---- graph -----------------------------------------------------------------------------------------------------------
def _graph(equations, points, x_range, y_range, title, style, mark):
    import numpy as np
    import sympy as sp
    plt, t = _plt(style)
    x, y = sp.symbols("x y")
    xr = _range(x_range, (-10, 10))
    eqs = _items(equations)
    fig, ax = plt.subplots(figsize=(7, 7))
    X = np.linspace(xr[0], xr[1], 1200)
    ys_seen = []
    notes = []
    for i, eq in enumerate(eqs):
        col = t["colors"][i % len(t["colors"])]
        lhs, rel, rhs = _split(eq)
        dashed = rel in ("<", ">")
        if lhs == "y" and "y" not in rhs:
            f = sp.lambdify(x, _sym(rhs), "numpy")
            with np.errstate(all="ignore"):
                Y = np.asarray(f(X), dtype=float) * np.ones_like(X)
            Y[~np.isfinite(Y)] = np.nan
            if np.nanmax(np.abs(np.diff(Y))) > (xr[1] - xr[0]) * 5 if np.isfinite(Y).sum() > 2 else False:
                jumps = np.abs(np.diff(Y)) > (xr[1] - xr[0]) * 5
                Y[1:][jumps] = np.nan                               # don't join across asymptotes
            ax.plot(X, Y, color=col, lw=2.4, ls="--" if dashed else "-", label=f"${sp.latex(sp.Eq(y, _sym(rhs))) if rel == '=' else 'y ' + {'<=': chr(92) + 'leq ', '>=': chr(92) + 'geq ', '<': '<', '>': '>'}[rel] + ' ' + sp.latex(_sym(rhs))}$")
            ys_seen.extend(Y[np.isfinite(Y)].tolist())
            if rel != "=":
                lo, hi = ax.get_ylim() if ys_seen else (-10, 10)
                big = 1e3
                if rel in ("<", "<="):
                    ax.fill_between(X, -big, Y, color=col, alpha=t["fill_alpha"], lw=0)
                else:
                    ax.fill_between(X, Y, big, color=col, alpha=t["fill_alpha"], lw=0)
            if mark and len(eqs) <= 3:                             # intercepts: where it crosses the axes
                try:
                    e = _sym(rhs)
                    y0 = float(e.subs(x, 0))
                    if math.isfinite(y0):
                        ax.plot([0], [y0], "o", color=col, ms=6); ax.annotate(f"(0, {y0:g})", (0, y0), textcoords="offset points", xytext=(8, 6), color=col, fontsize=10)
                    for r in sp.solve(sp.Eq(e, 0), x)[:4]:
                        if r.is_real and xr[0] <= float(r) <= xr[1]:
                            ax.plot([float(r)], [0], "o", color=col, ms=6)
                            ax.annotate(f"({float(r):.3g}, 0)", (float(r), 0), textcoords="offset points", xytext=(6, -14), color=col, fontsize=10)
                except Exception:
                    pass
        elif lhs == "x" and "x" not in rhs and "y" not in rhs:
            xv = float(_sym(rhs))
            ax.axvline(xv, color=col, lw=2.4, ls="--" if dashed else "-", label=f"$x {'=' if rel == '=' else rel} {xv:g}$")
            if rel != "=":
                lo, hi = xr
                ax.axvspan(lo if rel in ("<", "<=") else xv, xv if rel in ("<", "<=") else hi, color=col, alpha=t["fill_alpha"], lw=0)
        else:                                                      # implicit: circles, ellipses, anything in x and y
            g = sp.lambdify((x, y), _sym(lhs) - _sym(rhs), "numpy")
            yr0 = _range(y_range, xr)
            GX, GY = np.meshgrid(np.linspace(xr[0], xr[1], 500), np.linspace(yr0[0], yr0[1], 500))
            with np.errstate(all="ignore"):
                Z = np.asarray(g(GX, GY), dtype=float) * np.ones_like(GX)
            ax.contour(GX, GY, Z, levels=[0], colors=[col], linewidths=2.4, linestyles="--" if dashed else "-")
            if rel != "=":
                ax.contourf(GX, GY, Z, levels=[-1e9, 0] if rel in ("<", "<=") else [0, 1e9], colors=[col], alpha=t["fill_alpha"])
            ax.plot([], [], color=col, lw=2.4, label=f"${sp.latex(_sym(lhs))} {'=' if rel == '=' else rel.replace('<=', chr(92) + 'leq ').replace('>=', chr(92) + 'geq ')} {sp.latex(_sym(rhs))}$")
    pts = _points(points)
    # where the curves cross each other - for a system of equations, that IS the answer
    explicit = []
    for eq in eqs:
        lhs, rel, rhs = _split(eq)
        if lhs == "y" and "y" not in rhs:
            explicit.append(_sym(rhs))
    crossings = []
    for i in range(len(explicit)):
        for j in range(i + 1, len(explicit)):
            try:
                for r in sp.solve(sp.Eq(explicit[i], explicit[j]), x)[:4]:
                    if r.is_real and xr[0] <= float(r) <= xr[1]:
                        cy = float(explicit[i].subs(x, r))
                        crossings.append((float(r), cy))
            except Exception:
                pass
    for (cx_, cy_) in crossings:
        ax.plot([cx_], [cy_], "o", color=t["fg"], ms=9, mec=t["colors"][1], mew=2, zorder=6)
        ax.annotate(f"({cx_:.3g}, {cy_:.3g})", (cx_, cy_), textcoords="offset points", xytext=(10, -16), color=t["fg"], fontsize=12, fontweight="bold")
    for j, (lab, px, py) in enumerate(pts):
        ax.plot([px], [py], "o", color=t["fg"], ms=7, zorder=5)
        ax.annotate(f"{lab}({px:g}, {py:g})" if lab else f"({px:g}, {py:g})", (px, py), textcoords="offset points", xytext=(8, 8), color=t["fg"], fontsize=11)
    ax.set_xlim(*xr)
    if y_range:
        ax.set_ylim(*_range(y_range, xr))
    else:
        # like graph paper: the same scale both ways, centred on what matters (crossings, points, intercepts);
        # curves that leave the paper just leave it, as on a worksheet
        span = xr[1] - xr[0]
        focus = [c[1] for c in crossings] + [p[2] for p in pts]
        mid = (max(focus) + min(focus)) / 2 if focus else 0.0
        if focus and (max(focus) - min(focus)) > span * 0.9:
            ax.set_ylim(min(focus) - span * 0.1, max(focus) + span * 0.1)
        else:
            ax.set_ylim(mid - span / 2, mid + span / 2)
    from matplotlib.ticker import MaxNLocator
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=12)); ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=12))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_position("zero"); ax.spines["bottom"].set_position("zero")
    ax.grid(True, color=t["grid"], lw=0.8); ax.set_axisbelow(True)
    ax.set_aspect("equal" if abs((ax.get_ylim()[1] - ax.get_ylim()[0]) - (xr[1] - xr[0])) < (xr[1] - xr[0]) * 0.6 else "auto")
    if eqs:
        leg = ax.legend(loc="upper left", fontsize=11, framealpha=0.85, facecolor=t["bg"], edgecolor=t["grid"])
        for txt in leg.get_texts():
            txt.set_color(t["fg"])
    if title:
        ax.set_title(title, color=t["fg"], pad=14)
    return fig, plt, f"graph of {', '.join(eqs) or 'points'}" + (f" with {len(pts)} points" if pts else "")


# ---- number line -----------------------------------------------------------------------------------------------------
def _number_line(inequalities, points, x_range, title, style):
    plt, t = _plt(style)
    ineqs = _items(inequalities)
    rows = []                                                      # each: list of (lo, lo_closed, hi, hi_closed)
    for s in ineqs:
        parts = []
        for piece in re.split(r"\s+or\s+|\|\|", s, flags=re.I):
            p = piece.replace(" ", "").replace("≤", "<=").replace("≥", ">=")
            m = re.fullmatch(r"(-?[\d./]+)(<=|<)x(<=|<)(-?[\d./]+)", p)
            if m:
                parts.append((float(_sym(m.group(1))), m.group(2) == "<=", float(_sym(m.group(4))), m.group(3) == "<="))
                continue
            m = re.fullmatch(r"x(<=|>=|<|>|=)(-?[\d./]+)", p) or re.fullmatch(r"(-?[\d./]+)(<=|>=|<|>|=)x", p)
            if not m:
                raise ValueError(f"couldn't read '{piece}' - write it like x>3, x<=-1, -2<=x<5")
            if p.startswith("x"):
                op, v = m.group(1), float(_sym(m.group(2)))
            else:
                v, op = float(_sym(m.group(1))), {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "="}[m.group(2)]
            parts.append({"<": (-math.inf, False, v, False), "<=": (-math.inf, False, v, True), ">": (v, False, math.inf, False),
                          ">=": (v, True, math.inf, False), "=": (v, True, v, True)}[op])
        rows.append((s, parts))
    vals = [v for _, ps in rows for a, _, b, _ in ps for v in (a, b) if math.isfinite(v)] + [x for _, x, _ in _points(points)]
    lo, hi = _range(x_range, (min(vals + [0]) - 4, max(vals + [0]) + 4))
    fig, ax = plt.subplots(figsize=(8, 1.4 + 0.9 * max(1, len(rows))))
    for r, (label, parts) in enumerate(rows):
        yv = -r
        col = t["colors"][r % len(t["colors"])]
        ax.annotate("", xy=(hi + 0.5, yv), xytext=(lo - 0.5, yv), arrowprops=dict(arrowstyle="<->", color=t["axis"], lw=1.5))
        for k in range(math.ceil(lo), math.floor(hi) + 1):
            ax.plot([k, k], [yv - 0.08, yv + 0.08], color=t["axis"], lw=1)
            ax.text(k, yv - 0.28, f"{k}", ha="center", va="top", fontsize=10, color=t["fg"])
        for a, ac, b, bc in parts:
            a2, b2 = (lo - 0.5 if a == -math.inf else a), (hi + 0.5 if b == math.inf else b)
            if a2 != b2:
                ax.annotate("", xy=(b2, yv), xytext=(a2, yv), arrowprops=dict(arrowstyle=("<->" if a == -math.inf and b == math.inf else "->" if b == math.inf else "<-" if a == -math.inf else "-"), color=col, lw=5))
            for v, closed in ((a, ac), (b, bc)):
                if math.isfinite(v):
                    ax.plot([v], [yv], "o", ms=13, mfc=col if closed else t["bg"], mec=col, mew=2.5, zorder=5)
        nice = re.sub(r"\s+or\s+", lambda _m: r"\ \mathrm{or}\ ", label, flags=re.I)
        ax.text(lo - 0.7, yv + 0.32, f"${nice.replace('<=', chr(92) + 'leq ').replace('>=', chr(92) + 'geq ')}$", color=col, fontsize=12, ha="left")
    for lab, px, _ in _points(points):
        ax.plot([px], [0], "o", ms=8, color=t["fg"]); ax.text(px, 0.25, lab or f"{px:g}", ha="center", color=t["fg"])
    ax.set_xlim(lo - 1, hi + 1); ax.set_ylim(-len(rows) + 0.3, 0.8); ax.axis("off")
    if title:
        ax.set_title(title, color=t["fg"])
    return fig, plt, f"number line for {', '.join(ineqs)}"


# ---- geometry -------------------------------------------------------------------------------------------------------
def _geometry(data, title, style):
    import numpy as np
    plt, t = _plt(style)
    d = json.loads(data) if isinstance(data, str) else (data or {})
    P = {k: (float(v[0]), float(v[1])) for k, v in (d.get("points") or {}).items()}
    fig, ax = plt.subplots(figsize=(7, 7))
    col = t["colors"]
    for i, poly in enumerate(d.get("polygons") or []):
        xy = [P[p] for p in poly]
        ax.fill(*zip(*xy), color=col[i % len(col)], alpha=t["fill_alpha"])
        ax.plot(*zip(*(xy + [xy[0]])), color=col[i % len(col)], lw=2.4)
    for seg in d.get("segments") or []:
        a, b = P[seg[0]], P[seg[1]]
        ax.plot([a[0], b[0]], [a[1], b[1]], color=t["fg"], lw=1.6, ls="--" if (len(seg) > 3 and seg[3] == "dashed") else "-")
        if len(seg) > 2 and seg[2]:
            ax.text((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, f" {seg[2]}", color=t["fg"], fontsize=11)
    cx = np.mean([p[0] for p in P.values()]) if P else 0
    cy = np.mean([p[1] for p in P.values()]) if P else 0
    for side, lab in (d.get("side_labels") or {}).items():
        a, b = P[side[0]], P[side[1]]
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        nx, ny = mx - cx, my - cy
        n = math.hypot(nx, ny) or 1
        ax.text(mx + nx / n * 0.45, my + ny / n * 0.45, lab, color=col[1], fontsize=12, ha="center", va="center", fontweight="bold")
    for c in d.get("circles") or []:
        ctr = P[c["center"]] if isinstance(c.get("center"), str) else tuple(c.get("center", (0, 0)))
        ax.add_patch(plt.Circle(ctr, float(c["r"]), fill=True, color=col[2], alpha=t["fill_alpha"]))
        ax.add_patch(plt.Circle(ctr, float(c["r"]), fill=False, color=col[2], lw=2.4))
        if c.get("label"):
            ax.plot([ctr[0], ctr[0] + float(c["r"])], [ctr[1], ctr[1]], color=col[2], lw=1.5, ls="--")
            ax.text(ctr[0] + float(c["r"]) / 2, ctr[1] + 0.2, c["label"], color=col[2], ha="center", fontsize=12)
    for ang in d.get("angles") or []:
        v = P[ang["at"]]; a, b = P[ang["between"][0]], P[ang["between"][1]]
        t1 = math.degrees(math.atan2(a[1] - v[1], a[0] - v[0])); t2 = math.degrees(math.atan2(b[1] - v[1], b[0] - v[0]))
        if (t2 - t1) % 360 > 180:
            t1, t2 = t2, t1
        size = 0.12 * max(1.0, max(abs(p) for q in P.values() for p in q))
        if ang.get("right"):
            u1 = np.array([math.cos(math.radians(t1)), math.sin(math.radians(t1))]) * size * 0.8
            u2 = np.array([math.cos(math.radians(t2)), math.sin(math.radians(t2))]) * size * 0.8
            sq = [v + u1, v + u1 + u2, v + u2]
            ax.plot([p[0] for p in sq], [p[1] for p in sq], color=col[3], lw=1.8)
        else:
            from matplotlib.patches import Arc
            ax.add_patch(Arc(v, 2 * size, 2 * size, theta1=t1, theta2=t2, color=col[3], lw=1.8))
        if ang.get("label"):
            mid = math.radians(t1 + ((t2 - t1) % 360) / 2)
            ax.text(v[0] + math.cos(mid) * size * 1.7, v[1] + math.sin(mid) * size * 1.7, ang["label"], color=col[3], fontsize=12, ha="center", va="center")
    for k, (px, py) in P.items():
        ax.plot([px], [py], "o", color=t["fg"], ms=6, zorder=5)
        nx, ny = px - cx, py - cy
        n = math.hypot(nx, ny) or 1
        ax.text(px + nx / n * 0.35, py + ny / n * 0.35, k, color=t["fg"], fontsize=14, fontweight="bold", ha="center", va="center")
    ax.set_aspect("equal"); ax.axis("off")
    ax.autoscale(); ax.margins(0.18)
    if d.get("show_grid"):
        ax.axis("on"); ax.grid(True, color=t["grid"])
    if title:
        ax.set_title(title, color=t["fg"])
    return fig, plt, f"geometry figure with {len(P)} labelled points"


# ---- charts -----------------------------------------------------------------------------------------------------------
def _chart(data, title, style):
    import numpy as np
    plt, t = _plt(style)
    d = json.loads(data) if isinstance(data, str) else (data or {})
    kind = (d.get("type") or "bar").lower()
    labels = d.get("labels") or []
    series = d.get("series") or ({"values": d["values"]} if "values" in d else {})
    fig, ax = plt.subplots(figsize=(8, 5.5))
    col = t["colors"]
    if kind == "pie":
        name, vals = next(iter(series.items()))
        w, txts, auto = ax.pie(vals, labels=labels, autopct="%1.0f%%", colors=col[:len(vals)], startangle=90,
                               wedgeprops={"edgecolor": t["bg"], "linewidth": 2}, textprops={"color": t["fg"]})
        for a in auto:
            a.set_color(t["bg"]); a.set_fontweight("bold")
        ax.axis("equal")
    elif kind in ("bar", "column", "barh"):
        n = len(series); w = 0.8 / max(1, n); idx = np.arange(len(labels))
        for i, (name, vals) in enumerate(series.items()):
            pos = idx - 0.4 + w * (i + 0.5)
            bars = (ax.barh if kind == "barh" else ax.bar)(pos, vals, w * 0.92, color=col[i % len(col)], label=name)
            if len(labels) * n <= 16:
                ax.bar_label(bars, fmt="%g", color=t["fg"], fontsize=10, padding=3)
        (ax.set_yticks if kind == "barh" else ax.set_xticks)(idx, labels)
    elif kind in ("line", "area"):
        for i, (name, vals) in enumerate(series.items()):
            xs = labels if labels else list(range(len(vals)))
            ax.plot(xs, vals, "o-", color=col[i % len(col)], lw=2.4, ms=5, label=name)
            if kind == "area":
                ax.fill_between(range(len(vals)), vals, color=col[i % len(col)], alpha=t["fill_alpha"])
    elif kind == "scatter":
        for i, (name, pts) in enumerate(series.items()):
            xs, ys = zip(*pts)
            ax.scatter(xs, ys, color=col[i % len(col)], s=40, label=name)
            if d.get("trendline"):
                m, b = np.polyfit(xs, ys, 1)
                xx = np.linspace(min(xs), max(xs), 50)
                m, b = round(float(m), 3), round(float(b), 3)       # no 1.84e-15 noise in the label
                sign = "-" if b < 0 else "+"
                label = f"y = {m:g}x" + (f" {sign} {abs(b):g}" if b else "")
                ax.plot(xx, m * xx + b, "--", color=col[i % len(col)], lw=1.5, label=label)
    elif kind in ("histogram", "hist"):
        name, vals = next(iter(series.items()))
        ax.hist(vals, bins=d.get("bins", "auto"), color=col[0], edgecolor=t["bg"])
    else:
        raise ValueError(f"unknown chart type '{kind}' - bar, barh, line, area, scatter, pie or histogram")
    if kind != "pie":
        ax.grid(True, axis="y", color=t["grid"]); ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.set_xlabel(d.get("x_label", "")); ax.set_ylabel(d.get("y_label", ""))
        if len(series) > 1 or d.get("trendline"):
            leg = ax.legend(framealpha=0.85, facecolor=t["bg"], edgecolor=t["grid"])
            for txt in leg.get_texts():
                txt.set_color(t["fg"])
    if title:
        ax.set_title(title, color=t["fg"], pad=12)
    return fig, plt, f"{kind} chart" + (f" of {', '.join(series)}" if series else "")


# ---- flowcharts ---------------------------------------------------------------------------------------------------------
def _flow(data, title, style):
    plt, t = _plt(style)
    d = json.loads(data) if isinstance(data, str) else (data or {})
    nodes = d.get("nodes") or {}
    edges = d.get("edges") or []
    # layers: longest path from the roots (a simple top-to-bottom layout)
    level = {k: 0 for k in nodes}
    for _ in range(len(nodes)):
        for e in edges:
            if e[0] in level and e[1] in level and level[e[1]] < level[e[0]] + 1 and level[e[0]] < len(nodes):
                level[e[1]] = level[e[0]] + 1
    rows = {}
    for k, lv in level.items():
        rows.setdefault(lv, []).append(k)
    horizontal = (d.get("direction") or "TB").upper() == "LR"
    pos = {}
    for lv, ks in rows.items():
        for i, k in enumerate(ks):
            off = (i - (len(ks) - 1) / 2) * 3.2
            pos[k] = (lv * 3.4, -off) if horizontal else (off, -lv * 1.8)
    fig, ax = plt.subplots(figsize=(8, max(3, 1.6 * len(rows))) if not horizontal else (max(6, 2.6 * len(rows)), 4))
    for e in edges:
        if e[0] in pos and e[1] in pos:
            (x1, y1), (x2, y2) = pos[e[0]], pos[e[1]]
            ax.annotate("", xy=(x2, y2 + (0 if horizontal else 0.42)), xytext=(x1, y1 - (0 if horizontal else 0.42)),
                        arrowprops=dict(arrowstyle="-|>", color=t["axis"], lw=1.6, shrinkA=8, shrinkB=12))
            if len(e) > 2 and e[2]:
                ax.text((x1 + x2) / 2 + 0.15, (y1 + y2) / 2, e[2], color=t["colors"][1], fontsize=10)
    for i, (k, label) in enumerate(nodes.items()):
        x, y = pos[k]
        shape = "round,pad=0.45" if level[k] in (0, max(level.values())) else "square,pad=0.45"
        ax.text(x, y, label, ha="center", va="center", fontsize=11, color=t["fg"], wrap=True,
                bbox=dict(boxstyle=shape, fc=t["bg"], ec=t["colors"][0], lw=2))
    xs = [p[0] for p in pos.values()] or [0]; ys = [p[1] for p in pos.values()] or [0]
    ax.set_xlim(min(xs) - 2.2, max(xs) + 2.2); ax.set_ylim(min(ys) - 1.2, max(ys) + 1.2); ax.axis("off")
    if title:
        ax.set_title(title, color=t["fg"])
    return fig, plt, f"flowchart with {len(nodes)} steps"


@tool("diagram", description=("Draw a math or science picture and send it in the chat: GRAPHS of equations/inequalities (kind=graph, "
      "equations='y=2x+1; y<=x^2-3; x^2+y^2=25; x=4' - shaded for inequalities, intercepts marked), NUMBER LINES for "
      "inequalities (kind=number_line, inequalities='x>3; -2<=x<5; x<=-1 or x>2'), GEOMETRY figures (kind=geometry, "
      "data JSON: {\"points\":{\"A\":[0,0],\"B\":[4,0],\"C\":[0,3]}, \"polygons\":[[\"A\",\"B\",\"C\"]], "
      "\"side_labels\":{\"AB\":\"4 cm\",\"BC\":\"5 cm\"}, \"angles\":[{\"at\":\"A\",\"between\":[\"B\",\"C\"],\"right\":true}], "
      "\"circles\":[{\"center\":[0,0],\"r\":3,\"label\":\"r = 3\"}], \"segments\":[[\"A\",\"C\",\"h\",\"dashed\"]]}), CHARTS (kind=chart, data JSON: "
      "{\"type\":\"bar|barh|line|area|scatter|pie|histogram\", \"labels\":[...], \"series\":{\"name\":[values]}, \"x_label\":\"\", "
      "\"y_label\":\"\"} - scatter series are [[x,y],...], add \"trendline\":true for a best-fit line), and FLOWCHARTS "
      "(kind=flow, data JSON: {\"nodes\":{\"a\":\"Start\",\"b\":\"Heat water\"}, \"edges\":[[\"a\",\"b\",\"optional label\"]], \"direction\":\"TB|LR\"}). "
      "Use it whenever a picture would help explain homework or data (graphing a line, showing a solution set, labelling a "
      "triangle, plotting lab results). Put the returned `show` value in your reply so the picture appears."),
      risk="low", timeout=90, tags=["graph", "plot", "diagram", "chart", "draw", "number line", "geometry", "flowchart", "triangle", "equation", "inequality"])
def diagram(kind: str, equations: str = "", inequalities: str = "", points: str = "", x_range: str = "", y_range: str = "",
            data: str = "", title: str = "", style: str = "dark", mark_intercepts: bool = True) -> dict:
    kind = (kind or "").lower().replace("-", "_").replace(" ", "_")
    if kind in ("graph", "plot", "function"):
        fig, plt, what = _graph(equations, points, x_range, y_range, title, style, mark_intercepts)
    elif kind in ("number_line", "numberline"):
        fig, plt, what = _number_line(inequalities or equations, points, x_range, title, style)
    elif kind in ("geometry", "shape"):
        fig, plt, what = _geometry(data, title, style)
    elif kind in ("chart", "bar", "pie", "line", "scatter"):
        if kind != "chart" and data:
            dd = json.loads(data); dd.setdefault("type", kind); data = json.dumps(dd)
        fig, plt, what = _chart(data, title, style)
    elif kind in ("flow", "flowchart", "diagram"):
        fig, plt, what = _flow(data, title, style)
    else:
        raise ToolError("kind must be graph, number_line, geometry, chart or flow")
    p = _save(fig, plt, kind)
    from ..events import bus

    bus.emit("media", url=f"/api/media/{p.name}", alt=what)   # shown even if the reply forgets to include it
    return {"drew": what, "image": str(p), "shown": "in the chat"}

