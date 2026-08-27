# Flight Macro

A map-first dashboard that reads global flight-route data as a lens on macroeconomic shifts — tourism, business travel, migration, supply chains.

## Structure
- `index.html` — main map view with filterable insight feed (Leaflet + CARTO dark tiles, no build step)
- `data/insights.json` — signal records; the site fetches this at runtime
- `pages/about/` — project intro
- `pages/methodology/` — how flight data maps to macro signals
- `scripts/generate_insights.py` — Python pipeline: fetch → rank anomalies → enrich via Claude → write JSON
- `.github/workflows/update-insights.yml` — daily cron that runs the pipeline

## Aesthetic
Aeronautical-chart palette: deep navy background, pale cyan for up-trends, warm amber for down-trends. Inter + JetBrains Mono.

## Data pipeline (daily)
1. Fetch route-level flight volumes (OpenSky, BTS T-100, Eurocontrol)
2. Filter to corridors above the volume floor
3. Rank by `|delta%| × log(volume)` — takes the top ~25
4. Send each to Claude with macro context → get headline, reading, theme, confidence
5. Commit `data/insights.json` → GitHub Pages redeploys

## Setup
1. On github.com → repo Settings → Pages → Source: `main / (root)`
2. Repo Settings → Secrets and variables → Actions → add `ANTHROPIC_API_KEY`
3. Actions tab → enable workflows (first-time confirmation)
4. `scripts/generate_insights.py` currently has a stubbed `fetch_corridors()` — wire in a real source and the cron starts producing insights

## Workflow
- Site edits: made locally, pushed via GitHub Desktop
- Data updates: pushed autonomously by the daily Action
