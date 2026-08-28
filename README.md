# OvrHead

Reading global flight-route data as a lens on macroeconomic shifts — tourism, business travel, migration, supply chains.

## Structure
- `index.html` — 3D globe view (globe.gl on three.js). Click any country to load its corridors.
- `data/insights.json` — the signals the site consumes (small, committed).
- `pages/about/` — project intro
- `pages/methodology/` — how flight data maps to macro signals
- `scripts/` — Python pipeline (ingest → warehouse → rollup → Claude enrichment → JSON)
- `docs/ARCHITECTURE.md` — architecture overview
- `docs/GETTING_STARTED.md` — setup on the laptop, copy-paste

## Data flow

```
Sources                    Laptop warehouse                      Repo         Site
────────                   ────────────────────                  ─────        ────
OpenSky REST         ──▶   raw/opensky/*.parquet    ──▶
                                                        rollup ──▶  curated  ──▶  generate
Eurostat avia_par_*  ──▶   raw/eurostat/*.parquet   ──▶                            insights.json  ──▶  git push  ──▶  GitHub Pages
```

- **Warehouse** lives outside git (`~/data/ovrhead-warehouse/`), driven by DuckDB + Parquet
- **Only the small extracted JSON** is committed and served
- **Cadence**: daily cron on the laptop → repo → site refreshes ~1–2 min later

## Aesthetic
Refined finance-terminal palette: warm midnight-navy ground, brass/teak primary accent (LayOvr family), sage green for up trends, burnt sienna for down. Inter + JetBrains Mono.

## Quickstart
```bash
pip install -r scripts/requirements.txt
python scripts/ingest_opensky.py --day 2026-08-27
python scripts/ingest_eurostat.py --from 2026-05 --to 2026-05 --reporters DE,FR,ES
python scripts/rollup.py
python scripts/warehouse_inspect.py
python scripts/generate_insights.py --source warehouse --dry-run
```

See [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) for the full walkthrough.

## Workflow
- Site edits: locally → GitHub Desktop push
- Data updates: `python scripts/run_pipeline.py` on the laptop (daily cron) → auto-push
