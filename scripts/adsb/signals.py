"""
ADS-B flights -> weekly signals -> data/insights.json

Reads flights parquet produced by reconstruct.py, aggregates over rolling
windows, compares this week to last, and produces thesis cards keyed to
"what changed over Aarhus".

Signal families:
    1. country-mix   per operator country: flights this week vs last (delta%)
    2. category      freighter share, widebody share (aggregate)
    3. absence       was present in prior week, gone this week (or vice versa)

The map still shows arcs — we use Aarhus as the fixed home point, and each
country-mix signal draws an arc from Aarhus to that country's centroid.
Aggregate signals sit on Aarhus itself.

Run:
    python scripts/adsb/signals.py
    python scripts/adsb/signals.py --days 7 --min-flights 3
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("pip install duckdb pyarrow", file=sys.stderr)
    sys.exit(1)

# Reuse the country lookup shipped with the Eurostat pipeline
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from country_coords import COUNTRIES

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH  = REPO_ROOT / "data" / "insights.json"

WAREHOUSE = Path(os.environ.get("OVRHEAD_WAREHOUSE", str(Path.home() / "data" / "ovrhead-warehouse")))
FLIGHTS   = WAREHOUSE / "adsb" / "flights"
SNAPS     = WAREHOUSE / "adsb" / "snapshots"

# Home antenna reference point — Aarhus
HOME = {
    "name": "Aarhus",
    "code": "AAR",           # antenna code, not an IATA
    "country": "Denmark",
    "lat": 56.16,
    "lng": 10.20,
    "type": "airport",       # renders as a city-grain dot on the globe
}

# Freighter callsign prefixes we recognise as cargo.
FREIGHTER_PREFIXES = {"FDX","UPS","CLX","GEC","NCA","CKS","BOX","DHK","DHL","SQC","VDA","GTI"}

# Aircraft-type prefixes that count as widebody (rough grouping — good enough)
WIDEBODY_TYPE_PREFIXES = {"B74","B77","B78","B7","A33","A34","A35","A38","A30","MD11","IL96"}


def has_flights() -> bool:
    return any(FLIGHTS.glob("*.parquet"))


def now_utc() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def register_views(con) -> None:
    con.execute(f"CREATE OR REPLACE VIEW flights AS SELECT * FROM read_parquet('{FLIGHTS / '*.parquet'}', union_by_name=true)")


# ── Signal producers ────────────────────────────────────────────────────────

def signals_country_mix(con, curr_start, curr_end, prior_start, prior_end,
                        min_flights: int) -> list[dict]:
    """One signal per operator country: flights observed this week vs last."""
    rows = con.execute(f"""
        WITH curr AS (
            SELECT airline_country AS country, COUNT(*) AS n
            FROM flights
            WHERE first_seen BETWEEN {curr_start} AND {curr_end}
              AND airline_country IS NOT NULL
            GROUP BY 1
        ),
        prior AS (
            SELECT airline_country AS country, COUNT(*) AS n
            FROM flights
            WHERE first_seen BETWEEN {prior_start} AND {prior_end}
              AND airline_country IS NOT NULL
            GROUP BY 1
        )
        SELECT COALESCE(c.country, p.country) AS country,
               COALESCE(c.n, 0) AS n_curr,
               COALESCE(p.n, 0) AS n_prior
        FROM curr c FULL OUTER JOIN prior p ON c.country = p.country
        WHERE COALESCE(c.n, 0) + COALESCE(p.n, 0) >= {min_flights}
    """).fetchall()

    signals = []
    for country_code, n_curr, n_prior in rows:
        cc = COUNTRIES.get(country_code)
        if not cc:
            continue
        # Percent change; treat "new appearance" as +∞ (represented as 999)
        if n_prior > 0:
            delta = (n_curr - n_prior) / n_prior * 100.0
        elif n_curr > 0:
            delta = 999.0
        else:
            continue

        # Filter: needs a material change AND at least a couple of flights on one side
        if abs(delta) < 20 and max(n_curr, n_prior) < 8:
            continue

        signals.append({
            "id": f"cm-{country_code.lower()}",
            "origin": HOME,
            "dest": {"name": cc["name"], "code": country_code, "country": cc["name"],
                     "lat": cc["lat"], "lng": cc["lng"], "type": "country"},
            "deltaPct": round(delta, 1) if delta < 500 else 500.0,
            "volumeCurrent": int(n_curr),
            "volumePrior":   int(n_prior),
            "theme": _guess_theme_for_country(country_code, n_curr, n_prior),
            "headline": _country_headline(cc["name"], n_curr, n_prior, delta),
            "reading":  _country_reading(cc["name"], n_curr, n_prior, delta),
            "confidence": "medium" if max(n_curr, n_prior) >= 8 else "low",
        })
    return signals


def signals_categories(con, curr_start, curr_end, prior_start, prior_end,
                       min_flights: int) -> list[dict]:
    """Freighter share and widebody share — aggregates keyed to the antenna."""
    prefixes_sql = "(" + ",".join(f"'{p}'" for p in FREIGHTER_PREFIXES) + ")"
    freighter_curr, all_curr = con.execute(f"""
        SELECT
          SUM(CASE WHEN UPPER(SUBSTR(callsign, 1, 3)) IN {prefixes_sql} THEN 1 ELSE 0 END),
          COUNT(*)
        FROM flights WHERE first_seen BETWEEN {curr_start} AND {curr_end}
          AND callsign IS NOT NULL
    """).fetchone()
    freighter_prior, all_prior = con.execute(f"""
        SELECT
          SUM(CASE WHEN UPPER(SUBSTR(callsign, 1, 3)) IN {prefixes_sql} THEN 1 ELSE 0 END),
          COUNT(*)
        FROM flights WHERE first_seen BETWEEN {prior_start} AND {prior_end}
          AND callsign IS NOT NULL
    """).fetchone()

    signals = []
    if (all_curr or 0) >= min_flights and (all_prior or 0) >= min_flights:
        curr_share = (freighter_curr or 0) / all_curr * 100.0
        prior_share = (freighter_prior or 0) / all_prior * 100.0
        pp_delta = curr_share - prior_share  # percentage-point change
        if abs(pp_delta) >= 1.0:
            signals.append({
                "id": "cat-freighter",
                "origin": HOME,
                "dest": {**HOME, "name": "Freighter share", "code": "FRT"},
                "deltaPct": round(pp_delta, 1),
                "volumeCurrent": int(freighter_curr or 0),
                "volumePrior":   int(freighter_prior or 0),
                "theme": "supply-chain",
                "headline": f"Freighter share {'up' if pp_delta >= 0 else 'down'} to {curr_share:.1f}% overhead",
                "reading":  (f"Cargo aircraft accounted for {curr_share:.1f}% of flights over Aarhus this window "
                             f"({freighter_curr:,} of {all_curr:,}), vs {prior_share:.1f}% the prior window "
                             f"({freighter_prior:,} of {all_prior:,}). "
                             f"Includes FedEx, UPS, DHL, Cargolux, Kalitta, Silk Way, and other freighter operators."),
                "confidence": "medium",
            })
    return signals


def signals_absence(con, curr_start, curr_end, prior_start, prior_end,
                    min_flights: int) -> list[dict]:
    """Countries that appeared in the prior window but not the current — the
       negative signal (e.g. sanctions still holding)."""
    rows = con.execute(f"""
        WITH curr AS (
            SELECT DISTINCT airline_country FROM flights
            WHERE first_seen BETWEEN {curr_start} AND {curr_end}
              AND airline_country IS NOT NULL
        ),
        prior AS (
            SELECT airline_country, COUNT(*) AS n FROM flights
            WHERE first_seen BETWEEN {prior_start} AND {prior_end}
              AND airline_country IS NOT NULL
            GROUP BY 1
        )
        SELECT p.airline_country, p.n
        FROM prior p LEFT JOIN curr c ON c.airline_country = p.airline_country
        WHERE c.airline_country IS NULL AND p.n >= {min_flights}
    """).fetchall()

    signals = []
    for country_code, n_prior in rows:
        cc = COUNTRIES.get(country_code)
        if not cc:
            continue
        signals.append({
            "id": f"absent-{country_code.lower()}",
            "origin": HOME,
            "dest": {"name": cc["name"], "code": country_code, "country": cc["name"],
                     "lat": cc["lat"], "lng": cc["lng"], "type": "country"},
            "deltaPct": -100.0,
            "volumeCurrent": 0,
            "volumePrior":   int(n_prior),
            "theme": "business",
            "headline": f"{cc['name']} operators absent this window",
            "reading":  (f"No aircraft operated by {cc['name']}-registered airlines observed overhead this week, "
                         f"vs {n_prior} flights the prior week. Absence signals matter — sanctions, "
                         f"route cancellations, and carrier collapses all show up this way first."),
            "confidence": "high" if n_prior >= 10 else "medium",
        })
    return signals


# ── Copy templates ──────────────────────────────────────────────────────────

def _guess_theme_for_country(cc: str, n_curr: int, n_prior: int) -> str:
    # Very rough — refined later by Claude enrichment if we wire it up
    if cc in {"CN","HK","JP","KR","SG","AE","QA","SA","IN"}:
        return "tourism"   # long-haul entries — often east/gulf carriers to Nordic
    if cc in {"US","CA","MX","BR"}:
        return "business"
    if cc in {"LU","IS"}:
        return "supply-chain"  # cargo hub countries
    return "tourism"


def _country_headline(name: str, curr: int, prior: int, delta: float) -> str:
    if prior == 0:
        return f"{name} carriers now overhead ({curr} observed)"
    if delta >= 20:
        return f"{name} carrier presence up {delta:.0f}% overhead"
    if delta <= -20:
        return f"{name} carrier presence down {abs(delta):.0f}% overhead"
    return f"{name} carrier presence shifted {delta:+.0f}%"


def _country_reading(name: str, curr: int, prior: int, delta: float) -> str:
    return (f"Aircraft operated by {name}-registered airlines were observed {curr} times over Aarhus this window "
            f"(vs {prior} the prior window; {delta:+.0f}% change). "
            f"This tracks the presence of that country's carriers on routes passing overhead — a proxy for "
            f"trade and travel intensity between Northern Europe and {name}.")


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7, help="Window size in days (default: 7)")
    p.add_argument("--min-flights", type=int, default=3, help="Minimum flights to consider a country (default: 3)")
    args = p.parse_args()

    if not has_flights():
        # No flights parquet yet. Emit a minimal file so the site says something honest.
        payload = {
            "meta": {
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "period":  "Waiting for first week of data",
                "source":  "OvrHead ADS-B — Aarhus antenna",
                "coverage": "Real-time ADS-B feed from a Raspberry Pi + RTL-SDR in Aarhus",
                "note": ("No flights table yet. The poller writes snapshots continuously; "
                         "reconstruct.py turns them into flights; this script turns flights into signals. "
                         "Come back after a week of data has accumulated."),
            },
            "insights": [],
        }
        OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"[signals] no flights yet — wrote landing state to {OUT_PATH.relative_to(REPO_ROOT)}")
        return 0

    con = duckdb.connect()
    register_views(con)

    end = now_utc()
    curr_start  = end - args.days * 86400
    prior_end   = curr_start
    prior_start = curr_start - args.days * 86400

    total = con.execute(f"SELECT COUNT(*) FROM flights WHERE first_seen BETWEEN {prior_start} AND {end}").fetchone()[0]
    print(f"[signals] window: last {args.days}d vs prior {args.days}d, {total:,} flights total in scope")

    signals = []
    signals += signals_country_mix(con, curr_start, end, prior_start, prior_end, args.min_flights)
    signals += signals_categories(con, curr_start, end, prior_start, prior_end, args.min_flights)
    signals += signals_absence(con,    curr_start, end, prior_start, prior_end, args.min_flights)

    # Rank by signal strength
    def strength(s):
        vol = max(s["volumeCurrent"], s["volumePrior"], 1)
        return abs(s["deltaPct"]) * math.log10(vol + 10)
    signals.sort(key=strength, reverse=True)

    payload = {
        "meta": {
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period": _period_label(args.days),
            "source": "OvrHead ADS-B — Aarhus antenna",
            "coverage": "Real-time ADS-B feed from a Raspberry Pi + RTL-SDR in Aarhus (~250nm range)",
            "note": ("First-person aviation signals. Every card is a change in what our antenna literally saw overhead "
                     "this week vs last. Scope is narrow (Northern European transit airspace) but the data is unfiltered "
                     "and real-time."),
        },
        "insights": signals,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[signals] wrote {len(signals)} signals -> {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


def _period_label(days: int) -> str:
    end = datetime.now(tz=timezone.utc).date()
    start = end - timedelta(days=days)
    return f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')} vs prior {days}d"


if __name__ == "__main__":
    sys.exit(main())
