#!/usr/bin/env python3
"""Fixture tests for healthkit_to_duckdb.py.

    ./.venv/bin/python -m unittest test_healthkit -v

The fixture is two batches in the layout the HealthExport app writes, built by
hand: synthetic UUIDs, dates in 2030, no real data. They cover what a rebuild
from several batches has to get right -- the newest copy of a sample wins, a
deletion reported later removes a sample written earlier, a heart-rate series
comes back as its readings and is not doubled when read twice, and a planned
workout's activities become labelled blocks.
"""

import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
import uuid as uuidlib
from pathlib import Path

import duckdb

import healthkit_to_duckdb as conv

T0 = datetime.datetime(2030, 3, 4, 8, 0, tzinfo=datetime.UTC).timestamp()


def uid(n):
    # n goes in the leading bits, which is where hk_id reads; real UUIDs are
    # random there, and fixtures differing only in the tail would collide.
    return str(uuidlib.UUID(int=(n << 100) | 0xABCDEF)).upper()


def sample(n, type_, start, end=None, **kw):
    row = {"uuid": uid(n), "type": type_, "start": start, "end": end or start,
           "source": "Watch of Someone", "bundle": "com.apple.health.X", "product": "Watch7,1",
           "device": "Apple Watch"}  # fmt: skip
    row.update(kw)
    return row


def write_batch(root, name, files, manifest=True):
    d = Path(root) / "batches" / name
    d.mkdir(parents=True)
    for fname, rows in files.items():
        with open(d / f"{fname}.jsonl", "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    if manifest:
        (d / "manifest.json").write_text(json.dumps({"batch": name, "since": T0 - 86400}))
    return d


PLAN = {
    "kind": "custom",
    "activity_type": 37,
    "blocks": [
        {"role": "warmup", "iterations": 1, "steps": [{"kind": "warmup", "goal_unit": "s", "goal_value": 600}]},
        {"role": "intervals", "iterations": 2, "steps": [
            {"kind": "work", "goal_unit": "m", "goal_value": 1000, "target_type": "instantaneous_pace",
             "target_min": 2.7, "target_max": 2.9},
            {"kind": "recovery", "goal_unit": "s", "goal_value": 120},
        ]},
        {"role": "cooldown", "iterations": 1, "steps": [{"kind": "cooldown", "goal_unit": "open"}]},
    ],
}  # fmt: skip


def activity(n, start, dur, path=None):
    a = {"uuid": uid(900 + n), "index": n, "activity_type": 37, "location_type": 3,
         "start": start, "end": start + dur, "duration_s": dur,
         "statistics": [{"type": "HeartRate", "unit": "count/min", "start": start,
                         "end": start + dur, "avg": 150.0, "min": 120.0, "max": 170.0}]}  # fmt: skip
    if path:
        a["metadata"] = {"WOIntervalStepKeyPath": path}
    return a


def workout(n, start, dur, activities=(), plan=None, routes=True):
    w = sample(n, "Workout", start, start + dur, activity_type=37, duration_s=dur,
               distance_km=5.0, energy_kcal=400.0,
               statistics=[{"type": "HeartRate", "unit": "count/min", "start": start,
                            "end": start + dur, "avg": 151.0, "min": 101.0, "max": 181.0}],
               events=[{"type": 1, "start": start + 60, "duration_s": 0},
                       {"type": 2, "start": start + 90, "duration_s": 0}],
               activities=list(activities), metadata={"HKIndoorWorkout": 0})  # fmt: skip
    if plan:
        w["plan"] = plan
    return w


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        acts = [activity(0, T0, 600), activity(1, T0 + 600, 240), activity(2, T0 + 840, 120),
                activity(3, T0 + 960, 250), activity(4, T0 + 1210, 120),
                activity(5, T0 + 1330, 300)]  # fmt: skip
        keyed = [activity(i, T0 + 10_000 + 100 * i, 100, path)
                 for i, path in enumerate(["0.0.0", "1.0.0", "1.0.1", "1.1.0"])]  # fmt: skip
        write_batch(root, "20300304T100000Z", {
            "samples": [
                sample(1, "HeartRate", T0, T0 + 300, value=140.0, unit="count/min", count=3),
                sample(2, "RestingHeartRate", T0, value=52.0, unit="count/min", count=1),
                sample(3, "StepCount", T0, T0 + 60, value=80.0, count=1),
                sample(4, "SleepAnalysis", T0, T0 + 3600, value_int=3),
                sample(5, "RestingHeartRate", T0 + 86400, value=50.0, unit="count/min", count=1),
            ],
            # Three points, the middle one a run of identical readings over 10 s
            # at the series' 5 s cadence: five readings in all.
            "series_points": [
                {"uuid": uid(1), "t": T0, "dur": 0, "value": 140.0},
                {"uuid": uid(1), "t": T0 + 5, "dur": 10, "value": 141.0},
                {"uuid": uid(1), "t": T0 + 20, "dur": 0, "value": 150.0},
            ],
            "workouts": [workout(50, T0, 1630, acts, PLAN), workout(51, T0 + 10_000, 400, keyed, PLAN)],
            "routes": [sample(60, "WorkoutRoute", T0, T0 + 1630, workout=uid(50), count=2),
                       sample(61, "WorkoutRoute", T0, T0 + 1630, workout=uid(50), count=1)],
            "route_points": [
                {"route": uid(60), "t": T0, "lat": 48.1, "lon": 11.5, "ele": 520, "speed": 3.0,
                 "course": 90, "hacc": 3, "vacc": 2},
                {"route": uid(60), "t": T0 + 1, "lat": 48.1001, "lon": 11.5, "ele": 520,
                 "speed": 3.1, "course": 90, "hacc": 3, "vacc": 2},
                {"route": uid(61), "t": T0, "lat": 0, "lon": 0, "ele": 0, "speed": 0,
                 "course": 0, "hacc": 0, "vacc": 0},
            ],
            "activity_summary": [{"date": "2030-03-04", "active_energy": 100.0}],
        })  # fmt: skip
        # The second batch re-reads sample 2 with a new value, re-reads the series
        # (same points), deletes sample 3 and revises the day's rings.
        write_batch(root, "20300305T100000Z", {
            "samples": [
                sample(1, "HeartRate", T0, T0 + 300, value=140.0, unit="count/min", count=3),
                sample(2, "RestingHeartRate", T0, value=53.0, unit="count/min", count=1),
            ],
            "series_points": [
                {"uuid": uid(1), "t": T0, "dur": 0, "value": 140.0},
                {"uuid": uid(1), "t": T0 + 5, "dur": 10, "value": 141.0},
                {"uuid": uid(1), "t": T0 + 20, "dur": 0, "value": 150.0},
            ],
            "deleted": [{"uuid": uid(3), "type": "StepCount"}],
            "activity_summary": [{"date": "2030-03-04", "active_energy": 450.0}],
        })  # fmt: skip
        # Unfinished: no manifest, so never read.
        write_batch(root, "20300306T100000Z",
                    {"samples": [sample(7, "StepCount", T0, value=999.0, count=1)]},
                    manifest=False)  # fmt: skip
        self.db = os.path.join(root, "hk.duckdb")
        with contextlib.redirect_stderr(io.StringIO()):
            conv.build(conv.batch_dirs(root), self.db, "Europe/Berlin")
        self.con = duckdb.connect(self.db, read_only=True)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def q(self, sql, *params):
        return self.con.execute(sql, list(params)).fetchall()

    def test_id_matches_in_python_and_sql(self):
        sql_id = self.con.execute(f"SELECT {conv.hk_id_sql('?')}", [uid(1)]).fetchone()[0]
        self.assertEqual(sql_id, conv.hk_id(uid(1)))
        self.assertGreater(sql_id, 0)

    def test_newest_copy_wins_and_deletions_hold(self):
        self.assertEqual(self.q("SELECT value FROM records WHERE type = 'RestingHeartRate' "
                                "ORDER BY start_date"), [(53.0,), (50.0,)])  # fmt: skip
        self.assertEqual(self.q("SELECT count(*) FROM records WHERE type = 'StepCount'"), [(0,)])

    def test_series_comes_back_as_readings_once(self):
        rows = self.q("SELECT epoch(start_date) - ?, value FROM records WHERE type = 'HeartRate' "
                      "ORDER BY start_date", T0)  # fmt: skip
        self.assertEqual(rows, [(0, 140), (5, 141), (10, 141), (15, 141), (20, 150)])
        self.assertEqual(self.q("SELECT points, readings, value FROM quantity_series"),
                         [(3, 5, 140.0)])  # fmt: skip

    def test_category_and_type_columns(self):
        self.assertEqual(
            self.q("SELECT value, value_text, type_full, type_code FROM records "
                   "WHERE type = 'SleepAnalysis'"),
            [(None, "3", "HKCategoryTypeIdentifierSleepAnalysis", 63)],
        )  # fmt: skip

    def test_local_date_uses_profile_zone(self):
        # 08:00 UTC is 09:00 in Berlin in March.
        self.assertEqual(self.q("SELECT CAST(local_time AS VARCHAR) FROM records "
                                "WHERE type = 'SleepAnalysis'"), [("09:00:00",)])  # fmt: skip

    def test_workout_row(self):
        (row,) = self.q("SELECT activity, duration_min, avg_hr, max_hr, indoor, route_file "
                        "FROM workouts ORDER BY start_date LIMIT 1")  # fmt: skip
        self.assertEqual(row[0], "Running")
        self.assertAlmostEqual(row[1], 1630 / 60)
        self.assertEqual(row[2:5], (151.0, 181.0, False))
        self.assertEqual(row[5], "hk_" + uid(60), "the richer route wins")
        self.assertEqual(self.q("SELECT count(*) FROM route_points WHERE route_file = ?", row[5]),
                         [(2,)])  # fmt: skip

    def test_blocks_by_position_when_counts_match(self):
        rows = self.q("SELECT seq, kind, step_path, target_min FROM workout_blocks "
                      "WHERE workout_id = ? AND NOT is_primary ORDER BY seq",
                      conv.hk_id(uid(50)))  # fmt: skip
        self.assertEqual([r[1] for r in rows],
                         ["warmup", "work", "recovery", "work", "recovery", "cooldown"])  # fmt: skip
        self.assertEqual(rows[3][2], "1.1.0")
        self.assertEqual(rows[1][3], 2.7)
        self.assertEqual(self.q("SELECT count(*) FROM workout_blocks WHERE workout_id = ? "
                                "AND is_primary", conv.hk_id(uid(50))), [(1,)])  # fmt: skip

    def test_blocks_by_key_path_when_present(self):
        # Four activities against a six-step plan: position cannot work, the
        # key paths can.
        rows = self.q("SELECT kind FROM workout_blocks WHERE workout_id = ? AND NOT is_primary "
                      "ORDER BY seq", conv.hk_id(uid(51)))  # fmt: skip
        self.assertEqual([r[0] for r in rows], ["warmup", "work", "recovery", "work"])

    def test_no_labels_when_plan_and_activities_disagree(self):
        w = workout(70, T0, 100, [activity(i, T0 + i, 1) for i in range(3)], PLAN)
        labels = conv.label_activities(w, w["activities"])
        self.assertEqual(labels, [(None, None)] * 3)

    def test_events_and_statistics(self):
        self.assertEqual(self.q("SELECT type FROM workout_events WHERE workout_id = ? ORDER BY date",
                                conv.hk_id(uid(50))), [("Pause",), ("Resume",)])  # fmt: skip
        # Workout-level row plus one per activity, as the backup route has, and
        # only the workout-level one is the session's total.
        self.assertEqual(self.q("SELECT is_primary, count(*) FROM workout_statistics "
                                "WHERE workout_id = ? GROUP BY 1 ORDER BY 1",
                                conv.hk_id(uid(50))), [(False, 6), (True, 1)])  # fmt: skip

    def test_views_and_activity_summary(self):
        self.assertEqual(self.q("SELECT count(*) FROM hr"), [(5,)])
        self.assertEqual(self.q("SELECT resting_hr FROM daily_metrics ORDER BY local_date"),
                         [(53.0,), (50.0,)])  # fmt: skip
        self.assertEqual(self.q("SELECT active_energy FROM activity_summary"), [(450.0,)])

    def test_unfinished_batch_is_ignored(self):
        self.assertEqual(len(conv.batch_dirs(self.tmp.name)), 2)


if __name__ == "__main__":
    unittest.main()
