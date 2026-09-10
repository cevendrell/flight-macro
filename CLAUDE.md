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
  Hash routes: `#/` signals · `#/week` · `#/globe` · `#/explore` · `#/ask`
  · `#/data` · `#/method` · `#/about` · `#/place/<cc>` · `#/region/<name>`
  · `#/continent/<name>` · `#/operator/<icao>` · `#/type/<icao>`
  · `#/kind/<kind>` · `#/s/<signal-id>`
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

### Carrier taxonomy — what makes the macro readings possible
`data/adsb/carriers.json` (written by `scripts/adsb/carriers.py`) maps an ICAO
callsign prefix to an operator name and a **kind**: `cargo`, `bizjet`, `network`,
`lowcost`, `leisure`, `regional`, `state`. Without it a flight count says nothing
about the economy — freight, a holiday charter and a Monday business shuttle are
the same row. With it, `#/week` can separate them, and it does:

> Traffic over Aarhus is 2% heavier on a weekday than at the weekend — almost
> nothing. Underneath that flat total, freight runs +80% on weekdays and holiday
> charter runs −29%. The composition is the signal; the count is not.

Rules for editing it:
- **Only list a carrier whose identity is not in doubt.** Guessing at a prefix to
  raise coverage is the one change that makes everything downstream worthless.
- Hybrids go under the model they mostly fly (airBaltic, Eurowings → `lowcost`).
- Everything unlisted stays unclassified, and the site reports the share.
- Cargo is attributed by **operator**, never by airframe.

Both the pipeline and the browser read this same file, so a figure on the front
page and a query in Ask cannot disagree.

### The /week page is a React + Framer Motion island
Everything else on the site is vanilla ES modules. `/week` is the exception: it
mounts a React app with Framer Motion for the entrance choreography, the number
counters, and the diverging-bar chart. Both are lazy-loaded from esm.sh the
first time /week is opened — no build step, no NPM.

Rules that keep this from breaking:
- `esm.sh` serves each package with its own React bundle unless you tell it
  otherwise. Every non-react URL in `CDN` has `?deps=react@18` (react-dom also
  needs `?deps=react-dom@18`). Without those, hooks throw "Cannot read
  properties of null (reading 'useState')" — the classic two-Reacts error.
- The mount slot is `<div id="week-mount">`; the fallback is `<div
  id="week-fallback">` right beside it, painted with `viewWeekStatic(w)` and
  revealed if React hasn't landed after 800 ms.
- `unmountWeek()` runs from `route()` whenever the reader leaves /week, so no
  React root outlives its DOM. The vanilla view then owns the container.
- The evidence table and caveats at the foot of /week stay vanilla — they use
  the site's own `table()` (sort/filter/CSV), and are injected into the React
  tree via `dangerouslySetInnerHTML` rather than reimplemented.
- Framer's `MotionConfig reducedMotion="user"` honours the OS setting. Do not
  add per-component reduced-motion checks; let the config handle it.

Any other page can stay vanilla. React should only enter where the motion
carries meaning — a comparison being animated, a number being computed, an
order being read. Sprinkling motion.div in place of div is churn.

### Two traps worth knowing
**No ICU in duckdb-wasm**, so `strftime()` cannot bind against a
`TIMESTAMP WITH TIME ZONE`. `seen_at` is therefore built with
`make_timestamp(first_seen * 1000000)` — a naive timestamp already in UTC.
Any SQL published to the browser (including the `sql=` strings
`scripts/adsb/build_summary.py` bakes into each signal) must use `seen_at`, never
`TO_TIMESTAMP(first_seen)`. This is easy to reintroduce and fails only in the
browser, never in a local DuckDB.

**Wide-body matching is `A310`, not `A31`.** `A31` also catches the A318 and A319,
which are narrow-bodies. It did for months and inflated the wide-body count — the
site's long-haul proxy — by about a sixth. The prefix list lives in
`WIDEBODY_PREFIXES` and in the browser's view regex; keep them in step.

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
- A week is the unit. A Tuesday and a Sunday are not two samples of the same
  thing; `#/week` only appears once seven *consecutive complete* days exist, and
  it says plainly that composition is not trend.
- Where a figure rests on a small sample, show it — hatched bars and a
  "small sample" label — rather than dropping the row.
- Missing values render as missing (`—`, or a note saying why), never as zero.
- Anything that awaits — the query engine, a fetch — must compare `nav` against
  the value it captured before painting, or a slow page will overwrite a newer one.

## Data pipeline
- **Source**: a Raspberry Pi running readsb/tar1090 on the LAN, polled every
  15 s by `scripts/adsb/poller.py` into `snapshots/*.parquet`.
- **Nightly**: `routes.py` fetches the VRS standing-data callsign→route tables
  for the airline prefixes in the record (cached a week in the warehouse) and
  writes `enrichment/routes.parquet` for the callsigns heard → `reconstruct.py`
  sessionises snapshots into `flights/*.parquet` (30-minute gap starts a new
  flight), joins each callsign's route and keeps it only when the aircraft's
  observed heading agrees with the bearing to the destination (`route_check`:
  verified / unverified / conflict; a conflict clears origin/destination and
  keeps the rejected route in `route_conflict`) → `build_summary.py` writes
  `summary.json` — including the `routes` block the site is built around —
  and detects signals → `sync_to_repo.py` commits → GitHub Pages redeploys.
- **Routes are the primary lens; registration is the secondary one.** Origin
  and destination say where a flight is between. Registration country says
  where the airframe lives. Signals, Explore, the Globe and the entity pages
  lead with routes and keep registration as an add-on, in that order.
- **No approval layer** — generated readings ship straight to the site, which is
  why the confidence field and the caveat field are not optional.
- **Never hand-merge the derived JSON.** `summary.json`, `taxonomy.json`,
  `carriers.json` and `manifest.json` are written by the scripts and rewritten
  on both this machine and the collector laptop, so they conflict on nearly
  every merge — and both sides are stale the moment they disagree. Take either
  side to clear the conflict, then rerun `taxonomy.py` and `build_summary.py`:
  the data is the Parquet, and these files are only a projection of it.
  Resolving one by hand once left `summary.json` a 299-byte stub, and the site
  failed to boot with `S.countries` undefined.

## Secrets
`ANTHROPIC_API_KEY` belongs in GitHub Actions secrets, only if the (currently
disabled) `update-insights.yml` fallback is ever re-enabled. Never commit a key.

## Workflow
User commits and pushes via GitHub Desktop → GitHub Pages auto-deploys via
`.github/workflows/deploy-pages.yml`. The laptop pipeline also pushes to `main`
autonomously (data updates only).
