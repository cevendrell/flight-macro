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
except ImportError:
    print("pip install duckdb", file=sys.stderr)
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
    have_routes = (ENR / "routes.parquet").exists() and have_airports
    if have_routes:
        con.execute(f"CREATE OR REPLACE VIEW routes AS SELECT * FROM read_parquet('{ENR / 'routes.parquet'}')")

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
    ac_select     = ", ac.reg, ac.type AS ac_type, ac.desc AS ac_desc" if have_aircraft else ""
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

    # 3a) direction of travel, and the scheduled route checked against it.
    #
    # The heading is the great-circle bearing from where we first heard the
    # aircraft to where we last did. The route comes from the callsign table
    # (routes.py). A route is only believed when the aircraft was observed
    # heading toward its destination: the bearing from the aircraft's own
    # first position to the destination airport, within 45° of the observed
    # heading. Not from the origin — a San Francisco→Copenhagen flight leaves
    # heading north-east and passes Aarhus heading south-east, and only the
    # second is what the antenna can see. A route that fails the check is
    # kept in route_conflict for the record and cleared from origin and
    # destination, so nothing downstream counts it.
    def bearing(lat1, lon1, lat2, lon2):
        return f"""fmod(degrees(atan2(
            sin(radians({lon2} - {lon1})) * cos(radians({lat2})),
            cos(radians({lat1})) * sin(radians({lat2}))
              - sin(radians({lat1})) * cos(radians({lat2})) * cos(radians({lon2} - {lon1}))
        )) + 360, 360)"""
    routes_join = "LEFT JOIN routes r ON r.callsign = f.callsign" if have_routes else ""
    route_cols = ("r.origin, r.destination, r.legs, r.airports, "
                  "ad.latitude_deg AS dlat, ad.longitude_deg AS dlon"
                  if have_routes else
                  "NULL AS origin, NULL AS destination, NULL AS legs, NULL AS airports, "
                  "NULL AS dlat, NULL AS dlon")
    dest_join = "LEFT JOIN airports ad ON ad.ident = r.destination" if have_routes else ""
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW routed AS
        WITH g AS (
            SELECT f.hex, f.session_idx, f.first_lat, f.first_lon, f.last_lat, f.last_lon,
                   {route_cols}
            FROM flights_agg f
            {routes_join}
            {dest_join}
        ), b AS (
            SELECT *,
                CASE WHEN first_lat IS NOT NULL AND last_lat IS NOT NULL THEN
                    2 * 6371 * asin(sqrt(
                        pow(sin(radians(last_lat - first_lat) / 2), 2)
                        + cos(radians(first_lat)) * cos(radians(last_lat))
                          * pow(sin(radians(last_lon - first_lon) / 2), 2)))
                END AS moved_km,
                CASE WHEN first_lat IS NOT NULL AND last_lat IS NOT NULL THEN
                    {bearing('first_lat', 'first_lon', 'last_lat', 'last_lon')}
                END AS heading,
                CASE WHEN first_lat IS NOT NULL AND dlat IS NOT NULL THEN
                    {bearing('first_lat', 'first_lon', 'dlat', 'dlon')}
                END AS expected
            FROM g
        )
        SELECT hex, session_idx, origin, destination, legs, airports,
               round(moved_km, 1) AS moved_km, round(heading) AS heading,
               CASE WHEN heading IS NULL THEN NULL ELSE
                   (['N','NE','E','SE','S','SW','W','NW'])
                       [CAST(floor(fmod(heading + 22.5, 360) / 45) AS INTEGER) + 1]
               END AS direction,
               CASE WHEN origin IS NULL THEN NULL
                    WHEN moved_km IS NULL OR moved_km < 15 OR expected IS NULL THEN 'unverified'
                    WHEN abs(fmod(expected - heading + 540, 360) - 180) <= 45 THEN 'verified'
                    ELSE 'conflict' END AS route_check
        FROM b
    """)

    # 3b) attach airports + aircraft + airline + the checked route
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
            {airline_select},
            rt.moved_km, rt.heading, rt.direction,
            CASE WHEN rt.route_check = 'conflict' THEN NULL ELSE rt.origin END      AS origin,
            CASE WHEN rt.route_check = 'conflict' THEN NULL ELSE rt.destination END AS destination,
            CASE WHEN rt.route_check = 'conflict' THEN NULL ELSE rt.legs END        AS route_legs,
            rt.route_check,
            CASE WHEN rt.route_check = 'conflict' THEN rt.airports END              AS route_conflict
        FROM flights_agg f
        {ac_join}
        {airline_join}
        LEFT JOIN routed rt ON rt.hex = f.hex AND rt.session_idx = f.session_idx
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
