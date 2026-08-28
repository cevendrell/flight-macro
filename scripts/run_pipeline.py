"""
End-to-end pipeline orchestrator. This is what the daily cron runs.

Sequence:
  1. Ingest yesterday's OpenSky flights            → append to warehouse
  2. (Optionally) refresh Eurostat monthly         → append to warehouse
  3. Rollup                                        → curated Parquet
  4. Generate insights (query warehouse + Claude)  → data/insights.json
  5. Commit + push if the JSON changed             → GitHub Pages redeploys

Usage:
    python scripts/run_pipeline.py                 # everything, sensible defaults
    python scripts/run_pipeline.py --skip-eurostat --skip-git

Env:
    ANTHROPIC_API_KEY   required for enrichment (or pass --dry-run)
    OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET  required for OpenSky fetches
    OVRHEAD_WAREHOUSE   optional, overrides ~/data/ovrhead-warehouse
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"


def sh(cmd: list[str], check: bool = True) -> int:
    print(f"\n$ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(REPO))
    if check and r.returncode != 0:
        raise SystemExit(f"step failed: {' '.join(cmd)}")
    return r.returncode


def git_commit_and_push(message: str) -> bool:
    """Return True if a commit was made."""
    subprocess.check_call(["git", "add", "data/insights.json"], cwd=str(REPO))
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "data/insights.json"],
        cwd=str(REPO)
    ).returncode
    if diff == 0:
        print("[git] no changes to commit.")
        return False
    subprocess.check_call(["git", "commit", "-m", message], cwd=str(REPO))
    subprocess.check_call(["git", "push"], cwd=str(REPO))
    print("[git] pushed.")
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-opensky",  action="store_true")
    p.add_argument("--skip-eurostat", action="store_true", help="Skip Eurostat refresh (fast path)")
    p.add_argument("--skip-rollup",   action="store_true")
    p.add_argument("--skip-insights", action="store_true")
    p.add_argument("--skip-git",      action="store_true")
    p.add_argument("--dry-run",       action="store_true", help="Skip Claude enrichment")
    p.add_argument("--month",         help="YYYY-MM for insight extraction")
    p.add_argument("--eurostat-months-back", type=int, default=1,
                   help="Re-fetch this many recent Eurostat months (default 1)")
    args = p.parse_args()

    started = datetime.now(timezone.utc)
    print(f"[pipeline] start {started.isoformat()}")

    # 1. OpenSky
    if not args.skip_opensky:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        sh([sys.executable, str(SCRIPTS / "ingest_opensky.py"), "--day", yesterday.isoformat()],
           check=False)

    # 2. Eurostat (only recent months to keep it fast; monthly cron can widen)
    if not args.skip_eurostat:
        today = date.today()
        # Range = [today - eurostat_months_back months, today - 2 months]
        end_y, end_m = today.year, today.month - 2
        while end_m <= 0: end_m += 12; end_y -= 1
        start_y, start_m = end_y, end_m - args.eurostat_months_back + 1
        while start_m <= 0: start_m += 12; start_y -= 1
        sh([sys.executable, str(SCRIPTS / "ingest_eurostat.py"),
            "--from", f"{start_y:04d}-{start_m:02d}",
            "--to",   f"{end_y:04d}-{end_m:02d}"], check=False)

    # 3. Rollup
    if not args.skip_rollup:
        sh([sys.executable, str(SCRIPTS / "rollup.py")], check=False)

    # 4. Insights
    if not args.skip_insights:
        cmd = [sys.executable, str(SCRIPTS / "generate_insights.py"),
               "--source", "warehouse"]
        if args.month:   cmd += ["--month", args.month]
        if args.dry_run: cmd += ["--dry-run"]
        sh(cmd)

    # 5. Git
    if not args.skip_git:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            git_commit_and_push(f"chore(data): refresh insights {stamp}")
        except Exception as e:
            print(f"[git] push failed (non-fatal): {e}", file=sys.stderr)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"[pipeline] done in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
