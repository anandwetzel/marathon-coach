# Marathon Coach

Adaptive training planner for the **PEAK Lake Garda 42, Sunday 11 April 2027** —
Limone sul Garda to Malcesine, +180 m / −160 m, 09:00 start.

Generates a 36-week plan from coaching rules, re-plans automatically when sessions
are missed or a trip comes up, tracks whether the goal pace is actually supported
by logged fitness, and publishes workouts to Apple Calendar as a subscribable feed.

## Setup

```bash
cd marathon-coach
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Requires Python 3.10+.

## Usage

**Generate the plan** (writes `data/plan_current.json` and `data/marathon.ics`):

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
python -m src.cli adapt             # apply the rules engine to remaining weeks
python -m src.cli trip --name "Lisbon" --start 2026-10-16 --end 2026-10-19 \
    --runs 1 --max-minutes 45
```

**Gear:**

```bash
python -m src.cli gear              # phase-gated checklist, EUR, Dutch retailers
python -m src.cli gear --due-now
```

**Dashboard** (everything above, plus charts):

```bash
streamlit run app.py
```

**Strava live sync** (optional — API access may require a Strava subscription):

1. Create an API app at [strava.com/settings/api](https://www.strava.com/settings/api).
2. Set **Authorization Callback Domain** to `localhost`.
3. In the dashboard **Log** tab, paste Client ID + Client Secret → **Connect Strava**.
4. Click **Sync from Strava** (or leave auto-sync on).

```bash
python -m src.cli sync-strava
```

CSV import still works without a subscription; use distance unit **Auto** (values ≥100 are meters).
Tokens for API sync live in `data/_strava_tokens.json` (gitignored).

## Calendar

`generate` / `adapt` write `~/Dropbox/Marathon/marathon.ics` (a real file, not a
symlink — Dropbox does not sync symlink targets reliably).

To subscribe in Apple Calendar:

1. Wait for Dropbox to finish syncing (green check on the file).
2. Right-click the file → **Share** → set access to **Anyone with the link** →
   **Copy link**.
3. Rewrite the URL: change `www.dropbox.com` → `dl.dropboxusercontent.com`, and
   change `dl=0` → `raw=1`. Example:

   ```
   https://dl.dropboxusercontent.com/scl/fi/…/marathon.ics?rlkey=…&raw=1
   ```

4. Calendar → **File → New Calendar Subscription…** → paste that URL.
5. Set **Location** to **iCloud**, auto-refresh daily, then OK.

Do not use a Finder symlink into Dropbox. Re-running `generate` overwrites the
real file; Calendar picks up changes on the next refresh.

## How the plan is built

Two phases across 36 weeks:

| Phase | Dates | Weeks | Volume |
|---|---|---|---|
| Base | 3 Aug – 6 Dec 2026 | 18 | 12 → ~28 mpw |
| Marathon block | 7 Dec 2026 – 11 Apr 2027 | 18 | ~24 → 40 mpw peak, 3-week taper |

Base grows the aerobic engine and adds a 4th run day. The marathon block follows a
Higdon Intermediate 1 structure (`plans/higdon_int1.yaml`), with three 20-mile long
runs and marathon-pace work layered in.

Weekly shape: 4 runs, climbing Thursday + Saturday with strength after, long run on
Saturday or Sunday. The generator reflows the week around whichever long-run day is
chosen and never puts quality the day after heavy lifting.

## Goal pace is an output, not an input

`race.goal_time` in `config.yaml` records the ambition (3:49, an 8:44/mi pace).
`race.must_beat` records what actually defines success (sub-4, 9:09/mi).

The tool estimates fitness independently, via VDOT from logged runs and the two
timed trials (15K in November, half in March), discounted by a volume penalty
because neither VDOT nor Riegel account for a thin aerobic base. There are no
organised tune-up races on the plan — log each trial as `kind=race` so paces
recalibrate. `python -m src.cli status` shows the projected finish next to the
goal so the target can be adjusted on evidence rather than hope.

Self-reported training pace is read as a **moderate** effort by default
(`start.current_effort`), not an easy one — at low volume the "normal" pace is
usually moderate, and reading it as easy inflates every derived pace.

## Darkness is a first-class constraint

Sessions run after work in Amstelveen. The Amsterdamse Bos is unlit, and sunset
falls below 17:00 from late October to mid-February — essentially the whole
marathon block. `src/plan/daylight.py` computes sunset per session and tags each
one `lit`, `marginal`, or `dark`, which drives route suggestions, the headlamp
purchase trigger in the gear module, and a preference for scheduling long runs in
daylight on weekends.

Every dark session also carries a `fallback`: the specific lit route to use
instead of the Bos, and permission to swap the run for climbing if it is icy.
There is deliberately no treadmill in the default setup — a Dutch winter has few
genuinely unrunnable days, and darkness is solved far more cheaply by a headlamp
than by a gym membership. Set `constraints.treadmill_access: true` if that
changes and the fallbacks rewrite themselves.

## The rules engine

`src/plan/adapt.py`, all deterministic:

- **Ramp cap** — weekly volume rises at most 10% (7% if `athlete.injury_history` is
  non-empty); max 3 build weeks before a cutback to 78%. The comparison is against
  the highest volume in the previous three weeks, so a planned one-week cutback
  does not flatten the build after it, but a longer interruption does pull the
  ceiling down — because that interruption is real detraining.
- **Long run cap** — never more than 35% of weekly volume, hard ceiling 20 miles.
- **Missed session** — never stacked. Dropped bottom-up: easy first, then quality,
  long run last.
- **Return from a gap** — 7-13 days repeats the last completed week; 14-27 days
  drops to 70% and rebuilds; 28+ days re-enters base.
- **Trip mode** — regenerates the week as maintenance within stated availability,
  keeping the long run before anything else, then re-ramps under the cap on
  return. The 2.5-week Christmas trip in `config.yaml` is the big one: it costs
  roughly two miles off the eventual peak but leaves the three 19-20 mile long
  runs intact, which is the part that actually decides the race.
- **Injury guard** — flags ACWR (7-day load ÷ 28-day average) above 1.5, and forces
  a cutback after 3 consecutive runs logging pain at or above threshold.
- **Taper** — final 3 weeks at 80/60/40% volume, intensity preserved.

## Configuration

Everything lives in [`config.yaml`](config.yaml). The fields worth revisiting:

| Field | Meaning |
|---|---|
| `start.current_effort` | How hard the reported pace really is; drives all zones |
| `athlete.injury_history` | Any entry tightens the ramp cap automatically |
| `constraints.climbing_days` | Protected; the generator never moves these |
| `constraints.long_run_days` | Which days a long run may land on |
| `trips` | Known travel, so the plan is right the first time |
| `gear.budget_eur` | Caps the gear recommendations |

`config.yaml` is documentation as much as configuration, so the app never
rewrites it. Edits made in the dashboard or via `trip` are written as a delta to
`data/overrides.yaml` and merged over the top at load time. Delete that file to
revert to the committed defaults.

Keep clock values quoted. YAML reads an unquoted `17:30` as the integer 1050,
which is why times are normalised on load rather than trusted.

## Tests

```bash
python -m tests.test_plan
```

51 checks, no test framework needed. Each one corresponds to a bug found while
building the tool — the VDOT model is verified against Daniels' published
tables, sunset against known Amsterdam times, and the rules engine against the
invariant that matters most: **a clean plan must come out unchanged.**

## Layout

```
app.py                  Streamlit dashboard
config.yaml             athlete profile, race, constraints, rules
plans/                  coach plan templates as data
data/training.db        logged sessions
data/plan_current.json  materialized plan
data/marathon.ics       calendar feed
src/athlete.py          pace zones, VDOT, fitness estimation
src/plan/templates.py   phase and microcycle definitions
src/plan/generator.py   materialize the plan
src/plan/adapt.py       rules engine
src/plan/daylight.py    sunset calculation and session tagging
src/log.py              session logging, Strava CSV / GPX import
src/strava.py           Strava OAuth + activity sync
src/metrics.py          volume, ACWR, pace trend, projected finish
src/calendar_feed.py    .ics generation
src/gear.py             phase-gated gear recommendations
```
