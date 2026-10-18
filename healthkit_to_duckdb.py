#!/usr/bin/env python3
"""
Build the analysis database from what the HealthExport iOS app wrote.

Usage:
    ./.venv/bin/python healthkit_to_duckdb.py <export_dir> --profile NAME --db data/health-hk.duckdb
    ./.venv/bin/python healthkit_to_duckdb.py <export_dir> --db out.duckdb --compare data/health-live.duckdb

<export_dir> is the app's Documents folder as copied off the phone -- the one
holding `batches/` -- or that `batches/` folder itself. Every finished batch is
read (a batch is finished once its manifest.json exists) and the database is
rebuilt from all of them, newest copy of each sample winning, deleted samples
dropped. Rebuilding rather than appending keeps a re-read batch harmless.

Why this exists: the backup route (healthdb_to_duckdb.py) needs a full encrypted
backup, and on iOS 27 the scripted backup dies on the on-phone passcode prompt,
so every sync went through Finder by hand. The app reads the same store through
HealthKit, incrementally from saved anchors, and needs neither a backup nor its
password. See ios/HealthExport/README.md.

The tables and views match healthdb_to_duckdb.py (VIEWS is imported, not
copied), so analyze.py, analyze_intervals.py and hq.py run unchanged. What
differs is listed in docs/apple-health.md under "The HealthKit route"; the short
version is that `id` is derived from the sample's UUID rather than the phone's
row number, and `creation_date` is NULL because HealthKit does not expose it.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import duckdb

import athlete_profile
from health_to_duckdb import VIEWS
from healthdb_to_duckdb import (
    ACTIVITY_TYPES,
    CATEGORY_TYPES,
    OTHER_TYPES,
    QUANTITY_TYPES,
    WORKOUT_EVENT_TYPES,
    plan_step,
)

# Heart rate is the one type read as its readings, as on the backup route: see
# healthdb_to_duckdb.SERIES_READ_AS_POINTS.
SERIES_READ_AS_POINTS = ("HeartRate",)

# HKWorkoutSessionLocationType, the public enum: 1 unknown, 2 indoor, 3 outdoor.
LOCATION_INDOOR, LOCATION_OUTDOOR = 2, 3


def hk_id(uuid):
    """A BIGINT id for a HealthKit UUID: its first 60 bits.

    The backup route's ids are the phone's row numbers, which HealthKit does not
    expose. 60 bits keeps the id positive in a signed BIGINT; a collision
    between two of a person's few million samples is around one in 10^5.
    """
    return int(uuid.replace("-", "")[:15], 16)


def hk_id_sql(col):
    """hk_id() in DuckDB, for a VARCHAR column holding a UUID."""
    return f"CAST('0x' || substr(replace({col}, '-', ''), 1, 15) AS BIGINT)"


# ------------------------------------------------------------------ batches


def batch_dirs(root):
    """Finished batch folders under an export folder, oldest first."""
    root = Path(root).expanduser()
    base = root / "batches" if (root / "batches").is_dir() else root
    if not base.is_dir():
        raise SystemExit(f"{root}: no such folder")
    dirs = sorted(p for p in base.iterdir() if p.is_dir() and (p / "manifest.json").exists())
    if not dirs:
        raise SystemExit(f"{base}: no finished batch (a folder with a manifest.json)")
    return dirs


def sql_list(paths):
    return "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"


def stage(con, table, dirs, filename, columns):
    """Every batch's `filename` as one temp table, plus the batch it came from."""
    files = [
        d / filename for d in dirs if (d / filename).exists() and (d / filename).stat().st_size
    ]
    decl = ", ".join(f'"{k}" {v}' for k, v in columns.items())
    if not files:
        con.execute(f"CREATE TEMP TABLE {table} ({decl}, batch VARCHAR)")
        return
    spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"
    con.execute(f"""
        CREATE TEMP TABLE {table} AS
        SELECT * EXCLUDE (filename),
               regexp_extract(filename, '([^/]+)/[^/]+$', 1) AS batch
        FROM read_json({sql_list(files)}, format = 'newline_delimited',
                       columns = {spec}, filename = true)
    """)


SAMPLE_COLUMNS = {
    "uuid": "VARCHAR",
    "type": "VARCHAR",
    "start": "DOUBLE",
    "end": "DOUBLE",
    "value": "DOUBLE",
    "unit": "VARCHAR",
    "count": "BIGINT",
    "value_int": "BIGINT",
    "source": "VARCHAR",
    "bundle": "VARCHAR",
    "source_version": "VARCHAR",
    "product": "VARCHAR",
    "device": "VARCHAR",
    "model": "VARCHAR",
    "metadata": "JSON",
}


def load_staging(con, dirs):
    stage(con, "stage_samples", dirs, "samples.jsonl", SAMPLE_COLUMNS)
    stage(con, "stage_routes", dirs, "routes.jsonl", {**SAMPLE_COLUMNS, "workout": "VARCHAR"})
    stage(
        con,
        "stage_points",
        dirs,
        "series_points.jsonl",
        {"uuid": "VARCHAR", "t": "DOUBLE", "dur": "DOUBLE", "value": "DOUBLE"},
    )
    stage(
        con,
        "stage_route_points",
        dirs,
        "route_points.jsonl",
        {
            "route": "VARCHAR",
            "t": "DOUBLE",
            "lat": "DOUBLE",
            "lon": "DOUBLE",
            "ele": "DOUBLE",
            "speed": "DOUBLE",
            "course": "DOUBLE",
            "hacc": "DOUBLE",
            "vacc": "DOUBLE",
        },
    )
    stage(
        con,
        "stage_heartbeats",
        dirs,
        "heartbeats.jsonl",
        {"uuid": "VARCHAR", "t": "DOUBLE", "gap": "BOOLEAN"},
    )
    stage(
        con,
        "stage_activity",
        dirs,
        "activity_summary.jsonl",
        {
            "date": "DATE",
            "active_energy": "DOUBLE",
            "active_energy_goal": "DOUBLE",
            "exercise_min": "DOUBLE",
            "exercise_goal": "DOUBLE",
            "stand_hours": "DOUBLE",
            "stand_goal": "DOUBLE",
        },
    )
    stage(con, "stage_deleted", dirs, "deleted.jsonl", {"uuid": "VARCHAR", "type": "VARCHAR"})
    con.execute("CREATE TEMP TABLE deleted AS SELECT DISTINCT uuid FROM stage_deleted")

    # Newest copy of each sample, and nothing HealthKit has since reported deleted.
    # Routes are samples too; the backup route keeps them in `records`, so do we.
    con.execute("""
        CREATE TEMP TABLE samples AS
        SELECT * FROM (
            SELECT * FROM stage_samples
            UNION ALL BY NAME
            SELECT * EXCLUDE (workout) FROM stage_routes
        )
        WHERE uuid NOT IN (SELECT uuid FROM deleted)
        QUALIFY row_number() OVER (PARTITION BY uuid ORDER BY batch DESC) = 1
    """)
    con.execute("""
        CREATE TEMP TABLE routes AS
        SELECT * FROM stage_routes
        WHERE uuid NOT IN (SELECT uuid FROM deleted)
        QUALIFY row_number() OVER (PARTITION BY uuid ORDER BY batch DESC) = 1
    """)


# ---------------------------------------------------------------- the tables


def type_tables(con):
    """hk_types / hk_activities / hk_events, as the backup route has them."""
    con.execute("CREATE TABLE hk_types (code BIGINT, name VARCHAR, full_name VARCHAR)")
    rows = [(c, n, "HKQuantityTypeIdentifier" + n) for c, n in QUANTITY_TYPES.items()]
    rows += [(c, n, "HKCategoryTypeIdentifier" + n) for c, n in CATEGORY_TYPES.items()]
    rows += [(c, n, full) for c, (n, full) in OTHER_TYPES.items()]
    con.executemany("INSERT INTO hk_types VALUES (?, ?, ?)", sorted(rows))
    # Values arrive in display units already; the table exists for the surface.
    con.execute("CREATE TABLE hk_scale (name VARCHAR, factor DOUBLE, unit VARCHAR)")
    for table, mapping in (("hk_activities", ACTIVITY_TYPES), ("hk_events", WORKOUT_EVENT_TYPES)):
        con.execute(f"CREATE TABLE {table} (code BIGINT, name VARCHAR)")
        con.executemany(f"INSERT INTO {table} VALUES (?, ?)", sorted(mapping.items()))


def build_series_points(con):
    """The readings inside each heart-rate series, as `series_points` (uuid, t, value).

    Same expansion as healthdb_to_duckdb.build_series_points, over the points
    the app read with HKQuantitySeriesSampleQuery: a point whose interval has a
    duration absorbed a run of identical readings, so it comes back as readings
    at both ends and evenly in between, at the series' own cadence. A point
    with no duration is one reading.
    """
    # Points belong to the batch whose sample row survived, so a series read
    # twice is not doubled.
    con.execute("""
        CREATE TEMP TABLE series_points AS
        WITH points AS (
            SELECT p.uuid, p.t, p.dur, p.value,
                   lead(p.t) OVER (PARTITION BY p.uuid ORDER BY p.t) - p.t - p.dur AS gap
            FROM stage_points p JOIN samples s ON s.uuid = p.uuid AND s.batch = p.batch
        ),
        cadence AS (
            SELECT uuid, greatest(median(gap), 1.0) AS every
            FROM points WHERE gap > 0 GROUP BY uuid
        ),
        runs AS (
            SELECT p.uuid, p.t, p.dur, p.value,
                   CASE WHEN p.dur > 0
                        THEN greatest(1, CAST(round(p.dur / coalesce(c.every, p.dur)) AS BIGINT))
                        ELSE 0 END AS n
            FROM points p LEFT JOIN cadence c USING (uuid)
        )
        SELECT uuid, t + dur * i / greatest(n, 1) AS t, value
        FROM (SELECT *, unnest(generate_series(0, n)) AS i FROM runs)
    """)


def build_records(con, tz):
    series_types = ", ".join(f"'{t}'" for t in SERIES_READ_AS_POINTS)
    con.execute(f"""
        CREATE TABLE records AS
        WITH raw AS (
            SELECT s.*, pt.t AS pt_t, pt.value AS pt_value,
                   coalesce(json_extract_string(s.metadata, '$.HKTimeZone'), '{tz}') AS tz
            FROM samples s
            LEFT JOIN series_points pt ON pt.uuid = s.uuid AND s.type IN ({series_types})
        )
        SELECT
            {hk_id_sql("raw.uuid")}                         AS id,
            raw.type,
            coalesce(t.full_name, raw.type)                 AS type_full,
            t.code                                          AS type_code,
            raw.source                                      AS source_name,
            raw.source_version,
            coalesce(raw.device, raw.product)               AS device_name,
            raw.product                                     AS device_raw,
            raw.product                                     AS origin_product,
            raw.unit,
            CAST(NULL AS TIMESTAMPTZ)                       AS creation_date,
            to_timestamp(coalesce(raw.pt_t, raw.start))     AS start_date,
            to_timestamp(coalesce(raw.pt_t, raw."end"))     AS end_date,
            CAST(timezone(raw.tz, to_timestamp(coalesce(raw.pt_t, raw.start))) AS DATE) AS local_date,
            CAST(timezone(raw.tz, to_timestamp(coalesce(raw.pt_t, raw.start))) AS TIME) AS local_time,
            coalesce(raw.pt_value, raw.value)               AS value,
            CAST(raw.value_int AS VARCHAR)                  AS value_text
        FROM raw LEFT JOIN hk_types t ON t.name = raw.type
    """)
    con.execute(f"""
        CREATE TABLE quantity_series AS
        SELECT r.id, r.type, r.source_name, r.device_name, r.start_date, r.end_date,
               r.local_date, r.local_time, s.value, s."count" AS points, r.readings
        FROM (
            SELECT id, any_value(type) AS type, any_value(source_name) AS source_name,
                   any_value(device_name) AS device_name,
                   min(start_date) AS start_date, max(end_date) AS end_date,
                   arg_min(local_date, start_date) AS local_date,
                   arg_min(local_time, start_date) AS local_time,
                   count(*) AS readings
            FROM records
            WHERE id IN (SELECT {hk_id_sql("uuid")} FROM series_points)
            GROUP BY id
        ) r
        JOIN samples s ON {hk_id_sql("s.uuid")} = r.id
    """)
    con.execute(f"""
        CREATE TABLE heartbeat_points AS
        SELECT {hk_id_sql("h.uuid")} AS series_id, to_timestamp(h.t) AS t,
               h.gap AS preceded_by_gap
        FROM stage_heartbeats h JOIN samples s ON s.uuid = h.uuid AND s.batch = h.batch
        ORDER BY series_id, t
    """)
    for table in ("records", "quantity_series", "heartbeat_points"):
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<20} {n:>12,} rows", file=sys.stderr)


def build_routes(con):
    con.execute("""
        CREATE TABLE route_points AS
        SELECT 'hk_' || p.route AS route_file, to_timestamp(p.t) AS t,
               p.lat, p.lon, p.ele, p.speed AS speed_ms, p.course, p.hacc, p.vacc
        FROM stage_route_points p JOIN routes r ON r.uuid = p.route AND r.batch = p.batch
        ORDER BY route_file, t
    """)
    n = con.execute("SELECT count(*) FROM route_points").fetchone()[0]
    print(f"  {'route_points':<20} {n:>12,} rows", file=sys.stderr)


def build_activity_summary(con):
    con.execute("""
        CREATE TABLE activity_summary AS
        SELECT "date" AS local_date, active_energy, active_energy_goal, 'kcal' AS energy_unit,
               exercise_min, exercise_goal, stand_hours, stand_goal
        FROM stage_activity
        QUALIFY row_number() OVER (PARTITION BY "date" ORDER BY batch DESC) = 1
        ORDER BY local_date
    """)


# ------------------------------------------------------------------ workouts


def load_workouts(dirs, deleted):
    """Newest copy of every workout across the batches, deleted ones dropped."""
    latest = {}
    for d in dirs:
        path = d / "workouts.jsonl"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    w = json.loads(line)
                    latest[w["uuid"]] = w
    return [w for uuid, w in latest.items() if uuid not in deleted]


def plan_blocks(plan):
    """The plan as healthdb_to_duckdb.decode_plan returns it: steps per block."""
    if not isinstance(plan, dict) or plan.get("kind") != "custom":
        return None
    blocks = [b.get("steps") or [] for b in plan.get("blocks") or []]
    return [b for b in blocks if b] or None


def plan_sequence(plan, blocks):
    """(key path, step) for every step in the order the watch runs them."""
    seq = []
    for b, (block, raw) in enumerate(zip(blocks, plan["blocks"], strict=False)):
        for i in range(max(int(raw.get("iterations") or 1), 1)):
            for s, step in enumerate(block):
                seq.append((f"{b}.{i}.{s}", step))
    return seq


def label_activities(workout, activities):
    """(key path, step) for each activity, or (None, None) where unknown.

    The step an activity ran is named by `WOIntervalStepKeyPath` in its metadata
    when HealthKit passes it through. Without it, the activities are matched to
    the plan by position, but only when there are exactly as many as the plan
    has steps -- a session stopped early or with a skipped step stays
    unlabelled, and analyze_intervals.py then infers the kinds from effort.
    """
    blocks = plan_blocks(workout.get("plan"))
    unknown = [(None, None)] * len(activities)
    if not blocks:
        return unknown
    paths = [(a.get("metadata") or {}).get("WOIntervalStepKeyPath") for a in activities]
    if all(isinstance(p, str) for p in paths):
        return [(p, plan_step(blocks, p)) for p in paths]
    seq = plan_sequence(workout["plan"], blocks)
    return seq if len(seq) == len(activities) else unknown


PLAN_FIELDS = ("kind", "goal_value", "goal_unit", "target_type", "target_min", "target_max")


def block_row(uid, seq, primary, start, end, duration_s, path=None, step=None):
    """One wk_blocks row; the plan columns stay NULL without a step."""
    step = step or {}
    kind, *rest = (step.get(f) for f in PLAN_FIELDS)
    return (uid, seq, primary, start, end, duration_s, kind, path, *rest)


def workout_tables(workouts):
    """Rows for the workout staging tables, from the app's nested JSON."""
    raw, stats, events, blocks, meta = [], [], [], [], []
    for w in workouts:
        uid = w["uuid"]
        acts = sorted(w.get("activities") or [], key=lambda a: a["start"])
        top = {s["type"]: s for s in w.get("statistics") or []}
        hr = top.get("HeartRate") or {}
        distance = w.get("distance_km")
        if distance is None:
            distance = (
                sum(s.get("sum") or 0 for t, s in top.items() if t.startswith("Distance")) or None
            )
        location = acts[0].get("location_type") if acts else None
        indoor = (w.get("metadata") or {}).get("HKIndoorWorkout")
        if location in (LOCATION_INDOOR, LOCATION_OUTDOOR):
            indoor = location == LOCATION_INDOOR
        elif indoor is not None:
            indoor = bool(indoor)
        raw.append(
            (
                uid,
                w.get("activity_type"),
                w.get("duration_s"),
                w.get("source"),
                w.get("source_version"),
                w.get("device") or w.get("product"),
                w["start"],
                w["end"],
                distance,
                w.get("energy_kcal"),
                hr.get("avg"),
                hr.get("max"),
                hr.get("min"),
                indoor,
                (w.get("metadata") or {}).get("HKTimeZone"),
            )
        )

        # The backup route reads one quantity per statistics row, an average or
        # a sum depending on the type, into both columns. Do the same, so a query
        # that reads `average` for energy or `sum` for heart rate agrees.
        def stat_rows(scope, items, uid=uid):
            for s in items or []:
                avg = s.get("avg") if s.get("avg") is not None else s.get("sum")
                total = s.get("sum") if s.get("sum") is not None else s.get("avg")
                yield (uid, scope, s["type"], s["start"], s["end"], avg, s.get("min"),
                       s.get("max"), total, s.get("unit"))  # fmt: skip

        stats.extend(stat_rows("workout", w.get("statistics")))
        if len(acts) > 1:
            for a in acts:
                stats.extend(stat_rows("activity", a.get("statistics")))
        for e in w.get("events") or []:
            events.append((uid, e["type"], e["start"], e.get("duration_s")))

        # One primary row for the session as a whole, as the backup route has;
        # the activities are the blocks only when there is more than one.
        blocks.append(block_row(uid, 1, True, w["start"], w["end"], w.get("duration_s")))
        if len(acts) > 1:
            labels = label_activities(w, acts)
            for seq, (a, (path, step)) in enumerate(zip(acts, labels, strict=True), 1):
                end = a.get("end") or a["start"] + (a.get("duration_s") or 0)
                blocks.append(
                    block_row(uid, seq, False, a["start"], end, a.get("duration_s"), path, step)
                )
        for key, value in (w.get("metadata") or {}).items():
            meta.append((uid, key, value if isinstance(value, str) else json.dumps(value)))
    return raw, stats, events, blocks, meta


def build_workouts(con, workouts, tz):
    raw, stats, events, blocks, meta = workout_tables(workouts)
    con.execute("""
        CREATE TEMP TABLE wk_raw (
            uuid VARCHAR, activity_code BIGINT, duration_s DOUBLE, source VARCHAR,
            source_version VARCHAR, device VARCHAR, "start" DOUBLE, "end" DOUBLE,
            distance_km DOUBLE, energy_kcal DOUBLE, avg_hr DOUBLE, max_hr DOUBLE,
            min_hr DOUBLE, indoor BOOLEAN, tz VARCHAR)""")
    con.execute("""
        CREATE TEMP TABLE wk_stats (
            uuid VARCHAR, scope VARCHAR, type VARCHAR, "start" DOUBLE, "end" DOUBLE,
            average DOUBLE, minimum DOUBLE, maximum DOUBLE, sum DOUBLE, unit VARCHAR)""")
    con.execute(
        'CREATE TEMP TABLE wk_events (uuid VARCHAR, type BIGINT, "start" DOUBLE, duration_s DOUBLE)'
    )
    con.execute("""
        CREATE TEMP TABLE wk_blocks (
            uuid VARCHAR, seq BIGINT, is_primary BOOLEAN, "start" DOUBLE, "end" DOUBLE,
            duration_s DOUBLE, kind VARCHAR, step_path VARCHAR, goal_value DOUBLE,
            goal_unit VARCHAR, target_type VARCHAR, target_min DOUBLE, target_max DOUBLE)""")
    con.execute("CREATE TEMP TABLE wk_meta (uuid VARCHAR, key VARCHAR, value VARCHAR)")
    for table, rows in (
        ("wk_raw", raw),
        ("wk_stats", stats),
        ("wk_events", events),
        ("wk_blocks", blocks),
        ("wk_meta", meta),
    ):
        if rows:
            marks = ", ".join("?" * len(rows[0]))
            con.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)

    wid = hk_id_sql("w.uuid")
    # Workouts are placed on the profile's wall clock, as on the backup route.
    con.execute(f"""
        CREATE TABLE workouts AS
        SELECT
            {wid}                                           AS id,
            coalesce(act.name, 'activity_' || CAST(w.activity_code AS VARCHAR)) AS activity,
            w.duration_s / 60.0                             AS duration_min,
            w.source                                        AS source_name,
            w.source_version,
            w.device                                        AS device_name,
            w.device                                        AS device_raw,
            CAST(NULL AS TIMESTAMPTZ)                       AS creation_date,
            to_timestamp(w."start")                         AS start_date,
            to_timestamp(w."end")                           AS end_date,
            CAST(timezone('{tz}', to_timestamp(w."start")) AS DATE) AS local_date,
            CAST(timezone('{tz}', to_timestamp(w."start")) AS TIME) AS local_time,
            w.distance_km,
            w.energy_kcal                                   AS active_kcal,
            CAST(NULL AS DOUBLE)                            AS basal_kcal,
            CAST(NULL AS DOUBLE)                            AS steps,
            -- HealthKit's own session average, rounded as the backup route
            -- rounds its weighted one.
            round(w.avg_hr, 3)                              AS avg_hr,
            w.max_hr, w.min_hr,
            w.indoor,
            r.route_file
        FROM wk_raw w
        LEFT JOIN hk_activities act ON act.code = w.activity_code
        LEFT JOIN (
            -- One route per workout: the richest, lowest UUID breaking a tie.
            SELECT workout, 'hk_' || uuid AS route_file FROM routes
            QUALIFY row_number() OVER (PARTITION BY workout ORDER BY "count" DESC, uuid) = 1
        ) r ON r.workout = w.uuid
        ORDER BY start_date
    """)
    con.execute(f"""
        CREATE TABLE workout_statistics AS
        SELECT {wid} AS workout_id, w.type, to_timestamp(w."start") AS start_date,
               to_timestamp(w."end") AS end_date, w.average, w.minimum, w.maximum, w.sum, w.unit
        FROM wk_stats w
    """)
    con.execute(f"""
        CREATE TABLE workout_events AS
        SELECT {wid} AS workout_id,
               coalesce(ev.name, 'type_' || CAST(w.type AS VARCHAR)) AS type,
               to_timestamp(w."start") AS date, w.duration_s / 60.0 AS duration_min
        FROM wk_events w LEFT JOIN hk_events ev ON ev.code = w.type
    """)
    con.execute(f"""
        CREATE TABLE workout_blocks AS
        SELECT {wid} AS workout_id, w.seq, w.is_primary,
               to_timestamp(w."start") AS start_date, to_timestamp(w."end") AS end_date,
               w.duration_s / 60.0 AS duration_min, w.kind, w.step_path, w.goal_value,
               w.goal_unit, w.target_type, w.target_min, w.target_max
        FROM wk_blocks w
        ORDER BY workout_id, is_primary DESC, seq
    """)
    con.execute(
        f"CREATE TABLE workout_metadata AS SELECT {wid} AS workout_id, key, value FROM wk_meta w"
    )
    for table in ("workouts", "workout_statistics", "workout_events", "workout_blocks"):
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<20} {n:>12,} rows", file=sys.stderr)


def build_metadata(con):
    con.execute(f"""
        CREATE TABLE record_metadata AS
        SELECT {hk_id_sql("uuid")} AS record_id, key,
               json_extract_string(metadata, '$."' || replace(key, '"', '\\"') || '"') AS value
        FROM (SELECT uuid, metadata, unnest(json_keys(metadata)) AS key
              FROM samples WHERE metadata IS NOT NULL)
        UNION ALL
        SELECT workout_id, key, value FROM workout_metadata
    """)
    n = con.execute("SELECT count(*) FROM record_metadata").fetchone()[0]
    print(f"  {'record_metadata':<20} {n:>12,} rows", file=sys.stderr)


def build(dirs, out_db, tz):
    if os.path.exists(out_db):
        os.remove(out_db)
    con = duckdb.connect(out_db)
    type_tables(con)
    load_staging(con, dirs)
    build_series_points(con)
    build_records(con, tz)
    build_routes(con)
    deleted = {u for (u,) in con.execute("SELECT uuid FROM deleted").fetchall()}
    build_workouts(con, load_workouts(dirs, deleted), tz)
    build_metadata(con)
    build_activity_summary(con)

    manifests = [json.loads((d / "manifest.json").read_text()) for d in dirs]
    con.execute("CREATE TABLE export_meta (key VARCHAR, value VARCHAR)")
    con.executemany(
        "INSERT INTO export_meta VALUES (?, ?)",
        [
            ("source", "healthkit"),
            ("batches", str(len(dirs))),
            ("newest_batch", dirs[-1].name),
            ("since", str(manifests[0].get("since"))),
            ("system_version", str(manifests[-1].get("system_version"))),
        ],
    )
    con.execute(VIEWS)
    con.close()
    return out_db


# --------------------------------------------------------------- comparison


def compare(hk_db, other_db, tz, out=sys.stdout):
    """What differs between this database and a backup-built one, where they overlap.

    The window runs from the export's first day to the day before the other
    database's last sample, so a sync taken on different days does not read as
    a gap. The first day is the export's `since`, not its earliest sample: the
    query takes every sample that *overlaps* that instant, so a few start the
    day before, and counting that day compared a nearly empty day against a full
    one and read as a 3-5% shortfall in every type. Workouts are matched by
    start time, since the ids differ by design.
    """
    con = duckdb.connect(hk_db, read_only=True)
    con.execute(f"ATTACH '{other_db}' AS other (READ_ONLY)")
    days = con.execute(
        """
        SELECT greatest(
                   (SELECT CAST(timezone(?, to_timestamp(CAST(value AS DOUBLE))) AS DATE)
                    FROM export_meta WHERE key = 'since'),
                   (SELECT min(local_date) + 1 FROM other.records)),
               least((SELECT max(local_date) FROM records),
                     (SELECT max(local_date) FROM other.records)) - 1""",
        [tz],
    ).fetchone()
    lo_day, hi_day = days[0], days[1]
    print(f"# Overlap {lo_day} .. {hi_day} (whole days)\n", file=out)

    print("## records by type (count, mean)", file=out)
    rows = con.execute(
        """
        WITH a AS (SELECT type, count(*) n, avg(value) v FROM records
                   WHERE local_date BETWEEN ? AND ? GROUP BY type),
             b AS (SELECT type, count(*) n, avg(value) v FROM other.records
                   WHERE local_date BETWEEN ? AND ? GROUP BY type)
        SELECT coalesce(a.type, b.type) AS type, a.n, b.n, a.v, b.v
        FROM a FULL JOIN b USING (type) ORDER BY coalesce(b.n, 0) + coalesce(a.n, 0) DESC
        """,
        [lo_day, hi_day, lo_day, hi_day],
    ).fetchall()
    print(
        f"  {'type':<34} {'healthkit':>10} {'backup':>10}  {'mean hk':>10} {'mean bk':>10}",
        file=out,
    )
    for t, na, nb, va, vb in rows:
        same_n = na == nb
        same_v = (va is None and vb is None) or (
            va is not None and vb is not None and abs(va - vb) <= 1e-6 * max(abs(vb), 1)
        )
        flag = "" if same_n and same_v else "  <--"

        def num(x):
            return f"{x:>10.3f}" if x is not None else f"{'-':>10}"

        print(f"  {t:<34} {na or 0:>10,} {nb or 0:>10,}  {num(va)} {num(vb)}{flag}", file=out)

    print("\n## heart rate by sensor", file=out)
    for row in con.execute(
        """
        SELECT coalesce(a.sensor, b.sensor), a.n, b.n, a.mx, b.mx FROM
          (SELECT sensor, count(*) n, max(bpm) mx FROM hr WHERE local_date BETWEEN ? AND ? GROUP BY 1) a
        FULL JOIN
          (SELECT sensor, count(*) n, max(bpm) mx FROM other.hr WHERE local_date BETWEEN ? AND ? GROUP BY 1) b
        USING (sensor) ORDER BY 1
        """,
        [lo_day, hi_day, lo_day, hi_day],
    ).fetchall():
        print(f"  {row[0]!s:<10} healthkit {row[1] or 0:>8,}  backup {row[2] or 0:>8,}  "
              f"max {row[3]} / {row[4]}", file=out)  # fmt: skip

    print("\n## workouts (matched by start time, within 2 s)", file=out)
    pairs = con.execute(
        """
        SELECT a.id, b.id, a.local_date, a.local_time, a.activity, b.activity,
               a.duration_min, b.duration_min, a.distance_km, b.distance_km,
               a.active_kcal, b.active_kcal, a.avg_hr, b.avg_hr, a.max_hr, b.max_hr,
               a.min_hr, b.min_hr, a.indoor, b.indoor, a.route_file, b.route_file
        FROM workouts a
        FULL JOIN (SELECT * FROM other.workouts WHERE local_date BETWEEN ? AND ?) b
          ON abs(epoch(a.start_date) - epoch(b.start_date)) < 2
        WHERE coalesce(a.local_date, b.local_date) BETWEEN ? AND ?
        ORDER BY coalesce(a.start_date, b.start_date)
        """,
        [lo_day, hi_day, lo_day, hi_day],
    ).fetchall()
    fields = ("activity", "duration_min", "distance_km", "active_kcal", "avg_hr", "max_hr",
              "min_hr", "indoor")  # fmt: skip
    for p in pairs:
        a_id, b_id, day, at = p[0], p[1], p[2], p[3]
        if a_id is None or b_id is None:
            side = "only in backup" if a_id is None else "only in healthkit"
            print(f"  {side}: {b_id or a_id}", file=out)
            continue
        diffs = []
        for i, name in enumerate(fields):
            va, vb = p[4 + 2 * i], p[5 + 2 * i]
            if isinstance(va, float) and isinstance(vb, float):
                if abs(va - vb) > 0.005 * max(abs(vb), 1):
                    diffs.append(f"{name} {va:.2f} vs {vb:.2f}")
            elif va != vb:
                diffs.append(f"{name} {va} vs {vb}")
        extra = workout_detail(con, a_id, b_id, p[20], p[21])
        line = "; ".join(diffs + extra) or "identical"
        print(f"  {day} {str(at)[:5]} {p[4]:<28} hk {a_id} / bk {b_id}: {line}", file=out)
    con.close()


def workout_detail(con, a_id, b_id, a_route, b_route):
    """Differences in the side tables of one matched workout."""
    out = []
    q = con.execute
    ra = q("SELECT count(*) FROM route_points WHERE route_file = ?", [a_route]).fetchone()[0]
    rb = q("SELECT count(*) FROM other.route_points WHERE route_file = ?", [b_route]).fetchone()[0]
    if ra != rb:
        out.append(f"route points {ra} vs {rb}")
    ea = q("SELECT type, count(*) FROM workout_events WHERE workout_id = ? GROUP BY 1 ORDER BY 1",
           [a_id]).fetchall()  # fmt: skip
    eb = q("SELECT type, count(*) FROM other.workout_events WHERE workout_id = ? GROUP BY 1 ORDER BY 1",
           [b_id]).fetchall()  # fmt: skip
    if ea != eb:
        out.append(f"events {dict(ea)} vs {dict(eb)}")
    sql = ("SELECT count(*), count(kind), string_agg(coalesce(kind, '?'), ',' ORDER BY seq) "
           "FROM {t} WHERE workout_id = ? AND NOT is_primary")  # fmt: skip
    ba = q(sql.format(t="workout_blocks"), [a_id]).fetchone()
    bb = q(sql.format(t="other.workout_blocks"), [b_id]).fetchone()
    if ba != bb:
        out.append(f"blocks {ba[0]} ({ba[2]}) vs {bb[0]} ({bb[2]})")
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("source", help="the app's Documents folder as copied off the phone")
    ap.add_argument("--db", default=None, help="Defaults to the profile database.")
    ap.add_argument("--tz", default=None, help="time zone where a sample carries none")
    ap.add_argument("--compare", metavar="DB", help="diff against a backup-built database")
    athlete_profile.add_argument(ap, required=False)
    args = ap.parse_args()
    profile = athlete_profile.from_args(args, required=False)
    db = args.db or athlete_profile.field(profile, "db")
    if not db:
        raise SystemExit("no database. Pass --db PATH or --profile NAME.")
    tz = args.tz or athlete_profile.field(profile, "timezone") or os.environ.get("TZ") or "UTC"

    dirs = batch_dirs(args.source)
    out = os.path.expanduser(db)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    t0 = time.time()
    print(f"reading {len(dirs)} batch(es), newest {dirs[-1].name}", file=sys.stderr)
    build(dirs, out, tz)
    size = os.path.getsize(out) / 1e6
    print(f"done in {time.time() - t0:.0f}s -> {out} ({size:,.0f} MB)", file=sys.stderr)
    if args.compare:
        compare(out, os.path.expanduser(args.compare), tz)


if __name__ == "__main__":
    main()
