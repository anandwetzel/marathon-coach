"""Command line interface."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

from . import calendar_feed, gear as gearmod, log as logbook, metrics
from .athlete import (
    ZONE_LABEL,
    format_duration,
    format_pace,
    format_pace_km,
    pace_both,
)
from .config import load_config, save_config, currency_code
from .plan import templates as templatemod
from .plan.adapt import adapt_plan, describe
from .plan.generator import Plan, build_plan, load, save
from .plan import schedule as schedulemod


DAYLIGHT_MARK = {"lit": "", "marginal": "  (fading light)", "dark": "  (dark)"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="marathon-coach",
        description="Adaptive marathon training planner.")
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate", help="build the plan and calendar feed")

    p_week = sub.add_parser("week", help="show a week of the plan")
    p_week.add_argument("--next", action="store_true", dest="next_week")
    p_week.add_argument("--index", type=int, help="week number, 1-36")
    p_week.add_argument("--all", action="store_true", help="summarise every week")

    sub.add_parser("today", help="show today's session")
    sub.add_parser("paces", help="training paces from current fitness")
    sub.add_parser("status", help="fitness, projected finish, warnings")
    sub.add_parser("adapt", help="re-plan remaining weeks from the log")
    sub.add_parser("validate", help="check the plan templates")

    p_log = sub.add_parser("log", help="record a session")
    p_log.add_argument("--date", required=True)
    p_log.add_argument("--kind", default=logbook.KIND_RUN,
                       choices=[logbook.KIND_RUN, logbook.KIND_RACE,
                                logbook.KIND_CLIMB, logbook.KIND_STRENGTH,
                                logbook.KIND_CROSS])
    p_log.add_argument("--miles", type=float, default=0.0)
    p_log.add_argument("--time", dest="duration", help="MM:SS or H:MM:SS")
    p_log.add_argument("--rpe", type=int, help="perceived effort 1-10")
    p_log.add_argument("--pain", type=int, default=0, help="0-5")
    p_log.add_argument("--pain-location", default="")
    p_log.add_argument("--label", default="", help="race name or session label")
    p_log.add_argument("--notes", default="")

    p_trip = sub.add_parser("trip", help="add a trip and re-plan around it")
    p_trip.add_argument("--name", required=True)
    p_trip.add_argument("--start", required=True)
    p_trip.add_argument("--end", required=True)
    p_trip.add_argument("--runs", type=int, default=1)
    p_trip.add_argument("--max-minutes", type=float, default=45)
    p_trip.add_argument("--notes", default="")

    p_gear = sub.add_parser("gear", help="gear recommendations")
    p_gear.add_argument("--due-now", action="store_true")

    p_strava = sub.add_parser("import-strava", help="import activities.csv")
    p_strava.add_argument("path")
    p_strava.add_argument("--unit", default="auto",
                          choices=["auto", "m", "km", "mi"],
                          help="distance unit in the file (auto detects meters)")

    p_sync = sub.add_parser(
        "sync-strava",
        help="pull recent activities from a linked Strava account")
    p_sync.add_argument(
        "--days", type=int, default=None,
        help="lookback on first sync (default: config strava.lookback_days)")

    p_gpx = sub.add_parser("import-gpx", help="import a .gpx track")
    p_gpx.add_argument("path")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    handlers = {
        "generate": cmd_generate, "week": cmd_week, "today": cmd_today,
        "paces": cmd_paces, "status": cmd_status, "adapt": cmd_adapt,
        "validate": cmd_validate, "log": cmd_log, "trip": cmd_trip,
        "gear": cmd_gear, "import-strava": cmd_import_strava,
        "sync-strava": cmd_sync_strava, "import-gpx": cmd_import_gpx,
    }
    return handlers[args.command](cfg, args) or 0


# --- Commands --------------------------------------------------------------

def cmd_generate(cfg: dict, args) -> int:
    plan = _fresh_plan(cfg)
    result = adapt_plan(cfg, plan)
    moved = schedulemod.apply_schedule_moves(result.plan, cfg)
    save(result.plan, cfg["plan_path"])
    ics = calendar_feed.write(result.plan, cfg)

    print(f"Generated {len(result.plan.weeks)} weeks: "
          f"{result.plan.weeks[0].start} to {result.plan.weeks[-1].end}")
    print(f"Race: {result.plan.race_name} on {result.plan.race_date}")
    print(f"Peak week: {result.plan.peak_week.planned_miles:g} mi "
          f"(week {result.plan.peak_week.index})")
    print(f"Plan written to {cfg['plan_path']}")
    print(f"Calendar written to {ics}")
    for warning in result.plan.warnings:
        print(f"  warning: {warning}")
    if result.adaptations:
        print(f"\n{len(result.adaptations)} adaptation(s) applied:")
        print(describe(result))
    if moved:
        print(f"{moved} manual schedule move(s) re-applied.")
    return 0


def cmd_week(cfg: dict, args) -> int:
    plan = _load_plan(cfg)
    if args.all:
        _print_overview(plan)
        return 0

    if args.index:
        week = next((w for w in plan.weeks if w.index == args.index), None)
    else:
        target = date.today() + timedelta(days=7 if args.next_week else 0)
        week = plan.week_for(target) or plan.weeks[0]

    if not week:
        print("No week found.")
        return 1
    _print_week(week, plan)
    return 0


def cmd_today(cfg: dict, args) -> int:
    plan = _load_plan(cfg)
    today = date.today()
    sessions = plan.session_for(today)
    if not sessions:
        print(f"{today}: nothing scheduled - the plan runs "
              f"{plan.weeks[0].start} to {plan.weeks[-1].end}.")
        return 0
    print(f"{today:%A %d %B %Y}")
    for session in sessions:
        _print_session(session, indent="  ")
    return 0


def cmd_paces(cfg: dict, args) -> int:
    fitness = metrics.current_fitness(cfg)
    print(f"VDOT {fitness.vdot:.1f}  (from {fitness.source})")
    print(f"Recent peak volume: {fitness.peak_weekly_miles:.1f} mi/week\n")
    for zone, pace in fitness.paces.items():
        print(f"  {ZONE_LABEL[zone]:<20} {pace_both(pace)}")
    print()
    goal = cfg["race"]["goal_time"]
    must = cfg["race"]["must_beat"]
    distance = float(cfg["race"]["distance_miles"])
    from .athlete import parse_duration
    print(f"  {'Goal ' + goal:<20} "
          f"{pace_both(parse_duration(goal) / distance)}")
    print(f"  {'Must beat ' + must:<20} "
          f"{pace_both(parse_duration(must) / distance)}")
    return 0


def cmd_status(cfg: dict, args) -> int:
    plan = _load_plan(cfg)
    st = metrics.status(cfg, plan)

    print(f"As of {st.as_of}  ({_weeks_out(cfg, st.as_of)} weeks to race day)\n")
    print(f"  Fitness            VDOT {st.fitness.vdot:.1f} "
          f"({st.fitness.source})")
    print(f"  Projected finish   {format_duration(st.projected_finish)} "
          f"at {format_pace(st.fitness.predicted_marathon_pace)}")
    print(f"  Goal               {cfg['race']['goal_time']}"
          f"   Must beat {cfg['race']['must_beat']}")
    print(f"  Marathon potential {format_duration(st.fitness.marathon_potential)} "
          f"at full marathon volume")
    print()
    print(f"  Last 7 days        {st.acute_miles:g} mi")
    print(f"  4-week average     {st.chronic_weekly_miles:g} mi/week")
    print(f"  Workload ratio     "
          f"{st.acwr if st.acwr is not None else 'not enough history yet'}")
    if st.compliance_pct is not None:
        print(f"  Compliance         {st.compliance_pct:g}% of planned volume")
    if st.days_since_run is not None:
        print(f"  Days since a run   {st.days_since_run}")

    if st.warnings:
        print("\nWarnings:")
        for warning in st.warnings:
            print(f"  - {warning}")
    else:
        print("\nNothing flagged.")
    return 0


def cmd_adapt(cfg: dict, args) -> int:
    plan = _fresh_plan(cfg)
    result = adapt_plan(cfg, plan)
    schedulemod.apply_schedule_moves(result.plan, cfg)
    save(result.plan, cfg["plan_path"])
    calendar_feed.write(result.plan, cfg)
    print(describe(result))
    print(f"\nPlan and calendar updated.")
    return 0


def cmd_validate(cfg: dict, args) -> int:
    problems = 0
    for key, template in templatemod.load_all(cfg).items():
        warnings = templatemod.validate(template)
        status = "clean" if not warnings else f"{len(warnings)} warning(s)"
        print(f"{template.name}: {len(template.weeks)} weeks, {status}")
        for warning in warnings:
            print(f"  - {warning}")
        problems += len(warnings)
    return 1 if problems else 0


def cmd_log(cfg: dict, args) -> int:
    when = date.fromisoformat(args.date)
    logbook.log_session(
        cfg, when=when, kind=args.kind, miles=args.miles, duration=args.duration,
        rpe=args.rpe, pain=args.pain, pain_location=args.pain_location,
        label=args.label, notes=args.notes)
    print(f"Logged {args.kind} on {when}"
          + (f": {args.miles:g} mi" if args.miles else ""))

    plan = _load_plan(cfg, quiet=True)
    if plan:
        result = adapt_plan(cfg, plan)
        recent = [a for a in result.adaptations
                  if (_week_start(result.plan, a.week_index) or when) >= when]
        if recent:
            print("\nThis changes the plan:")
            for note in recent[:5]:
                print(f"  {note}")
            print("\nRun 'marathon-coach adapt' to apply.")
    return 0


def cmd_trip(cfg: dict, args) -> int:
    trip = {
        "name": args.name,
        "start": date.fromisoformat(args.start),
        "end": date.fromisoformat(args.end),
        "runs_possible": args.runs,
        "max_run_minutes": args.max_minutes,
    }
    if args.notes:
        trip["notes"] = args.notes
    cfg.setdefault("trips", [])
    cfg["trips"] = [t for t in cfg["trips"] if t.get("name") != args.name]
    cfg["trips"].append(trip)
    save_config(cfg, args.config if hasattr(args, "config") else None)
    print(f"Added trip '{args.name}' {trip['start']} to {trip['end']} "
          f"({args.runs} run(s), max {args.max_minutes:g} min).")
    return cmd_adapt(cfg, args)


def cmd_gear(cfg: dict, args) -> int:
    plan = _load_plan(cfg)
    items = (gearmod.due_now(cfg, plan) if args.due_now
             else gearmod.recommend(cfg, plan))
    if not items:
        print("Nothing due in the next three weeks.")
        return 0

    current = None
    for item in items:
        bucket = item.due.isoformat() if item.due else "any time"
        if bucket != current:
            current = bucket
            print(f"\n  by {bucket}" if item.due else "\n  any time")
        print(f"    [{item.priority:<11}] {item.price_label:<14} {item.name}")
        print(f"      {item.trigger}")
        print(f"      {item.why}")
        if item.retailers:
            print(f"      {', '.join(item.retailers)}")

    summary = gearmod.budget_summary(items, cfg["gear"].get("budget_eur"))
    cur = currency_code(cfg)
    print(f"\n  Essential {cur} {summary['essential']}"
          f" | Recommended {cur} {summary['recommended']}"
          f" | Optional {cur} {summary['optional']}"
          f" | Total {cur} {summary['total']}")
    return 0


def cmd_import_strava(cfg: dict, args) -> int:
    imported, skipped = logbook.import_strava_csv(cfg, args.path, args.unit)
    print(f"Imported {imported} activities, skipped {skipped}.")
    return 0


def cmd_sync_strava(cfg: dict, args) -> int:
    from . import strava
    if not strava.is_configured(cfg):
        print("Set strava.client_id and strava.client_secret first "
              "(Log tab in the dashboard, or overrides.yaml).")
        return 1
    if not strava.is_connected(cfg):
        print("Not connected. Open the dashboard Log tab and click Connect Strava.")
        print(f"Authorize URL:\n  {strava.authorize_url(cfg)}")
        return 1
    result = strava.sync_activities(cfg, lookback_days=args.days)
    print(f"Strava sync: {result}")
    if result.imported:
        plan = _fresh_plan(cfg)
        adapted = adapt_plan(cfg, plan)
        schedulemod.apply_schedule_moves(adapted.plan, cfg)
        save(adapted.plan, cfg["plan_path"])
        calendar_feed.write(adapted.plan, cfg)
        print(f"Plan re-adapted ({len(adapted.adaptations)} change(s)).")
    return 0


def cmd_import_gpx(cfg: dict, args) -> int:
    result = logbook.import_gpx(cfg, args.path)
    print(f"Imported {result['miles']:g} mi on {result['date']}.")
    return 0


# --- Rendering -------------------------------------------------------------

def _print_week(week, plan: Plan) -> None:
    header = (f"Week {week.index}/{len(plan.weeks)}  "
              f"{week.start} to {week.end}  "
              f"[{week.phase} phase, week {week.phase_week}, {week.kind}]")
    print(header)
    print("-" * len(header))
    print(f"  {week.planned_miles:g} mi planned over "
          f"{len([s for s in week.run_sessions if not s.optional])} runs, "
          f"long run {week.long_run_day.title()}")
    if week.focus:
        print(f"  Focus: {week.focus}")
    print()

    last_day = None
    for session in week.sessions:
        if session.day != last_day:
            print(f"  {session.day.title()[:3]} {session.date}")
            last_day = session.day
        _print_session(session, indent="      ")

    if week.note:
        print(f"\n  {week.note}")
    if week.adaptations:
        print("\n  Re-planned:")
        for note in week.adaptations:
            print(f"    {note}")


def _print_session(session, indent: str = "  ") -> None:
    if session.kind == "rest":
        print(f"{indent}Rest")
        return

    label = session.title + (" [optional]" if session.optional else "")
    mark = DAYLIGHT_MARK.get(session.daylight, "")
    print(f"{indent}{label}{mark}")
    if session.detail:
        print(f"{indent}  {session.detail}")
    if session.pace_low and session.pace_high:
        print(f"{indent}  {format_pace(session.pace_low)}-"
              f"{format_pace(session.pace_high)}  "
              f"({format_pace_km(session.pace_high)}-"
              f"{format_pace_km(session.pace_low)})"
              f"  ~{format_duration(session.duration_minutes * 60)}")
    if session.fallback:
        print(f"{indent}  {session.fallback}")


def _print_overview(plan: Plan) -> None:
    print(f"{'wk':>3} {'start':<12} {'phase':<9} {'kind':<9} "
          f"{'miles':>6} {'long':>6}")
    for week in plan.weeks:
        long_run = next((s for s in week.run_sessions if s.role == "long"), None)
        print(f"{week.index:>3} {week.start:<12} {week.phase:<9} "
              f"{week.kind:<9} {week.planned_miles:>6.1f} "
              f"{(long_run.miles if long_run else 0):>6.1f}")


# --- Helpers ---------------------------------------------------------------

def _fresh_plan(cfg: dict) -> Plan:
    fitness = metrics.current_fitness(cfg)
    overrides = logbook.long_run_overrides(cfg)
    return build_plan(cfg, fitness=fitness, long_run_overrides=overrides)


def _load_plan(cfg: dict, quiet: bool = False) -> Plan | None:
    path = Path(cfg["plan_path"])
    if path.exists():
        return load(path)
    if not quiet:
        print("No plan yet - generating one.")
    plan = _fresh_plan(cfg)
    result = adapt_plan(cfg, plan)
    schedulemod.apply_schedule_moves(result.plan, cfg)
    save(result.plan, path)
    return result.plan


def _week_start(plan: Plan, index: int) -> date | None:
    for week in plan.weeks:
        if week.index == index:
            return week.start_date
    return None


def _weeks_out(cfg: dict, as_of: date) -> int:
    return max(0, (cfg["race"]["date"] - as_of).days // 7)


if __name__ == "__main__":
    raise SystemExit(main())
