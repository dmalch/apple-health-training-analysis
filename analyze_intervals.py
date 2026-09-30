#!/usr/bin/env python3
"""
Break a single workout into work/recovery bouts and audit which sensor recorded it.

    python3 analyze_intervals.py --date 2026-08-26
    python3 analyze_intervals.py --profile NAME --workout 1503

Written for interval sessions (Norwegian 4x4 and friends), where the summary
row is useless: one avg HR over a session that alternates hard and easy says
nothing about whether the hard parts were hard enough.

Two things it reports that nothing else here does:

1. **Which sensor wrote each sample.** A chest strap and the watch's optical
   sensor both land in the same HeartRate stream. `sourceName` is localised and
   unhelpful ('Bluetooth-Gerät'); the `device` attribute names the hardware, so
   that is what the sensor split is built on. A strap that dropped out mid-
   session shows up as a coverage gap, not as missing data.

2. **Per-rep pace and heart rate**, from the GPS route where one exists. Reps
   come from the watch's own plan when the session was a custom workout, then
   from its lap markers, and are otherwise detected from smoothed speed.

Needs the duckdb module (see .venv). Everything else is stdlib.
"""

import argparse
import bisect
import math
import statistics
import sys
from datetime import datetime, timedelta
from typing import NamedTuple

import duckdb

import athlete_profile
from analyze import MAX_SAMPLE_GAP_S, resolve_max_hr

EARTH_R = 6371008.8


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def fmt_pace(min_per_km):
    if min_per_km is None or min_per_km <= 0 or min_per_km > 30:
        return "-"
    m = int(min_per_km)
    s = round((min_per_km - m) * 60)
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"


def fmt_target(target_pace):
    """ " (plan 5:00)" for a planned pace, a range when it has one, or ""."""
    if not target_pace:
        return ""
    fast, slow = (fmt_pace(p) for p in target_pace)
    return f" (plan {fast})" if fast == slow else f" (plan {fast}-{slow})"


def fmt_dur(seconds):
    if seconds is None:
        return "-"
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def mean_or_none(values):
    """Mean of a possibly empty sequence. Empty means "no answer", not an error."""
    return statistics.mean(values) if values else None


def rolling_median(values, window):
    """Centred rolling median; window is in samples."""
    n = len(values)
    half = window // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(statistics.median(values[lo:hi]))
    return out


def contiguous(flags, times, min_len_s, merge_gap_s):
    """Runs of True in `flags`, merged across short dips, then length-filtered."""
    runs = []
    start = None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            runs.append([start, i - 1])
            start = None
    if start is not None:
        runs.append([start, len(flags) - 1])

    merged = []
    for run in runs:
        if merged and (times[run[0]] - times[merged[-1][1]]).total_seconds() <= merge_gap_s:
            merged[-1][1] = run[1]
        else:
            merged.append(run)

    return [r for r in merged if (times[r[1]] - times[r[0]]).total_seconds() >= min_len_s]


# --------------------------------------------------------------------- loading


def pick_workout(con, date, workout_id):
    if workout_id:
        rows = con.execute(
            "SELECT id, local_date, local_time, activity, duration_min, distance_km,"
            " avg_hr, max_hr, min_hr, active_kcal, indoor, route_file, start_date, end_date,"
            " source_name, device_name"
            " FROM workout_summary WHERE id = ?",
            [workout_id],
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, local_date, local_time, activity, duration_min, distance_km,"
            " avg_hr, max_hr, min_hr, active_kcal, indoor, route_file, start_date, end_date,"
            " source_name, device_name"
            " FROM workout_summary WHERE local_date = ?"
            " ORDER BY duration_min DESC",
            [date],
        ).fetchall()
    if not rows:
        raise SystemExit(f"no workout found for {'id ' + str(workout_id) if workout_id else date}")
    return rows


def hr_series(con, start, end):
    return con.execute(
        "SELECT t, bpm, sensor, device_name, source_name FROM hr"
        " WHERE t BETWEEN ? AND ? ORDER BY t",
        [start, end],
    ).fetchall()


def route_series(con, route_file, start, end):
    if not route_file:
        return []
    return con.execute(
        "SELECT t, lat, lon, ele, speed_ms FROM route_points"
        " WHERE route_file = ? AND t BETWEEN ? AND ? ORDER BY t",
        [route_file, start, end],
    ).fetchall()


# -------------------------------------------------------------------- sections


def sensor_audit(samples, start, end, out):
    print("\n## Which sensor recorded this", file=out)
    if not samples:
        print("No heart-rate samples inside the workout window at all.", file=out)
        return {}

    by_sensor = {}
    for t, bpm, sensor, device, source in samples:
        by_sensor.setdefault((sensor, device or "-", source or "-"), []).append((t, bpm))

    print(
        f"\n{'sensor':<8} {'device':<22} {'source':<22} {'n':>7} {'Hz':>5} "
        f"{'from':>8} {'to':>8} {'min':>4} {'avg':>5} {'max':>4}",
        file=out,
    )
    for (sensor, device, source), rows in sorted(by_sensor.items(), key=lambda kv: -len(kv[1])):
        times = [r[0] for r in rows]
        bpms = [r[1] for r in rows]
        span = (times[-1] - times[0]).total_seconds()
        hz = len(rows) / span if span > 0 else 0
        print(
            f"{sensor:<8} {device[:22]:<22} {source[:22]:<22} {len(rows):>7} {hz:>5.2f} "
            f"{times[0].strftime('%H:%M:%S'):>8} {times[-1].strftime('%H:%M:%S'):>8} "
            f"{min(bpms):>4.0f} {statistics.mean(bpms):>5.1f} {max(bpms):>4.0f}",
            file=out,
        )

    # Coverage: how much of the workout each sensor actually spans, and where it
    # went quiet. A strap that unpaired mid-session leaves a gap, not a blank.
    total = (end - start).total_seconds()
    print(
        f"\nWorkout window {start.strftime('%H:%M:%S')}-{end.strftime('%H:%M:%S')} "
        f"({fmt_dur(total)})",
        file=out,
    )
    per_sensor = {}
    for t, bpm, sensor, _device, _source in samples:
        per_sensor.setdefault(sensor, []).append((t, bpm))
    for sensor, rows in sorted(per_sensor.items(), key=lambda kv: -len(kv[1])):
        times = [r[0] for r in rows]
        gaps = []
        for a, b in zip(times, times[1:], strict=False):
            d = (b - a).total_seconds()
            if d > 30:
                gaps.append((a, b, d))
        covered = (times[-1] - times[0]).total_seconds() - sum(g[2] for g in gaps)
        print(
            f"  {sensor:<7} covers {fmt_dur(covered)} of {fmt_dur(total)} "
            f"({100 * covered / total:.0f}%), {len(gaps)} gap(s) >30s",
            file=out,
        )
        for a, b, d in gaps[:8]:
            print(
                f"      gap {a.strftime('%H:%M:%S')} -> {b.strftime('%H:%M:%S')} ({fmt_dur(d)})",
                file=out,
            )
        if len(gaps) > 8:
            print(f"      ... {len(gaps) - 8} more", file=out)
    return per_sensor


def cross_check(per_sensor, out):
    """Do the strap and the watch agree where they overlap?"""
    if "strap" not in per_sensor or "watch" not in per_sensor:
        return
    strap = {t.replace(microsecond=0): b for t, b in per_sensor["strap"]}
    watch = {t.replace(microsecond=0): b for t, b in per_sensor["watch"]}
    shared = sorted(set(strap) & set(watch))
    print("\n## Strap vs watch, where both wrote a sample", file=out)
    if not shared:
        print("They never wrote the same second - nothing to compare directly.", file=out)
        return
    diffs = [watch[t] - strap[t] for t in shared]
    identical = sum(1 for d in diffs if d == 0)
    print(
        f"{len(shared)} shared seconds; watch - strap: mean {statistics.mean(diffs):+.1f} bpm, "
        f"median {statistics.median(diffs):+.0f}, "
        f"range {min(diffs):+.0f}..{max(diffs):+.0f}",
        file=out,
    )
    print(f"identical values: {identical} ({100 * identical / len(shared):.0f}%)", file=out)
    if identical / len(shared) > 0.8:
        print(
            "NOTE: near-total agreement means one stream is a copy of the other,\n"
            "      i.e. the watch was relaying the strap, not measuring optically.",
            file=out,
        )


def build_distance(points):
    """Cumulative metres along the GPS track, plus a per-sample speed."""
    if len(points) < 2:
        return [], []
    times = [p[0] for p in points]
    cum = [0.0]
    for a, b in zip(points, points[1:], strict=False):
        cum.append(cum[-1] + haversine(a[1], a[2], b[1], b[2]))
    speeds = []
    for i, p in enumerate(points):
        if p[4] is not None and p[4] >= 0:
            speeds.append(p[4])  # the watch's own speed, when present
        else:
            j = max(0, i - 1)
            dt = (times[i] - times[j]).total_seconds()
            speeds.append((cum[i] - cum[j]) / dt if dt > 0 else 0.0)
    return cum, speeds


class Block(NamedTuple):
    """One stretch of a session: what kind it was, when, and its planned pace."""

    kind: str  # warmup, work, recovery or cooldown
    start: datetime
    end: datetime
    # (fastest, slowest) in min/km, where the watch's plan set a pace target.
    target_pace: tuple[float, float] | None = None


def target_pace(target_type, low, high):
    """A pace target in m/s as (fastest, slowest) min/km, or None."""
    if not target_type or "pace" not in target_type or not low or not high:
        return None
    return 1000 / 60 / max(low, high), 1000 / 60 / min(low, high)


# A change of effort between two blocks counts only when it is at least this
# fraction of the session's whole spread, from its easiest block to its hardest.
EFFORT_STEP = 0.2
# Blocks whose efforts all lie within 10% of each other are one effort; any split
# of them into work and recovery would be noise.
MIN_EFFORT_SPREAD = 1.10


def label_blocks(efforts):
    """warmup / work / recovery / cooldown for each block, from its effort alone.

    `efforts` holds one figure per block, higher meaning harder -- speed or mean
    heart rate, the unit does not matter. None when the blocks barely differ.

    Apple's plan is a warm-up, then work and recovery steps, then a cool-down,
    and `workout_blocks` keeps the steps but not which kind each one was. Work
    can follow work (a tempo block, then strides that outrun it) and recovery
    can follow recovery (a set rest after the last rep's recovery), so neither
    "hotter than both neighbours" nor strict alternation holds. What does is
    hysteresis: a block keeps the kind of the block before it unless its effort
    moves clearly, by EFFORT_STEP of the session's spread, the other way.

    The first block is a warm-up when it is clearly easier than the one after
    it. The last is judged by the company it keeps rather than by the step into
    it: a cool-down jogged after walked recoveries is clearly harder than the
    block before it, and still nothing like the reps.
    """
    hi, lo = max(efforts), min(efforts)
    if hi <= lo * MIN_EFFORT_SPREAD:
        return None
    step = EFFORT_STEP * (hi - lo)

    def easier(a, b):
        return a < b - step

    kinds = ["warmup" if easier(efforts[0], efforts[1]) else "work"]
    for before, here in zip(efforts[:-2], efforts[1:-1], strict=True):
        if kinds[-1] == "work":
            kinds.append("recovery" if easier(here, before) else "work")
        else:
            kinds.append("work" if easier(before, here) else "recovery")

    last, before = efforts[-1], efforts[-2]
    if kinds[-1] == "work":
        kinds.append("cooldown" if easier(last, before) else "work")
    else:

        def distance(of_kind):
            return min(
                (abs(last - e) for e, k in zip(efforts[:-1], kinds, strict=True) if k in of_kind),
                default=math.inf,
            )

        nearer_work = distance({"work"}) < distance({"warmup", "recovery"})
        kinds.append("work" if nearer_work else "cooldown")
    return kinds


def block_speed(start, end, times, cum):
    """Metres per second over the route points inside one block, or None."""
    lo = bisect.bisect_left(times, start)
    hi = bisect.bisect_right(times, end) - 1
    if hi <= lo or times[hi] <= times[lo]:
        return None
    return (cum[hi] - cum[lo]) / (times[hi] - times[lo]).total_seconds()


def block_hr(start, end, stamps, hr_by_t):
    """Mean heart rate over the samples inside one block, or None."""
    lo = bisect.bisect_left(stamps, start)
    hi = bisect.bisect_right(stamps, end)
    return mean_or_none([bpm for _, bpm in hr_by_t[lo:hi]])


def block_efforts(rows, hr_by_t, times, cum):
    """One effort figure per block and what it measures, or (None, None).

    Pace when the route covers every block, heart rate otherwise. Heart rate
    trails effort by 30-60 s, so a one-minute stride's mean still reads mostly
    the recovery before it, and comes out below the recovery after it; pace has
    no such lag. The two cannot be mixed, so one block without a figure drops
    that signal for the whole session.
    """
    if cum:
        speeds = [block_speed(start, end, times, cum) for start, end in rows]
        if all(s is not None for s in speeds):
            return speeds, "pace"
    stamps = [t for t, _ in hr_by_t]
    means = [block_hr(start, end, stamps, hr_by_t) for start, end in rows]
    if all(m is not None for m in means):
        return means, "heart rate"
    return None, None


def blocks_from_structure(con, workout_id, hr_by_t, times=(), cum=()):
    """The blocks a structured workout was actually built from, each labelled.

    `workout_blocks` carries the watch's own plan, so the boundaries are exact
    rather than inferred, and a bout that was stopped early keeps its real
    length instead of the nominal one. Which kind each block was comes from the
    plan itself when the converter read it, and from label_blocks otherwise --
    never a mix of the two, so one unlabelled block sends the whole session to
    inference.

    Returns ([Block, ...], "the watch's plan" | "pace" | "heart rate"), or
    ([], None) when there is no plan or nothing to tell its blocks apart by.
    """
    columns = {
        name
        for (name,) in con.execute(
            "SELECT column_name FROM duckdb_columns() WHERE table_name = 'workout_blocks'"
        ).fetchall()
    }
    if not columns:
        return [], None  # an XML-derived database has no such table
    # A database built before the converter read the plan has none of these.
    planned = {"kind", "target_type", "target_min", "target_max"} <= columns
    plan_cols = "kind, target_type, target_min, target_max" if planned else "NULL, NULL, NULL, NULL"
    # `seq` counts the primary and the non-primary rows separately, so both
    # start at 1; ordering is only sound with the primary row filtered out.
    rows = con.execute(
        f"SELECT start_date, end_date, {plan_cols} FROM workout_blocks"
        " WHERE workout_id = ? AND NOT is_primary ORDER BY seq",
        [workout_id],
    ).fetchall()
    if len(rows) < 3:
        return [], None

    spans = [(a, b) for a, b, *_ in rows]
    targets = [target_pace(*plan[1:]) for _a, _b, *plan in rows]
    kinds = [kind for _a, _b, kind, *_ in rows]
    signal = "the watch's plan"
    if not all(kinds):
        efforts, signal = block_efforts(spans, hr_by_t, times, cum)
        kinds = label_blocks(efforts) if efforts else None
        if not kinds:
            return [], None
    blocks = [
        Block(kind, a, b, target)
        for kind, (a, b), target in zip(kinds, spans, targets, strict=True)
    ]
    return blocks, signal


def find_reps(con, workout_id, points, times, speeds, expected, out, hr_by_t=None, cum=None):
    """The watch's own structure first, then lap markers, then thresholded speed.

    Returns ([Block, ...], how). Only the structure knows about
    warm-up, recovery and cool-down; the other two yield work bouts alone.

    The order matters. `workout_blocks` is the plan the session was actually run
    to, so it beats anything inferred; markers come next when they are real laps;
    speed is the last resort, and the least trustworthy -- the watch's
    RunningSpeed stream carries spikes pinned at exactly 20.0 km/h, which drag
    the "fastest sustained pace" anchor far past anything that was run.
    """
    structured, signal = blocks_from_structure(con, workout_id, hr_by_t or [], times, cum or [])
    if structured:
        n_work = sum(b.kind == "work" for b in structured)
        print(
            f"\nUsing the watch's own workout structure: {len(structured)} blocks, "
            f"{n_work} of them work, told apart by {signal}.",
            file=out,
        )
        if signal == "heart rate":
            print(
                "  Heart rate lags effort by 30-60 s: trust these labels on long "
                "blocks, check them on short ones.",
                file=out,
            )
        if expected and n_work != expected:
            print(
                f"  NOTE: expected {expected} reps, and the plan has {n_work} work "
                f"blocks. Check the labels below before trusting the numbers.",
                file=out,
            )
        return structured, "structure"

    laps = con.execute(
        "SELECT type, date, duration_min FROM workout_events WHERE workout_id = ? ORDER BY date",
        [workout_id],
    ).fetchall()
    kinds = {}
    for kind, when, dur in laps:
        kinds.setdefault(kind, []).append((when, dur))
    if kinds:
        print("\n## Watch event markers", file=out)
        for kind, rows in kinds.items():
            print(f"  {kind:<16} {len(rows)}", file=out)

    marker = None
    for kind in ("Lap", "Segment", "Marker"):
        if kind in kinds and len(kinds[kind]) >= 2:
            marker = kinds[kind]
            break

    if marker and times:
        stamps = [m[0] for m in marker]
        edges = [times[0]] + stamps + [times[-1]]
        bounds = [
            (a, b) for a, b in zip(edges, edges[1:], strict=False) if (b - a).total_seconds() > 20
        ]
        session = (times[-1] - times[0]).total_seconds()
        covered = sum((b - a).total_seconds() for a, b in bounds)
        # Real lap markers leave the recoveries outside them. Markers that tile
        # the whole session end to end are the watch's automatic segmentation
        # and say nothing about the workout's structure -- ignore them.
        if session and covered / session < 0.95:
            print(f"\nUsing the watch's own markers: {len(bounds)} blocks.", file=out)
            return [Block("work", a, b) for a, b in bounds], "markers"
        print(
            f"\n  markers cover {100 * covered / session:.0f}% of the session end to end "
            f"-> automatic segmentation, not laps. Ignoring them.",
            file=out,
        )

    if not speeds:
        return [], "none"

    smooth = rolling_median(speeds, 15)
    ordered = sorted(smooth)
    fast = statistics.median(ordered[int(len(ordered) * 0.90) :] or ordered[-1:])
    # Anchor to the fast cluster, not to the midpoint of the whole session: a
    # warm-up jog sits far closer to rep pace than a walked recovery does, and a
    # midpoint threshold swallows it.
    threshold = 0.80 * fast
    print("\nDetecting reps from GPS speed.", file=out)
    print(
        f"  fastest sustained {fmt_pace(1000 / fast / 60)}/km, "
        f"counting anything under {fmt_pace(1000 / threshold / 60)}/km as work",
        file=out,
    )

    runs = contiguous([v >= threshold for v in smooth], times, min_len_s=60, merge_gap_s=25)
    if expected and len(runs) > expected:
        longest = sorted(runs, key=lambda r: -(times[r[1]] - times[r[0]]).total_seconds())
        runs = sorted(longest[:expected], key=lambda r: r[0])
        print(f"  keeping the {expected} longest of {len(longest)} candidate bouts", file=out)
    return [Block("work", times[a], times[b]) for a, b in runs], "speed"


def window_stats(t0, t1, times, cum, hr_by_t, above=None):
    idx = [i for i, t in enumerate(times) if t0 <= t <= t1]
    dist = (cum[idx[-1]] - cum[idx[0]]) if idx and cum else None
    dur = (t1 - t0).total_seconds()
    inside = [(t, b) for t, b in hr_by_t if t0 <= t <= t1]
    hrs = [b for _, b in inside]

    secs_above = None
    if above and len(inside) > 1:
        secs_above = 0.0
        for (ta, ba), (tb, _) in zip(inside, inside[1:], strict=False):
            gap = (tb - ta).total_seconds()
            if ba >= above and gap <= MAX_SAMPLE_GAP_S:
                secs_above += gap

    return {
        "dur": dur,
        "dist_km": dist / 1000 if dist else None,
        "pace": (dur / 60) / (dist / 1000) if dist and dist > 50 else None,
        "avg_hr": mean_or_none(hrs),
        "max_hr": max(hrs) if hrs else None,
        "min_hr": min(hrs) if hrs else None,
        "start_hr": mean_or_none([b for t, b in inside if t <= t0 + timedelta(seconds=15)]),
        "end_hr": mean_or_none([b for t, b in inside if t >= t1 - timedelta(seconds=20)]),
        "secs_above": secs_above,
        "n_hr": len(hrs),
    }


def report_blocks(blocks, times, cum, hr_by_t, max_hr, out):
    """One row per block, with a recovery wherever the blocks leave a gap.

    A structured workout's blocks tile the session, so nothing is inserted;
    bouts found from markers or speed get the recoveries between them.
    """
    ordered = []
    prev_end = None
    for block in blocks:
        if prev_end is not None and (block.start - prev_end).total_seconds() > 20:
            ordered.append(Block("recovery", prev_end, block.start))
        ordered.append(block)
        prev_end = block.end

    def cell(value, spec="", dash="-"):
        return dash if value is None else format(value, spec)

    print("\n## Blocks", file=out)
    header = (
        f"{'#':>2} {'kind':<9} {'start':>8} {'dur':>6} {'km':>6} {'pace':>7} "
        f"{'avgHR':>6} {'%max':>5} {'maxHR':>6} {'endHR':>6} {'>=90%':>6}"
    )
    print("\n" + header, file=out)
    print("-" * len(header), file=out)

    rows = []
    n_work = n_rest = 0
    for kind, a, b, target in ordered:
        st = window_stats(a, b, times, cum, hr_by_t, above=0.90 * max_hr)
        if kind == "work":
            n_work += 1
            label = f"rep {n_work}"
        elif kind == "recovery":
            n_rest += 1
            label = f"rest {n_rest}"
        else:
            label = {"warmup": "warm-up", "cooldown": "cool-down"}[kind]
        pct = 100 * st["avg_hr"] / max_hr if st["avg_hr"] else None
        print(
            f"{len(rows) + 1:>2} {label:<9} {a.strftime('%H:%M:%S'):>8} "
            f"{fmt_dur(st['dur']):>6} "
            f"{cell(st['dist_km'], '.2f'):>6} "
            f"{fmt_pace(st['pace']):>7} "
            f"{cell(st['avg_hr'], '.0f'):>6} "
            f"{(cell(pct, '.0f') + '%') if pct else '-':>5} "
            f"{cell(st['max_hr'], '.0f'):>6} "
            f"{cell(st['end_hr'], '.0f'):>6} "
            f"{(fmt_dur(st['secs_above']) if st['secs_above'] is not None else '-'):>6}",
            file=out,
        )
        rows.append((kind, label, a, b, st, target))

    work = [r for r in rows if r[0] == "work"]
    rest = [r for r in rows if r[0] == "recovery"]
    if work:
        print("\n## Reps at a glance", file=out)
        peaks = [r[4]["max_hr"] for r in work if r[4]["max_hr"]]
        ends = [r[4]["end_hr"] for r in work if r[4]["end_hr"]]
        durs = [r[4]["dur"] for r in work]
        print(
            f"  {len(work)} work bouts, {fmt_dur(sum(durs))} total "
            f"({', '.join(fmt_dur(d) for d in durs)})",
            file=out,
        )
        if peaks:
            print(
                f"  peak HR per rep: {', '.join(f'{p:.0f}' for p in peaks)} "
                f"({', '.join(f'{100 * p / max_hr:.0f}%' for p in peaks)} of max)",
                file=out,
            )
        if ends:
            print(f"  HR at the end of each rep: {', '.join(f'{e:.0f}' for e in ends)}", file=out)
        starts = [r[4]["start_hr"] for r in work if r[4]["start_hr"]]
        if starts:
            print(
                f"  HR entering each rep:      {', '.join(f'{s_:.0f}' for s_ in starts)}", file=out
            )
        above = [r[4]["secs_above"] for r in work if r[4]["secs_above"] is not None]
        if above:
            print(
                f"  time >=90% max inside the reps: "
                f"{', '.join(fmt_dur(a) for a in above)} "
                f"(total {fmt_dur(sum(above))} of {fmt_dur(sum(durs))})",
                file=out,
            )
        paced = [(r[4]["pace"], r[5]) for r in work if r[4]["pace"]]
        if paced:
            per_rep = ", ".join(f"{fmt_pace(p)}/km{fmt_target(t)}" for p, t in paced)
            print(f"  pace per rep: {per_rep}", file=out)
    if rest:
        print("\n## Recoveries", file=out)
        for _, label, _a, _b, st, _target in rest:
            drop = None
            if st["max_hr"] and st["min_hr"]:
                drop = st["max_hr"] - st["min_hr"]
            print(
                f"  {label}: {fmt_dur(st['dur'])}, HR fell to {cell(st['min_hr'], '.0f')}"
                f"{f' (-{drop:.0f} bpm)' if drop else ''}",
                file=out,
            )

    # Time above thresholds -- the point of a 4x4 is minutes spent up high, and
    # the ramp at the start of each rep is time the stimulus is not yet applied.
    if hr_by_t:
        print("\n## Time in the top zones (whole session)", file=out)
        span = [(t, b) for t, b in hr_by_t]
        for pct in (0.85, 0.90, 0.95):
            thr = max_hr * pct
            secs = 0
            for (t1, b1), (t2, _) in zip(span, span[1:], strict=False):
                gap = (t2 - t1).total_seconds()
                if b1 >= thr and gap <= MAX_SAMPLE_GAP_S:
                    secs += gap
            print(f"  >= {pct * 100:.0f}% max ({thr:.0f} bpm): {fmt_dur(secs)}", file=out)
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", default=None, help="Defaults to the profile database.")
    ap.add_argument("--date", help="YYYY-MM-DD; defaults to the newest workout")
    ap.add_argument("--workout", type=int, help="workout id, overrides --date")
    # No default. Every percentage below is cut from this number, so a borrowed
    # anchor does not fail -- it silently reports the wrong zone for every rep.
    ap.add_argument(
        "--max-hr",
        type=float,
        default=None,
        help="Max HR anchor. Omit to take the profile's.",
    )
    athlete_profile.add_argument(ap, required=False)
    ap.add_argument(
        "--expect-reps", type=int, default=None, help="how many work bouts you meant to run"
    )
    ap.add_argument("--tz", default=None, help="Defaults to the profile timezone.")
    args = ap.parse_args()

    profile = athlete_profile.from_args(args, required=False)
    db = args.db or athlete_profile.field(profile, "db")
    if not db:
        raise SystemExit("no database. Pass --db PATH or --profile NAME.")
    tz = args.tz or athlete_profile.field(profile, "timezone") or "UTC"

    con = duckdb.connect(db, read_only=True)
    con.execute("SET TimeZone=?", [tz])

    # Same order as analyze.py, including a profile's "strap", so one profile
    # never means two anchors.
    max_hr, _note = resolve_max_hr(args.max_hr, profile, con)
    if not max_hr:
        # With a profile loaded, the only way to get here is max_hr = "strap".
        why = (
            'The profile says "strap", and the database has no strap reading '
            "sustained for 10 s inside a workout. "
            if profile
            else ""
        )
        raise SystemExit(
            f"no max-HR anchor. {why}Pass --max-hr, or --profile NAME for a profile "
            "that sets one. Every percentage in this report is cut from that number, "
            "so guessing it would misreport every rep."
        )

    date = args.date
    if not date and not args.workout:
        date = con.execute("SELECT max(local_date) FROM workouts").fetchone()[0]
        print(f"(no --date given, using the newest workout day: {date})", file=sys.stderr)

    out = sys.stdout
    for row in pick_workout(con, date, args.workout):
        (
            wid,
            ldate,
            ltime,
            activity,
            dur,
            dist,
            avg_hr,
            max_hr_w,
            min_hr,
            kcal,
            indoor,
            route_file,
            start,
            end,
            source,
            device,
        ) = row

        print(f"\n{'=' * 78}", file=out)
        print(f"# {ldate} {ltime} - {activity} (workout id {wid})", file=out)
        print(f"{'=' * 78}", file=out)
        print(f"recorded by : {source} / {device or '-'}", file=out)
        print(
            f"duration    : {dur:.1f} min   elapsed {fmt_dur((end - start).total_seconds())}",
            file=out,
        )
        print(
            f"distance    : {f'{dist:.2f} km' if dist else '-'}"
            f"    pace {fmt_pace((dur / dist) if dist else None)}/km",
            file=out,
        )
        print(
            f"heart rate  : avg {avg_hr or '-'}  max {max_hr_w or '-'}  min {min_hr or '-'}"
            f"   (anchor max {max_hr:.0f})",
            file=out,
        )
        print(
            f"energy      : {f'{kcal:.0f} kcal' if kcal else '-'}"
            f"   indoor={indoor}   route={route_file or 'none'}",
            file=out,
        )

        samples = hr_series(con, start, end)
        per_sensor = sensor_audit(samples, start, end, out)
        cross_check(per_sensor, out)

        points = route_series(con, route_file, start, end)
        times = [p[0] for p in points]
        cum, speeds = build_distance(points)
        if not points:
            print("\nNo GPS route for this workout - pace per rep is unavailable.", file=out)

        # Pick the stream that actually covered the session, not a fixed order.
        # A treadmill run logged by the gym machine is the case that breaks a
        # priority list: the watch contributes 7 samples in the first half minute
        # while the AirPods carry all 28 minutes, and preferring the watch builds
        # every per-block number on those 7 readings. Sample count is the proxy
        # for coverage -- a strap at 1 Hz only loses to the watch's 0.2 Hz when it
        # really did drop out, and that is the right answer too. Ties go to the
        # strap, then the watch.
        rank = {"strap": 0, "watch": 1, "airpods": 2}
        candidates = [s for s in rank if per_sensor.get(s)]
        best = min(candidates, key=lambda s: (-len(per_sensor[s]), rank[s])) if candidates else None
        hr_by_t = sorted(per_sensor.get(best, []))
        if best:
            print(
                f"\nPer-block heart rate is taken from the {best} stream ({len(hr_by_t)} samples).",
                file=out,
            )
        if best == "airpods":
            print(
                "That is an optical sensor in the ear, not a strap - block "
                "averages are upper bounds, the same caveat as the watch.",
                file=out,
            )

        if not times and hr_by_t:
            times = [t for t, _ in hr_by_t]
            cum = []

        blocks, _how = find_reps(
            con, wid, points, times, speeds, args.expect_reps, out, hr_by_t=hr_by_t, cum=cum
        )
        if blocks:
            report_blocks(blocks, times, cum, hr_by_t, max_hr, out)
        else:
            print("\nCould not resolve discrete blocks.", file=out)


if __name__ == "__main__":
    main()
