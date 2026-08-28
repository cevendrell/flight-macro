"""
ADS-B health check. Prints how much data we have, how fresh, top operators.

    python scripts/adsb/inspect.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("pip install duckdb pyarrow", file=sys.stderr)
    sys.exit(1)

WAREHOUSE = Path(os.environ.get("OVRHEAD_WAREHOUSE", str(Path.home() / "data" / "ovrhead-warehouse")))
SNAPS = WAREHOUSE / "adsb" / "snapshots" / "*.parquet"


def size_of(paths):
    return sum(p.stat().st_size for p in paths)


def fmt_bytes(n):
    for u in ["B","KB","MB","GB"]:
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    files = sorted((WAREHOUSE / "adsb" / "snapshots").glob("*.parquet"))
    print(f"warehouse: {WAREHOUSE}")
    print(f"snapshot files: {len(files)}  ({fmt_bytes(size_of(files))})")
    if not files:
        print("no data yet — start the poller: python scripts/adsb/poller.py")
        return 0
    for f in files[-5:]:
        print(f"  {f.name}  ({fmt_bytes(f.stat().st_size)})")

    con = duckdb.connect()
    con.execute(f"CREATE VIEW snap AS SELECT * FROM read_parquet('{SNAPS}')")

    n_rows, n_hex = con.execute("SELECT COUNT(*), COUNT(DISTINCT hex) FROM snap").fetchone()
    lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM snap").fetchone()
    print(f"\nobservations: {n_rows:,}   unique hexes: {n_hex:,}")
    if lo and hi:
        lo_iso = datetime.fromtimestamp(lo, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        hi_iso = datetime.fromtimestamp(hi, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"range: {lo_iso}  ->  {hi_iso}")
        span_h = (hi - lo) / 3600
        if span_h > 0:
            print(f"snapshot rate: {n_rows / span_h:,.0f} rows/hour")

    print("\n── Top 10 callsigns (by observation count) ──")
    rows = con.execute("""
        SELECT flight, COUNT(*) AS n, COUNT(DISTINCT hex) AS ac
        FROM snap WHERE flight IS NOT NULL
        GROUP BY flight ORDER BY n DESC LIMIT 10
    """).fetchall()
    for f, n, ac in rows:
        print(f"  {f:10}  {n:>6} obs  ({ac} aircraft)")

    print("\n── Top 10 airline prefixes (first 3 letters of callsign) ──")
    rows = con.execute("""
        SELECT UPPER(SUBSTR(flight, 1, 3)) AS prefix,
               COUNT(DISTINCT hex) AS ac,
               COUNT(*) AS n
        FROM snap WHERE flight IS NOT NULL AND LENGTH(flight) >= 3
        GROUP BY prefix ORDER BY n DESC LIMIT 10
    """).fetchall()
    for p, ac, n in rows:
        print(f"  {p}   {ac:>4} aircraft  {n:>7,} obs")

    print("\n── Altitude bands (feet) ──")
    rows = con.execute("""
        SELECT CASE
            WHEN alt_baro IS NULL THEN 'unknown'
            WHEN alt_baro < 10000 THEN 'below FL100'
            WHEN alt_baro < 25000 THEN 'FL100-FL250'
            WHEN alt_baro < 33000 THEN 'FL250-FL330'
            ELSE 'FL330+'
        END AS band,
        COUNT(DISTINCT hex) AS ac
        FROM snap GROUP BY band ORDER BY 2 DESC
    """).fetchall()
    for b, ac in rows:
        print(f"  {b:<15} {ac:>5} aircraft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
