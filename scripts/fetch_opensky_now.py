"""
One-shot fetch of near-current flight data from OpenSky Network.

Fills the 2025/2026 gap that Eurostat can't (Eurostat lags 8+ months).
OpenSky publishes ADS-B flight records with ~1 day lag globally, free.

OpenSky migrated from basic auth to OAuth2 client credentials in 2024.
You need an API client (client_id + client_secret), not just a login:
    1. Sign up at https://opensky-network.org
    2. Log in → Account → API Client → create one → copy id + secret
    3. export OPENSKY_CLIENT_ID=...
       export OPENSKY_CLIENT_SECRET=...
    4. python3 scripts/fetch_opensky_now.py --days 30

Auth budget:
    - anonymous access is blocked (403)
    - standard authenticated user: ~4000 credits/day
    - each airport-day query costs 4 credits
    - default plan: 30 top EU airports × N days × 4 credits
      → for --days 30 that's ~3600 credits (one full day's budget)
    - script rate-limits itself to 0.5s between calls to stay polite
    - bearer token cached to disk; auto-refreshes when it expires

Docs: https://openskynetwork.github.io/opensky-api/rest.html
"""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
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


TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
TOKEN_CACHE = Path(__file__).resolve().parent / ".cache" / "opensky_token.json"


def _ssl_context() -> ssl.SSLContext:
    """
    Explicit certifi CA bundle — fresh Python on Windows sometimes ships without
    a working system trust store, which shows up as 'certificate verify failed'.
    Using certifi's bundle avoids that entire class of failure.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_CTX = _ssl_context()


def _fetch_bearer_token() -> str | None:
    """OAuth2 client-credentials flow. Caches token until 30s before expiry."""
    cid = os.environ.get("OPENSKY_CLIENT_ID")
    sec = os.environ.get("OPENSKY_CLIENT_SECRET")
    if not cid or not sec:
        return None

    # Try cache first
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
            if cached.get("expires_at", 0) > time.time() + 30:
                return cached["access_token"]
        except Exception:
            pass

    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": sec,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "ovrhead-ingest/0.2"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"[opensky] token endpoint {e.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[opensky] token fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    token = payload.get("access_token")
    ttl   = int(payload.get("expires_in", 300))
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps({
        "access_token": token, "expires_at": time.time() + ttl,
    }))
    return token


def _auth_ok() -> bool:
    """Cheap check we have credentials configured (doesn't hit the network)."""
    return bool(os.environ.get("OPENSKY_CLIENT_ID") and os.environ.get("OPENSKY_CLIENT_SECRET"))


def _fetch_json(url: str, retries: int = 3) -> list | None:
    token = _fetch_bearer_token()
    if not token:
        return None
    headers = {
        "User-Agent": "ovrhead-ingest/0.2",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    tries = 0
    while True:
        tries += 1
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:      # no flights that day for that airport
                return []
            if e.code == 401 and tries == 1:
                # Token may have expired mid-run — refresh and retry once
                TOKEN_CACHE.unlink(missing_ok=True)
                token = _fetch_bearer_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    continue
                return None
            if e.code == 403:
                print("[opensky] 403 Forbidden — check that OPENSKY_CLIENT_ID / "
                      "OPENSKY_CLIENT_SECRET are set and belong to a valid API client",
                      file=sys.stderr)
                return None
            if e.code == 429 and tries < retries:
                wait = 20 * tries
                print(f"[opensky] 429 rate-limited, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait); continue
            print(f"[opensky] HTTP {e.code} on {url[:80]}...", file=sys.stderr)
            return None
        except ssl.SSLCertVerificationError as e:
            print(f"[opensky] TLS cert verification failed: {e}", file=sys.stderr)
            print("           Try: pip install --upgrade certifi", file=sys.stderr)
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

    if not _auth_ok():
        print("Need OpenSky OAuth2 API client credentials.", file=sys.stderr)
        print("Log in at https://opensky-network.org → Account → API Client → create one.", file=sys.stderr)
        print("Then set: OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET and re-run.", file=sys.stderr)
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
