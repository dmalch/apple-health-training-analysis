# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

Scripts that turn an Apple Health export into a queryable DuckDB database and a
training-load report. Two ingestion routes — an XML export from the Health app, and
an encrypted iPhone backup — converge on the same views, so the analysis scripts run
unchanged against either.

There is no package and nothing to install. The scripts are run out of a clone.

## The three rules that matter

1. **Configuration is data, not code.** Everything per-person lives in a TOML
   profile: what the activity labels mean, the max-HR anchor, paths, time zone,
   device UDID. Never add a person's value as a default in a script. If something
   needs to differ between two people, it is a profile field.

2. **A wrong anchor does not fail, it lies.** Every zone is a percentage of
   `max_hr`. A borrowed or defaulted anchor produces a complete, plausible, wrong
   report. That is why `analyze_intervals.py` refuses to run without one rather than
   picking a number.

3. **Never commit health data.** `data*/` and `profiles/*.toml` are gitignored.
   Before adding any example, sample output or test fixture, check it carries no
   real dates, weights, heart rates or device identifiers. Tests use synthetic
   fixtures only.

## Before changing analysis code

Reports must be reproducible. Two checks, both cheap:

- Run `analyze.py` twice against the same database and diff. Identical output is the
  contract — a set iterated into a table once broke it, and the same export produced
  a different report on every run.
- Run the old and new code against the same database and diff. Any change in the
  numbers has to be explained, not discovered later.

## Verification

```sh
./.venv/bin/python -m unittest discover -p 'test_*.py'
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
./.venv/bin/pyright
```

All three run in CI and are required to merge.

`pyright` runs in basic mode with three rules off — `reportOptionalSubscript`,
`reportOptionalOperand` and `reportArgumentType`. They fire almost only on untyped
external data, where "a value or nothing" is the shape of the source rather than a
defect. Everything else is an error; do not widen the exemptions to make new code
pass.

## Where knowledge goes

Findings do not stay in the transcript:

- an operational gotcha about the phone or the backup → `.claude/skills/health-sync/SKILL.md`
- a fact about the export or the schema → `docs/apple-health.md`
- a decision about how something is measured → `docs/method.md`

Each of those documents what it cost to learn. Add to them in the same voice: state
the trap, then the measurement that settled it.
