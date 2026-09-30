#!/usr/bin/env python3
"""Tests for the rep detection in analyze_intervals.py.

    ./.venv/bin/python -m unittest test_intervals -v

The 2026-09-03 session is the case that motivated these: a structured 4x4 with
no GPS route, where the speed-based detector picked a "fastest sustained pace"
of 3:19/km off spikes pinned at exactly 20.0 km/h and shredded the reps, and the
watch's automatic Segment markers said nothing. The blocks were in
`workout_activities` all along.

Needs duckdb (see .venv); no real health data.
"""

import csv
import datetime
import io
import math
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import duckdb

import analyze_intervals as ai

REPO = Path(__file__).resolve().parent

BASE = datetime.datetime(2026, 9, 3, 9, 35, 51, tzinfo=datetime.UTC)

# Warm-up, then 4x4 with three-minute recoveries, then a cool-down. The fourth
# work bout was stopped 26 s early, exactly as the watch recorded it.
BLOCKS = [
    ("warmup", 0, 600, 136),
    ("work", 600, 840, 175),
    ("rest", 840, 1020, 138),
    ("work", 1020, 1260, 177),
    ("rest", 1260, 1440, 148),
    ("work", 1440, 1680, 180),
    ("rest", 1680, 1860, 154),
    ("work", 1860, 2073, 180),
    ("rest", 2073, 2253, 157),
    ("cooldown", 2253, 2553, 126),
]


def at(offset):
    return BASE + datetime.timedelta(seconds=offset)


def heart_rate():
    """One sample a second, flat inside each block."""
    out = []
    for _, start, end, bpm in BLOCKS:
        out.extend((at(s), float(bpm)) for s in range(start, end))
    return sorted(out)


class StructuredBlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intervals-test-")
        self.con = duckdb.connect(os.path.join(self.tmp, "t.duckdb"))
        self.con.execute("""
            CREATE TABLE workout_blocks (
                workout_id BIGINT, seq BIGINT, is_primary BOOLEAN,
                start_date TIMESTAMPTZ, end_date TIMESTAMPTZ, duration_min DOUBLE)""")
        self.con.execute(
            "INSERT INTO workout_blocks VALUES (1, 0, true, ?, ?, ?)", [at(0), at(2553), 2553 / 60]
        )
        for i, (_kind, start, end, _bpm) in enumerate(BLOCKS, 1):
            self.con.execute(
                "INSERT INTO workout_blocks VALUES (1, ?, false, ?, ?, ?)",
                [i, at(start), at(end), (end - start) / 60],
            )
        self.con.execute("""
            CREATE TABLE workout_events (
                workout_id BIGINT, type VARCHAR, date TIMESTAMPTZ, duration_min DOUBLE)""")

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_four_work_bouts_are_found(self):
        bounds = ai.work_blocks_from_structure(self.con, 1, heart_rate())
        self.assertEqual(
            bounds, [(at(start), at(end)) for kind, start, end, _ in BLOCKS if kind == "work"]
        )

    def test_a_late_recovery_above_the_session_mean_is_not_a_rep(self):
        # The last recovery sits at 157 bpm against a session mean near 150. A
        # threshold on the mean calls it work; only comparing each block with
        # its neighbours keeps it out.
        bounds = ai.work_blocks_from_structure(self.con, 1, heart_rate())
        self.assertNotIn((at(2073), at(2253)), bounds)

    def test_the_truncated_fourth_bout_keeps_its_real_length(self):
        bounds = ai.work_blocks_from_structure(self.con, 1, heart_rate())
        last = bounds[-1]
        self.assertEqual((last[1] - last[0]).total_seconds(), 213.0)

    def test_find_reps_prefers_the_structured_blocks(self):
        bounds, how = ai.find_reps(
            self.con, 1, [], [], [], None, io.StringIO(), hr_by_t=heart_rate()
        )
        self.assertEqual(how, "structure")
        self.assertEqual(len(bounds), 4)

    def test_find_reps_still_falls_back_when_there_is_no_structure(self):
        self.con.execute("DELETE FROM workout_blocks WHERE NOT is_primary")
        bounds, how = ai.find_reps(
            self.con, 1, [], [], [], None, io.StringIO(), hr_by_t=heart_rate()
        )
        self.assertNotEqual(how, "structure")
        self.assertEqual(bounds, [])

    def test_an_unstructured_workout_yields_nothing(self):
        self.con.execute("DELETE FROM workout_blocks WHERE NOT is_primary")
        self.assertEqual(ai.work_blocks_from_structure(self.con, 1, heart_rate()), [])

    def test_a_database_without_the_table_is_not_an_error(self):
        # analyze_intervals.py still runs against the XML-derived database,
        # which has no workout_blocks at all.
        self.con.execute("DROP TABLE workout_blocks")
        self.assertEqual(ai.work_blocks_from_structure(self.con, 1, heart_rate()), [])
        bounds, _how = ai.find_reps(
            self.con, 1, [], [], [], None, io.StringIO(), hr_by_t=heart_rate()
        )
        self.assertEqual(bounds, [])

    def test_no_heart_rate_means_no_answer_rather_than_a_guess(self):
        self.assertEqual(ai.work_blocks_from_structure(self.con, 1, []), [])


class WindowStatsTest(unittest.TestCase):
    """window_stats must survive a block whose heart-rate stream has holes.

    `hrs` being non-empty says nothing about the first 15 s or the last 20 s of
    the window, and statistics.mean([]) raises. One rep with a dropout at its
    boundary used to abort the entire report.
    """

    def stream(self, offsets_seconds, bpm=150):
        return [(BASE + datetime.timedelta(seconds=s), bpm) for s in offsets_seconds]

    def test_a_block_whose_stream_starts_late_does_not_crash(self):
        t0 = BASE
        t1 = BASE + datetime.timedelta(minutes=4)
        # First sample lands 30 s in, so the "first 15 s" slice is empty.
        st = ai.window_stats(t0, t1, [], [], self.stream(range(30, 240, 5)))
        self.assertIsNone(st["start_hr"])
        self.assertIsNotNone(st["end_hr"])
        self.assertIsNotNone(st["avg_hr"])

    def test_a_block_whose_stream_stops_early_does_not_crash(self):
        t0 = BASE
        t1 = BASE + datetime.timedelta(minutes=4)
        # Last sample is 60 s before the end, so the "last 20 s" slice is empty.
        st = ai.window_stats(t0, t1, [], [], self.stream(range(0, 180, 5)))
        self.assertIsNotNone(st["start_hr"])
        self.assertIsNone(st["end_hr"])

    def test_a_block_with_no_samples_at_all_reports_nothing_rather_than_raising(self):
        t0 = BASE
        t1 = BASE + datetime.timedelta(minutes=4)
        st = ai.window_stats(t0, t1, [], [], [])
        for key in ("avg_hr", "max_hr", "min_hr", "start_hr", "end_hr"):
            self.assertIsNone(st[key], key)

    def test_a_complete_stream_still_reports_both_ends(self):
        t0 = BASE
        t1 = BASE + datetime.timedelta(minutes=4)
        st = ai.window_stats(t0, t1, [], [], self.stream(range(0, 245, 5)))
        self.assertAlmostEqual(st["start_hr"], 150.0)
        self.assertAlmostEqual(st["end_hr"], 150.0)


# A second synthetic session, shaped like the one that exposed the faults the
# command-line tests below pin: a warm-up, a 20-minute tempo block, four
# one-minute strides with two-minute recoveries, and a cool-down.
# (kind, seconds, speed in m/s, the heart rate the block settles at)
STRIDES_BASE = datetime.datetime(2030, 5, 4, 8, 0, 0, tzinfo=datetime.UTC)
STRIDES = [
    ("warmup", 900, 2.2, 130),
    ("work", 1200, 2.6, 155),
    *[("work", 60, 3.1, 172), ("recovery", 120, 2.0, 150)] * 4,
    ("cooldown", 390, 2.2, 140),
]
# Heart rate trails effort. Modelled as a plain delay: each sample reads what the
# block LAG_S seconds earlier asked for, so a one-minute stride spends three
# quarters of itself still showing the recovery before it.
LAG_S = 45
# One sample far above anything sustained, the way a strap artifact reads.
SPIKE_S, SPIKE_BPM = 300, 199


def bulk_insert(con, table, rows, beside):
    """Load rows through a CSV: executemany takes seconds over a session's worth."""
    staging = f"{beside}.{table}.csv"
    with open(staging, "w", newline="") as fh:
        csv.writer(fh).writerows(
            [v.isoformat() if isinstance(v, datetime.datetime) else v for v in row] for row in rows
        )
    con.execute(f"COPY {table} FROM '{staging}' (HEADER false)")
    os.remove(staging)


def session_db(path, plan=STRIDES, base=STRIDES_BASE, sensor="strap"):
    """Write a one-workout database with the tables analyze_intervals.py reads."""

    def at_(s):
        return base + datetime.timedelta(seconds=s)

    edges = [0]
    for _, secs, _, _ in plan:
        edges.append(edges[-1] + secs)
    end = edges[-1]

    def block_at(s):
        for i, (lo, hi) in enumerate(zip(edges, edges[1:], strict=False)):
            if lo <= s < hi:
                return plan[i]
        return plan[-1]

    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE workouts ("
        " id BIGINT, local_date DATE, start_date TIMESTAMPTZ, end_date TIMESTAMPTZ)"
    )
    con.execute("INSERT INTO workouts VALUES (1, ?, ?, ?)", [base.date(), at_(0), at_(end)])

    # Straight east along a parallel, one fix a second at the block's speed.
    lat, lon = 10.0, 20.0
    route = []
    dist = 0.0
    for s in range(end + 1):
        speed = block_at(s)[2]
        route.append(("r1", at_(s), lat, lon, 0.0, speed))
        lon += math.degrees(speed / (ai.EARTH_R * math.cos(math.radians(lat))))
        dist += speed
    con.execute(
        "CREATE TABLE route_points ("
        " route_file VARCHAR, t TIMESTAMPTZ, lat DOUBLE, lon DOUBLE, ele DOUBLE, speed_ms DOUBLE)"
    )
    bulk_insert(con, "route_points", route, path)

    hr = []
    for s in range(end + 1):
        bpm = SPIKE_BPM if s == SPIKE_S else (block_at(s - LAG_S)[3] if s >= LAG_S else 110)
        hr.append((at_(s), float(bpm), sensor, "Test strap", "Test source"))
    con.execute(
        "CREATE TABLE hr ("
        " t TIMESTAMPTZ, bpm DOUBLE, sensor VARCHAR, device_name VARCHAR, source_name VARCHAR)"
    )
    bulk_insert(con, "hr", hr, path)

    con.execute("""
        CREATE TABLE workout_summary (
            id BIGINT, local_date DATE, local_time TIME, activity VARCHAR,
            duration_min DOUBLE, distance_km DOUBLE, avg_hr DOUBLE, max_hr DOUBLE,
            min_hr DOUBLE, active_kcal DOUBLE, indoor BOOLEAN, route_file VARCHAR,
            start_date TIMESTAMPTZ, end_date TIMESTAMPTZ,
            source_name VARCHAR, device_name VARCHAR)""")
    con.execute(
        "INSERT INTO workout_summary VALUES"
        " (1, ?, ?, 'Running', ?, ?, NULL, NULL, NULL, NULL, false, 'r1', ?, ?, 'Watch', 'Watch')",
        [base.date(), base.time(), end / 60, dist / 1000, at_(0), at_(end)],
    )

    con.execute("""
        CREATE TABLE workout_events (
            workout_id BIGINT, type VARCHAR, date TIMESTAMPTZ, duration_min DOUBLE)""")

    # Laid out the way the converter writes it: `seq` counts within the primary
    # and the non-primary rows separately, so both sets start at 1.
    con.execute("""
        CREATE TABLE workout_blocks (
            workout_id BIGINT, seq BIGINT, is_primary BOOLEAN,
            start_date TIMESTAMPTZ, end_date TIMESTAMPTZ, duration_min DOUBLE)""")
    con.execute(
        "INSERT INTO workout_blocks VALUES (1, 1, true, ?, ?, ?)", [at_(0), at_(end), end / 60]
    )
    for i, (lo, hi) in enumerate(zip(edges, edges[1:], strict=False), 1):
        con.execute(
            "INSERT INTO workout_blocks VALUES (1, ?, false, ?, ?, ?)",
            [i, at_(lo), at_(hi), (hi - lo) / 60],
        )
    con.close()


class CommandLineTest(unittest.TestCase):
    """The script end to end, run the way it is run by hand."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intervals-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "t.duckdb"
        self.profile = self.tmp / "athlete.toml"
        self.profile.write_text(
            textwrap.dedent(f"""
                timezone = "UTC"
                max_hr = "strap"
                strength = []
                hybrid = []
                db = "{self.db}"
            """)
        )

    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "HEALTH_PROFILE"}
        return subprocess.run(
            [
                sys.executable,
                str(REPO / "analyze_intervals.py"),
                "--profile-file",
                str(self.profile),
                "--workout",
                "1",
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO,
            timeout=120,
        )

    def test_a_strap_profile_takes_its_anchor_from_the_strap(self):
        # max_hr = "strap" is a valid profile, and analyze.py derives the anchor
        # from it. This script used to read the None it loads as and refuse. The
        # anchor is the strides' 172, held for a minute in all -- not the
        # one-sample 199, which a plain max() would take.
        session_db(self.db)
        result = self.run_cli("--expect-reps", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(anchor max 172)", result.stdout)

    def test_a_strap_profile_without_strap_samples_still_refuses_to_guess(self):
        session_db(self.db, sensor="watch")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--max-hr", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
