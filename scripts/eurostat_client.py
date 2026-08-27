"""
Eurostat client — pulls monthly air-passenger data between EU reporting countries
and their partners, aggregates to country pairs, and yields corridor rows.

Data source: Eurostat dataset family `avia_par_<reporting_country>` (e.g. avia_par_de).
Each dataset publishes monthly passenger counts by (reporting_airport, partner_airport).
We aggregate away the airport dimension to get country-pair totals.

API docs: https://wikis.ec.europa.eu/display/EUROSTATHELP/API+-+Data+queries
Format:   JSON-stat 2.0 (https://json-stat.org/)

No auth required, no rate limits published — cache raw responses to be polite.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

# Eurostat reporting countries for the avia_par_XX family.
# EL is Greece in Eurostat's ISO variant; airports use LT_EYVI style codes
# whose first token is the ISO2 country of the airport.
REPORTING = [
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IS", "IT", "LT", "LU", "LV", "MT", "NL", "NO", "PL",
    "PT", "RO", "SE", "SI", "SK", "UK",
]

CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "eurostat"


@dataclass
class MonthlyPair:
    reporter: str      # ISO2 of reporting country
    partner: str       # ISO2 of partner country
    month: str         # YYYY-MM
    passengers: int


def _cache_path(dataset: str, params_key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{dataset}__{params_key}.json"


def _fetch(dataset: str, params: dict, force: bool = False) -> dict:
    key = "_".join(f"{k}-{v}" for k, v in sorted(params.items()) if k != "format")
    cache = _cache_path(dataset, key)
    if cache.exists() and not force:
        return json.loads(cache.read_text())

    url = f"{BASE}/{dataset}"
    r = requests.get(url, params={"format": "JSON", **params}, timeout=60)
    r.raise_for_status()
    payload = r.json()
    cache.write_text(json.dumps(payload))
    return payload


def _decode_jsonstat(payload: dict) -> Iterable[tuple[dict, float]]:
    """
    Yield (dimensions_dict, value) tuples from a JSON-stat 2.0 response.
    Skips null cells. Preserves category codes (not labels).
    """
    dims = payload["id"]                              # e.g. ["freq","unit","tra_meas","airp_pr","geo","time"]
    sizes = payload["size"]                           # parallel list of sizes
    dim_meta = payload["dimension"]                   # each has category.index (code → position)

    # Build position → code arrays per dimension.
    codes_by_dim: list[list[str]] = []
    for d in dims:
        idx = dim_meta[d]["category"]["index"]
        if isinstance(idx, dict):
            arr = sorted(idx.items(), key=lambda kv: kv[1])
            codes_by_dim.append([k for k, _ in arr])
        else:  # list form
            codes_by_dim.append(list(idx))

    values = payload["value"]
    total = 1
    for s in sizes:
        total *= s

    # Values arrive either as list (dense) or dict {index: value} (sparse).
    if isinstance(values, dict):
        def get(i: int):
            return values.get(str(i))
    else:
        def get(i: int):
            return values[i] if i < len(values) else None

    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    for i in range(total):
        v = get(i)
        if v is None:
            continue
        coords = {}
        rem = i
        for j, d in enumerate(dims):
            pos = rem // strides[j]
            rem = rem % strides[j]
            coords[d] = codes_by_dim[j][pos]
        yield coords, v


def _partner_country(airport_code: str) -> str | None:
    """
    Eurostat airport codes look like `LT_EYVI` (country_ICAO).
    Some entries are country-level rollups like `LT` alone.
    Returns the 2-letter partner country or None if we can't tell.
    """
    if not airport_code:
        return None
    if "_" in airport_code:
        return airport_code.split("_", 1)[0]
    if len(airport_code) == 2 and airport_code.isalpha():
        return airport_code
    return None


def fetch_country_pairs(
    reporter: str,
    months: list[str],
    unit: str = "PAS",
    tra_meas: str = "PAS_CRD",
) -> list[MonthlyPair]:
    """
    Fetch monthly passenger totals between `reporter` and each partner country,
    for the given YYYY-MM months.

    Aggregates over all airport pairs to a single country-pair total per month.
    """
    dataset = f"avia_par_{reporter.lower()}"
    params = {
        "freq": "M",
        "unit": unit,
        "tra_meas": tra_meas,
    }
    # Eurostat accepts multiple `time` params — requests turns a list into repeated params.
    query = {**params, "time": months}

    try:
        payload = _fetch(dataset, query)
    except requests.HTTPError as e:
        print(f"[eurostat] {dataset} failed: {e}")
        return []

    # (reporter, partner, month) -> summed passengers
    agg: dict[tuple[str, str, str], int] = {}
    for coords, value in _decode_jsonstat(payload):
        airp = coords.get("airp_pr", "")
        partner = _partner_country(airp)
        if not partner or partner == reporter:
            continue
        month = coords.get("time", "")
        key = (reporter, partner, month)
        agg[key] = agg.get(key, 0) + int(value)

    return [
        MonthlyPair(reporter=r, partner=p, month=m, passengers=v)
        for (r, p, m), v in agg.items()
    ]


def fetch_all(months_current: list[str], months_prior: list[str], polite_delay: float = 0.5) -> list[MonthlyPair]:
    """Iterate all reporting countries; return a flat list of monthly country-pair rows."""
    out: list[MonthlyPair] = []
    for r in REPORTING:
        rows = fetch_country_pairs(r, months_current + months_prior)
        print(f"[eurostat] {r}: {len(rows)} country-pair rows")
        out.extend(rows)
        time.sleep(polite_delay)
    return out
