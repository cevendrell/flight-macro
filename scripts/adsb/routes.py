"""
Callsign → scheduled route, for every callsign the antenna has heard.

An airline flies the same flight number on the same route day after day, and
the callsign an aircraft broadcasts is the airline's ICAO code plus that
number (Ryanair uses fixed operational codes like RYR9SE instead of the
commercial FR number, but they are just as stable per rotation). The VRS
standing-data project publishes callsign → airports tables built from
thousands of receivers seeing where each callsign departs and lands. This
script fetches the tables for the airline prefixes present in our record —
about forty, out of some 1,500 files — and writes one small Parquet of the
routes for callsigns we have actually seen.

Whether a looked-up route is *true* of a given flight is not decided here.
reconstruct.py checks every match against the direction the aircraft was
observed travelling and marks it verified, unverified or conflict.

Storage:
    <warehouse>/adsb/routes-cache/            raw CSVs, refreshed weekly
    <warehouse>/adsb/enrichment/routes.parquet   callsign, origin, destination

Run:
    python scripts/adsb/routes.py            (uses OVRHEAD_WAREHOUSE like the rest)
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("Install deps first: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

WAREHOUSE = Path(os.environ.get("OVRHEAD_WAREHOUSE", str(Path.home() / "data" / "ovrhead-warehouse")))
ADSB      = WAREHOUSE / "adsb"
CACHE     = ADSB / "routes-cache"
OUT       = ADSB / "enrichment" / "routes.parquet"

REPO  = "vradarserver/standing-data"
TREE  = f"https://api.github.com/repos/{REPO}/git/trees/main?recursive=1"
RAW   = f"https://raw.githubusercontent.com/{REPO}/main/"
MAX_AGE_SEC = 7 * 86400          # refetch a table after a week

PREFIX_RE = re.compile(r"^[A-Z]{3}(?=[A-Z0-9]{1,4}$)")


def log(msg: str) -> None:
    print(f"[routes] {msg}")


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ovrhead-routes/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fresh(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < MAX_AGE_SEC


def seen_prefixes(con) -> set[str]:
    """Three-letter airline codes of every callsign in the snapshots."""
    snaps = list((ADSB / "snapshots").glob("*.parquet"))
    if not snaps:
        return set()
    con.execute(f"""
        CREATE OR REPLACE VIEW snap AS
        SELECT * FROM read_parquet('{ADSB / "snapshots" / "*.parquet"}', union_by_name=true)
    """)
    out = set()
    for (cs,) in con.execute(
        "SELECT DISTINCT UPPER(TRIM(flight)) FROM snap WHERE flight IS NOT NULL"
    ).fetchall():
        m = PREFIX_RE.match(cs or "")
        if m:
            out.add(m.group(0))
    return out


def route_files() -> list[str]:
    """Every route CSV path in the upstream repo, from one cached tree listing.
    A network failure falls back to whatever the cache already holds, however
    stale, so the pipeline keeps enriching from the last known good listing."""
    cache = CACHE / "tree.json"
    if not fresh(cache):
        try:
            log("listing upstream route tables")
            tree = json.loads(fetch(TREE))
            paths = [t["path"] for t in tree.get("tree", [])
                     if t["path"].startswith("routes/schema-01/") and t["path"].endswith(".csv")]
            if paths:
                CACHE.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(paths))
        except Exception as e:
            if cache.exists():
                log(f"tree listing failed ({type(e).__name__}: {e}); using stale cache")
            else:
                log(f"tree listing failed ({type(e).__name__}: {e}); no cache to fall back on")
                return []
    try:
        return json.loads(cache.read_text())
    except Exception:
        return []


def tables_for(prefix: str, all_paths: list[str]) -> list[str]:
    """Upstream paths for one prefix: PREFIX-all.csv, or PREFIX-1.csv … for the
    large carriers whose tables are split."""
    pat = re.compile(rf"/{re.escape(prefix)}-(all|\d+)\.csv$")
    return [p for p in all_paths if pat.search(p)]


def load_table(path: str) -> dict[str, str]:
    """callsign → 'ORIG-DEST[-…]' from one upstream CSV, via the local cache."""
    local = CACHE / path.split("routes/schema-01/", 1)[1]
    if not fresh(local):
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(fetch(RAW + path))
    text = local.read_text(encoding="utf-8-sig")
    out: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        cs, codes = (row.get("Callsign") or "").strip().upper(), (row.get("AirportCodes") or "").strip()
        if cs and codes and "-" in codes:
            out[cs] = codes
    return out


def _keep_previous(reason: str) -> int:
    """Leave whatever routes.parquet already exists in place. An empty or
    partial write would silently strip route columns from reconstruct's
    output; keeping yesterday's file is always the better default."""
    if OUT.exists():
        log(f"{reason}; keeping previous {OUT.name} ({OUT.stat().st_size / 1024:.0f} KB)")
    else:
        log(f"{reason}; no previous file to keep, reconstruct will run without routes")
    return 0


def main() -> int:
    # Any unexpected failure below returns 0 with an audible log message. The
    # nightly script also wraps this in TryRun, so both belts and braces: the
    # pipeline is guaranteed to reach reconstruct even if this script crashes.
    try:
        con = duckdb.connect()
        prefixes = seen_prefixes(con)
        if not prefixes:
            return _keep_previous("no callsigns in the snapshots yet")
        log(f"{len(prefixes)} airline prefixes in the record")

        all_paths = route_files()
        if not all_paths:
            return _keep_previous("no route listing available (offline?)")

        routes: dict[str, str] = {}
        missing: list[str] = []
        files = fetch_errors = 0
        for p in sorted(prefixes):
            paths = tables_for(p, all_paths)
            if not paths:
                missing.append(p)
                continue
            for path in paths:
                try:
                    routes.update(load_table(path))
                    files += 1
                except Exception as e:              # one bad table must not sink the run
                    fetch_errors += 1
                    log(f"  {path}: {type(e).__name__}: {e}")
        log(f"{files} tables, {len(routes):,} routes loaded"
            f"{f' ({fetch_errors} errors)' if fetch_errors else ''}; "
            f"no table for: {', '.join(missing) or '—'}")

        # A run that fetched almost nothing was almost certainly rate-limited or
        # offline in the middle. Keeping yesterday's file is better than
        # replacing it with a tiny fragment that quietly strips routes off most
        # flights when reconstruct joins against it.
        if files < max(3, len(prefixes) // 4) and OUT.exists():
            return _keep_previous(f"only {files} tables succeeded — likely a network problem")

        heard = {cs for (cs,) in con.execute(
            "SELECT DISTINCT UPPER(TRIM(flight)) FROM snap WHERE flight IS NOT NULL").fetchall()}
        rows = []
        for cs in sorted(heard):
            codes = routes.get(cs)
            if not codes:
                continue
            legs = codes.split("-")
            rows.append({"callsign": cs, "origin": legs[0], "destination": legs[-1],
                         "legs": len(legs) - 1, "airports": codes})

        OUT.parent.mkdir(parents=True, exist_ok=True)
        con.execute("CREATE TABLE out (callsign VARCHAR, origin VARCHAR, destination VARCHAR, "
                    "legs INTEGER, airports VARCHAR)")
        if rows:
            con.executemany("INSERT INTO out VALUES (?, ?, ?, ?, ?)",
                            [(r["callsign"], r["origin"], r["destination"], r["legs"], r["airports"])
                             for r in rows])
        con.execute(f"COPY out TO '{OUT}' (FORMAT 'parquet', COMPRESSION 'zstd')")
        log(f"{len(rows):,} of {len(heard):,} heard callsigns have a route "
            f"({len(rows) / max(1, len(heard)) * 100:.0f}%) -> {OUT.name} "
            f"({OUT.stat().st_size / 1024:.0f} KB)")
        return 0
    except Exception as e:
        return _keep_previous(f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
