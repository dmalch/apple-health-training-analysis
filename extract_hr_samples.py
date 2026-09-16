#!/usr/bin/env python3
"""
Pull raw HeartRate samples for specific dates out of an Apple Health export.

Usage:
    python3 extract_hr_samples.py <export.zip> 2024-10-26 2026-08-09 ...

Workout summaries only carry min/avg/max, which cannot distinguish a genuine
sustained peak from a one-sample optical-sensor spike. This streams the raw
per-sample HeartRate records for the requested days so that distinction can be
made — the deciding evidence for whether an observed max HR is real.

Writes hr_<date>.csv per day into --out (default: ./data). Stdlib only.
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


def parse_dt(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    except (TypeError, ValueError):
        return None


def open_export(path):
    path = os.path.expanduser(path)
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith("export.xml")]
        if not names:
            raise SystemExit(f"No export.xml inside {path}")
        return zf.open(sorted(names, key=len)[0])
    return open(path, "rb")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("dates", nargs="+", help="YYYY-MM-DD days to extract")
    ap.add_argument("--out", default=None, help="Defaults to the profile data_dir.")
    athlete_profile.add_argument(ap, required=False)
    args = ap.parse_args()
    profile = athlete_profile.from_args(args, required=False)
    out = args.out or athlete_profile.field(profile, "data_dir")
    if not out:
        raise SystemExit("no output directory. Pass --out DIR or --profile NAME.")

    want = set(args.dates)
    out_dir = os.path.expanduser(out)
    os.makedirs(out_dir, exist_ok=True)

    samples = defaultdict(list)
    fh = open_export(args.export)
    context = ET.iterparse(fh, events=("start", "end"))
    _, root = next(context)

    depth = 1
    seen = 0
    for event, elem in context:
        if event == "start":
            depth += 1
            continue
        depth -= 1
        if depth != 1:
            continue

        if elem.tag == "Record" and elem.get("type") == "HKQuantityTypeIdentifierHeartRate":
            start = elem.get("startDate") or ""
            day = start[:10]
            if day in want:
                dt = parse_dt(start)
                try:
                    bpm = float(elem.get("value"))
                except (TypeError, ValueError):
                    bpm = None
                if dt and bpm:
                    samples[day].append((dt, bpm, elem.get("sourceName") or ""))
                    seen += 1

        elem.clear()
        root.clear()

    fh.close()

    for day in sorted(samples):
        rows = sorted(samples[day])
        path = os.path.join(out_dir, f"hr_{day}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time", "bpm", "source"])
            for dt, bpm, src in rows:
                w.writerow([dt.isoformat(), bpm, src])
        print(f"{day}: {len(rows)} samples -> {path}", file=sys.stderr)

    missing = want - set(samples)
    if missing:
        print(f"no samples found for: {', '.join(sorted(missing))}", file=sys.stderr)


if __name__ == "__main__":
    main()
