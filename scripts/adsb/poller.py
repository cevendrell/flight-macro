"""
ADS-B poller — pulls aircraft.json from a local tar1090/readsb instance
every N seconds and appends observations to a daily Parquet file.

Runs forever. Designed to be launched at Windows startup by Task Scheduler
(runs in the background as a Python process; simple `while True` loop with
retry-on-error).

Config (env vars, with defaults):
    OVRHEAD_ADSB_URL    default: http://192.168.1.98/tar1090/data/aircraft.json
    OVRHEAD_POLL_SEC    default: 30
    OVRHEAD_WAREHOUSE   default: ~/data/ovrhead-warehouse

Storage:
    <warehouse>/adsb/snapshots/snapshots_YYYY-MM-DD.parquet
    <warehouse>/adsb/adsb.log

One row per aircraft per snapshot. Small (~100-200 rows per snapshot,
snapshot every 30s => ~15-30k rows/hour, ~4 MB/day compressed).

Run manually:
    python scripts/adsb/poller.py

Run in background on Windows:
    Start-Process pythonw.exe -ArgumentList "scripts\\adsb\\poller.py" -WindowStyle Hidden
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("Install deps first: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)


# ── Config ──────────────────────────────────────────────────────────────────

DEFAULT_URL       = "http://192.168.1.98/tar1090/data/aircraft.json"
DEFAULT_POLL_SEC  = 30
DEFAULT_WAREHOUSE = Path.home() / "data" / "ovrhead-warehouse"

URL       = os.environ.get("OVRHEAD_ADSB_URL", DEFAULT_URL)
POLL_SEC  = int(os.environ.get("OVRHEAD_POLL_SEC", DEFAULT_POLL_SEC))
WAREHOUSE = Path(os.environ.get("OVRHEAD_WAREHOUSE", str(DEFAULT_WAREHOUSE)))
OUT_DIR   = WAREHOUSE / "adsb" / "snapshots"
LOG_FILE  = WAREHOUSE / "adsb" / "adsb.log"

# Columns kept per aircraft observation — keep it minimal so files stay small.
COLUMNS = [
    "ts",           # snapshot epoch (int, seconds UTC)
    "hex",          # ICAO24 hex id (aircraft-unique)
    "flight",       # callsign (nullable)
    "lat", "lon",   # position (nullable if not currently reported)
    "alt_baro",     # barometric altitude in feet (int, nullable)
    "gs",           # ground speed knots (float, nullable)
    "track",        # track deg (float, nullable)
    "squawk",       # transponder code (string, nullable)
    "category",     # ADS-B category e.g. A5 for heavy, C1 for surface (nullable)
    "seen_pos",     # seconds since last position update (float)
]

# ── Logging ─────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # logging must never crash the poller


# ── Fetch ───────────────────────────────────────────────────────────────────

def fetch_once(url: str, timeout: int = 12) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ovrhead-adsb-poller/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        log(f"[poll] URL error: {e.reason}")
        return None
    except Exception as e:
        log(f"[poll] fetch failed: {type(e).__name__}: {e}")
        return None


def to_rows(payload: dict) -> list[dict]:
    """Turn a tar1090 aircraft.json payload into a list of observation rows."""
    ts_now = int(payload.get("now", time.time()))
    rows = []
    for a in payload.get("aircraft", []):
        # Skip aircraft we haven't seen a position for recently (>60s).
        # These often represent very stale entries lingering in the feed.
        seen_pos = a.get("seen_pos")
        if seen_pos is not None and seen_pos > 60:
            continue
        rows.append({
            "ts":       ts_now,
            "hex":      (a.get("hex") or "").lower() or None,
            "flight":   (a.get("flight") or "").strip() or None,
            "lat":      a.get("lat"),
            "lon":      a.get("lon"),
            # readsb, dump1090 and the various tar1090 builds disagree on the
            # names of these two. We were reading only the readsb spelling,
            # which is why every altitude and speed in the record so far came
            # out null. Take the first key that is actually present.
            "alt_baro": _int_or_none(_first(a, "alt_baro", "altitude", "alt", "alt_geom")),
            "gs":       _float_or_none(_first(a, "gs", "speed", "ground_speed", "gsp")),
            "track":    _float_or_none(_first(a, "track", "trak", "heading")),
            "squawk":   a.get("squawk"),
            "category": a.get("category"),
            "seen_pos": _float_or_none(seen_pos),
        })
    return rows


def _first(d: dict, *keys):
    """First key present with a non-null value, so we tolerate feed variants."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _int_or_none(v):
    if v is None or v == "ground":
        return None
    try: return int(v)
    except (TypeError, ValueError): return None


def _float_or_none(v):
    if v is None:
        return None
    try: return float(v)
    except (TypeError, ValueError): return None


# ── Buffered writes ─────────────────────────────────────────────────────────

class DailyWriter:
    """
    Accumulates rows in memory, flushes to a daily Parquet file every N
    snapshots (or every flush_sec seconds). Writing per snapshot would create
    thousands of tiny files; batching keeps things clean.

    On rotation (UTC day change) the buffer is flushed to yesterday's file
    before opening today's.
    """
    def __init__(self, out_dir: Path, flush_every: int = 20, flush_sec: int = 300):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.flush_every = flush_every
        self.flush_sec = flush_sec
        self.buf: list[dict] = []
        self.last_flush = time.time()
        self.current_day = self._today()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _path_for(self, day: str) -> Path:
        return self.out_dir / f"snapshots_{day}.parquet"

    def add(self, rows: list[dict]) -> None:
        # Day rollover — flush what we have to the previous day, then reset
        today = self._today()
        if today != self.current_day and self.buf:
            self._flush_to(self.current_day)
            self.current_day = today
        self.buf.extend(rows)
        if len(self.buf) >= self.flush_every * 100 or (time.time() - self.last_flush) > self.flush_sec:
            self.flush()

    def flush(self) -> None:
        if not self.buf:
            self.last_flush = time.time()
            return
        self._flush_to(self.current_day)
        self.last_flush = time.time()

    def _flush_to(self, day: str) -> None:
        if not self.buf:
            return
        path = self._path_for(day)
        new_tbl = pa.Table.from_pylist(self.buf, schema=_schema())
        if path.exists():
            old_tbl = pq.read_table(path)
            combined = pa.concat_tables([old_tbl, new_tbl], promote_options="default")
            pq.write_table(combined, path, compression="zstd")
        else:
            pq.write_table(new_tbl, path, compression="zstd")
        log(f"[write] {len(self.buf):>4} rows -> {path.name}  (file now {path.stat().st_size/1024:.0f} KB)")
        self.buf.clear()


def _schema() -> pa.Schema:
    return pa.schema([
        ("ts",       pa.int64()),
        ("hex",      pa.string()),
        ("flight",   pa.string()),
        ("lat",      pa.float64()),
        ("lon",      pa.float64()),
        ("alt_baro", pa.int64()),
        ("gs",       pa.float64()),
        ("track",    pa.float64()),
        ("squawk",   pa.string()),
        ("category", pa.string()),
        ("seen_pos", pa.float64()),
    ])


# ── Main loop ───────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=URL, help=f"aircraft.json URL (default: {URL})")
    p.add_argument("--interval", type=int, default=POLL_SEC, help=f"seconds between polls (default: {POLL_SEC})")
    p.add_argument("--once", action="store_true", help="single poll then exit (for testing)")
    args = p.parse_args()

    log(f"[start] url={args.url} interval={args.interval}s warehouse={WAREHOUSE}")

    writer = DailyWriter(OUT_DIR)

    # Graceful shutdown — flush buffer on SIGINT/SIGTERM
    def _shutdown(_signum, _frame):
        log("[stop] shutdown signal, flushing buffer")
        writer.flush()
        sys.exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(sig, _shutdown)
        except Exception: pass  # signals may not be settable on Windows in some contexts

    consecutive_failures = 0
    while True:
        payload = fetch_once(args.url)
        if payload is None:
            consecutive_failures += 1
            back = min(60, args.interval * (2 ** min(consecutive_failures, 4)))
            log(f"[poll] failure #{consecutive_failures}, backing off {back}s")
            if args.once: return 1
            time.sleep(back)
            continue
        consecutive_failures = 0

        rows = to_rows(payload)
        n_ac = len(payload.get("aircraft", []))
        writer.add(rows)
        if len(writer.buf) == 0:  # just flushed
            pass
        else:
            log(f"[poll] {len(rows):>3} of {n_ac} aircraft kept  (buffer: {len(writer.buf)})")

        if args.once:
            writer.flush()
            return 0

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
