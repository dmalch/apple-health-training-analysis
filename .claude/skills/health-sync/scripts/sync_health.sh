#!/usr/bin/env bash
# End-to-end refresh of a profile's health-live.duckdb from an iPhone.
#
#   backup (incremental) -> extract the two Health SQLite files -> convert -> report what is new
#
# Safe to re-run. The new database is built at a temporary path and only swapped
# in on success, so a failed run leaves the previous one intact.
#
# Usage:  sync_health.sh --profile NAME [--skip-backup] [--keep-extract] [--force]
#   --profile NAME  whose database to write; supplies db, backup_root, device_udid
#   --skip-backup   reuse the newest finished backup already on disk
#   --keep-extract  do not delete the extracted SQLite afterwards
#   --force         convert even if the backup is no newer than the database

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${HEALTH_PROJECT:-$(cd "$SKILL_DIR/../../.." && pwd)}"

SKIP_BACKUP=0
KEEP_EXTRACT=0
FORCE=0
PROFILE="${HEALTH_PROFILE:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)      PROFILE="${2:-}"; shift 2 ;;
    --profile=*)    PROFILE="${1#*=}"; shift ;;
    --skip-backup)  SKIP_BACKUP=1; shift ;;
    --keep-extract) KEEP_EXTRACT=1; shift ;;
    --force)        FORCE=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "$@" >&2; exit 1; }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

[ -n "$PROFILE" ] || die "no profile. Pass --profile NAME or set \$HEALTH_PROFILE.
The profile decides which database is written and which phone is allowed to write it."

# Prefer a project venv, fall back to whatever python3 is on PATH.
if [ -x "$PROJECT/.venv/bin/python" ]; then
  PY="$PROJECT/.venv/bin/python"
else
  PY="$(command -v python3)" || die "no python3 on PATH"
fi

field() { "$PY" "$PROJECT/athlete_profile.py" "$PROFILE" --get "$1" 2>/dev/null || true; }

DB="$(field db)"
[ -n "$DB" ] || die "profile $PROFILE has no db path"
BACKUP_ROOT="${HEALTH_BACKUP_ROOT:-$(field backup_root)}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/HealthBackups}"
BACKUP_ROOT="${BACKUP_ROOT/#\~/$HOME}"
DB="${DB/#\~/$HOME}"
WANT_UDID="$(field device_udid)"

echo "profile:  $PROFILE"
echo "database: $DB"

# Newest per-device backup under a root, chosen by Manifest.db mtime -- the same
# rule extract_health.py uses, so the directory validated here is the one converted.
newest_backup() {
  local root=$1 d newest="" newest_t=0 t
  [ -d "$root" ] || return 0
  for d in "$root"/*/; do
    [ -f "$d/Manifest.db" ] || continue
    t=$(stat -f %m "$d/Manifest.db" 2>/dev/null) || continue
    if [ "$t" -gt "$newest_t" ]; then newest_t=$t; newest="${d%/}"; fi
  done
  [ -n "$newest" ] && printf '%s\n' "$newest"
}

# Not piped into `head`: under `pipefail` the second producer would take SIGPIPE
# and fail the whole command substitution, aborting the script with no message.
resolve_backup() {
  local s
  s=$(newest_backup "$BACKUP_ROOT")
  # Fall back to the Finder/iTunes backup, which sits four levels deep.
  [ -n "$s" ] || s=$(newest_backup "$HOME/Library/Application Support/MobileSync/Backup")
  printf '%s\n' "$s"
}

# --- what do we already have? -------------------------------------------------
PREV_MAX=""
if [ -f "$DB" ]; then
  PREV_MAX=$("$PY" "$PROJECT/hq.py" --db "$DB" \
      "SELECT max(local_date) FROM workout_summary" 2>/dev/null \
      | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 || true)
fi
echo "current:  ${PREV_MAX:-<none>}"

# --- backup -------------------------------------------------------------------
if [ "$SKIP_BACKUP" -eq 0 ]; then
  step "backup"
  if [ -x "$PROJECT/.venv/bin/pymobiledevice3" ]; then
    PMD="$PROJECT/.venv/bin/pymobiledevice3"
  else
    PMD="$(command -v pymobiledevice3)" || die "pymobiledevice3 not installed (pip install -r requirements-sync.txt)"
  fi

  "$PMD" usbmux list 2>/dev/null | grep -q DeviceName \
    || die "no iPhone on USB -- plug it in and unlock it"

  # Health data is only present in ENCRYPTED backups. Never flip this off.
  [ "$("$PMD" backup2 encryption 2>/dev/null | tail -1)" = "on" ] \
    || die "backup encryption is off; Health data will not be included"

  mkdir -p "$BACKUP_ROOT"
  BACKUP_LOG=$(mktemp "${TMPDIR:-/tmp}/healthbackup.XXXXXX")

  # --only-regex keeps just the Health files on disk. It does NOT shorten the
  # transfer: the phone still streams everything.
  # Do NOT add --patch-manifest -- it demands --password at transfer time and
  # strips Manifest.db entries, which makes every later run a full one again.
  #
  # pymobiledevice3 EXITS 0 even when the transfer dies mid-stream: it logs
  # "Connection was terminated abruptly" and returns success. The exit code alone
  # is therefore not a completion signal -- the log and Status.plist are checked below.
  set +e
  "$PMD" backup2 backup --only-regex 'Health/healthdb' "$BACKUP_ROOT" 2>&1 | tee "$BACKUP_LOG"
  rc=${PIPESTATUS[0]}
  set -e
  [ "$rc" -eq 0 ] || die "backup exited $rc"

  if grep -qE "terminated abruptly|ERROR" "$BACKUP_LOG"; then
    echo "backup reported an error despite exiting 0:" >&2
    grep -E "terminated abruptly|ERROR" "$BACKUP_LOG" | tail -5 >&2
    die "the transfer did not complete -- re-run with the phone plugged in and unlocked"
  fi
  rm -f "$BACKUP_LOG"
fi

# --- verify the backup is finished, from the right phone, and newer than the DB ---
step "verify"
SOURCE=$(resolve_backup)
[ -n "$SOURCE" ] || die "no backup with a Manifest.db under $BACKUP_ROOT or MobileSync"
echo "source: $SOURCE"

SNAP=$(plutil -extract SnapshotState raw -o - "$SOURCE/Status.plist" 2>/dev/null || echo "?")
[ "$SNAP" = "finished" ] \
  || die "backup is still in progress (SnapshotState=$SNAP) -- nothing can be decrypted yet"

# THE guard for a household with two phones. Without it, whichever phone happens
# to be on the USB bus is backed up and then written over whichever database this
# profile names -- silently, with plausible data. Compare the backup's own target
# device against the profile before anything is extracted or converted.
GOT_UDID=$(plutil -extract "Target Identifier" raw -o - "$SOURCE/Info.plist" 2>/dev/null || echo "")
if [ -n "$WANT_UDID" ]; then
  [ "$GOT_UDID" = "$WANT_UDID" ] || die "this backup is from a different phone.
  backup device: ${GOT_UDID:-<unreadable>}
  profile $PROFILE expects: $WANT_UDID
  database that would have been overwritten: $DB
Nothing was touched. Plug in the right phone, or fix device_udid in the profile."
  echo "device: $GOT_UDID (matches profile)"
else
  echo "device: ${GOT_UDID:-<unreadable>} -- profile $PROFILE sets no device_udid," >&2
  echo "        so nothing is checking that this is the right phone. Set it." >&2
fi

# A crashed transfer leaves the previous backup intact, so the pipeline would
# happily rebuild the same database and report a quiet week. Refuse that instead.
if [ "$FORCE" -eq 0 ] && [ -f "$DB" ]; then
  BK_T=$(stat -f %m "$SOURCE/Manifest.db")
  DB_T=$(stat -f %m "$DB")
  if [ "$BK_T" -le "$DB_T" ]; then
    echo "backup:   $(stat -f %Sm "$SOURCE/Manifest.db")" >&2
    echo "database: $(stat -f %Sm "$DB")" >&2
    die "backup is no newer than the database -- converting it would look like a quiet week.
Re-run the backup, or pass --force to convert anyway."
  fi
fi

# --- extract ------------------------------------------------------------------
step "extract"
EXTRACT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/healthdb.XXXXXX")
cleanup() { [ "$KEEP_EXTRACT" -eq 1 ] || rm -rf "$EXTRACT_DIR"; }
trap cleanup EXIT

# Pass the source explicitly: extract_health.py otherwise re-resolves it on its
# own and could pick a different backup than the one verified above.
"$PY" "$SKILL_DIR/scripts/extract_health.py" "$SOURCE" --out "$EXTRACT_DIR" --profile "$PROFILE"

[ -f "$EXTRACT_DIR/healthdb_secure.sqlite" ] \
  || die "extraction produced no healthdb_secure.sqlite"

# --- convert ------------------------------------------------------------------
step "convert"
TMP_DB="$DB.new"
rm -f "$TMP_DB"
mkdir -p "$(dirname "$DB")"
"$PY" "$PROJECT/healthdb_to_duckdb.py" "$EXTRACT_DIR" --db "$TMP_DB" --profile "$PROFILE"
mv -f "$TMP_DB" "$DB"

# --- what arrived -------------------------------------------------------------
step "new since ${PREV_MAX:-the beginning}"
if [ -n "$PREV_MAX" ]; then
  WHERE="WHERE local_date > DATE '$PREV_MAX'"
else
  WHERE="WHERE local_date >= current_date - 14"
fi
"$PY" "$PROJECT/hq.py" --db "$DB" --profile "$PROFILE" "
  SELECT local_date, left(local_time::VARCHAR,5) AS at, activity,
         round(duration_min,1) AS min, round(distance_km,2) AS km,
         round(avg_hr) AS avg_hr, round(max_hr) AS max_hr
  FROM workout_summary $WHERE ORDER BY start_date"

echo
echo "database: $DB"
