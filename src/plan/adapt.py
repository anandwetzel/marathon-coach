"""The rules engine: re-plan the remaining weeks from what actually happened.

Every rule here is deterministic. Given the same log, config and date, the same
plan comes out. Rules run in a fixed order, each recording what it changed and
why, so the plan can always explain itself.

Order matters: travel and long absences reshape a week wholesale, so they run
before the ramp cap, which then smooths whatever those left behind.
"""

from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta

from ..athlete import parse_pace
from .. import log as logbook
from .generator import Plan, PlannedSession, PlannedWeek
from .templates import DROP_ORDER

# Below this weekly volume a percentage cap is meaningless, so an absolute
# step governs instead (10% of a 9-mile week is under a mile).
LOW_VOLUME_MILES = 15.0
MIN_ABSOLUTE_STEP = 1.5

# How far back the ramp cap looks for the volume to measure a build against.
# Three weeks is the shortest window that still spans a build-build-cutback
# microcycle, so planned recovery weeks are forgiven while a longer interruption
# is not.
ANCHOR_WEEKS = 3

# After downscaling, never let one session carry more than this share of a week.
# Higdon-style plans legitimately run close to half, so this is a safety net for
# rescaled weeks rather than a target - it exists to stop a shrunken week from
# leaving an untouched long run as almost all of its volume. Prefer
# rules.long_run_hard_share in config when set.
DEFAULT_LONG_RUN_HARD_SHARE = 0.55

# A tune-up half marathon costs about this much of the following long run.
POST_RACE_LONG_RUN_CUT = 2.0
POST_RACE_MIN_MILES = 10.0

RULE_TRIP = "trip"
RULE_GAP = "gap"
RULE_PAIN = "pain"
RULE_ACWR = "workload"
RULE_RAMP = "ramp"
RULE_POST_RACE = "post-race"
RULE_LONG_CAP = "long-run cap"
RULE_TAPER = "taper"


@dataclass
class Adaptation:
    week_index: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"week {self.week_index} [{self.rule}] {self.message}"


@dataclass
class AdaptResult:
    plan: Plan
    adaptations: list[Adaptation]

    def for_week(self, index: int) -> list[Adaptation]:
        return [a for a in self.adaptations if a.week_index == index]


def adapt_plan(cfg: dict, plan: Plan, as_of: date | None = None) -> AdaptResult:
    as_of = as_of or date.today()
    plan = copy.deepcopy(plan)
    adaptations: list[Adaptation] = []

    for week in plan.weeks:
        week.adaptations = []

    _apply_trips(cfg, plan, as_of, adaptations)
    _apply_gap(cfg, plan, as_of, adaptations)
    _apply_pain(cfg, plan, as_of, adaptations)
    _apply_post_race(cfg, plan, as_of, adaptations)
    _apply_ramp_cap(cfg, plan, as_of, adaptations)
    _apply_workload_hold(cfg, plan, as_of, adaptations)
    _enforce_long_run_caps(cfg, plan, as_of, adaptations)
    _enforce_taper(cfg, plan, as_of, adaptations)

    for note in adaptations:
        week = _week_by_index(plan, note.week_index)
        if week is not None:
            week.adaptations.append(f"[{note.rule}] {note.message}")

    return AdaptResult(plan=plan, adaptations=adaptations)


# --- Rules -----------------------------------------------------------------

def _apply_trips(cfg: dict, plan: Plan, as_of: date,
                 notes: list[Adaptation]) -> None:
    """Reshape travel weeks to the running that is actually possible.

    Quality work is dropped first: it needs routes and recovery that travel
    rarely allows. One longer effort is protected, because losing the long run
    entirely is what actually costs marathon fitness.
    """
    for trip in cfg.get("trips") or []:
        start, end = trip["start"], trip["end"]
        runs_possible = int(trip.get("runs_possible", 1))
        max_minutes = float(trip.get("max_run_minutes", 45))
        name = trip.get("name", "trip")

        for week in plan.weeks:
            if week.end_date < as_of or week.end_date < start or week.start_date > end:
                continue

            affected = [s for s in week.sessions
                        if s.miles > 0 and start <= s.as_date <= end]
            if not affected:
                continue

            keep = _choose_trip_keepers(affected, runs_possible)
            easy_pace = _easy_pace(cfg, week)
            max_miles = round(max_minutes / (easy_pace / 60.0), 1)

            dropped, shortened = [], []
            for session in affected:
                if session not in keep:
                    dropped.append(session)
                    _blank(session, f"Travelling ({name}) - rest day.")
                elif session.miles > max_miles:
                    shortened.append((session.miles, max_miles))
                    _resize(session, max_miles, easy_pace)

            week.kind = "trip"
            detail = []
            if keep:
                detail.append(f"{len(keep)} run(s) kept")
            if dropped:
                detail.append(f"{len(dropped)} dropped")
            if shortened:
                detail.append(f"longest capped at {max_miles:g} mi")
            notes.append(Adaptation(
                week.index, RULE_TRIP,
                f"{name} ({start} to {end}): " + ", ".join(detail)
                + ". Volume rebuilds under the ramp cap on return."))


def _choose_trip_keepers(sessions: list[PlannedSession],
                         runs_possible: int) -> list[PlannedSession]:
    if runs_possible <= 0:
        return []
    # Protect the long run first, then work back up the drop order.
    priority = list(reversed(DROP_ORDER))
    ranked = sorted(
        sessions,
        key=lambda s: (priority.index(s.role) if s.role in priority else 99,
                       -s.miles),
    )
    return ranked[:runs_possible]


def _apply_gap(cfg: dict, plan: Plan, as_of: date,
               notes: list[Adaptation]) -> None:
    """Re-entry after time off, tiered by how long the gap ran.

    Fitness does not vanish in a week, but it does after a month, and the most
    common way people get hurt is picking up exactly where they left off.
    """
    rules = cfg["rules"]
    gap = logbook.days_since_last_run(cfg, as_of)
    if gap is None or gap < int(rules["gap_repeat_days"]):
        return

    last_volume = _last_observed_volume(cfg, plan, as_of)
    if last_volume <= 0:
        last_volume = float(cfg["start"]["current_weekly_miles"])

    upcoming = [w for w in plan.weeks if w.end_date >= as_of]
    if not upcoming:
        return
    week = upcoming[0]

    if gap >= int(rules["gap_rebase_days"]):
        target = max(float(cfg["start"]["current_weekly_miles"]), last_volume * 0.5)
        message = (f"{gap} days off - back to base. This week is {target:.1f} mi "
                   f"and volume rebuilds from there under the ramp cap.")
    elif gap >= int(rules["gap_reduce_days"]):
        target = last_volume * float(rules["gap_reduce_pct"])
        message = (f"{gap} days off - this week drops to "
                   f"{rules['gap_reduce_pct']:.0%} of your last real week "
                   f"({last_volume:.1f} mi) at {target:.1f} mi.")
    else:
        target = last_volume
        message = (f"{gap} days off - repeating your last completed week "
                   f"({target:.1f} mi) rather than resuming the build.")

    if week.planned_miles > target > 0:
        _scale_week(week, target / week.planned_miles, cfg)
        week.kind = "cutback"
        notes.append(Adaptation(week.index, RULE_GAP, message))


def _apply_pain(cfg: dict, plan: Plan, as_of: date,
                notes: list[Adaptation]) -> None:
    """Repeated pain forces a cutback whatever the plan says."""
    rules = cfg["rules"]
    from ..metrics import pain_streak

    streak = pain_streak(cfg)
    if streak < int(rules["pain_consecutive"]):
        return

    upcoming = [w for w in plan.weeks if w.end_date >= as_of]
    if not upcoming:
        return
    week = upcoming[0]
    if week.kind in {"cutback", "taper"}:
        return

    _scale_week(week, 0.70, cfg)
    week.kind = "cutback"
    notes.append(Adaptation(
        week.index, RULE_PAIN,
        f"{streak} consecutive runs logged pain at or above "
        f"{rules['pain_threshold']} - this week cut to 70% and all quality "
        f"removed. If it does not settle, stop guessing and get it looked at."))
    for session in week.sessions:
        if session.kind in {"tempo", "marathon_pace", "intervals", "strides"}:
            _to_easy(session, cfg, week)


def _apply_post_race(cfg: dict, plan: Plan, as_of: date,
                     notes: list[Adaptation]) -> None:
    """A raced half marathon costs more recovery than the plan assumes."""
    race = logbook.most_recent_race(cfg, min_miles=POST_RACE_MIN_MILES)
    if not race:
        return
    race_date = race["date"]
    if isinstance(race_date, str):
        race_date = date.fromisoformat(race_date)

    for week in plan.weeks:
        if week.start_date <= race_date <= week.end_date:
            following = _week_by_index(plan, week.index + 1)
            if not following or following.end_date < as_of:
                return
            long_run = _long_session(following)
            if not long_run or long_run.is_race:
                return
            reduced = max(6.0, long_run.miles - POST_RACE_LONG_RUN_CUT)
            if reduced < long_run.miles:
                _resize(long_run, reduced, long_run.pace_target
                        or _easy_pace(cfg, following))
                notes.append(Adaptation(
                    following.index, RULE_POST_RACE,
                    f"Long run cut to {reduced:g} mi after racing "
                    f"{race['miles']:.1f} mi on {race_date}."))
            return


def _apply_ramp_cap(cfg: dict, plan: Plan, as_of: date,
                    notes: list[Adaptation]) -> None:
    """Never let volume climb faster than the body adapts.

    Past weeks anchor on what was actually run, so a stretch of under-training
    pulls the whole remaining plan down rather than leaving an impossible jump.
    The athlete's current capacity (logged peak, else start.current_weekly_miles)
    seeds the window and also lifts early template weeks that sit below where
    they already are - without using the plan's own later peak as that floor.
    """
    from ..config import effective_increase_cap
    from ..metrics import recent_peak_weekly
    cap_pct = effective_increase_cap(cfg)

    # The anchor is the highest volume recently *sustained*: the peak of a short
    # rolling window rather than the previous week alone. A single planned
    # cutback therefore cannot flatten the build that follows it, because the
    # build week before the dip is still inside the window. An interruption long
    # enough to fill the window - a 2.5 week trip, illness, a stretch of missed
    # sessions - does pull the anchor down, which is correct: that is real
    # detraining, and coming back to the old number is how people get hurt.
    recent: deque[float] = deque(maxlen=ANCHOR_WEEKS)
    capacity = recent_peak_weekly(cfg, as_of)
    if capacity > 0:
        recent.append(capacity)
    previous: float | None = capacity if capacity > 0 else None
    build_streak = 0

    for week in plan.weeks:
        is_build = week.kind in {"build", "peak"}
        anchor = max(recent) if recent else None

        if week.end_date < as_of:
            observed = _observed_volume(cfg, week)
            effective = observed if observed is not None else week.planned_miles
            recent.append(effective)
            build_streak = _next_streak(build_streak, is_build, effective, previous)
            previous = effective
            continue

        if is_build and anchor:
            allowed = anchor * (1 + cap_pct)
            if anchor < LOW_VOLUME_MILES:
                allowed = max(allowed, anchor + MIN_ABSOLUTE_STEP)

            if week.planned_miles > allowed + 0.05:
                original = week.planned_miles
                _scale_week(week, allowed / week.planned_miles, cfg)
                notes.append(Adaptation(
                    week.index, RULE_RAMP,
                    f"{original:.1f} mi cut to {week.planned_miles:.1f} mi - "
                    f"more than {cap_pct:.0%} above the {anchor:.1f} mi "
                    f"actually behind it."))
            elif capacity > 0 and week.planned_miles < capacity - 0.5:
                # Already running more than this template week - hold near
                # current capacity until the written plan catches up.
                original = week.planned_miles
                _scale_week(week, capacity / week.planned_miles, cfg,
                            allow_increase=True)
                notes.append(Adaptation(
                    week.index, RULE_RAMP,
                    f"{original:.1f} mi lifted to {week.planned_miles:.1f} mi - "
                    f"holding near your current {capacity:.1f} mi until the plan "
                    f"catches up."))

            build_streak = _next_streak(build_streak, True, week.planned_miles,
                                        previous)
            max_build = int(cfg["rules"]["build_weeks_before_cutback"])
            if build_streak > max_build:
                factor = float(cfg["rules"]["cutback_pct"])
                _scale_week(week, factor, cfg)
                week.kind = "cutback"
                build_streak = 0
                notes.append(Adaptation(
                    week.index, RULE_RAMP,
                    f"{max_build} straight build weeks - forced a cutback to "
                    f"{week.planned_miles:.1f} mi."))
        else:
            build_streak = _next_streak(build_streak, is_build,
                                        week.planned_miles, previous)

        recent.append(week.planned_miles)
        previous = week.planned_miles


def _next_streak(streak: int, is_build: bool, volume: float,
                 previous: float | None) -> int:
    """Count consecutive weeks that genuinely add load.

    A week labelled "build" that holds or drops volume is recovery in practice,
    which is exactly what happens where the base phase hands over to the
    marathon block. Counting labels rather than load would force a cutback
    into the middle of an already-easing week.
    """
    if not is_build:
        return 0
    if previous is not None and volume <= previous + 0.05:
        return 1
    return streak + 1


def _apply_workload_hold(cfg: dict, plan: Plan, as_of: date,
                         notes: list[Adaptation]) -> None:
    """Hold volume flat when the acute:chronic ratio is already elevated."""
    from ..metrics import acwr

    ratio, acute, chronic = acwr(cfg, as_of)
    if not ratio or ratio <= float(cfg["rules"]["acwr_flag"]):
        return

    upcoming = [w for w in plan.weeks if w.end_date >= as_of]
    if not upcoming:
        return
    week = upcoming[0]
    if week.kind in {"cutback", "taper", "race", "trip"}:
        return
    if week.planned_miles > chronic > 0:
        _scale_week(week, chronic / week.planned_miles, cfg)
        week.kind = "cutback"
        notes.append(Adaptation(
            week.index, RULE_ACWR,
            f"Workload ratio {ratio} is above {cfg['rules']['acwr_flag']} - "
            f"holding this week at your 4-week average of {chronic:.1f} mi "
            f"instead of building."))


def _enforce_long_run_caps(cfg: dict, plan: Plan, as_of: date,
                           notes: list[Adaptation]) -> None:
    """Hard ceilings that survive any amount of rescaling."""
    ceiling = float(cfg["rules"]["long_run_cap_miles"])
    for week in plan.weeks:
        if week.end_date < as_of:
            continue
        long_run = _long_session(week)
        if not long_run or long_run.is_race:
            continue

        if long_run.miles > ceiling:
            _resize(long_run, ceiling, long_run.pace_target or _easy_pace(cfg, week))
            notes.append(Adaptation(
                week.index, RULE_LONG_CAP,
                f"Long run capped at {ceiling:g} mi. Beyond that the recovery "
                f"cost outweighs the aerobic gain."))

        # Optional recovery runs count here, since taking them is recommended.
        total = week.planned_miles + sum(
            s.miles for s in week.run_sessions if s.optional)
        hard_share = float(cfg["rules"].get(
            "long_run_hard_share", DEFAULT_LONG_RUN_HARD_SHARE))
        if total > 0 and long_run.miles / total > hard_share:
            allowed = round(total * hard_share, 1)
            if allowed >= 4.0:
                _resize(long_run, allowed,
                        long_run.pace_target or _easy_pace(cfg, week))
                notes.append(Adaptation(
                    week.index, RULE_LONG_CAP,
                    f"Long run trimmed to {allowed:g} mi so it stays under "
                    f"{hard_share:.0%} of the week's volume."))


def _enforce_taper(cfg: dict, plan: Plan, as_of: date,
                   notes: list[Adaptation]) -> None:
    """The last three weeks come down regardless of what came before."""
    multipliers = [float(m) for m in cfg["rules"]["taper_multipliers"]]
    if len(plan.weeks) < len(multipliers) + 1:
        return

    peak = max(w.planned_miles for w in plan.weeks)
    taper_weeks = plan.weeks[-len(multipliers):]

    for week, multiplier in zip(taper_weeks, multipliers):
        if week.end_date < as_of:
            continue
        ceiling = peak * multiplier
        # Race week volume includes the marathon itself, which is not taper load.
        running = sum(s.miles for s in week.run_sessions
                      if not s.optional and not s.is_race)
        if running > ceiling + 0.05 and running > 0:
            for session in week.run_sessions:
                if not session.is_race:
                    _resize(session, session.miles * ceiling / running,
                            session.pace_target or _easy_pace(cfg, week))
            notes.append(Adaptation(
                week.index, RULE_TAPER,
                f"Taper: non-race volume held to {ceiling:.1f} mi "
                f"({multiplier:.0%} of the {peak:.1f} mi peak)."))


# --- Helpers ---------------------------------------------------------------

def _scale_week(week: PlannedWeek, factor: float, cfg: dict,
                allow_increase: bool = False) -> None:
    """Resize every run in a week proportionally, leaving races alone.

    Scaling everything together keeps the shape of the week intact - the long
    run stays the long run, and no single session silently becomes dominant.
    Increases are opt-in so legacy cutback callers stay one-way.
    """
    if factor >= 1.0 and not allow_increase:
        return
    if abs(factor - 1.0) < 0.005:
        return
    floor = float(cfg["start"].get("min_run_miles") or 0)
    easy_pace = _easy_pace(cfg, week)
    for session in week.run_sessions:
        if session.is_race:
            continue
        miles = max(1.0, session.miles * factor)
        # Floor only when holding or lifting volume. Applying it on a ramp cut
        # would pin a 3 x 4 mi week at 12 mi and defeat a low starting point.
        if floor and not session.optional and factor >= 1.0:
            miles = max(miles, floor)
        _resize(session, miles, session.pace_target or easy_pace)


def _resize(session: PlannedSession, miles: float, pace: float) -> None:
    miles = round(max(0.0, miles), 1)
    original = session.miles
    session.miles = miles
    if pace:
        session.duration_minutes = round(miles * pace / 60.0, 1)
    if original and miles != original:
        session.title = _retitle(session.title, original, miles)


def _retitle(title: str, original: float, miles: float) -> str:
    return title.replace(f"{original:g} mi", f"{miles:g} mi")


def _blank(session: PlannedSession, reason: str) -> None:
    session.miles = 0.0
    session.duration_minutes = 0.0
    session.kind = "rest"
    session.role = "rest"
    session.title = "Rest"
    session.detail = reason
    session.pace_target = session.pace_low = session.pace_high = None


def _to_easy(session: PlannedSession, cfg: dict, week: PlannedWeek) -> None:
    pace = _easy_pace(cfg, week)
    session.kind = "easy"
    session.zone = "easy"
    session.pace_target = round(pace, 1)
    session.title = f"Easy {session.miles:g} mi"
    session.detail = "Quality removed while pain settles. Easy effort only."
    session.duration_minutes = round(session.miles * pace / 60.0, 1)


def _easy_pace(cfg: dict, week: PlannedWeek) -> float:
    for session in week.run_sessions:
        if session.zone == "easy" and session.pace_target:
            return session.pace_target
    for session in week.run_sessions:
        if session.pace_target:
            return session.pace_target
    return parse_pace(cfg["start"]["current_easy_pace"])


def _long_session(week: PlannedWeek) -> PlannedSession | None:
    runs = [s for s in week.run_sessions if s.role == "long"]
    return runs[0] if runs else None


def _week_by_index(plan: Plan, index: int) -> PlannedWeek | None:
    for week in plan.weeks:
        if week.index == index:
            return week
    return None


def _observed_volume(cfg: dict, week: PlannedWeek) -> float | None:
    """Miles actually run in a past week.

    Returns None when nothing at all was logged, which is treated as a missing
    record rather than a missed week - otherwise a lapse in logging would
    silently dismantle the rest of the plan.
    """
    df = logbook.load_sessions(cfg, start=week.start_date, end=week.end_date)
    if df.empty:
        return None
    return float(df[df["kind"].isin(logbook.RUN_KINDS)]["miles"].sum())


def _last_observed_volume(cfg: dict, plan: Plan, as_of: date) -> float:
    for week in reversed([w for w in plan.weeks if w.end_date < as_of]):
        observed = _observed_volume(cfg, week)
        if observed:
            return observed
    return 0.0


def describe(result: AdaptResult) -> str:
    if not result.adaptations:
        return "No changes - the plan still fits what you have been doing."
    return "\n".join(str(a) for a in result.adaptations)
