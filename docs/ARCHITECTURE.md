# OvrHead — data architecture

Ambition: a live-ish global corridor dataset that lives on the author's laptop,
extracts small "signal cards" daily, and publishes them as a tiny static JSON to
GitHub Pages. No backend, no server bills.

## Split of concerns

| Where              | What                                     | Size            | In git? |
| ------------------ | ---------------------------------------- | --------------- | ------- |
| Laptop warehouse   | Every raw flight ever ingested           | ~10s of GB      | No      |
| Laptop warehouse   | Rolled-up corridor tables (monthly/wkly) | ~100 MB         | No      |
| Laptop scripts     | Ingest, rollup, extraction               | ~10 KB          | Yes     |
| Repo `data/`       | Top-N signals, ~50 KB                    | Tiny            | Yes     |
| GitHub Pages       | Serves the site + the small JSON         | —               | —       |

The bulk data never leaves the laptop. Only the extracted signals travel.

## Storage: Parquet + DuckDB

- **Parquet** on the local SSD, one file per source-month:
  `~/data/ovrhead-warehouse/raw/opensky/flights_2026-08.parquet`
- **DuckDB** (`ovrhead.duckdb`) with views over the Parquet files — reads them
  directly, no import step
- **Curated tables** are also Parquet (`corridor_monthly.parquet`,
  `corridor_weekly.parquet`) — recomputed by `rollup.py` after each ingest

Why this stack: Parquet compresses flight data ~10× (repeat codes/dates),
DuckDB is faster than Postgres for OLAP on a laptop, and everything is a file
you can back up or throw away.

Root override: `export OVRHEAD_WAREHOUSE=/some/other/path` if the default
`~/data/ovrhead-warehouse/` doesn't suit.

## Sources (starter set)

| Source            | Lag       | Coverage           | Cost | Notes |
| ----------------- | --------- | ------------------ | ---- | ----- |
| OpenSky REST      | ~1 day    | Global commercial  | Free | Per-airport, per-day queries; scriptable |
| OpenSky Trino DB  | ~1 day    | Global, historical | Free (research account) | Bulk backfill; downloads by day |
| Eurostat `avia_*` | ~2 months | EU + partners      | Free | Passenger totals; higher fidelity than OpenSky counts |
| BTS T-100         | ~3 months | US only            | Free | Monthly, per-route, per-carrier |
| Eurocontrol       | ~1 month  | European airspace  | Free | Flight movements; deep history |

Start with **OpenSky REST + Eurostat** — no auth for the first, no auth for the
second. Add OpenSky historical (Trino) once we want to backfill years of data.

## Cadence

```
04:00 UTC daily  → ingest_opensky.py   (yesterday's flights)
04:30 UTC daily  → rollup.py           (refresh corridor tables)
05:00 UTC daily  → generate_insights.py + git push
Mon 06:00 weekly → ingest_eurostat.py  (monthly releases)
```

Scheduling on macOS: use `launchd` with a `plist` in `~/Library/LaunchAgents/`,
or plain `crontab -e`. The Matebook needs to be awake at those times — either
plug it in overnight or run at a time when it's on.

## Anomaly detection

Signals come from **rolling-window YoY deltas** over the curated tables:

- **Weekly view** (near-live): last 7d vs same 7d last year
- **Monthly view** (steady): last full month vs same month last year

Ranking (same as today):
`signal_strength = |delta_pct| × log10(volume_current)`

Filters:
- `volume_current ≥ VOLUME_FLOOR` (kills small-route noise)
- `|delta_pct| ≥ DELTA_FLOOR_PCT` (kills base-rate wobble)

## Enrichment

Top-N ranked corridors → one Claude API call each → get `{headline, reading,
theme, confidence}`. Result stored in `data/insights.json`, committed, pushed.

The API key stays on the laptop; the repo has no secrets. GitHub Actions is no
longer required for enrichment — the daily cron on the laptop does it. Actions
can still run as a fallback if the laptop is offline.

## Trust, and how to earn it

- Every signal card carries `source`, `period`, `volume_current`, `volume_prior`,
  `confidence`. Nothing is opaque.
- The `data/insights.json` file is a small, human-readable artifact under git —
  the whole publication history is reviewable via `git log data/insights.json`.
- If the same corridor is flagged repeatedly with different readings, that's a
  bug worth catching. A daily diff of the JSON surfaces those in review.

## Scripts (files today)

| Script                        | Runs when      | Reads              | Writes                          |
| ----------------------------- | -------------- | ------------------ | ------------------------------- |
| `ingest_opensky.py`           | Daily          | OpenSky REST       | `raw/opensky/flights_YYYY-MM.parquet` |
| `ingest_eurostat.py` *(todo)* | Weekly         | Eurostat JSON API  | `raw/eurostat/*.parquet`        |
| `rollup.py`                   | After ingest   | `flights` view     | `curated/corridor_*.parquet`    |
| `generate_insights.py`        | Daily          | curated + Claude   | `data/insights.json`            |
| `warehouse.py`                | (helper)       | —                  | —                               |

## First-time setup

```bash
pip install -r scripts/requirements.txt
mkdir -p ~/data/ovrhead-warehouse    # or set OVRHEAD_WAREHOUSE
python scripts/ingest_opensky.py --day 2026-08-27   # try one day
python scripts/rollup.py
python scripts/generate_insights.py --dry-run
```

That end-to-end run — one day of raw flights, rolled up, extracted — is the
smoke test for the whole architecture.
