"""
Bake a simplified world coastline into the repo for the globe view.

The previous site pulled three.js, globe.gl, topojson-client and world-atlas
from a CDN at runtime — roughly 600 KB and four things that can break
independently of us. The globe only needs land outlines, so we fetch them
once, decode the topology, drop the smallest islands, thin the vertices and
store the result as a plain array of rings.

Run once (or whenever you want to re-simplify):
    python scripts/adsb/build_land.py
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

SRC = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-110m.json"
OUT = Path(__file__).resolve().parents[2] / "data" / "adsb" / "land.json"

MIN_AREA = 3.0      # square degrees; below this a ring is an island we can skip
TOLERANCE = 0.7     # degrees; vertex-thinning distance
PRECISION = 2       # decimal places kept per coordinate


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

    def resolve(idx: int) -> list[list[float]]:
        # A negative index means "traverse this arc backwards".
        return arcs[idx][::-1] if idx < 0 else arcs[~(~idx)]

    rings: list[list[list[float]]] = []
    for geom in topo["objects"]["land"]["geometries"]:
        polys = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]]
        for poly in polys:
            for ring_arcs in poly:
                ring: list[list[float]] = []
                for i in ring_arcs:
                    seg = arcs[~i][::-1] if i < 0 else arcs[i]
                    ring.extend(seg if not ring else seg[1:])
                if len(ring) >= 4:
                    rings.append(ring)
    return rings


def bbox_area(ring) -> float:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def thin(ring, tol: float):
    """Keep a vertex only once it is `tol` degrees from the last one kept."""
    out = [ring[0]]
    for p in ring[1:-1]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= tol:
            out.append(p)
    out.append(ring[-1])
    return out


def main() -> int:
    print(f"[land] fetching {SRC}")
    topo = json.loads(urllib.request.urlopen(SRC, timeout=60).read())

    rings = decode(topo)
    print(f"[land] {len(rings)} rings decoded")

    kept = []
    for r in rings:
        if bbox_area(r) < MIN_AREA:
            continue
        t = thin(r, TOLERANCE)
        if len(t) >= 4:
            kept.append([[round(x, PRECISION), round(y, PRECISION)] for x, y in t])

    kept.sort(key=bbox_area, reverse=True)
    verts = sum(len(r) for r in kept)

    OUT.write_text(json.dumps(kept, separators=(",", ":")), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"[land] kept {len(kept)} rings, {verts:,} vertices -> {OUT.name} ({kb:.0f} KB)")
    if kb > 120:
        print("[land] warning: larger than expected; raise TOLERANCE or MIN_AREA",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
