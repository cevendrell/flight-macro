"""
Download / refresh enrichment tables. Idempotent; safe to re-run.

  aircraft_db.parquet  hex -> registration, type, description
                       source: wiedehopf/tar1090-db (82 gzip-JSON chunks)

  airports.parquet     icao/iata -> name, country, lat/lon
                       source: OurAirports (public CSV)

  airlines.parquet     ICAO 3-letter callsign prefix -> airline + country
                       source: bundled dict below (curated top ~180)

Run:
    python scripts/adsb/enrich.py

Each source runs in its own try/except so one failure doesn't kill the others.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.csv as pv
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
except ImportError:
    print("pip install pyarrow", file=sys.stderr)
    sys.exit(1)

WAREHOUSE = Path(os.environ.get("OVRHEAD_WAREHOUSE", str(Path.home() / "data" / "ovrhead-warehouse")))
ENR = WAREHOUSE / "adsb" / "enrichment"
ENR.mkdir(parents=True, exist_ok=True)


def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ovrhead-adsb-enrich/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── 1. Aircraft DB (tar1090-db, chunked format) ─────────────────────────────
# The repo restructured — instead of one aircraft.csv.gz there are now 82
# gzipped-JSON chunk files under db/<hex-prefix>.js. Each file's keys are
# the *remainder* after the prefix (so "00002" in file "6.js" = hex "600002").
# Each value: [reg, type_code, flags, description].
#
# Not every chunk is a dict — a few are metadata lists (author etc.); those
# get skipped rather than blowing up the whole run.

TAR1090_DB_LIST = "https://api.github.com/repos/wiedehopf/tar1090-db/contents/db"


def refresh_aircraft() -> None:
    out = ENR / "aircraft_db.parquet"
    print(f"[aircraft] listing tar1090-db chunks...")
    listing = json.loads(http_get(TAR1090_DB_LIST, timeout=30))
    chunks = [f for f in listing if f["name"].endswith(".js") and f.get("download_url")]
    print(f"[aircraft] fetching {len(chunks)} chunks (~5 MB total)...")

    rows = []
    skipped = 0
    for i, f in enumerate(chunks, 1):
        prefix = f["name"][:-3].upper()  # "6.js" -> "6", "3D.js" -> "3D"
        try:
            raw = http_get(f["download_url"], timeout=30)
        except Exception as e:
            print(f"  [aircraft] chunk {f['name']} download failed: {e}")
            skipped += 1
            continue
        try:
            payload = json.loads(gzip.decompress(raw).decode("utf-8"))
        except (gzip.BadGzipFile, OSError):
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                print(f"  [aircraft] chunk {f['name']} parse failed: {e}")
                skipped += 1
                continue

        if not isinstance(payload, dict):
            # Metadata/index files show up as lists — skip cleanly.
            skipped += 1
            continue

        for suffix, values in payload.items():
            if not isinstance(values, (list, tuple)):
                continue
            hex_full = (prefix + str(suffix)).lower()
            rows.append({
                "hex":   hex_full,
                "reg":   values[0] if len(values) > 0 else None,
                "type":  values[1] if len(values) > 1 else None,
                "flags": values[2] if len(values) > 2 else None,
                "desc":  values[3] if len(values) > 3 else None,
            })
        if i % 20 == 0 or i == len(chunks):
            print(f"  [aircraft] {i}/{len(chunks)} chunks processed, {len(rows):,} aircraft, {skipped} skipped")

    if not rows:
        raise RuntimeError("no aircraft rows produced")
    tbl = pa.Table.from_pylist(rows)
    pq.write_table(tbl, out, compression="zstd")
    print(f"[aircraft] wrote {tbl.num_rows:,} rows -> {out.name}")


# ── 2. Airports (OurAirports) ───────────────────────────────────────────────

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"


def refresh_airports() -> None:
    out = ENR / "airports.parquet"
    print(f"[airports] fetching {AIRPORTS_URL}")
    raw = http_get(AIRPORTS_URL, timeout=120)
    tbl = pv.read_csv(io.BytesIO(raw))
    keep = ["ident","type","name","latitude_deg","longitude_deg","iso_country","iata_code","icao_code","municipality"]
    tbl = tbl.select(keep)
    mask = pc.is_in(tbl.column("type"),
                    value_set=pa.array(["large_airport","medium_airport"]))
    tbl = tbl.filter(mask)
    pq.write_table(tbl, out, compression="zstd")
    print(f"[airports] wrote {tbl.num_rows:,} rows -> {out.name}")


# ── 3. Airline callsign prefixes (curated, offline) ─────────────────────────

AIRLINES = {
  "AAL":{"name":"American Airlines","country":"US"},"AAR":{"name":"Asiana Airlines","country":"KR"},
  "ACA":{"name":"Air Canada","country":"CA"},"AEA":{"name":"Air Europa","country":"ES"},
  "AEE":{"name":"Aegean Airlines","country":"GR"},"AFL":{"name":"Aeroflot","country":"RU"},
  "AFR":{"name":"Air France","country":"FR"},"AIC":{"name":"Air India","country":"IN"},
  "AJA":{"name":"AnadoluJet","country":"TR"},"ANA":{"name":"All Nippon Airways","country":"JP"},
  "ANE":{"name":"Air Nostrum","country":"ES"},"ASA":{"name":"Alaska Airlines","country":"US"},
  "AUA":{"name":"Austrian Airlines","country":"AT"},"AVA":{"name":"Avianca","country":"CO"},
  "AZA":{"name":"ITA Airways","country":"IT"},"BAW":{"name":"British Airways","country":"GB"},
  "BEE":{"name":"BA Cityflyer","country":"GB"},"BER":{"name":"Air Berlin","country":"DE"},
  "BLA":{"name":"Blue Air","country":"RO"},"BLX":{"name":"TUI fly Nordic","country":"SE"},
  "BOX":{"name":"AeroLogic","country":"DE"},"CAI":{"name":"Corendon","country":"NL"},
  "CAY":{"name":"Cayman Airways","country":"KY"},"CCA":{"name":"Air China","country":"CN"},
  "CES":{"name":"China Eastern","country":"CN"},"CFG":{"name":"Condor","country":"DE"},
  "CHH":{"name":"Hainan Airlines","country":"CN"},"CKS":{"name":"Kalitta Air","country":"US"},
  "CLH":{"name":"Lufthansa Cityline","country":"DE"},"CLX":{"name":"Cargolux","country":"LU"},
  "CPA":{"name":"Cathay Pacific","country":"HK"},"CQH":{"name":"Spring Airlines","country":"CN"},
  "CRL":{"name":"Corsair","country":"FR"},"CSN":{"name":"China Southern","country":"CN"},
  "CTN":{"name":"Croatia Airlines","country":"HR"},"CYP":{"name":"Cyprus Airways","country":"CY"},
  "DAL":{"name":"Delta Air Lines","country":"US"},"DHK":{"name":"DHL Air UK","country":"GB"},
  "DHL":{"name":"DHL Aviation","country":"BE"},"DLH":{"name":"Lufthansa","country":"DE"},
  "EIN":{"name":"Aer Lingus","country":"IE"},"ELY":{"name":"El Al","country":"IL"},
  "ETD":{"name":"Etihad","country":"AE"},"ETH":{"name":"Ethiopian Airlines","country":"ET"},
  "EWG":{"name":"Eurowings","country":"DE"},"EXS":{"name":"Jet2","country":"GB"},
  "EZS":{"name":"easyJet Switzerland","country":"CH"},"EZY":{"name":"easyJet","country":"GB"},
  "FDX":{"name":"FedEx","country":"US"},"FIN":{"name":"Finnair","country":"FI"},
  "FPO":{"name":"Europe Airpost","country":"FR"},"FR":{"name":"Ryanair (alt)","country":"IE"},
  "GEC":{"name":"Lufthansa Cargo","country":"DE"},"GLO":{"name":"Gol","country":"BR"},
  "HAL":{"name":"Hawaiian Airlines","country":"US"},"HDA":{"name":"Hong Kong Airlines","country":"HK"},
  "IBB":{"name":"Iberia Express","country":"ES"},"IBE":{"name":"Iberia","country":"ES"},
  "ICE":{"name":"Icelandair","country":"IS"},"IRA":{"name":"Iran Air","country":"IR"},
  "JAL":{"name":"Japan Airlines","country":"JP"},"JBU":{"name":"JetBlue","country":"US"},
  "KAL":{"name":"Korean Air","country":"KR"},"KLM":{"name":"KLM","country":"NL"},
  "LGL":{"name":"Luxair","country":"LU"},"LOT":{"name":"LOT Polish Airlines","country":"PL"},
  "LZB":{"name":"Bulgaria Air","country":"BG"},"MAS":{"name":"Malaysia Airlines","country":"MY"},
  "MAU":{"name":"Air Mauritius","country":"MU"},"MEA":{"name":"Middle East Airlines","country":"LB"},
  "MSR":{"name":"EgyptAir","country":"EG"},"NAX":{"name":"Norwegian Air","country":"NO"},
  "NCA":{"name":"Nippon Cargo","country":"JP"},"NPT":{"name":"West Atlantic UK","country":"GB"},
  "NSZ":{"name":"Norwegian Air Sweden","country":"SE"},
  "PAL":{"name":"Philippine Airlines","country":"PH"},"PGT":{"name":"Pegasus","country":"TR"},
  "QFA":{"name":"Qantas","country":"AU"},"QTR":{"name":"Qatar Airways","country":"QA"},
  "RAM":{"name":"Royal Air Maroc","country":"MA"},"RJA":{"name":"Royal Jordanian","country":"JO"},
  "ROT":{"name":"TAROM","country":"RO"},"RYR":{"name":"Ryanair","country":"IE"},
  "SAA":{"name":"South African Airways","country":"ZA"},"SAS":{"name":"SAS","country":"SE"},
  "SIA":{"name":"Singapore Airlines","country":"SG"},"SKV":{"name":"Skyward Express","country":"KE"},
  "SQC":{"name":"Singapore Airlines Cargo","country":"SG"},"SVA":{"name":"Saudia","country":"SA"},
  "SWA":{"name":"Southwest","country":"US"},"SWR":{"name":"Swiss","country":"CH"},
  "TAP":{"name":"TAP Portugal","country":"PT"},"TAR":{"name":"Tunisair","country":"TN"},
  "THA":{"name":"Thai Airways","country":"TH"},"THY":{"name":"Turkish Airlines","country":"TR"},
  "TOM":{"name":"TUI Airways","country":"GB"},"TRA":{"name":"Transavia","country":"NL"},
  "TSC":{"name":"Air Transat","country":"CA"},"TUI":{"name":"TUIfly","country":"DE"},
  "TVF":{"name":"Transavia France","country":"FR"},"TVS":{"name":"Smartwings","country":"CZ"},
  "UAE":{"name":"Emirates","country":"AE"},"UAL":{"name":"United Airlines","country":"US"},
  "UPS":{"name":"UPS","country":"US"},"UZB":{"name":"Uzbekistan Airways","country":"UZ"},
  "VDA":{"name":"Volga-Dnepr","country":"RU"},"VIR":{"name":"Virgin Atlantic","country":"GB"},
  "VLG":{"name":"Vueling","country":"ES"},"VOI":{"name":"Volotea","country":"ES"},
  "WIF":{"name":"Widerøe","country":"NO"},"WJA":{"name":"WestJet","country":"CA"},
  "WMT":{"name":"Wamos Air","country":"ES"},"WZZ":{"name":"Wizz Air","country":"HU"},
}


def refresh_airlines() -> None:
    out = ENR / "airlines.parquet"
    rows = [{"prefix": k, "name": v["name"], "country": v["country"]} for k, v in AIRLINES.items()]
    tbl = pa.Table.from_pylist(rows)
    pq.write_table(tbl, out, compression="zstd")
    print(f"[airlines] wrote {tbl.num_rows} rows -> {out.name}")


def main() -> int:
    failures = 0
    for name, fn in [("aircraft", refresh_aircraft),
                     ("airports", refresh_airports),
                     ("airlines", refresh_airlines)]:
        try:
            fn()
        except Exception as e:
            failures += 1
            print(f"[{name}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"\ndone -> {ENR}   ({failures} failure{'s' if failures != 1 else ''})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
