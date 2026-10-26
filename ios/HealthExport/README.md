# HealthExport

A small iOS app that reads Apple Health through HealthKit and writes it to files, so
a Mac can copy them off over USB and build the same DuckDB database the backup route
builds. No backup, no backup password, no Finder.

It reads only, never writes to Health, and sends nothing anywhere. The files stay in
the app's own Documents folder until the Mac copies them.

```
iPhone: HealthExport  ──(devicectl, USB)──→  Mac: batches/  ─→  healthkit_to_duckdb.py  ─→  DuckDB
```

## Why it exists

Xcode cannot read Health on a phone, and nothing on the Mac can. The Health database
is open only to an app on the phone that holds the HealthKit entitlement and that the
user has allowed to read. A free Apple ID can sign such an app for its own phone.

The backup route still works, but on iOS 27 every scripted backup died on the
on-phone passcode prompt, so each sync meant Finder, the passcode and a full backup.
This app reads only what is new since its last run, in seconds.

## What it reads

Every quantity and category type in the iOS 27.1 SDK that is not deprecated, plus:

- **workouts**, with their statistics, events, activities (the blocks of a structured
  workout) and the **plan** each was run from, via WorkoutKit's `HKWorkout.workoutPlan`
- **routes**, as GPS points
- **heart-rate series**, unpacked into their readings with `HKQuantitySeriesSampleQuery`
- **beat-to-beat heartbeat series**, which neither the XML nor the backup route decodes
- **Activity rings**, one row a day

Values are written in the units the database already uses: heart rate per minute,
distance in km and speed in km/h. `HealthTypes.swift` has the table.

## Set up once

1. Copy `Local.xcconfig.example` to `Local.xcconfig` and set your team id and a bundle
   id of your own. The file is gitignored. The team id is in Xcode → Settings →
   Accounts. It is not the code in parentheses in `security find-identity`.
2. Plug the phone in by cable and unlock it. Trust the Mac if asked.
3. Turn on Developer Mode: **Settings → Privacy & Security → Developer Mode**, then
   restart. After the restart, unlock the phone and **confirm "Turn On" in the
   prompt** with your passcode. Without that confirmation the switch looks on, but
   the phone still reports `developerModeStatus: disabled`, and xcodebuild says
   "Developer Mode disabled".
4. Build and install with the phone unlocked:

   ```sh
   xcodebuild -project HealthExport.xcodeproj -scheme HealthExport -configuration Debug \
     -destination 'id=<phone UDID>' -destination-timeout 300 -derivedDataPath build/dd \
     -allowProvisioningUpdates -allowProvisioningDeviceRegistration build
   xcrun devicectl device install app --device <phone UDID> \
     build/dd/Build/Products/Debug-iphoneos/HealthExport.app
   ```

   It has to be the device destination. `generic/platform=iOS` builds and signs
   fine, but its profile covers only the Mac, so the app would not run on the phone.
5. Trust the developer on the phone: **Settings → General → VPN & Device Management
   → Apple Development: … → Trust**. Until then a launch fails with "invalid code
   signature, inadequate entitlements or its profile has not been explicitly trusted".
6. On the first export, tap **Turn On All** on the Health sheet, then **Allow**.

**A free team's profile lasts 7 days.** After that the app stops launching until step
4 is run again. The batches and the anchors survive a reinstall, so nothing is lost.
The expiry is in the profile:
`security cms -D -i build/dd/Build/Products/Debug-iphoneos/HealthExport.app/embedded.mobileprovision`.

## Run it

From the repository root, with the phone on USB and unlocked:

```sh
.claude/skills/health-sync/scripts/sync_healthkit.sh --profile NAME
```

That launches the app with `-autoExport YES`, waits for it to quit, copies
`Documents/batches/` off with `devicectl device copy from`, and rebuilds
`<data_dir>/health-hk.duckdb` from every batch. Use `--days N` to set how far back
the first export reaches (default 30), and `--full` to forget the anchors and read
that far back again. `--compare <backup-built db>` prints what differs.

The app also has a button. Exporting by hand and then running the script with
`--skip-launch` does the same.

## How the files are laid out

```
Documents/
  state.json                     anchors, and the start date of the first export
  batches/<UTC time>/            one per run, never changed afterwards
    samples.jsonl                quantity and category samples, heartbeat series
    series_points.jsonl          the points inside each heart-rate series
    heartbeats.jsonl             beat times of each heartbeat series
    workouts.jsonl               one workout per line, statistics, events, activities, plan
    routes.jsonl, route_points.jsonl
    activity_summary.jsonl       the rings, re-read in full every run
    deleted.jsonl                UUIDs HealthKit reported deleted since the last run
    manifest.json                written last; a batch without it is unfinished
```

Anchors are saved only after a batch is complete, so an interrupted run re-reads
what it lost on the next one. The loader keeps the newest copy of each UUID, so a
re-read costs time, not correctness.

## Files

| File | What |
|---|---|
| `HealthExport/Exporter.swift` | anchored queries, series, routes, workouts, the batch files |
| `HealthExport/HealthTypes.swift` | what is read, and the unit each quantity is written in |
| `HealthExport/PlanEncoder.swift` | a WorkoutKit plan as the columns `workout_blocks` carries |
| `HealthExport/JSONL.swift` | the writer, and HealthKit values made JSON-safe |
| `HealthExport/HealthExportApp.swift` | the model, and the `-autoExport` launch path |
| `HealthExport/ContentView.swift` | the screen: signature countdown, export button, a week of rings, recent workouts, batches, log |
| `HealthExport/Dashboard.swift` | what that screen shows, read back from the batches already written, never from HealthKit |
| `icon/make_icon.swift` | renders `Assets.xcassets/AppIcon.appiconset/AppIcon.png` |
| `Signing.xcconfig` | tracked defaults; includes your gitignored `Local.xcconfig` |
| `HealthExport.xcodeproj` | hand-written, Xcode 16 format with a synchronized folder |
