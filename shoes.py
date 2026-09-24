#!/usr/bin/env python3
"""
Mileage per pair of shoes.

    python3 shoes.py --profile NAME

Apple Health records no gear, so a pair is described in the profile by the
dates it was in use and the activities it is worn for:

    [[shoes]]
    name = "Road trainer"
    since = 2026-05-01
    limit_km = 600

Its mileage is then every workout of those activities from `since` to
`retired`, both days included, plus any `start_km` the pair came with. Workouts
from a profile's `duplicates` sources are left out, so a second app logging the
same run does not double the wear. Two pairs in use on the same days for the
same activity would both be charged every such workout; the report says so
rather than guessing which pair was worn.
"""

import argparse

import duckdb

import athlete_profile
import hq


def mileage(con, pairs, duplicates=()):
    """One row per pair: workouts, km, hours, last workout and share of its limit."""
    rows = []
    for s in pairs:
        n, km, minutes, last = con.execute(
            """
            SELECT count(*), coalesce(sum(distance_km), 0), coalesce(sum(duration_min), 0),
                   max(local_date)
            FROM workouts
            WHERE list_contains(?, activity)
              AND local_date >= ?
              AND (CAST(? AS DATE) IS NULL OR local_date <= ?)
              AND NOT list_contains(?, coalesce(source_name, ''))
            """,
            [s["activities"], s["since"], s["retired"], s["retired"], list(duplicates)],
        ).fetchone()
        km = float(km) + s["start_km"]
        rows.append(
            {
                "name": s["name"],
                "since": s["since"],
                "retired": s["retired"],
                "workouts": n,
                "km": km,
                "hours": float(minutes) / 60,
                "last": last,
                "limit_km": s["limit_km"],
                "of_limit": km / s["limit_km"] if s["limit_km"] else None,
            }
        )
    return rows


def overlaps(pairs):
    """Name pairs in use on a shared day for a shared activity."""
    out = []
    for i, a in enumerate(pairs):
        for b in pairs[i + 1 :]:
            if not set(a["activities"]) & set(b["activities"]):
                continue
            a_end, b_end = a["retired"], b["retired"]
            if (a_end is None or b["since"] <= a_end) and (b_end is None or a["since"] <= b_end):
                out.append((a["name"], b["name"]))
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", default=None, help="Defaults to the profile database.")
    athlete_profile.add_argument(ap)
    args = ap.parse_args()
    profile = athlete_profile.from_args(args)

    pairs = profile["shoes"]
    if not pairs:
        raise SystemExit(f"{profile['path']}: no [[shoes]] entries. See profiles/example.toml.")

    con = duckdb.connect(args.db or profile["db"], read_only=True)
    rows = mileage(con, pairs, profile["duplicates"])

    cols = ["shoe", "for", "since", "until", "workouts", "km", "hours", "last", "of limit"]
    table = []
    for r, s in zip(rows, pairs, strict=True):
        limit = (
            f"{r['of_limit']:.0%} of {r['limit_km']:.0f} km" if r["of_limit"] is not None else ""
        )
        table.append(
            [
                r["name"],
                ", ".join(s["activities"]),
                r["since"],
                r["retired"] or "in use",
                r["workouts"],
                f"{r['km']:.1f}",
                f"{r['hours']:.1f}",
                r["last"] or "-",
                limit,
            ]
        )
    hq.render_table(cols, table)

    for a, b in overlaps(pairs):
        print(f"\nwarning: {a!r} and {b!r} are in use on the same days for the same activity;")
        print("         every such workout is counted for both.")


if __name__ == "__main__":
    try:
        main()
    except athlete_profile.ProfileError as exc:
        raise SystemExit(f"profile error: {exc}") from None
