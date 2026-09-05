# Claude Code Instructions

## Working directory
Always edit files directly in this repository — never use a worktree or subdirectory.
The repo root is the working directory for all edits.

## Project purpose
Long-term ambition: read flight data as a proxy for large-scale economic shifts
(purchasing power, tourism flows, business travel, migration, supply-chain
rerouting). Not a flight-tracking site — the flight numbers are the lens.

**Where it actually is today.** The site is an *observatory*, not a macro
dashboard. It publishes what one ADS-B receiver in Aarhus, Denmark can honestly
support: counts, composition, rhythm and coverage, each with its limits stated.
Every card carries a `confidence` field (`observed` / `early` / `moderate` /
`strong`), and with only days of history almost nothing rises above `early`.
The Method page says out loud that aircraft counts are an input to economic
questions, not an answer to them. Keep it that way: inventing trends the record
cannot support is the fastest way to make this untrustworthy.

## Site structure
Static HTML. No build step — `index.html` is the entire application.
- `index.html` — single-page app: tokens, styles and an ES module, in one file.
  Hash routes: `#/` signals · `#/globe` · `#/explore` · `#/ask` · `#/data`
  · `#/method` · `#/about` · `#/place/<cc>` · `#/region/<name>`
  · `#/continent/<name>` · `#/operator/<icao>` · `#/type/<icao>` · `#/s/<signal-id>`
- `404.html`, `site.webmanifest`, `assets/` — social card and app icons
  (regenerate with `python scripts/make_og.py`)
- `pages/about/`, `pages/methodology/` — redirect stubs only. The real pages are
  `#/about` and `#/method`. Do not revive them as standalone pages.
- `data/adsb/` — everything the site reads (see below)
- `data/insights.json` — legacy, from the Eurostat prototype. **Not read by the
  site.** `scripts/generate_insights.py` still writes it.
- `scripts/adsb/` — the live pipeline. `scripts/` root — the older Eurostat /
  OpenSky prototype, kept but not feeding the site.

## Two data layers
1. **Fast layer** — `data/adsb/summary.json` (~50 KB): totals, entity rollups,
   daily and hourly series, and the detected signals. Every first paint comes
   from this, plus `taxonomy.json` and `manifest.json`.
2. **Deep layer** — DuckDB-Wasm, loaded on demand from jsDelivr, over
   `data/adsb/flights/*.parquet`. Powers Ask, the per-entity evidence tables
   and rhythm charts, and the Method coverage plot. `aircraft_db.parquet`
   (4.7 MB) is deliberately never loaded: type, registration and description
   are already denormalised onto each flight row.

The browser builds a `flights` view over the Parquet with derived columns
(`reg_country`, `reg_region`, `reg_continent`, `body`, `is_cargo`, `seen_at`).
The Data page documents all of them — keep `COLUMN_REF` in `index.html` in step
with the view.

### One trap worth knowing
duckdb-wasm ships **without ICU**, so `strftime()` cannot bind against a
`TIMESTAMP WITH TIME ZONE`. `seen_at` is therefore built with
`make_timestamp(first_seen * 1000000)` — a naive timestamp already in UTC.
Any SQL published to the browser (including the `sql=` strings
`scripts/adsb/build_summary.py` bakes into each signal) must use `seen_at`, never
`TO_TIMESTAMP(first_seen)`. This is easy to reintroduce and fails only in the
browser, never in a local DuckDB.

## Graphics
No mapping library. Two hand-rolled canvases:
- `#/globe` — orthographic projection with great-circle corridor arcs, drawn
  from `data/adsb/land.json`. Labels are real DOM so they stay crisp and
  clickable, placed greedily by traffic with collision rejection.
- `#/method` — azimuthal-equidistant coverage plot: every first-contact
  position by true bearing and ground distance, plus the 95th-percentile
  reception envelope per 5° sector.

Both read CSS tokens at draw time, so they follow the theme.

## Design tokens — the LayOvr palette
Navy dominant, amber as the warm action accent, burgundy reserved strictly for
negative/critical signals, periwinkle as the secondary voice on navy. The light
theme is LayOvr's warm off-white ground. The dark theme is OvrHead's own: the
page goes near-black and navy stays only in the pinned top bar.

Light (`:root`):
- `--bg: #f5f3f0` · `--surface: #ffffff` · `--sunken: #efece7`
- `--ink: #05164d` · `--ink-2: #777586` · `--mute: #8e8b9e` · `--faint: #aca9bb`
- `--rule: #e2dfe9` · `--rule-2: #cdc8da`
- `--accent: #8a5a33` (amber — action/CTA) · `--accent-bg: rgba(138,90,51,.09)`
- `--rise: #2e875e` · `--fall: #7d2b2d` (burgundy — critical only)
- `--topbar: #05164d` (pinned in **both** themes) · `--on-navy: #eef1f8`
  · `--on-navy-2: #82a5d6`

Dark (`@media (prefers-color-scheme: dark)` and `:root[data-theme="dark"]`):
- `--bg: #0c0c0e` · `--surface: #191919` · `--sunken: #060606`
- `--ink: #eef1f8` · `--accent: #d2955c` · `--rise: #5cc191` · `--fall: #d97b73`

Theme has three states — light, dark, and system — chosen in the top bar and
stored under `localStorage['ovrhead.theme']`; a small inline script in `<head>`
applies it before first paint. Never define a colour only inside a media query.

Fonts: Archivo (UI) and IBM Plex Mono (figures, eyebrows, codes), from Google
Fonts. Two radii only: `--r-control: 7px`, `--r-card: 11px`.

## House rules for the interface
- Tables own their state and repaint in place. Never route a column sort through
  the router — it scrolls the reader away and re-runs the query behind the table.
- `sortDir` is `1` ascending, `-1` descending, and the header arrow must always
  agree with the order on screen. Blanks sort last in both directions.
- Partial days are hatched and excluded from comparisons, everywhere.
- Missing values render as missing (`—`, or a note saying why), never as zero.
- Anything that awaits — the query engine, a fetch — must compare `nav` against
  the value it captured before painting, or a slow page will overwrite a newer one.

## Data pipeline
- **Source**: a Raspberry Pi running readsb/tar1090 on the LAN, polled every
  15 s by `scripts/adsb/poller.py` into `snapshots/*.parquet`.
- **Nightly**: `reconstruct.py` sessionises snapshots into `flights/*.parquet`
  (30-minute gap starts a new flight) → `enrich.py` adds registration, type and
  operator → `build_summary.py` writes `summary.json` and detects signals →
  `sync_to_repo.py` commits → GitHub Pages redeploys.
- **No approval layer** — generated readings ship straight to the site, which is
  why the confidence field and the caveat field are not optional.

## Secrets
`ANTHROPIC_API_KEY` belongs in GitHub Actions secrets, only if the (currently
disabled) `update-insights.yml` fallback is ever re-enabled. Never commit a key.

## Workflow
User commits and pushes via GitHub Desktop → GitHub Pages auto-deploys via
`.github/workflows/deploy-pages.yml`. The laptop pipeline also pushes to `main`
autonomously (data updates only).
