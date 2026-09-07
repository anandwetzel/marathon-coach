"""Regression tests. Run with: python -m tests.test_plan

Each test here corresponds to a bug found while building the tool, so they are
worth keeping rather than deleting once green.
"""

from __future__ import annotations

import copy
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import log as logbook, metrics
from src.athlete import (
    MARATHON_MILES,
    baseline_fitness,
    format_duration,
    race_time_for_vdot,
    vdot_from_performance,
)
from src.config import load_config
from src.plan import daylight as D
from src.plan import templates as T
from src.plan.adapt import adapt_plan
from src.plan.generator import build_plan

RESULTS: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    RESULTS.append((bool(condition), label))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def fresh_cfg() -> dict:
    """Config pointed at a throwaway database so tests never touch real data."""
    cfg = load_config()
    tmp = tempfile.mkdtemp(prefix="marathon-test-")
    cfg = copy.deepcopy(cfg)
    cfg["db_path"] = str(Path(tmp) / "training.db")
    cfg["plan_path"] = str(Path(tmp) / "plan.json")
    cfg["ics_path"] = str(Path(tmp) / "plan.ics")
    cfg["trips"] = []
    return cfg


def test_vdot_matches_published_tables() -> None:
    print("\nVDOT model against Daniels' published tables")
    # Daniels: VDOT 50 -> 5K 19:57, marathon 3:10; VDOT 40 -> 5K 24:08.
    five_k = 3.10686
    check(abs(race_time_for_vdot(50, five_k) - 1197) < 25,
          f"VDOT 50 5K is {format_duration(race_time_for_vdot(50, five_k))}, "
          f"table says 19:57")
    check(abs(race_time_for_vdot(40, five_k) - 1448) < 30,
          f"VDOT 40 5K is {format_duration(race_time_for_vdot(40, five_k))}, "
          f"table says 24:08")
    check(abs(race_time_for_vdot(50, MARATHON_MILES) - 11400) < 240,
          f"VDOT 50 marathon is "
          f"{format_duration(race_time_for_vdot(50, MARATHON_MILES))}, "
          f"table says about 3:10")
    # Round trip: a performance should reproduce the VDOT it came from.
    vdot = vdot_from_performance(13.1094, 6420)
    check(abs(race_time_for_vdot(vdot, 13.1094) - 6420) < 5,
          "VDOT inverts cleanly for a 1:47 half")


def test_sunset_matches_known_amsterdam_times() -> None:
    print("\nSunset for Amstelveen (longitude sign regression)")
    cfg = load_config()
    cases = {
        date(2026, 6, 21): "22:06",
        date(2026, 12, 21): "16:29",
        date(2027, 4, 11): "20:29",
    }
    for day, expected in cases.items():
        actual = f"{D.sun_times(day, cfg).sunset:%H:%M}"
        check(actual == expected, f"{day} sunset {actual} (expected {expected})")


def test_templates_are_ramp_safe() -> None:
    print("\nPlan templates validate clean")
    cfg = load_config()
    for template in T.load_all(cfg).values():
        warnings = T.validate(template)
        check(not warnings,
              f"{template.name}: {warnings if warnings else 'clean'}")


def test_plan_spans_to_race_day() -> None:
    print("\nPlan geometry")
    cfg = load_config()
    plan = build_plan(cfg)
    check(len(plan.weeks) == 36, f"{len(plan.weeks)} weeks")
    check(plan.weeks[-1].end == plan.race_date,
          f"final week ends {plan.weeks[-1].end}, race is {plan.race_date}")
    check(not plan.warnings, f"no geometry warnings: {plan.warnings}")
    races = [s for w in plan.weeks for s in w.sessions if s.is_race]
    check(len(races) == 3,
          f"{len(races)} hard efforts scheduled (2 timed trials + marathon)")
    check(all(s.as_date.weekday() == 6 for s in races),
          "every race/trial falls on a Sunday")
    trials = [w for w in plan.weeks
              if w.race and w.race.get("is_trial")]
    check(len(trials) == 2, f"{len(trials)} timed trials in the plan")
    check(all("time trial" in (w.race.get("name") or "").lower()
              for w in trials),
          "tune-ups are self-timed trials, not organised races")
    marathon = races[-1]
    # The gun time is declared in both config.yaml and the plan template; the
    # thing worth testing is that they have not drifted apart.
    expected = cfg["race"]["start_time"]
    check(marathon.start_time == expected,
          f"marathon starts at {marathon.start_time}, config says {expected}")


def test_no_runs_stacked_onto_climbing_days() -> None:
    print("\nDay assignment respects free days before cross-training days")
    cfg = load_config()
    plan = build_plan(cfg)
    protected = set(cfg["constraints"]["cross_training"]["days"])
    offenders = []
    for week in plan.weeks:
        used = {s.day for s in week.run_sessions if not s.optional}
        free = {d for d in ("tuesday", "wednesday", "friday") if d not in used}
        clashes = {s.day for s in week.run_sessions
                   if not s.optional and s.day in protected}
        if free and clashes:
            offenders.append(week.index)
    check(not offenders,
          f"no week doubles a run onto a cross-training day while a weekday is free "
          f"(offenders: {offenders})")


def test_aerobic_roles_have_distinct_pace_windows() -> None:
    print("\nLong / medium / easy pace windows differ and stay ≤30s wide")
    cfg = fresh_cfg()
    plan = build_plan(cfg)
    week = next(w for w in plan.weeks if w.run_sessions)
    by_role = {}
    for session in week.run_sessions:
        if session.zone != "easy" or not session.pace_low or not session.pace_high:
            continue
        by_role[session.role] = (session.pace_low, session.pace_high)
        width = abs(session.pace_high - session.pace_low)
        check(width <= 30.5, f"{session.role} window {width:.0f}s ≤ 30s")
    if "long" in by_role and "medium" in by_role:
        # Medium centre should be faster (lower sec/mi) than long centre.
        long_c = sum(by_role["long"]) / 2
        med_c = sum(by_role["medium"]) / 2
        check(med_c < long_c,
              f"medium centre {med_c:.0f}s faster than long {long_c:.0f}s")


def test_strength_never_loads_legs_before_a_long_run() -> None:
    print("\nStrength sequencing around the long run")
    cfg = load_config()
    for long_day in ("saturday", "sunday"):
        overrides = {w: long_day for w in range(1, 37)}
        plan = build_plan(cfg, long_run_overrides=overrides)
        days = ["monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday"]
        bad = []
        for week in plan.weeks:
            for session in week.sessions:
                if session.kind != "strength":
                    continue
                gap = days.index(long_day) - days.index(session.day)
                if gap == 1 and "lower" in session.strength_focus:
                    bad.append((week.index, session.day))
        check(not bad, f"long run on {long_day}: no heavy legs the day before "
                       f"({bad[:3]})")


def test_clean_plan_is_left_untouched() -> None:
    print("\nRules engine is a no-op on a clean plan")
    cfg = fresh_cfg()
    plan = build_plan(cfg)
    # Match the template opening week so the start-volume seed does not
    # itself reshape an otherwise clean plan.
    cfg["start"]["current_weekly_miles"] = plan.weeks[0].planned_miles
    result = adapt_plan(cfg, plan, as_of=date(2026, 8, 3))
    check(not result.adaptations,
          f"no adaptations with an empty log "
          f"({[str(a) for a in result.adaptations[:3]]})")
    check([w.planned_miles for w in result.plan.weeks]
          == [w.planned_miles for w in plan.weeks],
          "weekly volumes unchanged")


def test_cutback_week_does_not_flatten_the_build() -> None:
    print("\nA planned cutback does not lower the ramp anchor")
    cfg = fresh_cfg()
    plan = build_plan(cfg)
    cfg["start"]["current_weekly_miles"] = plan.weeks[0].planned_miles
    result = adapt_plan(cfg, plan, as_of=date(2026, 8, 3))
    # Base weeks 4, 8, 12 are cutbacks; the build weeks after them must survive.
    for index in (5, 9, 13):
        before = plan.weeks[index - 1].planned_miles
        after = result.plan.weeks[index - 1].planned_miles
        check(after == before,
              f"week {index} still {before:g} mi after the preceding cutback")


def test_start_volume_seeds_the_ramp() -> None:
    print("\nStarting weekly miles reshape early build weeks")
    cfg = fresh_cfg()
    cfg["start"]["current_weekly_miles"] = 8.0
    plan = build_plan(cfg)
    low = adapt_plan(cfg, plan, as_of=date(2026, 8, 3))
    check(low.plan.weeks[0].planned_miles <= 8.0 * 1.1 + 1.6,
          f"low start caps week 1 at {low.plan.weeks[0].planned_miles:g} mi")

    cfg["start"]["current_weekly_miles"] = 18.0
    high = adapt_plan(cfg, plan, as_of=date(2026, 8, 3))
    check(high.plan.weeks[0].planned_miles >= 17.0,
          f"high start lifts week 1 to {high.plan.weeks[0].planned_miles:g} mi")


def test_training_paces_adapt_from_logged_runs() -> None:
    print("\nLogged aerobic runs refresh fitness before the first race")
    cfg = fresh_cfg()
    cfg["start"]["current_easy_pace"] = "8:00"
    cfg["start"]["current_effort"] = "moderate"
    baseline = metrics.current_fitness(cfg, date(2026, 9, 1))
    for day, pace in [
        (date(2026, 8, 20), "9:30"),
        (date(2026, 8, 22), "9:40"),
        (date(2026, 8, 24), "9:20"),
        (date(2026, 8, 26), "9:35"),
    ]:
        logbook.log_session(cfg, day, miles=4.0, duration=_duration_for(4.0, pace),
                            rpe=4)
    adapted = metrics.current_fitness(cfg, date(2026, 9, 1))
    check("recent training" in adapted.source, f"source is {adapted.source}")
    check(adapted.paces["easy"] > baseline.paces["easy"],
          f"easy pace slowed from {format_duration(baseline.paces['easy'])} "
          f"to {format_duration(adapted.paces['easy'])}")
    cfg["start"]["adapt_paces_from_log"] = False
    locked = metrics.current_fitness(cfg, date(2026, 9, 1))
    check(locked.source.startswith("baseline"),
          f"lock falls back to baseline ({locked.source})")


def _duration_for(miles: float, pace: str) -> str:
    from src.athlete import parse_pace, format_duration
    return format_duration(miles * parse_pace(pace))


def test_under_training_pulls_the_plan_down() -> None:
    print("\nUnder-training reduces the remaining plan")
    cfg = fresh_cfg()
    plan = build_plan(cfg)
    for day, miles in [(date(2026, 8, 4), 2), (date(2026, 8, 9), 3),
                       (date(2026, 8, 11), 2), (date(2026, 8, 16), 3),
                       (date(2026, 8, 18), 2), (date(2026, 8, 23), 3)]:
        logbook.log_session(cfg, day, miles=miles, duration=f"{miles * 9}:00",
                            rpe=4)
    result = adapt_plan(cfg, plan, as_of=date(2026, 8, 24))
    week5 = result.plan.weeks[4].planned_miles
    check(week5 < plan.weeks[4].planned_miles,
          f"week 5 cut from {plan.weeks[4].planned_miles:g} to {week5:g} mi")
    check(any(a.rule == "ramp" for a in result.adaptations),
          "the ramp rule explained the change")


def test_gap_tiers() -> None:
    print("\nGap re-entry tiers")
    # Volume is kept deliberately low so each tier has something to cap; if the
    # upcoming week already matches the last completed one there is correctly
    # nothing for the rule to change.
    for gap_days, expect in [(9, "repeating"), (20, "70%"), (40, "base")]:
        cfg = fresh_cfg()
        plan = build_plan(cfg)
        last_run = date(2026, 8, 8)
        for offset, miles in [(-5, 2), (-3, 2), (0, 2)]:
            logbook.log_session(cfg, last_run + timedelta(days=offset),
                                miles=miles, duration=f"{miles * 9}:00", rpe=4)
        result = adapt_plan(cfg, plan, as_of=last_run + timedelta(days=gap_days))
        messages = " ".join(a.message for a in result.adaptations
                            if a.rule == "gap")
        check(expect in messages,
              f"{gap_days} days off mentions '{expect}': "
              f"{messages[:95] or 'no gap adaptation'}")


def test_pain_forces_a_cutback_and_strips_quality() -> None:
    print("\nRepeated pain forces a cutback")
    cfg = fresh_cfg()
    plan = build_plan(cfg)
    for day in (date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 6)):
        logbook.log_session(cfg, day, miles=4, duration="37:00", rpe=6, pain=3,
                            pain_location="left shin")
    result = adapt_plan(cfg, plan, as_of=date(2026, 9, 7))
    week = next(w for w in result.plan.weeks if w.contains(date(2026, 9, 9)))
    check(any(a.rule == "pain" for a in result.adaptations), "pain rule fired")
    check(week.kind == "cutback", f"week became {week.kind}")
    hard = [s.kind for s in week.run_sessions
            if s.kind in ("tempo", "marathon_pace", "intervals", "strides")]
    check(not hard, f"no quality left in the week ({hard})")


def test_trip_protects_the_long_run() -> None:
    print("\nTrip mode keeps one longer effort")
    cfg = fresh_cfg()
    cfg["trips"] = [{"name": "Lisbon", "start": date(2027, 1, 4),
                     "end": date(2027, 1, 10), "runs_possible": 1,
                     "max_run_minutes": 45}]
    plan = build_plan(cfg)
    result = adapt_plan(cfg, plan, as_of=date(2026, 12, 28))
    week = next(w for w in result.plan.weeks if w.start == "2027-01-04")
    kept = [s for s in week.run_sessions if s.miles > 0]
    check(len(kept) == 1, f"{len(kept)} run kept")
    check(kept and kept[0].role == "long",
          f"the kept run is the long run (got {kept[0].role if kept else None})")
    check(kept and kept[0].duration_minutes <= 46,
          f"capped at the stated {kept[0].duration_minutes if kept else 0} min")


def test_race_week_loads_nothing() -> None:
    print("\nRace week programmes no heavy lifting and no compulsory climbing")
    cfg = load_config()
    plan = build_plan(cfg)
    week = plan.weeks[-1]

    strength = [s for s in week.sessions if s.kind == "strength"]
    check(not any("squat" in s.detail.lower() or "deadlift" in s.detail.lower()
                  for s in strength),
          f"no loaded lifting in race week ({len(strength)} strength slots)")

    climbs = [s for s in week.sessions if s.kind == "climb"]
    check(climbs and all(s.optional for s in climbs),
          f"all {len(climbs)} climbing sessions are optional in race week")

    # A timed-trial week is not the goal race, so training carries on around it.
    tune_up = next(w for w in plan.weeks
                   if any(s.is_race for s in w.sessions) and w is not week
                   and w.index > 20)
    tune_climbs = [s for s in tune_up.sessions if s.kind == "climb"]
    check(tune_climbs and not all(s.optional for s in tune_climbs),
          "a timed-trial week still keeps normal climbing")


def test_long_interruption_lowers_the_ramp_anchor() -> None:
    print("\nA multi-week trip pulls the anchor down, a one-week dip does not")
    cfg = fresh_cfg()
    cfg["trips"] = [{"name": "Christmas", "start": date(2026, 12, 24),
                     "end": date(2027, 1, 10), "runs_possible": 3,
                     "max_run_minutes": 75}]
    plan = build_plan(cfg)
    result = adapt_plan(cfg, plan, as_of=date(2026, 12, 7))
    weeks = {w.start: w.planned_miles for w in result.plan.weeks}

    # The three weeks after the trip must step up off what was actually run
    # during it, not off the pre-trip peak.
    trip_peak = max(weeks["2026-12-28"], weeks["2027-01-04"])
    first_back = weeks["2027-01-11"]
    cap = float(cfg["rules"]["max_weekly_increase_pct"])
    check(first_back <= max(trip_peak, weeks["2026-12-21"]) * (1 + cap) + 0.1,
          f"first week back {first_back:g} mi is within {cap:.0%} of the "
          f"{trip_peak:g} mi actually run on the trip")

    # But the race-specific long runs must still be there at the end.
    longest = max(s.miles for w in result.plan.weeks for s in w.run_sessions
                  if s.role == "long" and not s.is_race)
    check(longest >= 19.0,
          f"longest training run still {longest:g} mi despite the trip")


def test_dark_sessions_carry_a_bad_weather_fallback() -> None:
    print("\nDark sessions name a lit route; lit sessions stay quiet")
    cfg = fresh_cfg()
    plan = build_plan(cfg)
    runs = [s for w in plan.weeks for s in w.run_sessions if s.miles > 0]

    dark = [s for s in runs if s.daylight == "dark" and not s.is_race]
    check(dark and all(s.fallback for s in dark),
          f"all {len(dark)} dark sessions have a fallback")
    route = (cfg["constraints"]["lit_routes"] or [""])[0].split(",")[0]
    check(any(route in s.fallback for s in dark),
          f"the fallback names a real route ({route})")

    lit = [s for s in runs if s.daylight == "lit"]
    check(not any(s.fallback for s in lit),
          "sessions in full daylight carry no fallback noise")

    cfg["constraints"]["treadmill_access"] = True
    treadmill = build_plan(cfg)
    dark_tm = [s for w in treadmill.weeks for s in w.run_sessions
               if s.daylight == "dark" and s.miles > 0 and not s.is_race]
    check(dark_tm and all("treadmill" in s.fallback for s in dark_tm),
          "with a gym membership the fallback becomes the treadmill")


def test_taper_comes_down() -> None:
    print("\nTaper reduces volume")
    cfg = load_config()
    plan = build_plan(cfg)
    peak = max(w.planned_miles for w in plan.weeks)
    ratios = []
    for week in plan.weeks[-3:]:
        running = sum(s.miles for s in week.run_sessions
                      if not s.optional and not s.is_race)
        ratios.append(running / peak)
    check(ratios[0] < 0.85, f"taper week 1 at {ratios[0]:.0%} of peak")
    check(ratios[1] < 0.65, f"taper week 2 at {ratios[1]:.0%} of peak")
    check(ratios[2] < 0.45, f"race week at {ratios[2]:.0%} of peak")
    check(ratios == sorted(ratios, reverse=True), "taper decreases monotonically")


def test_long_runs_stay_in_daylight() -> None:
    print("\nLong runs are never scheduled in the dark")
    cfg = load_config()
    plan = build_plan(cfg)
    dark = [(w.index, s.date) for w in plan.weeks for s in w.run_sessions
            if s.role == "long" and s.daylight == "dark"]
    check(not dark, f"no long run in the dark ({dark[:3]})")


def test_future_logs_do_not_create_negative_gaps() -> None:
    print("\nFuture-dated logs do not break the gap calculation")
    cfg = fresh_cfg()
    logbook.log_session(cfg, date(2026, 8, 4), miles=3, duration="27:00", rpe=4)
    gap = logbook.days_since_last_run(cfg, date(2026, 7, 30))
    check(gap is None or gap >= 0, f"gap is {gap}, must not be negative")


def test_acwr_withheld_until_history_exists() -> None:
    print("\nWorkload ratio is withheld without a baseline")
    cfg = fresh_cfg()
    logbook.log_session(cfg, date(2026, 8, 4), miles=6, duration="55:00", rpe=5)
    ratio, _, _ = metrics.acwr(cfg, date(2026, 8, 6))
    check(ratio is None, f"ratio is {ratio} with 2 days of history, expected none")


def test_config_times_survive_yaml_sexagesimal() -> None:
    print("\nClock values are not read as YAML integers")
    cfg = load_config()
    for field in ("work_start", "work_end", "weekday_earliest_start",
                  "weekend_start"):
        value = cfg["constraints"][field]
        check(isinstance(value, str) and ":" in value,
              f"constraints.{field} is {value!r}")
    from src.config import _as_time_string
    # Unquoted "17:30" in YAML parses to 1050; it must round-trip back.
    check(_as_time_string(1050) == "17:30",
          f"sexagesimal 1050 normalises to {_as_time_string(1050)}")


def test_overrides_do_not_rewrite_config() -> None:
    print("\nDashboard edits go to overrides, not config.yaml")
    import yaml
    from src.config import PROJECT_ROOT, save_config, _as_date

    config_file = PROJECT_ROOT / "config.yaml"
    before = config_file.read_text()
    base = yaml.safe_load(before)

    cfg = load_config()
    override_path = Path(tempfile.mkdtemp(prefix="marathon-ovr-")) / "ovr.yaml"
    cfg["overrides_path"] = str(override_path)
    # Start from config.yaml alone so personal data/overrides.yaml is not
    # re-emitted as part of the delta under test.
    cfg["athlete"] = copy.deepcopy(base["athlete"])
    cfg["start"] = copy.deepcopy(base["start"])
    cfg["start"]["plan_start"] = _as_date(cfg["start"]["plan_start"])
    cfg["race"] = copy.deepcopy(base["race"])
    cfg["race"]["date"] = _as_date(cfg["race"]["date"])
    if cfg["race"].get("registration_deadline"):
        cfg["race"]["registration_deadline"] = _as_date(
            cfg["race"]["registration_deadline"])
    cfg["race"]["goal_time"] = "3:55:00"

    save_config(cfg)
    check(config_file.read_text() == before, "config.yaml left byte-identical")
    check("# Base configuration" in config_file.read_text(),
          "config.yaml comments intact")

    written = yaml.safe_load(override_path.read_text())
    check(written == {"race": {"goal_time": "3:55:00"}},
          f"only the delta was written: {written}")


def test_baseline_fitness_is_not_optimistic() -> None:
    print("\nBaseline reads training pace as a sub-maximal effort")
    cfg = load_config()
    fitness = baseline_fitness(cfg)
    check(fitness.paces["easy"] > 540,
          f"easy pace {format_duration(fitness.paces['easy'])}/mi is slower "
          f"than the reported 8:30 training pace")
    check(fitness.predicted_marathon > 14400,
          f"projected {format_duration(fitness.predicted_marathon)} is outside "
          f"4:00:00 off 8 miles a week")


def test_schedule_move_persists_across_rebuild():
    print("schedule move persists across rebuild")
    cfg = fresh_cfg()
    plan = build_plan(cfg)
    week = plan.weeks[0]
    run = next(s for s in week.sessions if s.miles > 0 and s.role != "long")
    fps = __import__("src.plan.schedule", fromlist=["assign_fingerprints"]).assign_fingerprints(plan)
    fingerprint = next(fp for fp, s in fps.items() if s is run)
    target = week.start_date + timedelta(days=3)
    # Avoid landing on the same day.
    if run.as_date == target:
        target = week.start_date + timedelta(days=4)

    from src.plan import schedule as S
    assert S.move_session(plan, fingerprint, target, cfg)
    check(run.as_date == target, "session date updated")
    check(fingerprint in logbook.schedule_moves(cfg), "move persisted")

    rebuilt = build_plan(cfg)
    adapted = adapt_plan(cfg, rebuilt).plan
    applied = S.apply_schedule_moves(adapted, cfg)
    check(applied >= 1, "move re-applied after rebuild")
    moved = S.assign_fingerprints(adapted)[fingerprint]
    check(moved.as_date == target, "rebuilt plan keeps dragged date")


def test_strava_csv_distance_units() -> None:
    print("\nStrava CSV distance auto-detects meters")
    check(abs(logbook.distance_to_miles(8046.72, "auto") - 5.0) < 0.02,
          "8046 m → ~5 mi")
    check(abs(logbook.distance_to_miles(8.05, "auto") - 5.0) < 0.05,
          "8.05 km → ~5 mi")
    check(abs(logbook.distance_to_miles(5.0, "mi") - 5.0) < 0.01,
          "5 mi stays 5")
    # Guardrail: huge "miles" are meters.
    check(abs(logbook.distance_to_miles(8190.8, "mi") - 5.09) < 0.05,
          "8190 as 'mi' still treated as meters")
    row = {
        "Activity Date": "Aug 29, 2026, 4:13:40 PM",
        "Activity Type": "Run",
        "Activity Name": "Afternoon Run",
        "Distance": "8190.8",
        "Moving Time": "2492",
        "Activity ID": "19968915564",
    }
    parsed = logbook._parse_strava_row(row, "auto")
    check(parsed is not None, "row parses")
    check(abs(parsed["miles"] - 5.09) < 0.05, f"miles {parsed['miles']}")
    check(parsed["duration"] == 2492.0, "moving time seconds")


def test_strava_activity_mapping() -> None:
    print("\nStrava API activities map into log sessions")
    from src import strava as S
    run = S.activity_to_session({
        "id": 99,
        "name": "Easy 5",
        "sport_type": "Run",
        "distance": 8046.72,
        "moving_time": 2700,
        "start_date_local": "2026-09-01T07:30:00",
        "perceived_exertion": 4,
    })
    check(run is not None, "run maps")
    check(run["kind"] == logbook.KIND_RUN, "kind is run")
    check(abs(run["miles"] - 5.0) < 0.02, f"miles {run['miles']}")
    check(run["duration"] == 2700, "duration seconds")
    check(run["external_id"] == "strava:99", "external id")
    check(run["rpe"] == 4, "rpe from perceived_exertion")

    race = S.activity_to_session({
        "id": 100,
        "name": "5K",
        "type": "Run",
        "workout_type": 1,
        "distance": 5000,
        "moving_time": 1500,
        "start_date_local": "2026-09-02T09:00:00",
    })
    check(race["kind"] == logbook.KIND_RACE, "workout_type 1 is race")

    ride = S.activity_to_session({
        "id": 101, "sport_type": "Ride", "distance": 20000,
        "moving_time": 3600, "start_date_local": "2026-09-03T09:00:00",
    })
    check(ride is None, "rides are skipped")


def main() -> int:
    for test in [
        test_vdot_matches_published_tables,
        test_sunset_matches_known_amsterdam_times,
        test_templates_are_ramp_safe,
        test_plan_spans_to_race_day,
        test_no_runs_stacked_onto_climbing_days,
        test_aerobic_roles_have_distinct_pace_windows,
        test_strength_never_loads_legs_before_a_long_run,
        test_clean_plan_is_left_untouched,
        test_cutback_week_does_not_flatten_the_build,
        test_start_volume_seeds_the_ramp,
        test_training_paces_adapt_from_logged_runs,
        test_under_training_pulls_the_plan_down,
        test_gap_tiers,
        test_pain_forces_a_cutback_and_strips_quality,
        test_trip_protects_the_long_run,
        test_race_week_loads_nothing,
        test_long_interruption_lowers_the_ramp_anchor,
        test_dark_sessions_carry_a_bad_weather_fallback,
        test_taper_comes_down,
        test_long_runs_stay_in_daylight,
        test_future_logs_do_not_create_negative_gaps,
        test_acwr_withheld_until_history_exists,
        test_config_times_survive_yaml_sexagesimal,
        test_overrides_do_not_rewrite_config,
        test_baseline_fitness_is_not_optimistic,
        test_schedule_move_persists_across_rebuild,
        test_strava_csv_distance_units,
        test_strava_activity_mapping,
    ]:
        test()

    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"\n{passed} passed, {failed} failed, {len(RESULTS)} checks total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
