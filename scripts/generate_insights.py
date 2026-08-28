"""
Daily pipeline: fetch flight-route data → detect anomalies → enrich with Claude → write data/insights.json

Current source: Eurostat `avia_par_*` (monthly passenger totals between EU
reporting countries and their partners, aggregated to country-pair level).

Run locally:
    ANTHROPIC_API_KEY=sk-... python scripts/generate_insights.py

Run in CI: see .github/workflows/update-insights.yml
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from anthropic import Anthropic
except ImportError:
    print("Install deps: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

from airports import lookup_icao
from country_coords import lookup as country_lookup
from eurostat_client import MonthlyAirportPair, MonthlyPair, fetch_all, fetch_all_airport

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "data" / "insights.json"

# Country-level: broader picture, higher volume floor.
TOP_N_COUNTRY = 40
VOLUME_FLOOR_COUNTRY = 5_000

# City-level: many more signals, lower floor. Users pick a country and get a
# feed of specific city-pair anomalies rather than one aggregate number.
TOP_N_CITY = 120
VOLUME_FLOOR_CITY = 1_500

DELTA_FLOOR_PCT = 4.0   # min |YoY %| to qualify as anomalous (both levels)

MODEL = "claude-opus-4-5"


@dataclass
class Corridor:
    origin_code: str
    dest_code: str
    origin_name: str
    dest_name: str
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float
    period: str
    volume_current: int
    volume_prior: int
    granularity: str = "country"

    @property
    def delta_pct(self) -> float:
        if self.volume_prior <= 0:
            return 0.0
        return (self.volume_current - self.volume_prior) / self.volume_prior * 100.0

    @property
    def signal_strength(self) -> float:
        return abs(self.delta_pct) * math.log10(max(self.volume_current, 10))


# ---------------------------------------------------------------------------
# STEP 1 — INGEST
# ---------------------------------------------------------------------------

def month_str(d: date) -> str:
    return d.strftime("%Y-%m")


def latest_available_month(today: date | None = None) -> date:
    """Eurostat publishes monthly aviation data with ~2 month lag."""
    today = today or date.today()
    y, m = today.year, today.month - 3
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def build_corridors(rows: list[MonthlyPair], current_month: str, prior_month: str) -> list[Corridor]:
    """Pivot monthly rows into current/prior corridors, undirected by country pair."""
    # (reporter, partner) -> {month: passengers}
    grid: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        months_map = grid.setdefault((r.reporter, r.partner), {})
        months_map[r.month] = months_map.get(r.month, 0) + r.passengers

    period_label = f"{_pretty_month(current_month)} vs {_pretty_month(prior_month)}"
    corridors: list[Corridor] = []
    seen: set[tuple[str, str]] = set()

    for (rep, par), months in grid.items():
        # Undirected pair — always order alphabetically so we don't double-count.
        key = tuple(sorted([rep, par]))
        if key in seen:
            continue
        seen.add(key)

        # Combine directions (rep→par and par→rep) for a symmetric corridor volume.
        both = months.copy()
        other = grid.get((par, rep), {})
        for m, v in other.items():
            both[m] = both.get(m, 0) + v

        vc = both.get(current_month, 0)
        vp = both.get(prior_month, 0)
        if vc == 0 or vp == 0:
            continue

        oc = country_lookup(key[0])
        dc = country_lookup(key[1])
        if not oc or not dc:
            continue

        corridors.append(Corridor(
            origin_code=key[0], dest_code=key[1],
            origin_name=oc["name"], dest_name=dc["name"],
            origin_lat=oc["lat"], origin_lng=oc["lng"],
            dest_lat=dc["lat"], dest_lng=dc["lng"],
            period=period_label,
            volume_current=vc, volume_prior=vp,
        ))
    return corridors


def _pretty_month(yyyymm: str) -> str:
    try:
        return datetime.strptime(yyyymm, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return yyyymm


# ---------------------------------------------------------------------------
# STEP 1b — INGEST (city-level)
# ---------------------------------------------------------------------------

def build_city_corridors(rows: list[MonthlyAirportPair], current_month: str, prior_month: str) -> list[Corridor]:
    """City-pair corridors keyed on (reporter_country, partner_airport_ICAO)."""
    # (reporter_country, partner_airport_icao) -> {month: passengers}
    grid: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        if not r.partner_airport:
            continue
        key = (r.reporter_country, r.partner_airport)
        months_map = grid.setdefault(key, {})
        months_map[r.month] = months_map.get(r.month, 0) + r.passengers

    period_label = f"{_pretty_month(current_month)} vs {_pretty_month(prior_month)}"
    out: list[Corridor] = []

    for (reporter, partner_icao), months in grid.items():
        vc = months.get(current_month, 0)
        vp = months.get(prior_month, 0)
        if vc == 0 or vp == 0:
            continue
        partner_ap = lookup_icao(partner_icao)
        rep_country = country_lookup(reporter)
        if not partner_ap or not rep_country:
            continue

        out.append(Corridor(
            origin_code=reporter, dest_code=partner_ap["country"],
            origin_name=rep_country["name"],
            dest_name=f"{partner_ap['city']} ({partner_ap['iata']})",
            origin_lat=rep_country["lat"], origin_lng=rep_country["lng"],
            dest_lat=partner_ap["lat"], dest_lng=partner_ap["lng"],
            period=period_label,
            volume_current=vc, volume_prior=vp,
            granularity="city",
        ))
    return out


# ---------------------------------------------------------------------------
# STEP 2/3 — FILTER + RANK
# ---------------------------------------------------------------------------

def rank_anomalies(
    corridors: list[Corridor],
    top_n: int,
    volume_floor: int,
) -> list[Corridor]:
    filtered = [
        c for c in corridors
        if c.volume_current >= volume_floor and abs(c.delta_pct) >= DELTA_FLOOR_PCT
    ]
    filtered.sort(key=lambda c: c.signal_strength, reverse=True)
    return filtered[:top_n]


# ---------------------------------------------------------------------------
# STEP 4 — ENRICH VIA CLAUDE
# ---------------------------------------------------------------------------

ENRICH_PROMPT = """You are a macroeconomics analyst writing one signal card for a data dashboard.

Corridor: {origin} → {dest}
Period: {period}
Volume: {vol_current:,} passengers this period vs {vol_prior:,} same period prior year
YoY delta: {delta:+.1f}%

Propose:
1. A short headline (max 12 words), factual, no exclamation marks.
2. A 2-sentence reading of the likely macroeconomic driver — tourism, business,
   migration, supply-chain, sanctions, FX, visa policy, etc. Ground it in what
   you know about this specific corridor. If uncertain, say so.
3. A theme label — one of: tourism, business, migration, supply-chain.
4. A confidence label — one of: high, medium, low.

Return JSON only, no prose around it:
{{"headline": "...", "reading": "...", "theme": "...", "confidence": "..."}}"""


def enrich(client: Anthropic, c: Corridor) -> dict:
    prompt = ENRICH_PROMPT.format(
        origin=c.origin_name, dest=c.dest_name,
        period=c.period,
        vol_current=c.volume_current, vol_prior=c.volume_prior,
        delta=c.delta_pct,
    )
    msg = client.messages.create(
        model=MODEL, max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


# ---------------------------------------------------------------------------
# STEP 5 — ASSEMBLE + WRITE
# ---------------------------------------------------------------------------

def to_insight(c: Corridor, enrichment: dict) -> dict:
    slug = f"{c.origin_code.lower()}-{c.dest_code.lower()}-{c.granularity}-{c.period.replace(' ', '').lower()}"
    dest_country_name = (country_lookup(c.dest_code) or {}).get("name", c.dest_name)
    origin_country_name = (country_lookup(c.origin_code) or {}).get("name", c.origin_name)
    return {
        "id": slug,
        "origin": {
            "name": c.origin_name, "code": c.origin_code,
            "country": origin_country_name,
            "lat": c.origin_lat, "lng": c.origin_lng, "type": c.granularity,
        },
        "dest": {
            "name": c.dest_name, "code": c.dest_code,
            "country": dest_country_name,
            "lat": c.dest_lat, "lng": c.dest_lng, "type": c.granularity,
        },
        "period": c.period,
        "deltaPct": round(c.delta_pct, 1),
        "volumeCurrent": c.volume_current,
        "volumePrior": c.volume_prior,
        "theme": enrichment.get("theme", "business"),
        "headline": enrichment.get("headline", f"{c.origin_name} ↔ {c.dest_name} {c.delta_pct:+.1f}%"),
        "reading": enrichment.get("reading", ""),
        "confidence": enrichment.get("confidence", "low"),
    }


def collect_from_api(current_m: str, prior_m: str) -> list[Corridor]:
    """Fallback path: hit Eurostat's HTTP API directly (no warehouse required)."""
    rows = fetch_all([current_m], [prior_m])
    print(f"[fetch/api] {len(rows)} country-level rows")
    country = build_corridors(rows, current_m, prior_m)

    city_rows = fetch_all_airport([current_m, prior_m])
    print(f"[fetch/api] {len(city_rows)} airport-level rows")
    city = build_city_corridors(city_rows, current_m, prior_m)

    ranked_country = rank_anomalies(country, TOP_N_COUNTRY, VOLUME_FLOOR_COUNTRY)
    ranked_city    = rank_anomalies(city,    TOP_N_CITY,    VOLUME_FLOOR_CITY)
    print(f"[rank/api] country={len(ranked_country)} city={len(ranked_city)}")
    return ranked_country + ranked_city


def collect_from_warehouse(current_m: str, prior_m: str) -> list[Corridor]:
    """Primary path: query the local DuckDB warehouse populated by ingest_*.py."""
    try:
        from warehouse import connect, register_views
    except ImportError as e:
        print(f"[warehouse] duckdb missing: {e}", file=sys.stderr)
        return []

    con = connect(read_only=True)
    register_views(con)

    # Country-level from corridor_monthly (prefers passengers, falls back to flights × 100).
    period_label = f"{_pretty_month(current_m)} vs {_pretty_month(prior_m)}"
    country_rows = con.execute("""
        WITH cur AS (
            SELECT o_country, d_country,
                   COALESCE(passengers, flights * 100) AS volume
            FROM corridor_monthly WHERE month = ?
        ),
        pri AS (
            SELECT o_country, d_country,
                   COALESCE(passengers, flights * 100) AS volume
            FROM corridor_monthly WHERE month = ?
        )
        SELECT c.o_country, c.d_country, c.volume AS vc, p.volume AS vp
        FROM cur c JOIN pri p USING (o_country, d_country)
        WHERE c.volume > 0 AND p.volume > 0
    """, [current_m, prior_m]).fetchall()
    print(f"[fetch/warehouse] country pairs with both periods: {len(country_rows)}")

    country_corridors: list[Corridor] = []
    for o, d, vc, vp in country_rows:
        oc = country_lookup(o); dc = country_lookup(d)
        if not oc or not dc:
            continue
        country_corridors.append(Corridor(
            origin_code=o, dest_code=d,
            origin_name=oc["name"], dest_name=dc["name"],
            origin_lat=oc["lat"], origin_lng=oc["lng"],
            dest_lat=dc["lat"],   dest_lng=dc["lng"],
            period=period_label,
            volume_current=int(vc), volume_prior=int(vp),
            granularity="country",
        ))

    city_rows = con.execute("""
        WITH cur AS (
            SELECT o_country, d_country, d_airport,
                   COALESCE(passengers, flights * 100) AS volume
            FROM city_corridor_monthly WHERE month = ?
        ),
        pri AS (
            SELECT o_country, d_country, d_airport,
                   COALESCE(passengers, flights * 100) AS volume
            FROM city_corridor_monthly WHERE month = ?
        )
        SELECT c.o_country, c.d_country, c.d_airport, c.volume AS vc, p.volume AS vp
        FROM cur c JOIN pri p USING (o_country, d_country, d_airport)
        WHERE c.volume > 0 AND p.volume > 0
    """, [current_m, prior_m]).fetchall()
    print(f"[fetch/warehouse] city pairs with both periods: {len(city_rows)}")

    city_corridors: list[Corridor] = []
    for o, d, ap_icao, vc, vp in city_rows:
        oc = country_lookup(o)
        ap = lookup_icao(ap_icao)
        if not oc or not ap:
            continue
        city_corridors.append(Corridor(
            origin_code=o, dest_code=ap["country"],
            origin_name=oc["name"],
            dest_name=f"{ap['city']} ({ap['iata']})",
            origin_lat=oc["lat"], origin_lng=oc["lng"],
            dest_lat=ap["lat"],   dest_lng=ap["lng"],
            period=period_label,
            volume_current=int(vc), volume_prior=int(vp),
            granularity="city",
        ))

    ranked_country = rank_anomalies(country_corridors, TOP_N_COUNTRY, VOLUME_FLOOR_COUNTRY)
    ranked_city    = rank_anomalies(city_corridors,    TOP_N_CITY,    VOLUME_FLOOR_CITY)
    print(f"[rank/warehouse] country={len(ranked_country)} city={len(ranked_city)}")
    return ranked_country + ranked_city


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip Claude enrichment, use placeholder text")
    parser.add_argument("--month",   help="YYYY-MM to analyze (default: latest available)")
    parser.add_argument("--source",  choices=["warehouse", "api"], default="warehouse",
                        help="warehouse = query local DuckDB (default). api = hit Eurostat directly.")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        print("ANTHROPIC_API_KEY not set — use --dry-run to skip enrichment.", file=sys.stderr)
        return 1

    ref = datetime.strptime(args.month, "%Y-%m").date() if args.month else latest_available_month()
    prior = date(ref.year - 1, ref.month, 1)
    current_m, prior_m = month_str(ref), month_str(prior)
    print(f"[cfg] source={args.source} current={current_m} prior={prior_m}")

    if args.source == "warehouse":
        ranked = collect_from_warehouse(current_m, prior_m)
        if not ranked:
            print("[warehouse] empty result — falling back to API.")
            ranked = collect_from_api(current_m, prior_m)
    else:
        ranked = collect_from_api(current_m, prior_m)
    if not ranked:
        print("[rank] Nothing to publish. Leaving data/insights.json untouched.")
        return 0

    if args.dry_run:
        insights = [
            to_insight(c, {
                "theme": "business",
                "headline": f"{c.origin_name} ↔ {c.dest_name} {c.delta_pct:+.1f}%",
                "reading": "(dry run — enrichment skipped)",
                "confidence": "low",
            })
            for c in ranked
        ]
    else:
        client = Anthropic(api_key=api_key)
        insights = []
        for i, c in enumerate(ranked, 1):
            try:
                insights.append(to_insight(c, enrich(client, c)))
                print(f"[enrich] {i}/{len(ranked)}  {c.origin_name} ↔ {c.dest_name}")
            except Exception as e:
                print(f"[enrich] FAILED {c.origin_name} ↔ {c.dest_name}: {e}", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "meta": {
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "current": current_m,
            "prior": prior_m,
            "source": "OpenSky Network + Eurostat avia_par_*" if args.source == "warehouse" else "Eurostat avia_par_*",
            "coverage": "EU + EEA reporting countries plus their partners; city and country grain",
        },
        "insights": insights,
    }, indent=2))
    print(f"[write] {len(insights)} insights → {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
