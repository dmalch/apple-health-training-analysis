#!/usr/bin/env python3
"""Tests for the per-sample zone split in analyze.py.

    ./.venv/bin/python -m unittest test_analyze -v

The case that motivated these: two runs of analyze.py over one unchanged
database disagreed on which zone a session belonged to. The backup route had
carried some watch heart-rate samples with a timestamp but no value, a few of
them at the same instant as a real reading, and the per-sample gap was taken
from a window ordered by timestamp alone. Whichever of the two tied rows came
last took the seconds until the next sample, so a session's time moved between
a real reading and nothing from one run to the next. A session made mostly of
valueless samples also passed the coverage check with almost no time in any
zone, and was then filed under Z1 because every zone tied at zero.

Needs duckdb (see .venv); no real health data.
"""

import datetime
import unittest

import duckdb

import analyze
import athlete_profile

BASE = datetime.datetime(2026, 1, 5, 9, 0, tzinfo=datetime.UTC)

# A round anchor keeps the zone edges readable: Z3 is 140-160, Z4 160-180.
HR_MAX = 200.0


def at(offset):
    return BASE + datetime.timedelta(seconds=offset)


def zone_db(samples, duration_s=600):
    """One workout and its heart-rate stream, as (offset_s, bpm, sensor) rows.

    Rows go in in the order given, so a test can present the same samples in
    two orders and check the answer does not depend on it. A bpm of None is a
    sample the backup carried without a value.
    """
    con = duckdb.connect()
    con.execute("CREATE TABLE workouts (id BIGINT, start_date TIMESTAMPTZ, end_date TIMESTAMPTZ)")
    con.execute("INSERT INTO workouts VALUES (1, ?, ?)", [at(0), at(duration_s)])
    con.execute("CREATE TABLE hr (t TIMESTAMPTZ, bpm DOUBLE, sensor VARCHAR)")
    con.executemany(
        "INSERT INTO hr VALUES (?, ?, ?)", [(at(s), bpm, sensor) for s, bpm, sensor in samples]
    )
    return con


def zones(samples, duration_s=600):
    """What attach_zones makes of one session: (real, sparse) and its zone minutes."""
    con = zone_db(samples, duration_s)
    w = {"_id": 1, "date": BASE.date().isoformat(), "duration_min": duration_s / 60}
    counts = analyze.attach_zones(con, [w], HR_MAX)
    con.close()
    return counts, w.get("_zones")


def pairs(first, second, sensor="watch", every=5, duration_s=600):
    """Two samples at every instant, `first` inserted before `second`."""
    rows = []
    for s in range(0, duration_s, every):
        rows += [(s, first, sensor), (s, second, sensor)]
    return rows


class ZoneTieTest(unittest.TestCase):
    def setUp(self):
        analyze.ZONES = list(athlete_profile.DEFAULT_ZONES)

    def test_a_sample_without_a_value_does_not_take_the_time_of_the_reading_beside_it(self):
        # Ten minutes at 150 bpm, every reading shadowed by a valueless sample
        # at the same instant. However the tie is presented, all ten minutes
        # are Z3 -- not zero minutes in every zone, which zone_split then
        # files under Z1.
        for label, samples in (
            ("value first", pairs(150.0, None)),
            ("value last", pairs(None, 150.0)),
        ):
            with self.subTest(label):
                counts, z = zones(samples)
                self.assertEqual(counts, (1, 0))
                self.assertIsNotNone(z)
                assert z is not None
                self.assertAlmostEqual(z["Z3 tempo"], 10.0)
                self.assertAlmostEqual(sum(z.values()), 10.0)

    def test_valueless_samples_do_not_count_as_coverage(self):
        # Real readings for the first three minutes, then samples with no
        # value to the end. Three minutes of ten is under MIN_ZONE_COVERAGE,
        # so the session keeps the session-average treatment instead of
        # having three minutes scaled up to describe all ten.
        samples = [(s, 150.0, "watch") for s in range(0, 180, 5)]
        samples += [(s, None, "watch") for s in range(180, 600, 5)]
        counts, z = zones(samples)
        self.assertEqual(counts, (0, 1))
        self.assertIsNone(z)

    def test_two_readings_at_one_instant_split_the_same_way_in_either_order(self):
        first, second = zones(pairs(145.0, 165.0))[1], zones(pairs(165.0, 145.0))[1]
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first, second)
        self.assertAlmostEqual(sum(first.values()), 10.0)

    def test_the_strap_anchor_does_not_depend_on_the_order_of_tied_readings(self):
        # Two strap readings a second for a minute, 172 and 185 at each
        # instant. Which one holds for ten seconds must not depend on which
        # row the database happens to put last.
        anchors = []
        for samples in (
            pairs(172.0, 185.0, "strap", every=1, duration_s=60),
            pairs(185.0, 172.0, "strap", every=1, duration_s=60),
        ):
            con = zone_db(samples, duration_s=60)
            anchors.append(analyze.strap_anchor(con))
            con.close()
        self.assertIn(anchors[0], (172.0, 185.0))
        self.assertEqual(anchors[0], anchors[1])


if __name__ == "__main__":
    unittest.main()
