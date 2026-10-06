"""Point at things on screen by sight - for a main model that can't see (or icons with no text for OCR).

Vision models are good at "which labelled square is the gear icon in?" and bad at raw pixel coordinates, so:
  1. the screenshot gets a labelled grid (A1, B1, ... 8 x 6) and the vision model names the square,
  2. that square and its neighbours are cropped at full resolution with a finer grid, and it names a square again,
  3. the middle of that small square, mapped back to the screen, is where to click.
"""

from __future__ import annotations

import re

import numpy as np

COLS, ROWS = 8, 6           # first pass over the whole image
FINE = 6                    # second pass over the zoomed 3 x 3 neighbourhood


def label(c: int, r: int) -> str:
    return f"{chr(65 + c)}{r + 1}"


def draw_grid(img: np.ndarray, cols: int, rows: int) -> np.ndarray:
    """A copy of the image with thin grid lines and a readable label in each square's top-left corner."""
    import cv2

    out = img.copy()
    h, w = out.shape[:2]
    cw, ch = w / cols, h / rows
    over = out.copy()
    for c in range(1, cols):
        x = int(c * cw)
        cv2.line(over, (x, 0), (x, h), (0, 255, 255), max(1, w // 900))
    for r in range(1, rows):
        y = int(r * ch)
        cv2.line(over, (0, y), (w, y), (0, 255, 255), max(1, w // 900))
    out = cv2.addWeighted(over, 0.55, out, 0.45, 0)
    scale = max(0.45, min(1.2, min(cw, ch) / 110))
    th = max(1, int(scale * 2))
    for r in range(rows):
        for c in range(cols):
            t = label(c, r)
            (tw, tht), _ = cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, scale, th)
            x, y = int(c * cw) + 3, int(r * ch) + 3
            cv2.rectangle(out, (x, y), (x + tw + 6, y + tht + 8), (0, 0, 0), -1)
            cv2.putText(out, t, (x + 3, y + tht + 4), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 255, 255), th, cv2.LINE_AA)
    return out


def parse_cell(text: str, cols: int, rows: int) -> tuple[int, int] | None:
    """'C4', 'cell c4.', 'The gear is in D2' -> (col, row); 'NONE' / nonsense -> None. The last valid label wins
    (models tend to reason first and answer last)."""
    if re.search(r"\bnone\b|not (visible|found|there)|can'?t see", text, re.I) and not re.search(r"\b[A-Z]\d{1,2}\b", text):
        return None
    hit = None
    for m in re.finditer(r"\b([A-Za-z])\s?(\d{1,2})\b", text):
        c, r = ord(m.group(1).upper()) - 65, int(m.group(2)) - 1
        if 0 <= c < cols and 0 <= r < rows:
            hit = (c, r)
    return hit


def cell_box(w: int, h: int, cols: int, rows: int, c: int, r: int) -> tuple[float, float, float, float]:
    cw, ch = w / cols, h / rows
    return c * cw, r * ch, cw, ch


def neighbourhood(w: int, h: int, c: int, r: int) -> tuple[int, int, int, int]:
    """The chosen first-pass square plus one square around it (clamped to the image): x, y, width, height."""
    cw, ch = w / COLS, h / ROWS
    c0, r0 = max(0, c - 1), max(0, r - 1)
    c1, r1 = min(COLS, c + 2), min(ROWS, r + 2)
    return int(c0 * cw), int(r0 * ch), int((c1 - c0) * cw), int((r1 - r0) * ch)


PROMPT = ("This picture has a yellow grid; each square is labelled in its top-left corner ({first} ... {last}). "
          "Which square contains the CENTRE of: {target}?\n"
          "Reply with just the square's label (like {example}). If it isn't visible, reply NONE.")


async def locate(img: np.ndarray, target: str) -> tuple[int, int] | None:
    """Where `target` is in `img`, as pixel coordinates of the image - or None if the vision model can't find it."""
    from . import see

    h, w = img.shape[:2]
    ask = lambda im, cols, rows: see.ask(  # noqa: E731
        [draw_grid(im, cols, rows)],
        PROMPT.format(first=label(0, 0), last=label(cols - 1, rows - 1), target=target, example=label(2, 1)),
        max_tokens=400, max_side=1600)
    first = parse_cell(await ask(img, COLS, ROWS), COLS, ROWS)
    if first is None:
        return None
    x, y, nw, nh = neighbourhood(w, h, *first)
    crop = img[y:y + nh, x:x + nw]
    zoom = 1.0
    if max(nw, nh) < 900:                       # small screens / small windows: blow it up so the labels don't hide it
        import cv2

        zoom = 900 / max(nw, nh)
        crop = cv2.resize(crop, (int(nw * zoom), int(nh * zoom)), interpolation=cv2.INTER_CUBIC)
    fine = parse_cell(await ask(crop, FINE, FINE), FINE, FINE)
    if fine is None:                            # lost it when zoomed in: the middle of the first square is still close
        bx, by, bw, bh = cell_box(w, h, COLS, ROWS, *first)
        return int(bx + bw / 2), int(by + bh / 2)
    bx, by, bw, bh = cell_box(crop.shape[1], crop.shape[0], FINE, FINE, *fine)
    return int(x + (bx + bw / 2) / zoom), int(y + (by + bh / 2) / zoom)
