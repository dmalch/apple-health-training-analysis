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
    ("recovery", 840, 1020, 138),
    ("work", 1020, 1260, 177),
    ("recovery", 1260, 1440, 148),
    ("work", 1440, 1680, 180),
    ("recovery", 1680, 1860, 154),
    ("work", 1860, 2073, 180),
    ("recovery", 2073, 2253, 157),
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


def work(structure):
    """The work bouts out of what blocks_from_structure returns."""
    blocks, _signal = structure
    return [(b.start, b.end) for b in blocks if b.kind == "work"]


class StructuredBlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intervals-test-")
        self.con = duckdb.connect(os.path.join(self.tmp, "t.duckdb"))
        self.con.execute("""
            CREATE TABLE workout_blocks (
                workout_id BIGINT, seq BIGINT, is_primary BOOLEAN,
                start_date TIMESTAMPTZ, end_date TIMESTAMPTZ, duration_min DOUBLE)""")
        self.con.execute(
            "INSERT INTO workout_blocks VALUES (1, 1, true, ?, ?, ?)", [at(0), at(2553), 2553 / 60]
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
        bounds = work(ai.blocks_from_structure(self.con, 1, heart_rate()))
        self.assertEqual(
            bounds, [(at(start), at(end)) for kind, start, end, _ in BLOCKS if kind == "work"]
        )

    def test_every_block_is_labelled_warm_up_and_cool_down_included(self):
        blocks, signal = ai.blocks_from_structure(self.con, 1, heart_rate())
        self.assertEqual([b.kind for b in blocks], [kind for kind, *_ in BLOCKS])
        # No route here, so heart rate is all there is to go by.
        self.assertEqual(signal, "heart rate")

    def test_a_late_recovery_above_the_session_mean_is_not_a_rep(self):
        # The last recovery sits at 157 bpm against a session mean near 150. A
        # threshold on the mean calls it work; only comparing each block with
        # its neighbours keeps it out.
        bounds = work(ai.blocks_from_structure(self.con, 1, heart_rate()))
        self.assertNotIn((at(2073), at(2253)), bounds)

    def test_the_truncated_fourth_bout_keeps_its_real_length(self):
        bounds = work(ai.blocks_from_structure(self.con, 1, heart_rate()))
        last = bounds[-1]
        self.assertEqual((last[1] - last[0]).total_seconds(), 213.0)

    def test_find_reps_prefers_the_structured_blocks(self):
        blocks, how = ai.find_reps(
            self.con, 1, [], [], [], None, io.StringIO(), hr_by_t=heart_rate()
        )
        self.assertEqual(how, "structure")
        self.assertEqual(sum(b.kind == "work" for b in blocks), 4)

    def test_find_reps_still_falls_back_when_there_is_no_structure(self):
        self.con.execute("DELETE FROM workout_blocks WHERE NOT is_primary")
        bounds, how = ai.find_reps(
            self.con, 1, [], [], [], None, io.StringIO(), hr_by_t=heart_rate()
        )
        self.assertNotEqual(how, "structure")
        self.assertEqual(bounds, [])

    def test_an_unstructured_workout_yields_nothing(self):
        self.con.execute("DELETE FROM workout_blocks WHERE NOT is_primary")
        self.assertEqual(ai.blocks_from_structure(self.con, 1, heart_rate()), ([], None))

    def test_a_database_without_the_table_is_not_an_error(self):
        # analyze_intervals.py still runs against the XML-derived database,
        # which has no workout_blocks at all.
        self.con.execute("DROP TABLE workout_blocks")
        self.assertEqual(ai.blocks_from_structure(self.con, 1, heart_rate()), ([], None))
        bounds, _how = ai.find_reps(
            self.con, 1, [], [], [], None, io.StringIO(), hr_by_t=heart_rate()
        )
        self.assertEqual(bounds, [])

    def test_no_heart_rate_means_no_answer_rather_than_a_guess(self):
        self.assertEqual(ai.blocks_from_structure(self.con, 1, []), ([], None))


WU, W, R, CD = "warmup", "work", "recovery", "cooldown"


class LabelBlocksTest(unittest.TestCase):
    """Which block is which, from one effort figure per block.

    The figures are unit-free: pace when a route covers the session, heart rate
    otherwise. Apple's plan is a warm-up, work and recovery steps, a cool-down;
    `workout_blocks` keeps the steps but not which kind each one was.
    """

    def test_strides_after_a_tempo_block_are_all_work(self):
        # A work block can follow a work block. Requiring each rep to be harder
        # than both neighbours dropped the tempo block, which the first stride
        # outruns.
        efforts = [8.0, 9.5, 11.5, 7.5, 11.5, 7.5, 11.5, 7.5, 11.5, 7.5, 8.0]
        self.assertEqual(ai.label_blocks(efforts), [WU, W, W, R, W, R, W, R, W, R, CD])

    def test_a_set_rest_after_a_recovery_is_still_a_recovery(self):
        # So can a recovery follow a recovery: sets of short reps are split by a
        # longer rest, walked slower than the recoveries inside the set.
        efforts = [9, 14, 7, 14, 7, 4, 14, 7, 14, 7, 9]
        self.assertEqual(ai.label_blocks(efforts), [WU, W, R, W, R, R, W, R, W, R, CD])

    def test_a_cool_down_jogged_faster_than_walked_recoveries_is_still_a_cool_down(self):
        # Clearly harder than the block before it, and still not work: it sits
        # with the warm-up, nowhere near the reps.
        efforts = [10, 15, 5, 15, 5, 15, 5, 10]
        self.assertEqual(ai.label_blocks(efforts)[-1], CD)

    def test_a_rep_a_little_slower_than_the_one_before_it_is_still_work(self):
        # Fading on the last reps is not recovering. Only a step of a fifth of
        # the session's whole range counts as a change of effort.
        efforts = [9, 14, 13, 7, 14, 7, 9]
        self.assertEqual(ai.label_blocks(efforts), [WU, W, W, R, W, R, CD])

    def test_a_session_without_warm_up_or_cool_down_is_all_reps_and_recoveries(self):
        self.assertEqual(ai.label_blocks([15, 5, 15, 5, 15]), [W, R, W, R, W])

    def test_blocks_that_barely_differ_are_left_unlabelled(self):
        # Any split of a steady run into "work" and "recovery" would be noise.
        self.assertIsNone(ai.label_blocks([6.5, 6.4, 6.6]))


class PaceBeforeHeartRateTest(unittest.TestCase):
    """The strides session: pace separates what a lagging heart rate cannot.

    One-minute strides with two-minute recoveries are shorter than the time heart
    rate takes to follow effort, so a stride's mean heart rate comes out below
    the recovery after it. Comparing blocks by heart rate called every recovery a
    rep and missed the tempo block altogether.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intervals-pace-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        session_db(self.tmp / "t.duckdb")
        self.con = duckdb.connect(str(self.tmp / "t.duckdb"))
        self.addCleanup(self.con.close)
        points = self.con.execute(
            "SELECT t, lat, lon, ele, speed_ms FROM route_points ORDER BY t"
        ).fetchall()
        self.times = [p[0] for p in points]
        self.cum, _speeds = ai.build_distance(points)
        self.hr = self.con.execute("SELECT t, bpm FROM hr ORDER BY t").fetchall()

    def test_every_block_is_labelled_by_pace(self):
        blocks, signal = ai.blocks_from_structure(self.con, 1, self.hr, self.times, self.cum)
        self.assertEqual(signal, "pace")
        self.assertEqual([b.kind for b in blocks], [kind for kind, *_ in STRIDES])

    def test_without_a_route_heart_rate_is_used_and_the_report_says_so(self):
        out = io.StringIO()
        _blocks, how = ai.find_reps(self.con, 1, [], [], [], None, out, hr_by_t=self.hr)
        self.assertEqual(how, "structure")
        self.assertIn("told apart by heart rate", out.getvalue())


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


class HeartRateSeriesTest(unittest.TestCase):
    """What hr_series hands on goes into min(), statistics.mean() and sorted().

    A sample without a value raises in all three, and a database built from a
    backup before the converter dropped deleted samples carries thousands of
    them. Two samples at one instant must also come back in the same order on
    every run, or whichever is first differs between two identical reports.
    """

    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)
        self.con.execute(
            "CREATE TABLE hr ("
            " t TIMESTAMPTZ, bpm DOUBLE, sensor VARCHAR, device_name VARCHAR, source_name VARCHAR)"
        )
        self.con.executemany(
            "INSERT INTO hr VALUES (?, ?, 'watch', 'Apple Watch', 'Watch')",
            [
                (at(0), 150.0),
                (at(1), None),  # a deleted sample, at the instant of a real one
                (at(1), 152.0),
                (at(2), 160.0),  # a tie, inserted against value order
                (at(2), 140.0),
            ],
        )

    def test_samples_without_a_value_are_left_out(self):
        got = ai.hr_series(self.con, at(0), at(10))
        self.assertEqual(len(got), 4)
        self.assertNotIn(None, [row[1] for row in got])

    def test_samples_sharing_an_instant_come_back_in_value_order(self):
        got = ai.hr_series(self.con, at(0), at(10))
        self.assertEqual([row[1] for row in got], [150.0, 152.0, 140.0, 160.0])

    def test_the_sensor_audit_runs_over_them(self):
        samples = ai.hr_series(self.con, at(0), at(10))
        per_sensor = ai.sensor_audit(samples, at(0), at(10), io.StringIO())
        self.assertEqual(len(per_sensor["watch"]), 4)


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

# What the watch's own plan says of each STRIDES block, as the converter stores
# it: the kind, and the pace target in m/s -- 6:10/km for the tempo block, 5:00/km
# for the strides.
STRIDES_PLAN = [
    ("warmup", None),
    ("work", 1000 / 370),
    *[("work", 1000 / 300), ("recovery", None)] * 4,
    ("cooldown", None),
]


def bulk_insert(con, table, rows, beside):
    """Load rows through a CSV: executemany takes seconds over a session's worth."""
    staging = f"{beside}.{table}.csv"
    with open(staging, "w", newline="") as fh:
        csv.writer(fh).writerows(
            [v.isoformat() if isinstance(v, datetime.datetime) else v for v in row] for row in rows
        )
    con.execute(f"COPY {table} FROM '{staging}' (HEADER false)")
    os.remove(staging)


def session_db(path, plan=STRIDES, base=STRIDES_BASE, sensor="strap", recorded=None):
    """Write a one-workout database with the tables analyze_intervals.py reads.

    `recorded` is the watch's plan per block, (kind, pace target in m/s), as a
    converter that reads it stores them; None builds the older table without.
    """

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
    if recorded is not None:
        con.execute(
            "ALTER TABLE workout_blocks ADD COLUMN kind VARCHAR;"
            "ALTER TABLE workout_blocks ADD COLUMN target_type VARCHAR;"
            "ALTER TABLE workout_blocks ADD COLUMN target_min DOUBLE;"
            "ALTER TABLE workout_blocks ADD COLUMN target_max DOUBLE;"
        )
        for i, (kind, pace_ms) in enumerate(recorded, 1):
            con.execute(
                "UPDATE workout_blocks SET kind = ?, target_type = ?, target_min = ?,"
                " target_max = ? WHERE NOT is_primary AND seq = ?",
                [kind, pace_ms and "instantaneous_pace", pace_ms, pace_ms, i],
            )
    con.close()


class RecordedPlanTest(unittest.TestCase):
    """A database whose converter read the watch's plan: no inference needed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intervals-plan-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def structure(self, plan=STRIDES, recorded=STRIDES_PLAN):
        session_db(self.tmp / "t.duckdb", plan=plan, recorded=recorded)
        con = duckdb.connect(str(self.tmp / "t.duckdb"))
        self.addCleanup(con.close)
        self.con = con
        points = con.execute("SELECT t, lat, lon, ele, speed_ms FROM route_points ORDER BY t")
        points = points.fetchall()
        cum, _speeds = ai.build_distance(points)
        hr = con.execute("SELECT t, bpm FROM hr ORDER BY t").fetchall()
        return ai.blocks_from_structure(con, 1, hr, [p[0] for p in points], cum)

    def test_the_plan_labels_a_recovery_that_was_run_like_a_rep(self):
        # The last recovery run at stride pace. By effort it is work, and the
        # inference says so; the plan says it was a recovery, and that is what
        # the watch knew.
        fast_rest = [*STRIDES[:-2], ("recovery", 120, 3.1, 150), STRIDES[-1]]
        blocks, signal = self.structure(plan=fast_rest)
        self.assertEqual(signal, "the watch's plan")
        self.assertEqual([b.kind for b in blocks], [kind for kind, _pace in STRIDES_PLAN])

    def test_a_partly_labelled_plan_is_not_mixed_with_inference(self):
        recorded = [*STRIDES_PLAN[:-1], (None, None)]
        _blocks, signal = self.structure(recorded=recorded)
        self.assertEqual(signal, "pace")

    def test_each_block_carries_its_planned_pace(self):
        blocks, _signal = self.structure()
        self.assertIsNone(blocks[0].target_pace)
        target = blocks[1].target_pace
        assert target is not None, "the tempo block lost its planned pace"
        fastest, slowest = target
        self.assertAlmostEqual(fastest, 370 / 60)
        self.assertAlmostEqual(slowest, 370 / 60)


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

    def test_the_strides_session_counts_the_tempo_block_and_four_strides_as_work(self):
        session_db(self.db)
        result = self.run_cli("--max-hr", "190", "--expect-reps", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("5 work bouts, 24:00 total (20:00, 1:00, 1:00, 1:00, 1:00)", result.stdout)
        self.assertIn("warm-up", result.stdout)
        self.assertIn("cool-down", result.stdout)
        self.assertNotIn("NOTE", result.stdout)

    def test_each_rep_is_set_against_the_pace_it_was_planned_at(self):
        session_db(self.db, recorded=STRIDES_PLAN)
        result = self.run_cli("--max-hr", "190")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("told apart by the watch's plan", result.stdout)
        (line,) = [ln for ln in result.stdout.splitlines() if "pace per rep" in ln]
        self.assertEqual(line.count("(plan 6:10)"), 1, line)
        self.assertEqual(line.count("(plan 5:00)"), 4, line)

    def test_a_plan_that_disagrees_with_expect_reps_is_flagged(self):
        session_db(self.db)
        result = self.run_cli("--max-hr", "190", "--expect-reps", "4")
        self.assertIn("NOTE: expected 4 reps", result.stdout)

    def test_a_strap_profile_without_strap_samples_still_refuses_to_guess(self):
        session_db(self.db, sensor="watch")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--max-hr", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
