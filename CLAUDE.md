# Claude Code Instructions

## Working directory
Always edit files directly in this repository — never use a worktree or subdirectory.
The repo root is the working directory for all edits.

## Project purpose
A macroeconomic dashboard that reads flight-route data as a proxy for large-scale
economic shifts (purchasing power, tourism flows, business travel, migration,
supply-chain rerouting). Not a flight-tracking site — the flight numbers are the
lens, the story is macro.

Example insight: "Flights from China → Sweden up 4% YoY in July 2026, likely
driven by rising Chinese middle-class purchasing power feeding Lapland tourism."

## Site structure
Static HTML site. No build step for the site itself.
- `index.html` — main map view + insight feed
- `data/insights.json` — signal records consumed by the map
- `pages/about/` — project intro
- `pages/methodology/` — how flight data maps to macro signals
- `scripts/` — Python pipeline (fetch → detect anomalies → call Claude → write JSON)
- `.github/workflows/update-insights.yml` — daily cron that runs the pipeline
  and commits `data/insights.json` back to `main`

## Map library
Leaflet loaded from CDN (unpkg). Dark basemap tiles from CARTO.

## Filter model
Insights are filtered by:
- **Granularity**: continent / country / city
- **Time comparison**: month vs same month last year (default), YoY, QoQ
- **Theme**: tourism, business, migration, supply chain

## Design tokens — aeronautical chart palette
- `--bg: #050912` (deep navy, near-black)
- `--bg-2: #080e1a`
- `--surface: #0b1220`
- `--panel: #101a2e`
- `--cyan: #5eead4` (primary accent, "up" trend — pale cockpit teal)
- `--cyan-l: #99f6e4`
- `--amber: #f59e0b` (secondary accent, "down" trend — warning amber)
- `--amber-l: #fbbf24`
- `--text: #e8ecf5`
- `--dim: #8a97b3`
- `--mute: #4a5878`
- `--line: rgba(255,255,255,0.07)`

Fonts: Inter (UI), JetBrains Mono (numbers, eyebrows, coordinates)

## Data pipeline
- **Sources**: OpenSky Network (global ADS-B history), BTS T-100 (US DOT monthly
  route-level), Eurocontrol (European route stats). All free.
- **Cadence**: daily GitHub Actions cron.
- **Steps**: fetch → normalize into corridor table → compute YoY delta →
  filter by volume floor → rank by `|delta%| × log(volume)` → take top ~25 →
  enrich with Claude API to produce headline + reading + confidence → write
  `data/insights.json` → commit → GitHub Pages redeploys.
- **No approval layer yet** — auto-generated readings ship straight to the site.
  Every card carries a `confidence` field (high/medium/low).

## Secrets
Set `ANTHROPIC_API_KEY` in the repo's GitHub Actions secrets before enabling
the workflow. Never commit the key.

## Workflow
User commits and pushes via GitHub Desktop → GitHub Pages auto-deploys.
The Actions cron also pushes to `main` autonomously (data updates only).
