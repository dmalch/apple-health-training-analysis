#!/usr/bin/env python3
"""Fixture tests for healthdb_to_duckdb.py.

    ./.venv/bin/python -m unittest test_healthdb -v

Every assertion here is a bug that actually happened while the converter was
being written against a real iOS 26.6 database, and every one of them returned
plausible numbers rather than an error -- heart rate in count/second reads as
2.7, a strap session attributed to the watch reads as a normal watch session, a
metadata quantity reads as 'cm'. Nothing crashed; the numbers were just wrong.

The fixture is a miniature healthdb_secure.sqlite plus its healthdb.sqlite
companion, built with the layout iOS 18+ actually uses: ROWID declared as a real
column, workout activity and duration in `workout_activities`, totals in
`workout_statistics`, metadata values tagged by `value_type`.

Needs duckdb (see .venv); no other dependencies, no real health data.
"""

import contextlib
import datetime
import io
import os
import shutil
import sqlite3
import tempfile
import unittest

import duckdb

import healthdb_to_duckdb as conv

APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.UTC)


def apple_time(y, mo, d, h, mi, s=0):
    """Seconds since the HealthKit epoch, from a UTC wall clock."""
    when = datetime.datetime(y, mo, d, h, mi, s, tzinfo=datetime.UTC)
    return (when - APPLE_EPOCH).total_seconds()


SECURE_SCHEMA = """
CREATE TABLE objects (data_id INTEGER PRIMARY KEY, uuid BLOB, provenance INTEGER,
                      type INTEGER, creation_date REAL);
CREATE TABLE samples (data_id INTEGER PRIMARY KEY, start_date REAL, end_date REAL,
                      data_type INTEGER);
CREATE TABLE quantity_samples (data_id INTEGER PRIMARY KEY, quantity REAL,
                               original_quantity REAL, original_unit TEXT);
CREATE TABLE category_samples (data_id INTEGER PRIMARY KEY, value INTEGER);
CREATE TABLE binary_samples (data_id INTEGER PRIMARY KEY, payload BLOB);
-- ROWID is a declared column here, exactly as on the device. Aliasing another
-- one in on the way out collided and aborted the whole run.
CREATE TABLE data_provenances (ROWID INTEGER PRIMARY KEY, sync_provenance INTEGER,
                               origin_product_type TEXT, origin_build TEXT,
                               local_product_type TEXT, local_build TEXT,
                               source_id INTEGER, device_id INTEGER, tz_name TEXT);
CREATE TABLE workouts (data_id INTEGER PRIMARY KEY, total_distance REAL,
                       goal_type INTEGER, goal REAL, condenser_version INTEGER,
                       condenser_date REAL);
CREATE TABLE workout_activities (ROWID INTEGER PRIMARY KEY, uuid BLOB, owner_id INTEGER,
                                 is_primary_activity INTEGER, activity_type INTEGER,
                                 location_type INTEGER, swimming_location_type INTEGER,
                                 lap_length REAL, start_date REAL, end_date REAL,
                                 duration REAL, metadata BLOB);
CREATE TABLE workout_statistics (ROWID INTEGER PRIMARY KEY, workout_activity_id INTEGER,
                                 data_type INTEGER, quantity REAL, min REAL, max REAL);
CREATE TABLE workout_events (ROWID INTEGER PRIMARY KEY, owner_id INTEGER, date REAL,
                             type INTEGER, duration REAL, metadata BLOB,
                             session_uuid BLOB, error BLOB);
-- A workout route is a type-102 sample whose points live in a separate table,
-- reached by `hfd_key` -- NOT by data_id, which is a different number.
CREATE TABLE data_series (data_id INTEGER PRIMARY KEY, frozen INTEGER, count INTEGER,
                          insertion_era INTEGER, hfd_key INTEGER UNIQUE,
                          series_location INTEGER);
CREATE TABLE location_series_data (series_identifier INTEGER, timestamp REAL,
                                   latitude REAL, longitude REAL, altitude REAL,
                                   speed REAL, course REAL, horizontal_accuracy REAL,
                                   vertical_accuracy REAL, speed_accuracy REAL,
                                   course_accuracy REAL, signal_environment INTEGER);
-- The route is tied to its workout here: destination is the workout, source the route.
CREATE TABLE associations (ROWID INTEGER PRIMARY KEY, destination_object_id INTEGER,
                           source_object_id INTEGER, sync_provenance INTEGER,
                           sync_identity INTEGER, type INTEGER, deleted INTEGER,
                           creation_date REAL, destination_sub_object_id INTEGER,
                           behavior INTEGER);
CREATE TABLE metadata_keys (ROWID INTEGER PRIMARY KEY, key TEXT);
CREATE TABLE metadata_values (ROWID INTEGER PRIMARY KEY, key_id INTEGER, object_id INTEGER,
                              value_type INTEGER, string_value TEXT, numerical_value REAL,
                              date_value REAL, data_value BLOB);
"""

COMPANION_SCHEMA = """
CREATE TABLE sources (ROWID INTEGER PRIMARY KEY, uuid BLOB, name TEXT,
                      source_options INTEGER, local_device INTEGER, product_type TEXT);
CREATE TABLE source_devices (ROWID INTEGER PRIMARY KEY, name TEXT, bluetooth_identifier TEXT,
                             manufacturer TEXT, model TEXT, hardware TEXT, firmware TEXT,
                             software TEXT, localIdentifier TEXT);
"""

# Provenance rows: the middle one is the trap. A strap session is *owned by the
# watch*, so origin_product_type says 'Watch7,5' and only device_id tells the
# truth.
PROVENANCES = [
    (1, 0, "Watch7,5", "22R123", "iPhone15,2", "23A1", 1, 1, "Europe/Rome"),
    (2, 0, "Watch7,5", "22R123", "iPhone15,2", "23A1", 2, 2, "Europe/Rome"),
    (3, 0, "iPhone15,2", "23A1", "iPhone15,2", "23A1", 3, 3, "Europe/Rome"),
]
SOURCES = [
    (1, None, "Example Apple Watch", 0, 1, "Watch7,5"),
    (2, None, "Bluetooth Device", 0, 0, None),
    (3, None, "Example AirPods Pro", 0, 0, None),
]
DEVICES = [
    (1, "Apple Watch", None, "Apple Inc.", "Watch", "Watch7,5", "26.6", None, ""),
    (
        2,
        "Polar H10 0000ABCD",
        None,
        "Polar Electro Oy",
        "H10",
        "00760690.05",
        "5.0.0",
        None,
        "F4757C98",
    ),
    (3, "AirPods Pro", None, "Apple Inc.", "0x2027", "", None, None, "74:77:86"),
]

WORKOUT_START = apple_time(2026, 8, 26, 8, 0)


def build_fixture(root):
    """Write the two sqlite files and return the path of the secure one."""
    secure = os.path.join(root, "healthdb_secure.sqlite")
    con = sqlite3.connect(secure)
    con.executescript(SECURE_SCHEMA)
    con.executemany("INSERT INTO data_provenances VALUES (?,?,?,?,?,?,?,?,?)", PROVENANCES)

    next_id = [0]

    def sample(data_type, start, end, provenance, quantity=None, unit=None, category=None):
        next_id[0] += 1
        did = next_id[0]
        con.execute("INSERT INTO objects VALUES (?,?,?,?,?)", (did, None, provenance, 0, start))
        con.execute("INSERT INTO samples VALUES (?,?,?,?)", (did, start, end, data_type))
        if quantity is not None:
            con.execute(
                "INSERT INTO quantity_samples VALUES (?,?,?,?)", (did, quantity, quantity, unit)
            )
        if category is not None:
            con.execute("INSERT INTO category_samples VALUES (?,?)", (did, category))
        return did

    t = apple_time(2026, 8, 26, 12, 0)
    # Heart rate is stored in count/SECOND: 2.7 is 162 bpm, not 2.7 bpm.
    sample(5, t, t, 2, quantity=2.7)  # strap
    sample(5, t + 1, t + 1, 1, quantity=2.0)  # watch  -> 120
    sample(5, t + 2, t + 2, 3, quantity=2.5)  # airpods -> 150
    # Resting heart rate is NOT per-second; scaling it too would read 2880 bpm.
    sample(118, t, t, 1, quantity=48.0)
    sample(8, t, t, 1, quantity=5000.0)  # metres  -> 5 km
    sample(187, t, t, 1, quantity=1.5)  # m/s     -> 5.4 km/h
    sample(188, t, t, 1, quantity=0.7)  # metres  -> 70 cm
    sample(110, t, t, 1, quantity=1.2)  # swimming is already km
    sample(63, t, t + 300, 1, category=3)  # SleepAnalysis
    sample(999, t, t, 1, quantity=36.4)  # unknown code, must survive

    # 22:30 UTC in Europe/Rome is 00:30 the next day: the local date has to come
    # from the row's own timezone, not from the machine's.
    late = apple_time(2026, 8, 26, 22, 30)
    sample(5, late, late, 1, quantity=2.0)

    # A workout with two activity segments, one long and easy, one short and
    # hard. The workout-level average has to be weighted by segment length.
    wid = sample(79, WORKOUT_START, WORKOUT_START + 4800, 2)
    con.execute("INSERT INTO workouts VALUES (?,?,?,?,?,?)", (wid, 12.5, 0, None, 6, None))
    con.executemany(
        "INSERT INTO workout_activities VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, None, wid, 1, 63, 1, 0, None, WORKOUT_START, WORKOUT_START + 3600, 3600.0, None),
            (
                2,
                None,
                wid,
                1,
                63,
                1,
                0,
                None,
                WORKOUT_START + 3600,
                WORKOUT_START + 4800,
                1200.0,
                None,
            ),
        ],
    )
    con.executemany(
        "INSERT INTO workout_statistics VALUES (?,?,?,?,?,?)",
        [
            (1, 1, 5, 2.0, 1.5, 2.5),  # 120 bpm avg over 60 min
            (2, 2, 5, 3.0, 2.0, 3.2),  # 180 bpm avg over 20 min
            (3, 1, 10, 300.0, None, None),  # ActiveEnergyBurned
            (4, 2, 10, 100.0, None, None),
        ],
    )
    con.execute(
        "INSERT INTO workout_events VALUES (?,?,?,?,?,?,?,?)",
        (1, wid, WORKOUT_START + 600, 3, 60.0, None, None, None),
    )

    # The blocks of a structured workout: warm-up, work, recovery, and a second
    # work bout stopped 26 s early. They are is_primary_activity = 0, so the
    # workout's own duration must keep ignoring them.
    con.executemany(
        "INSERT INTO workout_activities VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (10, None, wid, 0, 63, 1, 0, None, WORKOUT_START, WORKOUT_START + 600, 600.0, None),
            (
                11,
                None,
                wid,
                0,
                63,
                1,
                0,
                None,
                WORKOUT_START + 600,
                WORKOUT_START + 840,
                240.0,
                None,
            ),
            (
                12,
                None,
                wid,
                0,
                63,
                1,
                0,
                None,
                WORKOUT_START + 840,
                WORKOUT_START + 1020,
                180.0,
                None,
            ),
            (
                13,
                None,
                wid,
                0,
                63,
                1,
                0,
                None,
                WORKOUT_START + 1020,
                WORKOUT_START + 1234,
                214.0,
                None,
            ),
        ],
    )

    # The GPS track. `hfd_key` is 720 while the sample's own data_id is something
    # else entirely; DECOY_POINTS sit under series_identifier = that data_id, so
    # a join on data_id returns them and lands the run in the wrong hemisphere.
    route_id = sample(102, WORKOUT_START, WORKOUT_START + 1234, 2)
    con.execute("INSERT INTO data_series VALUES (?,?,?,?,?,?)", (route_id, 1, 3, 0, 720, 2))
    con.executemany(
        "INSERT INTO location_series_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (720, WORKOUT_START, 48.1371, 11.5754, 519.7, 3.10, 90.0, 1.2, 2.0, 0.5, 5.0, 1),
            (720, WORKOUT_START + 617, 48.1380, 11.5766, 521.4, 3.85, 92.0, 1.1, 2.0, 0.5, 5.0, 1),
            (720, WORKOUT_START + 1234, 48.1392, 11.5779, 528.7, 2.05, 95.0, 1.3, 2.0, 0.5, 5.0, 1),
        ],
    )
    con.executemany(
        "INSERT INTO location_series_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (route_id, WORKOUT_START, -33.8688, 151.2093, 5.0, 9.9, 0.0, 9.0, 9.0, 9.0, 9.0, 0),
        ],
    )
    con.execute(
        "INSERT INTO associations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, wid, route_id, 0, 1, 0, 0, WORKOUT_START, None, 0),
    )

    # The same run synced twice: two live routes on one workout, the second
    # thinner than the first. Joined naively this yields two workout rows.
    twin_id = sample(102, WORKOUT_START, WORKOUT_START + 1234, 2)
    con.execute("INSERT INTO data_series VALUES (?,?,?,?,?,?)", (twin_id, 1, 2, 0, 888, 2))
    con.executemany(
        "INSERT INTO location_series_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (888, WORKOUT_START, 48.1371, 11.5754, 519.7, 3.10, 90.0, 1.2, 2.0, 0.5, 5.0, 1),
            (888, WORKOUT_START + 617, 48.1380, 11.5766, 521.4, 3.85, 92.0, 1.1, 2.0, 0.5, 5.0, 1),
        ],
    )
    con.execute(
        "INSERT INTO associations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (3, wid, twin_id, 0, 1, 0, 0, WORKOUT_START, None, 0),
    )

    # A route that was recorded and then deleted. Its association row is still
    # there with deleted = 1; joining it in gives the workout a second route and
    # duplicates the whole workout row.
    stale_id = sample(102, WORKOUT_START, WORKOUT_START + 60, 2)
    con.execute("INSERT INTO data_series VALUES (?,?,?,?,?,?)", (stale_id, 1, 0, 0, 999, 2))
    con.execute(
        "INSERT INTO associations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (2, wid, stale_id, 0, 1, 0, 1, WORKOUT_START, None, 0),
    )

    con.executemany(
        "INSERT INTO metadata_keys VALUES (?,?)",
        [(1, "HKElevationAscended"), (2, "HKIndoorWorkout"), (3, "HKTimeZone")],
    )
    con.executemany(
        "INSERT INTO metadata_values VALUES (?,?,?,?,?,?,?,?)",
        [
            # value_type 3 is a quantity: magnitude in numerical_value, UNIT in
            # string_value. Taking the first non-null column yields 'cm'.
            (1, 1, wid, 3, "cm", 122200.0, None, None),
            (2, 2, wid, 1, None, 0.0, None, None),
            (3, 3, wid, 0, "Europe/Rome", None, None, None),
        ],
    )
    con.commit()
    con.close()

    companion = sqlite3.connect(os.path.join(root, "healthdb.sqlite"))
    companion.executescript(COMPANION_SCHEMA)
    companion.executemany("INSERT INTO sources VALUES (?,?,?,?,?,?)", SOURCES)
    companion.executemany("INSERT INTO source_devices VALUES (?,?,?,?,?,?,?,?,?)", DEVICES)
    companion.commit()
    companion.close()
    return secure


class ConverterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="healthdb-test-")
        secure = build_fixture(cls.tmp)
        cls.db_path = os.path.join(cls.tmp, "out.duckdb")
        with contextlib.redirect_stderr(io.StringIO()):
            conv.build(secure, cls.db_path, "Europe/Berlin")
        cls.con = duckdb.connect(cls.db_path, read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def value_of(self, hk_type):
        return self.con.execute(
            "SELECT value FROM records WHERE type = ? ORDER BY value DESC LIMIT 1", [hk_type]
        ).fetchone()[0]

    # ------------------------------------------------------------------ units

    def test_heart_rate_is_converted_to_bpm(self):
        self.assertAlmostEqual(self.value_of("HeartRate"), 162.0, places=6)

    def test_resting_heart_rate_is_left_alone(self):
        self.assertAlmostEqual(self.value_of("RestingHeartRate"), 48.0, places=6)

    def test_distances_and_speeds_use_display_units(self):
        self.assertAlmostEqual(self.value_of("DistanceWalkingRunning"), 5.0, places=6)
        self.assertAlmostEqual(self.value_of("WalkingSpeed"), 5.4, places=6)
        self.assertAlmostEqual(self.value_of("WalkingStepLength"), 70.0, places=6)

    def test_swimming_distance_is_already_kilometres(self):
        self.assertAlmostEqual(self.value_of("DistanceSwimming"), 1.2, places=6)

    def test_unit_column_is_filled_for_scaled_types(self):
        unit = self.con.execute(
            "SELECT DISTINCT unit FROM records WHERE type = 'HeartRate'"
        ).fetchone()[0]
        self.assertEqual(unit, "count/min")

    # ---------------------------------------------------------------- sensors

    def test_strap_is_not_attributed_to_the_watch(self):
        row = self.con.execute("SELECT sensor, device_name FROM hr WHERE bpm = 162").fetchone()
        self.assertIsNotNone(row, "no hr row with bpm = 162")
        assert row is not None
        sensor, device = row
        self.assertEqual(device, "Polar H10 0000ABCD")
        self.assertEqual(sensor, "strap")
        # The product type is kept as its own column and is exactly the value
        # that would have misclassified this sample as a watch reading.
        origin = self.con.execute(
            "SELECT origin_product FROM records WHERE value = 162.0"
        ).fetchone()[0]
        self.assertEqual(origin, "Watch7,5")

    def test_every_sensor_is_labelled(self):
        got = dict(self.con.execute("SELECT sensor, count(*) FROM hr GROUP BY 1").fetchall())
        self.assertEqual(got.get("strap"), 1)
        self.assertEqual(got.get("airpods"), 1)
        self.assertEqual(got.get("watch"), 2)
        self.assertNotIn("other", got)

    def test_source_names_come_from_the_companion_database(self):
        names = {r[0] for r in self.con.execute("SELECT DISTINCT source_name FROM hr").fetchall()}
        self.assertEqual(names, {"Example Apple Watch", "Bluetooth Device", "Example AirPods Pro"})

    # --------------------------------------------------------------- calendar

    def test_local_date_follows_the_rows_own_timezone(self):
        row = self.con.execute("""
            SELECT local_date, local_time FROM records
            WHERE type = 'HeartRate' ORDER BY start_date DESC LIMIT 1""").fetchone()
        self.assertEqual(str(row[0]), "2026-08-27")
        self.assertEqual(str(row[1]), "00:30:00")

    # --------------------------------------------------------------- workouts

    def test_activity_type_is_resolved_to_a_name(self):
        activity = self.con.execute("SELECT activity FROM workouts").fetchone()[0]
        self.assertEqual(activity, "HighIntensityIntervalTraining")

    def test_duration_and_distance_come_from_the_activity_rows(self):
        row = self.con.execute("SELECT duration_min, distance_km FROM workouts").fetchone()
        self.assertAlmostEqual(row[0], 80.0, places=6)
        self.assertAlmostEqual(row[1], 12.5, places=6)

    def test_workout_heart_rate_is_weighted_by_segment_length(self):
        row = self.con.execute("SELECT avg_hr, max_hr, min_hr FROM workouts").fetchone()
        # 120 bpm for 60 min and 180 for 20 gives 135, not the 180 a max() picks.
        self.assertAlmostEqual(row[0], 135.0, places=3)
        self.assertAlmostEqual(row[1], 192.0, places=3)
        self.assertAlmostEqual(row[2], 90.0, places=3)

    def test_workout_energy_is_summed_across_segments(self):
        kcal = self.con.execute("SELECT active_kcal FROM workouts").fetchone()[0]
        self.assertAlmostEqual(kcal, 400.0, places=6)

    def test_workout_rows_are_not_also_records(self):
        n = self.con.execute("SELECT count(*) FROM records WHERE type_code = 79").fetchone()[0]
        self.assertEqual(n, 0)

    def test_events_are_named(self):
        row = self.con.execute("SELECT type, duration_min FROM workout_events").fetchone()
        self.assertEqual(row[0], "Lap")
        self.assertAlmostEqual(row[1], 1.0, places=6)

    # ----------------------------------------------------------------- route

    def test_route_points_are_extracted_from_the_location_series(self):
        rows = self.con.execute(
            "SELECT t, lat, lon, ele, speed_ms, course, hacc FROM route_points"
            " WHERE route_file = 'series_720' ORDER BY t"
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(rows[0][1], 48.1371, places=6)
        self.assertAlmostEqual(rows[0][2], 11.5754, places=6)
        self.assertAlmostEqual(rows[0][3], 519.7, places=3)
        self.assertAlmostEqual(rows[0][4], 3.10, places=3)
        self.assertAlmostEqual(rows[0][5], 90.0, places=3)
        self.assertAlmostEqual(rows[0][6], 1.2, places=3)

    def test_route_points_are_keyed_by_hfd_key_not_by_data_id(self):
        # The decoy row sits in Sydney under series_identifier = the route's own
        # data_id. Joining on data_id picks it up and nothing errors.
        far = self.con.execute("SELECT count(*) FROM route_points WHERE lat < 0").fetchone()[0]
        self.assertEqual(far, 0)

    def test_route_point_timestamps_are_real_wall_clock(self):
        row = self.con.execute("SELECT min(t), max(t) FROM route_points").fetchone()
        self.assertIsNotNone(row, "route_points is empty")
        assert row is not None
        first, last = row
        self.assertEqual((last - first).total_seconds(), 1234.0)

    def test_workout_route_file_links_to_its_own_points(self):
        n = self.con.execute("""
            SELECT count(*) FROM workouts w
            JOIN route_points r ON r.route_file = w.route_file""").fetchone()[0]
        self.assertEqual(n, 3)

    def test_a_deleted_route_does_not_duplicate_the_workout(self):
        n = self.con.execute("SELECT count(*) FROM workouts").fetchone()[0]
        self.assertEqual(n, 1)

    def test_the_live_route_wins_over_the_deleted_one(self):
        route_file = self.con.execute("SELECT route_file FROM workouts").fetchone()[0]
        self.assertEqual(route_file, "series_720")

    def test_a_twice_synced_route_does_not_duplicate_the_workout(self):
        # Two live routes on one workout is not a corrupt database; it is what a
        # re-sync leaves behind, and it must still be one workout.
        n = self.con.execute("SELECT count(*) FROM workouts").fetchone()[0]
        self.assertEqual(n, 1)

    def test_the_richer_of_two_routes_is_the_one_kept(self):
        route_file = self.con.execute("SELECT route_file FROM workouts").fetchone()[0]
        self.assertEqual(route_file, "series_720")

    # -------------------------------------------------------- workout blocks

    def test_structured_blocks_are_extracted_in_order(self):
        rows = self.con.execute(
            "SELECT seq, duration_min FROM workout_blocks WHERE NOT is_primary ORDER BY seq"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], [1, 2, 3, 4])
        got = [round(r[1], 4) for r in rows]
        self.assertEqual(got, [10.0, 4.0, 3.0, round(214 / 60, 4)])

    def test_blocks_carry_the_workout_they_belong_to(self):
        owners = {
            r[0]
            for r in self.con.execute("SELECT DISTINCT workout_id FROM workout_blocks").fetchall()
        }
        wid = self.con.execute("SELECT id FROM workouts").fetchone()[0]
        self.assertEqual(owners, {wid})

    def test_the_whole_session_row_is_flagged_primary(self):
        # The two is_primary_activity = 1 rows describe the session, not its
        # structure; filtering on the flag is what separates them.
        n = self.con.execute("SELECT count(*) FROM workout_blocks WHERE is_primary").fetchone()[0]
        self.assertEqual(n, 2)

    def test_blocks_do_not_change_the_workouts_own_duration(self):
        dur = self.con.execute("SELECT duration_min FROM workouts").fetchone()[0]
        self.assertAlmostEqual(dur, 80.0, places=6)

    # --------------------------------------------------------------- metadata

    def test_quantity_metadata_keeps_the_magnitude_not_the_unit(self):
        value = self.con.execute(
            "SELECT value FROM workout_metadata WHERE key = 'HKElevationAscended'"
        ).fetchone()[0]
        self.assertEqual(value, "122200.0 cm")

    def test_numeric_and_string_metadata_survive(self):
        got = dict(
            self.con.execute(
                "SELECT key, value FROM workout_metadata WHERE key <> 'HKElevationAscended'"
            ).fetchall()
        )
        self.assertEqual(got["HKIndoorWorkout"], "0.0")
        self.assertEqual(got["HKTimeZone"], "Europe/Rome")

    # ------------------------------------------------------------------ types

    def test_category_samples_land_in_value_text(self):
        row = self.con.execute(
            "SELECT type, value_text FROM records WHERE type = 'SleepAnalysis'"
        ).fetchone()
        self.assertEqual(row, ("SleepAnalysis", "3"))

    def test_unknown_type_codes_survive_rather_than_vanish(self):
        row = self.con.execute("SELECT type, value FROM records WHERE type_code = 999").fetchone()
        self.assertEqual(row[0], "type_999")
        self.assertAlmostEqual(row[1], 36.4, places=6)

    def test_known_codes_get_their_full_hk_identifier(self):
        full = self.con.execute(
            "SELECT DISTINCT type_full FROM records WHERE type = 'HeartRate'"
        ).fetchone()[0]
        self.assertEqual(full, "HKQuantityTypeIdentifierHeartRate")


if __name__ == "__main__":
    unittest.main()
