#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatten an SVG into polylines in inches (x right, y down). Standard library only.

Supports path (M L H V C S Q T A Z, absolute and relative), rect, circle, ellipse, line, polyline,
polygon, nested groups and the transform attribute. Text, clipping, <use> and CSS-only transforms are
not supported: convert text to paths in Inkscape first (Path > Object to Path).
"""
import math, re
import xml.etree.ElementTree as ET

UNITS = {"": 1 / 96, "px": 1 / 96, "in": 1.0, "mm": 1 / 25.4, "cm": 1 / 2.54, "pt": 1 / 72, "pc": 1 / 6}


def _length(s, default=None):
    if s is None:
        return default
    m = re.match(r"\s*([-+]?[0-9.]+(?:e[-+]?\d+)?)\s*([a-z%]*)", s.strip(), re.I)
    if not m or m.group(2) == "%":
        return default
    return float(m.group(1)) * UNITS.get(m.group(2).lower(), 1 / 96) * 96   # -> user units (px)


def _mul(a, b):   # 2x3 affine matrices (a, b, c, d, e, f)
    return (a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
            a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
            a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5])


def _apply(m, p):
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def _transform(s):
    m = (1, 0, 0, 1, 0, 0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", args)]
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            a = math.radians(v[0]); c, s_ = math.cos(a), math.sin(a)
            t = (c, s_, -s_, c, 0, 0)
            if len(v) == 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX":
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY":
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mul(m, t)
    return m


def _cubic(p0, p1, p2, p3, n):
    return [tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t * t * c + t ** 3 * d
                  for a, b, c, d in zip(p0, p1, p2, p3)) for t in (i / n for i in range(1, n + 1))]


def _arc(p0, rx, ry, phi, large, sweep, p1, n):
    """SVG elliptical arc, endpoint parameterisation (SVG 1.1 appendix F.6)."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [p1]
    rx, ry, phi = abs(rx), abs(ry), math.radians(phi)
    c, s = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = c * dx + s * dy, -s * dx + c * dy
    lam = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = max(rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1, 0)
    k = math.sqrt(num / (rx * rx * y1 * y1 + ry * ry * x1 * x1)) * (-1 if large == sweep else 1)
    cx1, cy1 = k * rx * y1 / ry, -k * ry * x1 / rx
    cx, cy = c * cx1 - s * cy1 + (p0[0] + p1[0]) / 2, s * cx1 + c * cy1 + (p0[1] + p1[1]) / 2
    ang = lambda ux, uy: math.atan2(uy, ux)
    t1 = ang((x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = ang((-x1 - cx1) / rx, (-y1 - cy1) / ry) - t1
    if sweep and dt < 0: dt += 2 * math.pi
    if not sweep and dt > 0: dt -= 2 * math.pi
    out = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        out.append((cx + rx * math.cos(t) * c - ry * math.sin(t) * s, cy + rx * math.cos(t) * s + ry * math.sin(t) * c))
    out[-1] = p1
    return out


def _path(d, n=24):
    """Parse path data into a list of (points, closed) in user units. Curves get n pieces (refined later)."""
    tok = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", d or "")
    subs, cur, pos, start, i, cmd, last_ctrl = [], [], (0.0, 0.0), (0.0, 0.0), 0, None, None
    def num():
        nonlocal i
        v = float(tok[i]); i += 1
        return v
    def flag():           # arc flags may be written without separators ("a1 1 0 01 2 2")
        nonlocal i
        t = tok[i]
        if len(t) > 1 and t[0] in "01" and "." not in t[:1]:
            tok[i] = t[1:]; return int(t[0])
        i += 1; return int(float(t))
    while i < len(tok):
        if re.match(r"[A-Za-z]", tok[i]):
            cmd = tok[i]; i += 1
            if cmd in "Zz":
                if cur:
                    subs.append((cur + [start], True)); cur = []
                pos = start; last_ctrl = None
                continue
        rel = cmd.islower(); C = cmd.upper()
        off = pos if rel else (0.0, 0.0)
        if C == "M":
            if cur: subs.append((cur, False))
            pos = (num() + off[0], num() + off[1]); start = pos; cur = [pos]
            cmd = "l" if rel else "L"; last_ctrl = None
        elif C == "L":
            pos = (num() + off[0], num() + off[1]); cur.append(pos); last_ctrl = None
        elif C == "H":
            pos = (num() + off[0], pos[1]); cur.append(pos); last_ctrl = None
        elif C == "V":
            pos = (pos[0], num() + off[1]); cur.append(pos); last_ctrl = None
        elif C in "CS":
            if C == "C":
                c1 = (num() + off[0], num() + off[1])
            else:
                c1 = (2 * pos[0] - last_ctrl[0], 2 * pos[1] - last_ctrl[1]) if last_ctrl and last_ctrl[2] == "c" else pos
            c2 = (num() + off[0], num() + off[1]); end = (num() + off[0], num() + off[1])
            cur += _cubic(pos, c1, c2, end, n); pos = end; last_ctrl = (c2[0], c2[1], "c")
        elif C in "QT":
            if C == "Q":
                q = (num() + off[0], num() + off[1])
            else:
                q = (2 * pos[0] - last_ctrl[0], 2 * pos[1] - last_ctrl[1]) if last_ctrl and last_ctrl[2] == "q" else pos
            end = (num() + off[0], num() + off[1])
            c1 = (pos[0] + 2 / 3 * (q[0] - pos[0]), pos[1] + 2 / 3 * (q[1] - pos[1]))
            c2 = (end[0] + 2 / 3 * (q[0] - end[0]), end[1] + 2 / 3 * (q[1] - end[1]))
            cur += _cubic(pos, c1, c2, end, n); pos = end; last_ctrl = (q[0], q[1], "q")
        elif C == "A":
            rx, ry, phi = num(), num(), num(); la, sw = flag(), flag()
            end = (num() + off[0], num() + off[1])
            cur += _arc(pos, rx, ry, phi, la, sw, end, n * 2); pos = end; last_ctrl = None
        else:
            i += 1
    if cur:
        subs.append((cur, False))
    return subs


def _shape(el, tag):
    g = lambda k, d=0.0: _length(el.get(k), d)
    if tag == "path":
        return _path(el.get("d"))
    if tag == "rect":
        x, y, w, h = g("x"), g("y"), g("width"), g("height")
        rx = g("rx", None); ry = g("ry", None)
        rx = ry if rx is None else rx; ry = rx if ry is None else ry
        if not rx:
            return [([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)], True)]
        rx, ry = min(rx, w / 2), min(ry, h / 2)
        return _path(f"M{x+rx},{y} H{x+w-rx} A{rx},{ry} 0 0 1 {x+w},{y+ry} V{y+h-ry} A{rx},{ry} 0 0 1 {x+w-rx},{y+h} "
                     f"H{x+rx} A{rx},{ry} 0 0 1 {x},{y+h-ry} V{y+ry} A{rx},{ry} 0 0 1 {x+rx},{y} Z")
    if tag in ("circle", "ellipse"):
        cx, cy = g("cx"), g("cy")
        rx, ry = (g("r"), g("r")) if tag == "circle" else (g("rx"), g("ry"))
        return _path(f"M{cx+rx},{cy} A{rx},{ry} 0 1 1 {cx-rx},{cy} A{rx},{ry} 0 1 1 {cx+rx},{cy} Z", n=48)
    if tag == "line":
        return [([(g("x1"), g("y1")), (g("x2"), g("y2"))], False)]
    if tag in ("polyline", "polygon"):
        v = [float(x) for x in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", el.get("points", ""))]
        pts = list(zip(v[0::2], v[1::2]))
        if tag == "polygon" and pts:
            pts.append(pts[0])
        return [(pts, tag == "polygon")] if len(pts) > 1 else []
    return []


def _simplify(pts, tol):
    """Drop points closer than tol to the line through their neighbours (Douglas-Peucker)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]; bx, by = pts[b]; dx, dy = bx - ax, by - ay; L = math.hypot(dx, dy)
        best, bi = 0.0, None
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dx * (py - ay) - dy * (px - ax)) / L if L else math.hypot(px - ax, py - ay)
            if d > best: best, bi = d, i
        if bi is not None and best > tol:
            keep[bi] = True; stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def flatten(svg_path, tolerance=0.002):
    """Return (polylines, (width_in, height_in)); each polyline is (list of (x, y) inches, closed)."""
    root = ET.parse(svg_path).getroot()
    strip = lambda t: t.split("}")[-1]
    w, h = _length(root.get("width")), _length(root.get("height"))
    vb = [float(x) for x in re.findall(r"[-+]?[0-9.]+(?:e[-+]?\d+)?", root.get("viewBox", ""))]
    base = (1 / 96, 0, 0, 1 / 96, 0, 0)                      # user units (px) -> inches
    if len(vb) == 4 and vb[2] and vb[3]:
        w = w or vb[2]; h = h or vb[3]
        base = _mul(base, (w / vb[2], 0, 0, h / vb[3], -vb[0] * w / vb[2], -vb[1] * h / vb[3]))
    out = []
    def walk(el, m):
        tag = strip(el.tag)
        if tag in ("defs", "clipPath", "mask", "symbol", "pattern", "marker", "metadata", "style", "title", "desc"):
            return
        if el.get("display") == "none" or "display:none" in (el.get("style") or "").replace(" ", ""):
            return
        m = _mul(m, _transform(el.get("transform")))
        for pts, closed in _shape(el, tag):
            q = _simplify([_apply(m, p) for p in pts], tolerance)
            if len(q) > 1:
                out.append((q, closed))
        for ch in el:
            walk(ch, m)
    walk(root, base)
    return out, ((w or 0) / 96, (h or 0) / 96)


if __name__ == "__main__":
    import sys
    polys, size = flatten(sys.argv[1])
    xs = [p[0] for pl, _ in polys for p in pl]; ys = [p[1] for pl, _ in polys for p in pl]
    print(f"page {size[0]:.3f} x {size[1]:.3f} in; {len(polys)} polylines, {len(xs)} points; "
          f"bbox x {min(xs):.3f}..{max(xs):.3f}  y {min(ys):.3f}..{max(ys):.3f}")
