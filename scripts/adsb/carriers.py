"""
Who is flying, and what kind of flying is it.

The flight table knows an ICAO callsign prefix. That is enough to count
aeroplanes and not nearly enough to say anything about the economy: a freighter,
a holiday charter and a Monday-morning business shuttle are the same row until
somebody says which is which.

This file is that somebody. It writes data/adsb/carriers.json, which both the
site and build_summary.py read to turn a three-letter prefix into an operator
name and a *kind*. The kinds are what make the macro readings possible at all:

    cargo    → freight, and therefore trade
    bizjet   → corporate travel at its most discretionary
    network  → full-service scheduled: business routes and connecting traffic
    lowcost  → price-led point-to-point: leisure and visiting-friends-and-family
    leisure  → charter and tour-operator flying: holidays, almost purely
    regional → short feeders, mostly domestic
    state    → military, government, police, air ambulance — not economic demand

It is a judgement, not a measurement, and it is published as a file so the
judgement can be argued with. Two rules keep it honest:

  1. A carrier is only listed when its identity is not in doubt. Guessing at a
     prefix to raise the coverage number would be the one change that makes
     every reading downstream untrustworthy.
  2. Hybrids are filed under the model they mostly fly. airBaltic and Eurowings
     sit in `lowcost` for that reason, and reasonable people would move them.

Everything unlisted stays unclassified and is reported as such, so the reader
can see how much of the sky this covers.

    python scripts/adsb/carriers.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "data" / "adsb" / "carriers.json"

KINDS = {
    "cargo":    {"label": "All-cargo",      "reads": "Freight capacity — the closest thing overhead to a trade figure."},
    "bizjet":   {"label": "Business jet",   "reads": "Corporate and private travel. The first budget cut in a downturn."},
    "network":  {"label": "Network",        "reads": "Full-service scheduled. Carries business travel and connecting traffic."},
    "lowcost":  {"label": "Low-cost",       "reads": "Price-led point-to-point. Leisure and visiting family."},
    "leisure":  {"label": "Charter",        "reads": "Tour-operator flying. Holiday demand, almost undiluted."},
    "regional": {"label": "Regional",       "reads": "Short feeders and domestic links. Tracks local activity."},
    "state":    {"label": "State",          "reads": "Military, government, police, air ambulance. Not economic demand."},
}

# prefix: (name, kind, registration country of the operator)
CARRIERS: dict[str, tuple[str, str, str]] = {
    # ── All-cargo ────────────────────────────────────────────────────────
    "CAO": ("Air China Cargo",            "cargo", "CN"),
    "CKK": ("China Cargo Airlines",       "cargo", "CN"),
    "YZR": ("Yangtze River Express",      "cargo", "CN"),
    "TAY": ("ASL Airlines Belgium",       "cargo", "BE"),
    "BCS": ("European Air Transport",     "cargo", "BE"),   # DHL's Leipzig airline
    "UPS": ("UPS Airlines",               "cargo", "US"),
    "FDX": ("FedEx Express",              "cargo", "US"),
    "GTI": ("Atlas Air",                  "cargo", "US"),
    "CKS": ("Kalitta Air",                "cargo", "US"),
    "CLX": ("Cargolux",                   "cargo", "LU"),
    "GEC": ("Lufthansa Cargo",            "cargo", "DE"),
    "BOX": ("AeroLogic",                  "cargo", "DE"),
    "ABW": ("AirBridgeCargo",             "cargo", "RU"),
    "SQC": ("Singapore Airlines Cargo",   "cargo", "SG"),
    "MPH": ("Martinair",                  "cargo", "NL"),
    "ICV": ("Silk Way West Airlines",     "cargo", "AZ"),
    "RCF": ("Aerotranscargo",             "cargo", "MD"),

    # ── Business aviation ────────────────────────────────────────────────
    "NJE": ("NetJets Europe",             "bizjet", "PT"),
    "VJT": ("VistaJet",                   "bizjet", "MT"),

    # ── Network / full-service scheduled ─────────────────────────────────
    "SAS": ("SAS",                        "network", "DK"),
    "FIN": ("Finnair",                    "network", "FI"),
    "DLH": ("Lufthansa",                  "network", "DE"),
    "KLM": ("KLM",                        "network", "NL"),
    "AFR": ("Air France",                 "network", "FR"),
    "BAW": ("British Airways",            "network", "GB"),
    "SWR": ("Swiss",                      "network", "CH"),
    "AUA": ("Austrian Airlines",          "network", "AT"),
    "BEL": ("Brussels Airlines",          "network", "BE"),
    "IBE": ("Iberia",                     "network", "ES"),
    "TAP": ("TAP Air Portugal",           "network", "PT"),
    "LOT": ("LOT Polish Airlines",        "network", "PL"),
    "AEE": ("Aegean Airlines",            "network", "GR"),
    "THY": ("Turkish Airlines",           "network", "TR"),
    "ICE": ("Icelandair",                 "network", "IS"),
    "LGL": ("Luxair",                     "network", "LU"),
    "GRL": ("Air Greenland",              "network", "GL"),
    "UAE": ("Emirates",                   "network", "AE"),
    "ETD": ("Etihad Airways",             "network", "AE"),
    "QTR": ("Qatar Airways",              "network", "QA"),
    "SVA": ("Saudia",                     "network", "SA"),
    "RJA": ("Royal Jordanian",            "network", "JO"),
    "ELY": ("El Al",                      "network", "IL"),
    "MSR": ("EgyptAir",                   "network", "EG"),
    "ETH": ("Ethiopian Airlines",         "network", "ET"),
    "CCA": ("Air China",                  "network", "CN"),
    "CES": ("China Eastern Airlines",     "network", "CN"),
    "CSN": ("China Southern Airlines",    "network", "CN"),
    "CHH": ("Hainan Airlines",            "network", "CN"),
    "CXA": ("Xiamen Air",                 "network", "CN"),
    "CSZ": ("Shenzhen Airlines",          "network", "CN"),
    "CSC": ("Sichuan Airlines",           "network", "CN"),
    "AAL": ("American Airlines",          "network", "US"),
    "DAL": ("Delta Air Lines",            "network", "US"),
    "UAL": ("United Airlines",            "network", "US"),
    "ACA": ("Air Canada",                 "network", "CA"),
    "AAR": ("Asiana Airlines",            "network", "KR"),
    "KAL": ("Korean Air",                 "network", "KR"),
    "ANA": ("All Nippon Airways",         "network", "JP"),
    "JAL": ("Japan Airlines",             "network", "JP"),
    "SIA": ("Singapore Airlines",         "network", "SG"),
    "AIC": ("Air India",                  "network", "IN"),
    "PIA": ("Pakistan International",     "network", "PK"),
    "AEA": ("Air Europa",                 "network", "ES"),

    # ── Low-cost ─────────────────────────────────────────────────────────
    "RYR": ("Ryanair",                    "lowcost", "IE"),
    "NSZ": ("Norwegian Air Sweden",       "lowcost", "SE"),
    "NOZ": ("Norwegian Air Norway",       "lowcost", "NO"),
    "NAX": ("Norwegian Air Shuttle",      "lowcost", "NO"),
    "WZZ": ("Wizz Air",                   "lowcost", "HU"),
    "WUK": ("Wizz Air UK",                "lowcost", "GB"),
    "EZY": ("easyJet",                    "lowcost", "GB"),
    "EZS": ("easyJet Switzerland",        "lowcost", "CH"),
    "EJU": ("easyJet Europe",             "lowcost", "AT"),
    "EWG": ("Eurowings",                  "lowcost", "DE"),   # hybrid, flown as low-cost
    "BTI": ("airBaltic",                  "lowcost", "LV"),   # hybrid, flown as low-cost
    "PGT": ("Pegasus Airlines",           "lowcost", "TR"),
    "TVF": ("Transavia France",           "lowcost", "FR"),
    "TRA": ("Transavia",                  "lowcost", "NL"),
    "VLG": ("Vueling",                    "lowcost", "ES"),
    "VOE": ("Volotea",                    "lowcost", "ES"),
    "WJA": ("WestJet",                    "lowcost", "CA"),

    # ── Charter / tour operator ──────────────────────────────────────────
    "VKG": ("Sunclass Airlines",          "leisure", "DK"),
    "BLX": ("TUI fly Nordic",             "leisure", "SE"),
    "TFL": ("TUI fly Netherlands",        "leisure", "NL"),
    "TUI": ("TUI Airways",                "leisure", "GB"),
    "ENT": ("Enter Air",                  "leisure", "PL"),
    "WMT": ("Wamos Air",                  "leisure", "ES"),
    "CAI": ("Corendon Dutch Airlines",    "leisure", "NL"),

    # ── Regional / feeder ────────────────────────────────────────────────
    "KLC": ("KLM Cityhopper",             "regional", "NL"),
    "CLH": ("Lufthansa CityLine",         "regional", "DE"),
    "FLI": ("Atlantic Airways",           "regional", "FO"),
    "MMD": ("Alsie Express",              "regional", "DK"),
    "WIF": ("Widerøe",                    "regional", "NO"),

    # ── State, military, medical ─────────────────────────────────────────
    "GAF": ("German Air Force",           "state", "DE"),
    "RRR": ("Royal Air Force",            "state", "GB"),
    "DOC": ("Norwegian Air Ambulance",    "state", "NO"),
}


def build() -> dict:
    return {
        "note": "ICAO callsign prefix -> operator and the kind of flying it does. "
                "Editorial, deliberately incomplete: a carrier appears only when "
                "its identity is not in doubt. Unlisted prefixes stay "
                "unclassified and the site reports how much of the record that is.",
        "kinds": KINDS,
        "carriers": {p: {"name": n, "kind": k, "country": c}
                     for p, (n, k, c) in sorted(CARRIERS.items())},
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), separators=(",", ":"), ensure_ascii=False),
                   encoding="utf-8")
    by_kind: dict[str, int] = {}
    for _, k, _ in CARRIERS.values():
        by_kind[k] = by_kind.get(k, 0) + 1
    print(f"[carriers] {len(CARRIERS)} operators -> {OUT.relative_to(REPO)} "
          f"({OUT.stat().st_size / 1024:.1f} KB)")
    for k in KINDS:
        print(f"  {k:9} {by_kind.get(k, 0)}")
