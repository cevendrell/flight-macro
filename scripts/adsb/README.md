# OvrHead ADS-B pipeline

Pulls flight observations from a home ADS-B receiver on the LAN
(dump1090 / readsb / tar1090), stores them locally, reconstructs
flights, enriches with aircraft / operator / airport tables, and
generates signals.

## Architecture

```
Pi (readsb + tar1090)
    ├── serves aircraft.json on the LAN
    │
    ▼
Matebook  scripts/adsb/poller.py       runs forever (Task Scheduler @ startup)
    ├── polls every 30s
    ├── writes to snapshots/snapshots_YYYY-MM-DD.parquet   (~4 MB/day)
    │
    ▼
scripts/adsb/reconstruct.py            runs daily via cron / Task Scheduler
    ├── sessionizes snapshots by hex + 30-min gap
    ├── writes flights/flights_YYYY-MM.parquet
    │
    ▼
scripts/adsb/signals.py                runs daily after reconstruct
    ├── weekly aggregates by operator, aircraft type, direction
    ├── writes ../../data/insights.json
    │
    ▼
git push  →  GitHub Pages redeploys
```

## Setup (one-off)

```powershell
# from repo root, in the venv
python scripts\adsb\enrich.py           # download tar1090-db + OurAirports (~30s)
```

## Running

**Start the poller** (leave running; every 30s it appends to today's file):
```powershell
python scripts\adsb\poller.py
```
Or, to run in the background so it survives closing PowerShell:
```powershell
Start-Process pythonw.exe -ArgumentList "scripts\adsb\poller.py" -WindowStyle Hidden
```

**Check what's captured**:
```powershell
python scripts\adsb\stats.py
```

**Reconstruct flights from all snapshots so far**:
```powershell
python scripts\adsb\reconstruct.py
```

## Config

Environment variables (all optional, sensible defaults):

- `OVRHEAD_ADSB_URL`  — the aircraft.json endpoint on your Pi.
  Default: `http://192.168.1.98/tar1090/data/aircraft.json`
- `OVRHEAD_POLL_SEC`  — poll interval in seconds. Default: 30.
- `OVRHEAD_WAREHOUSE` — root of local storage. Default: `~/data/ovrhead-warehouse`.

## Storage layout

```
~/data/ovrhead-warehouse/
  adsb/
    snapshots/
      snapshots_2026-08-29.parquet
      snapshots_2026-08-30.parquet
      ...
    flights/
      flights_2026-08.parquet
    enrichment/
      aircraft_db.parquet
      airports.parquet
      airlines.parquet
    adsb.log
```

## What the signals will use

Once flights accumulate for a week+, `signals.py` (TODO) will emit:

- Operator country mix — Chinese carriers over Aarhus this week vs last
- Aircraft category — widebody share (proxy for long-haul intensity)
- Freighter share — FDX/UPS/CLX/CKS callsign presence
- Transit corridors — flights where first/last position are near known
  hub airports on opposite sides of the antenna's range
- Absence signals — carriers that used to appear and no longer do

Each becomes a thesis card with the same shape the site already renders.

## Why local storage

- No API can shut you down. Your receiver, your data.
- Full history, unlimited retention (only bounded by your SSD).
- Standard Parquet — readable by DuckDB, pandas, Polars, anything.
