"""
Generate a large, realistic synthetic insights.json for UX testing.

This is NOT for production — the real pipeline (Eurostat + OpenSky + Claude)
overwrites data/insights.json. Use this to test the frontend with data volumes
similar to what the production pipeline will produce (~100+ signals).

Run: python scripts/generate_sample.py [--n-country 40] [--n-city 80] [--seed 42]

Deltas are drawn from a plausible distribution (most between ±3–8%, some
outliers). Volumes are calibrated per corridor size class. Headlines and
readings are template-driven with real country/theme context — plausible
enough for UX evaluation but not to be treated as real macro claims.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from airports import AIRPORTS
from country_coords import COUNTRIES

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "data" / "insights.json"


# ── Reporter countries we treat as origins for sampled corridors ──────────────
REPORTERS = [
    "DE", "FR", "ES", "IT", "NL", "BE", "PT", "GB", "IE", "AT", "CH", "SE",
    "DK", "NO", "FI", "PL", "CZ", "HU", "GR", "RO",
]

# Popular partners, weighted a bit higher toward EU + neighbors
PARTNERS = [
    "DE", "FR", "ES", "IT", "NL", "BE", "PT", "GB", "IE", "AT", "CH", "SE",
    "DK", "NO", "FI", "PL", "CZ", "HU", "GR", "RO", "TR", "MA", "TN", "EG",
    "AE", "QA", "US", "CA", "MX", "BR", "IN", "CN", "JP", "SG", "TH",
]

THEMES = ["tourism", "business", "migration", "supply-chain"]
THEME_WEIGHTS = [0.45, 0.30, 0.15, 0.10]

# ── Templated readings, keyed by theme. Placeholders: {a} {b} {sign} {mag} ────
READINGS = {
    "tourism": [
        "Sustained {sign} in outbound {a} leisure travel to {b}. Warm-weather corridors leading; short-haul beach destinations dominate the mix.",
        "Peak-season traffic between {a} and {b} moving {sign}. Load factors on {a}–{b} routes are the clearest driver.",
        "Ski / coastal seasonal corridor between {a} and {b} up {mag}%. Tour operators reporting strong pre-book velocity.",
        "{a} → {b} leisure demand recovering to pre-2020 baseline after several soft quarters. VFR (visiting friends & relatives) share climbing.",
        "Currency-driven leisure shift: {a} travellers reallocating to {b} as effective purchasing power widens.",
    ],
    "business": [
        "Corporate travel between {a} and {b} tracking {sign} — front-cabin share rising faster than overall traffic.",
        "{a} → {b} business flows aligned with cross-border investment activity; short-week weekday concentration remains the tell.",
        "Softer trans-national corporate travel on the {a}–{b} corridor. Consistent with sector-level PMI moves this quarter.",
        "Business intensity between {a} and {b} up {mag}% — professional-services and financial-services corridors dominant.",
        "Regional HQ relocation trend continues to lift {a}–{b} business travel. Composition skewed toward mid-week trips.",
    ],
    "migration": [
        "Diaspora / VFR corridor between {a} and {b} moving {sign}. One-way ticket share above baseline suggests structural composition.",
        "Student-visa corridor {a} → {b} strengthening ahead of academic-year start. Long-stay indicators corroborate.",
        "Skilled-migration flow from {a} to {b} shows {mag}% shift YoY — labour-market pull dominating cost dynamics.",
        "Diaspora traffic between {a} and {b} tracking recent policy changes; average stay length has risen materially.",
        "Reverse-migration signal from {b} to {a} beginning to appear in ticket-direction composition. Trend to monitor.",
    ],
    "supply-chain": [
        "Cargo and business travel between {a} and {b} up {mag}% — industrial-hub city pairs leading (not leisure).",
        "Nearshoring-adjacent traffic between {a} and {b} continues to build. Freight capacity on the corridor also expanding.",
        "Manufacturing corridor easing on {a}–{b} — tracks the softer sector PMI in the origin economy.",
        "Trade-corridor traffic {a} ↔ {b} rerouting supply patterns; middle-week concentration and short stays consistent with logistics travel.",
        "Cross-border industrial travel shift on {a}–{b} lane. Composition of carriers changing as freighter capacity comes on.",
    ],
}

HEADLINE_TEMPLATES = {
    True:  ["{a} → {b} up {mag}% YoY", "{a} ↔ {b} climbing {mag}%", "{a} → {b} corridor +{mag}%"],
    False: ["{a} → {b} down {mag}% YoY", "{a} ↔ {b} easing {mag}%", "{a} → {b} corridor −{mag}%"],
}

CONFIDENCE_WEIGHTS = [0.35, 0.5, 0.15]     # high, medium, low


def draw_delta() -> float:
    """Realistic distribution: mostly ±3–8%, some outliers to ±20%."""
    # 80% normal spread, 20% wider tails
    if random.random() < 0.8:
        return random.gauss(0, 4.5)
    return random.gauss(0, 10) * (1 if random.random() < 0.5 else -1)


def draw_volume(is_city: bool, delta: float) -> tuple[int, int]:
    """Return (current, prior) — prior is derived so the ratio matches delta."""
    if is_city:
        base = random.randint(15_000, 480_000)
    else:
        base = random.randint(120_000, 2_400_000)
    current = base
    prior = int(current / (1 + delta / 100)) if delta > -95 else int(current * 0.5)
    return current, max(prior, 1)


def pick_theme() -> str:
    return random.choices(THEMES, weights=THEME_WEIGHTS, k=1)[0]


def render_headline(a: str, b: str, delta: float) -> str:
    up = delta >= 0
    return random.choice(HEADLINE_TEMPLATES[up]).format(a=a, b=b, mag=abs(delta))


def render_reading(theme: str, a: str, b: str, delta: float) -> str:
    up = delta >= 0
    sign_word = "up" if up else "down"
    return random.choice(READINGS[theme]).format(a=a, b=b, sign=sign_word, mag=f"{abs(delta):.1f}").replace("{mag}", f"{abs(delta):.1f}")


def latest_month_label() -> str:
    today = date.today()
    y, m = today.year, today.month - 2
    while m <= 0: m += 12; y -= 1
    py = y - 1
    cur = datetime(y, m, 1).strftime("%b %Y")
    pri = datetime(py, m, 1).strftime("%b %Y")
    return f"{cur} vs {pri}"


def build_country_insight(reporter: str, partner: str, period: str) -> dict | None:
    oc = COUNTRIES.get(reporter); dc = COUNTRIES.get(partner)
    if not oc or not dc or reporter == partner:
        return None
    delta = round(draw_delta(), 1)
    if abs(delta) < 1.5:                     # filter near-zero, they're not signals
        return None
    theme = pick_theme()
    vc, vp = draw_volume(is_city=False, delta=delta)
    conf = random.choices(["high", "medium", "low"], weights=CONFIDENCE_WEIGHTS, k=1)[0]

    return {
        "id": f"{reporter.lower()}-{partner.lower()}-country-{period.replace(' ', '').lower()}",
        "origin": {"name": oc["name"], "code": reporter, "country": oc["name"],
                   "lat": oc["lat"], "lng": oc["lng"], "type": "country"},
        "dest":   {"name": dc["name"], "code": partner,  "country": dc["name"],
                   "lat": dc["lat"], "lng": dc["lng"], "type": "country"},
        "period": period,
        "deltaPct": delta,
        "volumeCurrent": vc,
        "volumePrior":   vp,
        "theme": theme,
        "headline": render_headline(oc["name"], dc["name"], delta),
        "reading":  render_reading(theme, oc["name"], dc["name"], delta),
        "confidence": conf,
    }


def build_city_insight(reporter: str, airport_icao: str, period: str) -> dict | None:
    oc = COUNTRIES.get(reporter)
    ap = AIRPORTS.get(airport_icao)
    if not oc or not ap:
        return None
    if ap["country"] == reporter:            # same-country airport isn't a corridor
        return None
    dest_country = COUNTRIES.get(ap["country"])
    if not dest_country:
        return None

    delta = round(draw_delta(), 1)
    if abs(delta) < 2.0:
        return None
    theme = pick_theme()
    vc, vp = draw_volume(is_city=True, delta=delta)
    conf = random.choices(["high", "medium", "low"], weights=CONFIDENCE_WEIGHTS, k=1)[0]

    city_label = f"{ap['city']} ({ap['iata']})"
    return {
        "id": f"{reporter.lower()}-{ap['iata'].lower()}-city-{period.replace(' ', '').lower()}",
        "origin": {"name": oc["name"], "code": reporter, "country": oc["name"],
                   "lat": oc["lat"], "lng": oc["lng"], "type": "country"},
        "dest":   {"name": city_label, "code": ap["country"], "country": dest_country["name"],
                   "lat": ap["lat"], "lng": ap["lng"], "type": "city"},
        "period": period,
        "deltaPct": delta,
        "volumeCurrent": vc,
        "volumePrior":   vp,
        "theme": theme,
        "headline": render_headline(oc["name"], city_label, delta),
        "reading":  render_reading(theme, oc["name"], city_label, delta),
        "confidence": conf,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-country", type=int, default=40)
    p.add_argument("--n-city",    type=int, default=80)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out",  default=str(OUT_PATH))
    args = p.parse_args()

    random.seed(args.seed)
    period = latest_month_label()

    # Country pairs — sample without replacement
    seen: set[tuple[str, str]] = set()
    country_insights: list[dict] = []
    tries = 0
    while len(country_insights) < args.n_country and tries < args.n_country * 10:
        tries += 1
        r = random.choice(REPORTERS)
        p_ = random.choice(PARTNERS)
        key = tuple(sorted([r, p_]))
        if key in seen or r == p_:
            continue
        seen.add(key)
        insight = build_country_insight(r, p_, period)
        if insight:
            country_insights.append(insight)

    # City pairs
    city_insights: list[dict] = []
    tries = 0
    city_seen: set[tuple[str, str]] = set()
    airport_pool = [icao for icao, meta in AIRPORTS.items() if meta["country"] not in REPORTERS or True]
    while len(city_insights) < args.n_city and tries < args.n_city * 20:
        tries += 1
        r = random.choice(REPORTERS)
        icao = random.choice(airport_pool)
        key = (r, icao)
        if key in city_seen:
            continue
        city_seen.add(key)
        insight = build_city_insight(r, icao, period)
        if insight:
            city_insights.append(insight)

    all_insights = country_insights + city_insights

    payload = {
        "meta": {
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period":  period,
            "source":  "SYNTHETIC SAMPLE — for UX testing; real pipeline will overwrite",
            "coverage": f"{args.n_country} country pairs + {args.n_city} city pairs, seed={args.seed}",
        },
        "insights": all_insights,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[sample] wrote {len(all_insights)} insights ({len(country_insights)} country + {len(city_insights)} city) → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
