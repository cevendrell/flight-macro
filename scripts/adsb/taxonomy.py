"""
Geographic taxonomy for OvrHead.

Every aircraft carries a 24-bit ICAO address (the `hex` field). Those addresses
are allocated to states in fixed blocks by ICAO Annex 10. That makes the hex
address the single most reliable country attribution we have: it is present on
100% of observations, it cannot be spoofed by a missing callsign, and it does
not depend on the operator being in our airline lookup.

Registration prefixes (OY-, D-, B-, N-) corroborate the same fact and are used
as a fallback when a hex address falls outside a known block.

The output is a single JSON file consumed by BOTH the summary builder and the
website, so the taxonomy is data-driven rather than duplicated in each layer.

    country -> economic region -> continent

Run:
    python scripts/adsb/taxonomy.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "data" / "adsb" / "taxonomy.json"

# ── ICAO 24-bit address blocks ───────────────────────────────────────────────
# (start_hex, end_hex, ISO-3166 alpha-2)
# Source: ICAO Annex 10 Vol III, Appendix to Chapter 9.
ICAO_BLOCKS: list[tuple[str, str, str]] = [
    # ── Europe: the contiguous 0x38-0x50 allocations ────────────────────────
    ("300000", "33FFFF", "IT"),  # Italy
    ("340000", "37FFFF", "ES"),  # Spain
    ("380000", "3BFFFF", "FR"),  # France
    ("3C0000", "3FFFFF", "DE"),  # Germany
    ("400000", "43FFFF", "GB"),  # United Kingdom
    ("440000", "447FFF", "AT"),  # Austria
    ("448000", "44FFFF", "BE"),  # Belgium
    ("450000", "457FFF", "BG"),  # Bulgaria
    ("458000", "45FFFF", "DK"),  # Denmark
    ("460000", "467FFF", "FI"),  # Finland
    ("468000", "46FFFF", "GR"),  # Greece
    ("470000", "477FFF", "HU"),  # Hungary
    ("478000", "47FFFF", "NO"),  # Norway
    ("480000", "487FFF", "NL"),  # Netherlands
    ("488000", "48FFFF", "PL"),  # Poland
    ("490000", "497FFF", "PT"),  # Portugal
    ("498000", "49FFFF", "CZ"),  # Czechia
    ("4A0000", "4A7FFF", "RO"),  # Romania
    ("4A8000", "4AFFFF", "SE"),  # Sweden
    ("4B0000", "4B7FFF", "CH"),  # Switzerland
    ("4B8000", "4BFFFF", "TR"),  # Turkey
    ("4C0000", "4C7FFF", "RS"),  # Serbia
    ("4C8000", "4CFFFF", "IE"),  # Ireland
    # Narrow sub-blocks. These sit inside the wide ranges above and win
    # because build() sorts narrowest-first.
    ("4CC000", "4CCFFF", "IS"),  # Iceland
    ("4D0000", "4D03FF", "LU"),  # Luxembourg
    ("4D2000", "4D23FF", "MT"),  # Malta
    ("4D4000", "4D43FF", "MC"),  # Monaco
    ("500000", "5003FF", "SM"),  # San Marino
    ("501000", "5013FF", "AL"),  # Albania
    ("501C00", "501FFF", "HR"),  # Croatia
    ("502C00", "502FFF", "LV"),  # Latvia
    ("503C00", "503FFF", "LT"),  # Lithuania
    ("504C00", "504FFF", "MD"),  # Moldova
    ("505C00", "505FFF", "SK"),  # Slovakia
    ("506C00", "506FFF", "SI"),  # Slovenia
    ("507C00", "507FFF", "UZ"),  # Uzbekistan
    ("508000", "50FFFF", "UA"),  # Ukraine
    ("510000", "5103FF", "BY"),  # Belarus
    ("511000", "5113FF", "EE"),  # Estonia
    ("512000", "5123FF", "MK"),  # North Macedonia
    ("513000", "5133FF", "BA"),  # Bosnia & Herzegovina
    ("514000", "5143FF", "GE"),  # Georgia
    ("515000", "5153FF", "TJ"),  # Tajikistan
    ("516000", "5163FF", "ME"),  # Montenegro
    ("51C000", "51CFFF", "CY"),  # Cyprus
    ("100000", "1FFFFF", "RU"),  # Russian Federation

    # ── North America ───────────────────────────────────────────────────────
    ("A00000", "AFFFFF", "US"),  # United States
    ("C00000", "C3FFFF", "CA"),  # Canada
    ("0D0000", "0D7FFF", "MX"),  # Mexico
    ("0B0000", "0B0FFF", "CR"),  # Costa Rica
    ("0C0000", "0C0FFF", "CU"),  # Cuba
    ("0D8000", "0D8FFF", "PA"),  # Panama

    # ── Asia-Pacific ────────────────────────────────────────────────────────
    ("780000", "7BFFFF", "CN"),  # China
    ("899000", "8993FF", "TW"),  # Taiwan
    ("7C0000", "7FFFFF", "AU"),  # Australia
    ("840000", "87FFFF", "JP"),  # Japan
    ("718000", "71BFFF", "KR"),  # South Korea
    ("800000", "83FFFF", "IN"),  # India
    ("768000", "76BFFF", "SG"),  # Singapore
    ("880000", "887FFF", "TH"),  # Thailand
    ("750000", "757FFF", "MY"),  # Malaysia
    ("8A0000", "8A7FFF", "ID"),  # Indonesia
    ("888000", "88FFFF", "VN"),  # Vietnam
    ("758000", "75FFFF", "PH"),  # Philippines
    ("C80000", "C87FFF", "NZ"),  # New Zealand
    ("700000", "700FFF", "AF"),  # Afghanistan
    ("702000", "7023FF", "BD"),  # Bangladesh
    ("760000", "7603FF", "PK"),  # Pakistan
    ("76C000", "76FFFF", "LK"),  # Sri Lanka
    ("708000", "70BFFF", "IR"),  # Iran

    # ── Middle East ─────────────────────────────────────────────────────────
    ("896000", "896FFF", "AE"),  # United Arab Emirates
    ("06A000", "06A3FF", "QA"),  # Qatar
    ("710000", "717FFF", "SA"),  # Saudi Arabia
    ("706000", "7063FF", "BH"),  # Bahrain
    ("706C00", "706FFF", "KW"),  # Kuwait
    ("70C000", "70C3FF", "OM"),  # Oman
    ("738000", "73FFFF", "IL"),  # Israel
    ("728000", "72FFFF", "IQ"),  # Iraq
    ("778000", "77FFFF", "JO"),  # Jordan
    ("74C000", "74FFFF", "LB"),  # Lebanon

    # ── Africa ──────────────────────────────────────────────────────────────
    ("008000", "00FFFF", "ZA"),  # South Africa
    ("010000", "017FFF", "EG"),  # Egypt
    ("018000", "01FFFF", "LY"),  # Libya
    ("020000", "027FFF", "DZ"),  # Algeria
    ("020C00", "020FFF", "MA"),  # Morocco
    ("03E000", "03EFFF", "TN"),  # Tunisia
    ("040000", "047FFF", "ET"),  # Ethiopia
    ("04C000", "04FFFF", "KE"),  # Kenya
    ("064000", "0643FF", "NG"),  # Nigeria

    # ── South America ───────────────────────────────────────────────────────
    ("E40000", "E7FFFF", "BR"),  # Brazil
    ("E00000", "E3FFFF", "AR"),  # Argentina
    ("E80000", "E80FFF", "CL"),  # Chile
    ("0A8000", "0AFFFF", "CO"),  # Colombia
    ("E84000", "E843FF", "PE"),  # Peru
]

# ── Registration prefixes (fallback / corroboration) ─────────────────────────
# Matched longest-first against the registration string.
REG_PREFIXES: dict[str, str] = {
    "OY": "DK", "SE": "SE", "LN": "NO", "OH": "FI", "TF": "IS",
    "D": "DE", "OE": "AT", "HB": "CH", "PH": "NL", "OO": "BE", "LX": "LU",
    "G": "GB", "EI": "IE", "F": "FR", "EC": "ES", "CS": "PT", "I": "IT",
    "SP": "PL", "OK": "CZ", "OM": "SK", "HA": "HU", "YL": "LV", "ES": "EE",
    "LY": "LT", "9H": "MT", "5B": "CY", "SX": "GR", "YR": "RO", "LZ": "BG",
    "YU": "RS", "9A": "HR", "S5": "SI", "Z3": "MK", "T7": "SM", "TC": "TR",
    "UR": "UA", "RA": "RU", "RF": "RU", "EW": "BY",
    "N": "US", "C": "CA", "XA": "MX", "XB": "MX", "XC": "MX",
    "B": "CN", "JA": "JP", "HL": "KR", "VT": "IN", "9V": "SG", "HS": "TH",
    "9M": "MY", "PK": "ID", "VN": "VN", "RP": "PH", "VH": "AU", "ZK": "NZ",
    "A6": "AE", "A7": "QA", "HZ": "SA", "A9C": "BH", "9K": "KW", "A4O": "OM",
    "4X": "IL", "JY": "JO", "OD": "LB", "EP": "IR",
    "ZS": "ZA", "SU": "EG", "7T": "DZ", "CN": "MA", "5A": "LY", "ET": "ET",
    "5Y": "KE", "5N": "NG", "TS": "TN",
    "PP": "BR", "PR": "BR", "PT": "BR", "LV": "AR", "CC": "CL", "HK": "CO",
    "OB": "PE", "HP": "PA",
}

# ── country -> (name, economic region, continent) ────────────────────────────
COUNTRIES: dict[str, tuple[str, str, str]] = {
    # Nordics
    "DK": ("Denmark",        "Nordics",         "Europe"),
    "SE": ("Sweden",         "Nordics",         "Europe"),
    "NO": ("Norway",         "Nordics",         "Europe"),
    "FI": ("Finland",        "Nordics",         "Europe"),
    "IS": ("Iceland",        "Nordics",         "Europe"),
    # DACH
    "DE": ("Germany",        "DACH",            "Europe"),
    "AT": ("Austria",        "DACH",            "Europe"),
    "CH": ("Switzerland",    "DACH",            "Europe"),
    # Benelux
    "NL": ("Netherlands",    "Benelux",         "Europe"),
    "BE": ("Belgium",        "Benelux",         "Europe"),
    "LU": ("Luxembourg",     "Benelux",         "Europe"),
    # British Isles
    "GB": ("United Kingdom", "British Isles",   "Europe"),
    "IE": ("Ireland",        "British Isles",   "Europe"),
    # Southern Europe
    "FR": ("France",         "Western Europe",  "Europe"),
    "ES": ("Spain",          "Southern Europe", "Europe"),
    "PT": ("Portugal",       "Southern Europe", "Europe"),
    "IT": ("Italy",          "Southern Europe", "Europe"),
    "GR": ("Greece",         "Southern Europe", "Europe"),
    "MT": ("Malta",          "Southern Europe", "Europe"),
    "CY": ("Cyprus",         "Southern Europe", "Europe"),
    "MC": ("Monaco",         "Western Europe",  "Europe"),
    "SM": ("San Marino",     "Southern Europe", "Europe"),
    # Central & Eastern Europe
    "PL": ("Poland",         "Central Europe",  "Europe"),
    "CZ": ("Czechia",        "Central Europe",  "Europe"),
    "SK": ("Slovakia",       "Central Europe",  "Europe"),
    "HU": ("Hungary",        "Central Europe",  "Europe"),
    "SI": ("Slovenia",       "Central Europe",  "Europe"),
    "HR": ("Croatia",        "Southern Europe", "Europe"),
    "RS": ("Serbia",         "Southern Europe", "Europe"),
    "BA": ("Bosnia & Herz.", "Southern Europe", "Europe"),
    "ME": ("Montenegro",     "Southern Europe", "Europe"),
    "MK": ("North Macedonia","Southern Europe", "Europe"),
    "AL": ("Albania",        "Southern Europe", "Europe"),
    "RO": ("Romania",        "Central Europe",  "Europe"),
    "BG": ("Bulgaria",       "Central Europe",  "Europe"),
    "MD": ("Moldova",        "Eastern Europe",  "Europe"),
    # Baltics
    "LV": ("Latvia",         "Baltics",         "Europe"),
    "EE": ("Estonia",        "Baltics",         "Europe"),
    "LT": ("Lithuania",      "Baltics",         "Europe"),
    # Eastern Europe
    "UA": ("Ukraine",        "Eastern Europe",  "Europe"),
    "BY": ("Belarus",        "Eastern Europe",  "Europe"),
    "RU": ("Russia",         "Eastern Europe",  "Europe"),
    "GE": ("Georgia",        "Eastern Europe",  "Europe"),
    "TR": ("Turkey",         "Turkey",          "Europe"),
    # North America
    "US": ("United States",  "North America",   "North America"),
    "CA": ("Canada",         "North America",   "North America"),
    "MX": ("Mexico",         "North America",   "North America"),
    "CR": ("Costa Rica",     "Central America", "North America"),
    "PA": ("Panama",         "Central America", "North America"),
    "CU": ("Cuba",           "Caribbean",       "North America"),
    # East & South Asia
    "CN": ("China",          "East Asia",       "Asia"),
    "TW": ("Taiwan",         "East Asia",       "Asia"),
    "JP": ("Japan",          "East Asia",       "Asia"),
    "KR": ("South Korea",    "East Asia",       "Asia"),
    "SG": ("Singapore",      "Southeast Asia",  "Asia"),
    "TH": ("Thailand",       "Southeast Asia",  "Asia"),
    "MY": ("Malaysia",       "Southeast Asia",  "Asia"),
    "ID": ("Indonesia",      "Southeast Asia",  "Asia"),
    "VN": ("Vietnam",        "Southeast Asia",  "Asia"),
    "PH": ("Philippines",    "Southeast Asia",  "Asia"),
    "IN": ("India",          "South Asia",      "Asia"),
    "PK": ("Pakistan",       "South Asia",      "Asia"),
    "BD": ("Bangladesh",     "South Asia",      "Asia"),
    "LK": ("Sri Lanka",      "South Asia",      "Asia"),
    "AF": ("Afghanistan",    "South Asia",      "Asia"),
    "UZ": ("Uzbekistan",     "Central Asia",    "Asia"),
    "TJ": ("Tajikistan",     "Central Asia",    "Asia"),
    # Middle East
    "AE": ("United Arab Em.","Gulf",            "Asia"),
    "QA": ("Qatar",          "Gulf",            "Asia"),
    "SA": ("Saudi Arabia",   "Gulf",            "Asia"),
    "BH": ("Bahrain",        "Gulf",            "Asia"),
    "KW": ("Kuwait",         "Gulf",            "Asia"),
    "OM": ("Oman",           "Gulf",            "Asia"),
    "IL": ("Israel",         "Levant",          "Asia"),
    "JO": ("Jordan",         "Levant",          "Asia"),
    "LB": ("Lebanon",        "Levant",          "Asia"),
    "IQ": ("Iraq",           "Levant",          "Asia"),
    "IR": ("Iran",           "Middle East",     "Asia"),
    # Africa
    "ZA": ("South Africa",   "Southern Africa", "Africa"),
    "EG": ("Egypt",          "North Africa",    "Africa"),
    "DZ": ("Algeria",        "North Africa",    "Africa"),
    "MA": ("Morocco",        "North Africa",    "Africa"),
    "TN": ("Tunisia",        "North Africa",    "Africa"),
    "LY": ("Libya",          "North Africa",    "Africa"),
    "ET": ("Ethiopia",       "East Africa",     "Africa"),
    "KE": ("Kenya",          "East Africa",     "Africa"),
    "NG": ("Nigeria",        "West Africa",     "Africa"),
    # South America
    "BR": ("Brazil",         "South America",   "South America"),
    "AR": ("Argentina",      "South America",   "South America"),
    "CL": ("Chile",          "South America",   "South America"),
    "CO": ("Colombia",       "South America",   "South America"),
    "PE": ("Peru",           "South America",   "South America"),
    # Oceania
    "AU": ("Australia",      "Oceania",         "Oceania"),
    "NZ": ("New Zealand",    "Oceania",         "Oceania"),
}


def build() -> dict:
    blocks = []
    for start, end, cc in ICAO_BLOCKS:
        if cc not in COUNTRIES:
            continue
        lo, hi = int(start, 16), int(end, 16)
        blocks.append({
            "lo": lo, "hi": hi, "cc": cc,
            # Zero-padded lowercase hex bounds. Every ICAO address is exactly
            # six hex digits, so a lexicographic string comparison on these is
            # equivalent to comparing the integers - which lets the browser do
            # country attribution as a plain SQL range join, with no casting.
            "lo_hex": f"{lo:06x}", "hi_hex": f"{hi:06x}",
            "width": hi - lo,
        })
    # Narrower blocks must win over the broad ones they sit inside.
    blocks.sort(key=lambda b: (b["width"], b["lo"]))

    return {
        "note": (
            "Country attribution is derived from the aircraft's 24-bit ICAO "
            "address, allocated to states in fixed blocks by ICAO Annex 10. "
            "Registration prefixes corroborate the same fact and act as a "
            "fallback. This identifies where an aircraft is REGISTERED - not "
            "where the flight departed from or is going to."
        ),
        "blocks": blocks,
        "reg_prefixes": REG_PREFIXES,
        "countries": {
            cc: {"name": n, "region": r, "continent": c}
            for cc, (n, r, c) in COUNTRIES.items()
        },
    }


def lookup_hex(hx: str, blocks: list[dict]) -> str | None:
    """Resolve a hex address to an ISO country code. Narrowest block wins."""
    if not hx:
        return None
    try:
        v = int(hx, 16)
    except ValueError:
        return None
    for b in blocks:
        if b["lo"] <= v <= b["hi"]:
            return b["cc"]
    return None


def lookup_reg(reg: str) -> str | None:
    """Resolve a registration to an ISO country code, longest prefix first."""
    if not reg:
        return None
    r = reg.upper().replace("-", "")
    for length in (3, 2, 1):
        if len(r) >= length and (cc := REG_PREFIXES.get(r[:length])):
            return cc
    return None


def main() -> int:
    tax = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(tax, indent=1), encoding="utf-8")
    print(f"[taxonomy] {len(tax['blocks'])} ICAO blocks, "
          f"{len(tax['countries'])} countries -> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
