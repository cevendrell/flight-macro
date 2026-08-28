"""
One-shot: fetch real Eurostat data now and produce a real data/insights.json.

Uses only Python stdlib (urllib, json) so it runs in any environment. This is
the pragmatic "get real numbers on the site today" path — no DuckDB, no Claude,
no cron. Later the full pipeline (ingest_eurostat + run_pipeline) will supersede
this, but the format it writes is identical.

Choices:
  - Reporters: top ~15 EU by traffic (edit REPORTERS below to expand)
  - Period: last complete month of 2024 vs same month 2023 (Eurostat aviation
    monthly data currently lags ~8 months, so YoY comparison over 2023→2024 is
    what's available today; switch when they publish 2025)
  - Enrichment: templated readings only; every card is marked as such so
    it's clear the LLM pass hasn't run yet.

Run: python3 scripts/fetch_real_now.py
       python3 scripts/fetch_real_now.py --month 2024-10 --top 40
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from airports import AIRPORTS
from country_coords import COUNTRIES

BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

# Top ~20 EU reporters by international traffic — extend freely.
REPORTERS = [
    "DE", "FR", "ES", "IT", "NL", "BE", "PT", "IE", "AT", "SE",
    "DK", "NO", "FI", "PL", "CZ", "HU", "EL", "RO", "CH", "HR",
]

OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "insights.json"


def parse_pair(code: str):
    parts = code.split("_")
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    return None, None, None, None


def decode_jsonstat(payload: dict):
    """Yield (dimensions_dict, value)."""
    dims = payload["id"]
    sizes = payload["size"]
    dim_meta = payload["dimension"]
    codes_by_dim = []
    for d in dims:
        idx = dim_meta[d]["category"]["index"]
        if isinstance(idx, dict):
            arr = sorted(idx.items(), key=lambda kv: kv[1])
            codes_by_dim.append([k for k, _ in arr])
        else:
            codes_by_dim.append(list(idx))
    values = payload["value"]
    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    if isinstance(values, dict):
        it = ((int(k), v) for k, v in values.items())
    else:
        it = enumerate(values)
    for i, v in it:
        if v is None:
            continue
        coords = {}
        rem = i
        for j, d in enumerate(dims):
            pos = rem // strides[j]
            rem = rem % strides[j]
            coords[d] = codes_by_dim[j][pos]
        yield coords, v


CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "eurostat_raw"


def fetch_reporter(reporter: str, months: list[str]) -> list[tuple[str, str, str, str, str, int]]:
    """Return list of (o_country, o_airport, p_country, p_airport, month, passengers)."""
    dataset = f"avia_par_{reporter.lower()}"
    key = f"{dataset}__{min(months)}__{max(months)}.json"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / key

    if cache.exists():
        payload = json.loads(cache.read_text())
        print(f"[eurostat] {reporter}: (cached)")
    else:
        # PAS_CRD_DEP = passengers on departing flights only (directed).
        # Combining reporter_A departures + reporter_B departures gives the true
        # bilateral total without double-counting.
        params = [
            ("format", "JSON"), ("freq", "M"), ("unit", "PAS"), ("tra_meas", "PAS_CRD_DEP"),
            ("sinceTimePeriod", min(months)), ("untilTimePeriod", max(months)),
        ]
        url = f"{BASE}/{dataset}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                payload = json.loads(r.read())
            cache.write_text(json.dumps(payload))
        except Exception as e:
            print(f"[eurostat] {reporter}: fetch failed — {e}", file=sys.stderr)
            return []

    out = []
    for coords, value in decode_jsonstat(payload):
        pair = coords.get("airp_pr", "")
        oc, oa, pc, pa = parse_pair(pair)
        if not pc or not oc:
            continue
        month = coords.get("time", "")
        if month not in months:
            continue
        out.append((oc, oa, pc, pa, month, int(value)))
    print(f"[eurostat] {reporter}: {len(out):,} observations")
    return out


def build_corridors(rows: list, current_month: str, prior_month: str):
    """Return two lists: country-pair corridors and city-pair corridors."""
    # Aggregate up
    country_grid: dict[tuple[str, str, str], int] = {}    # (reporter, partner, month) -> pax
    city_grid: dict[tuple[str, str, str, str], int] = {}  # (reporter, partner, dest_icao, month) -> pax

    for oc, oa, pc, pa, month, pax in rows:
        if oc == pc:
            continue  # skip domestic flights (same country both sides)
        # country grain — sum all airports
        k = (oc, pc, month)
        country_grid[k] = country_grid.get(k, 0) + pax
        # city grain — only rows where dest airport is a real ICAO in our lookup
        if pa and pa in AIRPORTS:
            kc = (oc, pc, pa, month)
            city_grid[kc] = city_grid.get(kc, 0) + pax

    period_label = f"{_fmt_month(current_month)} vs {_fmt_month(prior_month)}"

    # Country corridors — undirected pair (dedupe by sorted key), summed both directions.
    seen: set[tuple[str, str]] = set()
    country_corridors = []
    for (oc, pc, m), pax in country_grid.items():
        pair_key = tuple(sorted([oc, pc]))
        if pair_key in seen:
            continue
        # combine both directions
        vc = country_grid.get((pair_key[0], pair_key[1], current_month), 0) + \
             country_grid.get((pair_key[1], pair_key[0], current_month), 0)
        vp = country_grid.get((pair_key[0], pair_key[1], prior_month), 0) + \
             country_grid.get((pair_key[1], pair_key[0], prior_month), 0)
        if vc == 0 or vp == 0:
            continue
        seen.add(pair_key)
        country_corridors.append({
            "o_code": pair_key[0], "d_code": pair_key[1],
            "vc": vc, "vp": vp, "period": period_label,
            "grain": "country",
        })

    # City corridors — directed (reporter → partner airport).
    seen_city: set[tuple[str, str, str]] = set()
    city_corridors = []
    for (oc, pc, pa, m), pax in city_grid.items():
        key = (oc, pc, pa)
        if key in seen_city:
            continue
        vc = city_grid.get((oc, pc, pa, current_month), 0)
        vp = city_grid.get((oc, pc, pa, prior_month), 0)
        if vc == 0 or vp == 0:
            continue
        seen_city.add(key)
        city_corridors.append({
            "o_code": oc, "d_code": pc, "dest_icao": pa,
            "vc": vc, "vp": vp, "period": period_label,
            "grain": "city",
        })

    return country_corridors, city_corridors


def _fmt_month(yyyymm: str) -> str:
    return datetime.strptime(yyyymm, "%Y-%m").strftime("%b %Y")


def rank(corridors: list, top_n: int, volume_floor: int, delta_floor: float, delta_cap: float = 60.0):
    """
    Rank corridors by signal strength. Filters:
      - Both current and prior periods must have volume >= volume_floor
        (kills base-effect explosions from thin prior-period coverage)
      - |delta%| must be between delta_floor and delta_cap
        (anything > delta_cap almost always indicates a data quality issue,
         not a real macro signal)
    """
    out = []
    for c in corridors:
        if c["vc"] < volume_floor or c["vp"] < volume_floor:
            continue
        delta = (c["vc"] - c["vp"]) / c["vp"] * 100.0
        if abs(delta) < delta_floor or abs(delta) > delta_cap:
            continue
        strength = abs(delta) * math.log10(max(c["vc"], 10))
        out.append({**c, "delta": delta, "strength": strength})
    out.sort(key=lambda c: c["strength"], reverse=True)
    return out[:top_n]


def to_insight(c: dict) -> dict:
    grain = c["grain"]
    oc = COUNTRIES.get(c["o_code"]); dc = COUNTRIES.get(c["d_code"])
    if not oc or not dc:
        return None
    up = c["delta"] >= 0
    sign_word = "up" if up else "down"

    if grain == "country":
        origin = {"name": oc["name"], "code": c["o_code"], "country": oc["name"],
                  "lat": oc["lat"], "lng": oc["lng"], "type": "country"}
        dest   = {"name": dc["name"], "code": c["d_code"], "country": dc["name"],
                  "lat": dc["lat"], "lng": dc["lng"], "type": "country"}
        headline = f"{oc['name']} ↔ {dc['name']} {sign_word} {abs(c['delta']):.1f}% YoY"
        reading = (f"Eurostat departure data shows {oc['name']} ↔ {dc['name']} "
                   f"passenger volume {sign_word} {abs(c['delta']):.1f}% year-on-year "
                   f"({c['vc']:,} vs {c['vp']:,}). Context: this window (2024 vs 2023) "
                   f"still reflects the post-pandemic recovery, so many corridors show elevated growth. "
                   f"Macro attribution pending — the LLM enrichment pass is next in the pipeline.")
        slug_dest = c["d_code"].lower()
    else:  # city
        ap = AIRPORTS[c["dest_icao"]]
        origin = {"name": oc["name"], "code": c["o_code"], "country": oc["name"],
                  "lat": oc["lat"], "lng": oc["lng"], "type": "country"}
        dest_name = f"{ap['city']} ({ap['iata']})"
        dest = {"name": dest_name, "code": c["d_code"], "country": dc["name"],
                "lat": ap["lat"], "lng": ap["lng"], "type": "city"}
        headline = f"{oc['name']} → {dest_name} {sign_word} {abs(c['delta']):.1f}% YoY"
        reading = (f"Eurostat departure data shows {oc['name']} → {dest_name} passengers "
                   f"{sign_word} {abs(c['delta']):.1f}% year-on-year "
                   f"({c['vc']:,} vs {c['vp']:,}). Context: this window (2024 vs 2023) "
                   f"still reflects the post-pandemic recovery. Macro attribution pending "
                   f"— the LLM enrichment pass is next in the pipeline.")
        slug_dest = f"{c['d_code']}-{ap['iata']}".lower()

    # Theme guess — pure heuristic until LLM enriches. Bias toward tourism for
    # summer months, business for major EU capitals.
    period_low = c["period"].lower()
    if any(m in period_low for m in ["jun", "jul", "aug"]):
        theme = "tourism"
    elif dc["name"] in ("Germany", "France", "United Kingdom", "Netherlands", "Switzerland"):
        theme = "business"
    else:
        theme = "tourism"

    return {
        "id": f"{c['o_code'].lower()}-{slug_dest}-{grain}-{c['period'].replace(' ', '').lower()}",
        "origin": origin,
        "dest": dest,
        "period": c["period"],
        "deltaPct": round(c["delta"], 1),
        "volumeCurrent": int(c["vc"]),
        "volumePrior":   int(c["vp"]),
        "theme": theme,
        "headline": headline,
        "reading": reading,
        "confidence": "medium",   # unenriched; medium is a fair default
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--month", default="2024-10", help="YYYY-MM (default 2024-10)")
    p.add_argument("--top", type=int, default=40, help="Country-pair top N")
    p.add_argument("--top-city", type=int, default=100, help="City-pair top N")
    p.add_argument("--reporters", help="Comma-separated ISO2 (default: top 20)")
    args = p.parse_args()

    reporters = [r.strip().upper() for r in args.reporters.split(",")] if args.reporters else REPORTERS

    current = args.month
    year, month = current.split("-")
    prior = f"{int(year)-1}-{month}"
    print(f"[cfg] current={current} prior={prior} reporters={len(reporters)}")

    all_rows = []
    started = time.time()
    for i, r in enumerate(reporters, 1):
        print(f"[{i}/{len(reporters)}] fetching {r}…")
        rows = fetch_reporter(r, [current, prior])
        all_rows.extend(rows)
        time.sleep(0.4)   # polite
    print(f"[fetch] {len(all_rows):,} raw observations in {time.time()-started:.0f}s")

    country_corridors, city_corridors = build_corridors(all_rows, current, prior)
    print(f"[build] {len(country_corridors)} country pairs / {len(city_corridors)} city pairs")

    # Country grain: need real bulk on both sides; cap wild swings.
    ranked_country = rank(country_corridors, args.top,      volume_floor=15_000, delta_floor=3.0, delta_cap=50.0)
    # City grain: lower floor (single airport, single reporter), same discipline.
    ranked_city    = rank(city_corridors,    args.top_city, volume_floor=4_000,  delta_floor=4.0, delta_cap=60.0)
    print(f"[rank] top country={len(ranked_country)}  top city={len(ranked_city)}")

    insights = []
    for c in ranked_country + ranked_city:
        ins = to_insight(c)
        if ins:
            insights.append(ins)

    payload = {
        "meta": {
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period":  f"{_fmt_month(current)} vs {_fmt_month(prior)}",
            "source":  "Eurostat avia_par_* (real passenger data, unenriched)",
            "coverage": f"{len(reporters)} EU reporters, PAS_CRD_DEP measure",
            "note": ("Real Eurostat departure data. Deltas trend positive because 2024 vs 2023 "
                     "still captures the post-pandemic recovery in EU aviation. Macro readings "
                     "are placeholders until the Claude enrichment pass runs on the laptop."),
        },
        "insights": insights,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[write] {len(insights)} real signals → {OUT_PATH.relative_to(OUT_PATH.parents[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
