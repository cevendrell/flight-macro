"""
OpenSky Network → local parquet warehouse.

OpenSky publishes historical "flight tables": one row per detected flight, with
origin/destination airport (ICAO) and timestamps. Free, public, no key required
for small windows. Optional basic-auth (OPENSKY_USER / OPENSKY_PASS) unlocks
higher rate limits — highly recommended for backfills.

Usage:
    # yesterday, only airports in the lookup
    python scripts/ingest_opensky.py

    # a specific day
    python scripts/ingest_opensky.py --day 2026-08-27

    # backfill a range (inclusive)
    python scripts/ingest_opensky.py --from 2026-08-01 --to 2026-08-27

Storage: appends to ~/data/ovrhead-warehouse/raw/opensky/flights_YYYY-MM.parquet.
Idempotent: re-running the same day dedupes on (day, callsign, origin, first_seen).

Docs: https://openskynetwork.github.io/opensky-api/rest.html
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import requests
except ImportError:
    print("Install deps: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

# Local imports — ensure scripts/ is on sys.path when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from airports import AIRPORTS
from warehouse import warehouse_root

OPENSKY_BASE = "https://opensky-network.org/api"
REQUEST_TIMEOUT = 45
POLITE_SLEEP = 0.4         # between successful requests
BACKOFF_ON_429 = 30        # seconds to wait after a 429


def _session() -> requests.Session:
    s = requests.Session()
    user = os.environ.get("OPENSKY_USER")
    pw   = os.environ.get("OPENSKY_PASS")
    if user and pw:
        s.auth = (user, pw)
    s.headers.update({"User-Agent": "ovrhead-ingest/0.1 (github.com/cevendrell/ovrhead)"})
    return s


def _daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def fetch_airport_day(session: requests.Session, icao: str, day: date, direction: str) -> list[dict]:
    """
    direction: 'departure' → flights leaving this airport that day
               'arrival'   → flights landing here that day
    We only need one direction (departures) because every flight departs from
    somewhere in our lookup table — using arrivals too would double-count.
    """
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    end   = start + 24 * 3600
    url   = f"{OPENSKY_BASE}/flights/{direction}"
    params = {"airport": icao, "begin": start, "end": end}

    tries = 0
    while True:
        tries += 1
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if tries >= 3:
                print(f"[opensky] {icao} {day}: giving up after {tries} tries: {e}", file=sys.stderr)
                return []
            time.sleep(2 * tries)
            continue

        if r.status_code == 404:
            return []                          # no flights that day, not an error
        if r.status_code == 429:
            print(f"[opensky] rate-limited on {icao} {day}, sleeping {BACKOFF_ON_429}s")
            time.sleep(BACKOFF_ON_429)
            continue
        if r.status_code >= 500:
            if tries >= 3:
                print(f"[opensky] {icao} {day}: server error {r.status_code}, skipping", file=sys.stderr)
                return []
            time.sleep(5 * tries)
            continue

        r.raise_for_status()
        return r.json() or []


def fetch_day(day: date, airports: list[str], session: requests.Session | None = None) -> list[dict]:
    session = session or _session()
    all_rows: list[dict] = []
    for i, icao in enumerate(airports, 1):
        try:
            flights = fetch_airport_day(session, icao, day, "departure")
        except Exception as e:
            print(f"[opensky] {icao} {day}: {e}", file=sys.stderr)
            continue
        for f in flights:
            all_rows.append({
                "day":         day.isoformat(),
                "callsign":    (f.get("callsign") or "").strip() or None,
                "origin":      icao,
                "destination": f.get("estArrivalAirport"),
                "first_seen":  f.get("firstSeen"),
                "last_seen":   f.get("lastSeen"),
            })
        if i % 20 == 0:
            print(f"[opensky] {day} progress: {i}/{len(airports)} airports")
        time.sleep(POLITE_SLEEP)
    return all_rows


def write_month(rows: list[dict], day: date) -> Path | None:
    """Append rows to the monthly parquet, dedupe, write back."""
    month_file = warehouse_root() / "raw" / "opensky" / f"flights_{day.strftime('%Y-%m')}.parquet"
    if not rows:
        return month_file if month_file.exists() else None

    new_tbl = pa.Table.from_pylist(rows)
    if month_file.exists():
        old_tbl = pq.read_table(month_file)
        combined = pa.concat_tables([old_tbl, new_tbl], promote_options="default")
        # dedupe via pandas (small, per-month scope)
        df = combined.to_pandas().drop_duplicates(
            subset=["day", "callsign", "origin", "first_seen"], keep="last"
        )
        out_tbl = pa.Table.from_pandas(df, preserve_index=False)
    else:
        out_tbl = new_tbl

    pq.write_table(out_tbl, month_file, compression="zstd")
    print(f"[opensky] {day}: wrote {len(rows)} new rows → {month_file.name} "
          f"(total in file: {out_tbl.num_rows:,})")
    return month_file


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day",  help="YYYY-MM-DD (single day)")
    p.add_argument("--from", dest="from_", help="YYYY-MM-DD range start (inclusive)")
    p.add_argument("--to",   help="YYYY-MM-DD range end (inclusive)")
    p.add_argument("--airports", help="Comma-separated ICAO codes (default: all in airports.py)")
    args = p.parse_args()

    airports = args.airports.split(",") if args.airports else sorted(AIRPORTS.keys())

    if args.day:
        days = [datetime.strptime(args.day, "%Y-%m-%d").date()]
    elif args.from_ and args.to:
        a = datetime.strptime(args.from_, "%Y-%m-%d").date()
        b = datetime.strptime(args.to,    "%Y-%m-%d").date()
        days = list(_daterange(a, b))
    else:
        days = [(datetime.now(timezone.utc) - timedelta(days=1)).date()]

    print(f"[opensky] ingesting {len(days)} day(s), {len(airports)} airport(s)")
    if not os.environ.get("OPENSKY_USER"):
        print("[opensky] tip: set OPENSKY_USER/OPENSKY_PASS for higher rate limits")

    session = _session()
    for d in days:
        rows = fetch_day(d, airports, session)
        write_month(rows, d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
