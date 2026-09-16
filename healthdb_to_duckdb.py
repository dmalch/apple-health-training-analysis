#!/usr/bin/env python3
"""
Convert the iPhone's own Health database into the same DuckDB shape that
health_to_duckdb.py builds from export.xml.

Usage:
    ./.venv/bin/python healthdb_to_duckdb.py ~/HealthBackups/<udid> --db data/health-live.duckdb
    ./.venv/bin/python healthdb_to_duckdb.py /path/to/healthdb_secure.sqlite --db data/health-live.duckdb
    ./.venv/bin/python healthdb_to_duckdb.py <path> --inspect          # schema + type census, writes nothing
    ./.venv/bin/python healthdb_to_duckdb.py <path> --verify data/health.duckdb

Why this exists: "Export All Health Data" is a button inside Health.app. Apple
exposes no Shortcuts action and no API for it, so the XML route cannot be
automated -- somebody has to hold the phone. An encrypted local backup can be
automated end to end (idevicebackup2 / pymobiledevice3 on a timer), and it
carries healthdb_secure.sqlite, which is the raw store the XML is generated
from. Same samples, plus per-sample provenance the XML flattens.

The output tables and views are deliberately identical to health_to_duckdb.py
(VIEWS is imported from it, not copied), so analyze.py, analyze_intervals.py
and hq.py work against either database.

Needs the duckdb module (see .venv); the sqlite side is stdlib. Reading an
encrypted backup additionally needs `pip install iOSbackup`.

Schema caveat: Apple does not document healthdb_secure.sqlite and the layout
moves between iOS releases. Everything here is introspected first and missing
pieces are reported rather than assumed -- run --inspect before trusting a run
on a new iOS version, and --verify against an XML-derived database while both
sources still overlap.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import duckdb

import athlete_profile
from health_to_duckdb import VIEWS

# Core Data / HealthKit epoch is 2001-01-01 UTC.
APPLE_EPOCH_OFFSET = 978307200

# healthdb_secure.sqlite stores `samples.data_type` as an integer. The codes are
# undocumented but stable; these come from christophhagen's HealthDB, which
# reverse-engineered them against real databases. Verified on 2026-09-02 against
# a real 5.0M-sample database from iOS 26.6 -- every high-volume code resolved.
QUANTITY_TYPES = {
    0: "BodyMassIndex",
    1: "BodyFatPercentage",
    2: "Height",
    3: "BodyMass",
    4: "LeanBodyMass",
    5: "HeartRate",
    7: "StepCount",
    8: "DistanceWalkingRunning",
    9: "BasalEnergyBurned",
    10: "ActiveEnergyBurned",
    12: "FlightsClimbed",
    14: "OxygenSaturation",
    15: "BloodGlucose",
    16: "BloodPressureSystolic",
    17: "BloodPressureDiastolic",
    18: "BloodAlcoholContent",
    19: "PeripheralPerfusionIndex",
    20: "DietaryFatTotal",
    21: "DietaryFatPolyunsaturated",
    22: "DietaryFatMonounsaturated",
    23: "DietaryFatSaturated",
    24: "DietaryCholesterol",
    25: "DietarySodium",
    26: "DietaryCarbohydrates",
    27: "DietaryFiber",
    28: "DietarySugar",
    29: "DietaryEnergyConsumed",
    30: "DietaryProtein",
    31: "DietaryVitaminA",
    32: "DietaryVitaminB6",
    33: "DietaryVitaminB12",
    34: "DietaryVitaminC",
    35: "DietaryVitaminD",
    36: "DietaryVitaminE",
    37: "DietaryVitaminK",
    38: "DietaryCalcium",
    39: "DietaryIron",
    40: "DietaryThiamin",
    41: "DietaryRiboflavin",
    42: "DietaryNiacin",
    43: "DietaryFolate",
    44: "DietaryBiotin",
    45: "DietaryPantothenicAcid",
    46: "DietaryPhosphorus",
    47: "DietaryIodine",
    48: "DietaryMagnesium",
    49: "DietaryZinc",
    50: "DietarySelenium",
    51: "DietaryCopper",
    52: "DietaryManganese",
    53: "DietaryChromium",
    54: "DietaryMolybdenum",
    55: "DietaryChloride",
    56: "DietaryPotassium",
    57: "NumberOfTimesFallen",
    58: "ElectrodermalActivity",
    60: "InhalerUsage",
    61: "RespiratoryRate",
    62: "BodyTemperature",
    71: "ForcedVitalCapacity",
    72: "ForcedExpiratoryVolume1",
    73: "PeakExpiratoryFlowRate",
    75: "AppleExerciseTime",
    78: "DietaryCaffeine",
    83: "DistanceCycling",
    87: "DietaryWater",
    89: "UVExposure",
    90: "BasalBodyTemperature",
    101: "PushCount",
    110: "DistanceSwimming",
    111: "SwimmingStrokeCount",
    113: "DistanceWheelchair",
    114: "WaistCircumference",
    118: "RestingHeartRate",
    124: "VO2Max",
    125: "InsulinDelivery",
    137: "WalkingHeartRateAverage",
    138: "DistanceDownhillSnowSports",
    139: "HeartRateVariabilitySDNN",
    172: "EnvironmentalAudioExposure",
    173: "HeadphoneAudioExposure",
    182: "WalkingDoubleSupportPercentage",
    183: "SixMinuteWalkTestDistance",
    186: "AppleStandTime",
    187: "WalkingSpeed",
    188: "WalkingStepLength",
    194: "WalkingAsymmetryPercentage",
    195: "StairAscentSpeed",
    196: "StairDescentSpeed",
    248: "AtrialFibrillationBurden",
    249: "AppleWalkingSteadiness",
    251: "NumberOfAlcoholicBeverages",
    258: "RunningStrideLength",
    259: "RunningVerticalOscillation",
    260: "RunningGroundContactTime",
    266: "HeartRateRecoveryOneMinute",
    269: "UnderwaterDepth",
    270: "RunningPower",
    272: "EnvironmentalSoundReduction",
    274: "RunningSpeed",
    277: "WaterTemperature",
    279: "TimeInDaylight",
    280: "CyclingPower",
    281: "CyclingSpeed",
    282: "CyclingCadence",
    283: "CyclingFunctionalThresholdPower",
    286: "PhysicalEffort",
    # Identified here, not in HealthDB: 458 samples spanning 35.7-37.1 degC,
    # which is the sleeping skin-temperature range this project already knew.
    256: "AppleSleepingWristTemperature",
}

CATEGORY_TYPES = {
    63: "SleepAnalysis",
    70: "AppleStandHour",
    91: "CervicalMucusQuality",
    92: "OvulationTestResult",
    95: "MenstrualFlow",
    96: "IntermenstrualBleeding",
    97: "SexualActivity",
    99: "MindfulSession",
    147: "LowHeartRateEvent",
    157: "AbdominalCramps",
    158: "BreastPain",
    159: "Bloating",
    160: "Headache",
    161: "Acne",
    162: "LowerBackPain",
    163: "PelvicPain",
    164: "MoodChanges",
    165: "Constipation",
    166: "Diarrhea",
    167: "Fatigue",
    168: "Nausea",
    169: "SleepChanges",
    170: "AppetiteChanges",
    171: "HotFlashes",
    178: "EnvironmentalAudioExposureEvent",
    189: "ToothbrushingEvent",
    191: "Pregnancy",
    192: "Lactation",
    193: "Contraceptive",
    201: "RapidPoundingOrFlutteringHeartbeat",
    202: "SkippedHeartbeat",
    203: "Fever",
    204: "ShortnessOfBreath",
    205: "ChestTightnessOrPain",
    206: "Fainting",
    207: "Dizziness",
    220: "Vomiting",
    221: "Heartburn",
    222: "Coughing",
    223: "Wheezing",
    224: "SoreThroat",
    225: "SinusCongestion",
    226: "RunnyNose",
    229: "VaginalDryness",
    230: "NightSweats",
    231: "Chills",
    232: "HairLoss",
    233: "DrySkin",
    234: "BladderIncontinence",
    235: "MemoryLapse",
    237: "HandwashingEvent",
    240: "GeneralizedBodyAche",
    241: "LossOfSmell",
    242: "LossOfTaste",
    243: "PregnancyTestResult",
    244: "ProgesteroneTestResult",
    262: "IrregularMenstrualCycles",
    263: "ProlongedMenstrualPeriods",
    264: "PersistentIntermenstrualBleeding",
}

# Rows that are not quantity or category samples at all. Identified by which
# side table their data_ids live in: 79 joins `workouts`, 76 `activity_caches`,
# 102 `data_series` (the GPS series), 119 `binary_samples` (beat-to-beat
# heartbeat series -- 21,950 of them, 1:1 with the HRV samples, and absent from
# export.xml entirely), 144 `ecg_samples`.
OTHER_TYPES = {
    76: ("ActivitySummary", "HKActivitySummaryType"),
    79: ("Workout", "HKWorkoutTypeIdentifier"),
    102: ("WorkoutRoute", "HKSeriesTypeIdentifierWorkoutRoute"),
    119: ("HeartbeatSeries", "HKDataTypeIdentifierHeartbeatSeries"),
    144: ("Electrocardiogram", "HKDataTypeIdentifierElectrocardiogram"),
}

# healthdb_secure stores quantities in HealthKit's canonical SI units, while
# export.xml carries the display units. Heart rate is the one that matters: the
# database says 2.7 (count/s), the XML says 162 bpm. Every factor below was
# measured by comparing per-type averages against the XML database over the same
# window, which is what --verify prints; types not listed came out identical.
# The dietary family is likely g->mg the way DietaryCholesterol is, but only
# cholesterol had rows here, so the rest is left alone rather than guessed.
UNIT_SCALE = {
    "HeartRate": (60.0, "count/min"),
    "RespiratoryRate": (60.0, "count/min"),
    "WalkingSpeed": (3.6, "km/hr"),
    "RunningSpeed": (3.6, "km/hr"),
    "WalkingStepLength": (100.0, "cm"),
    "Height": (100.0, "cm"),
    "DistanceWalkingRunning": (0.001, "km"),
    "DistanceCycling": (0.001, "km"),
    "DistanceDownhillSnowSports": (0.001, "km"),
    "DietaryCholesterol": (1000.0, "mg"),
    # DistanceSwimming is NOT metres despite the family resemblance -- assuming
    # so made it 1000x too small, which --verify caught. It is stored in km.
}

# HKWorkoutActivityType, the public API constants. `workouts` stores the number;
# the XML pipeline stores the name, so the two databases only line up after this
# lookup. Checked against the XML database by joining on start_date.
ACTIVITY_TYPES = {
    1: "AmericanFootball",
    2: "Archery",
    3: "AustralianFootball",
    4: "Badminton",
    5: "Baseball",
    6: "Basketball",
    7: "Bowling",
    8: "Boxing",
    9: "Climbing",
    10: "Cricket",
    11: "CrossTraining",
    12: "Curling",
    13: "Cycling",
    14: "Dance",
    15: "DanceInspiredTraining",
    16: "Elliptical",
    17: "EquestrianSports",
    18: "Fencing",
    19: "Fishing",
    20: "FunctionalStrengthTraining",
    21: "Golf",
    22: "Gymnastics",
    23: "Handball",
    24: "Hiking",
    25: "Hockey",
    26: "Hunting",
    27: "Lacrosse",
    28: "MartialArts",
    29: "MindAndBody",
    30: "MixedMetabolicCardioTraining",
    31: "PaddleSports",
    32: "Play",
    33: "PreparationAndRecovery",
    34: "Racquetball",
    35: "Rowing",
    36: "Rugby",
    37: "Running",
    38: "Sailing",
    39: "SkatingSports",
    40: "SnowSports",
    41: "Soccer",
    42: "Softball",
    43: "Squash",
    44: "StairClimbing",
    45: "SurfingSports",
    46: "Swimming",
    47: "TableTennis",
    48: "Tennis",
    49: "TrackAndField",
    50: "TraditionalStrengthTraining",
    51: "Volleyball",
    52: "Walking",
    53: "WaterFitness",
    54: "WaterPolo",
    55: "WaterSports",
    56: "Wrestling",
    57: "Yoga",
    58: "Barre",
    59: "CoreTraining",
    60: "CrossCountrySkiing",
    61: "DownhillSkiing",
    62: "Flexibility",
    63: "HighIntensityIntervalTraining",
    64: "JumpRope",
    65: "Kickboxing",
    66: "Pilates",
    67: "Snowboarding",
    68: "Stairs",
    69: "StepTraining",
    70: "WheelchairWalkPace",
    71: "WheelchairRunPace",
    72: "TaiChi",
    73: "MixedCardio",
    74: "HandCycling",
    75: "DiscSports",
    76: "FitnessGaming",
    77: "CardioDance",
    78: "SocialDance",
    79: "Pickleball",
    80: "Cooldown",
    82: "SwimBikeRun",
    83: "Transition",
    84: "UnderwaterDiving",
    3000: "Other",
}

# HKWorkoutEventType, for the events table.
WORKOUT_EVENT_TYPES = {
    1: "Pause",
    2: "Resume",
    3: "Lap",
    4: "Marker",
    5: "MotionPaused",
    6: "MotionResumed",
    7: "Segment",
    8: "PauseOrResumeRequest",
}

# Still unresolved on that database, all low volume except the first: 77 (62,971
# rows, in no side table), 116 (6,368 category rows valued 0/1), 273, 275, 285,
# 296, 298, 302, 304 (iOS 18+ quantity types HealthDB predates). They surface as
# `type_<n>` rather than being dropped.


# ------------------------------------------------------------------- locating

HEALTH_DB_NAMES = ("healthdb_secure.sqlite", "healthdb.sqlite")


def looks_like_backup(path):
    return os.path.exists(os.path.join(path, "Manifest.db"))


def extract_from_backup(backup_dir, out_dir, password):
    """Pull the Health databases out of an encrypted iOS backup."""
    try:
        from iOSbackup import iOSbackup  # pyright: ignore[reportMissingImports]
    except ImportError:
        raise SystemExit(  # noqa: B904 -- the message replaces the cause
            "That looks like an iOS backup, which is encrypted. Either let\n"
            "pymobiledevice3 hand over the file already decrypted:\n"
            "    pymobiledevice3 backup2 extract HomeDomain Health/healthdb_secure.sqlite \\\n"
            '        <backup_root> -p "$IOS_BACKUP_PASSWORD"\n'
            "and point this script at the result, or install the reader:\n"
            "    ./.venv/bin/pip install iOSbackup\n"
            "and pass the backup password via --password-env."
        )
    if not password:
        raise SystemExit(
            "Encrypted backup needs the password. Put it in an env var and pass\n"
            "--password-env NAME (do not put it on the command line -- it lands in\n"
            "the shell history)."
        )

    udid = os.path.basename(backup_dir.rstrip("/"))
    root = os.path.dirname(backup_dir.rstrip("/"))
    b = iOSbackup(udid=udid, cleartextpassword=password, backuproot=root)

    os.makedirs(out_dir, exist_ok=True)
    found = {}
    for name in HEALTH_DB_NAMES:
        try:
            info = b.getFileDecryptedCopy(
                relativePath=f"Health/{name}",
                targetName=name,
                targetFolder=out_dir,
            )
        except Exception as exc:
            print(f"  {name}: not recovered ({exc})", file=sys.stderr)
            continue
        found[name] = info["decryptedFilePath"]
        print(f"  {name}: {found[name]}", file=sys.stderr)

    if "healthdb_secure.sqlite" not in found:
        raise SystemExit(
            "healthdb_secure.sqlite is not in this backup. Health data is only\n"
            "included when the backup is ENCRYPTED -- tick 'Encrypt local backup'\n"
            "in Finder (or pass the flag to idevicebackup2) and take a fresh one."
        )
    return found["healthdb_secure.sqlite"]


def resolve_source(path, work_dir, password):
    """Return a path to a readable healthdb_secure.sqlite."""
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        direct = os.path.join(path, "healthdb_secure.sqlite")
        if os.path.exists(direct):
            return direct
        if looks_like_backup(path):
            print(f"reading iOS backup {path}", file=sys.stderr)
            return extract_from_backup(path, work_dir, password)
    raise SystemExit(f"No healthdb_secure.sqlite and no iOS backup at {path}")


# --------------------------------------------------------------- introspection


def sqlite_open(path):
    # mode=ro rather than immutable=1: a database pulled out of a backup can
    # still have -wal/-shm siblings, and immutable would silently hide whatever
    # is only in the WAL.
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_columns(con):
    cols = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cols[name] = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
    return cols


def pick(columns, *candidates):
    """First candidate column that actually exists, else None."""
    for c in candidates:
        if c in columns:
            return c
    return None


def inspect(db_path):
    con = sqlite_open(db_path)
    cols = table_columns(con)
    print(f"{db_path}\n{len(cols)} tables\n")
    for name in sorted(cols):
        try:
            n = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        except sqlite3.DatabaseError:
            n = -1
        print(f"  {name:<28} {n:>12,}  {', '.join(cols[name])[:110]}")

    if "samples" in cols:
        print("\ndata_type census (unmapped codes marked ??):")
        rows = con.execute("""
            SELECT data_type, count(*) n, min(start_date), max(start_date)
            FROM samples GROUP BY data_type ORDER BY n DESC LIMIT 40
        """).fetchall()
        for dt, n, lo, hi in rows:
            name = type_name(dt)[0] or "??"
            span = ""
            if lo is not None:
                span = f"  {apple_ts_str(lo)} .. {apple_ts_str(hi)}"
            print(f"  {dt:>5}  {n:>10,}  {name:<32}{span}")
    con.close()


def apple_ts_str(seconds):
    import datetime

    return (
        datetime.datetime(2001, 1, 1, tzinfo=datetime.UTC)
        + datetime.timedelta(seconds=float(seconds))
    ).strftime("%Y-%m-%d")


# ------------------------------------------------------------------- transfer


def small_table(con_sq, con_db, name, target, extra_rowid=True):
    """Copy a small lookup table into DuckDB, materialising ROWID.

    The sqlite scanner does not expose implicit ROWIDs, and half of this schema
    joins on them (objects.provenance -> data_provenances.ROWID,
    metadata_values.key_id -> metadata_keys.ROWID), so those tables come across
    through Python instead of the scanner.
    """
    # Some of these tables (data_provenances, metadata_keys on iOS 26) declare
    # ROWID as a real column; aliasing it in again would collide.
    declared = [r[1] for r in con_sq.execute(f'PRAGMA table_info("{name}")')]
    add_rowid = extra_rowid and not any(c.lower() == "rowid" for c in declared)
    cur = con_sq.execute(f'SELECT {"ROWID AS rowid, " if add_rowid else ""}* FROM "{name}"')
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    decl = ", ".join(f'"{c}" VARCHAR' for c in columns)
    con_db.execute(f"DROP TABLE IF EXISTS {target}")
    con_db.execute(f"CREATE TABLE {target} ({decl})")
    if not rows:
        return 0
    con_db.executemany(
        f"INSERT INTO {target} VALUES ({', '.join('?' * len(columns))})",
        [[None if v is None else str(v) for v in r] for r in rows],
    )
    return len(rows)


def load_companion(con_db, healthdb_path):
    """Copy the two name lookups out of healthdb.sqlite, if it sits next door.

    healthdb_secure only stores numeric ids. The readable identities live in the
    unencrypted companion database: `sources` names the writing app or device
    ("<owner>'s Apple Watch", "Bluetooth Device") and `source_devices` names the
    actual hardware, which is what tells two straps of the same model apart -- the
    rows read "Polar H10 <serial>", carrying the same serial the XML's HKDevice
    string does.

    Returns {"sources": name_column | None, "devices": name_column | None}.
    """
    found = {"sources": None, "devices": None}
    if not os.path.exists(healthdb_path):
        return found
    try:
        sq = sqlite_open(healthdb_path)
        cols = table_columns(sq)
    except sqlite3.DatabaseError:
        return found
    try:
        for target, candidates in (
            ("sources", ("sources", "source")),
            ("devices", ("source_devices", "devices")),
        ):
            for table in candidates:
                if table not in cols:
                    continue
                name_col = pick(cols[table], "name", "source_name", "bundle_id")
                if not name_col:
                    continue
                n = small_table(sq, con_db, table, target)
                found[target] = name_col
                print(f"  {target:<20} {n:>12,} rows (healthdb.sqlite)", file=sys.stderr)
                break
    finally:
        sq.close()
    return found


def type_name(code):
    """(short name, HK identifier) for a data_type code, or (None, None)."""
    if code in QUANTITY_TYPES:
        return QUANTITY_TYPES[code], "HKQuantityTypeIdentifier" + QUANTITY_TYPES[code]
    if code in CATEGORY_TYPES:
        return CATEGORY_TYPES[code], "HKCategoryTypeIdentifier" + CATEGORY_TYPES[code]
    if code in OTHER_TYPES:
        return OTHER_TYPES[code]
    return None, None


def type_map_table(con_db):
    con_db.execute("DROP TABLE IF EXISTS hk_types")
    con_db.execute("CREATE TABLE hk_types (code BIGINT, name VARCHAR, full_name VARCHAR)")
    codes = set(QUANTITY_TYPES) | set(CATEGORY_TYPES) | set(OTHER_TYPES)
    con_db.executemany(
        "INSERT INTO hk_types VALUES (?, ?, ?)", [(c, *type_name(c)) for c in sorted(codes)]
    )

    con_db.execute("DROP TABLE IF EXISTS hk_scale")
    con_db.execute("CREATE TABLE hk_scale (name VARCHAR, factor DOUBLE, unit VARCHAR)")
    con_db.executemany(
        "INSERT INTO hk_scale VALUES (?, ?, ?)",
        [(k, f, u) for k, (f, u) in sorted(UNIT_SCALE.items())],
    )

    for table, mapping in (("hk_activities", ACTIVITY_TYPES), ("hk_events", WORKOUT_EVENT_TYPES)):
        con_db.execute(f"DROP TABLE IF EXISTS {table}")
        con_db.execute(f"CREATE TABLE {table} (code BIGINT, name VARCHAR)")
        con_db.executemany(f"INSERT INTO {table} VALUES (?, ?)", sorted(mapping.items()))


def build(db_path, out_db, tz_default):
    con_sq = sqlite_open(db_path)
    cols = table_columns(con_sq)

    required = ["samples", "objects"]
    missing = [t for t in required if t not in cols]
    if missing:
        raise SystemExit(f"{db_path} has no {missing} -- run --inspect, the schema moved")

    if os.path.exists(out_db):
        os.remove(out_db)
    con = duckdb.connect(out_db)
    con.execute("INSTALL sqlite; LOAD sqlite")
    con.execute(f"ATTACH '{db_path}' AS hk (TYPE sqlite, READ_ONLY)")

    type_map_table(con)

    # Provenance carries the honest device answer: origin_product_type is a
    # product code such as 'Watch7,1' or 'iPhone16,1', and the paired strap shows
    # up as its own row.
    prov_cols = cols.get("data_provenances", [])
    if prov_cols:
        n = small_table(con_sq, con, "data_provenances", "provenances")
    else:
        # Keep the joins valid on a schema that names provenance differently;
        # device_name simply comes out NULL and --inspect says why.
        con.execute("CREATE TABLE provenances (rowid VARCHAR)")
        n = 0
        print("  data_provenances is absent -- device/source will be NULL", file=sys.stderr)
    print(f"  provenances          {n:>12,} rows", file=sys.stderr)

    prod_col = pick(prov_cols, "origin_product_type", "local_product_type")
    dev_col = pick(prov_cols, "device_id")
    src_col = pick(prov_cols, "source_id", "source_bundle_id", "sync_provenance")
    tz_col = pick(prov_cols, "tz_name", "timezone_name")

    companion = load_companion(con, os.path.join(os.path.dirname(db_path), "healthdb.sqlite"))

    # device_name must mean what it means in the XML pipeline -- the hardware's
    # own name ('Apple Watch', 'Polar H10 <serial>') -- because the `hr` view
    # decides strap vs watch from it. origin_product_type would name the watch
    # for a strap session, since the watch owns the sample, and every strap
    # reading would come out classified as the watch. Product type is kept
    # alongside as origin_product; it is something the XML does not carry.
    prod_expr = f'p."{prod_col}"' if prod_col else "NULL"
    if dev_col and companion["devices"]:
        dev_expr = f'coalesce(dv."{companion["devices"]}", {prod_expr})'
    else:
        dev_expr = prod_expr
    dev_lookup = (
        f'LEFT JOIN devices dv ON CAST(dv.rowid AS BIGINT) = CAST(p."{dev_col}" AS BIGINT)'
        if dev_col and companion["devices"]
        else ""
    )

    if src_col and companion["sources"]:
        src_expr = f'coalesce(sr."{companion["sources"]}", p."{src_col}")'
    elif src_col:
        src_expr = f'p."{src_col}"'
    else:
        src_expr = "NULL"
    src_lookup = (
        f'LEFT JOIN sources sr ON CAST(sr.rowid AS BIGINT) = CAST(p."{src_col}" AS BIGINT)'
        if src_col and companion["sources"]
        else ""
    )
    tz_expr = (
        f"coalesce(nullif(p.\"{tz_col}\", ''), '{tz_default}')" if tz_col else f"'{tz_default}'"
    )

    has_quantity = "quantity_samples" in cols
    has_category = "category_samples" in cols
    qty = "LEFT JOIN hk.quantity_samples q ON q.data_id = s.data_id" if has_quantity else ""
    cat = "LEFT JOIN hk.category_samples c ON c.data_id = s.data_id" if has_category else ""
    # The workout object is itself a row in `samples`; the XML pipeline keeps
    # workouts out of `records`, so drop them here too rather than leaving a
    # data_type 0 row per session in the census.
    workout_filter = (
        "WHERE s.data_id NOT IN (SELECT data_id FROM hk.workouts)" if "workouts" in cols else ""
    )
    qty_val = "q.quantity" if has_quantity else "NULL"
    qty_unit = (
        "q.original_unit"
        if has_quantity and "original_unit" in cols.get("quantity_samples", [])
        else "NULL"
    )
    cat_val = "c.value" if has_category else "NULL"

    t0 = time.time()
    con.execute(f"""
        CREATE TABLE records AS
        WITH raw AS (
            SELECT
                s.data_id                                   AS id,
                s.data_type                                 AS type_code,
                to_timestamp(s.start_date + {APPLE_EPOCH_OFFSET})  AS start_date,
                to_timestamp(s.end_date   + {APPLE_EPOCH_OFFSET})  AS end_date,
                to_timestamp(o.creation_date + {APPLE_EPOCH_OFFSET}) AS creation_date,
                CAST({qty_val} AS DOUBLE)                   AS value,
                CAST({cat_val} AS VARCHAR)                  AS value_text,
                CAST({qty_unit} AS VARCHAR)                 AS unit,
                CAST({dev_expr} AS VARCHAR)                 AS device_name,
                CAST({prod_expr} AS VARCHAR)                AS origin_product,
                CAST({src_expr} AS VARCHAR)                 AS source_name,
                {tz_expr}                                   AS tz
            FROM hk.samples s
            JOIN hk.objects o ON o.data_id = s.data_id
            LEFT JOIN provenances p ON CAST(p.rowid AS BIGINT) = o.provenance
            {dev_lookup}
            {src_lookup}
            {qty}
            {cat}
            {workout_filter}
        )
        SELECT
            id,
            coalesce(t.name, 'type_' || CAST(type_code AS VARCHAR))      AS type,
            coalesce(t.full_name, 'type_' || CAST(type_code AS VARCHAR)) AS type_full,
            type_code,
            source_name,
            NULL                                            AS source_version,
            device_name,
            origin_product                                  AS device_raw,
            origin_product,
            coalesce(sc.unit, raw.unit)                     AS unit,
            creation_date, start_date, end_date,
            CAST(timezone(tz, start_date) AS DATE)          AS local_date,
            CAST(timezone(tz, start_date) AS TIME)          AS local_time,
            value * coalesce(sc.factor, 1.0)                AS value,
            nullif(value_text, '')                          AS value_text
        FROM raw
        LEFT JOIN hk_types t ON t.code = raw.type_code
        LEFT JOIN hk_scale sc ON sc.name = t.name
    """)
    n = con.execute("SELECT count(*) FROM records").fetchone()[0]
    print(f"  records              {n:>12,} rows  ({time.time() - t0:.1f}s)", file=sys.stderr)

    build_workouts(con, cols, tz_default, dev_expr, dev_lookup, src_expr, src_lookup)
    build_blocks(con, cols)
    build_route(con, cols)
    build_metadata(con, con_sq, cols)
    stub_missing(con)

    con.execute(VIEWS)
    con.close()
    con_sq.close()
    return out_db


def build_workouts(con, cols, tz_default, dev_expr, dev_lookup, src_expr, src_lookup):
    """One row per workout, in the column shape health_to_duckdb.py produces.

    Two layouts exist. Up to iOS 17 the `workouts` row itself carried activity
    type, duration and energy. On iOS 18+ (checked on 26.6) `workouts` keeps only
    distance and goal, while activity type and duration moved to
    `workout_activities` -- one row per activity, `is_primary_activity` marking
    the main one -- and the totals to `workout_statistics`. Both are handled.
    """
    if "workouts" not in cols:
        print("  workouts             (absent from this schema)", file=sys.stderr)
        con.execute(EMPTY_WORKOUTS)
        return

    w = cols["workouts"]
    dist = pick(w, "total_distance")
    dur = pick(w, "duration")
    kcal = pick(w, "total_energy_burned")
    act = pick(w, "activity_type", "workout_type")

    activities = "workout_activities" in cols
    if activities and not act:
        act_expr, dur_expr = "a.activity_type", "a.duration / 60.0"
        act_join = """LEFT JOIN (
                SELECT owner_id, max(activity_type) AS activity_type,
                       sum(duration) AS duration, max(location_type) AS location_type
                FROM hk.workout_activities
                WHERE is_primary_activity = 1 GROUP BY owner_id) a ON a.owner_id = w.data_id"""
        indoor_expr = "a.location_type = 1"
    else:
        act_expr = f'w."{act}"' if act else "NULL"
        dur_expr = f'w."{dur}" / 60.0' if dur else "NULL"
        act_join, indoor_expr = "", "NULL"

    con.execute(f"""
        CREATE TABLE workouts AS
        WITH raw AS (
            SELECT
                w.data_id                                   AS id,
                CAST({act_expr} AS BIGINT)                  AS activity_code,
                CAST({dur_expr} AS DOUBLE)                  AS duration_min,
                CAST({f'w."{dist}"' if dist else "NULL"} AS DOUBLE) AS distance_km,
                CAST({f'w."{kcal}"' if kcal else "NULL"} AS DOUBLE) AS active_kcal,
                CAST({indoor_expr} AS BOOLEAN)              AS indoor,
                to_timestamp(s.start_date + {APPLE_EPOCH_OFFSET}) AS start_date,
                to_timestamp(s.end_date   + {APPLE_EPOCH_OFFSET}) AS end_date,
                to_timestamp(o.creation_date + {APPLE_EPOCH_OFFSET}) AS creation_date,
                CAST({dev_expr} AS VARCHAR)                 AS device_name,
                CAST({src_expr} AS VARCHAR)                 AS source_name
            FROM hk.workouts w
            JOIN hk.samples s ON s.data_id = w.data_id
            JOIN hk.objects o ON o.data_id = w.data_id
            LEFT JOIN provenances p ON CAST(p.rowid AS BIGINT) = o.provenance
            {dev_lookup}
            {src_lookup}
            {act_join}
        )
        SELECT
            id,
            coalesce(act.name, 'activity_' || CAST(activity_code AS VARCHAR)) AS activity,
            duration_min,
            source_name,
            NULL                                            AS source_version,
            device_name,
            device_name                                     AS device_raw,
            creation_date, start_date, end_date,
            CAST(timezone('{tz_default}', start_date) AS DATE) AS local_date,
            CAST(timezone('{tz_default}', start_date) AS TIME) AS local_time,
            distance_km,
            active_kcal,
            NULL                                            AS basal_kcal,
            NULL                                            AS steps,
            NULL                                            AS avg_hr,
            NULL                                            AS max_hr,
            NULL                                            AS min_hr,
            indoor,
            NULL                                            AS route_file
        FROM raw LEFT JOIN hk_activities act ON act.code = raw.activity_code
    """)

    build_workout_statistics(con, cols)

    # avg/max/min HR: from workout_statistics where the phone kept them, and
    # recomputed from the samples in the window where it did not.
    con.execute("""
        CREATE OR REPLACE TABLE workouts AS
        SELECT w.* REPLACE (
            coalesce(st.avg_hr, h.avg_hr) AS avg_hr,
            coalesce(st.max_hr, h.max_hr) AS max_hr,
            coalesce(st.min_hr, h.min_hr) AS min_hr,
            coalesce(w.active_kcal, st.active_kcal) AS active_kcal)
        FROM workouts w
        LEFT JOIN (
            SELECT w.id,
                   round(avg(r.value), 1) AS avg_hr,
                   max(r.value)           AS max_hr,
                   min(r.value)           AS min_hr
            FROM workouts w
            JOIN records r ON r.type = 'HeartRate'
                          AND r.start_date BETWEEN w.start_date AND w.end_date
            GROUP BY w.id
        ) h USING (id)
        LEFT JOIN (
            -- A workout can hold several activities, each with its own stats
            -- row, so the workout-level average has to be weighted by how long
            -- each activity ran; taking the max instead reads ~20 bpm high on
            -- interval sessions.
            SELECT workout_id AS id,
                   round(sum(average * dur) FILTER (type = 'HeartRate')
                         / nullif(sum(dur) FILTER (type = 'HeartRate' AND average IS NOT NULL), 0),
                         3)                                 AS avg_hr,
                   max(maximum) FILTER (type = 'HeartRate')  AS max_hr,
                   min(minimum) FILTER (type = 'HeartRate')  AS min_hr,
                   sum(sum)     FILTER (type = 'ActiveEnergyBurned') AS active_kcal
            FROM (SELECT *, greatest(epoch(end_date) - epoch(start_date), 1) AS dur
                  FROM workout_statistics)
            GROUP BY 1
        ) st USING (id)
    """)
    n = con.execute("SELECT count(*) FROM workouts").fetchone()[0]
    print(f"  workouts             {n:>12,} rows", file=sys.stderr)


WORKOUT_ROUTE_TYPE = 102  # HKWorkoutRouteTypeIdentifier


def build_blocks(con, cols):
    """The blocks a structured workout was actually built from.

    Not to be confused with `workout_events` type 7 (`Segment`), which is
    Apple's automatic segmentation: overlapping spans that tile the session end
    to end and say nothing about how it was run. A custom workout's warm-up,
    work and recovery blocks live in `workout_activities`, one row each, with
    `is_primary_activity` marking the rows that describe the session as a whole.
    An ordinary unstructured workout has only those primary rows, which is how
    the two are told apart.
    """
    if "workout_activities" not in cols:
        con.execute("""
            CREATE TABLE workout_blocks (
                workout_id BIGINT, seq BIGINT, is_primary BOOLEAN,
                start_date TIMESTAMPTZ, end_date TIMESTAMPTZ, duration_min DOUBLE)""")
        return
    con.execute(f"""
        CREATE OR REPLACE TABLE workout_blocks AS
        SELECT owner_id                                       AS workout_id,
               row_number() OVER (PARTITION BY owner_id, is_primary_activity
                                  ORDER BY start_date)        AS seq,
               is_primary_activity = 1                        AS is_primary,
               to_timestamp(start_date + {APPLE_EPOCH_OFFSET}) AS start_date,
               to_timestamp(end_date   + {APPLE_EPOCH_OFFSET}) AS end_date,
               CAST(duration AS DOUBLE) / 60.0                AS duration_min
        FROM hk.workout_activities
        ORDER BY owner_id, start_date
    """)
    n = con.execute("SELECT count(*) FROM workout_blocks WHERE NOT is_primary").fetchone()[0]
    print(f"  workout_blocks       {n:>12,} rows", file=sys.stderr)


def build_route(con, cols):
    """GPS tracks, which are plain numbers here rather than a blob to decode.

    Two joins have to be right and neither is the obvious one:

    * `location_series_data.series_identifier` matches `data_series.hfd_key`,
      **not** `data_series.data_id`. The two are different numbers, and a
      data_id join silently returns another workout's points.
    * the route is tied to its workout through `associations`, where the
      workout is the *destination* and the type-102 route sample the *source*.
    """
    need = ("data_series", "location_series_data", "associations", "samples")
    if any(name not in cols for name in need):
        return
    con.execute(f"""
        CREATE OR REPLACE TABLE route_points AS
        SELECT 'series_' || CAST(ds.hfd_key AS VARCHAR)        AS route_file,
               to_timestamp(l.timestamp + {APPLE_EPOCH_OFFSET}) AS t,
               CAST(l.latitude  AS DOUBLE)                     AS lat,
               CAST(l.longitude AS DOUBLE)                     AS lon,
               CAST(l.altitude  AS DOUBLE)                     AS ele,
               CAST(l.speed     AS DOUBLE)                     AS speed_ms,
               CAST(l.course    AS DOUBLE)                     AS course,
               CAST(l.horizontal_accuracy AS DOUBLE)           AS hacc,
               CAST(l.vertical_accuracy   AS DOUBLE)           AS vacc
        FROM hk.location_series_data l
        JOIN hk.data_series ds ON ds.hfd_key = l.series_identifier
        ORDER BY route_file, t
    """)
    n = con.execute("SELECT count(*) FROM route_points").fetchone()[0]
    print(f"  route_points         {n:>12,} rows", file=sys.stderr)

    # A route that was recorded and then deleted keeps its association row with
    # deleted = 1. Joining it in gives the workout a second route -- and, because
    # this is a join, a second copy of the workout itself.
    deleted_filter = "WHERE a.deleted = 0" if pick(cols.get("associations", []), "deleted") else ""
    con.execute(f"""
        CREATE OR REPLACE TABLE workouts AS
        SELECT w.* REPLACE (r.route_file AS route_file)
        FROM workouts w
        LEFT JOIN (
            -- One route per workout. A re-synced run leaves two live routes on
            -- the same workout, and without this the join returns the workout
            -- twice. Richest track wins, lowest key breaks the tie.
            SELECT id, route_file FROM (
                SELECT a.destination_object_id                 AS id,
                       'series_' || CAST(ds.hfd_key AS VARCHAR) AS route_file,
                       row_number() OVER (
                           PARTITION BY a.destination_object_id
                           ORDER BY ds."count" DESC, ds.hfd_key) AS pick
                FROM hk.associations a
                JOIN hk.samples s ON s.data_id = a.source_object_id
                                 AND s.data_type = {WORKOUT_ROUTE_TYPE}
                JOIN hk.data_series ds ON ds.data_id = s.data_id
                {deleted_filter}
            ) WHERE pick = 1
        ) r USING (id)
    """)


def build_workout_statistics(con, cols):
    """Per-workout totals and the lap/segment events, where the schema has them.

    On iOS 18+ `workout_statistics` hangs off `workout_activities`, not off the
    workout, so it needs the extra hop to land on a workout id.
    """
    if "workout_statistics" not in cols or "workout_activities" not in cols:
        return
    con.execute(f"""
        CREATE OR REPLACE TABLE workout_statistics AS
        SELECT
            a.owner_id                                   AS workout_id,
            coalesce(t.name, 'type_' || CAST(s.data_type AS VARCHAR)) AS type,
            to_timestamp(a.start_date + {APPLE_EPOCH_OFFSET}) AS start_date,
            to_timestamp(a.end_date + {APPLE_EPOCH_OFFSET})   AS end_date,
            -- Same canonical-unit problem as the samples: HR arrives in count/s.
            CAST(s.quantity AS DOUBLE) * coalesce(sc.factor, 1.0) AS average,
            CAST(s."min" AS DOUBLE) * coalesce(sc.factor, 1.0)    AS minimum,
            CAST(s."max" AS DOUBLE) * coalesce(sc.factor, 1.0)    AS maximum,
            CAST(s.quantity AS DOUBLE) * coalesce(sc.factor, 1.0) AS sum,
            sc.unit                                      AS unit
        FROM hk.workout_statistics s
        JOIN hk.workout_activities a ON a.ROWID = s.workout_activity_id
        LEFT JOIN hk_types t ON t.code = s.data_type
        LEFT JOIN hk_scale sc ON sc.name = t.name
    """)
    n = con.execute("SELECT count(*) FROM workout_statistics").fetchone()[0]
    print(f"  workout_statistics   {n:>12,} rows", file=sys.stderr)

    if "workout_events" in cols:
        con.execute(f"""
            CREATE OR REPLACE TABLE workout_events AS
            SELECT e.owner_id                            AS workout_id,
                   coalesce(ev.name, 'type_' || CAST(e.type AS VARCHAR)) AS type,
                   to_timestamp(e.date + {APPLE_EPOCH_OFFSET}) AS date,
                   CAST(e.duration AS DOUBLE) / 60.0     AS duration_min
            FROM hk.workout_events e
            LEFT JOIN hk_events ev ON ev.code = e.type
        """)
        n = con.execute("SELECT count(*) FROM workout_events").fetchone()[0]
        print(f"  workout_events       {n:>12,} rows", file=sys.stderr)


def build_metadata(con, con_sq, cols):
    if "metadata_values" not in cols or "metadata_keys" not in cols:
        con.execute("CREATE TABLE record_metadata (record_id BIGINT, key VARCHAR, value VARCHAR)")
        con.execute("CREATE TABLE workout_metadata (workout_id BIGINT, key VARCHAR, value VARCHAR)")
        return

    small_table(con_sq, con, "metadata_keys", "meta_keys")

    # `value_type` says which column is the real value, and picking the first
    # non-null instead gets it wrong for quantities: those set value_type 3 and
    # put the magnitude in numerical_value with the *unit* in string_value, so a
    # coalesce returns 'cm' where the elevation should be. Apple's XML writes
    # those as "1163 cm", one string, which is what this reproduces.
    if "value_type" in cols["metadata_values"]:
        val_expr = f"""
            CASE m.value_type
                WHEN 0 THEN m.string_value
                WHEN 1 THEN CAST(m.numerical_value AS VARCHAR)
                WHEN 2 THEN CAST(to_timestamp(m.date_value + {APPLE_EPOCH_OFFSET}) AS VARCHAR)
                WHEN 3 THEN CAST(m.numerical_value AS VARCHAR) || ' ' || m.string_value
                ELSE NULL      -- 4: binary payload, not worth stringifying
            END"""
    else:

        def value_expr(col):
            if col.startswith("date_value"):
                return f'CAST(m."{col}" + {APPLE_EPOCH_OFFSET} AS VARCHAR)'
            return f'CAST(m."{col}" AS VARCHAR)'

        value_cols = [
            c
            for c in cols["metadata_values"]
            if c.startswith(("string_value", "numerical_value", "date_value"))
        ]
        val_expr = (
            f"coalesce({', '.join(value_expr(c) for c in value_cols)})" if value_cols else "NULL"
        )

    con.execute(f"""
        CREATE TABLE record_metadata AS
        SELECT m.object_id AS record_id,
               k."key"     AS key,
               CAST({val_expr} AS VARCHAR) AS value
        FROM hk.metadata_values m
        JOIN meta_keys k ON CAST(k.rowid AS BIGINT) = m.key_id
    """)
    con.execute("""
        CREATE TABLE workout_metadata AS
        SELECT record_id AS workout_id, key, value FROM record_metadata
        WHERE record_id IN (SELECT id FROM workouts)
    """)
    n = con.execute("SELECT count(*) FROM record_metadata").fetchone()[0]
    print(f"  record_metadata      {n:>12,} rows", file=sys.stderr)


EMPTY_WORKOUTS = """
CREATE TABLE workouts (
    id BIGINT, activity VARCHAR, duration_min DOUBLE, source_name VARCHAR,
    source_version VARCHAR, device_name VARCHAR, device_raw VARCHAR,
    creation_date TIMESTAMPTZ, start_date TIMESTAMPTZ, end_date TIMESTAMPTZ,
    local_date DATE, local_time TIME, distance_km DOUBLE, active_kcal DOUBLE,
    basal_kcal DOUBLE, steps DOUBLE, avg_hr DOUBLE, max_hr DOUBLE,
    min_hr DOUBLE, indoor BOOLEAN, route_file VARCHAR)
"""


def stub_missing(con):
    """Keep the XML database's surface so downstream scripts do not crash.

    Every CREATE here is IF NOT EXISTS, so it only fires for what this schema
    genuinely lacks: `activity_summary` always (the data sits unread in
    `activity_caches`), and the rest only on a database where the source table
    was missing. `workout_events`, `workout_statistics`, `route_points` and
    `workout_blocks` are normally filled in above; an empty table is honest and
    keeps the downstream queries valid either way.
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS workout_events (
            workout_id BIGINT, type VARCHAR, date TIMESTAMPTZ, duration_min DOUBLE)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS workout_statistics (
            workout_id BIGINT, type VARCHAR, start_date TIMESTAMPTZ,
            end_date TIMESTAMPTZ, average DOUBLE, minimum DOUBLE,
            maximum DOUBLE, sum DOUBLE, unit VARCHAR)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS activity_summary (
            local_date DATE, active_energy DOUBLE, active_energy_goal DOUBLE,
            energy_unit VARCHAR, exercise_min DOUBLE, exercise_goal DOUBLE,
            stand_hours DOUBLE, stand_goal DOUBLE)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS route_points (
            route_file VARCHAR, t TIMESTAMPTZ, lat DOUBLE, lon DOUBLE, ele DOUBLE,
            speed_ms DOUBLE, course DOUBLE, hacc DOUBLE, vacc DOUBLE)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS workout_blocks (
            workout_id BIGINT, seq BIGINT, is_primary BOOLEAN,
            start_date TIMESTAMPTZ, end_date TIMESTAMPTZ, duration_min DOUBLE)""")
    con.execute("CREATE TABLE IF NOT EXISTS export_meta (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO export_meta VALUES ('source', 'healthdb_secure.sqlite')")


# ---------------------------------------------------------------- verification


def verify(new_db, xml_db):
    """Compare against an XML-derived database over the days they share.

    This is the only real check on the type-code map and the timezone handling:
    if HeartRate 5 were wrong, per-day counts would not line up.
    """
    con = duckdb.connect(new_db)
    con.execute(f"ATTACH '{xml_db}' AS old (READ_ONLY)")
    print("\nday-by-day HeartRate counts, this DB vs XML DB:")
    rows = con.execute("""
        -- `xml` is a reserved word in DuckDB, hence xml_n.
        SELECT coalesce(a.local_date, b.local_date) AS d,
               a.n AS live, b.n AS xml_n, coalesce(a.n, 0) - coalesce(b.n, 0) AS diff
        FROM (SELECT local_date, count(*) n FROM hr GROUP BY 1) a
        FULL JOIN (SELECT local_date, count(*) n FROM old.hr GROUP BY 1) b
             ON a.local_date = b.local_date
        WHERE coalesce(a.n, 0) <> coalesce(b.n, 0)
        ORDER BY d DESC LIMIT 25
    """).fetchall()
    if not rows:
        print("  identical on every shared day")
    for d, live, xml, diff in rows:
        print(f"  {d}  live={live or 0:>7,}  xml={xml or 0:>7,}  diff={diff:+,}")

    # The scale check is the one that catches silent unit bugs: HealthKit's
    # canonical units are not the XML's display units, so a type whose ratio is
    # not 1.0 needs an entry in UNIT_SCALE.
    print("\nvalue scale per type (xml / live; anything but ~1.0 is a unit bug):")
    rows = con.execute("""
        -- Only the window both databases cover: the XML export is a snapshot and
        -- everything after it would otherwise read as a scale difference.
        WITH cutoff AS (SELECT max(local_date) AS d FROM old.records)
        SELECT a.type, a.n AS live_n, round(b.v / nullif(a.v, 0), 4) AS ratio
        FROM (SELECT type, count(*) n, avg(value) v FROM records
              WHERE value IS NOT NULL AND local_date < (SELECT d FROM cutoff)
              GROUP BY 1) a
        JOIN (SELECT type, count(*) n, avg(value) v FROM old.records
              WHERE value IS NOT NULL AND local_date < (SELECT d FROM cutoff)
              GROUP BY 1) b USING (type)
        WHERE a.n > 20 AND abs(coalesce(b.v / nullif(a.v, 0), 1) - 1) > 0.02
        ORDER BY a.n DESC LIMIT 20
    """).fetchall()
    if not rows:
        print("  every type within 2% of the XML database")
    for t, n, ratio in rows:
        print(f"  {t:<32} {n:>10,}  ratio {ratio}")

    print("\nworkouts matched on start time (2s tolerance):")
    row = con.execute("""
        WITH m AS (
            SELECT a.activity AS live_act, b.activity AS xml_act,
                   a.duration_min AS live_dur, b.duration_min AS xml_dur,
                   a.avg_hr AS live_hr, b.avg_hr AS xml_hr
            FROM workouts a JOIN old.workouts b
              ON abs(epoch(a.start_date) - epoch(b.start_date)) < 2)
        SELECT count(*),
               count(*) FILTER (live_act = xml_act),
               count(*) FILTER (abs(coalesce(live_dur, 0) - coalesce(xml_dur, 0)) < 0.5),
               count(*) FILTER (abs(coalesce(live_hr, 0) - coalesce(xml_hr, 0)) < 1.5)
        FROM m
    """).fetchone()
    total = con.execute("SELECT count(*) FROM workouts").fetchone()[0]
    print(
        f"  {row[0]:,} of {total:,} matched; activity {row[1]:,}, "
        f"duration {row[2]:,}, avg HR {row[3]:,}"
    )

    print("\ntype coverage (live rows whose code stayed unmapped):")
    for t, n in con.execute("""
            SELECT type, count(*) n FROM records
            WHERE type LIKE 'type\\_%' ESCAPE '\\'
            GROUP BY 1 ORDER BY n DESC LIMIT 15""").fetchall():
        print(f"  {t:<16} {n:>12,}")
    con.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("source", help="iOS backup folder, a folder holding the DBs, or the .sqlite")
    ap.add_argument("--db", default=None, help="Defaults to the profile database.")
    ap.add_argument("--inspect", action="store_true", help="print schema and type census only")
    ap.add_argument("--verify", metavar="XML_DB", help="cross-check against an export.xml database")
    ap.add_argument(
        "--password-env",
        metavar="VAR",
        help="env var holding the backup password (never pass it inline)",
    )
    ap.add_argument(
        "--tz",
        default=None,
        help="fallback timezone when the row carries none",
    )
    athlete_profile.add_argument(ap, required=False)
    args = ap.parse_args()
    profile = athlete_profile.from_args(args, required=False)
    db = args.db or athlete_profile.field(profile, "db")
    if not db:
        raise SystemExit("no database. Pass --db PATH or --profile NAME.")
    tz = args.tz or athlete_profile.field(profile, "timezone") or os.environ.get("TZ") or "UTC"

    password = os.environ.get(args.password_env) if args.password_env else None
    work_dir = tempfile.mkdtemp(prefix="healthdb-")
    try:
        db_path = resolve_source(args.source, work_dir, password)
        if args.inspect:
            inspect(db_path)
            return

        out = os.path.expanduser(db)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        t0 = time.time()
        print(f"reading {db_path}", file=sys.stderr)
        build(db_path, out, tz)
        size = os.path.getsize(out) / 1e6
        print(f"done in {time.time() - t0:.0f}s -> {out} ({size:,.0f} MB)", file=sys.stderr)

        if args.verify:
            verify(out, os.path.expanduser(args.verify))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
