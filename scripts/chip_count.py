#!/usr/bin/env python3
"""Count poker chips of two configurable colors on an overhead table shot.

Pipeline:
  1. Auto-crop to the bright wood table (Otsu threshold → biggest bright blob).
  2. Detect round objects with cv2.HoughCircles — the "model" that finds chips.
  3. For each circle, sample a disc around the center and classify by mean HSV
     against two caller-provided color specs. Nearest hue within tolerance wins.

Input:  a directory of JPEG/PNG shots.
Output: JSON report + annotated debug crops.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import cv2
import numpy as np


# ------------- color config --------------------------------------------------

@dataclass
class ColorSpec:
    name: str          # label to report, e.g. "red"
    hue: int           # center hue in OpenCV HSV scale [0..180]
    tolerance: int     # max hue distance (wraps around 180) to accept
    min_s: int = 70    # minimum saturation — rejects wood grain & shadows
    min_v: int = 50    # minimum value

    def matches(self, h: float, s: float, v: float) -> bool:
        if s < self.min_s or v < self.min_v:
            return False
        d = abs(h - self.hue)
        d = min(d, 180 - d)  # hue wraps around 180
        return d <= self.tolerance


# Players bet with two chip colors. Overridable via CLI.
DEFAULT_COLORS = {
    "player_1": ColorSpec(name="red",   hue=0,  tolerance=12, min_s=120, min_v=100),  # rejects laptop-screen reds
    "player_2": ColorSpec(name="green", hue=52, tolerance=22),  # a.04 overhead greens read ~37, phone samples ~60
}


# ------------- crop ----------------------------------------------------------

def crop_to_table(bgr: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    num, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if num <= 1:
        h, w = bgr.shape[:2]
        return bgr, (0, 0, w, h)
    areas = stats[1:, cv2.CC_STAT_AREA]
    idx = int(np.argmax(areas)) + 1
    x = int(stats[idx, cv2.CC_STAT_LEFT])
    y = int(stats[idx, cv2.CC_STAT_TOP])
    w = int(stats[idx, cv2.CC_STAT_WIDTH])
    h = int(stats[idx, cv2.CC_STAT_HEIGHT])
    pad_x = int(w * 0.02)
    pad_y = int(h * 0.02)
    x1, y1 = x + pad_x, y + pad_y
    x2, y2 = x + w - pad_x, y + h - pad_y
    return bgr[y1:y2, x1:x2].copy(), (x1, y1, x2 - x1, y2 - y1)


# ------------- circle detection (the model) --------------------------------

def find_circles(bgr: np.ndarray) -> np.ndarray:
    """Return an (N, 3) array of [x, y, radius] for round objects on the table."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 7)
    # Expect chip radius to be a small-but-visible fraction of the table.
    h, w = bgr.shape[:2]
    min_dim = min(h, w)
    min_r = max(20, int(min_dim * 0.025))
    max_r = max(min_r + 10, int(min_dim * 0.08))
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=int(min_dim * 0.06),
        param1=120,          # Canny upper threshold
        param2=35,           # accumulator threshold — lower = more hits
        minRadius=min_r,
        maxRadius=max_r,
    )
    if circles is None:
        return np.zeros((0, 3), dtype=np.float32)
    return circles[0].astype(np.float32)


# ------------- color classification -----------------------------------------

def sample_hsv(bgr: np.ndarray, x: int, y: int, r: int) -> tuple[float, float, float]:
    """Mean HSV over the chip's colored ring (0.4r..0.9r), ignoring low-sat pixels.

    Poker chips have a colored outer ring, white rim dots, and a mostly-white
    inner badge. A central disc would be dominated by white — so we sample an
    annulus, then additionally mask out near-white pixels before averaging.
    """
    h, w = bgr.shape[:2]
    rr = max(4, int(r))
    x0, y0 = max(0, x - rr), max(0, y - rr)
    x1, y1 = min(w, x + rr), min(h, y + rr)
    patch = bgr[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0, 0.0, 0.0
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    yy, xx = np.ogrid[:patch.shape[0], :patch.shape[1]]
    cy, cx = patch.shape[0] / 2, patch.shape[1] / 2
    d2 = (xx - cx) ** 2 + (yy - cy) ** 2
    r_in = (rr * 0.4) ** 2
    r_out = (rr * 0.9) ** 2
    ring = (d2 >= r_in) & (d2 <= r_out)
    # Drop near-white / low-saturation pixels so white rim dots don't pull the mean.
    sat = hsv[..., 1]
    val = hsv[..., 2]
    colored = ring & (sat >= 60) & (val >= 40)
    if colored.sum() < 20:
        return 0.0, float(sat[ring].mean() if ring.any() else 0), float(val[ring].mean() if ring.any() else 0)
    hue = hsv[..., 0][colored].astype(np.float32)
    rad = hue * (np.pi / 90.0)
    mean_hue = (np.arctan2(np.sin(rad).mean(), np.cos(rad).mean()) * (90.0 / np.pi)) % 180.0
    return float(mean_hue), float(sat[colored].mean()), float(val[colored].mean())


@dataclass
class ChipHit:
    color: str
    player: str
    x: int
    y: int
    radius: float
    mean_h: float
    mean_s: float
    mean_v: float


def classify(circles: np.ndarray, bgr: np.ndarray,
             colors: dict[str, ColorSpec]) -> list[ChipHit]:
    hits: list[ChipHit] = []
    for cx, cy, r in circles:
        h, s, v = sample_hsv(bgr, int(cx), int(cy), int(r))
        # Pick whichever configured color is closest within tolerance.
        best: tuple[str, ColorSpec, float] | None = None
        for player, spec in colors.items():
            if not spec.matches(h, s, v):
                continue
            d = abs(h - spec.hue)
            d = min(d, 180 - d)
            if best is None or d < best[2]:
                best = (player, spec, d)
        if best is None:
            continue
        player, spec, _ = best
        hits.append(ChipHit(
            color=spec.name, player=player,
            x=int(cx), y=int(cy), radius=float(r),
            mean_h=float(h), mean_s=float(s), mean_v=float(v),
        ))
    return hits


# ------------- driver --------------------------------------------------------

def annotate(bgr: np.ndarray, hits: list[ChipHit]) -> np.ndarray:
    out = bgr.copy()
    palette = {"red": (0, 0, 255), "green": (0, 200, 0),
               "blue": (255, 100, 0), "yellow": (0, 220, 220)}
    for c in hits:
        color = palette.get(c.color, (255, 255, 255))
        cv2.circle(out, (c.x, c.y), int(c.radius), color, 3)
        cv2.putText(out, c.color[:1].upper(), (c.x - 10, c.y + 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return out


def analyze(bgr: np.ndarray, colors: dict[str, ColorSpec]
            ) -> tuple[dict, np.ndarray, np.ndarray]:
    """Run the full pipeline on an in-memory BGR frame.

    Returns (report, cropped, annotated_crop). Used by both the batch CLI and
    the live overhead app (scripts/overhead_app.py).
    """
    cropped, box = crop_to_table(bgr)
    circles = find_circles(cropped)
    hits = classify(circles, cropped, colors)
    counts = {player: 0 for player in colors}
    for h in hits:
        counts[h.player] += 1
    report = {
        "crop": {"x": box[0], "y": box[1], "w": box[2], "h": box[3]},
        "counts": counts,
        "colors": {p: c.name for p, c in colors.items()},
        "circles_found": int(len(circles)),
        "detections": [asdict(h) for h in hits],
    }
    return report, cropped, annotate(cropped, hits)


def process(path: Path, out_dir: Path,
            colors: dict[str, ColorSpec]) -> dict:
    bgr = cv2.imread(str(path))
    if bgr is None:
        raise RuntimeError(f"could not read {path}")
    report, cropped, annotated = analyze(bgr, colors)

    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_dir / f"{path.stem}.crop.jpg"), cropped)
    cv2.imwrite(str(out_dir / f"{path.stem}.annotated.jpg"), annotated)

    return {"file": path.name, **report}


def parse_color_arg(spec: str) -> tuple[str, ColorSpec]:
    """player=name:hue:tol[:min_s[:min_v]]"""
    player, rest = spec.split("=", 1)
    parts = rest.split(":")
    if len(parts) < 3:
        raise ValueError(f"bad --color {spec!r}")
    name = parts[0]
    hue = int(parts[1])
    tol = int(parts[2])
    min_s = int(parts[3]) if len(parts) > 3 else 70
    min_v = int(parts[4]) if len(parts) > 4 else 50
    return player, ColorSpec(name=name, hue=hue, tolerance=tol,
                             min_s=min_s, min_v=min_v)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--color", action="append", default=[],
                    help="override color, e.g. player_1=red:0:12. "
                         "Repeat per player. Format: player=name:hue:tol[:min_s[:min_v]]")
    args = ap.parse_args(argv)

    colors: dict[str, ColorSpec] = dict(DEFAULT_COLORS)
    for s in args.color:
        p, c = parse_color_arg(s)
        colors[p] = c

    in_dir: Path = args.input_dir
    out_dir: Path = args.out or (in_dir / "out")
    files = sorted(p for p in in_dir.iterdir()
                   if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not files:
        print(f"no images in {in_dir}", file=sys.stderr)
        return 1

    report = [process(p, out_dir, colors) for p in files]
    print(json.dumps(report, indent=2))

    print("\nsummary", file=sys.stderr)
    for r in report:
        parts = "  ".join(f"{p}={n}" for p, n in r["counts"].items())
        print(f"  {r['file']}: {parts}  (circles={r['circles_found']})",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
