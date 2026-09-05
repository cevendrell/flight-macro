# OvrHead

An observatory over Aarhus, Denmark. One ADS-B receiver records every aircraft
that passes overhead; this repository holds the whole record and the static site
that reads it.

**Live:** https://cevendrell.github.io/flight-macro/

The long-term question is macroeconomic — what flight volume says about tourism,
business travel, migration and supply chains. The site does not pretend to
answer it yet. It publishes what a few days of one antenna can honestly support,
with the limits attached to every figure.

## The site

`index.html` is the entire application: tokens, styles and one ES module. No
build step, no backend, no analytics.

| Route | What it is |
| --- | --- |
| `#/` | Signals — headline counts and the detected claims, each with what it does *not* show |
| `#/week` | One full week, split by what kind of flying it is — freight, charter, business, scheduled |
| `#/globe` | Orthographic globe of registration countries, with corridor arcs |
| `#/explore` | Countries, airlines and aircraft types — sortable, filterable, exportable |
| `#/ask` | Build a question from dropdowns, or write SQL. Runs in your browser |
| `#/data` | Every file the site reads, and a reference for every column |
| `#/method` | What the record can and cannot show, including the receiver's real coverage footprint |
| `#/about` | Why it exists |

Entity pages hang off those: `#/place/SE`, `#/operator/SAS`, `#/type/A20N`,
`#/region/Nordics`, `#/continent/Europe`, `#/kind/cargo`. Individual signals are
linkable at `#/s/<id>`.

## What it can actually tell you

The record now holds a full week, which is the first thing it can honestly
describe. Over 29 August – 4 September 2026:

| | weekday | weekend day | difference |
| --- | ---: | ---: | ---: |
| All flights | 1,401 | 1,373 | **+2%** |
| Freight | 36 | 20 | **+80%** |
| Holiday charter | 18 | 26 | **−29%** |

Total traffic barely notices the working week. Underneath it, freight and
holidays run hard in opposite directions and cancel each other out in the
headline count. That separation is the whole point of the project, and it is only
possible because `data/adsb/carriers.json` says which operator is which kind —
freight, business jet, network, low-cost, charter, regional or state. 80% of
flights resolve to a classified operator; the rest are reported as unclassified,
never distributed to make the percentages tidy.

One week describes the shape of a week. It says nothing about whether that shape
is changing — that needs a second week, and the site says so until there is one.

## How it reads the data

Two layers, so the first paint never waits on the second:

1. **`data/adsb/summary.json`** (~50 KB) — precomputed totals, rollups, series
   and signals. Everything paints from this immediately.
2. **DuckDB-Wasm over `data/adsb/flights/*.parquet`** — loaded on demand when a
   page actually needs to query. Powers Ask, the per-entity evidence tables and
   the coverage plot. Nothing is sampled or rounded.

```
Sources                          Laptop                         Repo                Site
───────                          ──────                         ────                ────
Raspberry Pi (readsb/tar1090)
  1090 MHz ADS-B, on the LAN
        │  poller.py, every 15 s
        ▼
  snapshots/*.parquet  ──▶  reconstruct.py  ──▶  flights/*.parquet ──▶ git push ──▶ GitHub Pages
                            enrich.py            summary.json
                            build_summary.py     taxonomy.json
```

## Repository

```
index.html              the whole site
404.html                served for unknown paths
site.webmanifest        installable metadata
assets/                 social card + app icons  (python scripts/make_og.py)
data/adsb/
  summary.json          fast layer: rollups + signals
  taxonomy.json         ICAO address blocks → country → region
  carriers.json         callsign prefix → operator + kind of flying
  manifest.json         file list the Data page is built from
  land.json             coastline for the globe
  flights/*.parquet     one row per aircraft visit  ← the table Ask queries
  snapshots/*.parquet   raw 15-second observations
  enrichment/*.parquet  aircraft, airline and airport reference tables
scripts/adsb/           the live pipeline
scripts/                older Eurostat / OpenSky prototype, not feeding the site
pages/                  redirect stubs for pre-rebuild URLs
docs/                   architecture + laptop setup for the prototype pipeline
```

## Take the data

Everything is a static file, so you do not need this site to use the record:

```sql
INSTALL httpfs; LOAD httpfs;

SELECT ac_type, ac_desc, COUNT(*) AS flights
FROM read_parquet('https://cevendrell.github.io/flight-macro/data/adsb/flights/*.parquet')
GROUP BY 1, 2
ORDER BY 3 DESC;
```

Every table on the site also exports to CSV, and every signal links to the query
that produced it.

## Running it locally

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000/. A file:// URL will not work — the module and
the Parquet fetches need an origin.

## Pipeline

```bash
pip install -r scripts/requirements.txt
python scripts/adsb/reconstruct.py     # snapshots → flight sessions
python scripts/adsb/enrich.py          # registration / type / operator tables
python scripts/adsb/carriers.py        # carriers.json — operator + kind of flying
python scripts/adsb/build_summary.py   # summary.json + signal detection
```

See [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) for laptop setup and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the warehouse design of the
earlier prototype.

## Design

The LayOvr palette: navy dominant, amber as the action accent, burgundy reserved
for critical signals, periwinkle as the secondary voice on navy. Light theme on a
warm off-white ground; dark theme goes near-black with the navy kept only in the
pinned top bar. Three-state theme control in the header. Archivo and IBM Plex
Mono. Full token list in [CLAUDE.md](CLAUDE.md).

## Workflow

Site edits: locally, then push. Data updates: the laptop pipeline pushes to
`main` on its own; GitHub Pages redeploys either way.
