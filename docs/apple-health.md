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

### Workout heart rate older than 120 days is packed into series

**A session's heart-rate stream is packed once it is four months old, and only the
backup route can read it back.** The phone packs the dense samples a workout records
into quantity series 120 days after the fact: one `samples` row per series,
`quantity_sample_series` holding its point count and `hfd_key`, and the points in
`quantity_series_data`, keyed by `series_identifier` = `hfd_key` — the same
not-the-`data_id` trap as the routes. Point timestamps are absolute on the HealthKit
epoch and values are canonical (count/s). A series starts at its first point and ends at
its last point's timestamp plus duration, on every one of 4,255. In one backup every one
of the 4,113 packed heart-rate series lay inside a workout; 832 were created 120 days to
the day after they ended, and the history before them was worked through as a backlog
over the eight months after the phone's first iOS 26 build.

**iOS 27 also writes heart rate straight into series.** From 16 Sep 2026 the same
backup held 142 more, 3,786 points between them, created a median two minutes after
they ended (the latest nine hours), none inside a workout, 33 from the chest strap.
Series are not only a matter of age.

Read as a sample, a series is **one row**. `quantity_samples` gives it a single value
that is not the mean of its points (3 bpm above it on one), and it spans a median 14
minutes and 119 points. The XML export writes one `<Record>` per series with that value,
so the XML route cannot get the stream back. Read that way, a 32-minute run is three
heart-rate rows, and of 926 sessions with packed heart rate one qualified for real time
in zone (`docs/method.md`), against 81 of the 97 sessions younger than 120 days.

**The backup route reads the points, and they are run-length encoded**: consecutive
identical readings become one point whose `duration` runs from the first of them to the
last. An XML export that predated the packing of 95 series settled it. Their 13,959
original readings had become 9,947 points; every point with a duration had absorbed at
least two readings, the first at its timestamp and the last at timestamp + duration
(2,602 of 2,603), a median 5 s apart and never more than 11 s inside one point; and
13,947 of the readings carried their point's value exactly. Across the whole backup 29%
of heart-rate points carry a duration, 10,000 of them longer than the 15 s
`MAX_SAMPLE_GAP_S` credits a sample with, the longest 124 s. The strap's never do.

So the converter gives a point back as readings at both of its ends and evenly in
between, at its series' own cadence: the median gap from one point's end to the next
point's start, which is one reading interval because the next reading differed — 5 s for
the watch and AirPods, 1 s for the strap. That guess only decides how many readings come
back. On the 95 series it made 13,884 readings where there had been 13,959; a fixed 5 s
would have made 13,926, and the fewest that keep every gap under 15 s, 12,797. Coverage
and zones do not
depend on it as long as the spacing stays under 15 s. Against the export, the same 20
of the 21 sessions in those days qualify for per-sample zones as from the XML (3 did
before), each within half a point of the XML's coverage and of its share of every zone.

Readings rather than points with an end: `analyze.py` and `analyze_intervals.py` credit
a sample with the seconds until the next one, so point rows would have needed every
per-sample reader taught about `end_date` — and on the XML route a series' own row spans
its whole 14 minutes, so honouring `end_date` there credits 14 minutes to a value that
is not a reading. Readings keep `records` the shape both routes already share, and give
the per-sample means (a workout's average when the phone kept none, an interval's
average HR) their original weighting.

Each reading is a `records` row carrying its series' `data_id`, so **`records.id` is no
longer unique on this route**: `GROUP BY id` gives a series back, and the series'
metadata — `_HKPrivateHeartRateContext`, on 4,252 of the 4,255 — still reaches every
reading, as it reached each sample before the phone packed them. The series' own row
moves to `quantity_series`, with `points` and `readings`, because it is what the XML
holds and what `--verify` compares. That backup's 441,363 points came back as 639,994
readings, taking `records` from 5.13M rows to 5.77M and the build from 39 s to 42.

Workouts the phone kept no heart-rate statistics for — 973 in that backup, 881 of them
from 2020 to 2022 — take their average, maximum and minimum from the samples inside
their window, so they moved too. 485 changed: the maximum rose on 464, by 16 bpm on
average, because a series' single value had stood in for its highest reading, and 11
short sessions that had no sample starting inside them got an average for the first
time. The average itself moved by 1.8 bpm on average, 25 at most.

Only heart rate is read this way (`SERIES_READ_AS_POINTS`). Energy, distance and steps
are packed the same way, but there a series' value is the sum of its points, so daily
totals survive to the decimal as one row while their row counts drop about tenfold. The
other types that arrive as series hold readings like heart rate — environmental audio
exposure (102,816 series, 7.3M points), headphone audio, sound reduction, running speed,
power, stride, oscillation and ground contact, cycling power and cadence — but nothing
here reads them per sample, and all of them expanded would more than double `records`.

Only the dense in-workout stream is packed. The watch's background readings stay
individual samples, and so does a chest strap's stream: on a strap day the watch was
packed and 2,787 strap samples were not.

**So an XML export and a later backup disagree on every day that turned 120 days old in
between**, counted as the rows each stores, and not by a handful. Against an export five
weeks older, a backup had lost 15% to 87% of the heart-rate rows on each workout day of
the month that aged past 120 days in between (one day: 522 against 2,170), and the day's
mean fell by up to 50 bpm, because the workout stream shrank to a few rows while the
background readings stayed. The strap days lost the least and kept their mean. Read back
as readings, the 14 days of that month agree again: counts within 1.6% of the export's
(2,136 against 2,170 on the worst) and means within 0.4 bpm.

### Structured-workout blocks: `workout_activities` → `workout_blocks`

This is the table to reach for whenever a session was run as a custom workout on the
watch.

`workout_events` type 7 (`Segment`) is **Apple's automatic segmentation** —
overlapping spans that cover the session end to end and mean nothing.
`analyze_intervals.py` is right to discard them.

Type 4 (`Marker`, zero duration) is the opposite, and it is the only human-made structure
in an unstructured session: **manual segment presses land here, not under `Segment`.**
On a 77-minute interval class with eight presses, the markers split warm-up, six
interval blocks and the closing core work exactly, and three consecutive blocks of
8.4 / 7.8 / 8.1 min were enough to settle the work/rest scheme (16 × 30 s, i.e. 20/10)
that the athlete remembered as 40–45 s on / 15–20 s off. People skip presses mid-session,
so read a long gap as possibly two blocks rather than one.

For a structured workout the real structure is in
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
how to tell the two apart. The converter exposes all of it as `workout_blocks`, where
`seq` counts the primary and the non-primary rows separately — both start at 1, so
order by it only with `NOT is_primary` in the filter.

**The kind of each step is recorded, in two places no single row holds.** The block
rows themselves do not say whether they were a warm-up, a work step, a recovery or a
cool-down, but the backup does, and the converter joins the two into `workout_blocks`
as `kind`, `step_path`, `goal_value` / `goal_unit` and `target_type` / `target_min` /
`target_max`:

- **The plan** is the workout's `_HKPrivateWorkoutConfiguration` metadata, a value_type
  4 payload in `metadata_values.data_value` (still NULL in `workout_metadata`; it is
  decoded into `workout_blocks` instead). It is JSON, not a binary plist:
  `{"proto_data": <base64 protobuf>, "data": <base64 JSON>, ...}`, and `data` alone
  carries everything, readable without the protobuf. It decodes to `{"intervalWorkout":
  {"name", "warmupBlock", "stepBlocks", "cooldownBlock"}}`. Each block is `{"steps":
  [...], "count": <repetitions>}`, and each step has a `stepType` (**0 work, 1 recovery,
  2 warm-up, 3 cool-down**), a `goal` (a base64 NSKeyedArchiver plist whose
  `NLSessionActivityGoalValue` is in the unit its `HKQuantity` names, e.g. 900 s; 0 is
  an open goal) and `targetZoneDatas` (base64 JSON, e.g. `{"type": "instantaneous_pace",
  "min": 2.70, "max": 2.70}` in m/s).
- **Which step each block ran** is in `workout_activities.metadata`, one NSKeyedArchiver
  plist per row, under `WOIntervalStepKeyPath`: `"block.iteration.step"`. Blocks are
  numbered warm-up, step blocks, cool-down, **counting only blocks that have steps**: a
  plan with an empty warm-up starts at block 0 on its first step block, and indexing
  the full list instead runs off the end. `WOIntervalStepSuccessful` sits beside it
  but is only set on recent sessions (false or absent on every older one checked), so
  it cannot say which steps were cut short.

Following the key path into the plan labelled all 12 structured sessions checked,
including a rowing session with no heart rate inside its blocks at all.

Where that is not available — an XML-built database, one converted before the plan
was read, a session with a block the plan does not cover — `analyze_intervals.py` infers
the kind for the whole session instead, never mixing the two. Heart rate is the wrong
thing to infer it from on short blocks: it trails effort by 30–60 s. On one-minute
strides with two-minute recoveries the strides averaged within 4 bpm of the recoveries,
and below them twice in four. On 40 s / 20 s sets, calling a block work when it ran
hotter than both neighbours labelled every 20-second recovery as the rep. Pace has no
lag and split both sessions cleanly, every rep at least 30% faster than its recovery.
The inference labels by pace when the route covers every block, and by heart rate only
when it does not; that still works on 4-minute reps.

Two rules that look right fail on real plans. *Work is harder than both neighbours*
drops a tempo block followed by strides that outrun it. *Work and recovery alternate*
breaks on a set rest after the last rep's recovery. What holds is hysteresis: a block
keeps the kind of the block before it unless its effort moves clearly — a fifth of the
session's spread — the other way. The last block is judged by the blocks it is nearest
to, because a cool-down jogged after walked recoveries is clearly harder than the block
before it and still nothing like the reps. A threshold on the session mean breaks too:
heart rate drifts up as a session goes, so the last recoveries end up above the mean
while the warm-up sits below it.

The labels describe what was run, not what was planned. Against the decoded plans, the
inference matched 8 of 11 GPS sessions exactly, and every difference is one of those:
a set rest run at rep pace (inferred as work), an open-goal walk-back ending the session
(planned as a recovery, inferred as a cool-down), and the easy first step of an
"11 + 3" run (planned as work, inferred as a warm-up). The plan says what was meant;
effort says what happened. A report may want either, but it must say which.

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
- `--verify <xml-built.duckdb>` is the acceptance test: quantity and category rows
  without a value, each day's HeartRate count, completeness and mean over the days both
  databases cover, heart-rate series read back against an export that still holds their
  readings, the per-type value-scale ratio, and workouts matched on start time. The
  day-by-day and scale checks fold each series back into the one row the phone stores,
  because that is what the XML holds: compared as readings, 1,314 days differed where 799
  had, and heart rate came out 13% off in the scale check, which that check reads as a
  unit bug. The series check is the only one on how the points are read back, and it
  needs an export older than the packing — against one five weeks older, 95 series gave
  14,000 readings where the export held 14,050 over the same spans, and no series' mean
  was more than 0.5 bpm off. Run it while both sources still overlap, because that window
  closes — and read the
  days packed after the export as the phone's doing, not the converter's (*Workout
  heart rate older than 120 days is packed into series*, above). Those days also push
  energy, distance and steps 2–10% off in the scale check, since a packed row is a sum;
  leave them out and every ratio is 1.0. That is not a unit bug.
- When it was first verified against a real backup, per-day HR counts matched the XML
  database on every shared day bar a handful within ±30 samples, every type's value
  scale was within 2%, and there was **no** case where the XML had a heart rate and
  the backup database did not — while the backup route filled in average HR for
  hundreds of workouts where the XML has none. Four weeks later the phone had packed
  a month of those days into series.
- **A deleted sample is still a `samples` row.** The phone keeps its `objects` row with
  `type = 2` — the schema's own partial index on that value is named `objects_deleted`
  — and its `samples` row, and drops the quantity, the category value, the metadata and
  the associations; its `creation_date` becomes the time of deletion. Read like a live
  sample it is a timestamp with a NULL `value`. One backup held 36,914: 32k energy,
  distance and step rows, 4,857 heart rate from the watch and AirPods, and a few hundred
  resting heart rate, sleep and walking averages. The heart-rate ones were the originals
  of five workout days packed on iOS 27, each deleted 120 days after its day, which is
  why an XML export taken before had a value at every one of their instants. 35 shared
  an instant with a live reading, which made `analyze.py`'s zones change between runs,
  and `analyze_intervals.py` crashed on all seven sessions that held any. The converter now
  skips `objects.type = 2` and warns if a quantity or category row still arrives without
  a value. Days packed earlier, on iOS 26, had none left; whether iOS 26 never kept
  them or the phone purges them is not known — the oldest in that backup was 11 days old.
- **Matching counts are not matching values, and a list capped at 25 lines hides
  whatever sorts last.** `--verify` passed all of the above. A deleted sample counted as
  one row like the reading it replaced, so per-day counts matched, and the days that
  did differ never reached the screen: the list was ordered newest first, and the days
  after the export, which differ by definition, filled all 25 lines. It now compares
  each day's mean and count of rows without a value, keeps to the days both databases
  cover, says how many days differ, and lists the furthest apart first. Expect hundreds
  to differ without anything being wrong, because the routes file some samples under
  different days: near midnight, which moves a day's count by a few and its mean by up
  to about 1 bpm, and on travel days for hours at a stretch, where a pair of adjacent
  days swings by hundreds in opposite directions. Over one ten-day trip both databases
  held the same 5,814 instants and filed 2,850 of them under a different day.

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
| `quantity_series` | thousands | each heart-rate series as the one row the phone stores; `records` holds its readings — **backup route only** |
| `workout_blocks` | hundreds | blocks of a structured workout — **backup route only** |

Every timestamp is a real `TIMESTAMPTZ`, and every table also carries `local_date` /
`local_time` taken straight from the export's own offset. Use those for "which day
was this" questions: a `TIMESTAMPTZ` renders in the session time zone, and a 00:30
workout otherwise lands on the wrong date.

**The two routes do not produce the same schema.** An XML-built database has no
`workout_blocks`, no `quantity_series`, no `devices`, no `sources`; its `route_points`
is keyed by `route_file` with no `local_date` and no `workout_id`, so filter it by `t`
and join sessions by timestamp. And the two fill `records` differently where heart rate
was packed into series: the XML holds one row per series, the backup database the
readings, all under the series' `id`.

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
