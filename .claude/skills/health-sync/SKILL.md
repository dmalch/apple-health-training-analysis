---
name: health-sync
description: Pull new Apple Health workouts off an iPhone into a DuckDB database and analyse them. Use when asked to load, sync, refresh or update training data from a phone, or to analyse a recent workout, interval session or long run that is not in the database yet.
argument-hint: "--profile NAME [--skip-backup]"
allowed-tools: Bash Read Write Edit Grep
---

# Health sync — phone to DuckDB

Refreshes a profile's `health-live.duckdb` from an iPhone, then analyses whatever
arrived. That database is what `analyze.py`, `analyze_intervals.py` and `hq.py` all
read.

**Deep detail lives in `docs/apple-health.md` and `docs/method.md`** — schema, unit
scaling, which Apple activity labels lie, how zones are anchored. This file is only
the operational path. Read those before interpreting numbers; read this before
running anything.

This route is optional. The XML-export route (`health_to_duckdb.py`) needs no
pairing, no trust and no encryption password, and is the right starting point for
most people. See the README.

## The one command

```bash
${CLAUDE_SKILL_DIR}/scripts/sync_health.sh --profile NAME
```

Backup → extract → convert → print what is new since the previous database. Builds
at `health-live.duckdb.new` and swaps on success, so a failed run leaves the old
database intact. Add `--skip-backup` to reuse the newest backup already on disk,
and `--force` to convert a backup that is not newer than the current database.

The profile supplies the database path, the backup root and — importantly — the
`device_udid` this profile is allowed to read.

Then analyse. There is no automatic report; decide from what landed:

```bash
python3 hq.py --profile NAME "<query>"
python3 analyze_intervals.py --profile NAME --workout <id> --expect-reps 4
python3 analyze.py --profile NAME --months 12
```

## Before starting

1. **The phone must be on USB and unlocked** for the whole run. Wi-Fi sync is not
   used.
2. **Know whose phone it is.** See the next section — this is the failure that
   destroys data rather than wasting time.
3. **Read the session against what was planned**, not in isolation. A rep count, a
   pace cap or an HR ceiling only means something next to its prescription.

## Two phones in one household

This is the one failure here that loses data rather than time.

`sync_health.sh` backs up whatever iPhone is on the USB bus and converts the
newest backup it finds under the backup root. Point it at one person's profile
with the other person's phone plugged in, and it writes their data over the
database it was pointed at.

The guard is `device_udid` in the profile. Before extracting anything, the script
reads `Target Identifier` out of the resolved backup's `Info.plist` and refuses
when it does not match. Set it once per profile:

```bash
pymobiledevice3 usbmux list        # UDID of the phone currently attached
```

A profile with no `device_udid` gets a warning instead of a guard, which is worth
fixing before the second phone ever appears.

## Things that cost an hour when forgotten

- **A dropped transfer is silent: `pymobiledevice3` exits 0.** It logs
  `Connection was terminated abruptly` and returns success, so `set -e` does not
  catch it and the pipeline walks on to extract and convert — rebuilding the
  identical database and reporting a quiet week, which looks exactly like a week
  with no training. `sync_health.sh` greps the transfer log, and separately
  refuses to convert a backup that is not newer than the database (`--force`
  overrides).
- **The phone can drop off the USB bus entirely mid-transfer — check `usbmux list`
  before suspecting pairing.** A transfer that dies seconds in with only
  `ERROR Connection was terminated abruptly` and no progress lines usually means
  the bus, not the pairing: `lockdown info` then answers `Device is not
  connected`, and `usbmux list` returns an empty `[]`. Re-seat the cable. That is
  a different failure from a merely locked phone (`Device is password protected.
  Please unlock and retry`) and from a stored trust refusal (`User refused to
  trust this computer`, which fails *instantly*, with no `waiting user pairing
  dialog...` line — clear it with Settings → General → Transfer or Reset iPhone →
  Reset → Reset Location & Privacy).
- **Judge a flaky cable from the kernel log, not from usbmuxd.** usbmuxd logs
  `deviceRequiresMuxConfiguration` on every enumeration, and a person moving the cable
  between ports produces exactly the same burst as a bad cable — counting those lines
  "proved" a flapping connection on 24 Sep 2026 that was really someone re-plugging.
  The kernel tells them apart:
  ```bash
  /usr/bin/log show --last 15m --style compact \
    --predicate 'process == "kernel" AND eventMessage CONTAINS "AppleUSBHostPort"'
  ```
  `cableChangeOccurred: powering off` on one port followed by `powering on` on another
  is a hand; `terminateDevice: ... hardware connection lost` with no `cableChangeOccurred`
  around it is a real drop. One `hardware connection lost` within a second of the first
  enumeration is ordinary contact bounce at plug-in. (Call `/usr/bin/log` by full path:
  in zsh, `log` is a builtin and fails with `too many arguments`.)
- **iOS asks for the device passcode ON THE PHONE before an encrypted backup, and
  `pymobiledevice3` does not survive the prompt.** Seen on iOS 27, 19 Sep 2026. The run
  dies in the first seconds with nothing but `ERROR Connection was terminated abruptly`
  and exit 1 — the same signature as a dropped USB bus, which sends you diagnosing the
  wrong thing. The tell is one line earlier in a verbose run:
  `WARNING Please enter the device passcode to continue the backup`, followed ~7-15 s
  later by `INFO Device passcode prompt dismissed`. Everything else checks out while this
  happens: `usbmux list` shows the device, `lockdown info` answers, `backup2 encryption`
  returns `on`. **Distinguish it from the bus failure by whether `usbmux list` still has a
  `"ConnectionType": "USB"` entry** — bus failure removes it, this does not. Do not test
  for an empty list: on 24 Sep 2026 the list also carried a `Network` entry for the same
  phone while it was cabled, so it was never empty. The prompt window is short and
  unlogged on the Mac side, so catching it by hand takes luck.
  It recurred on 24 Sep 2026, so treat it as the normal case on iOS 27, not a one-off.
  **The reliable workaround is to let Finder take the backup** — it presents the passcode
  prompt natively and waits — and then convert it:
  `sync_health.sh --profile NAME --skip-backup`. `resolve_backup` falls through to the
  MobileSync directory on its own, and because `newest_backup` requires a `Manifest.db`,
  a half-finished Finder attempt is skipped rather than converted.
- **Finder can hang on "Loading…" over USB too — its device agents wedge, not the
  phone.** Seen 24 Sep 2026: the phone was in the sidebar, cabled and trusted
  (`lockdown info` answered, `TrustedHostAttached: true`), and its content never
  opened. The phone had bounced on the bus a few times while being plugged in, and
  `AMPDevicesAgent` logged a burst of `Assertion failure: err == (-42059)` at that
  moment, with `AMPDeviceDiscoveryAgent` failing lockdown on the stale device IDs
  (`kAMDDeviceDisconnectedError`). The sequence that fixed it — which step was the
  decisive one is not known, and each alone looked like it had failed:
  ```bash
  killall AMPDeviceDiscoveryAgent AMPDevicesAgent   # launchd respawns discovery itself
  launchctl kickstart gui/$(id -u)/com.apple.AMPDevicesAgent   # this one does NOT come back alone
  killall Finder   # it holds the dead agent's connection and drops the phone from the sidebar
  ```
  `launchctl kickstart -k` on the discovery agent is refused under SIP; `killall` works.
  Finder has the phone again when its log shows `deviceAttached <udid>`
  (subsystem `com.apple.DesktopServices`, category `iTunes`).
- **Finder's backup is a CLI underneath.** Finder does not back up in-process: it spawns
  `/System/Library/PrivateFrameworks/MobileDevice.framework/Versions/Current/AppleMobileDeviceHelper.app/Contents/Resources/AppleMobileBackup --pipe 9 30`,
  and the same binary documents `--backup --target <udid>` in its `--help`. It could
  replace the Finder click, **but that is untested** (24 Sep 2026) — in particular
  whether it waits for the on-phone passcode prompt the way Finder does. The same binary
  also has `--restore` and `--erase`; never run either.
- **An aborted transfer costs you the incremental path.** The phone, not the
  script, decides: after a crashed attempt the next `Status.plist` comes back
  `IsFullBackup: true` even though the local side asked for incremental. There is
  no resume in mobilebackup2 — every attempt re-streams from the start. So keep
  the phone plugged in and unlocked for the whole run, and never delete the backup
  directory to "start clean".
- **`SnapshotState` runs `uploading` → `moving` → `finished`.** Only `finished` is
  safe to decrypt; read it with
  `plutil -extract SnapshotState raw -o - <backup>/Status.plist`. A backup without
  `Manifest.db` is still uploading.
- **Backup encryption must stay on.** Health data is only included in *encrypted*
  backups. `backup2 encryption` reports the state without arguments. Never turn it
  off and on again — that rotates the password.
- **The password lives in the login keychain**, under the service name the profile
  gives (`keychain_service`, default `ios-backup`), optionally mirrored from a
  password manager via `op_ref`. `extract_health.py` resolves it itself. Never
  pass it on a command line.
- **Never add `--patch-manifest`.** It requires `--password` at transfer time and
  strips entries from `Manifest.db`, which forces every later run to be full
  again. Leaving it off is what keeps run 2 incremental.
- **`--only-regex 'Health/healthdb'` saves disk, not time.** The phone streams its
  whole payload either way; the regex only decides what is kept. A first run into
  an empty directory is unavoidably a full transfer.
- **There is a second backup at `~/Library/Application Support/MobileSync/Backup/<udid>`**
  — the Finder one, and much larger. The scripts fall back to it automatically. It
  is a valid source when it is fresh enough, and it sits four levels deep, so a
  shallow `find` will miss it.
- **Both SQLite files are required.** `healthdb_secure.sqlite` has the samples,
  `healthdb.sqlite` has the device *names* — without it every source reads as a
  bare product code and the strap-vs-watch split that anchors the HR zones is
  lost.

- **Wi-Fi does not work on iOS 27 + macOS 27 — use the cable.** Tested 22 Sep 2026, every
  route:
  - *Pairing is not the blocker.* usbmuxd hands the system pair record to any local
    client (`ReadPairRecord`, no root), and `create_using_tcp(host, identifier=udid,
    autopair=False, pair_record=...)` built from it comes back `paired: True`. (An
    earlier note here said the record was unreachable over TCP; that was wrong.)
  - *Services are.* Over that TCP lockdown only `get_value` works. Every started
    service — `mobilebackup2`, AFC, diagnostics — is closed by the phone at its first
    read (`ConnectionTerminatedError`, 0 bytes), with the phone unlocked and
    `EnableWifiConnections: True`. The phone also advertises `_remotepairing._tcp`,
    so iOS 27 appears to serve network sessions only over RemotePairing/RSD.
  - *Finder does not help either.* On 22 Sep macOS 27's usbmuxd did not list the phone
    as a network device (`usbmux list` stayed `[]`), so Finder showed it in the sidebar
    from bonjour and hung on "Loading…"; `AMPDevicesAgent` logs `client connection timed
    out waiting for device <udid>`. Toggling the phone's Wi-Fi changes nothing. On
    24 Sep, with the cable in, usbmuxd did list a `Network` entry beside the USB one —
    and connections through it still died at once (`MuxTCPSendNextSegmentOverNetwork
    Unexpected EOF` in the usbmuxd log). A listed network entry is not a usable one.
  - `pymobiledevice3 --mobdev2` also cannot pick a device non-interactively: bonjour
    reports `UniqueDeviceID: None` until pair verification.
  Plug in, let Finder take the backup, then `--skip-backup`. Over USB a Finder
  incremental took minutes.

- **A PyPI wheel's `.so` can be killed by Gatekeeper, and the dialog offers to delete it.**
  On macOS 26/27 an ad-hoc-signed extension module fails to load with
  `library load disallowed by system policy` and the user is shown a "Not Opened" dialog
  whose primary button is **Move to Bin** — one click and the library is gone from the
  venv (it lands in `~/.Trash` and can be moved straight back). Tell them to press *Done*.
  The fix is `xattr -dr com.apple.quarantine <venv>`, which clears it for every other
  wheel in that venv too.

## Reading a session

- **Structured workouts carry their real block boundaries** in `workout_blocks`
  (from the watch's own plan). Prefer that over HR-derived splits, and never over
  `RunningSpeed`, which spikes pinned at exactly 20.0 km/h.
  `analyze_intervals.py` already tries them in that order.
- **`workout_blocks` exists only on the backup route.** An XML export has no such
  table, so `analyze_intervals.py` does not apply to a database built by
  `health_to_duckdb.py`. Derive splits from `route_points` and haversine instead.
- **Apple's `Segment` events are not laps** — they tile the whole session and mean
  nothing.
- **Numbers compared across sessions must come from the same method.** Re-deriving
  an interval session from block boundaries moves every figure. Record which
  method produced a number next to the number.
- **Weather in `workout_metadata` is a service lookup, not a sensor** — present on
  indoor sessions too, taken at the start only, humidity stored ×100.
- **`daily_metrics` for the most recent days is provisional and gets rewritten.**
  A sync taken mid-day captures a partial day, and the next full sync silently
  revises it — resting HR and HRV both move enough to reverse a conclusion about
  whether someone started a session fresh. Never quote same-day or sync-day
  recovery numbers as settled; re-read them after the following sync.
- **A session can be split across several watch records — reconcile before
  reading.** Two records minutes apart may be one run, and the real interruption
  is often a pause *inside* one of them, visible as a gap in `route_points` rather
  than as the seam between records. `duration_min` already excludes paused time,
  so the pause is invisible in the workout row.
- **Match the window length to the claim before calling anything a discrepancy.**
  A "best 3 km" compared against a recorded "last 2 km" will disagree by 20–30
  s/km purely because the longer window pulls in slower running. Compute best-N
  over the same N the claim uses.
- **Haversine over `route_points` is trustworthy — within about 1.2% of Apple's
  own distance.** It is not the explanation for pace gaps larger than that; look
  for a window or method mismatch instead.
- **The walk/run threshold is per-person and 7 km/h is usually wrong.** 7.0 km/h
  is 8:34/km, which is inside many people's easy long-run range, so it labels real
  running as walking. Calibrate it against the person's own easy pace before using
  it to split a session.

## After a run that discovers something durable

Write it back — a new gotcha into this file, a measured fact into the docs. Findings
should not stay in the transcript.
