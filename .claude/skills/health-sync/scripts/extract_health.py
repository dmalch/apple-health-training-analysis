#!/usr/bin/env python3
"""Pull the two Health SQLite databases out of an encrypted iOS backup.

The samples live in ``healthdb_secure.sqlite``; the *names* of the recording
devices live in ``healthdb.sqlite``. Both are needed -- without the second one
every device comes out as a bare product code (``Watch7,5``-style) and the strap-vs-watch
split that the HR zones are anchored to collapses.

The password is never taken on the command line. Resolution order:

1. ``$IOS_BACKUP_PASSWORD``
2. macOS login keychain, under the service name the profile gives
   (``keychain_service``, default ``ios-backup``)
3. a 1Password item, only if the profile sets ``op_ref``; the value is then
   cached into the keychain so later runs need no password manager at all

Run with the project venv, which is where pyiosbackup lives:

    .venv/bin/python extract_health.py <backup-dir> --out <dir> --profile NAME
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# repo root: scripts -> health-sync -> skills -> .claude -> root
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import athlete_profile

DOMAIN = "HealthDomain"
TARGETS = ("Health/healthdb_secure.sqlite", "Health/healthdb.sqlite")
DEFAULT_KEYCHAIN_SERVICE = "ios-backup"


def _run(cmd: list[str]) -> str | None:
    """Return stripped stdout, or None if the command is missing or fails."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def resolve_password(profile=None) -> str:
    env = os.environ.get("IOS_BACKUP_PASSWORD")
    if env:
        return env

    service = (profile or {}).get("keychain_service") or DEFAULT_KEYCHAIN_SERVICE
    kc = _run(["security", "find-generic-password", "-s", service, "-w"])
    if kc:
        return kc

    op_ref = (profile or {}).get("op_ref")
    op_account = (profile or {}).get("op_account")
    op = None
    if op_ref:
        cmd = ["op", "read", op_ref]
        if op_account:
            cmd += ["--account", op_account]
        op = _run(cmd)
    if op:
        # Cache it so the next run does not need the desktop app at all.
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                os.environ.get("USER", ""),
                "-s",
                service,
                "-U",
                "-w",
                op,
                "-l",
                "iOS backup password (apple-health-training-analysis)",
            ],
            capture_output=True,
        )
        return op

    sys.exit(
        "No backup password. Set $IOS_BACKUP_PASSWORD, or store it once with:\n"
        f'  security add-generic-password -a "$USER" -s {service} -w\n'
        "A password manager can supply it instead: set op_ref in the profile."
    )


def newest_backup(root: Path) -> Path | None:
    """The device-udid subdirectory holding a finished backup, newest first."""
    if not root.is_dir():
        return None
    candidates = [d for d in root.iterdir() if d.is_dir() and (d / "Manifest.db").is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: (d / "Manifest.db").stat().st_mtime)


def default_source(backup_root=None) -> Path:
    """Prefer a purpose-built backup, fall back to the Finder/iTunes one."""
    home = Path.home()
    roots = []
    if backup_root:
        roots.append(Path(backup_root).expanduser())
    roots += [home / "HealthBackups", home / "Library/Application Support/MobileSync/Backup"]
    for root in roots:
        found = newest_backup(root)
        if found:
            return found
    sys.exit(
        "No finished backup found under ~/HealthBackups or MobileSync.\n"
        "A backup without Manifest.db is still uploading -- wait for it to finish."
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "backup",
        nargs="?",
        type=Path,
        help="backup directory (defaults to the newest finished one)",
    )
    ap.add_argument("--out", type=Path, required=True, help="destination directory")
    athlete_profile.add_argument(ap, required=False)
    args = ap.parse_args()

    profile = athlete_profile.from_args(args, required=False)
    source = args.backup or default_source(athlete_profile.field(profile, "backup_root"))
    if not (source / "Manifest.db").is_file():
        sys.exit(f"{source} has no Manifest.db -- backup is incomplete or still uploading.")

    args.out.mkdir(parents=True, exist_ok=True)

    from pyiosbackup import Backup  # imported late so --help works without the venv

    print(f"backup:  {source}")
    backup = Backup.from_path(source, password=resolve_password(profile))
    print(f"device:  iOS {backup.ios_version}, taken {backup.date}")

    started = time.time()
    for target in TARGETS:
        backup.extract_domain_and_path(DOMAIN, target, path=str(args.out))
        # pyiosbackup writes the basename into `path`, not the relative_path tree.
        written = args.out / Path(target).name
        if not written.is_file():
            sys.exit(f"extraction produced no {written.name} -- wrong backup or bad password?")
        print(f"  {written.name}  {written.stat().st_size / 1e6:,.1f} MB")

    print(f"done in {time.time() - started:.1f}s -> {args.out}")


if __name__ == "__main__":
    main()
