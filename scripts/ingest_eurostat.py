"""
Eurostat `avia_par_*` → local parquet warehouse.

Complements OpenSky. OpenSky gives us up-to-yesterday flight counts; Eurostat
gives us passenger counts with higher confidence but a ~2-month publication lag.
Together they let us: (a) surface signals fast via OpenSky, (b) reconcile /
correct them against Eurostat when the official numbers land.

Usage:
    # last 24 months, all reporting countries
    python scripts/ingest_eurostat.py

    # backfill a range
    python scripts/ingest_eurostat.py --from 2019-01 --to 2026-06

    # specific reporters only
    python scripts/ingest_eurostat.py --reporters DE,FR,ES

Storage:
    ~/data/ovrhead-warehouse/raw/eurostat/avia_par_<reporter>.parquet
        columns: reporter, partner_country, partner_airport, month, passengers
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("Install deps: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eurostat_client import REPORTING, fetch_airport_pairs
from warehouse import warehouse_root


def _months_in_range(start_yyyymm: str, end_yyyymm: str) -> list[str]:
    sy, sm = map(int, start_yyyymm.split("-"))
    ey, em = map(int, end_yyyymm.split("-"))
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1; y += 1
    return out


def _default_range() -> tuple[str, str]:
    """Last 24 complete months (Eurostat lags ~2 months)."""
    today = date.today()
    # end = two months back to be safe about availability
    y, m = today.year, today.month - 2
    while m <= 0:
        m += 12; y -= 1
    end = f"{y:04d}-{m:02d}"
    # start = 23 months before end
    sy, sm = y, m - 23
    while sm <= 0:
        sm += 12; sy -= 1
    start = f"{sy:04d}-{sm:02d}"
    return start, end


def ingest_reporter(reporter: str, months: list[str]) -> Path | None:
    print(f"[eurostat] {reporter}: fetching {len(months)} months")
    rows = fetch_airport_pairs(reporter, months)
    if not rows:
        print(f"[eurostat] {reporter}: no data returned")
        return None

    records = [{
        "reporter":         r.reporter_country,
        "partner_country":  r.partner_country,
        "partner_airport":  r.partner_airport,
        "month":            r.month,
        "passengers":       r.passengers,
    } for r in rows]

    out = warehouse_root() / "raw" / "eurostat" / f"avia_par_{reporter.lower()}.parquet"
    tbl = pa.Table.from_pylist(records)

    if out.exists():
        old = pq.read_table(out)
        combined = pa.concat_tables([old, tbl], promote_options="default")
        df = combined.to_pandas().drop_duplicates(
            subset=["reporter", "partner_country", "partner_airport", "month"], keep="last"
        )
        tbl = pa.Table.from_pandas(df, preserve_index=False)

    pq.write_table(tbl, out, compression="zstd")
    print(f"[eurostat] {reporter}: wrote {len(records):,} new rows → {out.name} "
          f"(total: {tbl.num_rows:,})")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="from_", help="YYYY-MM inclusive")
    p.add_argument("--to",   help="YYYY-MM inclusive")
    p.add_argument("--reporters", help="Comma-separated ISO2 codes (default: all EU reporters)")
    p.add_argument("--polite", type=float, default=0.5, help="Delay between datasets in seconds")
    args = p.parse_args()

    if args.from_ and args.to:
        months = _months_in_range(args.from_, args.to)
    elif args.from_ or args.to:
        print("Provide both --from and --to", file=sys.stderr); return 1
    else:
        start, end = _default_range()
        months = _months_in_range(start, end)

    reporters = [r.strip().upper() for r in args.reporters.split(",")] if args.reporters else REPORTING
    print(f"[eurostat] plan: {len(reporters)} reporter(s) × {len(months)} months")

    for r in reporters:
        try:
            ingest_reporter(r, months)
        except Exception as e:
            print(f"[eurostat] {r} failed: {e}", file=sys.stderr)
        time.sleep(args.polite)
    return 0


if __name__ == "__main__":
    sys.exit(main())
