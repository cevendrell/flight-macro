"""
Generate the social card and app icons from the same ingredients as the site.

The card is not decoration: it draws the real coastline out of
data/adsb/land.json with the same orthographic projection the globe page uses,
with great-circle arcs converging on the antenna. Rerun it when the palette or
the wording changes; the PNGs it writes are committed.

    python scripts/make_og.py

Fonts: Archivo and IBM Plex Mono, the two faces the site loads. They are
fetched to a local cache on first run (both SIL OFL). Without a network the
script falls back to a system sans, which looks off-brand but still renders.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("pip install pillow", file=sys.stderr)
    raise SystemExit(1)

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "assets"
LAND = REPO / "data" / "adsb" / "land.json"
CACHE = Path.home() / ".cache" / "ovrhead-fonts"

# The site's tokens, verbatim.
NAVY = (5, 22, 77)
SPHERE = (10, 32, 96)
LANDFILL = (18, 46, 118)
PERI = (130, 165, 214)
ON_NAVY = (238, 241, 248)
AMBER = (210, 149, 92)

FONTS = {
    "sans-600": "https://fonts.gstatic.com/s/archivo/v25/k3k6o8UDI-1M0wlSV9XAw6lQkqWY8Q82sJaRE-NWIDdgffTT6jRp8A.ttf",
    "sans-500": "https://fonts.gstatic.com/s/archivo/v25/k3k6o8UDI-1M0wlSV9XAw6lQkqWY8Q82sJaRE-NWIDdgffTTBjNp8A.ttf",
    "mono-500": "https://fonts.gstatic.com/s/ibmplexmono/v20/-F6qfjptAgt5VM-kVkqdyU8n3twJ8lc.ttf",
}
FALLBACK = "/System/Library/Fonts/HelveticaNeue.ttc"


def font(key: str, size: int) -> ImageFont.FreeTypeFont:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{key}.ttf"
    if not path.exists():
        try:
            urllib.request.urlretrieve(FONTS[key], path)
        except Exception as exc:                       # noqa: BLE001
            print(f"  ! {key}: {exc} — falling back to a system face", file=sys.stderr)
            return ImageFont.truetype(FALLBACK, size)
    return ImageFont.truetype(str(path), size)


def tracked(d: ImageDraw.ImageDraw, xy, text, f, fill, track):
    """Letter-spaced text. PIL has no tracking, and the wordmark needs it."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) + track
    return x


def make_globe(size: int, rot_lng: float, rot_lat: float, home) -> Image.Image:
    """Orthographic hemisphere, drawn the way the globe page draws it."""
    S = 3                                              # supersample
    img = Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    R = size * S / 2 - 2
    cx = cy = size * S / 2
    p0 = math.radians(rot_lat)

    def project(lat, lng):
        l = math.radians(lng + rot_lng)
        p = math.radians(lat)
        cosc = math.sin(p0) * math.sin(p) + math.cos(p0) * math.cos(p) * math.cos(l)
        if cosc < 0:
            return None
        return (cx + R * math.cos(p) * math.sin(l),
                cy - R * (math.cos(p0) * math.sin(p)
                          - math.sin(p0) * math.cos(p) * math.cos(l)))

    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=SPHERE + (255,))

    for lat in range(-60, 61, 30):
        run = []
        for lng in range(-180, 181, 2):
            q = project(lat, lng)
            if q:
                run.append(q)
            elif len(run) > 1:
                d.line(run, fill=PERI + (46,), width=S); run = []
            else:
                run = []
        if len(run) > 1:
            d.line(run, fill=PERI + (46,), width=S)
    for lng in range(-180, 180, 30):
        run = []
        for lat in range(-90, 91, 2):
            q = project(lat, lng)
            if q:
                run.append(q)
            elif len(run) > 1:
                d.line(run, fill=PERI + (46,), width=S); run = []
            else:
                run = []
        if len(run) > 1:
            d.line(run, fill=PERI + (46,), width=S)

    for ring in json.loads(LAND.read_text()):
        pts = [project(lat, lng) for lng, lat in ring]
        run = []
        for q in pts:
            if q:
                run.append(q)
            elif len(run) > 2:
                d.polygon(run, fill=LANDFILL + (255,), outline=PERI + (120,)); run = []
            else:
                run = []
        if len(run) > 2:
            d.polygon(run, fill=LANDFILL + (255,), outline=PERI + (120,))

    def great_circle(a, b, steps=64):
        def xyz(lat, lng):
            p, l = math.radians(lat), math.radians(lng)
            return (math.cos(p) * math.cos(l), math.cos(p) * math.sin(l), math.sin(p))
        A, B = xyz(*a), xyz(*b)
        dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(A, B))))
        om = math.acos(dot)
        for i in range(steps + 1):
            t = i / steps
            if om < 1e-6:
                v = A
            else:
                s1, s2 = math.sin((1 - t) * om) / math.sin(om), math.sin(t * om) / math.sin(om)
                v = tuple(A[j] * s1 + B[j] * s2 for j in range(3))
            yield (math.degrees(math.asin(v[2])), math.degrees(math.atan2(v[1], v[0])))

    # A handful of the corridors the antenna actually sees.
    for origin in [(35.86, 104.20), (37.09, -95.71), (23.42, 53.85),
                   (60.13, 18.64), (53.41, -8.24), (35.86, 127.77)]:
        run = []
        for lat, lng in great_circle(origin, home):
            q = project(lat, lng)
            if q:
                run.append(q)
            elif len(run) > 1:
                d.line(run, fill=AMBER + (150,), width=S); run = []
            else:
                run = []
        if len(run) > 1:
            d.line(run, fill=AMBER + (150,), width=S)

    hq = project(*home)
    if hq:
        r = 5 * S
        d.ellipse([hq[0] - r, hq[1] - r, hq[0] + r, hq[1] + r], fill=AMBER + (255,))
        for ring_r, alpha in ((12 * S, 150), (20 * S, 80)):
            d.ellipse([hq[0] - ring_r, hq[1] - ring_r, hq[0] + ring_r, hq[1] + ring_r],
                      outline=AMBER + (alpha,), width=max(1, S))

    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=PERI + (110,), width=S)
    return img.resize((size, size), Image.LANCZOS)


def make_og() -> None:
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)

    globe = make_globe(560, rot_lng=-10.2, rot_lat=26, home=(56.16, 10.20))
    img.paste(globe, (700, 35), globe)

    x = 78
    tracked(d, (x, 92), "OVRHEAD", font("mono-500", 27), PERI, 9)
    d.text((x, 168), "An observatory", font=font("sans-600", 76), fill=ON_NAVY)
    d.text((x, 246), "over Aarhus.", font=font("sans-600", 76), fill=ON_NAVY)
    for i, line in enumerate([
        "One antenna records every aircraft that",
        "passes overhead. The whole record is",
        "public, and queryable in your browser.",
    ]):
        d.text((x, 360 + i * 34), line, font=font("sans-500", 25), fill=PERI)
    d.line([(x, 492), (x + 86, 492)], fill=AMBER, width=2)
    tracked(d, (x, 514), "56.16°N  10.20°E  ·  1090 MHz  ·  ADS-B",
            font("mono-500", 19), PERI, 1.6)

    ASSETS.mkdir(exist_ok=True)
    img.save(ASSETS / "og.png", optimize=True)
    print("assets/og.png", (ASSETS / "og.png").stat().st_size // 1024, "KB")


def make_icons() -> None:
    """The favicon mark, scaled up: the antenna and its two range rings."""
    for size in (180, 192, 512):
        S = 4
        n = size * S
        img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * 0.22), fill=NAVY + (255,))
        c = n / 2
        for r, alpha, w in ((n * 0.235, 190, n * 0.034), (n * 0.39, 90, n * 0.034)):
            d.ellipse([c - r, c - r, c + r, c + r], outline=PERI + (alpha,), width=int(w))
        r = n * 0.082
        d.ellipse([c - r, c - r, c + r, c + r], fill=AMBER + (255,))
        img.resize((size, size), Image.LANCZOS).save(ASSETS / f"icon-{size}.png", optimize=True)
        print(f"assets/icon-{size}.png")


if __name__ == "__main__":
    make_og()
    make_icons()
