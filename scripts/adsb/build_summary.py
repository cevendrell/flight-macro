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
import random
import re
import sys
from datetime import datetime, timedelta, timezone
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


# Wide-body / long-haul airframes. Presence over Aarhus at cruise is the
# clearest available proxy for intercontinental corridor traffic.
#
# "A310", not "A31": a prefix match on A31 also catches the A318 and A319,
# which are narrow-bodies. It did, for months, and inflated the wide-body
# count - the site's long-haul proxy - by about a sixth.
WIDEBODY_PREFIXES = (
    "A30", "A310", "A33", "A34", "A35", "A38",
    "B74", "B76", "B77", "B78", "IL9", "MD11", "A124", "C5M",
)

# Who is flying, and what kind of flying it is. Lives in data/adsb/carriers.json
# so the site can read the same judgement - see scripts/adsb/carriers.py.
# Cargo is attributed at the OPERATOR level, never the airframe: a
# passenger-configured 777 and a freighter share a type code.
CARRIERS_FILE = DATA / "carriers.json"


def load_carriers() -> tuple[dict, dict, list]:
    if not CARRIERS_FILE.exists():
        print("[carriers] missing - run scripts/adsb/carriers.py", file=sys.stderr)
        return {}, {}, []
    doc = json.loads(CARRIERS_FILE.read_text(encoding="utf-8"))
    return doc.get("carriers", {}), doc.get("kinds", {}), doc.get("bizjet_types", [])


# ── how much of the record ships in the summary ──────────────────────────────
# The summary is the site's first paint, so it has to stay small however long
# the record gets. Everything that grows with time is cut to a recent window
# here; the full history stays in the Parquet, one query away in Ask.
WEEKS_DETAIL = 26        # weeks with a full day/kind/hour breakdown (the Week page)
DAYS_DETAIL = 120        # days in the daily series and the weekday table
SPARK_DAYS = 84          # days in the sparkline beside a signal
MACRO_WEEKS = 26         # weeks in a macro gauge's series
SKY_DAYS = 365           # the receiver footprint is drawn from the last year


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
    rows = con.execute("SELECT DISTINCT hex, reg FROM f").fetchall()
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

    carriers, kind_meta, bizjet_types = load_carriers()
    con.execute("CREATE TABLE carrier (prefix VARCHAR, op_name VARCHAR, kind VARCHAR, "
                "country VARCHAR)")
    if carriers:
        con.executemany("INSERT INTO carrier VALUES (?, ?, ?, ?)",
                        [(p, v["name"], v["kind"], v.get("country"))
                         for p, v in carriers.items()])
    con.execute("CREATE TABLE cmeta (cc VARCHAR, region VARCHAR, continent VARCHAR)")
    con.executemany("INSERT INTO cmeta VALUES (?, ?, ?)",
                    [(cc, m.get("region"), m.get("continent")) for cc, m in countries.items()])
    biz_list = ", ".join(f"'{t}'" for t in bizjet_types) or "''"

    wb_pred = " OR ".join(f"ac_type LIKE '{p}%'" for p in WIDEBODY_PREFIXES)

    # `dow` and `daytype` exist because one full week is the first thing this
    # record can honestly describe. A Tuesday and a Sunday are not two samples
    # of the same thing, and averaging them hides the only structure there is.
    # Route-derived columns, mirrored exactly in the browser's `flights` view
    # so a signal's SQL runs the same here and there. Guarded: a flights file
    # built before routes existed has no origin column and must still build.
    fcols = {r[0] for r in con.execute("DESCRIBE f").fetchall()}
    apf = DATA / "enrichment" / "airports.parquet"
    have_routes = "origin" in fcols and apf.exists()
    if have_routes:
        con.execute(f"""
            CREATE OR REPLACE VIEW ap AS
            SELECT ident, name, municipality AS city, iso_country AS cc,
                   latitude_deg AS lat, longitude_deg AS lng
            FROM read_parquet('{apf}')""")
    route_derived = ("""
               (f.origin IS NOT NULL
                AND NOT (f.origin LIKE 'EK%' OR f.destination LIKE 'EK%'))  AS is_overflight,
               CASE WHEN f.origin IS NOT NULL AND f.destination IS NOT NULL
                    THEN LEAST(f.origin, f.destination) || '-' || GREATEST(f.origin, f.destination)
               END                                                          AS pair,
               ao.cc                                                        AS origin_cc,
               ad.cc                                                        AS destination_cc,
               -- The world region at the far end of a route that leaves
               -- Europe. NULL for a flight inside Europe, or with no route.
               CASE WHEN mo.continent <> 'Europe' THEN mo.region
                    WHEN md.continent <> 'Europe' THEN md.region END        AS far_region,"""
        if have_routes else """
               NULL AS is_overflight, NULL AS pair, NULL AS origin_cc, NULL AS destination_cc,
               NULL AS far_region,""")
    route_joins = ("LEFT JOIN ap ao ON ao.ident = f.origin\n"
                   "        LEFT JOIN ap ad ON ad.ident = f.destination\n"
                   "        LEFT JOIN cmeta mo ON mo.cc = ao.cc\n"
                   "        LEFT JOIN cmeta md ON md.cc = ad.cc" if have_routes else "")
    # A table, not a view. Every figure below is a query against this, and as
    # a view each one re-read every Parquet file and re-ran the joins: a few
    # hundred scans of the whole record per build, growing with every month.
    # Materialised once it is one scan, and a year of flights is ~100 MB.
    con.execute(f"""
        CREATE TABLE fx AS
        SELECT f.*,{route_derived}
               h.cc                                              AS reg_cc,
               c.op_name                                         AS carrier_name,
               c.kind                                            AS carrier_kind,
               c.country                                         AS carrier_country,
               -- Business aviation is read off the airframe (see carriers.py):
               -- a state or cargo operator flying one is not business travel.
               COALESCE(f.ac_type IN ({biz_list})
                        AND (c.kind IS NULL OR c.kind = 'bizjet'), FALSE) AS is_bizav,
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
        {route_joins}
    """)

    lo, hi, total = con.execute(
        "SELECT MIN(first_seen), MAX(first_seen), COUNT(*) FROM fx"
    ).fetchone()
    span_days = (hi - lo) / 86400.0 if hi and lo else 0.0

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

    # Every "change" figure on the site - per country, region, operator and
    # type, and the shift signals built from them - compares the latest
    # complete week against the complete week before it. It used to compare
    # the last 24 hours with the 24 before, which on this data is mostly a
    # measure of which weekday each window landed on: a Wednesday against a
    # Tuesday, on a handful of flights, reported as a 67% move. A week against
    # a week holds one of every weekday on each side, so what is left is
    # change. Until two such weeks exist the comparison is withheld entirely,
    # and the site says on what date it will start.
    compare = weekly_compare(daily)
    # ...unless the two weeks sit either side of a schedule change. Airlines
    # switch timetables on the last Sunday of March and of October, and a week
    # of one against a week of the other measures the calendar. The windows
    # are still published so the page can say why the column is empty.
    baseline_complete = compare is not None and not compare["schedule_change"]

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
        in_cur = (f"day BETWEEN '{compare['cur_from']}' AND '{compare['cur_to']}'"
                  if baseline_complete else "FALSE")
        in_prev = (f"day BETWEEN '{compare['prev_from']}' AND '{compare['prev_to']}'"
                   if baseline_complete else "FALSE")
        q = f"""
            SELECT {dim} AS key,
                   COUNT(*)                                                    AS flights,
                   COUNT(DISTINCT hex)                                         AS aircraft,
                   SUM(CASE WHEN {in_cur}  THEN 1 ELSE 0 END)                  AS cur,
                   SUM(CASE WHEN {in_prev} THEN 1 ELSE 0 END)                  AS prev,
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

    hourly = [
        {"hour": int(h), "flights": n}
        for h, n in con.execute("""
            SELECT CAST(strftime(TO_TIMESTAMP(first_seen), '%H') AS INTEGER), COUNT(*)
            FROM fx GROUP BY 1 ORDER BY 1
        """).fetchall()
    ]

    weeks = build_weeks(con, daily, kind_meta)
    week = weeks[0] if weeks else None
    months = build_months(con, daily)
    trend = build_trend(daily)
    sky = build_sky(con)
    routes = build_routes(con, countries, compare if baseline_complete else None)
    macro = build_macro(con, daily, routes, carriers)

    signals = detect_signals(
        con, countries_roll, regions, operators, types, daily,
        span_days, total, compare, totals, baseline_complete, week, routes, macro,
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": {"name": "Aarhus, Denmark", "lat": 56.16, "lng": 10.20},
        "window": {
            "first_ts": lo, "last_ts": hi,
            "days_observed": round(span_days, 2),
            "baseline_complete": baseline_complete,
            "complete_days": sum(1 for d in daily if not d["partial"]),
            # What every change_pct on the site is measured across. None until
            # two complete weeks exist; the page uses this to label the column
            # and to say when comparisons will begin.
            "compare": compare,
        },
        "totals": totals,
        "signals": signals,
        "countries": countries_roll,
        "regions": regions,
        "continents": continents,
        "operators": operators[:60],
        "types": types[:60],
        "daily": daily[-DAYS_DETAIL:],
        "hourly": hourly,
        "week": week,
        "weeks": weeks,
        "months": months,
        "trend": trend,
        "sky": sky,
        "routes": routes,
        "macro": macro,
    }

    OUT.write_text(json.dumps(summary, separators=(",", ":")), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"[summary] {total:,} flights · {len(countries_roll)} countries · "
          f"{len(signals)} signals · {len(weeks)} week(s) · "
          f"{len(months['complete'])} month(s) -> {OUT.name} ({kb:.1f} KB)")
    return 0


# ── the week ─────────────────────────────────────────────────────────────────
DOW_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def weekly_compare(daily) -> dict | None:
    """
    The two windows every change figure is measured across: the newest seven
    consecutive complete days, and the seven consecutive complete days before
    them. Returns None unless both exist in full - a partial baseline is what
    turns "the receiver was switched on" into a 200% increase.
    """
    have = {d["day"] for d in daily if not d["partial"]}
    if len(have) < 14:
        return None
    newest = datetime.fromisoformat(max(have))
    days = [(newest - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    if not all(d in have for d in days):
        return None
    # A schedule change anywhere inside the fourteen days means the two weeks
    # are not flown to the same timetable.
    s0, s1 = season_of(days[0]), season_of(days[13])
    return {"kind": "week",
            "prev_from": days[0], "prev_to": days[6],
            "cur_from": days[7], "cur_to": days[13],
            "schedule_change": s1["from"] if s0["code"] != s1["code"] else None}


# ── schedule seasons ─────────────────────────────────────────────────────────
def _last_sunday(year: int, month: int) -> datetime:
    d = datetime(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def season_of(day: str) -> dict:
    """
    The airline schedule season a day falls in.

    Airlines worldwide publish two timetables a year and switch between them
    on the same two dates: the summer schedule starts on the last Sunday of
    March, the winter schedule on the last Sunday of October. Across either
    date thousands of routes start or stop at once. That step is the calendar,
    not the economy, and it is by far the largest move in this record - so
    every comparison on the site is made inside one season, or against the
    same weeks a year earlier, and never across the switch.
    """
    d = datetime.fromisoformat(day)
    summer, winter = _last_sunday(d.year, 3), _last_sunday(d.year, 10)
    if summer <= d < winter:
        lo, hi, code = summer, winter, f"S{d.year % 100:02d}"
        label = f"summer {d.year} schedule"
    elif d >= winter:
        lo, hi, code = winter, _last_sunday(d.year + 1, 3), f"W{d.year % 100:02d}"
        label = f"winter {d.year}/{(d.year + 1) % 100:02d} schedule"
    else:
        lo, hi, code = _last_sunday(d.year - 1, 10), summer, f"W{(d.year - 1) % 100:02d}"
        label = f"winter {d.year - 1}/{d.year % 100:02d} schedule"
    return {"code": code, "label": label, "from": lo.strftime("%Y-%m-%d"),
            "to": (hi - timedelta(days=1)).strftime("%Y-%m-%d")}


def build_routes(con, countries_meta: dict, compare: dict | None) -> dict | None:
    """
    Where the flights overhead actually begin and end.

    reconstruct.py joins each callsign to its scheduled route and keeps it only
    when the aircraft was observed heading toward that destination. Everything
    here counts those flights: the airports they touch, the city-pairs they
    join, the countries at each end, and the compass direction of travel for
    every positioned flight - routed or not. Coverage is reported so the page
    can say what share of the sky this describes.
    """
    cols = {r[0] for r in con.execute("DESCRIBE fx").fetchall()}
    apf = DATA / "enrichment" / "airports.parquet"
    if "origin" not in cols or not apf.exists():
        return None
    con.execute(f"""
        CREATE OR REPLACE VIEW ap AS
        SELECT ident, name, municipality AS city, iso_country AS cc,
               latitude_deg AS lat, longitude_deg AS lng
        FROM read_parquet('{apf}')""")

    total = con.execute("SELECT COUNT(*) FROM fx").fetchone()[0]
    cov = dict(con.execute(
        "SELECT COALESCE(route_check, 'none'), COUNT(*) FROM fx GROUP BY 1").fetchall())
    routed = con.execute("SELECT COUNT(*) FROM fx WHERE origin IS NOT NULL").fetchone()[0]
    touch_dk = con.execute("""
        SELECT COUNT(*) FROM fx
        WHERE origin IS NOT NULL AND (origin LIKE 'EK%' OR destination LIKE 'EK%')
    """).fetchone()[0]

    in_cur = (f"day BETWEEN '{compare['cur_from']}' AND '{compare['cur_to']}'"
              if compare else "FALSE")
    in_prev = (f"day BETWEEN '{compare['prev_from']}' AND '{compare['prev_to']}'"
               if compare else "FALSE")

    def change(cur, prev):
        ch = pct_change(cur, prev) if compare else None
        return round(ch, 1) if ch is not None else None

    # Airports, by flights touching them. A Copenhagen-Aalborg flight counts
    # once for each end, so these are touches, not a partition of the record.
    airports, index = [], {}
    for k, n, dep, arr, cur, prev, name, city, cc, lat, lng in con.execute(f"""
        WITH t AS (
            SELECT origin AS k, 1 AS dep, 0 AS arr, day FROM fx WHERE origin IS NOT NULL
            UNION ALL
            SELECT destination, 0, 1, day FROM fx WHERE destination IS NOT NULL)
        SELECT t.k, COUNT(*), SUM(dep), SUM(arr),
               SUM(CASE WHEN {in_cur} THEN 1 ELSE 0 END),
               SUM(CASE WHEN {in_prev} THEN 1 ELSE 0 END),
               a.name, a.city, a.cc, a.lat, a.lng
        FROM t LEFT JOIN ap a ON a.ident = t.k
        GROUP BY ALL ORDER BY 2 DESC""").fetchall():
        # OurAirports municipalities carry qualifiers - "Oslo (Gardermoen)",
        # "Paris (Roissy-en-France, Val-d'Oise)". The airport name already
        # says which airport; the city should just be the city.
        city = re.split(r"[,(]", city or "", 1)[0].strip()
        index[k] = [name or k, city, cc or ""]
        airports.append({
            "key": k, "name": name or k, "city": city, "cc": cc or "",
            "lat": lat, "lng": lng, "flights": n, "dep": dep, "arr": arr,
            "cur": cur, "prev": prev, "change_pct": change(cur, prev),
            "share": round(n / routed * 100, 1) if routed else 0,
            "local": bool(k.startswith("EK")),
        })
    coords = {a["key"]: (a["lat"], a["lng"]) for a in airports if a["lat"] is not None}

    def km(a, b):
        if a not in coords or b not in coords:
            return None
        (la1, lo1), (la2, lo2) = coords[a], coords[b]
        f1, f2 = math.radians(la1), math.radians(la2)
        dl = math.radians(lo2 - lo1)
        return round(2 * 6371 * math.asin(math.sqrt(
            math.sin((f2 - f1) / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2)))

    # City-pairs are unordered - Copenhagen-Oslo is one corridor - with each
    # direction kept so the page can show the balance.
    pairs = []
    for a, b, n, ab, cur, prev in con.execute(f"""
        SELECT LEAST(origin, destination), GREATEST(origin, destination), COUNT(*),
               SUM(CASE WHEN origin = LEAST(origin, destination) THEN 1 ELSE 0 END),
               SUM(CASE WHEN {in_cur} THEN 1 ELSE 0 END),
               SUM(CASE WHEN {in_prev} THEN 1 ELSE 0 END)
        FROM fx WHERE origin IS NOT NULL AND destination IS NOT NULL AND origin <> destination
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 120""").fetchall():
        pairs.append({
            "key": f"{a}-{b}", "a": a, "b": b, "flights": n, "ab": ab, "ba": n - ab,
            "cur": cur, "prev": prev, "change_pct": change(cur, prev),
            "km": km(a, b),
            "a_name": index.get(a, [a])[0], "b_name": index.get(b, [b])[0],
            "a_city": index.get(a, [a, ""])[1], "b_city": index.get(b, [b, ""])[1],
            "local": a.startswith("EK") or b.startswith("EK"),
        })

    # Countries at either end of a route - where the flying is actually
    # between, as opposed to where the aircraft is registered.
    # A flight touching the same country at both ends (Copenhagen-Aalborg)
    # counts once for it, so these are flights, not touches.
    countries = []
    for cc, n, dep, arr, cur, prev in con.execute(f"""
        WITH t AS (
            SELECT origin_cc AS cc, day,
                   1 AS dep, CASE WHEN destination_cc = origin_cc THEN 1 ELSE 0 END AS arr
            FROM fx WHERE origin_cc IS NOT NULL
            UNION ALL
            SELECT destination_cc, day, 0, 1
            FROM fx WHERE destination_cc IS NOT NULL AND destination_cc <> origin_cc)
        SELECT cc, COUNT(*), SUM(dep), SUM(arr),
               SUM(CASE WHEN {in_cur} THEN 1 ELSE 0 END),
               SUM(CASE WHEN {in_prev} THEN 1 ELSE 0 END)
        FROM t GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        meta = countries_meta.get(cc, {})
        countries.append({"key": cc, "name": meta.get("name", cc),
                          "region": meta.get("region"), "continent": meta.get("continent"),
                          "flights": n, "dep": dep, "arr": arr,
                          "cur": cur, "prev": prev, "change_pct": change(cur, prev)})

    order = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    dirs = dict(con.execute(
        "SELECT direction, COUNT(*) FROM fx WHERE direction IS NOT NULL GROUP BY 1").fetchall())
    directions = [{"k": d, "v": int(dirs.get(d, 0))} for d in order]

    return {
        "coverage": {
            "verified": cov.get("verified", 0), "unverified": cov.get("unverified", 0),
            "conflict": cov.get("conflict", 0), "none": cov.get("none", 0),
            "routed": routed, "routed_share": round(routed / total * 100, 1) if total else 0,
            "airports": len(airports), "pairs": len(pairs),
        },
        "local": {"touch_dk": touch_dk, "overflight": routed - touch_dk},
        "directions": directions,
        "airports": airports[:80],
        "pairs": pairs,
        "countries": countries,
        "index": index,
    }


def build_sky(con, origin=(56.16, 10.20), sample=1200, sectors=72) -> dict | None:
    """
    The receiver's footprint, small enough to ship inside the summary.

    The front page opens on this: every first contact on record by bearing and
    ground distance, plus the 95th-percentile reach in each 5° sector. Method
    draws the same figure from the full record through the query engine; here
    it is a seeded sample of the points and the envelope, a few kilobytes, so
    the instrument is on screen before anything heavier has loaded.
    """
    # The last year only: the footprint is a property of the antenna as it is
    # now, and reading every position ever recorded grows without limit.
    rows = con.execute(
        "SELECT first_lat, first_lon FROM fx "
        "WHERE first_lat IS NOT NULL AND first_lon IS NOT NULL "
        f"AND first_seen >= (SELECT MAX(first_seen) FROM fx) - {SKY_DAYS} * 86400"
    ).fetchall()
    if not rows:
        return None
    lat0, lng0 = origin
    f0 = math.radians(lat0)
    pts = []
    for lat, lon in rows:
        f, dl = math.radians(lat), math.radians(lon - lng0)
        cd = math.sin(f0) * math.sin(f) + math.cos(f0) * math.cos(f) * math.cos(dl)
        km = math.acos(max(-1.0, min(1.0, cd))) * 6371.0
        brg = math.atan2(math.sin(dl) * math.cos(f),
                         math.cos(f0) * math.sin(f) - math.sin(f0) * math.cos(f) * math.cos(dl))
        pts.append((km, (brg + 2 * math.pi) % (2 * math.pi)))

    kms = sorted(p[0] for p in pts)
    p50 = kms[len(kms) // 2]
    p95 = kms[min(len(kms) - 1, int(0.95 * len(kms)))]
    p99 = kms[min(len(kms) - 1, int(0.99 * len(kms)))]
    # Scale of the disc: the 99th-percentile reach with a small margin, so
    # 99% of contacts sit inside the frame and the 1% beyond are still visible.
    scale = math.ceil(max(p99 * 1.05, 200) / 50) * 50

    buckets: list[list[float]] = [[] for _ in range(sectors)]
    for km, brg in pts:
        buckets[int(brg / (2 * math.pi) * sectors) % sectors].append(km)
    env = [sorted(b)[int(0.95 * len(b))] if len(b) >= 4 else None for b in buckets]

    keep = pts if len(pts) <= sample else random.Random(1090).sample(pts, sample)
    return {
        # Flights in the same window, with or without a position, so the page
        # can say what share report one.
        "of": con.execute(
            "SELECT COUNT(*) FROM fx WHERE first_seen >= "
            f"(SELECT MAX(first_seen) FROM fx) - {SKY_DAYS} * 86400").fetchone()[0],
        "n": len(pts), "scale": scale,
        "p50": round(p50), "p95": round(p95), "p99": round(p99),
        "far": round(kms[-1]), "beyond": sum(1 for k in kms if k > scale),
        "env": [round(v) if v is not None else None for v in env],
        "pts": [[round(km), round(brg, 3)] for km, brg in keep],
    }


def build_months(con, daily) -> dict:
    """
    Calendar months, and an honest account of how far off the first one is.

    A month is only emitted once every one of its days is present and complete.
    Until then `progress` carries the count so the page can say what it is
    waiting for instead of drawing a bar out of a fortnight.
    """
    have = {d["day"]: d for d in daily if not d["partial"]}
    by_month: dict[str, list[str]] = {}
    for day in have:
        by_month.setdefault(day[:7], []).append(day)

    def days_in(ym: str) -> int:
        y, m = int(ym[:4]), int(ym[5:7])
        nxt = datetime(y + (m == 12), (m % 12) + 1, 1)
        return (nxt - datetime(y, m, 1)).days

    out, progress = [], []
    for ym in sorted(by_month, reverse=True):
        got, need = len(by_month[ym]), days_in(ym)
        if got < need:
            progress.append({"month": ym, "complete_days": got, "needs": need})
            continue
        lo, hi = f"{ym}-01", f"{ym}-{need:02d}"
        n, ac, wb, cg = con.execute(f"""
            SELECT COUNT(*), COUNT(DISTINCT hex),
                   SUM(CASE WHEN body = 'widebody' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_cargo THEN 1 ELSE 0 END)
            FROM fx WHERE day BETWEEN '{lo}' AND '{hi}'""").fetchone()
        out.append({"month": ym, "from": lo, "to": hi, "days": need,
                    "flights": n, "aircraft": ac, "widebody": wb, "cargo": cg,
                    "per_day": round(n / need, 1)})

    for i, m in enumerate(out):                       # newest first
        prev = out[i + 1] if i + 1 < len(out) else None
        ch = pct_change(m["flights"], prev["flights"]) if prev else None
        m["change_pct"] = round(ch, 1) if ch is not None else None
    return {"complete": out, "progress": progress}


def build_trend(daily) -> dict:
    """
    Strip the week out of the daily series.

    Traffic overhead is dominated by day-of-week: a Thursday and a Sunday are
    not two draws from the same distribution, so a raw day-on-day change is
    mostly just which weekday it happened to be. For anything forecast-shaped
    the useful quantity is a day measured against *its own weekday*, and what
    is left once that seasonality is removed.

    With a record this short each weekday has very few prior observations, so
    every figure here carries the count it rests on and the page is expected to
    say so. Two priors is a comparison; it is not yet a baseline.
    """
    full = [d for d in daily if not d["partial"]]
    if len(full) < 2:
        return {"days": [], "dow": [], "ready": False, "complete_days": len(full)}

    hist: dict[str, list[int]] = {}
    days = []
    for d in full:
        dow = datetime.fromisoformat(d["day"]).strftime("%a")
        priors = hist.get(dow, [])
        base = sum(priors) / len(priors) if priors else None
        days.append({
            "day": d["day"], "dow": dow, "flights": d["flights"],
            "dow_baseline": round(base, 1) if base else None,
            "dow_n": len(priors),
            # How far this day sat from what that weekday normally does. The
            # residual is the part a forecast would actually have to explain.
            "vs_dow_pct": round((d["flights"] / base - 1) * 100, 1)
                          if base else None,
        })
        hist.setdefault(dow, []).append(d["flights"])

    mean = sum(d["flights"] for d in full) / len(full)
    dow_profile = [
        {"dow": k, "n": len(v),
         "mean": round(sum(v) / len(v), 1),
         "index": round((sum(v) / len(v)) / mean * 100)}      # 100 = an average day
        for k, v in sorted(hist.items(), key=lambda kv: DOW_ORDER.index(kv[0]))
    ]
    return {
        "days": days[-DAYS_DETAIL:],
        "dow": dow_profile,
        "mean_per_day": round(mean, 1),
        "complete_days": len(full),
        # Below two observations per weekday the "baseline" is a single number
        # being compared against itself, which is not a baseline.
        "ready": all(p["n"] >= 2 for p in dow_profile) and len(dow_profile) == 7,
    }


# ── the macro board ──────────────────────────────────────────────────────────
# What the record is for. A count of aircraft says nothing about the economy;
# a count of *freighters*, or of business jets, or of flights to one end of a
# trade lane, does - provided it is compared against something that is not
# simply a different time of year. Each gauge below is one such line, defined
# by a predicate that runs unchanged here and in the browser's `flights` view.
GAUGES = [
    {"id": "freight", "label": "Air freight", "group": "Trade",
     "where": "is_cargo",
     "reads": "All-cargo operators. Freighters fly when there are goods to move, "
              "not when a timetable says so - the closest thing overhead to a "
              "trade figure."},
    {"id": "freight_cn", "label": "China-Europe freighters", "group": "Trade",
     "where": "is_cargo AND carrier_country = 'CN'",
     "reads": "Freighters flown by Chinese cargo airlines between China and "
              "European hubs: e-commerce parcels one way, machinery and luxury "
              "goods the other. A direct reading on one trade lane."},
    {"id": "freight_west", "label": "Express and Western freighters", "group": "Trade",
     "where": "is_cargo AND carrier_country <> 'CN'",
     "reads": "DHL, UPS, FedEx and the European overnight parcel network, plus "
              "Western heavy freighters. Tracks parcel volumes and business-to-"
              "business shipping inside Europe."},
    {"id": "bizav", "label": "Business aviation", "group": "Business",
     "where": "is_bizav",
     "reads": "Business jets, counted by airframe. Corporate and private travel "
              "at its most discretionary - the first line cut in a downturn and "
              "the first to return."},
    {"id": "network", "label": "Full-service scheduled", "group": "Business",
     "where": "carrier_kind = 'network'",
     "reads": "Network airlines. Carries business travel and connecting traffic; "
              "less seasonal than holiday flying, but it follows the timetable."},
    {"id": "domestic", "label": "Danish domestic", "group": "Business",
     "where": "origin LIKE 'EK%' AND destination LIKE 'EK%'",
     "reads": "Copenhagen to Jutland and back: shuttles that exist for the "
              "working week. Tracks Danish business activity more than tourism."},
    {"id": "transatlantic", "label": "Transatlantic passenger", "group": "Long-haul",
     "where": "far_region = 'North America' AND is_cargo IS NOT TRUE",
     "reads": "Passenger flights between Europe and North America crossing "
              "overhead. Long-haul capacity is expensive to fly empty, so it "
              "follows premium and tourist demand across the Atlantic."},
    {"id": "eastasia", "label": "East Asia passenger", "group": "Long-haul",
     "where": "far_region = 'East Asia' AND is_cargo IS NOT TRUE",
     "reads": "Passenger flights between Europe and China, Japan and Korea. "
              "Business ties and tourism with East Asia, and a reading on how "
              "open that corridor is."},
    {"id": "gulf", "label": "Gulf hubs", "group": "Long-haul",
     "where": "far_region = 'Gulf'",
     "reads": "Flights to and from Dubai, Doha and the other Gulf hubs - the "
              "connecting route from Scandinavia to South Asia, Africa and "
              "Australia."},
    {"id": "leisure", "label": "Low-cost and charter", "group": "Consumer",
     "where": "carrier_kind IN ('lowcost', 'leisure')",
     "reads": "Price-led and holiday flying: household discretionary spending. "
              "The most seasonal line here, so it only means something year on "
              "year."},
    {"id": "total", "label": "All flights", "group": "All",
     "where": "TRUE",
     "reads": "Everything the antenna hears. Mostly the passenger timetable; "
              "shown as the yardstick the other lines are read against."},
]

# How many stepped weeks back "a year ago" is. 52 weeks is 364 days, so the
# year-ago week holds the same weekdays and sits at the same point in the
# schedule season. Overridable only so the year-on-year path can be exercised
# before a year of record exists.
YOY_WEEKS = int(os.environ.get("OVRHEAD_YOY_WEEKS", "52"))


def nice_day(day: str, year: bool = False) -> str:
    """'9 November' - without %-d, which the collector's Windows Python lacks."""
    d = datetime.fromisoformat(day)
    return f"{d.day} {d.strftime('%B')}" + (f" {d.year}" if year else "")


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _dispersion(vals: list, seasons: list) -> float:
    """
    How much noisier than pure chance a weekly count is.

    A count of independent flights would vary week to week like a Poisson
    variable, with variance equal to its mean. Real traffic varies more: a
    rotation is several flights that come and go together. This estimates the
    ratio from successive weeks inside one schedule season, using the median
    so a genuine trend or one odd week does not inflate it.
    """
    r = []
    for i in range(len(vals) - 1):
        a, b = vals[i], vals[i + 1]
        if a is None or b is None or not seasons[i] or seasons[i] != seasons[i + 1]:
            continue
        if a + b > 0:
            r.append((a - b) ** 2 / (a + b))
    if len(r) < 3:
        return 2.0
    r.sort()
    return max(1.0, r[len(r) // 2])


def _versus(recent: list, base: list, phi: float) -> dict | None:
    """Mean of one set of weeks against another, with how sure the gap is."""
    if not recent or not base:
        return None
    a, b = _mean(recent), _mean(base)
    if b <= 0:
        return None
    se = math.sqrt(phi * a / len(recent) + phi * b / len(base))
    return {"pct": round((a / b - 1) * 100, 1), "recent": round(a, 1), "base": round(b, 1),
            "n_recent": len(recent), "n_base": len(base),
            "z": round((a - b) / se, 2) if se else 0.0}


def stepped_weeks(daily) -> list[dict]:
    """
    Every seven-day window back from the newest complete day to the start of
    the record, newest first. Unlike build_weeks this does not stop at the
    first gap: a day the receiver was down makes that one week unusable
    (`ok` False) and the series carries on behind it, which is what lets a
    year-on-year comparison survive an outage in between.
    """
    have = {d["day"] for d in daily if not d["partial"]}
    if not have:
        return []
    newest, first = datetime.fromisoformat(max(have)), datetime.fromisoformat(min(have))
    out, i = [], 0
    while True:
        end = newest - timedelta(days=7 * i)
        if end - timedelta(days=6) < first:
            break
        days = [(end - timedelta(days=j)).strftime("%Y-%m-%d") for j in range(6, -1, -1)]
        a, b = season_of(days[0]), season_of(days[-1])
        out.append({"from": days[0], "to": days[-1], "days": days,
                    "ok": all(d in have for d in days),
                    # None for the week the timetable changed in: it belongs
                    # to neither season and is compared with nothing.
                    "season": a["code"] if a["code"] == b["code"] else None})
        i += 1
    return out


def build_macro(con, daily, routes, carriers) -> dict | None:
    """
    The macro board: a handful of economically meaningful lines, each compared
    only against something comparable.

    The ladder, best first, chosen per gauge from what the record holds:

      yoy     the latest four weeks against the same four weeks a year earlier
              (52 weeks back, so weekdays and schedule season both line up).
              The only comparison with the time of year fully taken out.
      season  the latest k weeks against the k before them, all inside the
              current schedule season (k = 2 at four weeks in, up to 4). The
              timetable switch is excluded by construction; the time of year
              is not, and the output says so.
      none    too early in the season for either. The level is published and
              the change is withheld, with the date it resumes.

    Alongside, `step` records what each line did across the last timetable
    switch. It is published precisely because it is not a signal: it shows how
    much of each line is the calendar, which is the context every other figure
    needs.

    Cost is one grouped scan for the gauges and one per dimension for the
    movers, each over a fixed window, so it does not grow with the record. The
    output is capped at MACRO_WEEKS of series per gauge.
    """
    weeks = stepped_weeks(daily)
    usable = [w for w in weeks if w["ok"]]
    if not usable:
        return None

    cols = ", ".join(f"SUM(CASE WHEN {g['where']} THEN 1 ELSE 0 END)" for g in GAUGES)
    by_day = {r[0]: r[1:] for r in con.execute(
        f"SELECT day, {cols} FROM fx GROUP BY 1").fetchall()}
    zero = (0,) * len(GAUGES)

    newest = weeks[0]["to"]
    cur = season_of(newest)
    prev = season_of((datetime.fromisoformat(cur["from"]) - timedelta(days=1))
                     .strftime("%Y-%m-%d"))
    seasons = [w["season"] for w in weeks]

    # The run of complete weeks inside the current season, newest first.
    run = []
    for i, w in enumerate(weeks):
        if w["season"] != cur["code"]:
            break
        if w["ok"]:
            run.append(i)
    k = min(4, len(run) // 2)
    recent_ix, base_ix = run[:k], run[k:2 * k]

    yoy_cur = [i for i in range(4) if i < len(weeks) and weeks[i]["ok"]]
    yoy_prev = [i for i in range(YOY_WEEKS, YOY_WEEKS + 4)
                if i < len(weeks) and weeks[i]["ok"]]
    yoy_ready = len(yoy_cur) >= 3 and len(yoy_prev) >= 3

    # Either side of the last timetable switch: the first complete weeks of
    # this season against the last complete weeks of the one before.
    post_ix = [i for i in reversed(range(len(weeks)))
               if weeks[i]["season"] == cur["code"] and weeks[i]["ok"]][:4]
    pre_ix = [i for i in range(len(weeks))
              if weeks[i]["season"] == prev["code"] and weeks[i]["ok"]][:4]
    step_ready = len(post_ix) >= 2 and len(pre_ix) >= 2
    if step_ready:                      # same number of weeks on each side
        m = min(len(post_ix), len(pre_ix))
        post_ix, pre_ix = post_ix[:m], pre_ix[:m]

    gauges = []
    tot_j = next(j for j, g in enumerate(GAUGES) if g["id"] == "total")
    for j, g in enumerate(GAUGES):
        vals = [sum(by_day.get(d, zero)[j] for d in w["days"]) if w["ok"] else None
                for w in weeks]
        if not any(vals):
            continue
        phi = _dispersion(vals, seasons)
        pick = lambda ix: [vals[i] for i in ix]

        yoy = _versus(pick(yoy_cur), pick(yoy_prev), phi) if yoy_ready else None
        season = _versus(pick(recent_ix), pick(base_ix), phi) if k >= 2 else None
        step = _versus(pick(post_ix), pick(pre_ix), phi) if step_ready else None
        best = yoy or season
        basis = "yoy" if yoy else "season" if season else None

        # Weekday against weekend, per day, over the latest four usable weeks.
        wd = we = 0
        for i in yoy_cur:
            for d in weeks[i]["days"]:
                v = by_day.get(d, zero)[j]
                if datetime.fromisoformat(d).weekday() >= 5:
                    we += v
                else:
                    wd += v
        lift = round((wd / 5) / (we / 2) * 100 - 100) if we else None

        level = _mean(pick(yoy_cur))
        level_tot = _mean([sum(by_day.get(d, zero)[tot_j] for d in weeks[i]["days"])
                           for i in yoy_cur])
        gauges.append({
            "id": g["id"], "label": g["label"], "group": g["group"], "reads": g["reads"],
            "per_week": round(level, 1),
            "share": round(level / level_tot * 100, 1) if level_tot else None,
            "weekday_lift_pct": lift,
            "basis": basis,
            "change_pct": best["pct"] if best else None,
            "z": best["z"] if best else None,
            # Twice the week-to-week noise, and at least five percent: below
            # that a move is indistinguishable from an ordinary week.
            "significant": bool(best and abs(best["z"]) >= 2 and abs(best["pct"]) >= 5),
            "yoy": yoy, "season": season, "step": step,
            "series": [{"k": weeks[i]["from"][5:], "v": vals[i], "s": weeks[i]["season"]}
                       for i in reversed(range(min(len(weeks), MACRO_WEEKS)))],
            "sql": ("SELECT day, COUNT(*) AS flights\nFROM flights\n"
                    f"WHERE {g['where']}\nGROUP BY 1 ORDER BY 1;"),
        })

    first_day = min(d["day"] for d in daily if not d["partial"])
    day_plus = lambda day, n: (datetime.fromisoformat(day) + timedelta(days=n)).strftime("%Y-%m-%d")
    return {
        "season": {**cur, "weeks": len(run)},
        "prev_season": prev,
        "weeks": len(usable),
        "k": k,
        # Four complete weeks inside the season is the first in-season
        # comparison (two against two): the day after the 28th day.
        "season_ready_on": None if k >= 2 else day_plus(cur["from"], 28),
        "yoy_ready": yoy_ready,
        # Three of the latest four weeks need a counterpart 52 weeks back.
        "yoy_from": None if yoy_ready else day_plus(first_day, 7 * YOY_WEEKS + 21),
        "step_ready": step_ready,
        "step_weeks": len(post_ix) if step_ready else 0,
        "gauges": gauges,
        "movers": build_movers(con, weeks, yoy_cur, yoy_prev, routes, carriers)
                  if yoy_ready else [],
    }


def build_movers(con, weeks, cur_ix, prev_ix, routes, carriers) -> list[dict]:
    """
    Countries, city-pairs and airlines that moved year on year.

    Only year on year. A destination moving week on week, or across a season,
    is the timetable: flights to the Mediterranean fall every October and
    return every March, and reporting that is reporting the calendar. Against
    the same four weeks a year earlier the season cancels, and what is left is
    a route opened or closed, capacity added or withdrawn - a decision somebody
    made about demand.

    Each query reads two fixed 28-day windows, however long the record is.
    """
    def in_days(ix):
        return ", ".join(f"'{d}'" for i in ix for d in weeks[i]["days"])
    cur_d, prev_d = in_days(cur_ix), in_days(prev_ix)
    scope = f"day IN ({cur_d}, {prev_d})"
    agg = (f"SUM(CASE WHEN day IN ({cur_d}) THEN 1 ELSE 0 END), "
           f"SUM(CASE WHEN day IN ({prev_d}) THEN 1 ELSE 0 END)")
    nc, npv = len(cur_ix), len(prev_ix)

    names = {c["key"]: c["name"] for c in (routes or {}).get("countries", [])}
    index = (routes or {}).get("index", {})

    def pair_name(key):
        a, _, b = key.partition("-")
        return f"{index.get(a, [a, a])[1] or a} – {index.get(b, [b, b])[1] or b}"

    dims = [
        ("country", f"""
            WITH t AS (
                SELECT origin_cc AS k, day FROM fx WHERE origin_cc IS NOT NULL AND {scope}
                UNION ALL
                SELECT destination_cc, day FROM fx
                WHERE destination_cc IS NOT NULL AND destination_cc <> origin_cc AND {scope})
            SELECT k, {agg} FROM t GROUP BY 1""",
         lambda key: f"Flights to or from {names.get(key, key)}",
         lambda key: f"origin_cc = '{key}' OR destination_cc = '{key}'"),
        ("pair", f"SELECT pair, {agg} FROM fx WHERE pair IS NOT NULL AND {scope} GROUP BY 1",
         pair_name, lambda key: f"pair = '{key}'"),
        ("operator", f"SELECT airline_prefix, {agg} FROM fx "
                     f"WHERE airline_prefix IS NOT NULL AND {scope} GROUP BY 1",
         lambda key: (carriers.get(key, {}).get("name") or key),
         lambda key: f"airline_prefix = '{key}'"),
    ]
    out = []
    for dim, sql, label, where in dims:
        found = []
        for key, c, p in con.execute(sql).fetchall():
            a, b = c / nc, p / npv
            # Ten a week on both sides: below that one rotation is the move.
            if a < 10 or b < 10:
                continue
            v = _versus([a] * nc, [b] * npv, 2.0)
            if not v or abs(v["pct"]) < 20 or abs(v["z"]) < 3:
                continue
            found.append({"dim": dim, "key": key, "name": label(key), "where": where(key), **v})
        found.sort(key=lambda m: -abs(m["z"]))
        out += found[:3]
    out.sort(key=lambda m: -abs(m["z"]))
    return out

def build_weeks(con, daily, kind_meta) -> list[dict]:
    """
    Every complete week the record holds, newest first.

    The windows are *stepped*, not rolling: seven days, then the seven before
    that, and so on back from the most recent complete day. A rolling window
    would give a new "week" every day, but consecutive ones would share six of
    their seven days - a week-over-week change computed across them is mostly
    the same flights compared against themselves. Stepping keeps each week an
    independent sample, which is the only version of the comparison worth
    publishing.

    Each week also carries `wow`: how its totals moved against the week before,
    present only when that earlier week is itself complete.
    """
    full = [d["day"] for d in daily if not d["partial"]]
    if len(full) < 7:
        return []

    have = set(full)
    newest = datetime.fromisoformat(full[-1])

    windows: list[list[str]] = []
    step = 0
    while True:
        end = newest - timedelta(days=7 * step)
        days = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        if not all(d in have for d in days) or step >= WEEKS_DETAIL:
            break                          # ran off the start of the record
        windows.append(days)
        step += 1

    weeks = [build_week_for(con, w, kind_meta) for w in windows]

    # Week-over-week, against the immediately preceding stepped week. `weeks` is
    # newest-first, so a week's predecessor is the next element along.
    for i, wk in enumerate(weeks):
        prev = weeks[i + 1] if i + 1 < len(weeks) else None
        wk["index"] = i
        wk["prev_from"] = prev["from"] if prev else None
        wk["next_from"] = weeks[i - 1]["from"] if i > 0 else None
        if prev:
            a, b = wk["totals"]["flights"], prev["totals"]["flights"]
            ch = pct_change(a, b)
            wk["wow"] = {
                "prev_flights": b,
                "change_pct": round(ch, 1) if ch is not None else None,
                "prev_from": prev["from"], "prev_to": prev["to"],
                # Set when the two weeks are flown to different timetables, so
                # the page can say the change is mostly the calendar.
                "schedule_change": (season_of(wk["to"])["from"]
                                    if season_of(prev["from"])["code"]
                                    != season_of(wk["to"])["code"] else None),
            }
        else:
            wk["wow"] = None
    return weeks


def build_week_for(con, days: list[str], kind_meta) -> dict:
    """One week's worth of shape, for an already-validated run of seven days."""
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
                   span_days, total, compare, totals,
                   baseline_complete, week=None, routes=None, macro=None) -> list[dict]:
    """
    Turn the rollups into a ranked list of claims.

    Every signal carries the same shape: what changed, by how much, against
    which baseline, where it is concentrated, what it might mean, how sure we
    are, and the SQL that reproduces it. The interpretation field is always
    phrased as a possibility, never a conclusion.

    The `sql` field runs in the reader's browser, against the `flights` view
    index.html builds over the Parquet - not against the local `fx` view used
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
        # A `reading` has a direction - something moved, or runs one way. A
        # `profile` describes what the record is: a share, a count, a rarity.
        # An analyst wants the first kind first, and should not have to work
        # out which is which from the wording.
        kw.setdefault("category", "profile")
        # `lead` puts a signal first within its category regardless of the
        # size of its figure: a share and a distance cannot be ranked against
        # each other by magnitude, and the route facts come before the rest.
        kw.setdefault("lead", False)
        kw["_i"] = len(sig)
        sig.append(kw)

    full_days = [d for d in daily if not d["partial"]]

    # The shape beside a signal is the recent record, not all of it: a fixed
    # window keeps both the query and the file the same size every year.
    spark_days = full_days[-SPARK_DAYS:]
    days_sql = ", ".join(f"'{d['day']}'" for d in spark_days)

    def series(where: str | None = None) -> list[dict]:
        """One value per complete day, for the sparkline beside a signal.
        A day with nothing matching reads as zero rather than disappearing,
        so the shape stays aligned to the calendar."""
        if not full_days:
            return []
        w = f"WHERE day IN ({days_sql})" + (f" AND ({where})" if where else "")
        got = dict(con.execute(f"SELECT day, COUNT(*) FROM fx {w} GROUP BY 1").fetchall())
        return [{"k": d["day"][5:], "v": int(got.get(d["day"], 0) or 0)} for d in spark_days]

    def week_series(kind: str | None = None) -> list[dict]:
        """The seven days of the week, in order, for one kind or for all."""
        return [{"k": d["dow"], "v": d["kinds"].get(kind, 0) if kind else d["flights"]}
                for d in week["days"]]

    # ── The macro board ──────────────────────────────────────────────────
    # The readings the site exists for. Every one is a line with an economic
    # meaning, compared only against something comparable: the same weeks a
    # year earlier, or other weeks of the same schedule season. Nothing here
    # is measured across a timetable switch.
    if macro:
        G = {g["id"]: g for g in macro["gauges"]}
        sea, prev_sea = macro["season"], macro["prev_season"]
        word = "winter" if sea["code"].startswith("W") else "summer"

        def wk_series(g):
            return [{"k": p["k"], "v": p["v"] or 0} for p in g["series"]]

        # What the timetable switch did, and to what. Published because it is
        # NOT a signal: it sizes the calendar effect in each line, and the
        # lines that ignore it are the ones driven by demand.
        fr, tot = G.get("freight"), G.get("total")
        if macro["step_ready"] and fr and tot and fr["step"] and tot["step"]:
            st, tt = fr["step"]["pct"], tot["step"]["pct"]
            freight_said = ("Freight held." if abs(st) < 6 else
                            f"Freight {'rose' if st > 0 else 'fell'} {abs(st):.0f}%.")
            show = [G[i] for i in ("leisure", "network", "bizav", "transatlantic",
                                   "eastasia", "total") if i in G and G[i]["step"]]
            add(
                kind="season", scope="all", category="reading", lead=True,
                series=wk_series(fr),
                title=(f"The {word} timetable {'added' if tt > 0 else 'took'} "
                       f"{abs(tt):.0f}% {'to' if tt > 0 else 'of'} the flights overhead. "
                       f"{freight_said}"),
                metric=f"{st:+.0f}%", metric_label="air freight across the timetable switch",
                comparison=" · ".join(f"{g['label']} {g['step']['pct']:+.0f}%" for g in show),
                where=f"{macro['step_weeks']} weeks either side of {nice_day(sea['from'], True)}",
                interpretation=(
                    "Airlines publish two timetables a year and switch on the last "
                    "Sunday of March and of October. The move in passenger flying at "
                    "that date is the calendar, not the economy, so it is shown once, "
                    "here, and nothing else on this site is compared across it. A "
                    "line that holds through the switch is flying because of demand "
                    "rather than because of the timetable - which is why freight "
                    "leads the board."
                ),
                caveat=("Freight has a season of its own - the build-up to Christmas - "
                        "so a flat line through late October is not a flat year. Only "
                        "the same weeks a year earlier can separate that out"
                        + (f", from {nice_day(macro['yoy_from'], True)}."
                           if macro["yoy_from"] else ".")),
                confidence="observed",
                sql=("SELECT day, carrier_kind, COUNT(*) AS flights\n"
                     "FROM flights\n"
                     f"WHERE day BETWEEN '{(datetime.fromisoformat(sea['from']) - timedelta(days=28)).strftime('%Y-%m-%d')}'"
                     f" AND '{(datetime.fromisoformat(sea['from']) + timedelta(days=27)).strftime('%Y-%m-%d')}'\n"
                     "GROUP BY 1, 2 ORDER BY 1, 3 DESC;"),
            )

        # Gauges that moved by more than their own week-to-week noise.
        moved = [g for g in macro["gauges"] if g["id"] != "total" and g["significant"]]
        moved.sort(key=lambda g: -abs(g["z"]))
        for g in moved:
            yoy = g["basis"] == "yoy"
            v = g["yoy"] if yoy else g["season"]
            up = v["pct"] > 0
            add(
                kind="macro", scope="gauge", entity=g["id"], category="reading",
                lead=g["group"] in ("Trade", "Business"),
                series=wk_series(g),
                title=(f"{g['label']} {'up' if up else 'down'} {abs(v['pct']):.0f}% "
                       + ("on a year ago" if yoy else f"inside the {sea['label']}")),
                metric=f"{v['pct']:+.0f}%",
                metric_label=("vs the same four weeks a year earlier" if yoy else
                              f"latest {v['n_recent']} weeks vs the {v['n_base']} before"),
                comparison=f"{v['recent']:,.0f} flights a week vs {v['base']:,.0f}",
                where=g["group"],
                interpretation=(
                    g["reads"] + " " +
                    ("Against the same weeks a year earlier the season cancels out, so "
                     "this is a change in how much is being flown."
                     if yoy else
                     "Both windows are flown to the same timetable, so this is not the "
                     "schedule switch - and it is larger than this line's usual "
                     "week-to-week variation.")
                ),
                caveat=(
                    "Year on year removes the season but not a one-off: a strike, an "
                    "airspace closure or a holiday falling in a different week moves "
                    "it. Flights are capacity offered, not passengers or tonnes carried."
                    if yoy else
                    "The time of year is still in this figure: demand drifts inside "
                    "a season too. It can only be taken out against the same weeks a "
                    "year earlier"
                    + (f", which begins {nice_day(macro['yoy_from'], True)}."
                       if macro["yoy_from"] else ".")
                ),
                confidence=("strong" if yoy and abs(v["z"]) >= 3 else
                            "moderate" if yoy else "early"),
                sql=g["sql"],
            )

        # Too early in a season for any comparison: say so, with the date.
        if not macro["yoy_ready"] and macro["k"] < 2:
            add(
                kind="coverage", scope="all", category="reading",
                series=wk_series(tot) if tot else [],
                title=f"Comparisons resume {nice_day(macro['season_ready_on'])}",
                metric=f"{sea['weeks']}", metric_label=f"of 4 weeks into the {sea['label']}",
                comparison=f"The timetable changed on {nice_day(sea['from'], True)}",
                where=None,
                interpretation=(
                    "A change is only reported between weeks flown to the same "
                    "timetable. Four complete weeks of the new one are needed before "
                    "two can be set against two; until then the macro board shows "
                    "levels, and what each line did across the switch."
                ),
                caveat="Comparing the new timetable with the old one would report the "
                       "calendar as a finding. The site would rather report nothing.",
                confidence="observed",
                sql=("SELECT day, COUNT(*) AS flights\nFROM flights\n"
                     f"WHERE day >= '{sea['from']}'\nGROUP BY 1 ORDER BY 1;"),
            )

        # Destinations, city-pairs and airlines - year on year only.
        for m in macro["movers"][:5]:
            up = m["pct"] > 0
            add(
                kind="shift", scope=m["dim"], entity=m["key"], category="reading",
                series=series(m["where"]),
                title=f"{m['name']} {'up' if up else 'down'} {abs(m['pct']):.0f}% on a year ago",
                metric=f"{m['pct']:+.0f}%", metric_label="vs the same four weeks a year earlier",
                comparison=f"{m['recent']:,.0f} flights a week vs {m['base']:,.0f}",
                where=None,
                interpretation=(
                    "Measured against the same weeks last year, so the season is not "
                    "in it: this is capacity added or withdrawn - a route opened, "
                    "closed, or flown more or less often. Airlines move capacity "
                    "toward demand, with a lag of a schedule season or so."
                ),
                caveat="A change of airline on a route, or a callsign the route tables "
                       "did not know last year, can appear as a move. Flights are "
                       "capacity, not passengers.",
                confidence="strong" if abs(m["z"]) >= 5 else "moderate",
                sql=(f"SELECT day, COUNT(*) AS flights\nFROM flights\n"
                     f"WHERE {m['where']}\nGROUP BY 1 ORDER BY 1;"),
            )

        # Business jets keep the working week - the property that makes the
        # line a reading on business rather than on wealthy leisure.
        bz = G.get("bizav")
        if bz and bz["weekday_lift_pct"] is not None and bz["weekday_lift_pct"] >= 15:
            add(
                kind="composition", scope="gauge", entity="bizav", category="reading",
                series=wk_series(bz),
                title="Business jets keep the working week",
                metric=f"{bz['weekday_lift_pct']:+d}%",
                metric_label="business-jet flights, weekday vs weekend",
                comparison=f"{bz['per_week']:,.0f} business-jet flights a week · "
                           f"{bz['share']}% of traffic",
                where="latest four weeks",
                interpretation=(
                    "Counted by airframe, not by operator, so privately flown jets "
                    "are included. The weekday tilt says most of this is work: "
                    "meetings, site visits, deals. It is the most discretionary "
                    "travel there is, which is what makes its level worth tracking."
                ),
                caveat="A business jet is identified by its type. Who is aboard and "
                       "why is not observable, and some of this is private leisure.",
                confidence="observed",
                sql=("SELECT dow, COUNT(*) AS flights\nFROM flights\n"
                     "WHERE is_bizav\nGROUP BY 1;"),
            )

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
                kind="composition", scope="all", category="reading",
                series=week_series(),
                title="The working week barely changes how much flies - "
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
                caveat="This is the shape of one week, not a change in it. What "
                       "moved, and against what, is on the macro board.",
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
                kind="corridor", scope="all", category="reading",
                series=week_series("cargo"),
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

        if charter and charter["lift_pct"] is not None and charter["lift_pct"] <= -10:
            add(
                kind="composition", scope="all", category="reading",
                series=week_series("leisure"),
                title="Holiday flying is a weekend business",
                metric=f"{charter['lift_pct']:+d}%",
                metric_label="charter flights, weekday vs weekend",
                comparison=f"{charter['weekend_per_day']} a weekend day vs "
                           f"{charter['weekday_per_day']} on a weekday",
                where=span,
                interpretation=(
                    "Tour-operator flying is the purest leisure demand in the record - "
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
    #    periods, that IS the headline - publishing invented trends instead
    #    would be the single fastest way to make this product untrustworthy.
    if compare is None:
        need = max(0, 14 - len(full_days))
        eta = (nice_day((datetime.fromisoformat(full_days[-1]["day"])
                         + timedelta(days=need + 1)).strftime("%Y-%m-%d"))
               if full_days else "later")
        add(
            kind="coverage", scope="all", series=series(),
            title=f"Week-over-week comparisons begin around {eta}",
            metric=f"{len(full_days)}", metric_label="of 14 complete days",
            comparison=f"{need} more complete day{'s' if need != 1 else ''} needed",
            where=None,
            interpretation="Every change figure here is one complete week against the "
                           "complete week before it - one of every weekday on each side, "
                           "so day-of-week cancels out. That needs fourteen complete days. "
                           "Until then, no change is reported anywhere on the site, "
                           "rather than a day-on-day figure that mostly measures which "
                           "weekday it was.",
            caveat="Composition and counts below are directly observed and stand on "
                   "their own. Only the comparisons are waiting.",
            confidence="observed",
            sql=("SELECT strftime(seen_at, '%Y-%m-%d') AS day,\n"
                 "       COUNT(*) AS flights,\n"
                 "       (MAX(first_seen) - MIN(first_seen)) / 3600.0 AS hours_covered\n"
                 "FROM flights GROUP BY 1 ORDER BY 1;"),
        )

    # 2) Where the flights are actually between. These replace the old
    #    registration-based corridor claim: a route says where a flight began
    #    and where it is going, which registration never could.
    if routes and routes["coverage"]["routed"]:
        rc, loc = routes["coverage"], routes["local"]
        over = loc["overflight"] / rc["routed"] * 100

        add(
            kind="corridor", scope="all", lead=True,
            series=series("is_overflight"),
            title=f"{over:.0f}% of the flights overhead neither start nor end in Denmark",
            metric=f"{over:.0f}%", metric_label="of routed flights are overflights",
            comparison=f"{loc['overflight']:,} pass through · {loc['touch_dk']:,} touch a Danish airport",
            where=None,
            interpretation="Aarhus sits under the routings between Scandinavia and the "
                           "Continent, and between Northern Europe and the rest of the "
                           "world. Most of what the antenna hears is that corridor, not "
                           "Danish demand - which is what makes the count usable as a "
                           "signal about the wider network rather than one region.",
            caveat=f"Only the {rc['routed_share']:.0f}% of flights with a verified or "
                   "unverified route are counted here. Flights with no callsign, or "
                   "a callsign not in the route tables, are not - and they are not "
                   "assumed to be either kind.",
            confidence="observed",
            sql=("SELECT is_overflight, COUNT(*) AS flights\n"
                 "FROM flights WHERE origin IS NOT NULL GROUP BY 1;"),
        )

        top = routes["pairs"][0] if routes["pairs"] else None
        if top:
            add(
                kind="corridor", scope="pair", entity=top["key"], lead=True,
                series=series(f"pair = '{top['key']}'"),
                title=f"Busiest city-pair overhead: {top['a_city'] or top['a']} – "
                      f"{top['b_city'] or top['b']}",
                metric=f"{top['flights']:,}", metric_label="flights on this pair",
                comparison=f"{top['ab']:,} {top['a']}→{top['b']} · {top['ba']:,} the other way",
                where=f"{top['km']:,} km" if top["km"] else None,
                interpretation="The busiest corridor is the one the record can measure "
                               "most precisely. Week on week, this is the pair to watch "
                               "for schedule changes first.",
                caveat="A pair counts both directions. The balance between them is "
                       "schedule shape, not demand asymmetry - a rotation is a rotation.",
                confidence="observed",
                sql=(f"SELECT origin, destination, COUNT(*) AS flights\n"
                     f"FROM flights WHERE pair = '{top['key']}'\n"
                     "GROUP BY 1, 2;"),
            )

        far = sorted([p for p in routes["pairs"] if p["km"]], key=lambda p: -p["km"])[:3]
        far_keys = ", ".join(f"'{p['key']}'" for p in far)
        if far and far[0]["km"] >= 3000:
            add(
                kind="corridor", scope="all", lead=True,
                series=series(f"pair IN ({far_keys})"),
                title="Intercontinental routes crossing overhead",
                metric=f"{far[0]['km']:,}", metric_label="km, the longest route seen",
                # Two airports in one city read as the same pair; say it once.
                comparison=" · ".join(dict.fromkeys(
                    f"{p['a_city'] or p['a']}–{p['b_city'] or p['b']}" for p in far)),
                where=None,
                interpretation="Long-haul flights pass over Denmark on the great circles "
                               "between Central Europe and North America or Asia. Their "
                               "presence is a reading on those corridors, not on Denmark.",
                caveat="Distance is the great-circle length of the scheduled route, not "
                       "the path flown.",
                confidence="observed",
                sql=(f"SELECT pair, origin_name, destination_name, COUNT(*) AS flights\n"
                     f"FROM flights WHERE pair IN ({far_keys})\n"
                     "GROUP BY 1, 2, 3 ORDER BY 4 DESC;"),
            )

        d = {x["k"]: x["v"] for x in routes["directions"]}
        axis = d.get("NE", 0) + d.get("SW", 0)
        allp = sum(d.values()) or 1
        add(
            kind="corridor", scope="all", lead=True,
            series=routes["directions"],
            title="Traffic overhead runs north-east to south-west",
            metric=f"{axis / allp * 100:.0f}%", metric_label="of flights on the NE-SW axis",
            comparison=f"{d.get('NE', 0):,} heading NE · {d.get('SW', 0):,} heading SW",
            where=None,
            interpretation="That axis is Scandinavia to the Continent. Direction is "
                           "measured from the aircraft's own track, so it holds for "
                           "every positioned flight - including the ones with no route.",
            caveat="Eight compass sectors, from first to last position in range. A "
                   "flight seen only briefly can land in the wrong sector.",
            confidence="observed",
            sql=("SELECT direction, COUNT(*) AS flights\n"
                 "FROM flights WHERE direction IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;"),
        )

    # 4) The far end of the long-haul overhead - the alternative-data hook,
    #    now by destination: flights that begin or end outside Europe.
    rcountries = routes["countries"] if routes else []
    shown = 0
    for c in rcountries:
        if c.get("continent") in ("Europe", None) or c["flights"] < 15:
            continue
        cc = c["key"]
        add(
            kind="corridor", scope="country", entity=cc,
            series=series(f"origin_cc = '{cc}' OR destination_cc = '{cc}'"),
            title=f"Flights to or from {c['name']} crossing overhead",
            metric=f"{c['flights']:,}", metric_label="flights",
            comparison=f"{c['arr']:,} bound for {c['name']} · {c['dep']:,} leaving it",
            where=c.get("region"),
            interpretation=(
                f"None of these land in Denmark. They are the Europe-{c['name']} "
                "great circles that happen to cross this antenna's range, so the "
                "count is a reading on that corridor - and the split between the two "
                "directions is the same flow seen from both ends."
            ),
            caveat="Corridor routings shift with winds, airspace closures and slot "
                   "times. Flights are not passengers or freight; a change here is a "
                   "change in schedule before it is a change in demand.",
            confidence="observed",
            sql=(f"SELECT origin_city, destination_city, COUNT(*) AS flights\n"
                 f"FROM flights WHERE origin_cc = '{cc}' OR destination_cc = '{cc}'\n"
                 f"GROUP BY 1, 2 ORDER BY 3 DESC;"),
        )
        shown += 1
        if shown >= 2:
            break

    # 5) Wide-body share - a capacity signal distinct from a flight count.
    wb = totals["widebody"]
    if total and wb:
        add(
            kind="capacity", scope="all", series=series("body = 'widebody'"),
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

    # 6) Who the freighters are. A level, not a move - the moves are on the
    #    macro board - but it is what makes the freight line readable: more
    #    than half of it is one trade lane.
    cargo_ops = [o for o in operators if o.get("cargo_operator")]
    cg = sum(o["flights"] for o in cargo_ops)
    G = {g["id"]: g for g in (macro or {}).get("gauges", [])}
    if cg:
        cn, fr = G.get("freight_cn"), G.get("freight")
        cn_share = (cn["per_week"] / fr["per_week"] * 100
                    if cn and fr and fr["per_week"] else None)
        add(
            kind="cargo", scope="all", series=series("is_cargo"), lead=True,
            title=(f"{cn_share:.0f}% of the freighters overhead are flown by "
                   "Chinese cargo airlines" if cn_share and cn_share >= 25
                   else "All-cargo operator movements"),
            metric=f"{fr['per_week']:,.0f}" if fr else f"{cg:,}",
            metric_label="freighter flights a week" if fr else "flights",
            comparison=(f"{cn['per_week']:,.0f} a week on China-Europe rotations · "
                        f"{len(cargo_ops)} freight operators · {cg / total * 100:.1f}% of all traffic"
                        if cn else f"{len(cargo_ops)} operators · {cg / total * 100:.1f}% of traffic"),
            where=", ".join(o.get("name") or o["key"] for o in cargo_ops[:5]),
            interpretation="Freighters between China and the European cargo hubs - Liège, "
                           "Brussels, Stansted, Billund - cross Denmark on the great "
                           "circle, so one antenna in Aarhus hears a large share of that "
                           "lane: e-commerce parcels westbound, machinery and luxury "
                           "goods eastbound. The rest is the European overnight express "
                           "network. Neither follows the passenger timetable.",
            caveat="Cargo is attributed by operator, so only all-cargo airlines are "
                   "counted. Freight in the hold of passenger aircraft - roughly half "
                   "of air cargo worldwide - is invisible here, and a flight is not a "
                   "tonnage: load factors are not observable.",
            confidence="observed",
            sql=("SELECT operator, carrier_country, COUNT(*) AS flights\n"
                 "FROM flights WHERE is_cargo\n"
                 "GROUP BY 1, 2 ORDER BY 3 DESC;"),
        )

    # Readings before profile, then by confidence, then by the size of the
    # move. The old sort was confidence alone, which put a bare cargo count
    # marked "moderate" above every actual finding on the page.
    order = {"strong": 0, "moderate": 1, "observed": 2, "early": 3}

    def magnitude(s: dict) -> float:
        try:
            return abs(float(str(s.get("metric", "")).rstrip("%").replace(",", "")))
        except ValueError:
            return 0.0

    sig.sort(key=lambda s: (s["category"] != "reading",
                            not s["lead"],
                            order.get(s["confidence"], 9),
                            -magnitude(s) if s["category"] == "reading" else s["_i"]))
    for s in sig:
        s.pop("_i", None)
    for i, s in enumerate(sig):
        s["id"] = f"sig-{i+1}"
    return sig


if __name__ == "__main__":
    raise SystemExit(main())
