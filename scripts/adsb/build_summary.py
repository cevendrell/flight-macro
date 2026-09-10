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

    # Every "change" figure on the site — per country, region, operator and
    # type, and the shift signals built from them — compares the latest
    # complete week against the complete week before it. It used to compare
    # the last 24 hours with the 24 before, which on this data is mostly a
    # measure of which weekday each window landed on: a Wednesday against a
    # Tuesday, on a handful of flights, reported as a 67% move. A week against
    # a week holds one of every weekday on each side, so what is left is
    # change. Until two such weeks exist the comparison is withheld entirely,
    # and the site says on what date it will start.
    compare = weekly_compare(daily)
    baseline_complete = compare is not None

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
                  if compare else "FALSE")
        in_prev = (f"day BETWEEN '{compare['prev_from']}' AND '{compare['prev_to']}'"
                   if compare else "FALSE")
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

    signals = detect_signals(
        con, countries_roll, regions, operators, types, daily,
        span_days, total, compare, totals, baseline_complete, week,
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
        "daily": daily,
        "hourly": hourly,
        "week": week,
        "weeks": weeks,
        "months": months,
        "trend": trend,
        "sky": sky,
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
    them. Returns None unless both exist in full — a partial baseline is what
    turns "the receiver was switched on" into a 200% increase.
    """
    have = {d["day"] for d in daily if not d["partial"]}
    if len(have) < 14:
        return None
    newest = datetime.fromisoformat(max(have))
    days = [(newest - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    if not all(d in have for d in days):
        return None
    return {"kind": "week",
            "prev_from": days[0], "prev_to": days[6],
            "cur_from": days[7], "cur_to": days[13]}


def build_sky(con, origin=(56.16, 10.20), sample=1200, sectors=72) -> dict | None:
    """
    The receiver's footprint, small enough to ship inside the summary.

    The front page opens on this: every first contact on record by bearing and
    ground distance, plus the 95th-percentile reach in each 5° sector. Method
    draws the same figure from the full record through the query engine; here
    it is a seeded sample of the points and the envelope, a few kilobytes, so
    the instrument is on screen before anything heavier has loaded.
    """
    rows = con.execute(
        "SELECT first_lat, first_lon FROM fx "
        "WHERE first_lat IS NOT NULL AND first_lon IS NOT NULL"
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
    scale = math.ceil(max(p95 * 1.25, 120) / 50) * 50

    buckets: list[list[float]] = [[] for _ in range(sectors)]
    for km, brg in pts:
        buckets[int(brg / (2 * math.pi) * sectors) % sectors].append(km)
    env = [sorted(b)[int(0.95 * len(b))] if len(b) >= 4 else None for b in buckets]

    keep = pts if len(pts) <= sample else random.Random(1090).sample(pts, sample)
    return {
        "n": len(pts), "scale": scale, "p50": round(p50), "p95": round(p95),
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
        "days": days,
        "dow": dow_profile,
        "mean_per_day": round(mean, 1),
        "complete_days": len(full),
        # Below two observations per weekday the "baseline" is a single number
        # being compared against itself, which is not a baseline.
        "ready": all(p["n"] >= 2 for p in dow_profile) and len(dow_profile) == 7,
    }


def build_weeks(con, daily, kind_meta) -> list[dict]:
    """
    Every complete week the record holds, newest first.

    The windows are *stepped*, not rolling: seven days, then the seven before
    that, and so on back from the most recent complete day. A rolling window
    would give a new "week" every day, but consecutive ones would share six of
    their seven days — a week-over-week change computed across them is mostly
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
        if not all(d in have for d in days):
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
        # A `reading` has a direction — something moved, or runs one way. A
        # `profile` describes what the record is: a share, a count, a rarity.
        # An analyst wants the first kind first, and should not have to work
        # out which is which from the wording.
        kw.setdefault("category", "profile")
        sig.append(kw)

    full_days = [d for d in daily if not d["partial"]]

    days_sql = ", ".join(f"'{d['day']}'" for d in full_days)

    def series(where: str | None = None) -> list[dict]:
        """One value per complete day, for the sparkline beside a signal.
        A day with nothing matching reads as zero rather than disappearing,
        so the shape stays aligned to the calendar."""
        if not full_days:
            return []
        w = f"WHERE day IN ({days_sql})" + (f" AND ({where})" if where else "")
        got = dict(con.execute(f"SELECT day, COUNT(*) FROM fx {w} GROUP BY 1").fetchall())
        return [{"k": d["day"][5:], "v": int(got.get(d["day"], 0) or 0)} for d in full_days]

    def week_series(kind: str | None = None) -> list[dict]:
        """The seven days of the week, in order, for one kind or for all."""
        return [{"k": d["dow"], "v": d["kinds"].get(kind, 0) if kind else d["flights"]}
                for d in week["days"]]

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

        if charter and charter["lift_pct"] is not None and charter["lift_pct"] < 0:
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
        need = max(0, 14 - len(full_days))
        eta = ((datetime.fromisoformat(full_days[-1]["day"]) + timedelta(days=need + 1))
               .strftime("%-d %B") if full_days else "later")
        add(
            kind="coverage", scope="all", series=series(),
            title=f"Week-over-week comparisons begin around {eta}",
            metric=f"{len(full_days)}", metric_label="of 14 complete days",
            comparison=f"{need} more complete day{'s' if need != 1 else ''} needed",
            where=None,
            interpretation="Every change figure here is one complete week against the "
                           "complete week before it — one of every weekday on each side, "
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

    # 1) Busiest day on record — complete days only.
    if len(full_days) >= 2:
        peak = max(full_days, key=lambda d: d["flights"])
        others = [d["flights"] for d in full_days if d["day"] != peak["day"]]
        avg = sum(others) / len(others) if others else 0
        add(
            kind="record", scope="all", series=series(),
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
    # Thirty flights a week on each side is the floor: below it a 20% move is
    # six aircraft, which is one airline changing a rotation.
    movers = [
        c for c in countries
        if c["change_pct"] is not None and c["prev"] >= 30 and c["cur"] >= 30
    ]
    movers.sort(key=lambda c: -abs(c["change_pct"]))
    for c in movers[:3]:
        up = c["change_pct"] > 0
        add(
            kind="shift", scope="country", entity=c["key"], category="reading",
            series=series(f"reg_cc = '{c['key']}'"),
            title=f"{c['name']}-registered traffic {'up' if up else 'down'} "
                  f"{abs(c['change_pct']):.0f}% week on week",
            metric=f"{c['change_pct']:+.0f}%", metric_label="vs the previous week",
            comparison=f"{c['cur']} flights vs {c['prev']} the week before",
            where=c.get("region"),
            interpretation=(
                f"A week against a week removes the weekday cycle, so this is closer "
                f"to a real move in {c['name']}-registered flying — though schedule "
                "changes, aircraft rotation and weather routing can each produce it."
            ),
            caveat="Two weeks is the shortest baseline that makes this comparison "
                   "honest, not a long one. Watch whether it persists.",
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
            series=series(f"reg_cc = '{c['key']}'"),
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

    # 6) All-cargo operators. Small sample — say so plainly.
    cargo_ops = [o for o in operators if o.get("cargo_operator")]
    cg = sum(o["flights"] for o in cargo_ops)
    if cg:
        add(
            kind="cargo", scope="all", series=series("is_cargo"),
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
                            order.get(s["confidence"], 9),
                            -magnitude(s)))
    for i, s in enumerate(sig):
        s["id"] = f"sig-{i+1}"
    return sig


if __name__ == "__main__":
    raise SystemExit(main())
