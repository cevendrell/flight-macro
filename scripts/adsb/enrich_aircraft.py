"""
Aircraft DB refresh — standalone replacement for the aircraft section of
enrich_new.py, with explicit string-type enforcement.

The tar1090-db JSON values are [reg, type, flags, desc]. Any slot can be
missing, None, or — in malformed entries — a float (NaN). PyArrow's
from_pylist infers a mixed string/float column as float and then rejects
the write. This script casts all string fields to str (or None) before
building the table.

Run:
    python scripts/adsb/enrich_aircraft.py
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import urllib.request
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("pip install pyarrow", file=sys.stderr)
    sys.exit(1)

WAREHOUSE = Path(os.environ.get("OVRHEAD_WAREHOUSE", str(Path.home() / "data" / "ovrhead-warehouse")))
ENR = WAREHOUSE / "adsb" / "enrichment"
ENR.mkdir(parents=True, exist_ok=True)

TAR1090_DB_LIST = "https://api.github.com/repos/wiedehopf/tar1090-db/contents/db"


def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ovrhead-adsb-enrich/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _s(v) -> str | None:
    """Cast to str, keep None as None. Handles ints, floats, and NaN entries."""
    if v is None:
        return None
    if isinstance(v, float):
        return None  # NaN or other float in a string slot → treat as missing
    return str(v)


def main() -> int:
    out = ENR / "aircraft_db.parquet"
    print("[aircraft] listing tar1090-db chunks…")
    listing = json.loads(http_get(TAR1090_DB_LIST, timeout=30))
    chunks = [f for f in listing if f["name"].endswith(".js") and f.get("download_url")]
    print(f"[aircraft] fetching {len(chunks)} chunks (~5 MB total)…")

    rows: list[dict] = []
    skipped = 0

    for i, f in enumerate(chunks, 1):
        prefix = f["name"][:-3].upper()
        try:
            raw = http_get(f["download_url"], timeout=30)
        except Exception as e:
            print(f"  chunk {f['name']} download failed: {e}")
            skipped += 1
            continue

        try:
            payload = json.loads(gzip.decompress(raw).decode("utf-8"))
        except (gzip.BadGzipFile, OSError):
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                print(f"  chunk {f['name']} parse failed: {e}")
                skipped += 1
                continue

        if not isinstance(payload, dict):
            skipped += 1
            continue

        for suffix, values in payload.items():
            if not isinstance(values, (list, tuple)):
                continue
            hex_full = (prefix + str(suffix)).lower()
            rows.append({
                "hex":  hex_full,
                "reg":  _s(values[0] if len(values) > 0 else None),
                "type": _s(values[1] if len(values) > 1 else None),
                "desc": _s(values[3] if len(values) > 3 else None),
            })

        if i % 20 == 0 or i == len(chunks):
            print(f"  {i}/{len(chunks)} chunks, {len(rows):,} aircraft, {skipped} skipped")

    if not rows:
        print("ERROR: no aircraft rows produced", file=sys.stderr)
        return 1

    # Explicit schema: every column is a nullable string.
    schema = pa.schema([
        pa.field("hex",  pa.string()),
        pa.field("reg",  pa.string()),
        pa.field("type", pa.string()),
        pa.field("desc", pa.string()),
    ])
    tbl = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(tbl, out, compression="zstd")
    print(f"[aircraft] wrote {tbl.num_rows:,} rows -> {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
