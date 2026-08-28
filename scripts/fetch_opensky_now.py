"""
One-shot fetch of near-current flight data from OpenSky Network.

Fills the 2025/2026 gap that Eurostat can't (Eurostat lags 8+ months).
OpenSky publishes ADS-B flight records with ~1 day lag globally, free.

Requires a free account:
    1. Sign up at https://opensky-network.org (takes 30 seconds)
    2. export OPENSKY_USER=your_username
       export OPENSKY_PASS=your_password
    3. python3 scripts/fetch_opensky_now.py --days 30

What this writes:
    data/insights.json — signals derived from OpenSky flight counts, comparing
    the last N days to the same N days last year at each airport pair.
    Meta will read "OpenSky Network (flight counts)".

Auth budget:
    - anonymous access is now blocked (403)
    - standard user: ~4000 credits/day
    - each airport-day query costs 4 credits
    - default plan: 30 top EU airports × N days × 4 credits
      → for --days 30 that's ~3600 credits (one full day's budget)
    - script rate-limits itself to 0.5s between calls to stay polite

Docs: https://openskynetwork.github.io/opensky-api/rest.html
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from airports import AIRPORTS
from country_coords import COUNTRIES

BASE = "https://opensky-network.org/api"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "insights.json"
CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "opensky_raw"

# Top ~30 European hubs — one call per airport per day.
# Extend freely; the credit cost scales linearly.
HUB_ICAOS = [
    "EDDF","EDDM","EDDL","EDDB",         # DE
    "LFPG","LFPO",                        # FR
    "EGLL","EGKK","EGCC","EGSS",         # GB
    "EHAM",                               # NL
    "EBBR",                               # BE
    "LEMD","LEBL","LEPA","LEMG",         # ES
    "LPPT",                               # PT
    "EKCH","ESSA","ENGM","EFHK",         # Nordics
    "LIRF","LIMC","LIPZ",                 # IT
    "LKPR","LOWW","EPWA",                # CZ/AT/PL
    "LGAV",                               # EL
    "LTFM","LTAI",                        # TR
    "LSZH","LSGG",                        # CH
    "EIDW",                               # IE
]


def _basic_auth_header() -> str | None:
    u = os.environ.get("OPENSKY_USER")
    p = os.environ.get("OPENSKY_PASS")
    if not u or not p:
        return None
    token = base64.b64encode(f"{u}:{p}".encode()).decode()
    return f"Basic {token}"


def _fetch_json(url: str, retries: int = 3) -> list | None:
    auth = _basic_auth_header()
    headers = {"User-Agent": "ovrhead-ingest/0.1", "Accept": "application/json"}
    if auth: headers["Authorization"] = auth

    tries = 0
    while True:
        tries += 1
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:      # no flights that day for that airport
                return []
            if e.code == 403:
                print("[opensky] 403 Forbidden — set OPENSKY_USER / OPENSKY_PASS "
                      "(free account at https://opensky-network.org)", file=sys.stderr)
                return None
            if e.code == 429 and tries < retries:
                wait = 20 * tries
                print(f"[opensky] 429 rate-limited, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait); continue
            print(f"[opensky] HTTP {e.code} on {url[:80]}...", file=sys.stderr)
            return None
        except Exception as e:
            if tries < retries:
                time.sleep(2 * tries); continue
            print(f"[opensky] failed: {e}", file=sys.stderr)
            return None


def _day_bounds(d: date) -> tuple[int, int]:
    start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return start, start + 86400


def fetch_airport_day(icao: str, d: date) -> list[dict]:
    """Returns list of flight dicts (or [])."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"dep_{icao}_{d.isoformat()}.json"
    if cache.exists():
        return json.loads(cache.read_text()) or []

    begin, end = _day_bounds(d)
    url = f"{BASE}/flights/departure?airport={icao}&begin={begin}&end={end}"
    result = _fetch_json(url)
    if result is None:
        return []
    cache.write_text(json.dumps(result))
    return result


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def build_corridors(all_flights: list[dict]) -> dict:
    """
    all_flights: rows like {origin, destination, day}
    Returns: {(o_icao, d_icao): count}
    """
    counts: dict[tuple[str, str], int] = {}
    for f in all_flights:
        o = f.get("origin")
        d = f.get("destination")
        if not o or not d or o == d:
            continue
        counts[(o, d)] = counts.get((o, d), 0) + 1
    return counts


def diff_windows(current: dict, prior: dict, top_n: int, min_current: int = 20, min_prior: int = 10):
    """Compute YoY corridor deltas from two count dicts."""
    keys = set(current) | set(prior)
    rows = []
    for k in keys:
        vc, vp = current.get(k, 0), prior.get(k, 0)
        if vc < min_current or vp < min_prior:
            continue
        delta = (vc - vp) / vp * 100.0
        if abs(delta) < 5:
            continue
        strength = abs(delta) * math.log10(max(vc, 10))
        rows.append((k, vc, vp, delta, strength))
    rows.sort(key=lambda r: r[4], reverse=True)
    return rows[:top_n]


def to_insight(row, period_label: str) -> dict | None:
    (o_icao, d_icao), vc, vp, delta, _ = row
    o = AIRPORTS.get(o_icao); d = AIRPORTS.get(d_icao)
    if not o:                                   # unknown origin airport (should always be in our lookup)
        return None

    o_country = COUNTRIES.get(o["country"])
    if not o_country:
        return None

    up = delta >= 0
    sign_word = "up" if up else "down"

    if d and d.get("city"):
        d_country_iso = d["country"]
        d_country = COUNTRIES.get(d_country_iso, {"name": d_country_iso})
        dest_label = f"{d['city']} ({d['iata']})"
        origin_obj = {"name": o["city"], "code": o["country"], "country": o_country["name"],
                      "lat": o["lat"], "lng": o["lng"], "type": "city"}
        dest_obj   = {"name": dest_label, "code": d_country_iso, "country": d_country["name"],
                      "lat": d["lat"], "lng": d["lng"], "type": "city"}
        slug = f"{o['iata']}-{d['iata']}".lower()
        headline = f"{o['city']} → {dest_label} {sign_word} {abs(delta):.1f}% YoY (flights)"
    else:
        # Destination airport not in our lookup — bail (we can't place it on the map)
        return None

    reading = (f"OpenSky ADS-B flight counts for {o['city']} ({o['iata']}) → {dest_label} "
               f"were {sign_word} {abs(delta):.1f}% year-on-year "
               f"({vc:,} vs {vp:,} flights). This uses flight counts, not passenger "
               f"seats — big narrow-body carriers can move the number without moving demand. "
               f"Macro attribution pending LLM enrichment.")

    return {
        "id": f"{slug}-osky-{period_label.replace(' ', '').lower()}",
        "origin": origin_obj,
        "dest":   dest_obj,
        "period": period_label,
        "deltaPct": round(delta, 1),
        "volumeCurrent": int(vc),
        "volumePrior":   int(vp),
        "theme": "business",     # placeholder; enrichment refines
        "headline": headline,
        "reading": reading,
        "confidence": "medium",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=14, help="Window size in days (default 14)")
    p.add_argument("--end",  help="YYYY-MM-DD end of window (default: yesterday, UTC)")
    p.add_argument("--top",  type=int, default=120, help="Top N signals to write")
    p.add_argument("--airports", help="Comma-separated ICAOs (default: 30 EU hubs)")
    args = p.parse_args()

    if not _basic_auth_header():
        print("Need free OpenSky credentials. Sign up: https://opensky-network.org", file=sys.stderr)
        print("Then: export OPENSKY_USER=... OPENSKY_PASS=... and re-run.", file=sys.stderr)
        return 1

    end   = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else (datetime.now(timezone.utc) - timedelta(days=1)).date()
    start = end - timedelta(days=args.days - 1)
    prior_end   = date(end.year - 1, end.month, end.day)
    prior_start = prior_end - timedelta(days=args.days - 1)

    airports = args.airports.split(",") if args.airports else HUB_ICAOS
    print(f"[cfg] current: {start} → {end}   prior: {prior_start} → {prior_end}")
    print(f"[cfg] {len(airports)} airports × 2 windows × {args.days} days = {len(airports)*2*args.days} calls")

    def gather(days, label):
        rows = []
        total = len(airports) * len(days)
        n = 0
        for a in airports:
            for d in days:
                n += 1
                flights = fetch_airport_day(a, d)
                for f in flights:
                    rows.append({
                        "origin": a,
                        "destination": f.get("estArrivalAirport"),
                        "day": d.isoformat(),
                    })
                if n % 25 == 0:
                    print(f"[{label}] {n}/{total} calls, {len(rows):,} rows so far")
                time.sleep(0.5)
        print(f"[{label}] total {len(rows):,} rows")
        return rows

    current_days = list(daterange(start, end))
    prior_days   = list(daterange(prior_start, prior_end))

    current_rows = gather(current_days, "current")
    prior_rows   = gather(prior_days,   "prior")

    current_counts = build_corridors(current_rows)
    prior_counts   = build_corridors(prior_rows)
    print(f"[build] current unique corridors: {len(current_counts):,}   prior: {len(prior_counts):,}")

    period_label = f"{start.strftime('%b %d')}–{end.strftime('%d, %Y')} vs prior year"
    top = diff_windows(current_counts, prior_counts, top_n=args.top)
    insights = [x for x in (to_insight(r, period_label) for r in top) if x]

    payload = {
        "meta": {
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period":  period_label,
            "source":  "OpenSky Network (ADS-B flight counts)",
            "coverage": f"{len(airports)} EU hubs, {args.days}-day window",
            "note": ("Flight counts (not passenger volumes) from live ADS-B tracking. "
                     "Good for near-current freshness; less accurate on demand than Eurostat passenger data. "
                     "Ideal to blend with Eurostat's deeper history via the DuckDB warehouse."),
        },
        "insights": insights,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[write] {len(insights)} signals → {OUT_PATH.relative_to(OUT_PATH.parents[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
