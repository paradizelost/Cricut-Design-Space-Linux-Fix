#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cut an SVG on the Cricut Maker 3 natively (no Design Space).

Uses the recorded settings for Smart Paper Sticker Cardstock, loaded without a mat, fine-point blade
in clamp B. Artwork must fit in x 0.10..11.30 in, y 0.10..11.00 in; the material must be at least
max(6, artwork bottom + 1.8) inches long.

Usage:
  cricut_svg.py art.svg [--x 0.25] [--y 0.25] [--scale 1.0] [--tolerance 0.002] [--preview out.svg] [--dry-run] [--port PTY]
--x/--y place the top-left corner of the artwork's bounding box (inches from the material's top-left).
"""
import math, sys
from svgflat import flatten
from cricut_cut import Job, pack_records, MAX_RECORDS
from cricut_native import Cricut

AREA = (0.10, 11.30, 0.10, 11.00)    # x min, x max, y min, y max (inches); Design Space has cut to 11.29 x 10.84


def _overcut(pts, dist):
    out = []; left = dist
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg == 0:
            continue
        if seg >= left:
            t = left / seg
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            return out
        out.append(b); left -= seg
    return out


def order(polys):
    """Nearest-neighbour ordering to keep travel moves short."""
    rest = list(polys); out = []; pos = (0.0, 0.0)
    while rest:
        i = min(range(len(rest)), key=lambda k: math.hypot(rest[k][0][0][0] - pos[0], rest[k][0][0][1] - pos[1]))
        p = rest.pop(i); out.append(p); pos = p[0][-1]
    return out


BLADE_OFFSET = 0.4 / 25.4   # inches the fine-point blade tip trails the carriage (0.4 mm, from Design Space's path)
ARC_STEP = math.radians(15)  # Design Space swivels corners in 15 degree steps
MIN_TURN = math.radians(12)  # turns sharper than this get a swivel arc
LEAD_IN = 0.05               # inches of outline cut twice at the start of a closed shape


def _unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]; n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else None


def _swivel(pivot, d_from, d_to, r):
    """Carriage points that swing the blade around a stationary tip from heading d_from to d_to."""
    a0 = math.atan2(d_from[1], d_from[0]); a1 = math.atan2(d_to[1], d_to[0])
    da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    n = max(1, round(abs(da) / ARC_STEP))
    return [(pivot[0] + r * math.cos(a0 + da * i / n), pivot[1] + r * math.sin(a0 + da * i / n)) for i in range(1, n + 1)]


def _turn(d_from, d_to):
    return abs((math.atan2(d_to[1], d_to[0]) - math.atan2(d_from[1], d_from[0]) + math.pi) % (2 * math.pi) - math.pi)


def to_records(polys, r=BLADE_OFFSET, overcut=None, heading=(1.0, 0.0)):
    """Drag-knife compensated carriage path, built the way Design Space builds it: the carriage runs
    r ahead of the blade tip along each segment and swings around the vertex at sharp corners.
    polys are blade-tip paths. heading is the direction the blade points before the first shape
    (the recorded OrientBlade command leaves it pointing along +x)."""
    overcut = r if overcut is None else overcut
    recs = []
    for pts, closed in polys:
        pts = [p for i, p in enumerate(pts) if i == 0 or math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) > 1e-6]
        if len(pts) < 2:
            continue
        if closed:
            # Lead-in: start a little before the closing point, on the outline itself, so the blade settles
            # while cutting along a line that gets cut anyway; then run past the start by the overcut.
            back = _overcut(pts[::-1], LEAD_IN)
            pts = back[::-1][:1] + pts + _overcut(pts, overcut)
        dirs = [_unit(a, b) for a, b in zip(pts, pts[1:])]
        start = (pts[0][0] + r * heading[0], pts[0][1] + r * heading[1])
        recs += [(2, *start)] + [(0, *start)] * 5
        if _turn(heading, dirs[0]) > MIN_TURN:                 # pivot on the spot to face the first segment
            recs += [(0, *q) for q in _swivel(pts[0], heading, dirs[0], r)]
        for i, d in enumerate(dirs):
            end = pts[i + 1]
            recs.append((0, end[0] + r * d[0], end[1] + r * d[1]))
            if i + 1 < len(dirs) and _turn(d, dirs[i + 1]) > MIN_TURN:
                recs += [(0, *q) for q in _swivel(end, d, dirs[i + 1], r)]
        heading = dirs[-1]
        recs.append((2, recs[-1][1], recs[-1][2]))
    return recs


def place(polys, x, y, scale):
    xs = [p[0] for pl, _ in polys for p in pl]; ys = [p[1] for pl, _ in polys for p in pl]
    ox, oy = min(xs), min(ys)
    out = [([((px - ox) * scale + x, (py - oy) * scale + y) for px, py in pl], c) for pl, c in polys]
    w, h = (max(xs) - ox) * scale, (max(ys) - oy) * scale
    return out, (x, y, x + w, y + h)


def preview(polys, bbox, path):
    w, h = 12.0, max(2.0, bbox[3] + 0.3)
    el = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w*80:.0f}" height="{h*80:.0f}" viewBox="0 0 {w} {h}">',
          f'<rect width="{w}" height="{h}" fill="#fff"/>',
          f'<rect x="{AREA[0]}" y="{AREA[2]}" width="{AREA[1]-AREA[0]}" height="{AREA[3]-AREA[2]}" fill="none" stroke="#bbb" stroke-width="0.01" stroke-dasharray="0.05"/>']
    pos = None
    for pts, closed in polys:
        if pos:
            el.append(f'<line x1="{pos[0]:.4f}" y1="{pos[1]:.4f}" x2="{pts[0][0]:.4f}" y2="{pts[0][1]:.4f}" stroke="#39f" stroke-width="0.006" stroke-dasharray="0.03"/>')
        d = " ".join(f"{x:.4f},{y:.4f}" for x, y in pts)
        el.append(f'<polyline points="{d}" fill="none" stroke="#000" stroke-width="0.012"/>')
        el.append(f'<circle cx="{pts[0][0]:.4f}" cy="{pts[0][1]:.4f}" r="0.02" fill="#e33"/>')
        pos = pts[-1]
    el.append("</svg>")
    open(path, "w").write("\n".join(el))


if __name__ == "__main__":
    a = sys.argv
    opt = lambda k, d: float(a[a.index(k) + 1]) if k in a else d
    polys, page = flatten(a[1], tolerance=opt("--tolerance", 0.002))
    if not polys:
        sys.exit("no cuttable shapes found (text must be converted to paths first)")
    polys, bbox = place(polys, opt("--x", 0.25), opt("--y", 0.25), opt("--scale", 1.0))
    polys = order(polys)
    recs = to_records(polys)
    chunks = pack_records(recs)
    print(f"{len(polys)} shapes, {len(recs)} path records in {len(chunks)} commands; "
          f"artwork occupies x {bbox[0]:.2f}..{bbox[2]:.2f} in, y {bbox[1]:.2f}..{bbox[3]:.2f} in", flush=True)
    if "--preview" in a:
        preview(polys, bbox, a[a.index("--preview") + 1])
    if bbox[0] < AREA[0] or bbox[2] > AREA[1] or bbox[1] < AREA[2] or bbox[3] > AREA[3]:
        sys.exit(f"artwork does not fit the supported area x {AREA[0]}..{AREA[1]} in, y {AREA[2]}..{AREA[3]} in; "
                 f"move it with --x/--y or shrink it with --scale")
    if "--dry-run" in a:
        sys.exit(0)
    dev = Cricut(a[a.index("--port") + 1] if "--port" in a else None)
    info = dev.handshake()
    print("connected; status", info["status"], flush=True)
    try:
        Job(dev, log=lambda m: print(m, flush=True)).run(chunks, max_y=bbox[3])
    finally:
        dev.close()
