# Apple Health: getting the data out, and what it means once you have it

Everything here was learned by doing it. Apple does not document this schema, and
it moves between iOS releases — so read this before trusting a number, not before
every sync.

## Why there is an export step at all

Apple Health workout data lives **only on the iPhone and Apple Watch**. It is not
synced to a Mac in any readable form:

- macOS has no Health or Fitness app
- there is no HealthKit database anywhere under `~/Library`
- iCloud syncs Health end-to-end encrypted; no Mac-side container holds it

So the data has to leave the phone deliberately. There are two ways.

## Route 1: the XML export

On the **iPhone** (not the Watch):

1. Health app → tap the profile picture, top right
2. Scroll to the bottom → **Export All Health Data**
3. Confirm — it takes a few minutes to build
4. Share sheet → **Save to Files**, or AirDrop straight to the Mac

The result is `export.zip` containing `apple_health_export/export.xml`. It can be
several GB; most of it is heart-rate samples. This route needs no pairing, no
trust, no password and no extra dependencies. Start here.

**It cannot be automated.** "Export All Health Data" is a button inside Health.app:
no Shortcuts action, no API, nothing a third-party app can trigger. Apps that
advertise scheduled exports build **their own** file out of HealthKit — useful, but
not this XML. A FIT file in particular has one `heart_rate` field and no per-sample
source, which throws away the strap-versus-watch distinction the zones depend on.

## Route 2: the phone's own database, out of an encrypted backup

What *can* run unattended is the database Apple generates the XML from.
`healthdb_secure.sqlite` ships inside **encrypted** local backups, and a backup can
be taken from the Mac on a timer. This is what the `health-sync` skill automates.

**Health data is only present in encrypted backups.** Turning encryption on is what
makes the data available at all. Never turn it off and on again — that rotates the
password.

**Use `pymobiledevice3`, not `idevicebackup2`.** It installs cleanly on current
Python, flips backup encryption from the CLI, and brings `pyiosbackup` along, which
reads an encrypted backup **locally, with the phone unplugged**. libimobiledevice
stays worth having as the pairing and diagnostic toolbox.

```sh
pip install -r requirements-sync.txt

# This replaces the Finder tick box. The password is set here and must then be
# used for every read.
pymobiledevice3 backup2 encryption on -p "$IOS_BACKUP_PASSWORD"

pymobiledevice3 backup2 backup --only-regex 'Health/healthdb' ~/HealthBackups
```

**Do not use `backup2 extract` for this.** It is device-mediated — it asks the phone
to look the file up — and answers `MBErrorDomain/4, File not found in manifest` for
the Health databases under every path. Read the backup locally instead. The domain
is `HealthDomain` and the path is `Health/...`, not `HomeDomain` as the file layout
suggests:

```python
from pyiosbackup import Backup
b = Backup.from_path(backup_path, password=...)
b.extract_domain_and_path("HealthDomain", "Health/healthdb_secure.sqlite", path=out)
b.extract_domain_and_path("HealthDomain", "Health/healthdb.sqlite", path=out)
```

**Both files are required.** `healthdb_secure.sqlite` holds the samples;
`healthdb.sqlite` holds the device *names* (`sources`, `source_devices`). Without
the second one every device comes out as a bare product code and the
strap-versus-watch split collapses.

### Full backup or Health only

`--full` is not about scope — it forces a full run and discards local backup state,
where the default is incremental when valid local metadata exists. So the first run
transfers the whole phone whichever way it is invoked. Scope is `--only` (whose
presets do not include Health) and `--only-regex`, which matches manifest paths.

The trade is disk against repeat transfer:

- **Keep the whole backup** — every later run is incremental and short, because the
  manifest tells the phone what is already held. Costs tens of GB, and that is a
  complete encrypted copy of the phone: messages, photos, keychain. Think about
  where that sits before choosing this.
- **Health only** (`--only-regex 'Health/healthdb'`) — a couple of GB on disk.

Things that were guessed wrong here and then measured:

- **`--patch-manifest` cannot be used, and should not be wanted.** It refuses to
  start without `--password`, the flag worth never putting on a command line.
  Dropping it is also the *better* choice: `Manifest.db` stays complete, so the next
  run is genuinely incremental. The worry about the phone resending everything was a
  consequence of that flag, not of the filter.
- **A first run costs roughly half an hour.** The two Health files land in the first
  couple of minutes but are unusable until the very end, because `Manifest.db` is
  written last. `Status.plist` reads `SnapshotState: uploading` until then, and no
  password will open a backup without it.
- **Discarded payloads are written as zero-byte placeholders during the run** and
  removed at finalize. Mid-run the directory holds tens of thousands of empty files;
  the finished backup holds a handful. A file count taken while it runs says nothing.
- **Whether the filter saves transfer time is not settled.** It plainly saves disk.
  The observed entry rate implies more throughput than the USB link should manage,
  which *suggests* the discarded contents are not sent — an inference from a rate,
  not a measurement.

**Check for an existing backup before starting a cold one.** Besides your own backup
root there is the Finder/iTunes backup at
`~/Library/Application Support/MobileSync/Backup/<udid>`, encrypted with the same
password. It is a perfectly good source when it is fresh enough, and it sits four
levels deep, so a shallow `find` misses it — which is how one session started a
needless 31 GB cold transfer for nothing. `extract_health.py` checks both.

### What the backup route gains and loses

Gains, over the XML: no phone in hand; `origin_product` alongside the device name;
`workout_events` from the phone's own table rather than parsed out of XML; **the
blocks of a structured workout**, which the XML flattens away entirely; and
beat-to-beat heartbeat series, absent from `export.xml` and not yet decoded here.

**Routes are in, and they were never a blob.** The track sits in
`location_series_data`, a plain `WITHOUT ROWID` table holding latitude, longitude,
altitude, speed, course and four accuracy columns as REAL numbers. Two joins have to
be right and neither is the obvious one:

- `location_series_data.series_identifier` matches `data_series.`**`hfd_key`**, not
  `data_series.data_id`. They are different numbers, and a `data_id` join returns
  another workout's points without erroring.
- the route hangs off its workout through `associations`, where the **workout is the
  destination** and the type-102 `WorkoutRoute` sample is the source. Filter
  `deleted = 0`, and expect a re-synced run to carry **two live routes on one
  workout** — join both and the workout row itself doubles. The converter keeps the
  richer track, lowest `hfd_key` breaking the tie.

Loses: `activity_summary` is an empty stub on this route (the data is in
`activity_caches`, not yet unpacked), and a handful of very recent workouts may have
no XML counterpart.

### Structured-workout blocks: `workout_activities` → `workout_blocks`

This is the table to reach for whenever a session was run as a custom workout on the
watch.

`workout_events` type 7 (`Segment`) is **Apple's automatic segmentation** —
overlapping spans that cover the session end to end and mean nothing.
`analyze_intervals.py` is right to discard them. The real structure is in
`workout_activities`, keyed by `owner_id` → `workouts.data_id`, one row per block
plus one `is_primary_activity = 1` row spanning the whole session:

```sh
sqlite3 -header healthdb_secure.sqlite "
  select is_primary_activity,
         datetime(start_date+978307200,'unixepoch','localtime') st,
         round(duration,1) dur
  from workout_activities where owner_id = <workouts.data_id> order by start_date;"
```

On a 4×4 session it returns the plan exactly — warm-up, four work-recovery pairs,
cool-down — and it is the only place that records a work bout **stopped 26 s early**.
Detecting reps from heart rate landed within 20 s of these boundaries. Detecting them
from `RunningSpeed` failed outright: that stream carries spikes pinned at exactly
20.0 km/h, so a "fastest sustained pace" cutoff picks an impossible 3:19/km and
shreds the reps. Prefer `workout_activities` → HR-derived → speed-derived, in that
order.

Ordinary unstructured workouts have a single `is_primary_activity = 1` row, which is
how to tell the two apart. The converter exposes all of it as `workout_blocks`.

Which blocks are the work is decided by heart rate: a work bout runs hotter than the
blocks on *either side* of it. A threshold on the session mean looks simpler and
breaks — heart rate drifts up as a session goes, so the last recoveries end up above
the mean while the warm-up sits below it.

Reading real boundaries changes numbers. One session's reps turned out to be exactly
4:00 each rather than the 3:50–3:58 the speed detector inferred. **Anything compared
across sessions has to come from the same method.**

### Two traps that cost the most time

- **`device_name` must be the hardware name, not the product type.** The `hr` view
  decides strap versus watch from `device_name`. Provenance offers
  `origin_product_type`, but a chest-strap session is *owned* by the watch, so that
  column names the watch and every strap reading gets misclassified. The real name
  lives in `healthdb.sqlite`'s `source_devices`, keyed by
  `data_provenances.device_id` — and it is precise enough to separate two straps of
  the same model by serial.
- **Metadata values are typed, and the type matters.** `metadata_values.value_type`
  says which column holds the value. For quantities (type 3) the magnitude is in
  `numerical_value` and the **unit** in `string_value`, so taking the first non-null
  column returns `cm` where an elevation should be a number. Apple's XML writes those
  as one string, `"122200.0 cm"`, which is what the converter reproduces.
- **Canonical units are not display units.** The database stores heart rate in
  **count/second** — 2.7, not 162 — plus metres for distances, m/s for speeds and
  metres for step length. `UNIT_SCALE` holds the measured factors, and `--verify`
  prints the per-type ratio against an XML-built database. That is how a wrong guess
  got caught: `DistanceSwimming` is already in km, unlike every other distance.

### Before trusting a run

- `--inspect` prints the table list (over 160 on recent iOS) and a `data_type` census
  with unmapped codes marked `??`. Type codes are seeded from
  [christophhagen/HealthDB](https://github.com/christophhagen/HealthDB) and extended
  here. Codes that are neither quantity nor category were identified by which side
  table they join: 79 → `workouts`, 76 → `activity_caches`, 102 → `data_series`
  (routes), 119 → `binary_samples` (heartbeat series), 144 → `ecg_samples`.
- `--verify <xml-built.duckdb>` is the acceptance test: per-day HeartRate counts, the
  per-type value-scale ratio, and workouts matched on start time. Run it while both
  sources still overlap, because that window closes.
- When it was last verified against a real backup, per-day HR counts matched the XML
  database on every shared day bar a handful within ±30 samples, every type's value
  scale was within 2%, and there was **no** case where the XML had a heart rate and
  the backup database did not — while the backup route filled in average HR for
  hundreds of workouts where the XML has none.

## Parsing the XML

No unzipping needed, and no dependencies — stdlib only:

```sh
python3 parse_health_export.py ~/Downloads/export.zip --profile NAME
```

Also accepts an already-unzipped folder or a bare `export.xml`. The XML is streamed
with `iterparse`, so a multi-GB export never lands in memory.

| File | Contents |
|---|---|
| `workouts.csv` | one row per workout — activity, duration, distance, avg/max/min HR, energy, pace, speed, indoor flag |
| `daily_metrics.csv` | one row per day — resting HR, HRV (SDNN), VO2max, weight, steps, active energy, exercise minutes |

**Steps and walking distance are de-duplicated; everything else is summed.** The
iPhone and the Watch both record all day, so summing every source inflates them
badly — one export came out at a median 24,851 steps/day summed, against 14,902 from
the watch alone. Those two keep the largest single source per day
(`PER_SOURCE_MAX`); the rest come from one device and are safe to add up.

**Format notes.** Both export generations are handled: pre-iOS 15 put distance and
energy in attributes on `<Workout>`, newer ones moved them into
`<WorkoutStatistics>` children. Units are normalised to km / kg / minutes regardless
of the phone's locale, so a mi/lb-configured export comes out consistent.
`pace_min_per_km` is only populated for foot-based activities; cycling and swimming
get `speed_kmh`.

### Raw heart-rate samples

```sh
python3 extract_hr_samples.py ~/Downloads/export.zip 2026-08-09 --profile NAME
```

Pulls the per-sample HeartRate stream for given days. Needed whenever a summary
min/avg/max looks wrong, since it is the only way to tell a sustained peak from a
one-sample optical spike — and it reveals the *source* per sample, which is how a
chest strap gets found in the first place.

Note the stream is downsampled outside dense workout recording: a seven-hour hike may
carry one sample per two minutes. The absence of a peak there is not evidence against
it.

## DuckDB

The CSVs pre-aggregate to daily rows, because that is the only shape a spreadsheet
can hold. Per-second questions cannot be asked of them — what heart rate did inside
interval three, which sensor wrote each sample, how pace tracked against it. For
those, convert the whole export into a database:

```sh
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python health_to_duckdb.py ~/path/to/export.zip --profile NAME
```

A 2.2 GB `export.xml` converts in about 75 seconds into a 200 MB database; a 2.5 GB
one takes about 140 s. The XML is streamed, so memory stays flat; the staging step
writes CSVs and lets DuckDB's multithreaded reader do the loading, roughly an order
of magnitude faster than inserting row by row. `--no-routes` skips the GPX files.

`healthdb_to_duckdb.py` imports `VIEWS` from `health_to_duckdb.py` and builds the
same views, so `analyze.py`, `hq.py` and `analyze_intervals.py` run unchanged against
either database.

### Tables

| Table | Order of magnitude | Contents |
|---|---|---|
| `records` | millions | every sample: heart rate, steps, energy, weight, VO2max, sleep… |
| `route_points` | millions | GPS track points — lat/lon/ele/speed |
| `record_metadata` | millions | per-sample metadata (motion context and friends) |
| `workout_metadata` | thousands | per-workout metadata |
| `workout_events` | thousands | lap / segment / pause markers, mostly automatic |
| `workout_statistics` | thousands | per-workout HR / distance / energy aggregates |
| `workouts` | thousands | one row per workout |
| `activity_summary` | thousands | the daily rings |
| `workout_blocks` | hundreds | blocks of a structured workout — **backup route only** |

Every timestamp is a real `TIMESTAMPTZ`, and every table also carries `local_date` /
`local_time` taken straight from the export's own offset. Use those for "which day
was this" questions: a `TIMESTAMPTZ` renders in the session time zone, and a 00:30
workout otherwise lands on the wrong date.

**The two routes do not produce the same schema.** An XML-built database has no
`workout_blocks`, no `devices`, no `sources`; its `route_points` is keyed by
`route_file` with no `local_date` and no `workout_id`, so filter it by `t` and join
sessions by timestamp.

### Views

`hr` (samples with the sensor resolved), `hr_sources_by_day`, `workout_summary` (adds
pace and speed), and `daily_metrics` (the CSV equivalent, with the same
de-duplication of steps and walking distance).

**`hr.sensor` is the useful one.** `sourceName` is localised and near-useless — a
chest strap arrives as `Bluetooth Device` or `Bluetooth-Gerät` depending on the
phone's language, and says nothing about *which* strap. The `device` attribute names
the hardware, so `device_name` is parsed out of it and `sensor` buckets it into
strap / watch / phone / other.

### Weather in `workout_metadata` is not a sensor reading

`HKWeatherTemperature` and `HKWeatherHumidity` come from Apple's **weather service**
for the workout's location, snapshotted once when the workout starts. The watch has
no ambient temperature sensor; the only real thermometers in the export are
`AppleSleepingWristTemperature` and `WaterTemperature`.

Three consequences, all of which silently corrupt a temperature-versus-pace analysis:

- **Indoor workouts carry it too, and it is meaningless there.** Treadmill runs log
  the weather outside. Filter on `indoor = false`.
- **It is the value at the start, not the average.** A race starting at 09:15 in
  23.4 °C and finishing at 11:31 in about 32 °C carries 23.4 °C and nothing knows
  better. For anything over an hour, treat it as a lower bound.
- **Humidity is stored ×100.** The raw value `5800 %` means 58%.

That it is service data rather than a sensor is visible in the data itself:
consecutive workouts read 26.60 → 26.88 → 27.23 → 27.35 → 27.47 °C over 46 minutes.
A wrist sensor moving between sun and shade would never produce a curve that smooth.

### Querying

```sh
python3 hq.py --profile NAME --tables
python3 hq.py --profile NAME --schema hr
python3 hq.py --profile NAME "SELECT * FROM workout_summary ORDER BY start_date DESC LIMIT 10"
python3 hq.py --profile NAME --file q.sql --format csv > out.csv
```

Opens read-only and sets the session time zone from the profile, without which every
morning workout renders an hour early.
