"""
Snapshots -> flights.

An aircraft observed across a sequence of snapshots is one "session" in view.
Split sessions when we lose the aircraft for more than SESSION_GAP_SEC (default
30 min) — that gap almost always means it left the antenna's line-of-sight or
landed and later re-took-off.

Per session we record: first_seen, last_seen, first_pos, last_pos, first_alt,
last_alt, min_alt, max_alt, callsign (mode over the session), aircraft
enrichment (type, operator), and inferred nearest airports at first/last
positions.

Writes to:
    <warehouse>/adsb/flights/flights_YYYY-MM.parquet

Run:
    python scripts/adsb/reconstruct.py            # process everything
    python scripts/adsb/reconstruct.py --day 2026-08-29
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import duckdb
    import pyarrow.parquet as pq
except ImportError:
    print("pip install duckdb pyarrow", file=sys.stderr)
    sys.exit(1)

WAREHOUSE = Path(os.environ.get("OVRHEAD_WAREHOUSE", str(Path.home() / "data" / "ovrhead-warehouse")))
SNAP_GLOB = WAREHOUSE / "adsb" / "snapshots" / "*.parquet"
OUT_DIR   = WAREHOUSE / "adsb" / "flights"
ENR       = WAREHOUSE / "adsb" / "enrichment"

SESSION_GAP_SEC = 30 * 60   # any gap > 30 min = new session


def nearest_airport_sql(alias: str, lat_col: str, lon_col: str) -> str:
    """
    Returns a correlated subquery that finds the nearest airport within
    ~1° of the given lat/lon. Cheap because we've filtered airports to
    large/medium already (~10k rows).
    """
    return f"""(
      SELECT ap.ident FROM airports ap
      WHERE ap.latitude_deg BETWEEN {lat_col} - 1.0 AND {lat_col} + 1.0
        AND ap.longitude_deg BETWEEN {lon_col} - 1.5 AND {lon_col} + 1.5
      ORDER BY
        (ap.latitude_deg  - {lat_col}) * (ap.latitude_deg  - {lat_col}) +
        (ap.longitude_deg - {lon_col}) * (ap.longitude_deg - {lon_col})
      LIMIT 1
    )"""


def build_flights(con, day_filter: str | None) -> int:
    """Return count of flights written."""
    # Register views
    if not any((WAREHOUSE / "adsb" / "snapshots").glob("*.parquet")):
        print("[reconstruct] no snapshots yet — nothing to do.")
        return 0
    con.execute(f"""
        CREATE OR REPLACE VIEW snap AS
        SELECT * FROM read_parquet('{SNAP_GLOB}', union_by_name=true)
    """)

    have_airports = (ENR / "airports.parquet").exists()
    have_aircraft = (ENR / "aircraft_db.parquet").exists()
    have_airlines = (ENR / "airlines.parquet").exists()
    if have_airports:
        con.execute(f"CREATE OR REPLACE VIEW airports AS SELECT * FROM read_parquet('{ENR / 'airports.parquet'}')")
    if have_aircraft:
        con.execute(f"CREATE OR REPLACE VIEW aircraft_db AS SELECT * FROM read_parquet('{ENR / 'aircraft_db.parquet'}')")
    if have_airlines:
        con.execute(f"CREATE OR REPLACE VIEW airlines AS SELECT * FROM read_parquet('{ENR / 'airlines.parquet'}')")

    day_where = ""
    if day_filter:
        y, m, d = day_filter.split("-")
        start = int(datetime(int(y), int(m), int(d), tzinfo=timezone.utc).timestamp())
        end   = start + 86400
        day_where = f"WHERE ts BETWEEN {start} AND {end}"

    # 1) sessionize per hex using a big gap threshold via window function
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW sessioned AS
        WITH ordered AS (
            SELECT *,
                   ts - LAG(ts) OVER (PARTITION BY hex ORDER BY ts) AS gap
            FROM snap
            {day_where}
        ),
        breaks AS (
            SELECT *,
                   SUM(CASE WHEN gap IS NULL OR gap > {SESSION_GAP_SEC} THEN 1 ELSE 0 END)
                       OVER (PARTITION BY hex ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                       AS session_idx
            FROM ordered
        )
        SELECT * FROM breaks
    """)

    # 2) aggregate each (hex, session_idx) into a flight row
    airport_first = nearest_airport_sql("f", "f.first_lat", "f.first_lon") if have_airports else "NULL"
    airport_last  = nearest_airport_sql("f", "f.last_lat",  "f.last_lon")  if have_airports else "NULL"
    ac_join       = "LEFT JOIN aircraft_db ac ON ac.hex = f.hex" if have_aircraft else ""
    ac_select     = ", ac.reg, ac.type AS ac_type, ac.desc AS ac_desc, ac.ownop AS ac_ownop" if have_aircraft else ""
    airline_join  = "LEFT JOIN airlines al ON al.prefix = f.airline_prefix" if have_airlines else ""
    airline_select= ", al.name AS airline, al.country AS airline_country" if have_airlines else ""

    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW flights_agg AS
        SELECT
            hex,
            session_idx,
            MIN(ts) AS first_seen,
            MAX(ts) AS last_seen,
            MAX(ts) - MIN(ts) AS duration_sec,
            COUNT(*) AS n_obs,
            MODE(flight) AS callsign,
            UPPER(SUBSTR(MODE(flight), 1, 3)) AS airline_prefix,
            (ARRAY_AGG(lat ORDER BY ts) FILTER (WHERE lat IS NOT NULL))[1] AS first_lat,
            (ARRAY_AGG(lon ORDER BY ts) FILTER (WHERE lon IS NOT NULL))[1] AS first_lon,
            (ARRAY_AGG(lat ORDER BY ts DESC) FILTER (WHERE lat IS NOT NULL))[1] AS last_lat,
            (ARRAY_AGG(lon ORDER BY ts DESC) FILTER (WHERE lon IS NOT NULL))[1] AS last_lon,
            MIN(alt_baro) AS min_alt,
            MAX(alt_baro) AS max_alt,
            MAX(gs) AS max_gs
        FROM sessioned
        WHERE hex IS NOT NULL
        GROUP BY hex, session_idx
        HAVING n_obs >= 2
    """)

    # 3) attach airports + aircraft + airline
    q = f"""
        SELECT
            f.hex, f.callsign,
            f.first_seen, f.last_seen, f.duration_sec, f.n_obs,
            f.first_lat, f.first_lon, f.last_lat, f.last_lon,
            f.min_alt, f.max_alt, f.max_gs,
            {airport_first} AS near_first_airport,
            {airport_last}  AS near_last_airport,
            f.airline_prefix
            {ac_select}
            {airline_select}
        FROM flights_agg f
        {ac_join}
        {airline_join}
    """
    con.execute(f"CREATE OR REPLACE TEMP VIEW flights AS {q}")
    n = con.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
    if n == 0:
        print("[reconstruct] 0 flights produced (need more snapshot data).")
        return 0

    # 4) write partitioned by month of first_seen
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    months = [r[0] for r in con.execute("""
        SELECT DISTINCT strftime(TO_TIMESTAMP(first_seen), '%Y-%m') AS ym FROM flights ORDER BY 1
    """).fetchall()]
    for ym in months:
        out = OUT_DIR / f"flights_{ym}.parquet"
        con.execute(f"""
            COPY (SELECT * FROM flights
                  WHERE strftime(TO_TIMESTAMP(first_seen), '%Y-%m') = '{ym}')
            TO '{out}' (FORMAT 'parquet', COMPRESSION 'zstd')
        """)
        count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
        print(f"[reconstruct] {ym}: {count:,} flights -> {out.name}")
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", help="YYYY-MM-DD (default: all snapshots)")
    args = p.parse_args()

    con = duckdb.connect()
    n = build_flights(con, args.day)
    print(f"\n[reconstruct] total {n:,} flights across {sum(1 for _ in OUT_DIR.glob('*.parquet'))} monthly files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
