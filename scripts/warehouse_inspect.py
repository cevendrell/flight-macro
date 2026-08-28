"""
Quick health-check for the local warehouse. Prints row counts, date ranges, and
the top corridors currently on file. Run this after ingesting to sanity-check.

    python scripts/warehouse_inspect.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from warehouse import connect, register_views, warehouse_root


def size_of(path: Path) -> str:
    if not path.exists():
        return "—"
    n = path.stat().st_size
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    root = warehouse_root()
    print(f"[warehouse] root: {root}")

    print("\n── Files on disk ──────────────────────────────────────")
    for p in sorted(root.rglob("*.parquet")):
        print(f"  {p.relative_to(root)}  {size_of(p)}")

    con = connect(read_only=True)
    register_views(con)

    print("\n── Sources ────────────────────────────────────────────")
    for view in ("flights", "eurostat"):
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
            print(f"  {view}: {n:,} rows")
            if n > 0:
                if view == "flights":
                    lo, hi = con.execute("SELECT MIN(day), MAX(day) FROM flights").fetchone()
                    print(f"    date range: {lo} → {hi}")
                else:
                    lo, hi = con.execute("SELECT MIN(month), MAX(month) FROM eurostat").fetchone()
                    print(f"    month range: {lo} → {hi}")
        except Exception as e:
            print(f"  {view}: not available ({type(e).__name__})")

    print("\n── Curated ────────────────────────────────────────────")
    for view in ("corridor_monthly", "corridor_weekly", "city_corridor_monthly"):
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
            print(f"  {view}: {n:,} rows")
        except Exception:
            print(f"  {view}: not built yet")

    # A little teaser: top 10 corridors by traffic in the most recent month.
    try:
        latest = con.execute("SELECT MAX(month) FROM corridor_monthly").fetchone()[0]
        if latest:
            print(f"\n── Top corridors in {latest} ────────────────────")
            rows = con.execute("""
                SELECT o_country, d_country,
                       COALESCE(passengers, flights * 100) AS vol,
                       flights, passengers
                FROM corridor_monthly WHERE month = ?
                ORDER BY vol DESC NULLS LAST LIMIT 10
            """, [latest]).fetchall()
            for o, d, vol, fl, pax in rows:
                print(f"  {o} → {d:>3}  vol={vol:>10}  flights={fl or '—':>6}  pax={pax or '—':>10}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
