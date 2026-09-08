"""Generate a subscribable .ics feed so the plan lives in Apple Calendar.

Written as a full RFC 5545 calendar rather than individual invites: re-running
the generator rewrites every event in place, because each one keeps a stable
UID derived from its date and role. A re-planned week therefore updates in the
calendar instead of duplicating.

Event times are written in UTC (…Z). That avoids needing a VTIMEZONE block,
which Apple Calendar often rejects when missing, and is the most reliable form
for Dropbox / webcal subscriptions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .athlete import format_duration, format_pace, format_pace_km
from .config import cross_training_name
from .plan.daylight import DARK, MARGINAL
from .plan.generator import Plan, PlannedSession, PlannedWeek
from .plan.schedule import assign_fingerprints, calendar_uid

PRODID = "-//marathon-coach//EN"
CALENDAR_NAME = "Marathon Training"

REMINDER_MINUTES = 60


def build_ics(plan: Plan, cfg: dict) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{CALENDAR_NAME}",
        f"X-WR-CALDESC:{_escape(plan.race_name)} on {plan.race_date}",
        f"X-WR-TIMEZONE:{cfg['location']['timezone']}",
    ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for fingerprint, session in assign_fingerprints(plan).items():
        if session.kind == "rest":
            continue
        week = next((w for w in plan.weeks if session in w.sessions), None)
        if week is None:
            continue
        lines.extend(_event(session, week, plan, cfg, stamp, fingerprint))

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _event(session: PlannedSession, week: PlannedWeek, plan: Plan,
           cfg: dict, stamp: str, fingerprint: str) -> list[str]:
    day = session.as_date
    start_time = _start_time(session, week, cfg)
    local = ZoneInfo(cfg["location"]["timezone"])
    begin_local = datetime.combine(day, start_time, tzinfo=local)
    duration = session.duration_minutes or _default_duration(session)
    end_local = begin_local + timedelta(minutes=duration)
    begin = begin_local.astimezone(timezone.utc)
    end = end_local.astimezone(timezone.utc)

    # Fingerprint-based UID stays stable when a session is dragged to a new day,
    # so Apple Calendar updates the event instead of leaving a stale copy.
    uid = calendar_uid(fingerprint)
    summary = _summary(session, week, cfg)

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{begin:%Y%m%dT%H%M%SZ}",
        f"DTEND:{end:%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(_description(session, week, plan))}",
        f"CATEGORIES:{session.kind.upper()}",
    ]
    if session.is_race:
        lines.append("PRIORITY:1")
    lines.extend([
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"TRIGGER:-PT{REMINDER_MINUTES}M",
        f"DESCRIPTION:{_escape(summary)}",
        "END:VALARM",
        "END:VEVENT",
    ])
    return lines


def _summary(session: PlannedSession, week: PlannedWeek, cfg: dict) -> str:
    if session.kind == "climb":
        return cross_training_name(cfg)
    if session.kind == "strength":
        return f"Strength: {session.strength_focus}"

    title = session.title
    if session.optional:
        title += " (optional)"
    if session.daylight == DARK:
        title += " - headlamp"
    return title


def _description(session: PlannedSession, week: PlannedWeek, plan: Plan) -> str:
    """Event body tailored to the session type.

    Cross-training and strength get only their workout prescription. Running
    events keep pace, daylight, and week-level coaching notes that actually
    apply to running - not gym work.
    """
    parts: list[str] = []
    if session.detail:
        parts.append(session.detail)

    if session.kind in {"climb", "strength"}:
        # Prescription already includes stretches and cues.
        return "\n\n".join(parts)

    if session.miles:
        pace_bits = []
        if session.pace_low and session.pace_high:
            pace_bits.append(
                f"Pace {format_pace(session.pace_low)}-{format_pace(session.pace_high)}"
                f" ({format_pace_km(session.pace_high)}-"
                f"{format_pace_km(session.pace_low)})")
        if session.duration_minutes:
            pace_bits.append(
                f"about {format_duration(session.duration_minutes * 60)}")
        if pace_bits:
            parts.append(" | ".join(pace_bits))

    if session.daylight in (DARK, MARGINAL) and session.daylight_note:
        parts.append(session.daylight_note)
    if session.fallback:
        parts.append(session.fallback)

    # Week context only on runs - gym sessions should not inherit "run easy" notes.
    context: list[str] = [
        f"Week {week.index} of {len(plan.weeks)} - {week.phase} phase, "
        f"{week.kind}, {week.planned_miles:g} mi planned."
    ]
    if week.focus:
        context.append(f"Focus: {week.focus}")
    if week.adaptations:
        context.append("Re-planned: " + "; ".join(week.adaptations))
    # Session-specific week notes (fueling, race logistics) belong on runs.
    if week.note and (
        session.is_race
        or session.role == "long"
        or session.kind in {"tempo", "marathon_pace", "intervals", "strides"}
        or week.kind in {"race", "taper"}
    ):
        context.append(week.note)
    weeks_out = _weeks_out(session.as_date, plan)
    if weeks_out is not None and weeks_out > 0 and (
            session.is_race or session.role == "long" or week.index >= 30):
        context.append(f"{weeks_out} weeks to {plan.race_name}.")

    parts.extend(context)
    return "\n\n".join(parts)


def _weeks_out(day: date, plan: Plan) -> int | None:
    race = date.fromisoformat(plan.race_date)
    delta = (race - day).days
    return delta // 7 if delta >= 0 else None


def _start_time(session: PlannedSession, week: PlannedWeek, cfg: dict):
    from datetime import time
    if session.start_time:
        hh, mm = session.start_time.split(":")[:2]
        return time(int(hh), int(mm))
    key = ("weekend_start" if session.as_date.weekday() >= 5
           else "weekday_earliest_start")
    hh, mm = str(cfg["constraints"][key]).split(":")[:2]
    return time(int(hh), int(mm))


def _default_duration(session: PlannedSession) -> float:
    if session.kind == "climb":
        return 90.0
    if session.kind == "strength":
        return 40.0
    return 45.0


def _escape(text: str) -> str:
    """Escape per RFC 5545. Long lines are left unfolded - most clients handle
    them, and folding mid-UTF8 is a common source of corrupted feeds.
    """
    return (str(text)
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n"))


def write(plan: Plan, cfg: dict, path: str | Path | None = None) -> Path:
    target = Path(path or cfg["ics_path"]).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_ics(plan, cfg), encoding="utf-8")
    return target
