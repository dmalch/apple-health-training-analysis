#!/usr/bin/env python3
"""Tests for the XML-export route in health_to_duckdb.py.

    python3 -m unittest test_xml_route -v

The case that motivated these: a workout Apple splits into several activities
writes one <WorkoutStatistics> per activity. Keeping whichever came last
reported the final segment as if it were the whole session -- a distance
covering one leg, and an average heart rate biased by roughly 20 bpm on an
interval session, because the hard bouts are short and the recoveries are not.
The backup route has always summed and duration-weighted these; this route now
does the same, so the two agree.

Synthetic XML only; no real health data.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

import duckdb

import health_to_duckdb as h

FMT = "%Y-%m-%d %H:%M:%S %z"


def stat(stype, start, end, **attrs):
    """One <WorkoutStatistics> element."""
    el = ET.Element("WorkoutStatistics", type=stype, startDate=start, endDate=end)
    for k, v in attrs.items():
        el.set(k, str(v))
    return el


def almost(case, value, expected, places=7):
    """assertAlmostEqual, but it says so when the value is missing entirely."""
    case.assertIsNotNone(value, f"expected about {expected}, got nothing")
    assert value is not None
    case.assertAlmostEqual(value, expected, places=places)


class StatisticsAggregationTest(unittest.TestCase):
    """The helpers, exercised directly."""

    def rows(self, *elements):
        return [(el, el.get("unit")) for el in elements]

    def test_a_single_row_is_its_own_average(self):
        rows = self.rows(
            stat(
                "HeartRate",
                "2026-09-03 09:00:00 +0200",
                "2026-09-03 09:10:00 +0200",
                average=150,
                maximum=170,
                minimum=120,
            )
        )
        almost(self, h.stat_weighted_average(rows), 150.0)

    def test_the_average_is_weighted_by_how_long_each_activity_ran(self):
        # 4 min at 170 and 12 min at 120. A plain mean says 145; the last-wins
        # bug said 120; the truth, weighted, is 132.5.
        rows = self.rows(
            stat(
                "HeartRate",
                "2026-09-03 09:00:00 +0200",
                "2026-09-03 09:04:00 +0200",
                average=170,
                maximum=178,
                minimum=150,
            ),
            stat(
                "HeartRate",
                "2026-09-03 09:04:00 +0200",
                "2026-09-03 09:16:00 +0200",
                average=120,
                maximum=140,
                minimum=95,
            ),
        )
        almost(self, h.stat_weighted_average(rows), 132.5)

    def test_the_maximum_is_the_session_maximum_not_the_last_segments(self):
        rows = self.rows(
            stat(
                "HeartRate",
                "2026-09-03 09:00:00 +0200",
                "2026-09-03 09:04:00 +0200",
                average=170,
                maximum=178,
                minimum=150,
            ),
            stat(
                "HeartRate",
                "2026-09-03 09:04:00 +0200",
                "2026-09-03 09:16:00 +0200",
                average=120,
                maximum=140,
                minimum=95,
            ),
        )
        self.assertEqual(h.stat_extreme(rows, "maximum", max), 178.0)
        self.assertEqual(h.stat_extreme(rows, "minimum", min), 95.0)

    def test_distance_is_the_total_across_legs(self):
        rows = [
            (
                stat(
                    "D",
                    "2026-09-03 09:00:00 +0200",
                    "2026-09-03 09:04:00 +0200",
                    sum=1.5,
                    unit="km",
                ),
                "km",
            ),
            (
                stat(
                    "D",
                    "2026-09-03 09:04:00 +0200",
                    "2026-09-03 09:16:00 +0200",
                    sum=4.0,
                    unit="km",
                ),
                "km",
            ),
        ]
        almost(self, h.stat_total(rows, h.to_km), 5.5)

    def test_rows_without_the_value_do_not_drag_the_answer_to_zero(self):
        rows = self.rows(
            stat(
                "HeartRate", "2026-09-03 09:00:00 +0200", "2026-09-03 09:10:00 +0200", average=150
            ),
            stat(
                "HeartRate", "2026-09-03 09:10:00 +0200", "2026-09-03 09:20:00 +0200", maximum=160
            ),
        )
        almost(self, h.stat_weighted_average(rows), 150.0)

    def test_nothing_to_aggregate_is_none_rather_than_zero(self):
        self.assertIsNone(h.stat_weighted_average([]))
        self.assertIsNone(h.stat_extreme([], "maximum", max))
        self.assertIsNone(h.stat_total([], h.to_km))

    def test_an_unparseable_date_degrades_to_an_unweighted_mean(self):
        rows = self.rows(
            stat("HeartRate", "not a date", "nor this", average=100),
            stat("HeartRate", "not a date", "nor this", average=200),
        )
        almost(self, h.stat_weighted_average(rows), 150.0)


EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_GB">
 <ExportDate value="2026-09-17 10:00:00 +0200"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning"
          duration="16" durationUnit="min"
          sourceName="Watch" device="&lt;&lt;HKDevice: 0x1&gt;, name:Apple Watch&gt;"
          creationDate="2026-09-03 09:20:00 +0200"
          startDate="2026-09-03 09:00:00 +0200"
          endDate="2026-09-03 09:16:00 +0200">
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"
     startDate="2026-09-03 09:00:00 +0200" endDate="2026-09-03 09:04:00 +0200"
     average="170" minimum="150" maximum="178" unit="count/min"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"
     startDate="2026-09-03 09:04:00 +0200" endDate="2026-09-03 09:16:00 +0200"
     average="120" minimum="95" maximum="140" unit="count/min"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning"
     startDate="2026-09-03 09:00:00 +0200" endDate="2026-09-03 09:04:00 +0200"
     sum="1.5" unit="km"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning"
     startDate="2026-09-03 09:04:00 +0200" endDate="2026-09-03 09:16:00 +0200"
     sum="4.0" unit="km"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned"
     startDate="2026-09-03 09:00:00 +0200" endDate="2026-09-03 09:04:00 +0200"
     sum="60" unit="kcal"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned"
     startDate="2026-09-03 09:04:00 +0200" endDate="2026-09-03 09:16:00 +0200"
     sum="120" unit="kcal"/>
 </Workout>
</HealthData>
"""


class MultiActivityWorkoutTest(unittest.TestCase):
    """End to end: a split workout must come out as one whole session."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="xml-route-"))
        export = cls.tmp / "export.xml"
        export.write_text(EXPORT, encoding="utf-8")
        stage = cls.tmp / "_staging"
        stage.mkdir()
        h.stream_xml(str(export), str(stage))
        cls.db = cls.tmp / "out.duckdb"
        cls.con = duckdb.connect(str(cls.db))
        h.load(cls.con, str(stage), with_routes=False)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def row(self):
        r = self.con.execute(
            "SELECT avg_hr, max_hr, min_hr, distance_km, active_kcal FROM workouts"
        ).fetchone()
        self.assertIsNotNone(r, "the workout did not load at all")
        assert r is not None
        return r

    def test_the_average_heart_rate_is_the_whole_session(self):
        avg_hr = self.row()[0]
        # 132.5, not 120 (the last segment) and not 170 (the first).
        almost(self, avg_hr, 132.5, places=2)

    def test_the_peak_survives_even_though_it_is_in_the_first_segment(self):
        _, max_hr, min_hr, _, _ = self.row()
        almost(self, max_hr, 178.0)
        almost(self, min_hr, 95.0)

    def test_distance_and_energy_are_totals_not_the_last_leg(self):
        _, _, _, distance_km, active_kcal = self.row()
        almost(self, distance_km, 5.5, places=3)
        almost(self, active_kcal, 180.0, places=3)

    def test_every_statistics_row_is_still_kept_individually(self):
        n = self.con.execute("SELECT count(*) FROM workout_statistics").fetchone()
        assert n is not None
        self.assertEqual(n[0], 6, "the per-activity rows must survive for auditing")


if __name__ == "__main__":
    unittest.main()
