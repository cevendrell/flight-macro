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

from country_coords import lookup as country_lookup
from eurostat_client import MonthlyPair, fetch_all

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "data" / "insights.json"

TOP_N = 25              # how many anomalies to enrich
VOLUME_FLOOR = 5_000    # min monthly passengers to qualify as material
DELTA_FLOOR_PCT = 4.0   # min |YoY %| to qualify as anomalous

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
# STEP 2/3 — FILTER + RANK
# ---------------------------------------------------------------------------

def rank_anomalies(corridors: list[Corridor]) -> list[Corridor]:
    filtered = [
        c for c in corridors
        if c.volume_current >= VOLUME_FLOOR and abs(c.delta_pct) >= DELTA_FLOOR_PCT
    ]
    filtered.sort(key=lambda c: c.signal_strength, reverse=True)
    return filtered[:TOP_N]


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
    slug = f"{c.origin_code.lower()}-{c.dest_code.lower()}-{c.period.replace(' ', '').lower()}"
    return {
        "id": slug,
        "origin": {
            "name": c.origin_name, "code": c.origin_code,
            "lat": c.origin_lat, "lng": c.origin_lng, "type": c.granularity,
        },
        "dest": {
            "name": c.dest_name, "code": c.dest_code,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip Claude enrichment, use placeholder text")
    parser.add_argument("--month", help="YYYY-MM to analyze (default: latest available)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        print("ANTHROPIC_API_KEY not set — use --dry-run to skip enrichment.", file=sys.stderr)
        return 1

    ref = datetime.strptime(args.month, "%Y-%m").date() if args.month else latest_available_month()
    prior = date(ref.year - 1, ref.month, 1)
    current_m, prior_m = month_str(ref), month_str(prior)
    print(f"[cfg] current={current_m}  prior={prior_m}  top_n={TOP_N}  floor={VOLUME_FLOOR:,}")

    rows = fetch_all([current_m], [prior_m])
    print(f"[fetch] {len(rows)} monthly reporter/partner rows")

    corridors = build_corridors(rows, current_m, prior_m)
    print(f"[build] {len(corridors)} unique country-pair corridors")

    ranked = rank_anomalies(corridors)
    print(f"[rank] {len(ranked)} corridors above threshold")

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
            "source": "Eurostat avia_par_*",
            "coverage": "EU + EEA reporting countries, aggregated by partner country",
        },
        "insights": insights,
    }, indent=2))
    print(f"[write] {len(insights)} insights → {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
