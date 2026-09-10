"""
Bake a high-resolution regional coastline for the coverage plot.

`land.json` is thinned at 0.7° — about 78 km — because it only ever has to
survive being drawn as a globe a few hundred pixels wide. The Method page's
coverage figure is a different problem: it is roughly 400 km from the middle to
the edge, so that same file would put the Jutland coast tens of kilometres from
where it belongs and quietly libel the antenna. A wrong coastline under a
reception envelope is worse than no coastline, which is why that figure went out
without one.

So we bake a second file: the 50m land layer, clipped to what the receiver can
actually hear and thinned at a tolerance appropriate to the scale. Clipping is
what keeps it small — Eurasia is a single enormous ring, and we want a few
degrees of it.

Rings are cut into open polylines rather than kept closed. The figure strokes
coastlines and never fills them, so a run of points that leaves the box and
comes back should be two strokes, not one with a chord across the middle.

Run once (or whenever the receiver moves):
    python scripts/adsb/build_coast.py
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

SRC = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-50m.json"
OUT = Path(__file__).resolve().parents[2] / "data" / "adsb" / "coast.json"

# The receiver, and the range the figure has to cover.
HOME_LAT, HOME_LNG = 56.16, 10.20
RANGE_KM = 480.0            # a little beyond the furthest first contacts

TOLERANCE_KM = 3.0          # vertex thinning, in real distance
PRECISION = 3               # ~110 m at this latitude — below one screen pixel


def bbox() -> tuple[float, float, float, float]:
    """Lat/lng box that comfortably contains the range circle."""
    dlat = RANGE_KM / 111.0
    dlng = RANGE_KM / (111.0 * math.cos(math.radians(HOME_LAT)))
    return (HOME_LAT - dlat, HOME_LAT + dlat,
            HOME_LNG - dlng, HOME_LNG + dlng)


def decode(topo: dict) -> list[list[list[float]]]:
    """Expand TopoJSON delta-encoded arcs into absolute lon/lat rings."""
    tr = topo["transform"]
    sx, sy = tr["scale"]
    tx, ty = tr["translate"]

    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append([x * sx + tx, y * sy + ty])
        arcs.append(pts)

    rings: list[list[list[float]]] = []
    for geom in topo["objects"]["land"]["geometries"]:
        polys = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]]
        for poly in polys:
            for ring_arcs in poly:
                ring: list[list[float]] = []
                for i in ring_arcs:
                    seg = arcs[~i][::-1] if i < 0 else arcs[i]
                    ring.extend(seg if not ring else seg[1:])
                if len(ring) >= 2:
                    rings.append(ring)
    return rings


def clip(ring, lo_lat, hi_lat, lo_lng, hi_lng) -> list[list[list[float]]]:
    """
    Cut a ring into the runs of it that fall inside the box.

    One vertex of slack is kept on each side of a crossing so a stroke runs to
    the edge of the figure instead of stopping short of it and leaving a gap
    where the coast should continue past the rim.
    """
    inside = [lo_lng <= p[0] <= hi_lng and lo_lat <= p[1] <= hi_lat for p in ring]
    if not any(inside):
        return []

    runs, cur = [], []
    for i, p in enumerate(ring):
        if inside[i]:
            if not cur and i > 0:
                cur.append(ring[i - 1])       # step back over the boundary
            cur.append(p)
        elif cur:
            cur.append(p)                     # and one step past it
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2]


def thin(line, tol_km: float):
    """Drop vertices closer than `tol_km` to the last one kept."""
    kd_lat = 111.0
    kd_lng = 111.0 * math.cos(math.radians(HOME_LAT))
    out = [line[0]]
    for p in line[1:-1]:
        dx = (p[0] - out[-1][0]) * kd_lng
        dy = (p[1] - out[-1][1]) * kd_lat
        if math.hypot(dx, dy) >= tol_km:
            out.append(p)
    out.append(line[-1])
    return out


def main() -> int:
    lo_lat, hi_lat, lo_lng, hi_lng = bbox()
    print(f"[coast] window lat {lo_lat:.2f}..{hi_lat:.2f}  lng {lo_lng:.2f}..{hi_lng:.2f}")
    print(f"[coast] fetching {SRC}")
    topo = json.loads(urllib.request.urlopen(SRC, timeout=120).read())

    rings = decode(topo)
    print(f"[coast] {len(rings)} rings decoded")

    lines = []
    for r in rings:
        for run in clip(r, lo_lat, hi_lat, lo_lng, hi_lng):
            t = thin(run, TOLERANCE_KM)
            if len(t) >= 2:
                lines.append([[round(x, PRECISION), round(y, PRECISION)] for x, y in t])

    verts = sum(len(l) for l in lines)
    payload = {
        "origin": {"lat": HOME_LAT, "lng": HOME_LNG},
        "range_km": RANGE_KM,
        "source": "world-atlas land-50m",
        "lines": lines,
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"[coast] {len(lines)} polylines, {verts:,} vertices -> {OUT.name} ({kb:.0f} KB)")
    if kb > 160:
        print("[coast] warning: larger than expected; raise TOLERANCE_KM", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
