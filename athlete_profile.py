#!/usr/bin/env python3
"""
Per-person configuration, loaded from a TOML file.

Apple's activity labels do not mean the same thing for two people. The same
`CrossTraining` value is a barbell class on one watch and a spin class on
another; `HighIntensityIntervalTraining` is a studio class for one person and a
2021 phone-app workout for the next. Every classification that depends on
knowing what a label stands for therefore lives in a named profile rather than
being assumed by the code.

The same goes for the max-HR anchor. Zones are cut as percentages of it, so an
anchor borrowed from somebody else does not fail -- it silently produces
plausible, wrong numbers for every session you have ever recorded.

Profiles are read, never written, so this uses stdlib `tomllib` (Python 3.11+).

Resolution order for `--profile NAME`:

    $HEALTH_PROFILES/NAME.toml      (if the variable is set)
    ./profiles/NAME.toml            (relative to the current directory)
    <this file's dir>/profiles/NAME.toml

An explicit `--profile-file PATH` skips the search entirely.

As a shell helper, one field can be printed on stdout:

    python3 athlete_profile.py NAME --get db
"""

import os
import tomllib
from datetime import date, datetime
from pathlib import Path

# Percentage-of-max bands. Overridable per profile, but the defaults are the
# conventional five-zone split and changing them makes reports incomparable.
DEFAULT_ZONES = [
    ("Z1 recovery", 0.00, 0.60),
    ("Z2 aerobic", 0.60, 0.70),
    ("Z3 tempo", 0.70, 0.80),
    ("Z4 threshold", 0.80, 0.90),
    ("Z5 max", 0.90, 9.99),
]

# Chest-strap samples appear under these source names in the raw HeartRate
# stream (the workout itself is still owned by the watch). Both spellings are
# the same thing: the export uses the phone's locale, so a German phone writes
# "Bluetooth-Gerat" for the device an English one calls "Bluetooth Device".
DEFAULT_STRAP_SOURCES = ["Bluetooth Device", "Bluetooth-Gerät"]

REQUIRED = ("timezone", "max_hr", "strength", "hybrid")

KNOWN = {
    "name",
    "timezone",
    "data_dir",
    "db",
    "max_hr",
    "strength",
    "hybrid",
    "duplicates",
    "cross_talk",
    "strap_sources",
    "zones",
    "shoes",
    "backup_root",
    "device_udid",
    "keychain_service",
    "op_ref",
    "op_account",
}

LIST_FIELDS = ("strength", "hybrid", "duplicates", "cross_talk", "strap_sources")


class ProfileError(Exception):
    """Raised for anything wrong with a profile file, with a path in the message."""


def candidates(name):
    """Every path `name` could resolve to, in the order they are tried."""
    here = Path(__file__).resolve().parent
    out = []
    env = os.environ.get("HEALTH_PROFILES")
    if env:
        out.append(Path(env).expanduser() / f"{name}.toml")
    out.append(Path.cwd() / "profiles" / f"{name}.toml")
    out.append(here / "profiles" / f"{name}.toml")
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def find(name):
    tried = candidates(name)
    for path in tried:
        if path.is_file():
            return path
    raise ProfileError(
        f"no profile named {name!r}. Looked in:\n  "
        + "\n  ".join(str(p) for p in tried)
        + "\n\nCopy profiles/example.toml to one of those paths and edit it."
    )


def load(name=None, path=None):
    """Read a profile and return it as a validated dict."""
    src = Path(path).expanduser() if path else find(name)
    try:
        with open(src, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{src}: not valid TOML -- {exc}") from exc
    except OSError as exc:
        raise ProfileError(f"{src}: cannot be read -- {exc}") from exc

    # An unknown key is almost always a typo, and a typo in a config file is
    # silent: the setting you meant simply never takes effect. Refuse instead.
    unknown = sorted(set(raw) - KNOWN)
    if unknown:
        raise ProfileError(
            f"{src}: unknown field(s) {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(KNOWN))}"
        )

    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ProfileError(
            f"{src}: missing required field(s) {', '.join(missing)}. "
            "See profiles/example.toml for what each one means."
        )

    prof = dict(raw)
    prof.setdefault("name", src.stem)
    prof.setdefault("data_dir", "data")
    prof.setdefault("db", str(Path(prof["data_dir"]) / "health.duckdb"))
    prof.setdefault("strap_sources", list(DEFAULT_STRAP_SOURCES))
    prof.setdefault("duplicates", [])
    prof.setdefault("cross_talk", [])

    for key in LIST_FIELDS:
        value = prof.get(key)
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ProfileError(f"{src}: {key} must be a list of strings")

    # "strap" means: derive the anchor from chest-strap samples at analysis time.
    anchor = prof["max_hr"]
    if isinstance(anchor, str):
        if anchor != "strap":
            raise ProfileError(
                f'{src}: max_hr must be a number or the string "strap", got {anchor!r}'
            )
        prof["max_hr"] = None
    elif isinstance(anchor, bool) or not isinstance(anchor, (int, float)):
        raise ProfileError(f'{src}: max_hr must be a number or "strap", got {anchor!r}')
    else:
        prof["max_hr"] = float(anchor)
        if not 120 <= prof["max_hr"] <= 230:
            raise ProfileError(
                f"{src}: max_hr {prof['max_hr']:.0f} is outside 120-230. "
                "That is almost certainly a typo, and every zone below would be wrong."
            )

    prof["zones"] = parse_zones(prof.get("zones"), src)
    prof["shoes"] = parse_shoes(prof.get("shoes"), src)
    prof["path"] = str(src)
    return prof


def parse_zones(zones, src):
    """Validate custom zone bands, or hand back the default five.

    Every check here exists because the failure it prevents is silent. A band
    written the wrong way round, a gap between two bands, or a first band that
    does not start at zero all produce a report where some minutes belong to no
    zone at all -- the totals simply come out short, with no error anywhere.
    """
    if zones is None:
        return [tuple(z) for z in DEFAULT_ZONES]
    if not isinstance(zones, list) or not zones:
        raise ProfileError(f"{src}: zones must be a non-empty array of tables")

    out = []
    for i, z in enumerate(zones):
        try:
            out.append((str(z["name"]), float(z["lo"]), float(z["hi"])))
        except (TypeError, KeyError, ValueError) as exc:
            raise ProfileError(f"{src}: zones[{i}] needs name, lo and hi -- {exc}") from exc

    for name, lo, hi in out:
        if not lo < hi:
            raise ProfileError(
                f"{src}: zone {name!r} has lo {lo} and hi {hi}. A band that does not "
                "run upwards can never match a sample, so its time would be counted "
                "in no zone at all."
            )

    for (na, _, hi), (nb, lo, _) in zip(out, out[1:], strict=False):
        if lo < hi - 1e-9:
            raise ProfileError(
                f"{src}: zone {nb!r} starts at {lo}, below where {na!r} ends ({hi}). "
                "Overlapping bands would count the same minute twice."
            )
        if lo > hi + 1e-9:
            raise ProfileError(
                f"{src}: zones leave a gap between {na!r} (hi {hi}) and {nb!r} (lo {lo}); "
                "time in that band would be counted in no zone at all"
            )

    if out[0][1] > 1e-9:
        raise ProfileError(
            f"{src}: the first zone starts at {out[0][1]}, not 0. Every heart rate "
            "below that would be counted in no zone at all."
        )

    return out


SHOE_FIELDS = {"name", "since", "retired", "limit_km", "start_km", "activities"}


def _shoe_date(value, what, src):
    """A TOML date, or the same written as an ISO string."""
    if isinstance(value, datetime):
        raise ProfileError(f"{src}: {what} must be a date, not a date-time ({value})")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ProfileError(f"{src}: {what} must be a date such as 2026-05-01, got {value!r}")


def parse_shoes(shoes, src):
    """Validate the shoe list, one entry per pair, in use from `since` to `retired`.

    Apple Health records no gear, so a pair's mileage is every run inside its date
    range. A misread entry would not fail: a typo'd `limit` would never warn, and a
    date read as text would count nothing. So every field is checked here.
    """
    if shoes is None:
        return []
    if not isinstance(shoes, list):
        raise ProfileError(f"{src}: shoes must be an array of tables ([[shoes]])")

    out = []
    for i, s in enumerate(shoes):
        if not isinstance(s, dict):
            raise ProfileError(f"{src}: shoes[{i}] must be a table")
        name = s.get("name")
        if not isinstance(name, str) or not name:
            raise ProfileError(f"{src}: shoes[{i}] needs a name")
        unknown = sorted(set(s) - SHOE_FIELDS)
        if unknown:
            raise ProfileError(
                f"{src}: shoe {name!r} has unknown field(s) {', '.join(unknown)}. "
                f"Known fields: {', '.join(sorted(SHOE_FIELDS))}"
            )
        if "since" not in s:
            raise ProfileError(f"{src}: shoe {name!r} needs since, the first day in use")
        since = _shoe_date(s["since"], f"shoe {name!r} since", src)
        retired = s.get("retired")
        if retired is not None:
            retired = _shoe_date(retired, f"shoe {name!r} retired", src)
            if retired < since:
                raise ProfileError(
                    f"{src}: shoe {name!r} is retired on {retired}, before it was "
                    f"first used on {since}"
                )

        limit_km = s.get("limit_km")
        if limit_km is not None:
            if isinstance(limit_km, bool) or not isinstance(limit_km, (int, float)):
                raise ProfileError(f"{src}: shoe {name!r} limit_km must be a number")
            if limit_km <= 0:
                raise ProfileError(f"{src}: shoe {name!r} limit_km must be above 0")
            limit_km = float(limit_km)

        start_km = s.get("start_km", 0.0)
        if isinstance(start_km, bool) or not isinstance(start_km, (int, float)) or start_km < 0:
            raise ProfileError(f"{src}: shoe {name!r} start_km must be a number, 0 or more")

        activities = s.get("activities", ["Running"])
        if (
            not isinstance(activities, list)
            or not activities
            or any(not isinstance(a, str) for a in activities)
        ):
            raise ProfileError(
                f"{src}: shoe {name!r} activities must be a non-empty list of strings"
            )

        if any(o["name"] == name for o in out):
            raise ProfileError(f"{src}: two shoes are called {name!r}; names must differ")

        out.append(
            {
                "name": name,
                "since": since,
                "retired": retired,
                "limit_km": limit_km,
                "start_km": float(start_km),
                "activities": list(activities),
            }
        )
    return out


def add_argument(ap, required=True):
    """Attach the two selection flags to an argparse parser."""
    ap.add_argument(
        "--profile",
        default=os.environ.get("HEALTH_PROFILE"),
        required=required and not os.environ.get("HEALTH_PROFILE"),
        help="Whose export this is. Decides what the activity labels "
        "mean and which max-HR anchor the zones are cut from. "
        "Defaults to $HEALTH_PROFILE.",
    )
    ap.add_argument(
        "--profile-file",
        default=None,
        help="Path to a profile TOML, instead of looking one up by name.",
    )


def field(profile, key):
    """One field of an optional profile, or None when none was selected."""
    if not profile:
        return None
    return profile.get(key)


def from_args(args, required=True):
    """Load the profile an argparse namespace selects, or None when optional."""
    path = getattr(args, "profile_file", None)
    name = getattr(args, "profile", None)
    if not path and not name:
        if required:
            raise ProfileError(
                "no profile selected. Pass --profile NAME, --profile-file PATH, "
                "or set $HEALTH_PROFILE."
            )
        return None
    return load(name=name, path=path)


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="Print one field of a profile.")
    ap.add_argument("name")
    ap.add_argument("--get", required=True, help="Field to print, e.g. db or device_udid")
    args = ap.parse_args(argv)
    prof = load(args.name)
    value = prof.get(args.get)
    if value is None:
        raise SystemExit(f"{prof['path']}: no value for {args.get!r}")
    if isinstance(value, list):
        print("\n".join(str(v) for v in value))
    else:
        print(value)


if __name__ == "__main__":
    try:
        main()
    except ProfileError as exc:
        raise SystemExit(f"profile error: {exc}") from None
