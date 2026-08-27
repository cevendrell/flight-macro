"""
Daily pipeline: fetch flight-route data → detect anomalies → enrich with Claude → write data/insights.json

SCAFFOLD — the fetch and normalize steps have TODOs. The scoring, ranking, and
enrichment steps are implemented so once you plug in a real source, insights
start generating.

Run locally:
    ANTHROPIC_API_KEY=sk-... python scripts/generate_insights.py

Run in CI: see .github/workflows/update-insights.yml
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from anthropic import Anthropic
except ImportError:
    print("Install deps: pip install anthropic requests", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "data" / "insights.json"

# How many top-ranked corridors to enrich and publish.
TOP_N = 25
# Minimum absolute volume (flights per month) to qualify as material.
VOLUME_FLOOR = 500
# Minimum |YoY %| to qualify as anomalous.
DELTA_FLOOR = 3.0

MODEL = "claude-opus-4-5"


@dataclass
class Corridor:
    origin_name: str
    origin_code: str
    origin_lat: float
    origin_lng: float
    dest_name: str
    dest_code: str
    dest_lat: float
    dest_lng: float
    period: str
    volume_current: int
    volume_prior: int
    granularity: str  # "continent" | "country" | "city"

    @property
    def delta_pct(self) -> float:
        if self.volume_prior == 0:
            return 0.0
        return (self.volume_current - self.volume_prior) / self.volume_prior * 100.0

    @property
    def signal_strength(self) -> float:
        return abs(self.delta_pct) * math.log10(max(self.volume_current, 10))


# ---------------------------------------------------------------------------
# STEP 1 — INGEST
# ---------------------------------------------------------------------------

def fetch_corridors() -> list[Corridor]:
    """
    TODO: replace this stub with real ingestion.

    Suggested sources (all free):
      - OpenSky Network historical dumps: https://opensky-network.org/data/impala
        (needs a free account; monthly flight-level ADS-B, aggregate by origin/dest country)
      - BTS T-100 Segment: https://www.transtats.bts.gov/DL_SelectFields.aspx?Table_ID=293
        (monthly US route stats)
      - Eurocontrol Aviation Data: https://ansperformance.eu/data/
        (European route stats)

    Normalize each source into the Corridor dataclass above.
    Aggregate by country-pair for the current month and the same month last year.
    """
    print("[fetch] STUB — returning empty corridor list. Wire up real sources here.")
    return []


# ---------------------------------------------------------------------------
# STEP 2/3 — FILTER + RANK
# ---------------------------------------------------------------------------

def rank_anomalies(corridors: list[Corridor]) -> list[Corridor]:
    filtered = [
        c for c in corridors
        if c.volume_current >= VOLUME_FLOOR and abs(c.delta_pct) >= DELTA_FLOOR
    ]
    filtered.sort(key=lambda c: c.signal_strength, reverse=True)
    return filtered[:TOP_N]


# ---------------------------------------------------------------------------
# STEP 4 — ENRICH VIA CLAUDE
# ---------------------------------------------------------------------------

ENRICH_PROMPT = """You are a macroeconomics analyst writing one signal card for a data dashboard.

Corridor: {origin} → {dest}
Period: {period}
Volume: {vol_current:,} flights this period vs {vol_prior:,} same period prior year
YoY delta: {delta:+.1f}%

Propose:
1. A short headline (max 12 words), factual, no exclamation marks.
2. A 2-sentence reading of the likely macroeconomic driver. Grounded in what
   you know about this corridor's context (tourism, business, migration,
   supply-chain, sanctions, FX, etc.). If uncertain, say so.
3. A theme label — one of: tourism, business, migration, supply-chain.
4. A confidence label — one of: high, medium, low.

Return JSON only, no prose around it:
{{"headline": "...", "reading": "...", "theme": "...", "confidence": "..."}}"""


def enrich(client: Anthropic, c: Corridor) -> dict:
    prompt = ENRICH_PROMPT.format(
        origin=c.origin_name,
        dest=c.dest_name,
        period=c.period,
        vol_current=c.volume_current,
        vol_prior=c.volume_prior,
        delta=c.delta_pct,
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    # tolerate models that wrap in fences
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


# ---------------------------------------------------------------------------
# STEP 5 — ASSEMBLE + WRITE
# ---------------------------------------------------------------------------

def to_insight(c: Corridor, enrichment: dict) -> dict:
    return {
        "id": f"{c.origin_code.lower()}-{c.dest_code.lower()}-{c.period.replace(' ', '').lower()}",
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
        "theme": enrichment.get("theme", "business"),
        "headline": enrichment.get("headline", f"{c.origin_name} → {c.dest_name} {c.delta_pct:+.1f}%"),
        "reading": enrichment.get("reading", ""),
        "confidence": enrichment.get("confidence", "low"),
    }


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set. Skipping enrichment.", file=sys.stderr)
        return 1

    corridors = fetch_corridors()
    ranked = rank_anomalies(corridors)
    print(f"[rank] {len(ranked)} corridors above threshold, out of {len(corridors)}")

    if not ranked:
        print("[rank] Nothing to enrich. Keeping existing data/insights.json.")
        return 0

    client = Anthropic(api_key=api_key)
    insights = []
    for i, c in enumerate(ranked, 1):
        try:
            enrichment = enrich(client, c)
            insights.append(to_insight(c, enrichment))
            print(f"[enrich] {i}/{len(ranked)}  {c.origin_name} → {c.dest_name}")
        except Exception as e:
            print(f"[enrich] FAILED {c.origin_name} → {c.dest_name}: {e}", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "meta": {
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "note": "Auto-generated by scripts/generate_insights.py",
        },
        "insights": insights,
    }, indent=2))
    print(f"[write] {len(insights)} insights → {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
