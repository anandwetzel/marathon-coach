"""Manual schedule moves: drag sessions to new days and keep them across rebuilds.

Fingerprints are stable across regenerates (origin week + role + kind + miles +
occurrence), so a drag survives `generate` / adapt. Apple Calendar UIDs use the
same fingerprint so a moved session updates in place instead of duplicating.
"""

from __future__ import annotations

from datetime import date, timedelta

from .. import log as logbook
from . import daylight as D
from .generator import DAYS, Plan, PlannedSession, PlannedWeek, save


KIND_COLOUR = {
    "easy": "#1f6b8a",
    "recovery": "#4a8fa8",
    "long": "#c45c16",
    "tempo": "#b33a3a",
    "intervals": "#9c2f5a",
    "marathon_pace": "#c45c16",
    "strides": "#5a7a8a",
    "race": "#a32020",
    "climb": "#5b4a9c",
    "strength": "#2f6b52",
    "rest": "#8a8a8a",
}

ROLE_LABEL = {
    "long": "Long Run",
    "easy": "Easy Run",
    "quality": "Quality Run",
    "medium": "Medium Run",
    "optional": "Optional Run",
    "climb": "Cross-train",
    "strength": "Strength",
    "rest": "Rest",
}


def _origin(week: PlannedWeek, session: PlannedSession) -> int:
    return session.origin_week or week.index


def _base_key(origin: int, session: PlannedSession) -> str:
    return (
        f"{origin}|{session.role}|{session.kind}|"
        f"{session.miles:g}|{session.strength_focus}|"
        f"{int(session.optional)}"
    )


def assign_fingerprints(plan: Plan) -> dict[str, PlannedSession]:
    """Return fingerprint -> session for every session in the plan."""
    grouped: dict[int, list[tuple[PlannedWeek, PlannedSession]]] = {}
    for week in plan.weeks:
        for session in week.sessions:
            grouped.setdefault(_origin(week, session), []).append((week, session))

    by_fp: dict[str, PlannedSession] = {}
    for origin, items in grouped.items():
        counts: dict[str, int] = {}
        for _week, session in items:
            base = _base_key(origin, session)
            n = counts.get(base, 0)
            counts[base] = n + 1
            by_fp[f"{base}|{n}"] = session
    return by_fp


def calendar_uid(fingerprint: str) -> str:
    safe = fingerprint.replace("|", "-").replace(" ", "")
    return f"{safe}@marathon-coach"


def apply_schedule_moves(plan: Plan, cfg: dict) -> int:
    """Apply persisted moves onto a freshly built plan. Returns moves applied."""
    moves = logbook.schedule_moves(cfg)
    if not moves:
        return 0
    applied = 0
    for fingerprint, to_date in moves.items():
        if move_session(plan, fingerprint, date.fromisoformat(to_date), cfg,
                        persist=False):
            applied += 1
    return applied


def move_session(plan: Plan, fingerprint: str, new_day: date, cfg: dict,
                 persist: bool = True) -> bool:
    """Move one session to new_day. Optionally persist the override."""
    located = _find(plan, fingerprint)
    if not located:
        return False
    week, session = located
    if session.as_date == new_day:
        return False
    if session.is_race:
        race = week.race or {}
        if race and not race.get("optional", True):
            return False

    old_day = session.as_date
    target_week = plan.week_for(new_day)
    if target_week is None:
        return False

    if session.origin_week == 0:
        session.origin_week = week.index

    if target_week is not week:
        week.sessions.remove(session)
        target_week.sessions.append(session)
        _ensure_rest_day(plan, old_day)
        week = target_week

    session.date = new_day.isoformat()
    session.day = DAYS[new_day.weekday()]
    _refresh_timing(session, cfg)

    if session.role == "long":
        week.long_run_day = session.day

    if target_week.contains(old_day) or plan.week_for(old_day) is not None:
        _ensure_rest_day(plan, old_day)
    _strip_rest_if_busy(week, new_day)
    _sort_week(week)

    if persist:
        logbook.set_schedule_move(cfg, fingerprint, new_day)

    return True


def plan_to_events(plan: Plan, cfg: dict,
                   include_rest: bool = False) -> list[dict]:
    """FullCalendar events with short titles (preview) and rich extendedProps."""
    from ..config import cross_training_name
    cross_name = cross_training_name(cfg)
    events: list[dict] = []
    for fingerprint, session in assign_fingerprints(plan).items():
        if session.kind == "rest" and not include_rest:
            continue
        week = plan.week_for(session.as_date) or next(
            (w for w in plan.weeks if session in w.sessions), plan.weeks[0])
        start, end = _event_bounds(session, cfg)
        colour = KIND_COLOUR.get(session.kind, "#555555")
        if session.role == "long":
            colour = KIND_COLOUR["long"]
        events.append({
            "id": fingerprint,
            "title": _preview_title(session, cross_name),
            "start": start,
            "end": end,
            "allDay": True,
            "backgroundColor": colour,
            "borderColor": colour,
            "textColor": "#ffffff",
            "extendedProps": {
                "fingerprint": fingerprint,
                "week": week.index,
                "origin_week": session.origin_week or week.index,
                "role": session.role,
                "kind": session.kind,
                "detail": session.detail,
                "full_title": session.title,
                "miles": session.miles,
                "daylight": session.daylight,
                "optional": session.optional,
                "pace_low": session.pace_low,
                "pace_high": session.pace_high,
                "duration_minutes": session.duration_minutes,
                "daylight_note": session.daylight_note,
                "fallback": session.fallback,
                "strength_focus": session.strength_focus,
            },
        })
    return events


def save_and_publish(plan: Plan, cfg: dict) -> None:
    from .. import calendar_feed
    save(plan, cfg["plan_path"])
    calendar_feed.write(plan, cfg)


def _find(plan: Plan, fingerprint: str
          ) -> tuple[PlannedWeek, PlannedSession] | None:
    session = assign_fingerprints(plan).get(fingerprint)
    if not session:
        return None
    for week in plan.weeks:
        if session in week.sessions:
            return week, session
    return None


def _refresh_timing(session: PlannedSession, cfg: dict) -> None:
    if session.kind == "rest":
        return
    if session.kind in {"climb", "strength"}:
        start = D.session_start(session.as_date, cfg)
        session.start_time = start.strftime("%H:%M")
        return
    if not session.miles:
        return
    start = D.session_start(session.as_date, cfg)
    session.start_time = start.strftime("%H:%M")
    duration = session.duration_minutes or 40.0
    tag = D.classify(session.as_date, start, duration, cfg)
    session.daylight = tag
    session.daylight_note = D.daylight_note(tag, session.as_date, cfg)


def _ensure_rest_day(plan: Plan, day: date) -> None:
    week = plan.week_for(day)
    if not week:
        return
    day_str = day.isoformat()
    remaining = [s for s in week.sessions if s.date == day_str]
    if remaining:
        return
    week.sessions.append(PlannedSession(
        date=day_str,
        day=DAYS[day.weekday()],
        role="rest",
        kind="rest",
        title="Rest",
        detail="No running.",
        origin_week=week.index,
    ))
    _sort_week(week)


def _strip_rest_if_busy(week: PlannedWeek, day: date) -> None:
    day_str = day.isoformat()
    busy = [s for s in week.sessions
            if s.date == day_str and s.kind != "rest"]
    if not busy:
        return
    week.sessions = [
        s for s in week.sessions
        if not (s.date == day_str and s.kind == "rest")
    ]


def _sort_week(week: PlannedWeek) -> None:
    week.sessions.sort(key=lambda s: (s.date, s.kind == "rest", s.role))


def _preview_title(session: PlannedSession, cross_name: str = "Gym") -> str:
    """Short, scannable chip text for week/month grids."""
    if session.kind == "climb":
        return cross_name
    if session.kind == "strength":
        focus = (session.strength_focus or "full").split("(")[0].strip()
        # "Lower + upper" / "Upper only" / "Light" / race-week labels
        if "upper" in focus.lower() and "lower" not in focus.lower():
            return "Strength · upper"
        if "light" in focus.lower() or "mobility" in focus.lower():
            return "Strength · light"
        if "race" in focus.lower():
            return "Strength · race week"
        if "maintenance" in focus.lower():
            return "Strength · maint."
        return "Strength · full"
    if session.kind == "rest":
        return "Rest"
    if session.is_race or session.kind == "race":
        miles = f" {session.miles:g} mi" if session.miles else ""
        return f"Race{miles}"

    role = ROLE_LABEL.get(session.role, session.role.title() or "Run")
    if session.role == "quality":
        kind_bit = {
            "tempo": "tempo",
            "intervals": "intervals",
            "marathon_pace": "MP",
            "strides": "strides",
        }.get(session.kind, session.kind)
        label = f"{role} · {kind_bit}" if kind_bit else role
    else:
        label = role

    if session.miles:
        label = f"{label} {session.miles:g} mi"
    if session.optional:
        label += " *"
    if session.daylight == "dark":
        label += " · dark"
    return label


def _event_bounds(session: PlannedSession, cfg: dict) -> tuple[str, str]:
    """All-day bounds so week/month chips stack cleanly (ICS still uses clock times)."""
    day = session.as_date
    return day.isoformat(), (day + timedelta(days=1)).isoformat()
