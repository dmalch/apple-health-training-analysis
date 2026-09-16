#!/usr/bin/env python3
"""
Convert an Apple Health export into a DuckDB database.

Usage:
    python3 health_to_duckdb.py ~/Downloads/export.zip --db data/health.duckdb
    python3 health_to_duckdb.py ./apple_health_export --db data/health.duckdb --no-routes

Accepts the export.zip straight from the iPhone, an unzipped folder, or a bare
export.xml. The XML is streamed with iterparse, so a multi-GB export never lands
in memory.

Why DuckDB and not more CSVs: export.xml is ~2 GB and holds ~10M records. The
CSV pipeline (parse_health_export.py) pre-aggregates to daily rows because
anything else is unusable in a spreadsheet. Per-second questions -- what did
heart rate do inside interval three, which sensor wrote each sample -- need the
raw stream, and that is only tolerable in a columnar store.

Staging via CSV is deliberate: DuckDB's CSV reader is multithreaded and beats
row-by-row executemany by roughly an order of magnitude on 10M rows.

Needs the duckdb module (see .venv). Everything else is stdlib.
"""

import argparse
import csv
import os
import shutil
import sys
import time
import zipfile
from xml.etree import ElementTree as ET

import duckdb

import athlete_profile

# Fields pulled out of <WorkoutStatistics> onto the workout row itself.
STAT_SHORTCUTS = {
    "HKQuantityTypeIdentifierHeartRate": "hr",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_kcal",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "basal_kcal",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "distance_km",
    "HKQuantityTypeIdentifierDistanceCycling": "distance_km",
    "HKQuantityTypeIdentifierDistanceSwimming": "distance_km",
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierRunningPower": "power_w",
    "HKQuantityTypeIdentifierRunningSpeed": "speed",
}

GPX_NS = "{http://www.topografix.com/GPX/1/1}"


# ------------------------------------------------------------------ utilities


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_km(value, unit):
    v = to_float(value)
    if v is None:
        return None
    unit = (unit or "").lower()
    if unit == "mi":
        return v * 1.609344
    if unit == "m":
        return v / 1000.0
    if unit == "yd":
        return v * 0.0009144
    return v


def to_min(value, unit):
    v = to_float(value)
    if v is None:
        return None
    unit = (unit or "").lower()
    if unit in ("s", "sec"):
        return v / 60.0
    if unit in ("h", "hr"):
        return v * 60.0
    return v


def to_kcal(value, unit):
    v = to_float(value)
    if v is None:
        return None
    if (unit or "").lower() == "kj":
        return v / 4.184
    return v


def open_export(path):
    """Yield a readable binary stream for export.xml, zipped or not."""
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        for cand in ("export.xml", "Export.xml"):
            full = os.path.join(path, cand)
            if os.path.exists(full):
                return open(full, "rb")
        raise SystemExit(f"No export.xml in {path}")
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith("export.xml")]
        if not names:
            raise SystemExit(f"No export.xml inside {path}")
        return zf.open(sorted(names, key=len)[0])
    return open(path, "rb")


def route_reader(path):
    """Yield (arcname, bytes) for every workout-route GPX in the export."""
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        root = os.path.join(path, "workout-routes")
        if not os.path.isdir(root):
            return
        for name in sorted(os.listdir(root)):
            if name.lower().endswith(".gpx"):
                with open(os.path.join(root, name), "rb") as fh:
                    yield name, fh.read()
        return
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        for name in sorted(zf.namelist()):
            if name.lower().endswith(".gpx"):
                yield os.path.basename(name), zf.read(name)


class Sink:
    """A CSV staging file that DuckDB will COPY from."""

    def __init__(self, stage_dir, name, columns):
        self.name = name
        self.columns = columns
        self.path = os.path.join(stage_dir, f"{name}.csv")
        # Held open for the lifetime of the Sink and closed in close(); the
        # writer is used across many calls, so a context manager does not fit.
        self._fh = open(self.path, "w", newline="")  # noqa: SIM115
        self._w = csv.writer(self._fh)
        self._w.writerow(columns)
        self.rows = 0

    def write(self, row):
        self._w.writerow(row)
        self.rows += 1

    def close(self):
        self._fh.close()


# ------------------------------------------------------------------- xml pass


def stream_xml(source, stage_dir, progress_every=1_000_000):
    sinks = {
        "records": Sink(
            stage_dir,
            "records",
            [
                "id",
                "type",
                "source_name",
                "source_version",
                "device",
                "unit",
                "creation_date",
                "start_date",
                "end_date",
                "value",
                "value_text",
            ],
        ),
        "record_metadata": Sink(stage_dir, "record_metadata", ["record_id", "key", "value"]),
        "workouts": Sink(
            stage_dir,
            "workouts",
            [
                "id",
                "activity",
                "duration_min",
                "source_name",
                "source_version",
                "device",
                "creation_date",
                "start_date",
                "end_date",
                "distance_km",
                "active_kcal",
                "basal_kcal",
                "steps",
                "avg_hr",
                "max_hr",
                "min_hr",
                "indoor",
                "route_file",
            ],
        ),
        "workout_events": Sink(
            stage_dir,
            "workout_events",
            [
                "workout_id",
                "type",
                "date",
                "duration_min",
            ],
        ),
        "workout_statistics": Sink(
            stage_dir,
            "workout_statistics",
            [
                "workout_id",
                "type",
                "start_date",
                "end_date",
                "average",
                "minimum",
                "maximum",
                "sum",
                "unit",
            ],
        ),
        "workout_metadata": Sink(stage_dir, "workout_metadata", ["workout_id", "key", "value"]),
        "activity_summary": Sink(
            stage_dir,
            "activity_summary",
            [
                "local_date",
                "active_energy",
                "active_energy_goal",
                "energy_unit",
                "exercise_min",
                "exercise_goal",
                "stand_hours",
                "stand_goal",
            ],
        ),
        "export_meta": Sink(stage_dir, "export_meta", ["key", "value"]),
    }

    fh = open_export(source)
    context = ET.iterparse(fh, events=("start", "end"))
    _, root = next(context)
    for key, value in root.attrib.items():
        sinks["export_meta"].write([key, value])

    record_id = 0
    workout_id = 0
    depth = 1
    started = time.time()

    for event, elem in context:
        if event == "start":
            depth += 1
            continue
        depth -= 1
        # Children are reached on the way out of their parent; only act on
        # top-level elements, then clear the whole subtree.
        if depth != 1:
            continue

        tag = elem.tag

        if tag == "Record":
            record_id += 1
            raw = elem.get("value")
            sinks["records"].write(
                [
                    record_id,
                    elem.get("type"),
                    elem.get("sourceName"),
                    elem.get("sourceVersion"),
                    elem.get("device"),
                    elem.get("unit"),
                    elem.get("creationDate"),
                    elem.get("startDate"),
                    elem.get("endDate"),
                    raw if to_float(raw) is not None else "",
                    "" if to_float(raw) is not None else (raw or ""),
                ]
            )
            for md in elem.findall("MetadataEntry"):
                sinks["record_metadata"].write([record_id, md.get("key"), md.get("value")])
            if record_id % progress_every == 0:
                rate = record_id / max(time.time() - started, 1e-9)
                print(f"  {record_id:>12,} records  ({rate:,.0f}/s)", file=sys.stderr)

        elif tag == "Workout":
            workout_id += 1
            stats = {}
            for st in elem.findall("WorkoutStatistics"):
                stype = st.get("type")
                unit = st.get("unit")
                sinks["workout_statistics"].write(
                    [
                        workout_id,
                        stype,
                        st.get("startDate"),
                        st.get("endDate"),
                        st.get("average"),
                        st.get("minimum"),
                        st.get("maximum"),
                        st.get("sum"),
                        unit,
                    ]
                )
                short = STAT_SHORTCUTS.get(stype)
                if short:
                    stats[short] = (st, unit)

            meta = {}
            for md in elem.findall("MetadataEntry"):
                meta[md.get("key")] = md.get("value")
                sinks["workout_metadata"].write([workout_id, md.get("key"), md.get("value")])

            for ev in elem.findall("WorkoutEvent"):
                sinks["workout_events"].write(
                    [
                        workout_id,
                        ev.get("type"),
                        ev.get("date"),
                        to_min(ev.get("duration"), ev.get("durationUnit")),
                    ]
                )

            route_file = ""
            for wr in elem.findall("WorkoutRoute"):
                for fr in wr.findall("FileReference"):
                    route_file = os.path.basename(fr.get("path") or "")

            # Distance and energy live on the element in old exports and in
            # <WorkoutStatistics> children in new ones. Prefer the statistic.
            if "distance_km" in stats:
                st, unit = stats["distance_km"]
                distance = to_km(st.get("sum"), unit)
            else:
                distance = to_km(elem.get("totalDistance"), elem.get("totalDistanceUnit"))

            if "active_kcal" in stats:
                st, unit = stats["active_kcal"]
                active = to_kcal(st.get("sum"), unit)
            else:
                active = to_kcal(elem.get("totalEnergyBurned"), elem.get("totalEnergyBurnedUnit"))

            basal = None
            if "basal_kcal" in stats:
                st, unit = stats["basal_kcal"]
                basal = to_kcal(st.get("sum"), unit)

            steps = None
            if "steps" in stats:
                steps = to_float(stats["steps"][0].get("sum"))

            avg_hr = max_hr = min_hr = None
            if "hr" in stats:
                st, _ = stats["hr"]
                avg_hr = to_float(st.get("average"))
                max_hr = to_float(st.get("maximum"))
                min_hr = to_float(st.get("minimum"))

            indoor = meta.get("HKIndoorWorkout")
            indoor = "" if indoor is None else ("true" if indoor in ("1", "1.0") else "false")

            activity = (elem.get("workoutActivityType") or "").replace("HKWorkoutActivityType", "")
            sinks["workouts"].write(
                [
                    workout_id,
                    activity,
                    to_min(elem.get("duration"), elem.get("durationUnit")),
                    elem.get("sourceName"),
                    elem.get("sourceVersion"),
                    elem.get("device"),
                    elem.get("creationDate"),
                    elem.get("startDate"),
                    elem.get("endDate"),
                    distance,
                    active,
                    basal,
                    steps,
                    avg_hr,
                    max_hr,
                    min_hr,
                    indoor,
                    route_file,
                ]
            )

        elif tag == "ActivitySummary":
            sinks["activity_summary"].write(
                [
                    elem.get("dateComponents"),
                    elem.get("activeEnergyBurned"),
                    elem.get("activeEnergyBurnedGoal"),
                    elem.get("activeEnergyBurnedUnit"),
                    elem.get("appleExerciseTime"),
                    elem.get("appleExerciseTimeGoal"),
                    elem.get("appleStandHours"),
                    elem.get("appleStandHoursGoal"),
                ]
            )

        elif tag in ("ExportDate", "Me"):
            for key, value in elem.attrib.items():
                sinks["export_meta"].write([f"{tag}.{key}", value])

        elem.clear()
        root.clear()

    fh.close()
    for sink in sinks.values():
        sink.close()
    return sinks


def gpx_ext_value(ext, tag):
    """One value out of a trkpt's <extensions>, or "" when absent."""
    if ext is None:
        return ""
    node = ext.find(f"{GPX_NS}{tag}")
    return node.text if node is not None and node.text else ""


def stream_routes(source, stage_dir):
    sink = Sink(
        stage_dir,
        "route_points",
        [
            "route_file",
            "t",
            "lat",
            "lon",
            "ele",
            "speed",
            "course",
            "hacc",
            "vacc",
        ],
    )
    files = 0
    for name, blob in route_reader(source):
        try:
            gpx = ET.fromstring(blob)
        except ET.ParseError:
            print(f"  skipped unparseable {name}", file=sys.stderr)
            continue
        files += 1
        # lat/lon are attributes, so the lon-before-lat ordering some exports
        # use makes no difference here.
        for pt in gpx.iter(f"{GPX_NS}trkpt"):
            ext = pt.find(f"{GPX_NS}extensions")

            def ext_val(tag, ext=ext):
                return gpx_ext_value(ext, tag)

            t = pt.find(f"{GPX_NS}time")
            ele = pt.find(f"{GPX_NS}ele")
            sink.write(
                [
                    name,
                    t.text if t is not None else "",
                    pt.get("lat"),
                    pt.get("lon"),
                    ele.text if ele is not None else "",
                    ext_val("speed"),
                    ext_val("course"),
                    ext_val("hAcc"),
                    ext_val("vAcc"),
                ]
            )
    sink.close()
    print(f"  {files} route files, {sink.rows:,} points", file=sys.stderr)
    return sink


# ------------------------------------------------------------------- loading

TS = "strptime({col}, '%Y-%m-%d %H:%M:%S %z')"
DEV = "regexp_extract({col}, 'name:([^,>]*)', 1)"


def csv_src(stage_dir, name):
    path = os.path.join(stage_dir, f"{name}.csv")
    return f"read_csv('{path}', header=true, all_varchar=true, quote='\"', escape='\"')"


def load(con, stage_dir, with_routes):
    def ts(col):
        return TS.format(col=col)

    def dev(col):
        return DEV.format(col=col)

    steps = [
        ("export_meta", f"SELECT key, value FROM {csv_src(stage_dir, 'export_meta')}"),
        (
            "records",
            f"""
            SELECT
                CAST(id AS BIGINT)                        AS id,
                replace(replace(type, 'HKQuantityTypeIdentifier', ''),
                        'HKCategoryTypeIdentifier', '')   AS type,
                type                                      AS type_full,
                source_name, source_version,
                nullif({dev("device")}, '')               AS device_name,
                device                                    AS device_raw,
                unit,
                {ts("creation_date")}                     AS creation_date,
                {ts("start_date")}                        AS start_date,
                {ts("end_date")}                          AS end_date,
                CAST(substr(start_date, 1, 10) AS DATE)   AS local_date,
                CAST(substr(start_date, 12, 8) AS TIME)   AS local_time,
                TRY_CAST(value AS DOUBLE)                 AS value,
                nullif(value_text, '')                    AS value_text
            FROM {csv_src(stage_dir, "records")}
        """,
        ),
        (
            "record_metadata",
            f"""
            SELECT CAST(record_id AS BIGINT) AS record_id, key, value
            FROM {csv_src(stage_dir, "record_metadata")}
        """,
        ),
        (
            "workouts",
            f"""
            SELECT
                CAST(id AS BIGINT)                        AS id,
                activity,
                TRY_CAST(duration_min AS DOUBLE)          AS duration_min,
                source_name, source_version,
                nullif({dev("device")}, '')               AS device_name,
                device                                    AS device_raw,
                {ts("creation_date")}                     AS creation_date,
                {ts("start_date")}                        AS start_date,
                {ts("end_date")}                          AS end_date,
                CAST(substr(start_date, 1, 10) AS DATE)   AS local_date,
                CAST(substr(start_date, 12, 8) AS TIME)   AS local_time,
                TRY_CAST(distance_km AS DOUBLE)           AS distance_km,
                TRY_CAST(active_kcal AS DOUBLE)           AS active_kcal,
                TRY_CAST(basal_kcal AS DOUBLE)            AS basal_kcal,
                TRY_CAST(steps AS DOUBLE)                 AS steps,
                TRY_CAST(avg_hr AS DOUBLE)                AS avg_hr,
                TRY_CAST(max_hr AS DOUBLE)                AS max_hr,
                TRY_CAST(min_hr AS DOUBLE)                AS min_hr,
                TRY_CAST(indoor AS BOOLEAN)               AS indoor,
                nullif(route_file, '')                    AS route_file
            FROM {csv_src(stage_dir, "workouts")}
        """,
        ),
        (
            "workout_events",
            f"""
            SELECT
                CAST(workout_id AS BIGINT)                AS workout_id,
                replace(type, 'HKWorkoutEventType', '')   AS type,
                {ts("date")}                              AS date,
                TRY_CAST(duration_min AS DOUBLE)          AS duration_min
            FROM {csv_src(stage_dir, "workout_events")}
        """,
        ),
        (
            "workout_statistics",
            f"""
            SELECT
                CAST(workout_id AS BIGINT)                AS workout_id,
                replace(type, 'HKQuantityTypeIdentifier', '') AS type,
                {ts("start_date")}                        AS start_date,
                {ts("end_date")}                          AS end_date,
                TRY_CAST(average AS DOUBLE)               AS average,
                TRY_CAST(minimum AS DOUBLE)               AS minimum,
                TRY_CAST(maximum AS DOUBLE)               AS maximum,
                TRY_CAST(sum AS DOUBLE)                   AS sum,
                unit
            FROM {csv_src(stage_dir, "workout_statistics")}
        """,
        ),
        (
            "workout_metadata",
            f"""
            SELECT CAST(workout_id AS BIGINT) AS workout_id, key, value
            FROM {csv_src(stage_dir, "workout_metadata")}
        """,
        ),
        (
            "activity_summary",
            f"""
            SELECT
                CAST(local_date AS DATE)                  AS local_date,
                TRY_CAST(active_energy AS DOUBLE)         AS active_energy,
                TRY_CAST(active_energy_goal AS DOUBLE)    AS active_energy_goal,
                energy_unit,
                TRY_CAST(exercise_min AS DOUBLE)          AS exercise_min,
                TRY_CAST(exercise_goal AS DOUBLE)         AS exercise_goal,
                TRY_CAST(stand_hours AS DOUBLE)           AS stand_hours,
                TRY_CAST(stand_goal AS DOUBLE)            AS stand_goal
            FROM {csv_src(stage_dir, "activity_summary")}
        """,
        ),
    ]

    if with_routes:
        steps.append(
            (
                "route_points",
                f"""
            SELECT
                route_file,
                CAST(t AS TIMESTAMPTZ)                    AS t,
                TRY_CAST(lat AS DOUBLE)                   AS lat,
                TRY_CAST(lon AS DOUBLE)                   AS lon,
                TRY_CAST(ele AS DOUBLE)                   AS ele,
                TRY_CAST(speed AS DOUBLE)                 AS speed_ms,
                TRY_CAST(course AS DOUBLE)                AS course,
                TRY_CAST(hacc AS DOUBLE)                  AS hacc,
                TRY_CAST(vacc AS DOUBLE)                  AS vacc
            FROM {csv_src(stage_dir, "route_points")}
        """,
            )
        )

    for name, select in steps:
        t0 = time.time()
        con.execute(f"DROP TABLE IF EXISTS {name}")
        con.execute(f"CREATE TABLE {name} AS {select}")
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"  {name:<20} {n:>12,} rows  ({time.time() - t0:.1f}s)", file=sys.stderr)


VIEWS = """
-- Heart-rate samples with the sensor spelled out. `device_name` is the honest
-- answer to "strap or watch?" -- sourceName is localised ('Bluetooth Device' /
-- 'Bluetooth-Gerät') and says nothing about which strap.
CREATE OR REPLACE VIEW hr AS
SELECT
    start_date AS t, local_date, local_time, value AS bpm,
    source_name, device_name,
    CASE
        WHEN device_name ILIKE '%polar%' OR device_name ILIKE '%H10%' THEN 'strap'
        -- AirPods Pro write heart rate too, 5k samples since Oct 2025, and on
        -- gym-machine runs they are the only sensor in the window. They used to
        -- fall through to 'other', which silently dropped those sessions from
        -- anything filtering on strap/watch.
        WHEN device_name ILIKE '%airpod%' OR source_name ILIKE '%airpod%' THEN 'airpods'
        WHEN device_name ILIKE '%watch%' OR source_name ILIKE '%watch%'  THEN 'watch'
        WHEN device_name ILIKE '%iphone%' OR source_name ILIKE '%iphone%' THEN 'phone'
        ELSE 'other'
    END AS sensor
FROM records
WHERE type = 'HeartRate';

-- One row per workout, pace filled in for foot-based activities.
CREATE OR REPLACE VIEW workout_summary AS
SELECT
    id, local_date, local_time, activity, source_name, device_name,
    duration_min, distance_km, avg_hr, max_hr, min_hr,
    active_kcal, indoor, route_file,
    CASE WHEN distance_km > 0 AND activity IN
              ('Running','Walking','Hiking','CrossCountrySkiing','Elliptical')
         THEN duration_min / distance_km END AS pace_min_per_km,
    CASE WHEN duration_min > 0 THEN distance_km / (duration_min / 60.0) END AS speed_kmh,
    start_date, end_date
FROM workouts;

-- Per-day sensor census: how many HR samples each device wrote.
CREATE OR REPLACE VIEW hr_sources_by_day AS
SELECT local_date, sensor, device_name, source_name,
       count(*) AS samples, min(bpm) AS min_bpm,
       round(avg(bpm), 1) AS avg_bpm, max(bpm) AS max_bpm,
       min(t) AS first_sample, max(t) AS last_sample
FROM hr
GROUP BY ALL;

-- Daily metrics, the DuckDB equivalent of daily_metrics.csv. Steps and walking
-- distance keep the largest single source instead of summing, because the
-- phone and the watch both record all day and summing double-counts badly.
CREATE OR REPLACE VIEW daily_metrics AS
WITH point AS (
    SELECT local_date, type, avg(value) AS v
    FROM records
    WHERE type IN ('RestingHeartRate','HeartRateVariabilitySDNN','VO2Max',
                   'BodyMass','WalkingHeartRateAverage','RespiratoryRate')
    GROUP BY ALL
),
summed AS (
    SELECT local_date, type, sum(value) AS v
    FROM records
    WHERE type IN ('ActiveEnergyBurned','AppleExerciseTime','AppleStandTime')
    GROUP BY ALL
),
per_source AS (
    SELECT local_date, type, max(v) AS v FROM (
        SELECT local_date, type, source_name, sum(value) AS v
        FROM records
        WHERE type IN ('StepCount','DistanceWalkingRunning')
        GROUP BY ALL
    ) GROUP BY ALL
),
all_metrics AS (
    SELECT * FROM point UNION ALL SELECT * FROM summed UNION ALL SELECT * FROM per_source
)
SELECT
    local_date,
    max(v) FILTER (type = 'RestingHeartRate')          AS resting_hr,
    max(v) FILTER (type = 'HeartRateVariabilitySDNN')  AS hrv_sdnn_ms,
    max(v) FILTER (type = 'VO2Max')                    AS vo2max,
    max(v) FILTER (type = 'BodyMass')                  AS weight_kg,
    max(v) FILTER (type = 'RespiratoryRate')           AS respiratory_rate,
    max(v) FILTER (type = 'StepCount')                 AS steps,
    max(v) FILTER (type = 'DistanceWalkingRunning')    AS distance_km,
    max(v) FILTER (type = 'ActiveEnergyBurned')        AS active_kcal,
    max(v) FILTER (type = 'AppleExerciseTime')         AS exercise_min
FROM all_metrics
GROUP BY ALL;
"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("export", help="export.zip, the unzipped folder, or export.xml")
    ap.add_argument("--db", default=None, help="Defaults to the profile database.")
    ap.add_argument("--no-routes", action="store_true", help="skip workout-routes GPX")
    ap.add_argument("--keep-staging", action="store_true", help="leave the CSVs behind")
    ap.add_argument("--stage-dir", default=None)
    athlete_profile.add_argument(ap, required=False)
    args = ap.parse_args()
    profile = athlete_profile.from_args(args, required=False)
    db = args.db or athlete_profile.field(profile, "db")
    if not db:
        raise SystemExit("no database. Pass --db PATH or --profile NAME.")

    db_path = os.path.expanduser(db)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    stage_dir = args.stage_dir or os.path.join(os.path.dirname(db_path) or ".", "_staging")
    os.makedirs(stage_dir, exist_ok=True)

    overall = time.time()
    print(f"streaming {args.export}", file=sys.stderr)
    sinks = stream_xml(args.export, stage_dir)
    for name, sink in sinks.items():
        print(f"  {name:<20} {sink.rows:>12,} rows staged", file=sys.stderr)

    with_routes = not args.no_routes
    if with_routes:
        print("reading workout routes", file=sys.stderr)
        rs = stream_routes(args.export, stage_dir)
        with_routes = rs.rows > 0

    print(f"loading into {db_path}", file=sys.stderr)
    if os.path.exists(db_path):
        os.remove(db_path)
    con = duckdb.connect(db_path)
    load(con, stage_dir, with_routes)
    con.execute(VIEWS)
    con.close()

    if not args.keep_staging:
        shutil.rmtree(stage_dir, ignore_errors=True)

    size = os.path.getsize(db_path) / 1e6
    print(f"done in {time.time() - overall:.0f}s -> {db_path} ({size:,.0f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main()
