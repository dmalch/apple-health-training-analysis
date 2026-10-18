#!/usr/bin/env bash
# Refresh a profile's HealthKit-built database from the HealthExport app.
#
#   launch the app on the phone with -autoExport -> it exports and quits ->
#   copy its batches/ off -> rebuild the database from every batch
#
# No backup, no backup password, no Finder. The app has to be installed on the
# phone first (ios/HealthExport/README.md), and the phone must be on USB and
# unlocked: HealthKit cannot be read while the phone is locked.
#
# Usage:  sync_healthkit.sh --profile NAME [--db PATH] [--days N] [--full] [--skip-launch] [--compare DB]
#   --profile NAME  supplies device_udid (required), data_dir and the time zone
#   --db PATH       database to build; default <data_dir>/health-hk.duckdb
#   --days N        how far back the FIRST export reaches; later ones are incremental
#   --full          forget the app's anchors, so this export is a first one again
#   --skip-launch   only copy and rebuild, from what the phone already holds
#   --compare DB    print the differences against a backup-built database

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${HEALTH_PROJECT:-$(cd "$SKILL_DIR/../../.." && pwd)}"

PROFILE="${HEALTH_PROFILE:-}"
DB=""
DAYS=30
SKIP_LAUNCH=0
RESET=NO
COMPARE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)     PROFILE="${2:-}"; shift 2 ;;
    --profile=*)   PROFILE="${1#*=}"; shift ;;
    --db)          DB="${2:-}"; shift 2 ;;
    --days)        DAYS="${2:-}"; shift 2 ;;
    --full)        RESET=YES; shift ;;
    --skip-launch) SKIP_LAUNCH=1; shift ;;
    --compare)     COMPARE="${2:-}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "$@" >&2; exit 1; }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

[ -n "$PROFILE" ] || die "no profile. Pass --profile NAME or set \$HEALTH_PROFILE."
if [ -x "$PROJECT/.venv/bin/python" ]; then
  PY="$PROJECT/.venv/bin/python"
else
  PY="$(command -v python3)" || die "no python3 on PATH"
fi
field() { "$PY" "$PROJECT/athlete_profile.py" "$PROFILE" --get "$1" 2>/dev/null || true; }

# The profile's phone and no other: devicectl would otherwise pick whichever
# phone answers, and with two in the house that writes one person's data into
# the other's folder.
UDID="$(field device_udid)"
[ -n "$UDID" ] || die "profile $PROFILE has no device_udid, which is what keeps another phone out"
DATA_DIR="$(field data_dir)"
DATA_DIR="${DATA_DIR/#\~/$HOME}"
EXPORT_DIR="${HEALTHKIT_EXPORT_DIR:-$DATA_DIR/healthkit}"
DB="${DB:-$DATA_DIR/health-hk.duckdb}"
DB="${DB/#\~/$HOME}"

LOCAL_XCCONFIG="$PROJECT/ios/HealthExport/Local.xcconfig"
BUNDLE="${HEALTHEXPORT_BUNDLE:-}"
if [ -z "$BUNDLE" ] && [ -f "$LOCAL_XCCONFIG" ]; then
  BUNDLE="$(sed -n 's/^PRODUCT_BUNDLE_IDENTIFIER *= *//p' "$LOCAL_XCCONFIG" | tail -1)"
fi
[ -n "$BUNDLE" ] || die "no bundle id: set PRODUCT_BUNDLE_IDENTIFIER in $LOCAL_XCCONFIG or \$HEALTHEXPORT_BUNDLE"

echo "profile:  $PROFILE"
echo "phone:    $UDID"
echo "app:      $BUNDLE"
echo "exports:  $EXPORT_DIR"
echo "database: $DB"

if [ "$SKIP_LAUNCH" -eq 0 ]; then
  step "export on the phone"
  log="$(mktemp -t healthkit-export)"
  # --console waits for the app to quit, which it does after an -autoExport.
  # Its stdout carries the per-type counts and the final EXPORT DONE line.
  xcrun devicectl device process launch --device "$UDID" --terminate-existing --console \
    "$BUNDLE" -- -autoExport YES -days "$DAYS" -resetAnchors "$RESET" 2>&1 | tee "$log" || true
  if ! grep -q '^EXPORT DONE' "$log"; then
    grep -q 'EXPORT FAILED' "$log" && die "the app reported a failure (above)."
    die "no EXPORT DONE from the app. Is the phone unlocked, the app installed, and its
7-day profile still valid? A first run also needs someone to tap Allow on the Health sheet."
  fi
fi

step "copy batches off the phone"
mkdir -p "$EXPORT_DIR"
staging="$(mktemp -d -t healthkit-copy)"
xcrun devicectl device copy from --device "$UDID" --domain-type appDataContainer \
  --domain-identifier "$BUNDLE" --source Documents/batches --destination "$staging/batches" >/dev/null
# Only finished batches, and never over one already here: a batch is immutable
# once its manifest exists, so an existing folder is the same data.
mkdir -p "$EXPORT_DIR/batches"
new=0
for b in "$staging"/batches/*/; do
  b="${b%/}"
  [ -f "$b/manifest.json" ] || continue
  name="$(basename "$b")"
  [ -d "$EXPORT_DIR/batches/$name" ] && continue
  mv "$b" "$EXPORT_DIR/batches/$name"
  new=$((new + 1))
done
rm -rf "$staging"
echo "$new new batch(es); $(find "$EXPORT_DIR/batches" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ') in all"

step "rebuild $DB"
# Built beside the old one and swapped in on success, so a failed run keeps it.
rm -f "$DB.new"
compare=()
[ -n "$COMPARE" ] && compare=(--compare "${COMPARE/#\~/$HOME}")
"$PY" "$PROJECT/healthkit_to_duckdb.py" "$EXPORT_DIR" --profile "$PROFILE" --db "$DB.new" \
  ${compare[@]+"${compare[@]}"}
mv "$DB.new" "$DB"
