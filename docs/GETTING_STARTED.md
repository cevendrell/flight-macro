# OvrHead — laptop setup (Matebook)

The whole pipeline runs from your laptop. This guide is copy-paste for a fresh
setup this afternoon.

---

## 1. One-time setup (10 min)

```bash
# from anywhere
cd ~/Documents/GitHub/ovrhead    # or wherever you cloned it

# Python deps
python3 -m venv .venv
source .venv/bin/activate         # on Windows: .venv\Scripts\activate
pip install -r scripts/requirements.txt

# API key — needed only for the Claude enrichment step
export ANTHROPIC_API_KEY="sk-ant-..."

# Optional: OpenSky free account for higher rate limits
# https://opensky-network.org/ → sign up → then:
export OPENSKY_USER="your_username"
export OPENSKY_PASS="your_password"
```

That's the whole install. The warehouse directory (`~/data/ovrhead-warehouse/`)
gets created automatically on first run.

## 2. Fast path — real data in 2 minutes (no deps beyond stdlib)

If you just want real Eurostat numbers on the site without setting up DuckDB,
run this. Writes real signals to `data/insights.json`, uses only Python stdlib.

```bash
python3 scripts/fetch_real_now.py --month 2024-10 --top 40 --top-city 100
```

- ~2 min on first run (Eurostat API is slow)
- Subsequent runs use disk cache in `scripts/.cache/eurostat_raw/`
- Readings are placeholders — real LLM enrichment happens once the full
  pipeline runs (below)

Commit, push, done. This is the pragmatic "prove it works" step.

## 3. Smoke test (5 min) — the full pipeline

Prove the whole chain works before wiring up cron:

```bash
# One day of OpenSky flights (only airports in scripts/airports.py)
python scripts/ingest_opensky.py --day 2026-08-27

# One recent Eurostat month
python scripts/ingest_eurostat.py --from 2026-05 --to 2026-05 --reporters DE,FR,ES

# Roll it up
python scripts/rollup.py

# See what's in the warehouse
python scripts/warehouse_inspect.py

# Extract insights (skip Claude for the smoke test)
python scripts/generate_insights.py --source warehouse --dry-run
```

If `data/insights.json` updated with real corridors, everything works.

## 3. Backfill (optional, run once)

Get some history so weekly/monthly comparisons are meaningful:

```bash
# 24 months of Eurostat, all reporters (~5 min, ~50 MB)
python scripts/ingest_eurostat.py --from 2024-08 --to 2026-06

# Last 30 days of OpenSky for headline airports (~30 min with free auth)
python scripts/ingest_opensky.py --from 2026-07-28 --to 2026-08-27

python scripts/rollup.py
python scripts/warehouse_inspect.py
```

For a bigger OpenSky backfill (years), switch to their bulk/research access
later — the REST endpoint is fine for daily-forward but slow for years-back.

## 4. Daily production run

Once smoke-test passes, this is the daily command:

```bash
python scripts/run_pipeline.py
```

It does: OpenSky ingest → Eurostat refresh (last 1 month) → rollup → insights → git push.

Sub-flags:
- `--skip-eurostat` — fast path when you know nothing new was released
- `--skip-git` — local test, don't push
- `--dry-run` — skip Claude entirely
- `--month 2026-06` — regenerate a specific historical month

## 5. Wire up the cron

### macOS / Linux
```bash
crontab -e
```

Paste:
```cron
# OvrHead — daily 04:00 UTC
0 4 * * * cd ~/Documents/GitHub/ovrhead && source .venv/bin/activate && python scripts/run_pipeline.py >> ~/data/ovrhead-warehouse/pipeline.log 2>&1
```

Env vars in cron won't inherit — either export them in `~/.zshrc` (and start
cron with a login shell), or hardcode via a wrapper script. Simplest wrapper:

```bash
#!/usr/bin/env bash
# save as ~/bin/ovrhead-daily.sh, chmod +x
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENSKY_USER="..."
export OPENSKY_PASS="..."
cd ~/Documents/GitHub/ovrhead
source .venv/bin/activate
python scripts/run_pipeline.py
```

Then the cron line becomes:
```cron
0 4 * * * ~/bin/ovrhead-daily.sh >> ~/data/ovrhead-warehouse/pipeline.log 2>&1
```

### Windows (Task Scheduler)
1. Open Task Scheduler → Create Basic Task
2. Trigger: Daily at 04:00
3. Action: Start a Program
   - Program: `powershell.exe`
   - Arguments: `-Command "cd ~\Documents\GitHub\ovrhead; .\.venv\Scripts\Activate.ps1; python scripts\run_pipeline.py"`
4. Under Conditions, uncheck "Start only on AC power" if you want it to run on battery too

## 6. What to look at after each run

```bash
python scripts/warehouse_inspect.py     # row counts + top corridors
git log data/insights.json --oneline    # commit history of the published JSON
```

The published site refreshes ~1–2 minutes after `git push` — GitHub Pages picks
it up automatically. Nothing else to babysit.

---

## Trouble

- **OpenSky returns 429**: you're hitting the anonymous rate limit. Set
  `OPENSKY_USER` / `OPENSKY_PASS` (free account).
- **Eurostat returns empty**: the month you asked for isn't published yet.
  Eurostat lags ~2 months; the ingester's default range already accounts for it.
- **`duckdb` missing**: `pip install -r scripts/requirements.txt` in the venv.
- **Nothing in `data/insights.json` after a run**: check
  `python scripts/warehouse_inspect.py` — probably no rows yet for the target month.
- **Git push fails from cron**: cron doesn't have your ssh agent by default. Use
  an SSH key without a passphrase, or switch the remote to HTTPS + a personal
  access token stored in `~/.git-credentials`.
