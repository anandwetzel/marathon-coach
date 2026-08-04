"""Materialize the 36-week plan: turn template roles into dated sessions.

The interesting work here is day assignment. Climbing on Thursday and Saturday
is protected, the long run can land on either weekend day, and the sequencing
rules that prevent heavy legs landing the day before a long run have to hold
whichever way the week is arranged.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ..athlete import (
    Fitness,
    MARATHON_MILES,
    baseline_fitness,
    format_duration,
    format_pace,
    goal_paces,
)
from ..workouts import (
    STRENGTH_FULL,
    STRENGTH_LIGHT,
    STRENGTH_RACE_WEEK,
    STRENGTH_UPPER,
    climb_detail,
    post_run_stretch,
    rest_detail,
    strength_detail,
)
from . import daylight as D
from .templates import (
    PlanTemplate,
    RaceInfo,
    ROLE_EASY,
    ROLE_LONG,
    ROLE_MEDIUM,
    ROLE_OPTIONAL,
    ROLE_QUALITY,
    SessionTemplate,
    WeekTemplate,
    load_all,
)

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

ROLE_CLIMB = "climb"
ROLE_STRENGTH = "strength"
ROLE_REST = "rest"

# Pace bands per zone, as (seconds slower, seconds faster) around the target.
PACE_BAND = {
    "recovery": (0, 45),
    "easy": (-15, 30),
    "marathon": (-8, 8),
    "threshold": (-6, 6),
    "interval": (-5, 5),
    "repetition": (-5, 5),
    "race": (-10, 10),
}

# Long runs beyond this duration need in-run fueling rehearsal.
FUEL_THRESHOLD_MINUTES = 90


@dataclass
class PlannedSession:
    date: str
    day: str
    role: str
    kind: str
    title: str
    detail: str = ""
    miles: float = 0.0
    zone: str = ""
    pace_target: float | None = None
    pace_low: float | None = None
    pace_high: float | None = None
    duration_minutes: float = 0.0
    start_time: str = ""
    daylight: str = ""
    daylight_note: str = ""
    optional: bool = False
    is_race: bool = False
    strength_focus: str = ""
    fallback: str = ""
    # Week the session was generated in. Dragging to another week must not
    # change this - fingerprints and calendar UIDs key off it.
    origin_week: int = 0

    @property
    def as_date(self) -> date:
        return date.fromisoformat(self.date)


@dataclass
class PlannedWeek:
    index: int
    phase: str
    phase_week: int
    start: str
    end: str
    kind: str
    target_miles: float
    long_run_day: str
    focus: str = ""
    note: str = ""
    sessions: list[PlannedSession] = field(default_factory=list)
    race: dict | None = None
    adaptations: list[str] = field(default_factory=list)

    @property
    def start_date(self) -> date:
        return date.fromisoformat(self.start)

    @property
    def end_date(self) -> date:
        return date.fromisoformat(self.end)

    @property
    def run_sessions(self) -> list[PlannedSession]:
        return [s for s in self.sessions if s.miles > 0]

    @property
    def planned_miles(self) -> float:
        return round(sum(s.miles for s in self.run_sessions if not s.optional), 1)

    def contains(self, d: date) -> bool:
        return self.start_date <= d <= self.end_date


@dataclass
class Plan:
    generated_at: str
    race_name: str
    race_date: str
    goal_time: str
    must_beat: str
    vdot: float
    fitness_source: str
    weeks: list[PlannedWeek] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def week_for(self, d: date) -> PlannedWeek | None:
        for wk in self.weeks:
            if wk.contains(d):
                return wk
        return None

    def session_for(self, d: date) -> list[PlannedSession]:
        wk = self.week_for(d)
        if not wk:
            return []
        return [s for s in wk.sessions if s.date == d.isoformat()]

    @property
    def peak_week(self) -> PlannedWeek:
        return max(self.weeks, key=lambda w: w.planned_miles)


def build_plan(cfg: dict, fitness: Fitness | None = None,
               long_run_overrides: dict[int, str] | None = None) -> Plan:
    templates = load_all(cfg)
    fitness = fitness or baseline_fitness(cfg)
    overrides = long_run_overrides or {}

    plan_start = cfg["start"]["plan_start"]
    weeks: list[PlannedWeek] = []
    warnings: list[str] = []

    index = 0
    cursor = plan_start
    for phase_key in ("base", "marathon"):
        template = templates[phase_key]
        for wk_template in template.weeks:
            index += 1
            week = _build_week(
                cfg=cfg,
                fitness=fitness,
                template=template,
                wk=wk_template,
                index=index,
                monday=cursor,
                long_run_override=overrides.get(index),
            )
            weeks.append(week)
            cursor = cursor + timedelta(days=7)

    race_date = cfg["race"]["date"]
    final_end = weeks[-1].end_date
    if final_end != race_date:
        warnings.append(
            f"Plan ends {final_end} but the race is {race_date}. "
            "Adjust start.plan_start so the final Sunday lands on race day."
        )

    return Plan(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        race_name=cfg["race"]["name"],
        race_date=race_date.isoformat(),
        goal_time=cfg["race"]["goal_time"],
        must_beat=cfg["race"]["must_beat"],
        vdot=round(fitness.vdot, 1),
        fitness_source=fitness.source,
        weeks=weeks,
        warnings=warnings,
    )


def _build_week(cfg: dict, fitness: Fitness, template: PlanTemplate,
                wk: WeekTemplate, index: int, monday: date,
                long_run_override: str | None) -> PlannedWeek:
    constraints = cfg["constraints"]
    long_day = _choose_long_run_day(cfg, wk, long_run_override)
    layout = _day_layout(cfg, wk, long_day)

    sessions: list[PlannedSession] = []
    for offset, day_name in enumerate(DAYS):
        d = monday + timedelta(days=offset)
        for item in layout.get(day_name, []):
            sessions.append(_materialize(cfg, fitness, wk, item, d, day_name, layout))

    for session in sessions:
        session.origin_week = index

    return PlannedWeek(
        index=index,
        phase=template.phase,
        phase_week=wk.week,
        start=monday.isoformat(),
        end=(monday + timedelta(days=6)).isoformat(),
        kind=wk.kind,
        target_miles=wk.total,
        long_run_day=long_day,
        focus=wk.focus,
        note=wk.note,
        sessions=sessions,
        race=_race_dict(wk.race) if wk.race else None,
    )


def _choose_long_run_day(cfg: dict, wk: WeekTemplate, override: str | None) -> str:
    allowed = [d.lower() for d in cfg["constraints"]["long_run_days"]]
    if override and override.lower() in allowed:
        return override.lower()
    # Races have a fixed day; Dutch road races are Sunday events.
    if wk.race:
        return "sunday"
    default = str(cfg["constraints"]["default_long_run_day"]).lower()
    return default if default in allowed else allowed[-1]


def _day_layout(cfg: dict, wk: WeekTemplate,
                long_day: str) -> dict[str, list[dict]]:
    """Assign session roles to weekdays.

    Rules enforced here:
      - the configured rest day stays clear of running
      - climbing days are protected; if the long run needs one, that climb
        moves to the other weekend day rather than being dropped
      - the quality session never lands the day after a heavy lift
      - a strength session the day before a long run drops lower-body work,
        and the day after one is mobility only
    """
    constraints = cfg["constraints"]
    climbing = [d.lower() for d in constraints["climbing_days"]]
    rest_day = str(constraints["rest_day"]).lower()
    strength_on = bool(constraints.get("strength_enabled", True))

    layout: dict[str, list[dict]] = {d: [] for d in DAYS}

    # 1. Long run first - everything else arranges around it.
    long_tpl = wk.long_run
    if long_tpl:
        layout[long_day].append({"role": ROLE_LONG, "template": long_tpl})

    # 2. Climbing. A climb displaced by the long run moves to the other
    #    weekend day so the second session is not simply lost.
    climb_days = []
    for day in climbing:
        if day == long_day:
            alt = "sunday" if long_day == "saturday" else "saturday"
            if alt != long_day and alt not in climb_days:
                day = alt
            else:
                continue
        if day not in climb_days:
            climb_days.append(day)
    for day in climb_days:
        layout[day].append({"role": ROLE_CLIMB})

    # 3. Running days: everything that is not rest, not the long run day.
    available = [d for d in DAYS if d != rest_day and d != long_day]
    # Prefer days without climbing, and keep the natural Tue/Wed/Fri rhythm.
    available.sort(key=lambda d: (d in climb_days, DAYS.index(d)))

    remaining = [s for s in wk.sessions if s.role != ROLE_LONG]
    ordered = (
        [s for s in remaining if s.role == ROLE_QUALITY]
        + [s for s in remaining if s.role == ROLE_MEDIUM]
        + [s for s in remaining if s.role == ROLE_EASY]
        + [s for s in remaining if s.role == ROLE_OPTIONAL]
    )

    long_index = DAYS.index(long_day)
    for tpl in ordered:
        day = _pick_day(tpl, available, layout, climb_days, long_index, rest_day)
        layout[day].append({"role": tpl.role, "template": tpl})

    # 4. Strength, paired to climbing so it never adds a separate gym trip.
    if strength_on:
        for day in climb_days:
            focus = _strength_focus(day, long_day, wk)
            layout[day].append({"role": ROLE_STRENGTH, "focus": focus})

    # 5. Rest days are explicit so they show up in the calendar.
    for day in DAYS:
        if not layout[day]:
            layout[day].append({"role": ROLE_REST})

    return layout


def _pick_day(tpl: SessionTemplate, available: list[str],
              layout: dict[str, list[dict]], climb_days: list[str],
              long_index: int, rest_day: str) -> str:
    """Choose the best day for one session."""
    # Doubling a run onto a climbing day is always worse than using a free day,
    # so every climb-day cost stays positive - it only ranks which session is
    # least disruptive when the free days run out.
    climb_cost = {ROLE_QUALITY: 20, ROLE_MEDIUM: 10, ROLE_EASY: 4, ROLE_OPTIONAL: 2}
    before_long_cost = {ROLE_QUALITY: 15, ROLE_MEDIUM: 3}
    after_long_cost = {ROLE_QUALITY: 12}

    def penalty(day: str) -> tuple:
        idx = DAYS.index(day)
        already_running = any(item.get("template") for item in layout[day])

        cost = 0
        if already_running:
            cost += 100          # never double up before every day is used
        if day in climb_days:
            cost += climb_cost.get(tpl.role, 5)
        if (long_index - idx) == 1:
            cost += before_long_cost.get(tpl.role, 0)
        if (idx - long_index) == 1:
            cost += after_long_cost.get(tpl.role, 0)
        return (cost, idx)

    return min(available, key=penalty)


def _strength_focus(day: str, long_day: str, wk: WeekTemplate) -> str:
    """Heavy legs the day before a long run is how long runs go wrong."""
    # Goal race week: nothing loaded, all week. There is no strength gain left to
    # bank this close in, and squats even three days out leave legs flat.
    if _is_goal_race_week(wk):
        return STRENGTH_RACE_WEEK
    idx, long_idx = DAYS.index(day), DAYS.index(long_day)
    if long_idx - idx == 1:
        return STRENGTH_UPPER
    if idx - long_idx == 1:
        return STRENGTH_LIGHT
    if wk.kind in {"peak", "taper"}:
        return f"{STRENGTH_FULL} (maintenance loads)"
    return STRENGTH_FULL


def _is_goal_race_week(wk: WeekTemplate) -> bool:
    """The marathon itself, as opposed to an optional tune-up or timed trial."""
    return bool(wk.race and not wk.race.optional)


def _materialize(cfg: dict, fitness: Fitness, wk: WeekTemplate, item: dict,
                 d: date, day_name: str, layout: dict) -> PlannedSession:
    role = item["role"]

    if role == ROLE_REST:
        return PlannedSession(
            date=d.isoformat(), day=day_name, role=ROLE_REST, kind="rest",
            title="Rest", detail=rest_detail(),
        )

    if role == ROLE_CLIMB:
        return PlannedSession(
            date=d.isoformat(), day=day_name, role=ROLE_CLIMB, kind="climb",
            title=("Climbing (skip if travelling)" if _is_goal_race_week(wk)
                   else "Climbing"),
            optional=_is_goal_race_week(wk),
            detail=climb_detail(race_week=_is_goal_race_week(wk)),
        )

    if role == ROLE_STRENGTH:
        focus = item.get("focus", STRENGTH_FULL)
        return PlannedSession(
            date=d.isoformat(), day=day_name, role=ROLE_STRENGTH, kind="strength",
            title=f"Strength: {focus}", detail=strength_detail(focus),
            strength_focus=focus,
        )

    tpl: SessionTemplate = item["template"]
    zone = tpl.zone
    pace = fitness.paces.get(zone)
    if zone == "race":
        pace = _race_pace(cfg, fitness, tpl)

    low = high = None
    if pace:
        slower, faster = PACE_BAND.get(zone, (-10, 10))
        low, high = pace + slower, pace + faster

    duration = (tpl.miles * pace / 60.0) if pace else 0.0
    start = D.session_start(d, cfg)
    # Races start when the gun goes, not when the athlete would choose.
    if tpl.is_race and wk.race and wk.race.start_time:
        hh, mm = str(wk.race.start_time).split(":")[:2]
        start = time(int(hh), int(mm))
    tag = D.classify(d, start, duration, cfg) if duration else D.LIT

    title, detail = _describe(cfg, fitness, wk, tpl, duration)
    # Every run finishes with a short stretch block specific to the session.
    stretch_kind = "long" if tpl.role == ROLE_LONG else tpl.kind
    detail = f"{detail}\n\n{post_run_stretch(stretch_kind)}"

    return PlannedSession(
        date=d.isoformat(),
        day=day_name,
        role=tpl.role,
        kind="race" if tpl.is_race else tpl.kind,
        title=title,
        detail=detail,
        miles=tpl.miles,
        zone=zone,
        pace_target=round(pace, 1) if pace else None,
        pace_low=round(low, 1) if low else None,
        pace_high=round(high, 1) if high else None,
        duration_minutes=round(duration, 1),
        start_time=start.strftime("%H:%M"),
        daylight=tag,
        daylight_note=D.daylight_note(tag, d, cfg),
        optional=tpl.optional,
        is_race=tpl.is_race,
        fallback=_fallback(cfg, tpl, tag),
    )


def _fallback(cfg: dict, tpl: SessionTemplate, tag: str) -> str:
    """What to do when the route is dark or the weather is genuinely unrunnable.

    Without a treadmill the answer is a lit route, so the useful thing is to
    name one in advance rather than decide in the rain.
    """
    if tpl.is_race or tag == D.LIT:
        return ""

    constraints = cfg["constraints"]
    if constraints.get("treadmill_access"):
        return "If conditions are bad, run this on the treadmill."

    routes = constraints.get("lit_routes") or []
    place = (cfg.get("location") or {}).get("name") or "your usual routes"
    if tpl.kind in {"tempo", "marathon_pace", "intervals"} and routes:
        return (f"Quality in the dark needs a lit, even surface: {routes[0]}. "
                f"Unlit gravel around {place} is how ankles get rolled.")
    if routes:
        # Descriptors on the configured routes explain what each is good for,
        # which only matters when pace does; an easy run just needs streetlights.
        names = ", ".join(r.split(",")[0].strip() for r in routes[:2])
        return (f"Dark, so stay on lit ground: {names}. If it is icy, swap this "
                f"for climbing and let the plan absorb the miles - a fall costs "
                f"more than a missed run.")
    return ("Stay on lit, even ground. If it is icy, skip it rather than risk "
            "a fall; the plan re-ramps automatically.")


def _race_pace(cfg: dict, fitness: Fitness, tpl: SessionTemplate) -> float:
    """Target pace for a race session."""
    if abs(tpl.miles - MARATHON_MILES) < 0.5:
        # The marathon itself is run at the goal pace, not a predicted one.
        return goal_paces(cfg)["goal"]
    from ..athlete import race_time_for_vdot
    return race_time_for_vdot(fitness.vdot, tpl.miles) / tpl.miles


def _describe(cfg: dict, fitness: Fitness, wk: WeekTemplate,
              tpl: SessionTemplate, duration: float) -> tuple[str, str]:
    paces = fitness.paces
    mp = paces.get("marathon")
    goal_mp = goal_paces(cfg)["goal"]

    if tpl.is_race:
        race = wk.race
        label = race.distance_label if race else f"{tpl.miles:g} mi"
        name = race.name if race else "Race"
        is_trial = bool(race and race.is_trial)
        title = (f"{label} timed trial: {name}" if is_trial
                 else f"{label} race: {name}")
        if abs(tpl.miles - MARATHON_MILES) < 0.5:
            detail = (f"Race day. Target {format_pace(goal_mp)} for "
                      f"{cfg['race']['goal_time']}, "
                      f"{format_pace(goal_paces(cfg)['must_beat'])} still breaks "
                      f"{cfg['race']['must_beat']}.")
        elif is_trial:
            pace = _race_pace(cfg, fitness, tpl)
            detail = (f"Solo time trial at genuine race effort, roughly "
                      f"{format_pace(pace)}. Flat measured route, full warmup, "
                      f"no stops. Log it as a race - the result recalibrates "
                      f"every training pace.")
        else:
            pace = _race_pace(cfg, fitness, tpl)
            detail = (f"Race effort, roughly {format_pace(pace)}. "
                      f"Result recalibrates every training pace.")
        return title, detail

    band = _band_text(tpl.zone, fitness)

    if tpl.kind == "tempo":
        warm = max(1.0, round((tpl.miles - (tpl.tempo_minutes or 20) / 8.0) / 2, 1))
        title = f"Tempo {tpl.miles:g} mi"
        detail = (f"{warm:g} mi warmup, {tpl.tempo_minutes} min at threshold "
                  f"{band}, {warm:g} mi cooldown. Comfortably hard - you could "
                  f"speak a sentence, not hold a conversation.")
        return title, detail

    if tpl.kind == "marathon_pace":
        mp_miles = tpl.mp_miles or 3.0
        rest = max(0.5, (tpl.miles - mp_miles) / 2)
        title = f"Marathon pace {tpl.miles:g} mi"
        detail = (f"{rest:g} mi easy, {mp_miles:g} mi at marathon pace "
                  f"{format_pace(mp)} (goal pace {format_pace(goal_mp)}), "
                  f"{rest:g} mi easy.")
        return title, detail

    if tpl.kind == "strides":
        title = f"Easy {tpl.miles:g} mi + strides"
        detail = (f"{tpl.miles:g} mi easy {band}, then {tpl.reps} x "
                  f"{tpl.rep_seconds}s strides with full walk-back recovery. "
                  f"Fast but relaxed, not a sprint.")
        return title, detail

    if tpl.kind == "intervals":
        title = f"Intervals {tpl.miles:g} mi"
        detail = f"{tpl.reps} x {tpl.rep_seconds}s at interval pace {band}."
        return title, detail

    if tpl.role == ROLE_LONG:
        title = f"Long run {tpl.miles:g} mi"
        parts = [f"Steady and conversational {band}."]
        if tpl.mp_finish_miles:
            parts.append(
                f"Last {tpl.mp_finish_miles:g} mi at marathon pace "
                f"{format_pace(mp)} on tired legs."
            )
        if duration >= FUEL_THRESHOLD_MINUTES:
            parts.append(
                f"About {format_duration(duration * 60)} - take a gel every 40 min "
                f"with water, and practise exactly what you will use on race day."
            )
        return title, " ".join(parts)

    if tpl.role == ROLE_MEDIUM:
        return f"Medium run {tpl.miles:g} mi", f"Steady aerobic running {band}."

    label = "Recovery" if tpl.zone == "recovery" else "Easy"
    title = f"{label} {tpl.miles:g} mi"
    detail = f"{label} effort {band}."
    if tpl.optional:
        detail += " Optional - take it when the legs feel good, skip it when they do not."
    return title, detail


def _band_text(zone: str, fitness: Fitness) -> str:
    pace = fitness.paces.get(zone)
    if not pace:
        return ""
    slower, faster = PACE_BAND.get(zone, (-10, 10))
    return f"({format_pace(pace + slower)}-{format_pace(pace + faster)})"


def _race_dict(race: RaceInfo) -> dict:
    return asdict(race)


def save(plan: Plan, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(asdict(plan), f, indent=2)


def load(path: str | Path) -> Plan:
    with open(path) as f:
        raw = json.load(f)
    weeks = []
    for w in raw["weeks"]:
        sessions = [PlannedSession(**s) for s in w.pop("sessions", [])]
        weeks.append(PlannedWeek(sessions=sessions, **w))
    raw["weeks"] = weeks
    return Plan(**raw)
