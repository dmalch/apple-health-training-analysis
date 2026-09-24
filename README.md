# apple-health-training-analysis

Turn an Apple Health export into a queryable DuckDB database and a training-load
report — time in heart-rate zones from the per-sample stream, aerobic progression,
interval sessions broken down rep by rep.

Everything runs locally. No account, no server, no upload.

```
Health app export (XML)  ─┐
                          ├─→  DuckDB  ─→  reports, SQL, per-rep interval analysis
encrypted iPhone backup  ─┘
```

Both ingestion routes build the same views, so the analysis scripts run unchanged
against either. The XML route needs nothing but Python; the backup route can run
unattended but needs pairing and the backup password.

## Why not just read the Fitness app

Because it averages. A session that alternates 4 minutes hard with 3 minutes easy has
a meaningless average heart rate, and a month of such sessions has a meaningless
"average intensity". This reads the per-sample stream instead: real time in zone per
session, real boundaries for each repetition, and which sensor actually recorded it —
a chest strap and an optical wrist sensor do not agree, and the difference is large
enough to change what a training week looks like.

## Install

Needs **Python 3.11 or newer** (profiles are TOML, and `tomllib` arrived in 3.11) and
macOS or Linux. The backup route is macOS-only.

```sh
git clone https://github.com/dmalch/apple-health-training-analysis.git
cd apple-health-training-analysis
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Two pinned dependencies: `duckdb`, plus `pytz`, which duckdb itself needs to
hand a `TIMESTAMPTZ` back to Python. The optional backup route adds three more:

```sh
./.venv/bin/pip install -r requirements-sync.txt
```

## Quick start

**1. Export from the Health app.** iPhone → Health → profile picture → *Export All
Health Data*. AirDrop the zip to your machine. (There is no API and no Shortcuts
action for this; the button is the only way.)

**2. Convert it.**

```sh
./.venv/bin/python health_to_duckdb.py ~/Downloads/export.zip --db data/health.duckdb
```

A 2 GB export takes a couple of minutes and lands as a ~300 MB database. `data/` is
gitignored.

**3. Write your profile.** This step is not optional — see below.

```sh
cp profiles/example.toml profiles/me.toml
$EDITOR profiles/me.toml
```

**4. Report.**

```sh
./.venv/bin/python analyze.py --profile me --months 12 > report.md
```

**Прочитать по-русски:** [`docs/ru/QUICKSTART.ru.md`](docs/ru/QUICKSTART.ru.md).

## The profile is the whole configuration

Nothing personal lives in the code. A profile is one TOML file holding what the
scripts cannot know:

- **What your activity labels mean.** The same `CrossTraining` value is a barbell
  class on one watch and a spin class on another. Which labels are resistance work
  changes what gets excluded from the aerobic intensity split.
- **Your max-HR anchor.** Every zone is a percentage of this one number, so an anchor
  borrowed from somebody else does not fail — it silently reports the wrong zone for
  every session you have ever recorded.
- **Where your data lives**, your time zone, and, for the backup route, which phone
  this profile is allowed to read.

`profiles/example.toml` documents every field along with the query that answers it.
Only that template is tracked by git; your own profile is ignored.

## The tools

| Script | What it does |
|---|---|
| `health_to_duckdb.py` | XML export → DuckDB, including GPS routes |
| `healthdb_to_duckdb.py` | iPhone backup → DuckDB, with structured workout blocks |
| `parse_health_export.py` | XML export → CSVs, stdlib only, no dependencies at all |
| `extract_hr_samples.py` | the per-sample heart-rate stream for given days |
| `hq.py` | read-only SQL over the database |
| `analyze.py` | the Markdown training-load report |
| `analyze_intervals.py` | one interval session, rep by rep |
| `shoes.py` | mileage per pair of shoes, from date ranges in the profile |
| `athlete_profile.py` | loads and validates a profile |

## Documentation

- [`docs/apple-health.md`](docs/apple-health.md) — how to get the data out, what the
  schema means, and the traps that cost the most time. Apple documents none of this
  and it moves between iOS releases.
- [`docs/method.md`](docs/method.md) — what the analysis assumes: how the max-HR
  anchor is chosen, which sensor is believed, why session counts mislead.
- [`docs/ru/QUICKSTART.ru.md`](docs/ru/QUICKSTART.ru.md) — быстрый старт по-русски.
- [`.claude/skills/health-sync/SKILL.md`](.claude/skills/health-sync/SKILL.md) — the
  automated backup route, packaged as a Claude Code skill.

## A few things worth knowing before you trust a number

- **`analyze_intervals.py` does not work on an XML-built database.** It needs
  `workout_blocks`, the watch's own block boundaries, and Apple's XML export does not
  carry them. That table only exists on the backup route.
- **Apple's `Segment` events are not laps.** They tile the whole session end to end
  and mean nothing.
- **Weather in `workout_metadata` is a service lookup, not a sensor.** It is present
  on indoor workouts, it is the value at the start rather than an average, and
  humidity is stored ×100.
- **Steps must be de-duplicated, not summed.** The phone and the watch both record all
  day; summing every source produced a median of 24,851 steps/day against 14,902 from
  the watch alone.
- **Recent `daily_metrics` are provisional.** A mid-day sync captures a partial day and
  the next one silently revises it.
- **Two phones in one household is the one failure that loses data.** Set `device_udid`
  in each profile; the sync script then refuses a backup from the wrong phone before
  it writes anything.

## Development

```sh
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m unittest discover -p 'test_*.py'
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
./.venv/bin/pyright
```

All three are required in CI. Tests use synthetic fixtures — no health data is
committed anywhere in this repository, and none should be.

## License

MIT. See [LICENSE](LICENSE).
