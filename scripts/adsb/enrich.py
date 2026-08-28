"""
Download / refresh enrichment tables. Idempotent; safe to re-run.

  aircraft_db.parquet  hex -> registration, type, description, operator
                       source: wiedehopf/tar1090-db (public, updated weekly)

  airports.parquet     icao/iata -> name, country, lat/lon
                       source: OurAirports (public CSV, updated weekly)

  airlines.parquet     ICAO 3-letter callsign prefix -> airline + country
                       source: bundled JSON below (curated top ~200)

Run:
    python scripts/adsb/enrich.py
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


# ── 1. Aircraft DB (tar1090-db) ─────────────────────────────────────────────

AIRCRAFT_URL = "https://github.com/wiedehopf/tar1090-db/raw/master/aircraft.csv.gz"


def refresh_aircraft() -> None:
    out = ENR / "aircraft_db.parquet"
    print(f"[aircraft] fetching {AIRCRAFT_URL}")
    raw = http_get(AIRCRAFT_URL)
    csv_bytes = gzip.decompress(raw)
    tbl = pv.read_csv(io.BytesIO(csv_bytes),
                      read_options=pv.ReadOptions(column_names=["hex","reg","type","flags","desc","ownop"]),
                      parse_options=pv.ParseOptions(delimiter=";"))
    # Normalize hex to lowercase for join compatibility with poller output
    hex_col = tbl.column("hex").to_pylist()
    tbl = tbl.set_column(0, "hex", pa.array([h.lower() if h else None for h in hex_col]))
    pq.write_table(tbl, out, compression="zstd")
    print(f"[aircraft] wrote {tbl.num_rows:,} rows -> {out.name}")


# ── 2. Airports (OurAirports) ───────────────────────────────────────────────

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"


def refresh_airports() -> None:
    out = ENR / "airports.parquet"
    print(f"[airports] fetching {AIRPORTS_URL}")
    raw = http_get(AIRPORTS_URL, timeout=90)
    tbl = pv.read_csv(io.BytesIO(raw))
    # Keep only what we need. Filter to types we care about (skip heliports/seaplane bases)
    keep = ["ident","type","name","latitude_deg","longitude_deg","iso_country","iata_code","icao_code","municipality"]
    tbl = tbl.select(keep)
    # Filter to substantial airports
    import pyarrow.compute as pc
    mask = pc.is_in(tbl.column("type"),
                    value_set=pa.array(["large_airport","medium_airport"]))
    tbl = tbl.filter(mask)
    pq.write_table(tbl, out, compression="zstd")
    print(f"[airports] wrote {tbl.num_rows:,} rows -> {out.name}")


# ── 3. Airline callsign prefixes ────────────────────────────────────────────
# ICAO 3-letter callsign prefix -> airline + country. Curated: top ~180 by
# global traffic. Add rows as new operators show up in the receiver feed.

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
    refresh_aircraft()
    refresh_airports()
    refresh_airlines()
    print(f"\ndone -> {ENR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
