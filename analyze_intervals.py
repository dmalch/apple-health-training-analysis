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
   come from the watch's own lap markers when it recorded any, and are otherwise
   detected from smoothed speed.

Needs the duckdb module (see .venv). Everything else is stdlib.
"""

import argparse
import bisect
import math
import statistics
import sys
from datetime import timedelta

import duckdb

import athlete_profile

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


def fmt_dur(seconds):
    if seconds is None:
        return "-"
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


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


def work_blocks_from_structure(con, workout_id, hr_by_t):
    """Work bouts from the blocks a structured workout was actually built from.

    `workout_blocks` carries the watch's own plan -- warm-up, work, recovery,
    cool-down -- so the boundaries are exact rather than inferred, and a bout
    that was stopped early keeps its real length instead of the nominal one.

    Which blocks are the work still has to be decided, and the rule is that a
    work bout runs hotter than the blocks on either side of it. A threshold on
    the session mean does not survive a long session: heart rate drifts up as it
    goes, and the last recoveries end up above the mean while the warm-up sits
    below it.
    """
    if not con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'workout_blocks'"
    ).fetchone()[0]:
        return []  # an XML-derived database has no such table
    rows = con.execute(
        "SELECT start_date, end_date FROM workout_blocks"
        " WHERE workout_id = ? AND NOT is_primary ORDER BY seq",
        [workout_id],
    ).fetchall()
    if len(rows) < 3 or not hr_by_t:
        return []

    stamps = [t for t, _ in hr_by_t]
    means = []
    for start, end in rows:
        lo = bisect.bisect_left(stamps, start)
        hi = bisect.bisect_right(stamps, end)
        window = [bpm for _, bpm in hr_by_t[lo:hi]]
        means.append(statistics.mean(window) if window else None)

    bounds = []
    for i, (start, end) in enumerate(rows):
        here = means[i]
        if here is None:
            continue
        neighbours = [
            means[j] for j in (i - 1, i + 1) if 0 <= j < len(rows) and means[j] is not None
        ]
        if neighbours and all(here > other for other in neighbours):
            bounds.append((start, end))
    return bounds


def find_reps(con, workout_id, points, times, speeds, expected, out, hr_by_t=None):
    """The watch's own structure first, then lap markers, then thresholded speed.

    The order matters. `workout_blocks` is the plan the session was actually run
    to, so it beats anything inferred; markers come next when they are real laps;
    speed is the last resort, and the least trustworthy -- the watch's
    RunningSpeed stream carries spikes pinned at exactly 20.0 km/h, which drag
    the "fastest sustained pace" anchor far past anything that was run.
    """
    structured = work_blocks_from_structure(con, workout_id, hr_by_t or [])
    if structured:
        print(
            f"\nUsing the watch's own workout structure: {len(structured)} work "
            f"blocks out of the session's plan.",
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
            return bounds, "markers"
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
    return [(times[a], times[b]) for a, b in runs], "speed"


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
            if ba >= above and gap <= 10:
                secs_above += gap

    return {
        "dur": dur,
        "dist_km": dist / 1000 if dist else None,
        "pace": (dur / 60) / (dist / 1000) if dist and dist > 50 else None,
        "avg_hr": statistics.mean(hrs) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "min_hr": min(hrs) if hrs else None,
        "start_hr": statistics.mean([b for t, b in inside if t <= t0 + timedelta(seconds=15)])
        if hrs
        else None,
        "end_hr": statistics.mean([b for t, b in inside if t >= t1 - timedelta(seconds=20)])
        if hrs
        else None,
        "secs_above": secs_above,
        "n_hr": len(hrs),
    }


def report_blocks(bounds, times, cum, hr_by_t, max_hr, out):
    """Interleave the work bouts with the recoveries that fall between them."""
    ordered = []
    prev_end = None
    for a, b in bounds:
        if prev_end is not None and (a - prev_end).total_seconds() > 20:
            ordered.append(("recovery", prev_end, a))
        ordered.append(("WORK", a, b))
        prev_end = b

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
    for kind, a, b in ordered:
        st = window_stats(a, b, times, cum, hr_by_t, above=0.90 * max_hr)
        if kind == "WORK":
            n_work += 1
            label = f"rep {n_work}"
        else:
            n_rest += 1
            label = f"rest {n_rest}"
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
        rows.append((kind, label, a, b, st))

    work = [r for r in rows if r[0] == "WORK"]
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
        paces = [r[4]["pace"] for r in work if r[4]["pace"]]
        if paces:
            print(f"  pace per rep: {', '.join(fmt_pace(p) + '/km' for p in paces)}", file=out)
    if rest:
        print("\n## Recoveries", file=out)
        for _, label, _a, _b, st in rest:
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
                if b1 >= thr and gap <= 10:
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
    max_hr = args.max_hr or athlete_profile.field(profile, "max_hr")
    if not max_hr:
        raise SystemExit(
            "no max-HR anchor. Pass --max-hr, or --profile NAME for a profile that "
            "sets one. Every percentage in this report is cut from that number, so "
            "guessing it would misreport every rep."
        )
    db = args.db or athlete_profile.field(profile, "db")
    if not db:
        raise SystemExit("no database. Pass --db PATH or --profile NAME.")
    tz = args.tz or athlete_profile.field(profile, "timezone") or "UTC"

    con = duckdb.connect(db, read_only=True)
    con.execute(f"SET TimeZone='{tz}'")

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

        bounds, _how = find_reps(
            con, wid, points, times, speeds, args.expect_reps, out, hr_by_t=hr_by_t
        )
        if bounds:
            report_blocks(bounds, times, cum, hr_by_t, max_hr, out)
        else:
            print("\nCould not resolve discrete blocks.", file=out)


if __name__ == "__main__":
    main()
