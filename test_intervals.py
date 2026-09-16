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

import datetime
import io
import os
import shutil
import tempfile
import unittest

import duckdb

import analyze_intervals as ai

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


if __name__ == "__main__":
    unittest.main()
