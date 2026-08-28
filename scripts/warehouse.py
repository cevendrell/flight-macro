"""
DuckDB warehouse layer.

Convention: all bulk data lives outside the repo, in ~/data/ovrhead-warehouse/,
so git stays tiny. DuckDB queries Parquet files directly — no import step.

Layout:
  ~/data/ovrhead-warehouse/
    raw/
      opensky/flights_YYYY-MM.parquet         (one file per month, append daily)
      eurostat/<dataset>_<reporter>.parquet
    curated/
      corridor_monthly.parquet                (o, d, month, pax, flights)
      corridor_weekly.parquet                 (o, d, week_start, pax, flights)
    ovrhead.duckdb                             (persistent views, small)

Override the root by exporting OVRHEAD_WAREHOUSE=/some/path.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import duckdb
except ImportError:
    duckdb = None  # allow import for scripts that don't need it


def warehouse_root() -> Path:
    root = os.environ.get("OVRHEAD_WAREHOUSE") or "~/data/ovrhead-warehouse"
    p = Path(root).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    (p / "raw" / "opensky").mkdir(parents=True, exist_ok=True)
    (p / "raw" / "eurostat").mkdir(parents=True, exist_ok=True)
    (p / "curated").mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return warehouse_root() / "ovrhead.duckdb"


def connect(read_only: bool = False):
    if duckdb is None:
        raise RuntimeError("duckdb not installed. `pip install duckdb pyarrow`.")
    return duckdb.connect(str(db_path()), read_only=read_only)


def register_views(con) -> None:
    """
    Register views that abstract over the parquet files. This lets downstream
    code write plain SQL: `SELECT * FROM flights WHERE month = '2025-08'`.
    """
    root = warehouse_root()
    opensky_glob   = str(root / "raw" / "opensky" / "flights_*.parquet")
    eurostat_glob  = str(root / "raw" / "eurostat" / "*.parquet")
    corridor_month = str(root / "curated" / "corridor_monthly.parquet")
    corridor_week  = str(root / "curated" / "corridor_weekly.parquet")

    con.execute(f"""
        CREATE OR REPLACE VIEW flights AS
        SELECT * FROM read_parquet('{opensky_glob}', union_by_name=true)
    """)
    con.execute(f"""
        CREATE OR REPLACE VIEW eurostat AS
        SELECT * FROM read_parquet('{eurostat_glob}', union_by_name=true)
    """)
    if Path(corridor_month).exists():
        con.execute(f"""
            CREATE OR REPLACE VIEW corridor_monthly AS
            SELECT * FROM read_parquet('{corridor_month}')
        """)
    if Path(corridor_week).exists():
        con.execute(f"""
            CREATE OR REPLACE VIEW corridor_weekly AS
            SELECT * FROM read_parquet('{corridor_week}')
        """)
