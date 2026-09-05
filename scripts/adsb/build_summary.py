"""
Fast layer: precompute everything the homepage needs into one small JSON file.

The website used to download ~5 MB of Parquet before it could render anything.
This builder collapses the headline numbers, the entity rollups and the detected
signals into a file of a few tens of kilobytes, so the first paint is immediate.
DuckDB-Wasm and the full flight table still load afterwards, lazily, for the
Explore and Ask layers.

It also runs the signal detection. A "signal" here is a claim about the data
with an explicit confidence level attached. We are deliberately conservative:
with only a few days of history almost nothing qualifies as more than an early
observation, and the output says so rather than implying a trend.

Writes:
    data/adsb/summary.json

Run:
    python scripts/adsb/build_summary.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("pip install duckdb", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import lookup_hex, lookup_reg  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "adsb"
OUT = DATA / "summary.json"
TAX = DATA / "taxonomy.json"

PERIOD_H = 24  # comparison window, hours

# Wide-body / long-haul airframes. Presence over Aarhus at cruise is the
# clearest available proxy for intercontinental corridor traffic.
WIDEBODY_PREFIXES = (
    "A30", "A31", "A33", "A34", "A35", "A38",
    "B74", "B76", "B77", "B78", "IL9", "MD11", "A124", "C5M",
)

# All-cargo operators. We attribute cargo at the OPERATOR level, not the
# airframe level: a passenger-configured 777 and a freighter share type codes,
# so claiming per-aircraft cargo status would overstate what we can see.
CARGO_PREFIXES = {
    "FDX": "FedEx Express",       "UPS": "UPS Airlines",
    "GTI": "Atlas Air",           "CLX": "Cargolux",
    "CKS": "Kalitta Air",         "ABW": "AirBridgeCargo",
    "CAO": "Air China Cargo",     "CKK": "China Cargo Airlines",
    "GEC": "Lufthansa Cargo",     "BOX": "AeroLogic",
    "SQC": "Singapore Air Cargo", "MPH": "Martinair Cargo",
    "TAY": "ASL Airlines Belgium","ICV": "Cargolux Italia",
    "RCF": "Aero Charter",        "QTR": None,  # passenger — excluded
    "CSN": None, "CES": None,
}
CARGO_PREFIXES = {k: v for k, v in CARGO_PREFIXES.items() if v}


# ── confidence model ─────────────────────────────────────────────────────────
def confidence(n: int, days: float, change_pct: float | None = None) -> str:
    """
    How much weight does this claim deserve?

    observed  - a direct count, no inference at all
    early     - real but the history is too short to call it a trend
    moderate  - enough history and volume to be interesting
    strong    - large, consistent, well-sampled
    """
    if change_pct is None:
        return "observed"
    if days < 14:
        return "early"
    if n < 30:
        return "early"
    if days >= 30 and n >= 100 and abs(change_pct) >= 20:
        return "strong"
    return "moderate"


def pct_change(cur: float, prev: float) -> float | None:
    if not prev:
        return None
    return (cur - prev) / prev * 100.0


def main() -> int:
    if not TAX.exists():
        print("run scripts/adsb/taxonomy.py first", file=sys.stderr)
        return 1
    tax = json.loads(TAX.read_text(encoding="utf-8"))
    blocks, countries = tax["blocks"], tax["countries"]

    con = duckdb.connect()
    flights_glob = str(DATA / "flights" / "*.parquet")
    if not list((DATA / "flights").glob("*.parquet")):
        print("no flight files yet", file=sys.stderr)
        return 1
    con.execute(
        f"CREATE VIEW f AS SELECT * FROM read_parquet('{flights_glob}', union_by_name=true)"
    )

    # Resolve every flight to a registration country in Python, then push the
    # mapping back into DuckDB as a table we can join against.
    rows = con.execute("SELECT hex, reg FROM f").fetchall()
    seen: dict[str, str] = {}
    for hx, reg in rows:
        if hx in seen:
            continue
        cc = lookup_hex(hx, blocks) or lookup_reg(reg or "")
        if cc:
            seen[hx] = cc
    con.execute("CREATE TABLE hex_cc (hex VARCHAR, cc VARCHAR)")
    if seen:
        con.executemany("INSERT INTO hex_cc VALUES (?, ?)", list(seen.items()))

    wb_pred = " OR ".join(f"ac_type LIKE '{p}%'" for p in WIDEBODY_PREFIXES)
    cargo_list = ", ".join(f"'{p}'" for p in CARGO_PREFIXES)

    con.execute(f"""
        CREATE VIEW fx AS
        SELECT f.*,
               h.cc                                              AS reg_cc,
               CASE WHEN {wb_pred} THEN 'widebody' ELSE 'narrowbody' END AS body,
               (f.airline_prefix IN ({cargo_list}))              AS is_cargo
        FROM f LEFT JOIN hex_cc h ON h.hex = f.hex
    """)

    lo, hi, total = con.execute(
        "SELECT MIN(first_seen), MAX(first_seen), COUNT(*) FROM fx"
    ).fetchone()
    span_days = (hi - lo) / 86400.0 if hi and lo else 0.0
    cur_lo, prev_lo = hi - PERIOD_H * 3600, hi - 2 * PERIOD_H * 3600

    # A period-over-period change is only meaningful when BOTH windows are
    # fully covered by the record. Early on, the baseline window reaches back
    # past the first observation, which makes every comparison look like a
    # dramatic increase — an artefact of when the receiver was switched on,
    # not a change in the sky. Detect that and refuse to compute the change
    # at all rather than publishing a number we would have to caveat away.
    baseline_complete = prev_lo >= lo

    def scalar(sql: str):
        return con.execute(sql).fetchone()[0]

    totals = {
        "flights":   total,
        "aircraft":  scalar("SELECT COUNT(DISTINCT hex) FROM fx"),
        "operators": scalar("SELECT COUNT(DISTINCT airline_prefix) FROM fx WHERE airline_prefix IS NOT NULL"),
        "countries": scalar("SELECT COUNT(DISTINCT reg_cc) FROM fx WHERE reg_cc IS NOT NULL"),
        "types":     scalar("SELECT COUNT(DISTINCT ac_type) FROM fx WHERE ac_type IS NOT NULL"),
        "widebody":  scalar("SELECT COUNT(*) FROM fx WHERE body='widebody'"),
        "cargo":     scalar("SELECT COUNT(*) FROM fx WHERE is_cargo"),
        "unresolved_country": scalar("SELECT COUNT(*) FROM fx WHERE reg_cc IS NULL"),
        "no_callsign":        scalar("SELECT COUNT(*) FROM fx WHERE callsign IS NULL"),
    }

    # ── entity rollups, each with a current-vs-previous comparison ───────────
    def rollup(dim: str, extra: str = "") -> list[dict]:
        q = f"""
            SELECT {dim} AS key,
                   COUNT(*)                                                    AS flights,
                   COUNT(DISTINCT hex)                                         AS aircraft,
                   SUM(CASE WHEN first_seen >= {cur_lo} THEN 1 ELSE 0 END)     AS cur,
                   SUM(CASE WHEN first_seen >= {prev_lo}
                             AND first_seen <  {cur_lo} THEN 1 ELSE 0 END)     AS prev,
                   SUM(CASE WHEN body='widebody' THEN 1 ELSE 0 END)            AS widebody,
                   SUM(CASE WHEN is_cargo THEN 1 ELSE 0 END)                   AS cargo,
                   MIN(first_seen)                                             AS first_ts,
                   MAX(first_seen)                                             AS last_ts
                   {extra}
            FROM fx WHERE {dim} IS NOT NULL AND {dim} != ''
            GROUP BY 1 ORDER BY 2 DESC
        """
        out = []
        for r in con.execute(q).fetchall():
            key, n, ac, cur, prev, wb, cg, fts, lts = r[:9]
            ch = pct_change(cur, prev) if baseline_complete else None
            out.append({
                "key": key, "flights": n, "aircraft": ac,
                "cur": cur, "prev": prev,
                "change_pct": round(ch, 1) if ch is not None else None,
                "widebody": wb, "cargo": cg,
                "share": round(n / total * 100, 1),
                "first_ts": fts, "last_ts": lts,
                "confidence": confidence(n, span_days, ch),
            })
        return out

    countries_roll = rollup("reg_cc")
    for c in countries_roll:
        meta = countries.get(c["key"], {})
        c["name"] = meta.get("name", c["key"])
        c["region"] = meta.get("region")
        c["continent"] = meta.get("continent")

    # Regions and continents are aggregates of the country rollup, so the
    # hierarchy stays consistent by construction rather than by a second query.
    def group_by(field: str) -> list[dict]:
        acc: dict[str, dict] = {}
        for c in countries_roll:
            k = c.get(field)
            if not k:
                continue
            a = acc.setdefault(k, {
                "key": k, "flights": 0, "aircraft": 0, "cur": 0, "prev": 0,
                "widebody": 0, "cargo": 0, "countries": [],
            })
            for m in ("flights", "aircraft", "cur", "prev", "widebody", "cargo"):
                a[m] += c[m]
            a["countries"].append(c["key"])
        out = []
        for a in acc.values():
            ch = pct_change(a["cur"], a["prev"]) if baseline_complete else None
            a["change_pct"] = round(ch, 1) if ch is not None else None
            a["share"] = round(a["flights"] / total * 100, 1)
            a["confidence"] = confidence(a["flights"], span_days, ch)
            out.append(a)
        return sorted(out, key=lambda x: -x["flights"])

    regions = group_by("region")
    continents = group_by("continent")

    operators = rollup("airline_prefix")
    op_names = dict(con.execute(
        "SELECT DISTINCT airline_prefix, airline FROM fx WHERE airline IS NOT NULL"
    ).fetchall())
    for o in operators:
        o["name"] = op_names.get(o["key"]) or CARGO_PREFIXES.get(o["key"])
        o["cargo_operator"] = o["key"] in CARGO_PREFIXES

    types = rollup("ac_type")
    type_desc = dict(con.execute(
        "SELECT DISTINCT ac_type, ac_desc FROM fx WHERE ac_desc IS NOT NULL"
    ).fetchall())
    for t in types:
        t["desc"] = type_desc.get(t["key"])
        t["widebody_type"] = t["key"].startswith(WIDEBODY_PREFIXES)

    # Track how much of each calendar day the receiver was actually up for.
    # The first and last day of any record are partial, and comparing a partial
    # day against a full one is the single easiest way to invent a fake trend.
    daily = []
    for d, n, a, w, dlo, dhi in con.execute("""
        SELECT strftime(TO_TIMESTAMP(first_seen), '%Y-%m-%d'), COUNT(*),
               COUNT(DISTINCT hex), SUM(CASE WHEN body='widebody' THEN 1 ELSE 0 END),
               MIN(first_seen), MAX(first_seen)
        FROM fx GROUP BY 1 ORDER BY 1
    """).fetchall():
        hours = (dhi - dlo) / 3600.0
        daily.append({
            "day": d, "flights": n, "aircraft": a, "widebody": w,
            "hours_covered": round(hours, 1),
            "partial": hours < 20.0,
        })
    hourly = [
        {"hour": int(h), "flights": n}
        for h, n in con.execute("""
            SELECT CAST(strftime(TO_TIMESTAMP(first_seen), '%H') AS INTEGER), COUNT(*)
            FROM fx GROUP BY 1 ORDER BY 1
        """).fetchall()
    ]

    signals = detect_signals(
        con, countries_roll, regions, operators, types, daily,
        span_days, total, cur_lo, prev_lo, totals, baseline_complete,
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": {"name": "Aarhus, Denmark", "lat": 56.16, "lng": 10.20},
        "window": {
            "first_ts": lo, "last_ts": hi,
            "days_observed": round(span_days, 2),
            "period_hours": PERIOD_H,
            "current_from": cur_lo, "previous_from": prev_lo,
            "baseline_complete": baseline_complete,
            "complete_days": sum(1 for d in daily if not d["partial"]),
        },
        "totals": totals,
        "signals": signals,
        "countries": countries_roll,
        "regions": regions,
        "continents": continents,
        "operators": operators[:60],
        "types": types[:60],
        "daily": daily,
        "hourly": hourly,
    }

    OUT.write_text(json.dumps(summary, separators=(",", ":")), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"[summary] {total:,} flights · {len(countries_roll)} countries · "
          f"{len(signals)} signals -> {OUT.name} ({kb:.1f} KB)")
    return 0


# ── signal detection ─────────────────────────────────────────────────────────
def detect_signals(con, countries, regions, operators, types, daily,
                   span_days, total, cur_lo, prev_lo, totals,
                   baseline_complete) -> list[dict]:
    """
    Turn the rollups into a ranked list of claims.

    Every signal carries the same shape: what changed, by how much, against
    which baseline, where it is concentrated, what it might mean, how sure we
    are, and the SQL that reproduces it. The interpretation field is always
    phrased as a possibility, never a conclusion.

    The `sql` field runs in the reader's browser, against the `flights` view
    index.html builds over the Parquet — not against the local `fx` view used
    here. Two consequences:

      * refer to the browser's column names (`reg_country`, `body`, `is_cargo`);
      * use `seen_at` for anything temporal, never `TO_TIMESTAMP(first_seen)`.
        duckdb-wasm ships without ICU, so `strftime()` cannot bind against a
        TIMESTAMP WITH TIME ZONE. This fails only in the browser, so a local
        test of the same query will not catch it.
    """
    sig: list[dict] = []

    def add(**kw):
        kw.setdefault("confidence", "observed")
        sig.append(kw)

    full_days = [d for d in daily if not d["partial"]]

    # 0) State of the record. When there is not yet enough history to compare
    #    periods, that IS the headline — publishing invented trends instead
    #    would be the single fastest way to make this product untrustworthy.
    if not baseline_complete:
        add(
            kind="coverage", scope="all",
            title="No trend comparisons yet — the baseline is still being built",
            metric=f"{len(full_days)}", metric_label="complete days recorded",
            comparison=f"{span_days:.1f} days of observations so far",
            where=None,
            interpretation="Period-over-period comparison needs two full, equal "
                           "windows. The record does not yet reach back far enough, so "
                           "every change figure would be measuring when the receiver "
                           "was switched on rather than anything about air traffic.",
            caveat="Composition and counts below are directly observed and stand on "
                   "their own. Change figures are deliberately withheld until the "
                   "baseline supports them.",
            confidence="observed",
            sql=("SELECT strftime(seen_at, '%Y-%m-%d') AS day,\n"
                 "       COUNT(*) AS flights,\n"
                 "       (MAX(first_seen) - MIN(first_seen)) / 3600.0 AS hours_covered\n"
                 "FROM flights GROUP BY 1 ORDER BY 1;"),
        )

    # 1) Busiest day on record — complete days only.
    if len(full_days) >= 2:
        peak = max(full_days, key=lambda d: d["flights"])
        others = [d["flights"] for d in full_days if d["day"] != peak["day"]]
        avg = sum(others) / len(others) if others else 0
        add(
            kind="record", scope="all",
            title=f"Busiest full day observed: {peak['day']}",
            metric=f"{peak['flights']:,}", metric_label="flights",
            comparison=f"vs {avg:,.0f} average across {len(others)} other "
                       f"full day{'s' if len(others) != 1 else ''}" if avg else None,
            where=None,
            interpretation="Day-to-day variation at this stage reflects weather, "
                           "receiver uptime and normal schedule variation as much "
                           "as anything about demand.",
            caveat="Partial days are excluded from this comparison.",
            confidence="observed",
            sql=("SELECT strftime(seen_at, '%Y-%m-%d') AS day,\n"
                 "       COUNT(*) AS flights\n"
                 "FROM flights GROUP BY 1 ORDER BY 2 DESC;"),
        )

    # 2) Corridor composition — the structural fact that makes this antenna
    #    interesting. Aarhus sits under long-haul routings, so a meaningful
    #    share of what passes overhead is not going anywhere near Denmark.
    non_nordic = [c for c in countries if c.get("region") != "Nordics"]
    nn = sum(c["flights"] for c in non_nordic)
    if total:
        add(
            kind="composition", scope="all",
            title="Most aircraft overhead are not Nordic-registered",
            metric=f"{nn / total * 100:.0f}%", metric_label="of observed flights",
            comparison=f"{nn:,} of {total:,} flights",
            where=", ".join(c["name"] for c in non_nordic[:4]) or None,
            interpretation="Aarhus sits beneath routings between Northern Europe and "
                           "the rest of the world. A large non-Nordic share is evidence "
                           "the antenna is seeing corridor traffic, not just local movements.",
            caveat="Registration country is where an aircraft is registered — not "
                   "where the flight began or where it is going.",
            confidence="observed",
            sql=("SELECT reg_country, COUNT(*) AS flights\n"
                 "FROM flights GROUP BY 1 ORDER BY 2 DESC;"),
        )

    # 3) Largest movers, country level. Requires presence in both windows.
    movers = [
        c for c in countries
        if c["change_pct"] is not None and c["prev"] >= 5 and c["cur"] >= 5
    ]
    movers.sort(key=lambda c: -abs(c["change_pct"]))
    for c in movers[:3]:
        up = c["change_pct"] > 0
        add(
            kind="shift", scope="country", entity=c["key"],
            title=f"{c['name']}-registered traffic {'up' if up else 'down'} "
                  f"{abs(c['change_pct']):.0f}%",
            metric=f"{c['change_pct']:+.0f}%", metric_label="vs previous 24h",
            comparison=f"{c['cur']} flights vs {c['prev']}",
            where=c.get("region"),
            interpretation=(
                f"Movement in {c['name']}-registered aircraft can reflect schedule "
                "changes, aircraft rotation, weather routing, or genuine changes in "
                "activity. At this sample size the first three are more likely."
            ),
            caveat="A 24-hour comparison on a few days of history is noise-dominated. "
                   "Treat as something to watch, not a finding.",
            confidence=c["confidence"],
            sql=(f"SELECT strftime(seen_at, '%Y-%m-%d') AS day,\n"
                 f"       COUNT(*) AS flights\n"
                 f"FROM flights WHERE reg_country = '{c['key']}'\n"
                 f"GROUP BY 1 ORDER BY 1;"),
        )

    # 4) Long-haul presence from a distant country — the alternative-data hook.
    for c in countries:
        if c.get("continent") in ("Europe", None):
            continue
        if c["flights"] < 15:
            continue
        wb_share = c["widebody"] / c["flights"] * 100 if c["flights"] else 0
        add(
            kind="corridor", scope="country", entity=c["key"],
            title=f"{c['name']}-registered aircraft crossing overhead",
            metric=f"{c['flights']:,}", metric_label="flights observed",
            comparison=f"{c['aircraft']} distinct aircraft · "
                       f"{wb_share:.0f}% wide-body",
            where=c.get("region"),
            interpretation=(
                f"{c['name']} has no scheduled service to Aarhus. These are aircraft "
                "at cruise altitude on intercontinental routings that happen to pass "
                "through this antenna's range. The count is a proxy for how busy that "
                "corridor is."
            ),
            caveat="Corridor routings shift with winds, airspace closures and slot "
                   "times. Volume here is not the same as trade or passenger volume.",
            confidence="observed",
            sql=(f"SELECT callsign, ac_type, reg, seen_at\n"
                 f"FROM flights WHERE reg_country = '{c['key']}'\n"
                 f"ORDER BY first_seen DESC;"),
        )
        if len([s for s in sig if s["kind"] == "corridor"]) >= 2:
            break

    # 5) Wide-body share — a capacity signal distinct from a flight count.
    wb = totals["widebody"]
    if total and wb:
        add(
            kind="capacity", scope="all",
            title="Wide-body share of traffic",
            metric=f"{wb / total * 100:.0f}%", metric_label="of observed flights",
            comparison=f"{wb:,} wide-body movements",
            where=None,
            interpretation="Wide-body airframes indicate long-haul routings. Tracking "
                           "this share separately from the flight count matters: the "
                           "same number of flights carrying larger aircraft is more "
                           "capacity, not flat activity.",
            caveat="Airframe type is identified from the aircraft's registered type "
                   "code. It does not tell us the cabin configuration or how full it is.",
            confidence="observed",
            sql=("SELECT ac_type, ac_desc, COUNT(*) AS flights\n"
                 "FROM flights\n"
                 "WHERE ac_type SIMILAR TO '(A33|A35|A38|B74|B77|B78).*'\n"
                 "GROUP BY 1,2 ORDER BY 3 DESC;"),
        )

    # 6) All-cargo operators. Small sample — say so plainly.
    cargo_ops = [o for o in operators if o.get("cargo_operator")]
    cg = sum(o["flights"] for o in cargo_ops)
    if cg:
        add(
            kind="cargo", scope="all",
            title="All-cargo operator movements",
            metric=f"{cg:,}", metric_label="flights",
            comparison=f"{len(cargo_ops)} operators · {cg / total * 100:.1f}% of traffic",
            where=", ".join(o.get("name") or o["key"] for o in cargo_ops[:4]),
            interpretation="Freighter movements are the observation closest to trade "
                           "flow. Sustained change in this count would be the most "
                           "economically meaningful signal this antenna can produce.",
            caveat=f"Only {cg} flights so far. Far too few to support any claim about "
                   "trade. Included to establish the baseline, not to draw a conclusion.",
            confidence="early" if cg < 200 else "moderate",
            sql=("SELECT airline_prefix, COUNT(*) AS flights\n"
                 "FROM flights\n"
                 "WHERE airline_prefix IN "
                 "('FDX','UPS','GTI','CLX','CKS','ABW','CAO','CKK','GEC','BOX')\n"
                 "GROUP BY 1 ORDER BY 2 DESC;"),
        )

    # 7) Rare airframes — a genuinely interesting "look at this" observation.
    rare = [t for t in types if t["flights"] == 1 and t.get("widebody_type")]
    if rare:
        names = ", ".join(f"{t['key']}" for t in rare[:5])
        add(
            kind="outlier", scope="all",
            title=f"{len(rare)} wide-body types seen exactly once",
            metric=f"{len(rare)}", metric_label="one-off airframes",
            comparison=names,
            where=None,
            interpretation="Single appearances of long-haul airframes are usually "
                           "charters, repositioning flights, or routings diverted off "
                           "their usual track.",
            caveat="One observation is an anecdote. These are listed because they are "
                   "interesting, not because they mean anything yet.",
            confidence="observed",
            sql=("SELECT ac_type, ac_desc, callsign, seen_at\n"
                 "FROM flights\n"
                 "WHERE ac_type IN (SELECT ac_type FROM flights\n"
                 "                  GROUP BY 1 HAVING COUNT(*) = 1)\n"
                 "ORDER BY first_seen DESC;"),
        )

    order = {"strong": 0, "moderate": 1, "observed": 2, "early": 3}
    sig.sort(key=lambda s: order.get(s["confidence"], 9))
    for i, s in enumerate(sig):
        s["id"] = f"sig-{i+1}"
    return sig


if __name__ == "__main__":
    raise SystemExit(main())
