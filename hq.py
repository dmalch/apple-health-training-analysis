#!/usr/bin/env python3
"""
Run SQL against the Health DuckDB database.

    python3 hq.py "SELECT * FROM workout_summary ORDER BY start_date DESC LIMIT 10"
    python3 hq.py --file q.sql --format csv
    python3 hq.py --tables
    python3 hq.py --schema hr

Opens read-only, so it is safe to run against a database another process is
querying. The session time zone comes from the profile: every timestamp in
the database is a true instant (TIMESTAMPTZ), and without this it would render
in UTC and every morning workout would look like it happened an hour earlier.
Columns named local_date / local_time carry the wall-clock reading instead and
are unaffected.
"""

import argparse
import csv
import sys

import duckdb

import athlete_profile


def render_table(cols, rows, out=sys.stdout):
    if not rows:
        print("(no rows)", file=out)
        return
    cells = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(cols)]
    print(" | ".join(c.ljust(w) for c, w in zip(cols, widths, strict=False)), file=out)
    print("-+-".join("-" * w for w in widths), file=out)
    for row in cells:
        print(" | ".join(v.ljust(w) for v, w in zip(row, widths, strict=False)), file=out)
    print(f"\n({len(rows)} rows)", file=out)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("sql", nargs="?")
    ap.add_argument("--db", default=None, help="Defaults to the profile database.")
    ap.add_argument("--file", help="read the query from a file")
    ap.add_argument("--format", choices=("table", "csv"), default="table")
    ap.add_argument("--tz", default=None, help="Defaults to the profile timezone.")
    ap.add_argument("--tables", action="store_true", help="list tables and views")
    ap.add_argument("--schema", metavar="NAME", help="describe one table or view")
    athlete_profile.add_argument(ap, required=False)
    args = ap.parse_args()
    profile = athlete_profile.from_args(args, required=False)

    db = args.db or athlete_profile.field(profile, "db")
    if not db:
        raise SystemExit("no database. Pass --db PATH or --profile NAME.")
    tz = args.tz or athlete_profile.field(profile, "timezone") or "UTC"

    con = duckdb.connect(db, read_only=True)
    con.execute(f"SET TimeZone='{tz}'")

    if args.tables:
        sql = (
            "SELECT table_name AS name, table_type AS type FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_type, table_name"
        )
    elif args.schema:
        sql = f"DESCRIBE {args.schema}"
    elif args.file:
        with open(args.file) as fh:
            sql = fh.read()
    elif args.sql:
        sql = args.sql
    else:
        sql = sys.stdin.read()

    con.execute(sql)
    cols = [d[0] for d in con.description]
    rows = con.fetchall()

    if args.format == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(cols)
        w.writerows(rows)
    else:
        render_table(cols, rows)


if __name__ == "__main__":
    main()
