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
#
# "A310", not "A31": a prefix match on A31 also catches the A318 and A319,
# which are narrow-bodies. It did, for months, and inflated the wide-body
# count — the site's long-haul proxy — by about a sixth.
WIDEBODY_PREFIXES = (
    "A30", "A310", "A33", "A34", "A35", "A38",
    "B74", "B76", "B77", "B78", "IL9", "MD11", "A124", "C5M",
)

# Who is flying, and what kind of flying it is. Lives in data/adsb/carriers.json
# so the site can read the same judgement — see scripts/adsb/carriers.py.
# Cargo is attributed at the OPERATOR level, never the airframe: a
# passenger-configured 777 and a freighter share a type code.
CARRIERS_FILE = DATA / "carriers.json"


def load_carriers() -> tuple[dict, dict]:
    if not CARRIERS_FILE.exists():
        print("[carriers] missing — run scripts/adsb/carriers.py", file=sys.stderr)
        return {}, {}
    doc = json.loads(CARRIERS_FILE.read_text(encoding="utf-8"))
    return doc.get("carriers", {}), doc.get("kinds", {})


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

    carriers, kind_meta = load_carriers()
    con.execute("CREATE TABLE carrier (prefix VARCHAR, op_name VARCHAR, kind VARCHAR)")
    if carriers:
        con.executemany("INSERT INTO carrier VALUES (?, ?, ?)",
                        [(p, v["name"], v["kind"]) for p, v in carriers.items()])

    wb_pred = " OR ".join(f"ac_type LIKE '{p}%'" for p in WIDEBODY_PREFIXES)

    # `dow` and `daytype` exist because one full week is the first thing this
    # record can honestly describe. A Tuesday and a Sunday are not two samples
    # of the same thing, and averaging them hides the only structure there is.
    con.execute(f"""
        CREATE VIEW fx AS
        SELECT f.*,
               h.cc                                              AS reg_cc,
               c.op_name                                         AS carrier_name,
               c.kind                                            AS carrier_kind,
               CASE WHEN {wb_pred} THEN 'widebody' ELSE 'narrowbody' END AS body,
               (c.kind = 'cargo')                                AS is_cargo,
               make_timestamp(f.first_seen * 1000000)            AS seen_at,
               strftime(make_timestamp(f.first_seen * 1000000), '%Y-%m-%d') AS day,
               strftime(make_timestamp(f.first_seen * 1000000), '%a')       AS dow,
               CASE WHEN strftime(make_timestamp(f.first_seen * 1000000), '%a')
                         IN ('Sat', 'Sun') THEN 'weekend' ELSE 'weekday' END AS daytype
        FROM f
        LEFT JOIN hex_cc h ON h.hex = f.hex
        LEFT JOIN carrier c ON c.prefix = f.airline_prefix
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
        # The curated table wins over whatever was baked into the flight rows:
        # it is the one we can correct without regenerating months of Parquet.
        meta = carriers.get(o["key"], {})
        o["name"] = meta.get("name") or op_names.get(o["key"])
        o["kind"] = meta.get("kind")
        o["cargo_operator"] = meta.get("kind") == "cargo"

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

    week = build_week(con, daily, kind_meta)

    signals = detect_signals(
        con, countries_roll, regions, operators, types, daily,
        span_days, total, cur_lo, prev_lo, totals, baseline_complete, week,
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
        "week": week,
    }

    OUT.write_text(json.dumps(summary, separators=(",", ":")), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"[summary] {total:,} flights · {len(countries_roll)} countries · "
          f"{len(signals)} signals -> {OUT.name} ({kb:.1f} KB)")
    return 0


# ── the week ─────────────────────────────────────────────────────────────────
DOW_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def build_week(con, daily, kind_meta) -> dict | None:
    """
    Seven consecutive complete days, one of each weekday.

    This is the first structure the record can honestly carry. A single week
    cannot show a trend — that needs two — but it can show *shape*, and shape is
    where the economics live: freight runs on the working week, holidays run on
    the weekend, and a total that averages the two shows neither.

    Returns None until seven consecutive complete days exist, rather than
    describing "a week" out of five days and a shrug.
    """
    full = [d["day"] for d in daily if not d["partial"]]
    if len(full) < 7:
        return None
    days = full[-7:]
    start, end = datetime.fromisoformat(days[0]), datetime.fromisoformat(days[-1])
    if (end - start).days != 6:            # a gap in the middle is not a week
        return None

    lo, hi = days[0], days[-1]
    scope = f"day BETWEEN '{lo}' AND '{hi}'"

    per_day = [
        {"day": d, "dow": dow, "flights": n, "widebody": wb, "cargo": cg,
         "kinds": {}}
        for d, dow, n, wb, cg in con.execute(f"""
            SELECT day, dow, COUNT(*),
                   SUM(CASE WHEN body = 'widebody' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_cargo THEN 1 ELSE 0 END)
            FROM fx WHERE {scope} GROUP BY 1, 2 ORDER BY 1""").fetchall()
    ]
    by_day = {d["day"]: d for d in per_day}
    for d, k, n in con.execute(f"""
            SELECT day, COALESCE(carrier_kind, 'unclassified'), COUNT(*)
            FROM fx WHERE {scope} GROUP BY 1, 2""").fetchall():
        by_day[d]["kinds"][k] = n

    kinds = []
    for k, n, wkday, wkend, wb in con.execute(f"""
            SELECT COALESCE(carrier_kind, 'unclassified') AS k, COUNT(*),
                   SUM(CASE WHEN daytype = 'weekday' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN daytype = 'weekend' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN body = 'widebody' THEN 1 ELSE 0 END)
            FROM fx WHERE {scope} GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        wd, we = wkday / 5.0, wkend / 2.0
        meta = kind_meta.get(k, {})
        kinds.append({
            "key": k,
            "label": meta.get("label") or k.title(),
            "reads": meta.get("reads"),
            "flights": n,
            "widebody": wb,
            "weekday_per_day": round(wd, 1),
            "weekend_per_day": round(we, 1),
            "lift_pct": round((wd / we - 1) * 100) if we else None,
            # A ratio between two directly counted groups is observed. Whether
            # it holds is a different question, and one week cannot answer it.
            "confidence": "observed" if n >= 200 else "early",
        })

    hourly = [
        {"hour": int(h), "weekday": round(wd / 5.0, 1), "weekend": round(we / 2.0, 1)}
        for h, wd, we in con.execute(f"""
            SELECT CAST(strftime(seen_at, '%H') AS INTEGER),
                   SUM(CASE WHEN daytype = 'weekday' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN daytype = 'weekend' THEN 1 ELSE 0 END)
            FROM fx WHERE {scope} GROUP BY 1 ORDER BY 1""").fetchall()
    ]

    total, classified, anon = con.execute(f"""
        SELECT COUNT(*),
               SUM(CASE WHEN carrier_kind IS NOT NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN airline_prefix IS NULL THEN 1 ELSE 0 END)
        FROM fx WHERE {scope}""").fetchone()

    wd_total = sum(d["flights"] for d in per_day if d["dow"] not in ("Sat", "Sun"))
    we_total = sum(d["flights"] for d in per_day if d["dow"] in ("Sat", "Sun"))

    return {
        "from": lo, "to": hi,
        "days": per_day,
        "kinds": kinds,
        "hourly": hourly,
        "totals": {
            "flights": total,
            "weekday_per_day": round(wd_total / 5.0, 1),
            "weekend_per_day": round(we_total / 2.0, 1),
            "lift_pct": round((wd_total / 5.0) / (we_total / 2.0) * 100 - 100)
                        if we_total else None,
        },
        "coverage": {
            "classified": classified,
            "share": round(100.0 * classified / total, 1) if total else 0,
            "no_callsign": anon,
        },
    }


# ── signal detection ─────────────────────────────────────────────────────────
def detect_signals(con, countries, regions, operators, types, daily,
                   span_days, total, cur_lo, prev_lo, totals,
                   baseline_complete, week=None) -> list[dict]:
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

    # ── What a full week shows ───────────────────────────────────────────
    # These are the first readings on this site that are about the economy
    # rather than about the antenna. They are still composition, not trend:
    # one week can say what a week looks like and nothing about whether it is
    # changing.
    if week:
        kinds = {k["key"]: k for k in week["kinds"]}
        wt = week["totals"]
        span = f"{week['from']} to {week['to']}"

        cargo, charter = kinds.get("cargo"), kinds.get("leisure")
        if cargo and charter and wt["lift_pct"] is not None:
            add(
                kind="composition", scope="all",
                title="The working week barely changes how much flies — "
                      "it changes what",
                metric=f"{wt['lift_pct']:+d}%", metric_label="weekday vs weekend traffic",
                comparison=f"{wt['weekday_per_day']:,.0f} flights a weekday vs "
                           f"{wt['weekend_per_day']:,.0f} at the weekend",
                where=span,
                interpretation=(
                    "The total is almost flat, which is what you would expect over a "
                    "point that mostly sees aircraft at cruise. Underneath it the mix "
                    f"moves hard in both directions: freight runs {cargo['lift_pct']:+d}% "
                    f"on weekdays while charter runs {charter['lift_pct']:+d}%. Averaging "
                    "a Tuesday with a Sunday cancels the two against each other and "
                    "reports nothing. The composition is the signal; the count is not."
                ),
                caveat="One week. This describes the shape of a week, not a change in "
                       "it — that needs a second week to compare against.",
                confidence="observed",
                sql=("SELECT daytype, carrier_kind, COUNT(*) AS flights\n"
                     "FROM flights\n"
                     f"WHERE day BETWEEN '{week['from']}' AND '{week['to']}'\n"
                     "GROUP BY 1, 2 ORDER BY 1, 3 DESC;"),
            )

        if cargo and cargo["lift_pct"] is not None:
            by_dow = {d["dow"]: d["kinds"].get("cargo", 0) for d in week["days"]}
            path = " → ".join(f"{d} {by_dow.get(d, 0)}" for d in DOW_ORDER)
            add(
                kind="corridor", scope="all",
                title="Freight keeps office hours",
                metric=f"{cargo['lift_pct']:+d}%",
                metric_label="all-cargo flights, weekday vs weekend",
                comparison=f"{cargo['weekday_per_day']} a weekday vs "
                           f"{cargo['weekend_per_day']} at the weekend",
                where=path,
                interpretation=(
                    "Freighters follow the working week of the businesses that load "
                    "them, and the count climbs through it before easing on Friday. "
                    "This is the closest thing overhead to a trade figure: it is "
                    "capacity moving because somebody has goods to move."
                ),
                caveat="Cargo is attributed by operator, not by aircraft. A freighter "
                       "flown by a passenger airline is not counted here, and belly "
                       "freight under passengers is invisible to us entirely.",
                confidence=cargo["confidence"],
                sql=("SELECT dow, COUNT(*) AS freight_flights\n"
                     "FROM flights\n"
                     f"WHERE carrier_kind = 'cargo'\n"
                     f"  AND day BETWEEN '{week['from']}' AND '{week['to']}'\n"
                     "GROUP BY 1;"),
            )

        if charter and charter["lift_pct"] is not None and charter["lift_pct"] < 0:
            add(
                kind="composition", scope="all",
                title="Holiday flying is a weekend business",
                metric=f"{charter['lift_pct']:+d}%",
                metric_label="charter flights, weekday vs weekend",
                comparison=f"{charter['weekend_per_day']} a weekend day vs "
                           f"{charter['weekday_per_day']} on a weekday",
                where=span,
                interpretation=(
                    "Tour-operator flying is the purest leisure demand in the record — "
                    "it exists because somebody booked a holiday. It runs opposite to "
                    "freight, which is why the two cancel in the headline count."
                ),
                caveat=f"Only {charter['flights']} charter flights in the week. The "
                       "direction is clear; the size of it is not, at this sample.",
                confidence=charter["confidence"],
                sql=("SELECT dow, COUNT(*) AS charter_flights\n"
                     "FROM flights\n"
                     f"WHERE carrier_kind = 'leisure'\n"
                     f"  AND day BETWEEN '{week['from']}' AND '{week['to']}'\n"
                     "GROUP BY 1;"),
            )

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
            title=f"{len(rare)} wide-body type{'s' if len(rare) != 1 else ''} "
                  f"seen exactly once",
            metric=f"{len(rare)}",
            metric_label=f"one-off airframe{'s' if len(rare) != 1 else ''}",
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
