# Marathon Coach

Local adaptive marathon training planner. It builds a multi-week plan from YAML
templates and coaching rules, re-plans when you miss sessions or travel, tracks
whether your goal is supported by logged fitness, and publishes workouts as an
Apple Calendar–subscribable `.ics` feed.

Location, race, starting fitness, cross-training days, and gear currency are all
config — copy [`config.example.yaml`](config.example.yaml) or edit Settings in
the dashboard. Personal overrides land in `data/overrides.yaml` (gitignored) so
`config.yaml` stays shareable.

## Setup

```bash
cd marathon-coach
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Requires Python 3.10+.

Point `config.yaml` (or the example file) at your race date, location
(`lat` / `lon` / `timezone`), starting mileage and pace, and `ics_path`. Use
`timezone: local` to pick up the machine timezone.

## Usage

**Generate the plan** (writes `plan_path` and `ics_path` from config):

```bash
python -m src.cli generate
```

**See what to run:**

```bash
python -m src.cli week              # current week
python -m src.cli week --next       # next week
python -m src.cli today
python -m src.cli paces             # training zones from current fitness
python -m src.cli status            # fitness, projected finish, ACWR, warnings
```

**Log a session:**

```bash
python -m src.cli log --date 2026-08-04 --miles 3.2 --time 27:30 --rpe 4
python -m src.cli log --date 2026-08-07 --kind climb
python -m src.cli log --date 2026-08-09 --miles 6 --time 57:00 --rpe 6 --pain 1 \
    --notes "left calf tight last mile"
```

**Re-plan after a change:**

```bash
python -m src.cli adapt
python -m src.cli trip --name "Long weekend" --start 2026-10-16 --end 2026-10-19 \
    --runs 1 --max-minutes 45
```

**Gear checklist** (phase-gated; currency from `gear.currency`):

```bash
python -m src.cli gear
python -m src.cli gear --due-now
```

**Dashboard** (schedule, log, progress, settings):

```bash
streamlit run app.py
```

**Strava** (optional — API access may require a Strava subscription):

1. Create an API app at [strava.com/settings/api](https://www.strava.com/settings/api).
2. Set **Authorization Callback Domain** to `localhost`.
3. In **Log**, paste Client ID + Client Secret → **Connect Strava** → sync.

```bash
python -m src.cli sync-strava
```

CSV import works without a subscription; use distance unit **Auto** (values ≥100
are treated as meters). OAuth tokens live in `data/_strava_tokens.json` (gitignored).

## Calendar

`generate` / `adapt` write a real `.ics` file at `ics_path` (not a symlink —
cloud sync tools often fail to follow symlink targets).

A common setup is Dropbox + Apple Calendar:

1. Set `ics_path` to something like `~/Dropbox/Marathon/marathon.ics`.
2. Wait for sync, then share the file with a link.
3. Rewrite the URL: `www.dropbox.com` → `dl.dropboxusercontent.com`, and
   `dl=0` → `raw=1`.
4. Calendar → **File → New Calendar Subscription…** → paste that URL.
5. Prefer **iCloud**, daily refresh.

Re-running `generate` overwrites the file; Calendar picks up changes on refresh.
You can also point `ics_path` at `data/marathon.ics` and download it from the
dashboard Settings page.

## How the plan is built

Default templates are an aerobic **base** phase then a **marathon** block
(Higdon Intermediate 1–style in `plans/higdon_int1.yaml`), sized to land the
final Sunday on `race.date`. Exact week counts and peak volume depend on
`start.plan_start` and the templates.

Weekly shape is driven by config: target run days, protected
`constraints.cross_training` days (climbing, gym, yoga, …) with optional
strength paired to them, and a long run on Saturday or Sunday. The generator
reflows the week around the long-run day and avoids hard quality the day after
heavy lifting.

Starting weekly miles and typical pace (plus logged volume and aerobic paces)
reshape early weeks and training zones on rebuild. A logged race still wins for
VDOT calibration.

## Goal pace is an output, not an input

`race.goal_time` is the ambition; `race.must_beat` is the success line. Neither
is baked into training paces.

Fitness is estimated from evidence: seed start pace + effort, then recent logged
easy runs, then a logged race when you have one — discounted by a volume penalty
while the aerobic base is still thin. Tune-up trials on the plan should be logged
as `kind=race` so zones recalibrate. `python -m src.cli status` shows projected
finish next to the goal.

Self-reported training pace defaults to **moderate** effort
(`start.current_effort`), not easy — at low volume a “normal” pace is usually
moderate, and reading it as easy inflates every derived zone.

## Darkness is a first-class constraint

Weekday sessions often land after work. `src/plan/daylight.py` computes sunset
for `location.lat` / `lon` / `timezone` and tags each session `lit`, `marginal`,
or `dark`. That drives route notes, gear triggers (e.g. headlamp), and a
preference for long runs in weekend daylight.

Dark sessions carry a `fallback` from `constraints.lit_routes` (edit these for
wherever you train). Set `constraints.treadmill_access: true` if indoor running
should replace “find a lit path” as the bad-weather answer.

## The rules engine

`src/plan/adapt.py`, all deterministic:

- **Ramp cap** — weekly volume rises at most 10% (7% if `athlete.injury_history`
  is non-empty); max 3 build weeks before a cutback. Anchored on recent
  *observed* volume (and your current weekly miles), so under-training or a long
  gap pulls later weeks down; starting ahead of the template holds near current
  capacity until the written plan catches up.
- **Long run cap** — soft fraction of the week plus a hard mile ceiling from
  `rules`.
- **Missed session** — never stacked onto a protected day when another weekday
  is free. Dropped bottom-up: easy first, quality next, long run last.
- **Return from a gap** — short gaps repeat the last week; longer gaps cut volume
  and rebuild under the ramp.
- **Trip mode** — regenerates weeks inside stated availability, protecting the
  long run when possible, then re-ramps on return.
- **Injury guard** — flags high ACWR; forces a cutback after repeated pain at or
  above threshold.
- **Taper** — final weeks step volume down while intensity is preserved.

## Configuration

Primary file: [`config.yaml`](config.yaml). For a friend or a fresh machine,
start from [`config.example.yaml`](config.example.yaml). Fields worth setting
early:

| Field | Meaning |
|---|---|
| `location.*` | Name, lat/lon, timezone (`local` = laptop zone) |
| `race.*` | Goal race, date, goal / must-beat times |
| `start.plan_start` | Monday the plan begins (final Sunday should be race day) |
| `start.current_weekly_miles` / `current_easy_pace` / `current_effort` | Seed fitness and volume |
| `start.adapt_paces_from_log` | Refresh VDOT from recent aerobic runs on rebuild |
| `constraints.cross_training` | Protected non-running days + display name |
| `constraints.lit_routes` | Where to run when it’s dark |
| `constraints.long_run_days` | Which days a long run may land on |
| `trips` | Known travel so the plan is right the first time |
| `gear.currency` | Display currency for gear prices |
| `ics_path` | Where the calendar feed is written |

The dashboard never rewrites `config.yaml`. Edits go to `data/overrides.yaml` and
merge on load. Delete that file to revert to committed defaults.

Keep clock values quoted. YAML reads an unquoted `17:30` as the integer 1050;
times are normalised on load.

## Tests

```bash
python -m tests.test_plan
```

No external test framework. Checks cover VDOT against Daniels’ tables, daylight
math, ramp/gap/pain/trip rules, adaptive start volume, and CSV distance units.
A clean plan with matching start mileage must come out of the rules engine
unchanged.

## Layout

```
app.py                  Streamlit dashboard
config.yaml             athlete / race / constraints (local defaults)
config.example.yaml     portable starter for sharing
plans/                  coach plan templates as data
data/                   db, plan JSON, overrides, tokens (mostly gitignored)
src/athlete.py          pace zones, VDOT, fitness estimation
src/plan/templates.py   phase and microcycle definitions
src/plan/generator.py   materialize the plan
src/plan/adapt.py       rules engine
src/plan/daylight.py    sunset calculation and session tagging
src/plan/schedule.py    interactive calendar moves
src/log.py              session logging, Strava CSV / GPX import
src/strava.py           Strava OAuth + activity sync
src/metrics.py          volume, ACWR, pace trend, projected finish
src/calendar_feed.py    .ics generation
src/gear.py             phase-gated gear recommendations
src/workouts.py         cross-training, strength, stretch prescriptions
```
