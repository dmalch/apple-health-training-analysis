# Method: what the analysis assumes, and why

The numbers in a report are only as good as three decisions: what the activity
labels mean, what the max-HR anchor is, and which sensor is believed. All three are
per-person, which is why they live in a profile rather than in the code.

## The profile is not optional

Apple's activity labels mean different things on different watches. The same
`CrossTraining` value is a barbell class on one and a spin class on the next; the
same `HighIntensityIntervalTraining` is a studio class for one person and a 2021
phone-app workout for another. Nothing in the export says which.

Running an export under the wrong profile does not fail. It produces plausible,
wrong numbers. Write a profile before analysing a new export — `profiles/example.toml`
walks through every field with the query that answers it.

## The max-HR anchor

Every zone is a percentage of one number. Get it wrong and every percentage in every
report is wrong by the same factor, quietly.

Three ways to set it, in descending order of trust:

1. **A measured maximum**, from a chest strap at a real maximal effort.
2. **`max_hr = "strap"`** — derive it from the highest chest-strap reading sustained
   for 10 s inside a workout. Only useful if the strap is actually worn hard.
3. **The highest value ever recorded on a hard session.** This is a *floor*, not a
   measurement. If the true maximum is five beats higher, every percentage is
   roughly two points too high. Write that down somewhere rather than forgetting it.

Two anchors that look reasonable and are not, both of which were tried and discarded:

- **`220 − age`** is a population average with a standard deviation around 10 bpm.
  It understated a real anchor by 16 beats in one case, which inflated every "% of
  max" in the first reports built on it.
- **The 99th percentile of recent session maxima** is a statement about how often you
  go hard, not about your ceiling. It came out 6 beats low on the same data.

### A single high reading is usually an artifact

Filtering on `max(bpm)` alone will find a number that is not a heart rate.

The signature to look for: heart rate sits at a plausible 118, holds a flat 200 for
exactly one minute, and reads 58 four minutes later — *after* the last logged workout
ended. That is R-wave doubling on drying electrodes as a chest strap comes off. Taken
as a maximum it would raise the anchor by eight beats and deflate every percentage in
every report.

Optical wrist peaks above 200 on multi-hour hikes are the same story from the other
sensor: poles, grip, cold and wrist flexion all produce them.

Pull the raw per-sample stream for the day before believing any new maximum.

## Which sensor is believed

**Anchor zones on chest-strap data where it exists.** A strap shows up in the raw
HeartRate stream as source `Bluetooth Device` (or `Bluetooth-Gerät` — the export uses
the phone's locale) at 1 Hz, while the workout itself is still owned by the watch.

Optical wrist sensors are accurate on running and over-read elsewhere: measured
against a strap on the same sessions, **+12 bpm average on barbell work and +7 on
circuits**. So non-running intensity figures from a watch alone are upper bounds.

**Check which strap.** Two straps of the same model are indistinguishable by source
name and separable only by the serial in `device_name`. A watch pairs to whichever
answers first, so *which* strap wrote a session is worth checking before trusting a
surprising number.

**There can be a fourth source.** AirPods report heart rate, and on gym-machine
sessions they can be effectively the *only* sensor — hundreds of samples against a
handful from everything else. The `hr` view classifies them as `other`, so any query
filtered to `strap` or `watch` silently drops those sessions entirely.

**Two people who share a strap will cross-contaminate.** The strap pairs to whichever
watch asks first, and the export labels the samples with the device, never with whose
chest they came from. The test is exact: pull the raw stream for the day out of both
exports and intersect on (timestamp, bpm). Two independent sensors on two people never
agree beat for beat; one strap agrees on nearly every shared second. List such days in
`cross_talk` — volume and pace stay, only the heart rate is dropped.

## Time in zone, not session averages

With a database, the report computes **real time in zone** from the per-sample stream.
With CSVs only, it buckets each session by its average heart rate, which is a
session-level classification rather than time in zone.

Not every session qualifies. A workout needs samples spanning at least 60% of its
duration (`MIN_ZONE_COVERAGE`), with each sample counting for at most 15 s
(`MAX_SAMPLE_GAP_S`). Below that the stream is too thin to describe the session and it
keeps the average-based treatment. Typically fewer than half of aerobic sessions
qualify — the rest are activities the watch barely samples, like an elliptical at 19
samples per 27 minutes.

**What changes when you switch.** On sessions that have both, the easy/hard headline
barely moves, but the *shape* does: Z1 up about 12 points, Z2 down 11, Z3 down 7, Z4 up
5. The average-based method inflates the middle at the expense of both ends. The real
gains are per-session: a 4×4 that averages Z4 holds 29% Z5, and a circuit class that
averages Z3 spends 41% of its time in Z4 — neither visible before.

Because it can, the report also prints the zone profile of the two buckets it
*excludes* from the aerobic split. They are excluded because an average heart rate
over a barbell class is meaningless — but real time in zone is not.

## Two corrections that matter more than they look

- **Training days, not session counts.** One gym visit logs two or three separate
  blocks, so "sessions per week" overstates how often you actually train — by more
  than 2× in one real dataset (4 sessions/week against 2 training days/week). Read
  hours and days, never session counts.
- **Absolute VO₂, not just VO₂max.** VO₂max is per-kilogram, so losing weight inflates
  it with no aerobic gain at all. Multiplying back out by body weight separates the
  two: a +16% VO₂max was really a +5% engine.

## Interval sessions

```sh
python3 analyze_intervals.py --profile NAME --workout <id> --expect-reps 4
```

A session that alternates hard and easy has a meaningless average, so this reports per
rep: pace, average and peak HR, HR entering and leaving the rep, and how much of the
rep was spent above 90% of max. Recoveries get their HR drop.

Rep detection tries three sources in order — structured `workout_blocks`, then heart
rate, then GPS speed. Two things it deliberately does not trust:

- **Apple's `Segment` events are not laps.** One session had 13 of them tiling the
  entire 47 minutes end to end with no gaps: automatic segmentation with nothing to do
  with the intervals actually run. Markers are used only when they leave the recoveries
  outside them (under 95% coverage).
- **A midpoint speed threshold swallows the warm-up.** A warm-up jog at 6:00/km sits
  far closer to rep pace (4:22/km) than a walked recovery (10:30/km) does, so the
  threshold is anchored at 80% of the fastest sustained speed rather than halfway down
  the range.

**This script needs `workout_blocks`,** so it only applies to a database built from a
phone backup. On an XML-built database, derive splits from `route_points` and haversine
instead.

## Calibrations that are yours, not the tool's

- **The walk/run threshold.** 7 km/h is a common default and is 8:34/km — inside many
  people's easy long-run range, where it labels real running as walking. On one dataset
  it called 43 of 70 running minutes "walking"; the right threshold there was below
  5.5 km/h (slower than 10:54/km), which scored the same run at 2.5 minutes. Between
  the two is a shuffle. Calibrate against the person's own easy pace.
- **Match the window to the claim.** A "best 3 km" compared against a recorded "last
  2 km" will disagree by 20–30 s/km purely because the longer window pulls in slower
  running. Compute best-N over the same N the claim uses before calling it a
  discrepancy.
- **Haversine over `route_points` is trustworthy** — within about 1.2% of Apple's own
  distance. It does not explain larger pace gaps; look for a window or method mismatch
  instead.
- **Recent `daily_metrics` are provisional.** A sync taken mid-day captures a partial
  day, and the next full sync silently revises it. Resting HR and HRV both move enough
  to reverse a conclusion about whether someone started a session fresh. Never quote
  same-day recovery numbers as settled.
