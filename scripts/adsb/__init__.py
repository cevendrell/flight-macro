"""
OvrHead ADS-B pipeline.

Poll a local tar1090/readsb instance on the LAN, store aircraft snapshots
to Parquet, reconstruct flights from snapshot sequences, enrich with
aircraft/airline/airport tables, and generate weekly signals.

Modules:
    poller.py       Continuous poller — runs forever, appends snapshots.
    reconstruct.py  Snapshots -> flights (per-hex sessions with gap detection).
    enrich.py       Downloads aircraft DB (tar1090-db) and airport DB (OurAirports).
    signals.py      Flights -> weekly aggregates -> insights.json.
    stats.py        Health check: rows, date range, top operators.
                    (name avoids clashing with the stdlib `inspect` module)
"""
