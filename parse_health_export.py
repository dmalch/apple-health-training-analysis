#!/usr/bin/env python3
"""
Parse an Apple Health export into flat CSVs for training analysis.

Usage:
    python3 parse_health_export.py ~/Downloads/export.zip
    python3 parse_health_export.py ~/Downloads/apple_health_export/export.xml
    python3 parse_health_export.py <path> --out ./data

Accepts the export.zip straight from the iPhone (no need to unzip) and streams
it, so a multi-GB export.xml never lands in memory.

Writes into --out (default: ./data):
    workouts.csv       one row per workout, with HR / distance / energy stats
    daily_metrics.csv  one row per day: resting HR, HRV, VO2max, weight,
                       steps, active kcal, exercise minutes
and prints a summary to stdout.

Stdlib only, per repo convention.
"""

import argparse
import csv
import os
import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from xml.etree import ElementTree as ET

import athlete_profile

# ---------------------------------------------------------------- record types

# Point-in-time metrics: keep every sample, reduce to a daily mean later.
POINT_METRICS = {
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_hr",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_sdnn_ms",
    "HKQuantityTypeIdentifierVO2Max": "vo2max",
    "HKQuantityTypeIdentifierBodyMass": "weight_kg",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "walking_hr",
    "HKQuantityTypeIdentifierRespiratoryRate": "respiratory_rate",
}

# High-volume metrics: sum per day, never store individual samples.
SUM_METRICS = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_kcal",
    "HKQuantityTypeIdentifierAppleExerciseTime": "exercise_min",
    "HKQuantityTypeIdentifierAppleStandTime": "stand_min",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "distance_km_total",
}

# Metrics the iPhone and the Watch both record all day. Summing every source
# inflates them by the overlap — one export showed a median of 24,851 steps/day
# summed versus 14,972 from the watch alone. For these, keep the largest single
# source's daily total instead of the sum; every other SUM_METRIC comes from one
# device and is safe to add up.
PER_SOURCE_MAX = {"steps", "distance_km_total"}

DAILY_FIELDS = ["date"] + sorted(set(POINT_METRICS.values()) | set(SUM_METRICS.values()))

# Workout-level statistics we pull out of <WorkoutStatistics> children.
WORKOUT_STAT_KEYS = {
    "HKQuantityTypeIdentifierHeartRate": "hr",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_kcal",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "basal_kcal",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "distance_km",
    "HKQuantityTypeIdentifierDistanceCycling": "distance_km",
    "HKQuantityTypeIdentifierDistanceSwimming": "distance_km",
    "HKQuantityTypeIdentifierStepCount": "steps",
}

FOOT_ACTIVITIES = {
    "Running",
    "Walking",
    "Hiking",
    "CrossCountrySkiing",
    "Elliptical",
    "StairClimbing",
}

WORKOUT_FIELDS = [
    "start",
    "end",
    "date",
    "weekday",
    "activity",
    "source",
    "duration_min",
    "distance_km",
    "active_kcal",
    "total_kcal",
    "avg_hr",
    "max_hr",
    "min_hr",
    "pace_min_per_km",
    "speed_kmh",
    "indoor",
]


def parse_dt(value):
    """Apple stamps look like '2026-08-01 07:00:00 +0200'."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_km(value, unit):
    """Normalise a distance to km."""
    v = to_float(value)
    if v is None:
        return None
    unit = (unit or "").lower()
    if unit in ("km",):
        return v
    if unit in ("mi",):
        return v * 1.609344
    if unit in ("m",):
        return v / 1000.0
    if unit in ("yd",):
        return v * 0.0009144
    return v


def to_min(value, unit):
    v = to_float(value)
    if v is None:
        return None
    unit = (unit or "").lower()
    if unit in ("min",):
        return v
    if unit in ("sec", "s"):
        return v / 60.0
    if unit in ("hr", "h"):
        return v * 60.0
    return v


def to_kg(value, unit):
    v = to_float(value)
    if v is None:
        return None
    unit = (unit or "").lower()
    if unit == "lb":
        return v * 0.45359237
    return v


def open_export(path):
    """Yield a readable file object for export.xml, from a zip, dir, or file."""
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        for candidate in (
            os.path.join(path, "export.xml"),
            os.path.join(path, "apple_health_export", "export.xml"),
        ):
            if os.path.exists(candidate):
                return open(candidate, "rb")
        raise SystemExit(f"No export.xml found under {path}")

    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith("export.xml")]
        if not names:
            raise SystemExit(f"No export.xml inside {path}")
        # Prefer the shortest path — avoids export_cda.xml style siblings.
        return zf.open(sorted(names, key=len)[0])

    if os.path.exists(path):
        return open(path, "rb")

    raise SystemExit(f"Not found: {path}")


def parse(path):
    workouts = []
    daily_points = defaultdict(lambda: defaultdict(list))  # date -> metric -> [values]
    daily_sums = defaultdict(lambda: defaultdict(float))  # date -> metric -> total
    daily_by_source = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    skipped_types = defaultdict(int)

    fh = open_export(path)
    context = ET.iterparse(fh, events=("start", "end"))
    _, root = next(context)

    depth = 1
    for event, elem in context:
        if event == "start":
            depth += 1
            continue

        depth -= 1
        if depth != 1:
            # A nested child (e.g. WorkoutStatistics). Leave it attached so the
            # parent Workout handler below can still read it.
            continue

        tag = elem.tag
        if tag == "Record":
            handle_record(elem, daily_points, daily_sums, daily_by_source, skipped_types)
        elif tag == "Workout":
            row = handle_workout(elem)
            if row:
                workouts.append(row)

        elem.clear()
        root.clear()

    fh.close()

    # Collapse the per-source tallies down to one value per day.
    for day, metrics in daily_by_source.items():
        for name, by_source in metrics.items():
            daily_sums[day][name] = max(by_source.values())

    return workouts, daily_points, daily_sums, skipped_types


def handle_record(elem, daily_points, daily_sums, daily_by_source, skipped_types):
    rtype = elem.get("type")
    start = parse_dt(elem.get("startDate"))
    if start is None:
        return
    day = start.date().isoformat()
    unit = elem.get("unit")

    if rtype in POINT_METRICS:
        name = POINT_METRICS[rtype]
        value = (
            to_kg(elem.get("value"), unit) if name == "weight_kg" else to_float(elem.get("value"))
        )
        if value is not None:
            daily_points[day][name].append(value)
    elif rtype in SUM_METRICS:
        name = SUM_METRICS[rtype]
        value = (
            to_km(elem.get("value"), unit)
            if name.endswith("_km_total")
            else to_float(elem.get("value"))
        )
        if value is not None:
            if name in PER_SOURCE_MAX:
                daily_by_source[day][name][elem.get("sourceName") or ""] += value
            else:
                daily_sums[day][name] += value
    else:
        skipped_types[rtype] += 1


def handle_workout(elem):
    start = parse_dt(elem.get("startDate"))
    end = parse_dt(elem.get("endDate"))
    if start is None:
        return None

    activity = (elem.get("workoutActivityType") or "").replace(
        "HKWorkoutActivityType", ""
    ) or "Unknown"
    duration = to_min(elem.get("duration"), elem.get("durationUnit"))

    # Legacy attributes (pre-iOS 15). Newer exports use WorkoutStatistics instead.
    distance_km = to_km(elem.get("totalDistance"), elem.get("totalDistanceUnit"))
    active_kcal = to_float(elem.get("totalEnergyBurned"))
    basal_kcal = None
    avg_hr = max_hr = min_hr = None
    indoor = None

    for child in elem:
        if child.tag == "WorkoutStatistics":
            key = WORKOUT_STAT_KEYS.get(child.get("type"))
            if key == "hr":
                avg_hr = to_float(child.get("average")) or avg_hr
                max_hr = to_float(child.get("maximum")) or max_hr
                min_hr = to_float(child.get("minimum")) or min_hr
            elif key == "distance_km":
                d = to_km(child.get("sum"), child.get("unit"))
                if d is not None:
                    distance_km = d
            elif key == "active_kcal":
                v = to_float(child.get("sum"))
                if v is not None:
                    active_kcal = v
            elif key == "basal_kcal":
                basal_kcal = to_float(child.get("sum"))
        elif child.tag == "MetadataEntry" and child.get("key") == "HKIndoorWorkout":
            indoor = child.get("value") in ("1", "true", "YES")

    pace = None
    speed = None
    if distance_km and duration and distance_km > 0:
        speed = distance_km / (duration / 60.0)
        # min/km only makes sense on foot; wheels and water get speed instead.
        if activity in FOOT_ACTIVITIES:
            pace = duration / distance_km

    total_kcal = None
    if active_kcal is not None or basal_kcal is not None:
        total_kcal = (active_kcal or 0) + (basal_kcal or 0)

    return {
        "start": start.isoformat(),
        "end": end.isoformat() if end else "",
        "date": start.date().isoformat(),
        "weekday": start.strftime("%a"),
        "activity": activity,
        "source": elem.get("sourceName") or "",
        "duration_min": round(duration, 2) if duration is not None else "",
        "distance_km": round(distance_km, 3) if distance_km is not None else "",
        "active_kcal": round(active_kcal, 1) if active_kcal is not None else "",
        "total_kcal": round(total_kcal, 1) if total_kcal is not None else "",
        "avg_hr": round(avg_hr, 1) if avg_hr is not None else "",
        "max_hr": round(max_hr, 1) if max_hr is not None else "",
        "min_hr": round(min_hr, 1) if min_hr is not None else "",
        "pace_min_per_km": round(pace, 2) if pace is not None else "",
        "speed_kmh": round(speed, 2) if speed is not None else "",
        "indoor": "" if indoor is None else int(indoor),
    }


def write_workouts(workouts, out_dir):
    path = os.path.join(out_dir, "workouts.csv")
    workouts.sort(key=lambda r: r["start"])
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=WORKOUT_FIELDS)
        writer.writeheader()
        writer.writerows(workouts)
    return path


def write_daily(daily_points, daily_sums, out_dir):
    path = os.path.join(out_dir, "daily_metrics.csv")
    days = sorted(set(daily_points) | set(daily_sums))
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
        writer.writeheader()
        for day in days:
            row = {"date": day}
            for name, values in daily_points[day].items():
                row[name] = round(sum(values) / len(values), 2)
            for name, total in daily_sums[day].items():
                row[name] = round(total, 2)
            writer.writerow(row)
    return path, len(days)


def summarise(workouts, daily_points, daily_sums, skipped_types):
    out = []
    if not workouts:
        out.append("No <Workout> elements found in the export.")
        return "\n".join(out)

    first, last = workouts[0]["date"], workouts[-1]["date"]
    out.append(f"Workouts: {len(workouts)}  ({first} .. {last})")

    by_activity = defaultdict(lambda: {"n": 0, "min": 0.0, "km": 0.0, "kcal": 0.0})
    for w in workouts:
        a = by_activity[w["activity"]]
        a["n"] += 1
        a["min"] += w["duration_min"] or 0
        a["km"] += w["distance_km"] or 0
        a["kcal"] += w["active_kcal"] or 0

    out.append("")
    out.append(f"{'Activity':<28}{'n':>6}{'hours':>9}{'km':>10}{'kcal':>10}")
    for name, a in sorted(by_activity.items(), key=lambda kv: -kv[1]["n"]):
        out.append(f"{name:<28}{a['n']:>6}{a['min'] / 60:>9.1f}{a['km']:>10.1f}{a['kcal']:>10.0f}")

    # Weekly volume, most recent 12 weeks.
    by_week = defaultdict(lambda: {"n": 0, "min": 0.0, "km": 0.0})
    for w in workouts:
        d = datetime.fromisoformat(w["start"]).date()
        key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        by_week[key]["n"] += 1
        by_week[key]["min"] += w["duration_min"] or 0
        by_week[key]["km"] += w["distance_km"] or 0

    out.append("")
    out.append("Last 12 weeks:")
    out.append(f"{'week':<12}{'n':>5}{'hours':>9}{'km':>10}")
    for key in sorted(by_week)[-12:]:
        v = by_week[key]
        out.append(f"{key:<12}{v['n']:>5}{v['min'] / 60:>9.1f}{v['km']:>10.1f}")

    covered = {m for day in daily_points.values() for m in day} | {
        m for day in daily_sums.values() for m in day
    }
    out.append("")
    out.append("Daily metrics present: " + (", ".join(sorted(covered)) or "none"))

    if skipped_types:
        top = sorted(skipped_types.items(), key=lambda kv: -kv[1])[:8]
        out.append("")
        out.append("Other record types in export (not extracted), by volume:")
        for name, count in top:
            out.append(
                f"  {name.replace('HKQuantityTypeIdentifier', '').replace('HKCategoryTypeIdentifier', ''):<40}{count:>10,}"
            )

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("export", help="Path to export.zip, export.xml, or the unzipped folder")
    ap.add_argument("--out", default=None, help="Defaults to the profile data_dir.")
    athlete_profile.add_argument(ap, required=False)
    args = ap.parse_args()
    profile = athlete_profile.from_args(args, required=False)
    out = args.out or athlete_profile.field(profile, "data_dir")
    if not out:
        raise SystemExit("no output directory. Pass --out DIR or --profile NAME.")

    out_dir = os.path.expanduser(out)
    os.makedirs(out_dir, exist_ok=True)

    print(f"Parsing {args.export} ...", file=sys.stderr)
    workouts, daily_points, daily_sums, skipped = parse(args.export)

    w_path = write_workouts(workouts, out_dir)
    d_path, n_days = write_daily(daily_points, daily_sums, out_dir)

    print(f"Wrote {w_path} ({len(workouts)} workouts)", file=sys.stderr)
    print(f"Wrote {d_path} ({n_days} days)", file=sys.stderr)
    print()
    print(summarise(workouts, daily_points, daily_sums, skipped))


if __name__ == "__main__":
    main()
