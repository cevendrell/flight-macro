"""
Sync ADS-B parquet files from the local warehouse into the git repo.

The repo publishes a rolling window of raw parquet under data/adsb/, which
GitHub Pages serves to the website. DuckDB-Wasm in the browser queries them
directly over HTTP range requests — no backend needed.

Layout inside the repo:
    data/adsb/
        snapshots/                       rolling N days (default 30)
            snapshots_YYYY-MM-DD.parquet
        flights/                         everything reconstruct has produced
            flights_YYYY-MM.parquet
        enrichment/                      current tables
            aircraft_db.parquet
            airports.parquet
            airlines.parquet
        manifest.json                    files + sizes + updated_at (for the site)

Older snapshots in the repo are deleted so the working tree stays under ~120 MB.
Everything in the warehouse is preserved — this only prunes the repo copy.

Run:
    python scripts/adsb/sync_to_repo.py
    python scripts/adsb/sync_to_repo.py --keep-days 60
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WAREHOUSE = Path(os.environ.get("OVRHEAD_WAREHOUSE", str(Path.home() / "data" / "ovrhead-warehouse")))
WAREHOUSE_ADSB = WAREHOUSE / "adsb"

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_DATA = REPO_ROOT / "data" / "adsb"

DEFAULT_KEEP_DAYS = 30

SNAP_RE = re.compile(r"snapshots_(\d{4}-\d{2}-\d{2})\.parquet$")


def sha256_short(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def copy_if_changed(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        # size match is a cheap proxy; the poller only appends within a day,
        # so a synced-then-updated file will differ in size.
        return False
    shutil.copy2(src, dst)
    return True


def sync_snapshots(keep_days: int) -> dict:
    src_dir = WAREHOUSE_ADSB / "snapshots"
    dst_dir = REPO_DATA / "snapshots"
    dst_dir.mkdir(parents=True, exist_ok=True)

    if not src_dir.exists():
        return {"copied": 0, "removed": 0, "kept": 0, "files": []}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days - 1)).date()
    keep_names = set()
    copied = 0

    for src in sorted(src_dir.glob("snapshots_*.parquet")):
        m = SNAP_RE.search(src.name)
        if not m:
            continue
        day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        if day < cutoff:
            continue
        keep_names.add(src.name)
        dst = dst_dir / src.name
        if copy_if_changed(src, dst):
            copied += 1

    removed = 0
    for existing in dst_dir.glob("snapshots_*.parquet"):
        if existing.name not in keep_names:
            existing.unlink()
            removed += 1

    files = sorted(dst_dir.glob("snapshots_*.parquet"))
    return {
        "copied": copied,
        "removed": removed,
        "kept": len(files),
        "files": [f.name for f in files],
    }


def sync_dir(src_dir: Path, dst_dir: Path, pattern: str) -> dict:
    if not src_dir.exists():
        return {"copied": 0, "kept": 0, "files": []}
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted(src_dir.glob(pattern)):
        if copy_if_changed(src, dst_dir / src.name):
            copied += 1
    files = sorted(dst_dir.glob(pattern))
    return {"copied": copied, "kept": len(files), "files": [f.name for f in files]}


def write_manifest() -> Path:
    """A tiny JSON index so the browser can enumerate files without a directory listing."""
    def entries(dir_: Path, pattern: str) -> list[dict]:
        if not dir_.exists():
            return []
        out = []
        for p in sorted(dir_.glob(pattern)):
            out.append({
                "name": p.name,
                "path": str(p.relative_to(REPO_ROOT)).replace("\\", "/"),
                "bytes": p.stat().st_size,
            })
        return out

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": {
            "name": "Aarhus, Denmark",
            "lat": 56.16,
            "lng": 10.20,
            "note": "One antenna. One sky. Everything that flies overhead.",
        },
        "snapshots": entries(REPO_DATA / "snapshots", "snapshots_*.parquet"),
        "flights":   entries(REPO_DATA / "flights",   "flights_*.parquet"),
        "enrichment": entries(REPO_DATA / "enrichment", "*.parquet"),
    }
    out = REPO_DATA / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    return out


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS,
                   help=f"snapshot days to keep in the repo (default {DEFAULT_KEEP_DAYS})")
    args = p.parse_args()

    print(f"[sync] warehouse: {WAREHOUSE_ADSB}")
    print(f"[sync] repo dst:  {REPO_DATA}")

    snaps = sync_snapshots(args.keep_days)
    print(f"[sync] snapshots: copied {snaps['copied']}, removed {snaps['removed']}, kept {snaps['kept']}")

    flights = sync_dir(WAREHOUSE_ADSB / "flights",    REPO_DATA / "flights",    "flights_*.parquet")
    print(f"[sync] flights:   copied {flights['copied']}, kept {flights['kept']}")

    enr = sync_dir(WAREHOUSE_ADSB / "enrichment", REPO_DATA / "enrichment", "*.parquet")
    print(f"[sync] enrichment: copied {enr['copied']}, kept {enr['kept']}")

    manifest = write_manifest()
    total = sum(f.stat().st_size for f in REPO_DATA.rglob("*.parquet"))
    print(f"[sync] manifest -> {manifest.relative_to(REPO_ROOT)}")
    print(f"[sync] repo payload: {human_bytes(total)} across {sum(1 for _ in REPO_DATA.rglob('*.parquet'))} parquet files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
