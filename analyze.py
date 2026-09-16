#!/usr/bin/env python3
"""
Training-load analysis over the CSVs produced by parse_health_export.py.

Usage:
    python3 analyze.py --data ./data [--since 2024-01-01]

Prints a Markdown report to stdout. Stdlib only.

Intensity is bucketed from each session's *average* HR, which is the finest
granularity the workout summary carries. That understates time in the top zones
for interval sessions (a session averaging Z2 may contain hard Z4 repeats), so
treat the distribution as a session-level classification, not time-in-zone.
"""

import argparse
import csv
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta

import athlete_profile

# Rebound from the selected profile at startup. The module-level names exist
# because most of this file reads them; the values come from a TOML file, never
# from here. See athlete_profile.py for why each one has to be per-person.
ZONES = list(athlete_profile.DEFAULT_ZONES)

# Resistance work, excluded from the aerobic intensity distribution: heart rate
# during lifting reflects local muscular demand and rest intervals, not aerobic
# stimulus, so mixing it in makes the easy/hard split meaningless. Which Apple
# labels mean resistance work differs per person, so the set is a profile field.
STRENGTH_ACTIVITIES: set[str] = set()

# Mixed circuit work: cardio + core + dumbbells in one session. Neither pure
# aerobic base nor progressive strength, so it gets its own category and is
# reported separately rather than forced into either bucket.
HYBRID_ACTIVITIES: set[str] = set()

# Sources that write a second, parallel record for a workout the watch already
# logged -- typically a gym's own app. Counting both double-counts the time.
DUPLICATE_SOURCES: set[str] = set()

# Chest-strap samples appear under these source names in the raw HeartRate
# stream (the workout itself is still owned by the watch). Optical wrist HR
# spikes badly on long hikes -- poles, grip, cold, wrist flexion -- so peaks it
# reports alone are not trustworthy. Anchor zones on strap-measured maxima
# where they exist.
STRAP_SOURCES: tuple[str, ...] = tuple(athlete_profile.DEFAULT_STRAP_SOURCES)


def apply_profile(profile):
    """Rebind the per-person classification constants from a loaded profile."""
    global ZONES, STRENGTH_ACTIVITIES, HYBRID_ACTIVITIES, DUPLICATE_SOURCES, STRAP_SOURCES
    ZONES = profile["zones"]
    STRENGTH_ACTIVITIES = set(profile["strength"])
    HYBRID_ACTIVITIES = set(profile["hybrid"])
    DUPLICATE_SOURCES = set(profile["duplicates"])
    STRAP_SOURCES = tuple(profile["strap_sources"])
    return profile


def f(row, key):
    """Float or None from a CSV cell."""
    v = row.get(key, "")
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load(data_dir, cross_talk=frozenset()):
    with open(os.path.join(data_dir, "workouts.csv"), encoding="utf-8") as fh:
        workouts = list(csv.DictReader(fh))
    dropped = [w for w in workouts if w.get("source") in DUPLICATE_SOURCES]
    workouts = [w for w in workouts if w.get("source") not in DUPLICATE_SOURCES]
    for w in workouts:
        if w["date"] in cross_talk:
            w["avg_hr"] = w["max_hr"] = w["min_hr"] = ""
    daily = []
    dpath = os.path.join(data_dir, "daily_metrics.csv")
    if os.path.exists(dpath):
        with open(dpath, encoding="utf-8") as fh:
            daily = list(csv.DictReader(fh))
    for w in workouts:
        w["_d"] = date.fromisoformat(w["date"])
    for d in daily:
        d["_d"] = date.fromisoformat(d["date"])
    return workouts, daily, dropped


def load_strap_days(data_dir, cross_talk=frozenset()):
    """Days with meaningful chest-strap coverage, from hr_sources_by_day.csv if present."""
    path = os.path.join(data_dir, "hr_sources_by_day.csv")
    if not os.path.exists(path):
        return set()
    days = set()
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["source"] in STRAP_SOURCES and int(row["samples"]) > 50:
                days.add(row["date"])
    return days - set(cross_talk)


# ----------------------------------------------------------------- duckdb path
#
# The CSVs carry one average heart rate per session, so intensity had to be
# bucketed from that average — a session-level classification, not time in zone,
# and it is wrong in both directions: a 4x4 averaging Z4 holds 29% Z5, while the
# Monday circuit class averaging Z3 actually spends 43% of its time in Z4. The
# database has every sample, so the zone split below is real elapsed time.

FOOT_ACTIVITIES = {
    "Running",
    "Walking",
    "Hiking",
    "CrossCountrySkiing",
    "Elliptical",
    "StairClimbing",
}

# A sample only counts for the seconds until the next one, and only if that gap
# is short. Outside dense workout recording the stream is downsampled to one
# sample per minute or worse, and letting those gaps count would invent hours of
# zone time out of background sampling.
MAX_SAMPLE_GAP_S = 15

# Below this, the stream is too sparse to describe the session and the old
# session-average bucketing is the more honest answer for that workout.
MIN_ZONE_COVERAGE = 0.60


def open_db(path, tz="UTC"):
    import duckdb

    con = duckdb.connect(path, read_only=True)
    # Every TIMESTAMPTZ renders in this zone, including the start times the
    # report sorts and groups by, so it is a profile field rather than a
    # constant. Hard-coding one person's zone here silently shifted every
    # late-evening session for anybody else.
    con.execute("SET TimeZone=?", [tz])
    return con


def strap_anchor(con):
    """Highest chest-strap reading that was *sustained inside a workout*.

    Both qualifiers matter. A bare `max(bpm)` over the strap stream finds a
    reading that is not a heart rate: 118 bpm two minutes earlier, a flat 200 for
    one minute, 58 bpm four minutes later, and all of it ten minutes after the
    last workout of the day ended. That is R-wave doubling on drying electrodes as
    the strap comes off, and taking it would raise the anchor by eight beats and
    deflate every percentage in this report. Restricting to samples inside a
    workout window drops it; requiring the value to hold for ten cumulative
    seconds guards against the single-sample version of the same fault.
    """
    row = con.execute(f"""
        WITH s AS (
            SELECT h.bpm,
                   epoch(lead(h.t) OVER (PARTITION BY w.id ORDER BY h.t) - h.t) AS d
            FROM workouts w
            JOIN hr h ON h.t BETWEEN w.start_date AND w.end_date AND h.sensor = 'strap'
        )
        SELECT max(bpm) FROM (
            SELECT bpm, sum(d) AS secs FROM s
            WHERE d IS NOT NULL AND d <= {MAX_SAMPLE_GAP_S}
            GROUP BY bpm
        ) WHERE secs >= 10
    """).fetchone()
    return row[0] if row else None


def load_db(db_path, cross_talk=frozenset(), tz="UTC"):
    """Same shapes `load()` returns, read from DuckDB instead of the CSVs."""
    con = open_db(db_path, tz)
    foot = ", ".join(f"'{a}'" for a in sorted(FOOT_ACTIVITIES))
    rows = con.execute(f"""
        SELECT id,
               strftime(start_date, '%Y-%m-%dT%H:%M:%S') AS start,
               CAST(local_date AS VARCHAR)               AS date,
               left(dayname(local_date), 3)              AS weekday,
               activity, source_name AS source,
               duration_min, distance_km, active_kcal,
               avg_hr, max_hr, min_hr,
               CASE WHEN distance_km > 0 AND activity IN ({foot})
                    THEN duration_min / distance_km END  AS pace_min_per_km,
               CASE WHEN duration_min > 0
                    THEN distance_km / (duration_min / 60.0) END AS speed_kmh,
               indoor
        FROM workouts ORDER BY start_date
    """).fetchall()
    cols = [d[0] for d in con.description]
    workouts = [dict(zip(cols, r, strict=False)) for r in rows]

    dropped = [w for w in workouts if w.get("source") in DUPLICATE_SOURCES]
    workouts = [w for w in workouts if w.get("source") not in DUPLICATE_SOURCES]
    for w in workouts:
        w["_id"] = w.pop("id")
        w["_d"] = date.fromisoformat(w["date"])
        if w["date"] in cross_talk:
            w["avg_hr"] = w["max_hr"] = w["min_hr"] = None

    drows = con.execute("""
        SELECT CAST(local_date AS VARCHAR) AS date,
               resting_hr, hrv_sdnn_ms, vo2max, weight_kg,
               steps, active_kcal, exercise_min
        FROM daily_metrics ORDER BY local_date
    """).fetchall()
    dcols = [d[0] for d in con.description]
    daily = [dict(zip(dcols, r, strict=False)) for r in drows]
    for d in daily:
        d["_d"] = date.fromisoformat(d["date"])

    strap_days = {
        r[0]
        for r in con.execute("""
        SELECT DISTINCT CAST(local_date AS VARCHAR) FROM hr_sources_by_day
        WHERE sensor = 'strap' AND samples > 50
    """).fetchall()
    } - set(cross_talk)

    return con, workouts, daily, dropped, strap_days


def attach_zones(con, workouts, hr_max, cross_talk=frozenset()):
    """Real seconds-in-zone per workout, from the per-sample stream.

    The stream that actually covered the session wins, measured in elapsed
    seconds rather than sample count -- otherwise a 1 Hz strap that dropped out
    after two minutes beats a watch that recorded the whole hour. Ties go to the
    strap, then the watch. The AirPods are in the running because on treadmill
    runs logged by the gym machine the watch contributes a handful of background
    samples and the earpiece carries the session; Apple's own workout summary
    already uses that stream, so leaving it out made the zone split disagree with
    the avg-HR column two tables above it.

    Coverage is still measured rather than assumed: a session whose samples span
    less than MIN_ZONE_COVERAGE of its duration keeps the old session-average
    treatment, because a 13-minute strap trace does not describe an 87-minute
    climb.
    """
    edges = [(name, lo * hr_max, hi * hr_max) for name, lo, hi in ZONES]
    cases = ",\n".join(
        f"               sum(d) FILTER (WHERE bpm >= {lo} AND bpm < {hi}) AS z{i}"
        for i, (_, lo, hi) in enumerate(edges)
    )

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE w_sensor AS
        WITH gaps AS (
            SELECT w.id, h.sensor,
                   epoch(lead(h.t) OVER (PARTITION BY w.id, h.sensor ORDER BY h.t) - h.t) AS d
            FROM workouts w
            JOIN hr h ON h.t BETWEEN w.start_date AND w.end_date
            WHERE h.sensor IN ('strap', 'watch', 'airpods')
        ),
        cov AS (
            SELECT id, sensor, sum(d) AS covered
            FROM gaps WHERE d IS NOT NULL AND d <= {MAX_SAMPLE_GAP_S}
            GROUP BY 1, 2
        )
        SELECT id, sensor AS use_sensor FROM (
            SELECT id, sensor,
                   row_number() OVER (PARTITION BY id ORDER BY covered DESC,
                       CASE sensor WHEN 'strap' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END) AS rn
            FROM cov)
        WHERE rn = 1
    """)
    rows = con.execute(f"""
        SELECT id, sum(d) AS covered,
{cases}
        FROM (
            SELECT w.id, h.bpm,
                   epoch(lead(h.t) OVER (PARTITION BY w.id ORDER BY h.t) - h.t) AS d
            FROM workouts w
            JOIN w_sensor s USING (id)
            JOIN hr h ON h.t BETWEEN w.start_date AND w.end_date
                     AND h.sensor = s.use_sensor
        )
        WHERE d IS NOT NULL AND d <= {MAX_SAMPLE_GAP_S}
        GROUP BY id
    """).fetchall()
    by_id = {r[0]: r for r in rows}

    real = sparse = 0
    for w in workouts:
        row = by_id.get(w.get("_id"))
        dur_s = (f(w, "duration_min") or 0) * 60
        if not row or not dur_s or w["date"] in cross_talk:
            continue
        covered = row[1] or 0.0
        if covered < MIN_ZONE_COVERAGE * dur_s:
            sparse += 1
            continue
        # Scale covered time up to the session's real duration so zone hours
        # stay consistent with the volume totals elsewhere in the report.
        scale = (dur_s / covered) / 60.0
        w["_zones"] = {name: (row[2 + i] or 0.0) * scale for i, (name, _, _) in enumerate(edges)}
        w["_coverage"] = covered / dur_s
        real += 1
    return real, sparse


def reliability(workouts, out, strap_days, months=12):
    """Strap vs optical, per activity — optical over-reads when the arms move."""
    if not strap_days:
        return
    out.append(h("Heart-rate data reliability"))
    last = workouts[-1]["_d"]
    cutoff = last - timedelta(days=30 * months)
    rec = [w for w in workouts if w["_d"] >= cutoff and f(w, "avg_hr")]
    strap = [w for w in rec if w["date"] in strap_days]
    optic = [w for w in rec if w["date"] not in strap_days]

    out.append(
        f"- **{len(strap_days)} days** carry chest-strap heart rate; the rest is optical wrist."
    )
    rows = []
    for act in sorted({w["activity"] for w in strap} & {w["activity"] for w in optic}):
        s_ = [f(w, "avg_hr") for w in strap if w["activity"] == act]
        o_ = [f(w, "avg_hr") for w in optic if w["activity"] == act]
        # Fewer than eight sessions a side is not a bias estimate, it is two
        # medians over whichever sessions happened to have the strap on. And a
        # watch stops sampling optically once a strap is connected, so there is
        # rarely paired data inside a session to fall back on.
        if len(s_) >= 8 and len(o_) >= 5:
            ms, mo = statistics.median(s_), statistics.median(o_)
            rows.append([act, len(s_), f"{ms:.0f}", len(o_), f"{mo:.0f}", f"{mo - ms:+.0f}"])
    if rows:
        out.append("")
        out.append(
            table(
                ["Activity", "n strap", "median HR", "n optical", "median HR", "optical bias"],
                rows,
                ["<", ">", ">", ">", ">", ">"],
            )
        )
        out.append("")
        out.append(
            "Optical wrist HR over-reads when the arms are working — the error is "
            "largest on circuit and strength sessions and near zero on running. "
            "Treat the non-running intensity figures as an upper bound."
        )
    else:
        out.append("")
        out.append(
            "_Too few strap-measured sessions to check the optical sensor against a "
            "reference — every heart rate in this report is the wrist sensor's, "
            "unvalidated._"
        )


def h(title):
    return f"\n## {title}\n"


def table(headers, rows, aligns=None):
    aligns = aligns or ["<"] * len(headers)
    widths = [len(x) for x in headers]
    srows = []
    for r in rows:
        sr = [str(c) for c in r]
        srows.append(sr)
        for i, c in enumerate(sr):
            widths[i] = max(widths[i], len(c))
    out = ["| " + " | ".join(x.ljust(widths[i]) for i, x in enumerate(headers)) + " |"]
    out.append("|" + "|".join(("-" * (widths[i] + 2)) for i in range(len(headers))) + "|")
    for sr in srows:
        cells = []
        for i, c in enumerate(sr):
            cells.append(c.rjust(widths[i]) if aligns[i] == ">" else c.ljust(widths[i]))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def pct(n, d):
    return f"{100.0 * n / d:.0f}%" if d else "—"


# ------------------------------------------------------------------ sections


def overview(workouts, out):
    out.append(h("Overview"))
    first, last = workouts[0]["_d"], workouts[-1]["_d"]
    total_h = sum(f(w, "duration_min") or 0 for w in workouts) / 60
    span_years = max((last - first).days / 365.25, 0.01)
    out.append(
        f"- **{len(workouts):,} sessions** logged, {first} → {last} ({span_years:.1f} years)"
    )
    out.append(
        f"- **{total_h:,.0f} hours** total, averaging "
        f"**{total_h / span_years / 52:.1f} h/week** across the whole span"
    )

    by_year = defaultdict(lambda: {"n": 0, "min": 0.0, "km": 0.0})
    for w in workouts:
        y = by_year[w["_d"].year]
        y["n"] += 1
        y["min"] += f(w, "duration_min") or 0
        y["km"] += f(w, "distance_km") or 0
    rows = []
    for y in sorted(by_year):
        v = by_year[y]
        weeks = 52.0
        if y == last.year:
            weeks = max(last.timetuple().tm_yday / 7.0, 1)
        if y == first.year and y != last.year:
            weeks = max((date(y, 12, 31) - first).days / 7.0, 1)
        rows.append(
            [
                y,
                v["n"],
                f"{v['n'] / weeks:.1f}",
                f"{v['min'] / 60:,.0f}",
                f"{v['min'] / 60 / weeks:.1f}",
                f"{v['km']:,.0f}",
            ]
        )
    out.append("")
    out.append(
        table(
            ["Year", "Sessions", "/week", "Hours", "h/week", "km"],
            rows,
            ["<", ">", ">", ">", ">", ">"],
        )
    )


def activity_mix(workouts, out, months=12):
    out.append(h(f"Activity mix — last {months} months vs all time"))
    cutoff = workouts[-1]["_d"] - timedelta(days=30 * months)
    recent = [w for w in workouts if w["_d"] >= cutoff]

    def agg(ws):
        d = defaultdict(lambda: {"n": 0, "min": 0.0})
        for w in ws:
            a = d[w["activity"]]
            a["n"] += 1
            a["min"] += f(w, "duration_min") or 0
        return d

    all_a, rec_a = agg(workouts), agg(recent)
    tot_all = sum(v["min"] for v in all_a.values()) or 1
    tot_rec = sum(v["min"] for v in rec_a.values()) or 1

    # Recent minutes decide the order, but almost every activity ties at zero
    # there, and a bare sort over a set leaves those ties in set-iteration
    # order -- which Python randomises per process. That made the same export
    # produce a different table on every run. All-time minutes, then the name,
    # break the tie so a report can be diffed against last month's.
    names = sorted(
        set(all_a) | set(rec_a),
        key=lambda n: (
            -rec_a.get(n, {"min": 0.0})["min"],
            -all_a.get(n, {"min": 0.0})["min"],
            n,
        ),
    )
    rows = []
    for n in names[:14]:
        r, a = rec_a.get(n), all_a.get(n)
        rows.append(
            [
                n,
                r["n"] if r else 0,
                f"{r['min'] / 60:.0f}" if r else "0",
                pct(r["min"], tot_rec) if r else "—",
                pct(a["min"], tot_all) if a else "—",
            ]
        )
    out.append("")
    out.append(
        table(
            ["Activity", "n (recent)", "h (recent)", "% recent", "% all-time"],
            rows,
            ["<", ">", ">", ">", ">"],
        )
    )
    return recent


def consistency(workouts, out, months=12):
    out.append(h("Consistency"))
    last = workouts[-1]["_d"]
    cutoff = last - timedelta(days=30 * months)
    recent = [w for w in workouts if w["_d"] >= cutoff]

    weeks = defaultdict(lambda: {"n": 0, "min": 0.0})
    for w in recent:
        iso = w["_d"].isocalendar()
        weeks[(iso[0], iso[1])]["n"] += 1
        weeks[(iso[0], iso[1])]["min"] += f(w, "duration_min") or 0

    # Distinct calendar days, not session count: one gym visit often logs 2-3
    # separate blocks, which makes sessions/week overstate how often you train.
    days_in_week = defaultdict(set)
    for w in recent:
        iso = w["_d"].isocalendar()
        days_in_week[(iso[0], iso[1])].add(w["_d"])

    counts = [v["n"] for v in weeks.values()]
    day_counts = [len(v) for v in days_in_week.values()]
    span_weeks = len(
        {
            (d.isocalendar()[0], d.isocalendar()[1])
            for d in (cutoff + timedelta(days=i) for i in range((last - cutoff).days + 1))
        }
    )
    zero_weeks = span_weeks - len(weeks)
    train_days = len({w["_d"] for w in recent})
    span_days = (last - cutoff).days + 1

    if counts:
        out.append(
            f"- Trained on **{train_days} of {span_days} days** ({pct(train_days, span_days)})"
        )
        out.append(
            f"- Active in **{len(weeks)} of {span_weeks} weeks**; "
            f"{zero_weeks} weeks with nothing logged"
        )
        out.append(
            f"- Median **{statistics.median(day_counts):.0f} training days/week** "
            f"(range {min(day_counts)}–{max(day_counts)}) — "
            f"vs {statistics.median(counts):.0f} logged sessions/week, since a single "
            f"visit often logs several blocks"
        )
        hrs = [v["min"] / 60 for v in weeks.values()]
        out.append(
            f"- Median **{statistics.median(hrs):.1f} h/week** "
            f"(range {min(hrs):.1f}–{max(hrs):.1f})"
        )

    # Longest gaps in the recent window.
    days = sorted({w["_d"] for w in recent})
    gaps = []
    for a, b in zip(days, days[1:], strict=False):
        gap = (b - a).days
        if gap > 1:
            gaps.append((gap - 1, a, b))
    gaps.sort(reverse=True)
    if gaps:
        out.append(
            "- Longest breaks: " + ", ".join(f"**{g} days** ({a}→{b})" for g, a, b in gaps[:3])
        )

    since_last = (date.today() - last).days
    out.append(f"- Last logged session: **{last}** ({since_last} days ago)")

    dow = Counter(w["weekday"] for w in recent)
    dow_days = defaultdict(set)
    for w in recent:
        dow_days[w["weekday"]].add(w["_d"])
    order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out.append("")
    out.append(
        table(
            ["Weekday"] + order,
            [
                ["sessions"] + [dow.get(d, 0) for d in order],
                ["days trained"] + [len(dow_days[d]) for d in order],
            ],
            ["<"] + [">"] * 7,
        )
    )


def zone_split(rows, hr_max):
    """Minutes per zone for a set of workouts, plus total, easy total, and how
    many sessions had real per-sample data rather than a bucketed average."""
    buckets = defaultdict(lambda: {"n": 0, "min": 0.0})
    n_real = 0
    for w in rows:
        zones = w.get("_zones")
        if zones:
            n_real += 1
            for name, _, _ in ZONES:
                buckets[name]["min"] += zones.get(name, 0.0)
            # For the session count, file the session under wherever it spent
            # the most time — a session is in exactly one bucket, its minutes
            # are spread across several.
            buckets[max(ZONES, key=lambda z: zones.get(z[0], 0.0))[0]]["n"] += 1
            continue
        hr = f(w, "avg_hr")
        if not hr:
            continue
        frac = hr / hr_max
        for name, lo, hi in ZONES:
            if lo <= frac < hi:
                buckets[name]["n"] += 1
                buckets[name]["min"] += f(w, "duration_min") or 0
                break
    total = sum(v["min"] for v in buckets.values())
    easy = sum(buckets[n]["min"] for n, lo, _ in ZONES if lo < 0.70)
    return buckets, total, easy, n_real


def zone_profile(rows, hr_max):
    """Zone minutes for a bucket, using only sessions that have real sample data."""
    real = [w for w in rows if w.get("_zones")]
    if not real:
        return None, 0, 0
    buckets, total, _easy, _n = zone_split(real, hr_max)
    return buckets, total, len(real)


def method_delta(rows, hr_max, out):
    """What the session-average method would have said about the same sessions.

    Worth printing because it is the one number that says how much to trust the
    older reports: where the two agree, their conclusions still stand.
    """
    real = [w for w in rows if w.get("_zones")]
    if len(real) < 10:
        return
    true_b, true_t, _e, _n = zone_split(real, hr_max)
    stripped = [{k: v for k, v in w.items() if k != "_zones"} for w in real]
    old_b, old_t, _e2, _n2 = zone_split(stripped, hr_max)
    if not true_t or not old_t:
        return
    out.append("")
    out.append(
        f"How the two methods differ on the same {len(real)} sessions — the older "
        f"reports here used the session-average column:"
    )
    out.append("")
    rows_out = []
    for name, _lo, _hi in ZONES:
        t = true_b[name]["min"] / true_t * 100
        o = old_b[name]["min"] / old_t * 100
        rows_out.append([name, f"{o:.0f}%", f"{t:.0f}%", f"{t - o:+.0f} pp"])
    out.append(
        table(
            ["Zone", "session average", "real time in zone", "delta"],
            rows_out,
            ["<", ">", ">", ">"],
        )
    )


def intensity(
    workouts,
    out,
    months=12,
    hr_max=None,
    strap_days=None,
    anchor_note="supplied",
    from_db=False,
):
    out.append(h("Intensity distribution"))
    last = workouts[-1]["_d"]
    cutoff = last - timedelta(days=30 * months)
    recent = [w for w in workouts if w["_d"] >= cutoff]
    with_hr = [w for w in recent if f(w, "avg_hr") and f(w, "max_hr")]

    if not with_hr:
        out.append("_No heart-rate data on recent sessions — intensity can't be assessed._")
        return None

    if hr_max:
        out.append(f"- Max HR anchor: **{hr_max:.0f} bpm** ({anchor_note})")
    elif strap_days:
        strap_max = max(
            (f(w, "max_hr") or 0) for w in workouts if w["date"] in strap_days and f(w, "max_hr")
        )
        hr_max = strap_max
        out.append(
            f"- Max HR anchor: **{hr_max:.0f} bpm** — highest value ever recorded on "
            f"a chest strap. Optical wrist peaks run higher but are not trustworthy: "
            f"they spike on long hikes."
        )
    else:
        # No anchor and nothing to derive one from. The previous behaviour here
        # was to take the 99th percentile of session maxima, which docs/method.md
        # records as tried and discarded: it came out six beats low, and every
        # percentage in this report is cut from this one number. A report built
        # on a guessed anchor is complete, plausible and wrong, so say so instead.
        raise SystemExit(
            "No max-HR anchor, and no chest-strap data to derive one from.\n"
            "Set max_hr in the profile to a measured maximum, or pass --max-hr.\n"
            "Every zone in this report is a percentage of that number, so guessing "
            "it would misreport every session rather than fail."
        )
    out.append(
        f"- {len(with_hr)} of {len(recent)} recent sessions carry HR "
        f"({pct(len(with_hr), len(recent))})"
    )

    no_strength = [w for w in with_hr if w["activity"] not in STRENGTH_ACTIVITIES]
    aerobic = [w for w in no_strength if w["activity"] not in HYBRID_ACTIVITIES]

    buckets, tot_min, _, n_real = zone_split(aerobic, hr_max)
    tot_min = tot_min or 1
    if n_real:
        out.append(
            f"- Zone times are **real time in zone** from the per-sample heart-rate "
            f"stream for {n_real} of {len(aerobic)} aerobic sessions"
            + (
                f"; the remaining {len(aerobic) - n_real} were too sparsely "
                f"sampled and fall back to bucketing the session average"
                if len(aerobic) > n_real
                else ""
            )
        )
    else:
        out.append(
            "- Zones are bucketed from each session's **average** HR — a "
            "session-level classification, not time in zone. "
            + (
                "No session had heart-rate samples covering enough of its "
                "duration to compute real time in zone."
                if from_db
                else "Point --db at a DuckDB database for real per-sample zone times."
            )
        )

    strength_h = (
        sum(f(w, "duration_min") or 0 for w in with_hr if w["activity"] in STRENGTH_ACTIVITIES) / 60
    )
    hybrid_h = (
        sum(f(w, "duration_min") or 0 for w in with_hr if w["activity"] in HYBRID_ACTIVITIES) / 60
    )
    if strength_h or hybrid_h:
        out.append(
            f"- Zones below cover **pure aerobic work only**: excluded "
            f"{strength_h:.1f} h of resistance work and {hybrid_h:.1f} h of mixed "
            f"circuit work — in both, heart rate reflects muscular demand and rest "
            f"intervals rather than aerobic stimulus"
        )

    rows = []
    for name, lo, hi in ZONES:
        v = buckets[name]
        bar = "█" * round(20 * v["min"] / tot_min)
        hi_bpm = hr_max if hi > 1.0 else hi * hr_max
        rows.append(
            [
                name,
                f"{lo * hr_max:.0f}–{hi_bpm:.0f}",
                v["n"],
                f"{v['min'] / 60:.0f}",
                pct(v["min"], tot_min),
                bar,
            ]
        )
    out.append("")
    out.append(
        table(["Zone", "bpm", "n", "hours", "% time", ""], rows, ["<", ">", ">", ">", ">", "<"])
    )

    out.append("")
    out.append("Easy/hard split at three scopes — the honest range:")
    out.append("")
    scope_rows = []
    for label, subset in (
        ("All logged activity", with_hr),
        ("Excluding pure resistance work", no_strength),
        ("Pure aerobic only (also excluding circuit work)", aerobic),
    ):
        _, t, e, _n = zone_split(subset, hr_max)
        if t:
            scope_rows.append([label, f"{t / 60:.0f}", pct(e, t), pct(t - e, t)])
    out.append(table(["Scope", "hours", "easy", "hard"], scope_rows, ["<", ">", ">", ">"]))

    method_delta(aerobic, hr_max, out)

    # The two excluded buckets are excluded because an average heart rate over
    # a barbell class means nothing. Real time in zone does mean something, and
    # for the circuit class it is the single most surprising number here — so
    # report it separately rather than leaving the reader to assume it is easy.
    for label, subset in (
        ("Resistance work", [w for w in with_hr if w["activity"] in STRENGTH_ACTIVITIES]),
        ("Mixed circuit work", [w for w in with_hr if w["activity"] in HYBRID_ACTIVITIES]),
    ):
        buckets, total, n = zone_profile(subset, hr_max)
        if not total:
            continue
        out.append("")
        out.append(
            f"**{label}** — excluded from the split above, but its real time in "
            f"zone over {n} sampled session(s):"
        )
        out.append("")
        out.append(
            table(
                ["Zone", "hours", "% time"],
                [
                    [name, f"{buckets[name]['min'] / 60:.1f}", pct(buckets[name]["min"], total)]
                    for name, _, _ in ZONES
                ],
                ["<", ">", ">"],
            )
        )
    return hr_max


def strength_volume(workouts, out):
    """Hours, not session counts — app-logged blocks make counts meaningless."""
    out.append(h("Strength volume"))
    rows_by_year = defaultdict(list)
    for w in workouts:
        if w["activity"] in STRENGTH_ACTIVITIES:
            rows_by_year[w["_d"].year].append(w)
    if not rows_by_year:
        out.append("_No resistance work logged._")
        return

    rows = []
    for y in sorted(rows_by_year):
        ws = rows_by_year[y]
        durs = [f(w, "duration_min") or 0 for w in ws]
        mix = Counter(w["activity"] for w in ws)
        mix_s = ", ".join(f"{k[:18]}×{v}" for k, v in mix.most_common(3))
        rows.append([y, len(ws), f"{sum(durs) / 60:.1f}", f"{statistics.median(durs):.0f}", mix_s])
    out.append("")
    out.append(
        table(
            ["Year", "sessions", "hours", "median min", "composition"],
            rows,
            ["<", ">", ">", ">", "<"],
        )
    )
    out.append("")
    out.append(
        "Session counts mislead badly here: a year of 4-minute app-logged blocks "
        "outnumbers a year of hour-long classes while carrying far less volume. "
        "**Read the hours column.**"
    )

    hyb = defaultdict(list)
    for w in workouts:
        if w["activity"] in HYBRID_ACTIVITIES:
            hyb[w["_d"].year].append(w)
    if hyb:
        out.append("")
        out.append(
            "Mixed circuit work (cardio + core + dumbbells), which carries real "
            "resistance load but is not progressive strength training:"
        )
        out.append("")
        rows = []
        for y in sorted(hyb):
            ws = hyb[y]
            durs = [f(w, "duration_min") or 0 for w in ws]
            srcs = Counter(w["source"] for w in ws).most_common(2)
            rows.append(
                [
                    y,
                    len(ws),
                    f"{sum(durs) / 60:.1f}",
                    f"{statistics.median(durs):.0f}",
                    ", ".join(f"{k}×{v}" for k, v in srcs),
                ]
            )
        out.append(
            table(
                ["Year", "sessions", "hours", "median min", "source"],
                rows,
                ["<", ">", ">", ">", "<"],
            )
        )


def aerobic_progression(workouts, out):
    """Pace at a given HR over time — the cleanest fitness signal available here."""
    out.append(h("Aerobic progression (running)"))
    runs = []
    for w in workouts:
        if w["activity"] != "Running":
            continue
        pace, hr, dist = f(w, "pace_min_per_km"), f(w, "avg_hr"), f(w, "distance_km")
        # Drop sub-2km efforts and implausible paces — GPS drift and accidental
        # starts otherwise dominate the median.
        if pace is None or hr is None or dist is None:
            continue
        if dist >= 2.0 and 3.0 <= pace <= 12.0:
            runs.append(w)
    if len(runs) < 8:
        out.append("_Too few runs with both pace and HR to trend._")
        return

    by_year = defaultdict(list)
    for w in runs:
        by_year[w["_d"].year].append(w)

    rows = []
    for y in sorted(by_year):
        ws = by_year[y]
        paces = [f(w, "pace_min_per_km") or 0.0 for w in ws]
        hrs = [f(w, "avg_hr") or 0.0 for w in ws]
        dists = [f(w, "distance_km") or 0.0 for w in ws]
        med_pace = statistics.median(paces)
        med_hr = statistics.median(hrs)
        # Speed per heartbeat: higher = more aerobically efficient.
        eff = (60.0 / med_pace) / med_hr * 1000
        rows.append(
            [
                y,
                len(ws),
                f"{statistics.median(dists):.1f}",
                f"{int(med_pace)}:{round((med_pace % 1) * 60):02d}",
                f"{med_hr:.0f}",
                f"{eff:.2f}",
            ]
        )
    out.append("")
    out.append(
        table(
            ["Year", "runs", "med km", "med pace", "med HR", "efficiency*"],
            rows,
            ["<", ">", ">", ">", ">", ">"],
        )
    )
    out.append("")
    out.append(
        "\\* efficiency = (km/h ÷ avg HR) × 1000 — speed per heartbeat. "
        "Rising = aerobic fitness improving."
    )


def mmss(pace):
    return f"{int(pace)}:{round((pace % 1) * 60):02d}"


def clean_runs(workouts, min_km=4.0):
    """Runs carrying a believable pace and a heart rate. Short efforts and GPS
    nonsense otherwise dominate every median."""
    out = []
    for w in workouts:
        if w["activity"] != "Running":
            continue
        pace, hr, km = f(w, "pace_min_per_km"), f(w, "avg_hr"), f(w, "distance_km")
        if pace is None or hr is None or km is None:
            continue
        if km >= min_km and 3.0 <= pace <= 10.0:
            out.append(w)
    return out


def running_detail(workouts, out, months=12, hr_max=None):
    """Volume, race efforts and the pace-at-a-fixed-effort trend.

    Only fires when running is a real part of the load — for a runner it is the
    section that matters, and for everyone else it would be noise.
    """
    runs_all = [w for w in workouts if w["activity"] == "Running"]
    run_min = sum(f(w, "duration_min") or 0 for w in runs_all)
    total_min = sum(f(w, "duration_min") or 0 for w in workouts)
    if not total_min or run_min / total_min < 0.20:
        return

    out.append(h("Running in detail"))
    last = workouts[-1]["_d"]

    # --- volume by year
    by_year = defaultdict(lambda: {"n": 0, "km": 0.0, "min": 0.0})
    for w in runs_all:
        y = by_year[w["_d"].year]
        y["n"] += 1
        y["km"] += f(w, "distance_km") or 0
        y["min"] += f(w, "duration_min") or 0
    first_year = min(by_year)
    rows = []
    for y in sorted(by_year):
        v = by_year[y]
        weeks = 52.0
        if y == last.year:
            weeks = max(last.timetuple().tm_yday / 7.0, 1)
        if y == first_year and y != last.year:
            start = min(w["_d"] for w in runs_all if w["_d"].year == y)
            weeks = max((date(y, 12, 31) - start).days / 7.0, 1)
        rows.append(
            [y, v["n"], f"{v['km']:,.0f}", f"{v['km'] / weeks:.1f}", f"{v['min'] / 60:.0f}"]
        )
    out.append("")
    out.append(table(["Year", "runs", "km", "km/week", "hours"], rows, ["<", ">", ">", ">", ">"]))

    # --- the long ones. Anything past 30 km on a watch is a marathon or a
    # marathon-specific long run; GPS runs ~2% long, so the recorded distance
    # overshoots the certified course.
    longest = sorted(
        (w for w in runs_all if (f(w, "distance_km") or 0) >= 25), key=lambda w: w["date"]
    )
    if longest:
        out.append("")
        out.append(
            "**Efforts past 25 km** — every marathon and marathon-specific long "
            "run in the file. Watch distance runs about 2% long against a "
            "certified course:"
        )
        out.append("")
        rows = []
        for w in longest:
            km, dur = f(w, "distance_km"), f(w, "duration_min")
            pace = f(w, "pace_min_per_km")
            hr = f(w, "avg_hr")
            rows.append(
                [
                    w["date"],
                    f"{km:.1f}",
                    f"{int(dur // 60)}:{int(dur % 60):02d}",
                    mmss(pace) if pace else "—",
                    f"{hr:.0f}" if hr else "—",
                    f"{100 * hr / hr_max:.0f}%" if hr and hr_max else "—",
                ]
            )
        out.append(
            table(
                ["Date", "km", "time", "pace", "avg HR", "% max"],
                rows,
                ["<", ">", ">", ">", ">", ">"],
            )
        )

    # --- pace at a fixed effort. The single cleanest fitness signal available
    # without a lab: hold the pace band constant and watch what the heart does.
    band = [w for w in clean_runs(workouts) if 6.0 <= (f(w, "pace_min_per_km") or 0) <= 6.5]
    halves = defaultdict(list)
    for w in band:
        halves[f"{w['_d'].year}-H{1 if w['_d'].month <= 6 else 2}"].append(w)
    rows = []
    for key in sorted(halves):
        ws = halves[key]
        if len(ws) < 4:
            continue
        rows.append(
            [
                key,
                len(ws),
                mmss(statistics.median([f(w, "pace_min_per_km") for w in ws])),
                f"{statistics.median([f(w, 'avg_hr') for w in ws]):.0f}",
            ]
        )
    if len(rows) >= 3:
        out.append("")
        out.append(
            "**Heart rate at a fixed pace (6:00-6:30 /km)** — same work, "
            "measured cost. Falling = the engine is getting bigger:"
        )
        out.append("")
        out.append(
            table(["Half-year", "runs", "median pace", "median HR"], rows, ["<", ">", ">", ">"])
        )

    # --- distance mix and the race-pace check
    cutoff = last - timedelta(days=30 * months)
    recent = [w for w in runs_all if w["_d"] >= cutoff and f(w, "distance_km")]
    if recent:
        buckets = [
            (0, 5, "under 5 km"),
            (5, 8, "5-8 km"),
            (8, 12, "8-12 km"),
            (12, 16, "12-16 km"),
            (16, 21, "16-21 km"),
            (21, 999, "21 km+"),
        ]
        # A session with no distance -- treadmill, or one the watch never
        # resolved -- has nothing to bucket by, so it sits this table out. The
        # shares below still divide by every recent run, not just these.
        measured = [(w, d) for w in recent if (d := f(w, "distance_km")) is not None]
        rows = []
        for lo, hi, label in buckets:
            ws = [(w, d) for w, d in measured if lo <= d < hi]
            km = sum(d for _, d in ws)
            rows.append(
                [
                    label,
                    len(ws),
                    pct(len(ws), len(recent)),
                    f"{km:.0f}",
                    "█" * round(20 * len(ws) / len(recent)),
                ]
            )
        out.append("")
        out.append(f"Distance mix, last {months} months:")
        out.append("")
        out.append(table(["Distance", "runs", "share", "km", ""], rows, ["<", ">", ">", ">", "<"]))

        # Racing faster than you ever train is the classic amateur signature.
        races = [w for w in recent if (f(w, "distance_km") or 0) >= 40 and f(w, "pace_min_per_km")]
        training = [w for w in clean_runs(recent) if (f(w, "distance_km") or 0) < 40]
        if races and training:
            fastest_race = min(f(w, "pace_min_per_km") for w in races)
            quicker = [w for w in training if f(w, "pace_min_per_km") <= fastest_race]
            out.append("")
            out.append(
                f"- Fastest race pace in the window: **{mmss(fastest_race)} /km**. "
                f"Training runs held at that pace or better: "
                f"**{len(quicker)} of {len(training)}** "
                f"({pct(len(quicker), len(training))})"
            )


def physiology(daily, out):
    out.append(h("Physiology trends"))
    if not daily:
        out.append("_No daily metrics in export._")
        return

    metrics = [
        ("resting_hr", "Resting HR", "bpm", "lower better"),
        ("hrv_sdnn_ms", "HRV (SDNN)", "ms", "higher better"),
        ("vo2max", "VO2max", "ml/kg/min", "higher better"),
        ("weight_kg", "Weight", "kg", ""),
    ]

    by_q = defaultdict(lambda: defaultdict(list))
    for d in daily:
        q = f"{d['_d'].year}-Q{(d['_d'].month - 1) // 3 + 1}"
        for key, *_ in metrics:
            v = f(d, key)
            if v:
                by_q[q][key].append(v)

    quarters = sorted(by_q)[-10:]
    present = [m for m in metrics if any(by_q[q].get(m[0]) for q in quarters)]
    if not present:
        out.append("_No resting HR / HRV / VO2max / weight samples._")
        return

    rows = []
    for q in quarters:
        row = [q]
        for key, *_ in present:
            vals = by_q[q].get(key)
            row.append(f"{statistics.median(vals):.1f}" if vals else "—")
        rows.append(row)
    out.append("")
    out.append(table(["Quarter"] + [m[1] for m in present], rows, ["<"] + [">"] * len(present)))
    out.append("")
    for key, label, unit, direction in present:
        series = [(q, statistics.median(by_q[q][key])) for q in quarters if by_q[q].get(key)]
        if len(series) >= 2:
            delta = series[-1][1] - series[0][1]
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            note = f" ({direction})" if direction else ""
            out.append(
                f"- **{label}**: {series[0][1]:.1f} → {series[-1][1]:.1f} {unit} "
                f"{arrow} {delta:+.1f}{note}"
            )

    # VO2max is per-kg, so losing weight raises it without any aerobic gain.
    # Multiplying back out separates real engine growth from the denominator.
    abs_q = [
        (q, statistics.median(by_q[q]["vo2max"]) * statistics.median(by_q[q]["weight_kg"]) / 1000)
        for q in sorted(by_q)
        if by_q[q].get("vo2max") and by_q[q].get("weight_kg")
    ]
    if len(abs_q) >= 3:
        out.append("")
        out.append("**Absolute VO2 (VO2max × weight) — strips out the weight-loss effect:**")
        out.append("")
        out.append(table(["Quarter", "L/min"], [[q, f"{v:.2f}"] for q, v in abs_q], ["<", ">"]))
        out.append("")
        out.append(
            f"- Absolute: {abs_q[0][1]:.2f} → {abs_q[-1][1]:.2f} L/min "
            f"({abs_q[-1][1] - abs_q[0][1]:+.2f})"
        )


def load_vs_recovery(workouts, daily, out, months=12):
    """Does resting HR rise in the weeks following the heaviest training?"""
    out.append(h("Load vs recovery"))
    rhr = {d["_d"]: f(d, "resting_hr") for d in daily if f(d, "resting_hr")}
    if len(rhr) < 30:
        out.append("_Not enough resting-HR data to correlate with load._")
        return

    last = workouts[-1]["_d"]
    cutoff = last - timedelta(days=30 * months)
    wl = defaultdict(float)
    for w in workouts:
        if w["_d"] >= cutoff:
            iso = w["_d"].isocalendar()
            wl[(iso[0], iso[1])] += (f(w, "duration_min") or 0) / 60

    wr = defaultdict(list)
    for d, v in rhr.items():
        if d >= cutoff:
            iso = d.isocalendar()
            wr[(iso[0], iso[1])].append(v)

    weeks = sorted(set(wl) & set(wr))
    if len(weeks) < 8:
        out.append("_Not enough overlapping weeks._")
        return

    loads = [wl[w] for w in weeks]
    med_load = statistics.median(loads)
    heavy = [statistics.median(wr[w]) for w in weeks if wl[w] > med_load]
    light = [statistics.median(wr[w]) for w in weeks if wl[w] <= med_load]

    out.append(f"- Median weekly load: **{med_load:.1f} h**")
    if heavy and light:
        out.append(
            f"- Resting HR in heavier-than-median weeks: **{statistics.median(heavy):.1f} bpm**"
        )
        out.append(f"- Resting HR in lighter weeks: **{statistics.median(light):.1f} bpm**")
        diff = statistics.median(heavy) - statistics.median(light)
        verdict = (
            "absorbing the load well"
            if diff <= 0.5
            else "showing some strain on heavy weeks"
            if diff <= 2
            else "not recovering from heavy weeks"
        )
        out.append(f"- Difference **{diff:+.1f} bpm** → {verdict}")

    # Hardest weeks, for context.
    top = sorted(weeks, key=lambda w: -wl[w])[:5]
    rows = [
        [f"{y}-W{wk:02d}", f"{wl[(y, wk)]:.1f}", f"{statistics.median(wr[(y, wk)]):.0f}"]
        for y, wk in top
    ]
    out.append("")
    out.append(table(["Heaviest weeks", "hours", "med RHR"], rows, ["<", ">", ">"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=None,
        help="DuckDB database from health_to_duckdb.py. Defaults to "
        "<--data>/health.duckdb when that file exists.",
    )
    ap.add_argument("--data", default=None, help="CSV directory. Defaults to the profile data_dir.")
    ap.add_argument(
        "--no-db", action="store_true", help="Force the CSV path even if a database is present."
    )
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument(
        "--max-hr",
        type=float,
        default=None,
        help="Max HR anchor. Omit to take the profile's, then chest-strap data.",
    )
    athlete_profile.add_argument(ap)
    args = ap.parse_args()

    profile = apply_profile(athlete_profile.from_args(args))
    cross_talk = set(profile["cross_talk"])

    data_dir = args.data or profile["data_dir"]
    db_path = args.db or profile["db"]
    use_db = os.path.exists(db_path) and not args.no_db

    con = None
    if use_db:
        con, workouts, daily, dropped, strap_days = load_db(
            db_path, cross_talk, profile["timezone"]
        )
    else:
        workouts, daily, dropped = load(data_dir, cross_talk)
        strap_days = load_strap_days(data_dir, cross_talk)
    if not workouts:
        raise SystemExit("No workouts found.")
    workouts.sort(key=lambda w: w["start"])

    # The anchor has to be settled before zones can be cut, so it is resolved
    # here rather than inside intensity(): explicit flag, then the profile's
    # own figure, then the strap.
    hr_max = args.max_hr or profile["max_hr"]
    anchor_note = "supplied" if args.max_hr else "from the profile"
    if not hr_max and con is not None:
        hr_max = strap_anchor(con)
        anchor_note = (
            "highest chest-strap reading sustained for 10 s inside a workout — "
            "optical wrist peaks and strap artifacts on removal both run higher "
            "and are not trustworthy"
        )

    zone_note = ""
    if con is not None and hr_max:
        real, sparse = attach_zones(con, workouts, hr_max, cross_talk)
        zone_note = f"{real:,} sessions with per-sample zone data, {sparse:,} too sparse"

    out = [
        "# Training analysis",
        "",
        f"_Generated {date.today()} from Apple Health export "
        f"({len(workouts):,} sessions, {len(daily):,} days of daily metrics)._",
    ]
    out.append("")
    out.append(
        f"_Source: `{db_path}`" + (f" — {zone_note}._" if zone_note else "._")
        if use_db
        else f"_Source: CSVs in `{data_dir}` — intensity is bucketed from session "
        f"averages; build a DuckDB database for real time in zone._"
    )
    if cross_talk:
        out.append("")
        out.append(
            f"_Heart rate discarded on {len(cross_talk)} day(s) "
            f"({', '.join(sorted(cross_talk))}) — the watch was reading another "
            f"person's chest strap. Distance and pace are kept._"
        )
    if dropped:
        hrs = sum(f(w, "duration_min") or 0 for w in dropped) / 60
        out.append("")
        out.append(
            f"_Excluded {len(dropped)} duplicate records ({hrs:.1f} h) from "
            f"{', '.join(sorted(DUPLICATE_SOURCES))} — a second log of workouts the "
            f"watch already recorded._"
        )

    overview(workouts, out)
    activity_mix(workouts, out, args.months)
    consistency(workouts, out, args.months)
    hr_max = intensity(
        workouts, out, args.months, hr_max, strap_days, anchor_note, from_db=con is not None
    )
    strength_volume(workouts, out)
    aerobic_progression(workouts, out)
    running_detail(workouts, out, args.months, hr_max)
    reliability(workouts, out, strap_days, args.months)
    physiology(daily, out)
    load_vs_recovery(workouts, daily, out, args.months)

    if con is not None:
        con.close()
    print("\n".join(out))


if __name__ == "__main__":
    main()
