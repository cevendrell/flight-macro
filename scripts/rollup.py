"""
Corridor rollups: turn raw ingested data into curated Parquet tables ready for
anomaly detection.

Output tables (in ~/data/ovrhead-warehouse/curated/):

  corridor_monthly.parquet
    columns: o_country, d_country, month, flights, passengers
    - `flights`    from OpenSky (nullable if no OpenSky data for that pair/month)
    - `passengers` from Eurostat (nullable if pair not covered)

  corridor_weekly.parquet
    columns: o_country, d_country, week_start, flights
    - OpenSky only; Eurostat is monthly

  city_corridor_monthly.parquet
    columns: o_country, d_country, d_airport, month, flights, passengers
    - city-pair granularity, used to surface Frankfurt→Madrid style signals

Run: `python scripts/rollup.py` after any ingest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from airports import AIRPORTS
from warehouse import connect, register_views, warehouse_root


def load_airport_table(con) -> None:
    """Small in-DB lookup: ICAO → country + city + IATA."""
    rows = [
        (icao, meta["country"], meta["city"].replace("'", "''"), meta["iata"])
        for icao, meta in AIRPORTS.items()
    ]
    values = ",".join(f"('{r[0]}','{r[1]}','{r[2]}','{r[3]}')" for r in rows)
    con.execute(f"""
        CREATE OR REPLACE TABLE airport_country AS
        SELECT * FROM (VALUES {values}) AS t(icao, country, city, iata)
    """)


def has_source(con, view: str) -> bool:
    try:
        n = con.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
        return n > 0
    except Exception:
        return False


def rollup_monthly(con) -> Path:
    """
    Merge OpenSky flight counts with Eurostat passenger counts at the
    (o_country, d_country, month) grain. Full outer join so we keep pairs
    covered by only one of the two sources.
    """
    out = warehouse_root() / "curated" / "corridor_monthly.parquet"

    parts = []
    if has_source(con, "flights"):
        con.execute("""
            CREATE OR REPLACE TEMP VIEW opensky_monthly AS
            SELECT
              strftime(CAST(day AS DATE), '%Y-%m') AS month,
              ao.country AS o_country,
              ad.country AS d_country,
              COUNT(*)   AS flights
            FROM flights f
            LEFT JOIN airport_country ao ON ao.icao = f.origin
            LEFT JOIN airport_country ad ON ad.icao = f.destination
            WHERE ao.country IS NOT NULL AND ad.country IS NOT NULL
            GROUP BY 1, 2, 3
        """)
        parts.append("opensky_monthly")

    if has_source(con, "eurostat"):
        con.execute("""
            CREATE OR REPLACE TEMP VIEW eurostat_monthly AS
            SELECT
              month,
              reporter        AS o_country,
              partner_country AS d_country,
              SUM(passengers) AS passengers
            FROM eurostat
            WHERE partner_country IS NOT NULL
            GROUP BY 1, 2, 3
        """)
        parts.append("eurostat_monthly")

    if not parts:
        print("[rollup] no source data at all — nothing to write.")
        return out

    if len(parts) == 2:
        query = """
            SELECT
              COALESCE(o.o_country, e.o_country) AS o_country,
              COALESCE(o.d_country, e.d_country) AS d_country,
              COALESCE(o.month,     e.month)     AS month,
              o.flights,
              e.passengers
            FROM opensky_monthly o
            FULL OUTER JOIN eurostat_monthly e
              ON o.o_country = e.o_country AND o.d_country = e.d_country AND o.month = e.month
        """
    elif parts == ["opensky_monthly"]:
        query = "SELECT o_country, d_country, month, flights, CAST(NULL AS BIGINT) AS passengers FROM opensky_monthly"
    else:
        query = "SELECT o_country, d_country, month, CAST(NULL AS BIGINT) AS flights, passengers FROM eurostat_monthly"

    con.execute(f"COPY ({query}) TO '{out}' (FORMAT 'parquet', COMPRESSION 'zstd')")
    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
    print(f"[rollup] monthly → {out.name} ({n:,} rows)")
    return out


def rollup_weekly(con) -> Path:
    """Weekly OpenSky-only rollup — powers the near-live signal layer."""
    out = warehouse_root() / "curated" / "corridor_weekly.parquet"
    if not has_source(con, "flights"):
        print("[rollup] no OpenSky data — skipping weekly rollup.")
        return out

    con.execute(f"""
        COPY (
            SELECT
              date_trunc('week', CAST(day AS DATE)) AS week_start,
              ao.country AS o_country,
              ad.country AS d_country,
              COUNT(*)   AS flights
            FROM flights f
            LEFT JOIN airport_country ao ON ao.icao = f.origin
            LEFT JOIN airport_country ad ON ad.icao = f.destination
            WHERE ao.country IS NOT NULL AND ad.country IS NOT NULL
            GROUP BY 1, 2, 3
        ) TO '{out}' (FORMAT 'parquet', COMPRESSION 'zstd')
    """)
    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
    print(f"[rollup] weekly → {out.name} ({n:,} rows)")
    return out


def rollup_city_monthly(con) -> Path:
    """
    City-pair (o_country → d_airport) monthly rollup.
    Uses OpenSky flight counts and Eurostat passengers at airport grain.
    """
    out = warehouse_root() / "curated" / "city_corridor_monthly.parquet"

    parts = []
    if has_source(con, "flights"):
        con.execute("""
            CREATE OR REPLACE TEMP VIEW opensky_city_monthly AS
            SELECT
              strftime(CAST(day AS DATE), '%Y-%m') AS month,
              ao.country AS o_country,
              ad.country AS d_country,
              f.destination AS d_airport,
              COUNT(*)   AS flights
            FROM flights f
            LEFT JOIN airport_country ao ON ao.icao = f.origin
            LEFT JOIN airport_country ad ON ad.icao = f.destination
            WHERE ao.country IS NOT NULL AND ad.country IS NOT NULL
            GROUP BY 1, 2, 3, 4
        """)
        parts.append("opensky_city_monthly")

    if has_source(con, "eurostat"):
        con.execute("""
            CREATE OR REPLACE TEMP VIEW eurostat_city_monthly AS
            SELECT
              month,
              reporter        AS o_country,
              partner_country AS d_country,
              partner_airport AS d_airport,
              SUM(passengers) AS passengers
            FROM eurostat
            WHERE partner_airport IS NOT NULL AND partner_airport <> ''
            GROUP BY 1, 2, 3, 4
        """)
        parts.append("eurostat_city_monthly")

    if not parts:
        print("[rollup] no source data — skipping city rollup.")
        return out

    if len(parts) == 2:
        query = """
            SELECT
              COALESCE(o.o_country, e.o_country) AS o_country,
              COALESCE(o.d_country, e.d_country) AS d_country,
              COALESCE(o.d_airport, e.d_airport) AS d_airport,
              COALESCE(o.month,     e.month)     AS month,
              o.flights,
              e.passengers
            FROM opensky_city_monthly o
            FULL OUTER JOIN eurostat_city_monthly e
              ON o.o_country = e.o_country AND o.d_country = e.d_country
             AND o.d_airport = e.d_airport AND o.month = e.month
        """
    elif parts == ["opensky_city_monthly"]:
        query = "SELECT o_country, d_country, d_airport, month, flights, CAST(NULL AS BIGINT) AS passengers FROM opensky_city_monthly"
    else:
        query = "SELECT o_country, d_country, d_airport, month, CAST(NULL AS BIGINT) AS flights, passengers FROM eurostat_city_monthly"

    con.execute(f"COPY ({query}) TO '{out}' (FORMAT 'parquet', COMPRESSION 'zstd')")
    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
    print(f"[rollup] city monthly → {out.name} ({n:,} rows)")
    return out


def main() -> int:
    con = connect()
    register_views(con)
    load_airport_table(con)

    have_flights   = has_source(con, "flights")
    have_eurostat  = has_source(con, "eurostat")
    if not have_flights and not have_eurostat:
        print("[rollup] warehouse is empty. Run ingest_opensky.py or ingest_eurostat.py first.")
        return 0
    if have_flights:
        n = con.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
        print(f"[rollup] OpenSky rows: {n:,}")
    if have_eurostat:
        n = con.execute("SELECT COUNT(*) FROM eurostat").fetchone()[0]
        print(f"[rollup] Eurostat rows: {n:,}")

    rollup_monthly(con)
    rollup_weekly(con)
    rollup_city_monthly(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
