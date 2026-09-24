#!/usr/bin/env python3
"""Tests for shoe mileage.

    python3 -m unittest test_shoes -v

Apple Health records no gear, so a pair's mileage is every workout of its
activities inside its date range. Each test pins one edge of that range or one
thing that must not be counted. Synthetic workouts only.
"""

import unittest
from datetime import date

import duckdb

import shoes


def db(rows):
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE workouts (activity VARCHAR, local_date DATE, distance_km DOUBLE, "
        "duration_min DOUBLE, source_name VARCHAR)"
    )
    con.executemany("INSERT INTO workouts VALUES (?, ?, ?, ?, ?)", rows)
    return con


def shoe(**overrides):
    base = {
        "name": "Trainer",
        "since": date(2025, 3, 1),
        "retired": None,
        "limit_km": None,
        "start_km": 0.0,
        "activities": ["Running"],
    }
    base.update(overrides)
    return base


class MileageTest(unittest.TestCase):
    def test_only_runs_inside_the_date_range_count_both_ends_included(self):
        con = db(
            [
                ("Running", date(2025, 2, 28), 10.0, 60.0, "Watch"),  # before the gift
                ("Running", date(2025, 3, 1), 5.0, 30.0, "Watch"),  # first day
                ("Running", date(2025, 4, 1), 7.0, 42.0, "Watch"),  # retirement day
                ("Running", date(2025, 5, 1), 9.0, 50.0, "Watch"),  # after
            ]
        )
        (row,) = shoes.mileage(con, [shoe(retired=date(2025, 4, 1))])
        self.assertEqual(row["workouts"], 2)
        self.assertAlmostEqual(row["km"], 12.0)
        self.assertAlmostEqual(row["hours"], 1.2)
        self.assertEqual(row["last"], date(2025, 4, 1))

    def test_other_activities_do_not_wear_the_shoe_unless_listed(self):
        con = db(
            [
                ("Running", date(2025, 3, 2), 5.0, 30.0, "Watch"),
                ("Hiking", date(2025, 3, 3), 12.0, 240.0, "Watch"),
            ]
        )
        (running_only,) = shoes.mileage(con, [shoe()])
        self.assertAlmostEqual(running_only["km"], 5.0)
        (with_hiking,) = shoes.mileage(con, [shoe(activities=["Running", "Hiking"])])
        self.assertAlmostEqual(with_hiking["km"], 17.0)

    def test_km_already_on_a_used_pair_is_added(self):
        con = db([("Running", date(2025, 3, 2), 5.0, 30.0, "Watch")])
        (row,) = shoes.mileage(con, [shoe(start_km=50.0)])
        self.assertAlmostEqual(row["km"], 55.0)

    def test_a_duplicate_source_is_not_counted_twice(self):
        # A second app logging the same run would otherwise double the wear.
        con = db(
            [
                ("Running", date(2025, 3, 2), 5.0, 30.0, "Watch"),
                ("Running", date(2025, 3, 2), 5.0, 30.0, "GymApp"),
            ]
        )
        (row,) = shoes.mileage(con, [shoe()], duplicates=["GymApp"])
        self.assertEqual(row["workouts"], 1)
        self.assertAlmostEqual(row["km"], 5.0)

    def test_a_run_without_distance_counts_as_a_run_but_adds_no_km(self):
        con = db(
            [
                ("Running", date(2025, 3, 2), 5.0, 30.0, "Watch"),
                ("Running", date(2025, 3, 3), None, 20.0, "Watch"),
            ]
        )
        (row,) = shoes.mileage(con, [shoe()])
        self.assertEqual(row["workouts"], 2)
        self.assertAlmostEqual(row["km"], 5.0)

    def test_the_share_of_the_limit_is_reported(self):
        con = db([("Running", date(2025, 3, 2), 150.0, 900.0, "Watch")])
        (row,) = shoes.mileage(con, [shoe(limit_km=600.0)])
        self.assertAlmostEqual(row["of_limit"], 0.25)
        (no_limit,) = shoes.mileage(con, [shoe()])
        self.assertIsNone(no_limit["of_limit"])

    def test_a_pair_with_no_runs_yet_reports_zero(self):
        con = db([("Running", date(2025, 2, 1), 5.0, 30.0, "Watch")])
        (row,) = shoes.mileage(con, [shoe(start_km=3.0)])
        self.assertEqual(row["workouts"], 0)
        self.assertAlmostEqual(row["km"], 3.0)
        self.assertAlmostEqual(row["hours"], 0.0)
        self.assertIsNone(row["last"])


class OverlapTest(unittest.TestCase):
    """Two pairs in use on the same days would both be charged every run."""

    def test_two_open_pairs_on_one_activity_overlap(self):
        pairs = [shoe(name="A"), shoe(name="B", since=date(2025, 6, 1))]
        self.assertEqual(shoes.overlaps(pairs), [("A", "B")])

    def test_back_to_back_pairs_do_not_overlap(self):
        pairs = [
            shoe(name="A", retired=date(2025, 5, 31)),
            shoe(name="B", since=date(2025, 6, 1)),
        ]
        self.assertEqual(shoes.overlaps(pairs), [])

    def test_a_handover_day_is_an_overlap(self):
        pairs = [
            shoe(name="A", retired=date(2025, 6, 1)),
            shoe(name="B", since=date(2025, 6, 1)),
        ]
        self.assertEqual(shoes.overlaps(pairs), [("A", "B")])

    def test_pairs_for_different_activities_do_not_overlap(self):
        pairs = [shoe(name="Road"), shoe(name="Boots", activities=["Hiking"])]
        self.assertEqual(shoes.overlaps(pairs), [])


if __name__ == "__main__":
    unittest.main()
