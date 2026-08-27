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

## Design tokens — refined finance-terminal palette
Warm midnight-navy ground, brass/teak (LayOvr family) as primary accent, sage
green for up trends, burnt sienna for down. Parchment text (not blue-white) for
a Bloomberg-terminal-under-a-brass-lamp feel.

- `--bg: #0a1119`   (deep midnight navy, slightly warm)
- `--bg-2: #0d1420`
- `--surface: #121a26`
- `--panel: #18202e`
- `--brass: #c9a86a`   (primary accent — matches LayOvr teak)
- `--brass-l: #dcc296`
- `--brass-d: #8f7748`
- `--up: #7fa88c`      (sage — ledger up, not neon)
- `--up-l: #a3c1ac`
- `--down: #b87857`    (burnt sienna — down)
- `--down-l: #cf9578`
- `--steel: #6b8caf`   (subtle aeronautical blue — secondary accent)
- `--steel-l: #9ab2cc`
- `--text: #e8dfd0`    (warm parchment)
- `--dim: #9a9484`
- `--mute: #5e5a4e`
- `--line: rgba(201,180,140,0.08)`   (warm hairlines, not white alpha)

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
