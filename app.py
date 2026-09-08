from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from src import gear as gearmod, log as logbook, metrics, strava
from src.athlete import (
    ZONE_LABEL,
    format_duration,
    format_pace,
    format_pace_km,
    parse_duration,
)
from src.config import (
    load_config, save_config, currency_code, local_timezone_name,
    cross_training_name, cross_training_days,
)
from src.plan import daylight as D
from src.plan.generator import load as load_plan
from src.plan import schedule as schedulemod

try:
    from streamlit_calendar import calendar as st_calendar
except ImportError:  # pragma: no cover - optional until installed
    st_calendar = None


st.set_page_config(page_title="Marathon Coach", page_icon=":runner:",
                   layout="wide")

DAYLIGHT_BADGE = {
    "lit": ("daylight", "#2f8f4e"),
    "marginal": ("fading light", "#b8860b"),
    "dark": ("dark - headlamp", "#8b3a3a"),
}

KIND_COLOUR = {
    "build": "#2f6f9f", "cutback": "#7a7a7a", "peak": "#8b3a3a",
    "taper": "#2f8f4e", "race": "#a05000", "trip": "#6a4c93",
}


def _fc_day(raw: str) -> str:
    """Map a FullCalendar date string to YYYY-MM-DD in the laptop's local TZ.

    All-day cells often arrive as UTC midnights (Tue 00:00 local =
    Mon 22:00Z in summer Europe). Slicing the ISO string then shows the
    previous day.
    """
    text = str(raw or "").strip()
    if not text:
        return text
    if len(text) >= 10 and text[4] == "-" and text[7] == "-" and (
            len(text) == 10 or text[10] in "Tt "):
        # Pure date, or local datetime without Z — trust the date part only when
        # there is no explicit UTC/offset marker.
        if "Z" not in text.upper() and "+" not in text[10:] and text.count("-") == 2:
            return text[:10]
    try:
        normalised = text.replace("Z", "+00:00") if text.endswith(("Z", "z")) else text
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            return dt.date().isoformat()
        return dt.astimezone().date().isoformat()
    except ValueError:
        return text[:10]


# --- Data ------------------------------------------------------------------

def get_config() -> dict:
    return load_config()


def get_plan(cfg: dict, rebuild: bool = False):
    path = Path(cfg["plan_path"])
    if rebuild or not path.exists():
        return _rebuild(cfg)
    return load_plan(path)


def regenerate(cfg: dict) -> int:
    result_plan, adaptations = _rebuild(cfg, return_adaptations=True)
    return adaptations


def _rebuild(cfg: dict, return_adaptations: bool = False):
    from src.plan.rebuild import rebuild_plan
    result = rebuild_plan(cfg)
    if return_adaptations:
        return result.plan, len(result.adaptations)
    return result.plan


# --- Shared bits -----------------------------------------------------------

def countdown(cfg: dict, today: date) -> tuple[int, int]:
    delta = (cfg["race"]["date"] - today).days
    return max(delta, 0), max(delta // 7, 0)


def header(cfg: dict, plan, today: date) -> None:
    days, weeks = countdown(cfg, today)
    st.title("Marathon Coach")
    st.caption(f"{plan.race_name} - {plan.race_date} - {days} days "
               f"({weeks} weeks) to go")


def daylight_badge(tag: str) -> str:
    label, colour = DAYLIGHT_BADGE.get(tag, ("", "#666"))
    if not label:
        return ""
    return (f"<span style='background:{colour};color:white;padding:1px 6px;"
            f"border-radius:3px;font-size:0.72em'>{label}</span>")


def render_session(session, cfg: dict) -> None:
    if session.kind == "rest":
        st.markdown(f"**Rest** &nbsp; <span style='color:#888'>"
                    f"no running</span>", unsafe_allow_html=True)
        return

    title = session.title + (" *(optional)*" if session.optional else "")
    badge = daylight_badge(session.daylight) if session.miles else ""
    st.markdown(f"**{title}** &nbsp; {badge}", unsafe_allow_html=True)
    if session.detail:
        st.caption(session.detail)
    if session.pace_low and session.pace_high:
        st.caption(
            f"{format_pace(session.pace_low)}-{format_pace(session.pace_high)}"
            f"  ({format_pace_km(session.pace_high)}-"
            f"{format_pace_km(session.pace_low)})"
            f"  |  about {format_duration(session.duration_minutes * 60)}")
    if session.daylight in ("dark", "marginal") and session.daylight_note:
        st.caption(session.daylight_note)
    if session.fallback:
        st.caption(f":grey[{session.fallback}]")


# --- Pages -----------------------------------------------------------------

def page_schedule(cfg: dict, plan, today: date) -> None:
    """Week / month calendar: preview on tiles, click for detail, drag to move."""
    st.markdown(
        """
        <style>
        div[data-testid="stVerticalBlock"] > div:has(.schedule-shell) {
            gap: 0.4rem;
        }
        .schedule-shell {
            font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        }
        .schedule-legend {
            display: flex; flex-wrap: wrap; gap: 0.55rem 1rem;
            margin: 0.35rem 0 0.75rem 0; font-size: 0.82rem; color: #3a3a3a;
        }
        .schedule-legend span {
            display: inline-flex; align-items: center; gap: 0.4rem;
        }
        .schedule-legend i {
            width: 0.7rem; height: 0.7rem; border-radius: 2px; display: inline-block;
        }
        .schedule-week-card {
            background: linear-gradient(160deg, #f4f7f5 0%, #eef2f6 100%);
            border: 1px solid #d5dde6; border-radius: 10px; padding: 0.9rem 1rem;
            margin-bottom: 0.75rem;
        }
        .schedule-week-card .meta {
            color: #5a6570; font-size: 0.85rem; margin-top: 0.25rem;
        }
        .schedule-day-panel {
            background: #fafbfc; border: 1px solid #e2e8ef; border-radius: 10px;
            padding: 0.85rem 1rem 1rem;
        }
        .kind-pill {
            display: inline-block; color: white; padding: 2px 9px;
            border-radius: 999px; font-size: 0.72rem; letter-spacing: 0.02em;
            text-transform: uppercase;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="schedule-shell">', unsafe_allow_html=True)
    st.subheader("Schedule")
    st.caption(
        "Drag any block to another day — the plan and Apple Calendar update "
        "together. Click a day or session for the full prescription below."
    )

    if st_calendar is None:
        st.error(
            "Install `streamlit-calendar` to use the schedule view "
            "(`pip install streamlit-calendar`).")
        page_this_week_list(cfg, plan, today)
        st.markdown("</div>", unsafe_allow_html=True)
        return

    if "schedule_selected" not in st.session_state:
        st.session_state.schedule_selected = today.isoformat()
    if "schedule_rev" not in st.session_state:
        st.session_state.schedule_rev = 0

    legend = [
        ("Long Run", schedulemod.KIND_COLOUR["long"]),
        ("Easy / Medium", schedulemod.KIND_COLOUR["easy"]),
        ("Quality Run", schedulemod.KIND_COLOUR["tempo"]),
        (cross_training_name(cfg), schedulemod.KIND_COLOUR["climb"]),
        ("Strength", schedulemod.KIND_COLOUR["strength"]),
        ("Race", schedulemod.KIND_COLOUR["race"]),
    ]
    legend_col, view_col = st.columns([4, 1])
    with legend_col:
        st.markdown(
            '<div class="schedule-legend">'
            + "".join(
                f'<span><i style="background:{c}"></i>{label}</span>'
                for label, c in legend)
            + "</div>",
            unsafe_allow_html=True,
        )
    with view_col:
        view_label = st.radio(
            "View",
            ["Week", "Month"],
            horizontal=True,
            label_visibility="collapsed",
            key="schedule_view_mode",
        )

    events = schedulemod.plan_to_events(plan, cfg)
    # Stable order within a day: long first, then runs, then climb/strength.
    rank = {"long": 0, "quality": 1, "medium": 2, "easy": 3, "optional": 4,
            "race": 0, "climb": 5, "strength": 6, "rest": 9}
    events.sort(key=lambda e: (
        e["start"],
        rank.get((e.get("extendedProps") or {}).get("role", ""), 5),
        e["title"],
    ))

    # Remount on view change so the iframe height matches content (week short,
    # month taller) without an inner scrollbar. height:"auto" sizes to events.
    fc_view = "dayGridWeek" if view_label == "Week" else "dayGridMonth"
    options = {
        "initialView": fc_view,
        "initialDate": st.session_state.schedule_selected,
        "timeZone": "local",
        "headerToolbar": {
            "left": "today prev,next",
            "center": "title",
            "right": "",
        },
        "buttonText": {"today": "Today"},
        "height": "auto",
        "editable": True,
        "selectable": True,
        "weekNumbers": False,
        "firstDay": 1,
        "navLinks": False,
        "fixedWeekCount": False,
        "showNonCurrentDates": True,
        "dayMaxEvents": False,
        "displayEventTime": False,
        "eventDisplay": "block",
        "eventStartEditable": True,
        "eventDurationEditable": False,
        "forceEventDuration": True,
    }
    custom_css = """
    .fc {
        --fc-border-color: #d7dee6;
        --fc-page-bg-color: transparent;
        --fc-today-bg-color: rgba(47, 107, 82, 0.08);
        --fc-neutral-bg-color: #f3f5f7;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
    }
    .fc .fc-toolbar.fc-header-toolbar { margin-bottom: 0.45rem; }
    .fc .fc-toolbar-title {
        font-size: 1.05rem; font-weight: 650; letter-spacing: -0.01em;
        color: #1c2430;
    }
    .fc .fc-button {
        background: #1c2430; border: none; border-radius: 6px;
        text-transform: none; font-weight: 550; padding: 0.3rem 0.65rem;
        box-shadow: none; font-size: 0.85rem;
    }
    .fc .fc-button:hover { background: #2c3747; }
    .fc .fc-button-primary:not(:disabled).fc-button-active,
    .fc .fc-button-primary:not(:disabled):active {
        background: #2f6b52; border: none;
    }
    .fc .fc-col-header-cell {
        background: #eef2f6; padding: 0.25rem 0;
    }
    .fc .fc-col-header-cell-cushion {
        color: #3a4654; font-weight: 600; font-size: 0.78rem;
        text-decoration: none;
    }
    .fc .fc-daygrid-day {
        cursor: pointer;
    }
    .fc .fc-daygrid-day:hover {
        background: rgba(47, 107, 82, 0.05);
    }
    .fc .fc-daygrid-day-number {
        color: #4a5562; font-weight: 600; font-size: 0.75rem;
        padding: 0.2rem 0.3rem; text-decoration: none;
        cursor: pointer;
    }
    .fc .fc-dayGridWeek-view .fc-daygrid-day-frame {
        min-height: 3.4rem; padding: 0.05rem;
    }
    .fc .fc-dayGridMonth-view .fc-daygrid-day-frame {
        min-height: 4.6rem; padding: 0.08rem;
    }
    .fc .fc-daygrid-event {
        border: none; border-radius: 5px; margin: 1px 2px;
        padding: 2px 5px; box-shadow: 0 1px 0 rgba(0,0,0,0.06);
    }
    .fc .fc-event-title {
        font-weight: 650; font-size: 0.74rem; line-height: 1.2;
        white-space: normal; overflow: visible;
        padding: 0;
    }
    .fc .fc-daygrid-event-dot { display: none; }
    .fc .fc-scroller,
    .fc .fc-scroller-liquid-absolute {
        overflow: visible !important;
    }
    .fc .fc-day-today .fc-daygrid-day-number {
        background: #2f6b52; color: white; border-radius: 999px;
        width: 1.4rem; height: 1.4rem; display: inline-flex;
        align-items: center; justify-content: center; padding: 0;
    }
    """

    state = st_calendar(
        events=events,
        options=options,
        custom_css=custom_css,
        callbacks=["eventClick", "eventChange", "dateClick"],
        key=f"schedule_cal_{fc_view}_{st.session_state.schedule_rev}",
    )

    if state:
        _handle_calendar_callback(cfg, plan, state)

    selected = date.fromisoformat(st.session_state.schedule_selected)
    week = plan.week_for(selected) or plan.week_for(today) or plan.weeks[0]

    st.divider()
    left, right = st.columns([1.65, 1], gap="large")
    with left:
        st.markdown('<div class="schedule-day-panel">', unsafe_allow_html=True)
        _render_day_detail(cfg, plan, selected, today)
        st.markdown("</div>", unsafe_allow_html=True)
    with right:
        colour = KIND_COLOUR.get(week.kind, "#444")
        long_run = next((s for s in week.run_sessions if s.role == "long"), None)
        long_txt = (
            f"{long_run.miles:g} mi · {long_run.day.title()}"
            if long_run else "—")
        st.markdown(
            f"""
            <div class="schedule-week-card">
              <div>
                <strong>Week {week.index}</strong>
                <span class="kind-pill" style="background:{colour};margin-left:0.45rem">
                  {week.kind}
                </span>
              </div>
              <div class="meta">{week.phase} · phase week {week.phase_week}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        m1, m2 = st.columns(2)
        m1.metric("Planned", f"{week.planned_miles:g} mi")
        m2.metric("Long run", long_txt)
        if week.focus:
            st.info(week.focus)
        if week.adaptations:
            st.warning("\n".join(f"- {a}" for a in week.adaptations))

        moves = logbook.schedule_moves(cfg)
        if moves:
            st.caption(f"{len(moves)} manual move(s) saved across the plan.")
            if st.button("Clear all manual moves"):
                logbook.clear_schedule_moves(cfg)
                regenerate(cfg)
                st.session_state.schedule_rev += 1
                st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)


def _handle_calendar_callback(cfg: dict, plan, state: dict) -> None:
    callback = state.get("callback")
    if callback == "dateClick":
        raw = state.get("dateClick", {}).get("date", "")
        if raw:
            st.session_state.schedule_selected = _fc_day(raw)
    elif callback == "eventClick":
        event = state.get("eventClick", {}).get("event", {})
        start = event.get("start") or ""
        if start:
            st.session_state.schedule_selected = _fc_day(start)
    elif callback == "eventChange":
        change = state.get("eventChange", {})
        event = change.get("event") or {}
        old = change.get("oldEvent") or {}
        fingerprint = (
            (event.get("extendedProps") or {}).get("fingerprint")
            or event.get("id")
            or (old.get("extendedProps") or {}).get("fingerprint")
            or old.get("id")
        )
        new_start = event.get("start") or ""
        if not fingerprint or not new_start:
            return
        new_day = date.fromisoformat(_fc_day(new_start))
        token = f"{fingerprint}->{new_day.isoformat()}"
        if st.session_state.get("schedule_last_move") == token:
            return
        moved = schedulemod.move_session(plan, fingerprint, new_day, cfg)
        if moved:
            schedulemod.save_and_publish(plan, cfg)
            st.session_state.schedule_last_move = token
            st.session_state.schedule_selected = new_day.isoformat()
            st.session_state.schedule_rev += 1
            st.success(f"Moved to {new_day:%A} {new_day.day} {new_day:%b}. "
                       "Calendar feed updated.")
            st.rerun()
        else:
            st.warning("Could not move that session (race day is fixed, or "
                       "the target is outside the plan).")


def _render_day_detail(cfg: dict, plan, day: date, today: date) -> None:
    marker = " · today" if day == today else ""
    st.markdown(f"#### {day:%A} {day.day} {day:%b %Y}{marker}")

    sessions = plan.session_for(day)
    if not sessions:
        sessions = [
            s for w in plan.weeks for s in w.sessions
            if s.date == day.isoformat()
        ]

    logged = logbook.load_sessions(cfg, day, day)
    if not sessions:
        st.caption("Nothing planned — drag a session here, or enjoy the rest day.")
    for session in sessions:
        with st.container(border=True):
            render_session(session, cfg)

    if not logged.empty:
        for _, row in logged.iterrows():
            st.success(logbook.summarise(row), icon=":material/check:")
    elif sessions:
        st.caption("Not logged yet.")


def page_this_week_list(cfg: dict, plan, today: date) -> None:
    """Fallback list view when streamlit-calendar is unavailable."""
    week = plan.week_for(today) or plan.weeks[0]

    choices = [f"Week {w.index}: {w.start} ({w.planned_miles:g} mi, {w.kind})"
               for w in plan.weeks]
    default = plan.weeks.index(week)
    picked = st.selectbox("Week", choices, index=default)
    week = plan.weeks[choices.index(picked)]

    colour = KIND_COLOUR.get(week.kind, "#444")
    st.markdown(
        f"### Week {week.index} of {len(plan.weeks)} &nbsp;"
        f"<span style='background:{colour};color:white;padding:2px 8px;"
        f"border-radius:4px;font-size:0.6em'>{week.kind}</span>",
        unsafe_allow_html=True)

    cols = st.columns(4)
    runs = [s for s in week.run_sessions if not s.optional]
    long_run = next((s for s in week.run_sessions if s.role == "long"), None)
    cols[0].metric("Planned", f"{week.planned_miles:g} mi")
    cols[1].metric("Runs", len(runs))
    cols[2].metric("Long run", f"{long_run.miles:g} mi" if long_run else "-")
    cols[3].metric("Phase", f"{week.phase} {week.phase_week}")

    if week.focus:
        st.info(week.focus)
    if week.adaptations:
        st.warning("Re-planned:\n\n" + "\n\n".join(f"- {a}" for a in week.adaptations))

    st.divider()
    by_day: dict[str, list] = {}
    for session in week.sessions:
        by_day.setdefault(session.date, []).append(session)
    logged = logbook.load_sessions(cfg, week.start_date, week.end_date)
    for day_str, sessions in by_day.items():
        day = date.fromisoformat(day_str)
        done = logged[logged["date"] == day] if not logged.empty else pd.DataFrame()
        left, right = st.columns([3, 2])
        with left:
            marker = " (today)" if day == today else ""
            st.markdown(f"**{day:%A %d %b}**{marker}")
            for session in sessions:
                render_session(session, cfg)
        with right:
            if not done.empty:
                for _, row in done.iterrows():
                    st.success(logbook.summarise(row), icon=":material/check:")
            else:
                st.caption("not logged")
        st.divider()


def _log_entry_label(row) -> str:
    """Short picker label: date: type distance (hides sqlite ids)."""
    day = row["date"]
    day_str = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    kind = str(row["kind"])
    try:
        miles = float(row["miles"] or 0)
    except (TypeError, ValueError):
        miles = 0.0
    if miles:
        return f"{day_str}: {kind} {miles:g} mi"
    return f"{day_str}: {kind}"


def page_log(cfg: dict, plan, today: date) -> None:
    st.subheader("Log a session")
    st.caption(
        "Log whatever you actually did - including runs on rest days, skipped "
        "sessions, or extra miles. The plan re-adapts from the log; it does not "
        "require you to match the schedule day-for-day.")

    with st.form("log_form", clear_on_submit=True):
        cols = st.columns(3)
        when = cols[0].date_input("Date", value=today)
        kind = cols[1].selectbox(
            "Type", [logbook.KIND_RUN, logbook.KIND_RACE, logbook.KIND_CLIMB,
                     logbook.KIND_STRENGTH, logbook.KIND_CROSS])
        label = cols[2].text_input("Label", placeholder="race name, or notes tag")

        cols = st.columns(3)
        miles = cols[0].number_input("Miles", min_value=0.0, step=0.1, value=0.0)
        duration = cols[1].text_input("Time", placeholder="MM:SS or H:MM:SS")
        rpe = cols[2].slider("Effort (RPE)", 1, 10, 5,
                             help="1 is a stroll, 10 is all-out racing.")

        cols = st.columns([1, 2])
        pain = cols[0].slider(
            "Pain", 0, 5, 0,
            help=f"{cfg['rules']['pain_consecutive']} runs in a row at "
                 f"{cfg['rules']['pain_threshold']}+ forces a cutback week.")
        pain_location = cols[1].text_input("Where", placeholder="left shin, right knee")

        notes = st.text_area("Notes", placeholder="how it felt, weather, route")
        submitted = st.form_submit_button("Log it", type="primary")

    if submitted:
        if duration:
            try:
                parse_duration(duration)
            except ValueError:
                st.error(f"Could not read '{duration}' as a time.")
                return
        logbook.log_session(cfg, when=when, kind=kind, miles=miles,
                            duration=duration or None, rpe=rpe, pain=pain,
                            pain_location=pain_location, label=label, notes=notes)
        changed = regenerate(cfg)
        st.success(f"Logged. Plan re-checked, {changed} adaptation(s) active.")
        st.rerun()

    st.divider()
    st.subheader("Strava")
    st.caption(
        "Official API access now requires a Strava subscription for some "
        "accounts. CSV import works without one (distance auto-detects meters).")
    _render_strava_panel(cfg)

    st.divider()
    st.subheader("Import file")
    st.caption(
        "Upload Strava's `activities.csv` archive export, or a single GPX. "
        "Distance is auto-detected (meters vs km) — values like 8000+ in the "
        "CSV are meters, not miles.")
    bad = logbook.load_sessions(cfg)
    if not bad.empty and (bad["miles"] >= 100).any():
        n_bad = int((bad["miles"] >= 100).sum())
        st.warning(
            f"{n_bad} session(s) look like a bad CSV import (≥100 mi — almost "
            "certainly meters stored as miles).")
        cols_fix = st.columns(2)
        if cols_fix[0].button("Fix distances (÷ 1609)"):
            fixed = logbook.repair_meter_distances(cfg)
            regenerate(cfg)
            st.success(f"Repaired {fixed} session(s).")
            st.rerun()
        if cols_fix[1].button("Delete broken imports (≥100 mi)"):
            removed = logbook.delete_sessions(cfg, min_miles=100)
            regenerate(cfg)
            st.success(f"Removed {removed}. Re-import the CSV with unit Auto.")
            st.rerun()
    cols = st.columns(2)
    with cols[0]:
        uploaded = st.file_uploader("Strava activities.csv", type="csv")
        unit = st.radio(
            "Distance unit in the file",
            ["auto", "m", "km", "mi"],
            horizontal=True,
            help="Auto treats values ≥100 as meters (common in Strava CSV).")
        if uploaded and st.button("Import Strava export"):
            tmp = Path(cfg["db_path"]).parent / "_strava_upload.csv"
            tmp.write_bytes(uploaded.getvalue())
            imported, skipped = logbook.import_strava_csv(cfg, tmp, unit)
            tmp.unlink(missing_ok=True)
            regenerate(cfg)
            st.success(f"Imported {imported}, skipped {skipped}.")
            st.rerun()
    with cols[1]:
        gpx = st.file_uploader("GPX track", type="gpx")
        if gpx and st.button("Import GPX"):
            tmp = Path(cfg["db_path"]).parent / f"_{gpx.name}"
            tmp.write_bytes(gpx.getvalue())
            try:
                result = logbook.import_gpx(cfg, tmp)
                regenerate(cfg)
                st.success(f"Imported {result['miles']:g} mi on {result['date']}.")
            except Exception as exc:
                st.error(str(exc))
            finally:
                tmp.unlink(missing_ok=True)

    st.divider()
    st.subheader("Recent sessions")
    # Newest first; older entries stay below the fold in the scrollable table.
    recent = logbook.load_sessions(cfg)
    if recent.empty:
        st.caption("Nothing logged yet.")
        return

    ordered = recent.sort_values(
        ["date", "id"], ascending=[False, False]).reset_index(drop=True)
    display = ordered.copy()
    display["pace"] = display["pace"].map(
        lambda p: format_pace(p) if pd.notna(p) else "")
    display["time"] = display["duration_s"].map(
        lambda s: format_duration(s) if pd.notna(s) else "")
    st.dataframe(
        display[["date", "kind", "miles", "time", "pace", "rpe", "pain", "notes"]],
        hide_index=True, use_container_width=True, height=280)

    # Labels hide the sqlite id; disambiguate same-day twins with time.
    choices: dict[str, int] = {}
    for _, r in ordered.iterrows():
        label = _log_entry_label(r)
        if label in choices:
            dur = r["duration_s"]
            extra = format_duration(dur) if pd.notna(dur) else str(int(r["id"]))
            label = f"{label} ({extra})"
        choices[label] = int(r["id"])

    picked = st.selectbox("Edit or remove", ["-"] + list(choices))
    if picked == "-":
        return

    session_id = choices[picked]
    row = logbook.get_session(cfg, session_id)
    if row is None:
        st.warning("That entry is gone.")
        return

    kinds = [logbook.KIND_RUN, logbook.KIND_RACE, logbook.KIND_CLIMB,
             logbook.KIND_STRENGTH, logbook.KIND_CROSS]
    kind_index = kinds.index(row["kind"]) if row["kind"] in kinds else 0
    duration_default = (
        format_duration(row["duration_s"]) if row.get("duration_s") is not None
        else "")

    with st.form(f"edit_log_{session_id}"):
        st.caption(f"Editing {_log_entry_label(pd.Series(row))}")
        cols = st.columns(3)
        when = cols[0].date_input(
            "Date", value=date.fromisoformat(str(row["date"])[:10]))
        kind = cols[1].selectbox("Type", kinds, index=kind_index)
        label = cols[2].text_input("Label", value=row.get("label") or "")

        cols = st.columns(3)
        miles = cols[0].number_input(
            "Miles", min_value=0.0, step=0.1,
            value=float(row.get("miles") or 0))
        duration = cols[1].text_input(
            "Time", value=duration_default, placeholder="MM:SS or H:MM:SS")
        rpe = cols[2].slider(
            "Effort (RPE)", 1, 10,
            int(row["rpe"]) if row.get("rpe") is not None else 5)

        cols = st.columns([1, 2])
        pain = cols[0].slider(
            "Pain", 0, 5, int(row.get("pain") or 0))
        pain_location = cols[1].text_input(
            "Where", value=row.get("pain_location") or "")
        notes = st.text_area("Notes", value=row.get("notes") or "")

        save_col, del_col = st.columns(2)
        save = save_col.form_submit_button("Save changes", type="primary")
        delete = del_col.form_submit_button("Delete entry")

    if save:
        if duration:
            try:
                parse_duration(duration)
            except ValueError:
                st.error(f"Could not read '{duration}' as a time.")
                return
        logbook.update_session(
            cfg, session_id, when=when, kind=kind, miles=miles,
            duration=duration or None, rpe=rpe, pain=pain,
            pain_location=pain_location, label=label, notes=notes)
        regenerate(cfg)
        st.success("Updated. Plan re-checked.")
        st.rerun()

    if delete:
        logbook.delete_session(cfg, session_id)
        regenerate(cfg)
        st.success("Deleted.")
        st.rerun()


def page_progress(cfg: dict, plan, today: date) -> None:
    st.subheader("Progress")
    status = metrics.status(cfg, plan, today)

    cols = st.columns(4)
    cols[0].metric("Projected finish", format_duration(status.projected_finish),
                   delta=_finish_delta(status),
                   delta_color="inverse")
    cols[1].metric("VDOT", f"{status.fitness.vdot:.1f}")
    cols[2].metric("Last 7 days", f"{status.acute_miles:g} mi")
    cols[3].metric("Workload ratio",
                   status.acwr if status.acwr is not None else "n/a",
                   help="Last 7 days over the 4-week average. Above "
                        f"{cfg['rules']['acwr_flag']} is where injury risk climbs.")

    st.caption(f"Fitness from {status.fitness.source}. "
               f"At full marathon volume this VDOT is worth "
               f"{format_duration(status.fitness.marathon_potential)}; the "
               f"projection above discounts that for your current base.")

    for warning in status.warnings:
        st.warning(warning)

    st.divider()

    st.markdown("**Planned against actual volume**")
    df = metrics.plan_vs_actual(cfg, plan)
    melted = df.melt(id_vars=["week", "week_start", "kind"],
                     value_vars=["planned", "actual"],
                     var_name="series", value_name="miles")
    chart = (alt.Chart(melted)
             .mark_line(point=True)
             .encode(
                 x=alt.X("week:Q", title="week"),
                 y=alt.Y("miles:Q", title="miles"),
                 color=alt.Color("series:N", title=""),
                 tooltip=["week", "series", "miles", "kind"])
             .properties(height=300))
    st.altair_chart(chart, use_container_width=True)

    cols = st.columns(2)
    with cols[0]:
        st.markdown("**Projected finish if the plan is followed**")
        st.caption("Holds fitness constant and relaxes only the volume penalty, "
                   "so this is the gain from consistency alone.")
        series = metrics.projected_finish_series(cfg, plan)
        series["finish_hours"] = series["projected_seconds"] / 3600
        goal_hours = parse_duration(cfg["race"]["goal_time"]) / 3600
        must_hours = parse_duration(cfg["race"]["must_beat"]) / 3600
        base = (alt.Chart(series).mark_line()
                .encode(x=alt.X("week:Q", title="week"),
                        y=alt.Y("finish_hours:Q", title="projected hours",
                                scale=alt.Scale(zero=False)),
                        tooltip=["week", "planned_miles"]))
        rules = (alt.Chart(pd.DataFrame({
                    "y": [goal_hours, must_hours],
                    "label": ["goal", "sub-4"]}))
                 .mark_rule(strokeDash=[4, 4])
                 .encode(y="y:Q", color=alt.Color("label:N", title="")))
        st.altair_chart((base + rules).properties(height=280),
                        use_container_width=True)

    with cols[1]:
        st.markdown("**Easy-run pace over time**")
        trend = metrics.pace_trend(cfg)
        if trend.empty:
            st.caption("Log a few runs and this fills in.")
        else:
            trend = trend.copy()
            trend["pace_min"] = trend["pace"] / 60
            scatter = (alt.Chart(trend)
                       .mark_circle(size=70)
                       .encode(x=alt.X("date:T", title=""),
                               y=alt.Y("pace_min:Q", title="min/mile",
                                       scale=alt.Scale(zero=False, reverse=True)),
                               size=alt.Size("miles:Q", title="miles"),
                               tooltip=["date", "miles", "rpe"])
                       .properties(height=280))
            st.altair_chart(scatter, use_container_width=True)

    st.divider()
    st.markdown("**Full plan**")
    show = df.copy()
    show["long"] = [
        next((s.miles for s in w.run_sessions if s.role == "long"), 0.0)
        for w in plan.weeks]
    st.dataframe(show[["week", "week_start", "phase", "kind", "planned",
                       "long", "actual", "delta"]],
                 hide_index=True, use_container_width=True, height=400)


def _finish_delta(status) -> str:
    gap = status.projected_finish - status.goal_seconds
    sign = "+" if gap > 0 else "-"
    return f"{sign}{format_duration(abs(gap))} vs goal"


def page_trips(cfg: dict, plan, today: date) -> None:
    st.subheader("Trips")
    st.caption("Tell the plan about travel and the affected weeks become "
               "maintenance: one longer effort protected, quality dropped, "
               "then volume rebuilds under the ramp cap on your return.")

    trips = cfg.get("trips") or []
    if trips:
        st.dataframe(pd.DataFrame([{
            "name": t["name"], "start": t["start"], "end": t["end"],
            "runs possible": t.get("runs_possible", 1),
            "max minutes": t.get("max_run_minutes", 45),
        } for t in trips]), hide_index=True, use_container_width=True)

        remove = st.selectbox("Remove a trip", ["-"] + [t["name"] for t in trips])
        if remove != "-" and st.button("Remove"):
            cfg["trips"] = [t for t in trips if t["name"] != remove]
            save_config(cfg)
            regenerate(cfg)
            st.rerun()
    else:
        st.caption("No trips recorded.")

    st.divider()
    with st.form("trip_form", clear_on_submit=True):
        cols = st.columns(3)
        name = cols[0].text_input("Name", placeholder="Lisbon long weekend")
        start = cols[1].date_input("From", value=today + timedelta(days=30))
        end = cols[2].date_input("To", value=today + timedelta(days=34))

        cols = st.columns(2)
        runs = cols[0].number_input("Runs possible while away", 0, 7, 1)
        max_minutes = cols[1].number_input("Longest run (minutes)", 15, 240, 45)
        notes = st.text_input("Notes", placeholder="hotel by the river, flat")

        if st.form_submit_button("Add and re-plan", type="primary"):
            if not name:
                st.error("Give the trip a name.")
            elif end < start:
                st.error("The end date is before the start date.")
            else:
                cfg.setdefault("trips", [])
                cfg["trips"] = [t for t in cfg["trips"] if t.get("name") != name]
                cfg["trips"].append({
                    "name": name, "start": start, "end": end,
                    "runs_possible": int(runs),
                    "max_run_minutes": float(max_minutes),
                    "notes": notes,
                })
                save_config(cfg)
                changed = regenerate(cfg)
                st.success(f"Added. {changed} adaptation(s) applied.")
                st.rerun()


def page_gear(cfg: dict, plan, today: date) -> None:
    st.subheader("Gear")
    st.caption("Tied to the week that creates the need, not a flat list. "
               "Most of this should not be bought yet.")

    items = gearmod.recommend(cfg, plan, today)
    summary = gearmod.budget_summary(items, cfg["gear"].get("budget_eur"))
    cur = currency_code(cfg)

    cols = st.columns(4)
    cols[0].metric("Essential", f"{cur} {summary['essential']}")
    cols[1].metric("Recommended", f"{cur} {summary['recommended']}")
    cols[2].metric("Optional", f"{cur} {summary['optional']}")
    cols[3].metric("Total", f"{cur} {summary['total']}")

    only_due = st.checkbox("Only what is due in the next three weeks")
    if only_due:
        items = gearmod.due_now(cfg, plan, today)
        if not items:
            st.success("Nothing due in the next three weeks.")
            return

    for item in items:
        due = f"by {item.due:%d %b %Y}" if item.due else "any time"
        overdue = item.due and item.due < today
        marker = " - overdue" if overdue else ""
        with st.expander(
                f"{item.name}  |  {item.price_label}  |  {due}{marker}",
                expanded=bool(overdue)):
            st.markdown(f"**{item.priority.title()}** - {item.category}")
            st.write(item.why)
            st.caption(f"Trigger: {item.trigger}")
            if item.retailers:
                st.caption("Where: " + ", ".join(item.retailers))


def page_settings(cfg: dict, plan, today: date) -> None:
    st.subheader("Settings")
    race_name = cfg["race"].get("name") or "the race"
    local_tz = local_timezone_name()

    with st.form("settings"):
        st.markdown("**Location**")
        st.caption(
            "Daylight tags and calendar times follow this. Friends in another "
            "country should set their city, coordinates, and timezone (or use "
            "the laptop timezone).")
        cols = st.columns(2)
        loc_name = cols[0].text_input(
            "City / area", value=str(cfg["location"].get("name") or ""))
        use_laptop_tz = cols[1].checkbox(
            f"Use laptop timezone ({local_tz})",
            value=cfg["location"].get("timezone") == local_tz,
            help="Uses the timezone of the machine running Streamlit.")
        cols = st.columns(3)
        lat = cols[0].number_input(
            "Latitude", -90.0, 90.0,
            float(cfg["location"].get("lat") or 0.0),
            format="%.4f",
            help="Needed for sunset / dark-run tagging.")
        lon = cols[1].number_input(
            "Longitude", -180.0, 180.0,
            float(cfg["location"].get("lon") or 0.0),
            format="%.4f")
        tz_manual = cols[2].text_input(
            "Timezone (IANA)",
            value=str(cfg["location"].get("timezone") or local_tz),
            help="e.g. America/New_York, America/Chicago, Europe/Amsterdam. "
                 "Ignored if 'Use laptop timezone' is checked.",
            disabled=use_laptop_tz)

        lit_routes = st.text_area(
            "Lit routes for dark evenings (one per line)",
            value="\n".join(cfg["constraints"].get("lit_routes") or []),
            help="Named in advance so a wet Tuesday does not become a debate.")

        st.markdown("**Cross-training**")
        st.caption(
            "Protected non-running sessions (Climbing, Gym, Yoga, …). Strength "
            "is paired to the same days. Leave days blank to disable.")
        cols = st.columns(2)
        xt_name = cols[0].text_input(
            "Activity name",
            value=cross_training_name(cfg),
            help="Shown on the calendar and schedule.")
        xt_days = cols[1].multiselect(
            "Days",
            ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"],
            default=cross_training_days(cfg),
        )

        st.markdown("**Athlete**")
        cols = st.columns(4)
        age = cols[0].number_input("Age", 0, 100,
                                   int(cfg["athlete"].get("age") or 0))
        height_ft = cols[1].number_input(
            "Height (ft)", 0, 8,
            int(cfg["athlete"].get("height_ft") or 0))
        height_in = cols[2].number_input(
            "Height (in)", 0, 11,
            int(cfg["athlete"].get("height_in") or 0))
        weight = cols[3].number_input(
            "Weight (lbs)", 0, 400,
            int(cfg["athlete"].get("weight_lbs") or 0))
        injuries = st.text_area(
            "Injury history (one per line)",
            value="\n".join(cfg["athlete"].get("injury_history") or []),
            help="Any entry here tightens the weekly ramp cap from 10% to 7%.")

        st.markdown("**Starting point**")
        st.caption(
            "Edit these for where you are now. Volume and paces also refresh "
            "automatically from the log when you rebuild (race results win; "
            "otherwise recent aerobic runs update training paces)."
        )
        snapshot = metrics.log_start_snapshot(cfg, today)
        if snapshot:
            bits = [f"~{snapshot['current_weekly_miles']:g} mi/wk"]
            if "current_easy_pace" in snapshot:
                bits.append(
                    f"median {snapshot['current_easy_pace']}/mi "
                    f"({snapshot.get('run_count', 0)} runs)"
                )
            st.caption("From your recent log: " + ", ".join(bits) + ".")
        apply_log = st.checkbox(
            "On save, overwrite starting point from the log",
            value=False,
            disabled=not bool(snapshot),
            help="Uses recent peak volume and median aerobic pace from logged "
                 "runs instead of the typed fields below.")

        cols = st.columns(3)
        plan_start = cols[0].date_input(
            "Plan start (Monday)",
            value=cfg["start"]["plan_start"],
            help="First Monday of the plan. Shift so the final Sunday is race day.")
        weekly = cols[1].number_input(
            "Current weekly miles", 0.0, 100.0,
            float(cfg["start"]["current_weekly_miles"]), step=0.5)
        long_run = cols[2].number_input(
            "Typical long run (mi)", 0.0, 30.0,
            float(cfg["start"]["current_long_run_miles"]), step=0.5)

        cols = st.columns(3)
        pace = cols[0].text_input(
            "Typical pace",
            value=cfg["start"]["current_easy_pace"])
        effort = cols[1].selectbox(
            "That pace feels", ["easy", "moderate", "hard"],
            index=["easy", "moderate", "hard"].index(
                cfg["start"].get("current_effort", "moderate")),
            help="At low volume a 'normal' pace is usually moderate. Calling it "
                 "easy inflates every training pace the plan gives you.")
        runs_pw = cols[2].number_input(
            "Runs per week", 1, 7,
            int(cfg["start"].get("current_runs_per_week") or 3))
        adapt_paces = st.checkbox(
            "Adapt training paces from logged runs",
            value=bool(cfg["start"].get("adapt_paces_from_log", True)),
            help="When on, median recent aerobic pace refreshes VDOT on rebuild. "
                 "A logged race always wins. Turn off to lock to the fields above.")

        st.markdown("**Goal**")
        cols = st.columns(2)
        goal = cols[0].text_input("Goal time", value=cfg["race"]["goal_time"])
        must = cols[1].text_input("Must beat", value=cfg["race"]["must_beat"])
        registered = st.checkbox(
            f"Bib purchased for {race_name}",
            value=bool(cfg["race"].get("registered")),
            help="Stops the registration deadline nag once you have entered.")

        st.markdown("**Gear**")
        cols = st.columns(2)
        currency = cols[0].selectbox(
            "Currency",
            ["EUR", "USD", "GBP", "CAD", "AUD"],
            index=["EUR", "USD", "GBP", "CAD", "AUD"].index(
                currency_code(cfg) if currency_code(cfg) in
                {"EUR", "USD", "GBP", "CAD", "AUD"} else "EUR"),
        )
        budget = cols[1].number_input(
            f"Budget ({currency}, 0 for none)", 0, 5000,
            int(cfg["gear"].get("budget_eur") or 0))

        if st.form_submit_button("Save and re-plan", type="primary"):
            cfg["location"]["name"] = loc_name.strip() or cfg["location"].get("name")
            cfg["location"]["lat"] = float(lat)
            cfg["location"]["lon"] = float(lon)
            cfg["location"]["timezone"] = (
                local_tz if use_laptop_tz else (tz_manual.strip() or local_tz))
            cfg["constraints"]["lit_routes"] = [
                line.strip() for line in lit_routes.splitlines() if line.strip()]
            cfg["constraints"]["cross_training"] = {
                "name": xt_name.strip() or "Gym",
                "days": list(xt_days),
            }
            cfg["constraints"]["climbing_days"] = list(xt_days)
            cfg["athlete"]["age"] = age or None
            cfg["athlete"]["height_ft"] = height_ft or None
            cfg["athlete"]["height_in"] = height_in if height_ft or height_in else None
            cfg["athlete"]["weight_lbs"] = weight or None
            cfg["athlete"].pop("height_cm", None)
            cfg["athlete"].pop("weight_kg", None)
            cfg["athlete"]["injury_history"] = [
                line.strip() for line in injuries.splitlines() if line.strip()]
            monday = plan_start - timedelta(days=plan_start.weekday())
            cfg["start"]["plan_start"] = monday
            cfg["start"]["current_weekly_miles"] = weekly
            cfg["start"]["current_long_run_miles"] = long_run
            cfg["start"]["current_easy_pace"] = pace
            cfg["start"]["current_effort"] = effort
            cfg["start"]["current_runs_per_week"] = int(runs_pw)
            if apply_log and snapshot:
                for key in ("current_weekly_miles", "current_easy_pace",
                            "current_long_run_miles", "current_effort",
                            "current_runs_per_week"):
                    if key in snapshot:
                        cfg["start"][key] = snapshot[key]
            cfg["start"]["adapt_paces_from_log"] = adapt_paces
            cfg["race"]["goal_time"] = goal
            cfg["race"]["must_beat"] = must
            cfg["race"]["registered"] = registered
            cfg["gear"]["currency"] = currency
            cfg["gear"]["budget_eur"] = budget or None
            save_config(cfg)
            regenerate(cfg)
            st.success("Saved and re-planned.")
            st.rerun()

    st.divider()
    url = cfg["race"].get("registration_url")
    if url and not cfg["race"].get("registered"):
        st.markdown(f"[{race_name} registration]({url})")
        st.divider()
    st.markdown("**Training paces**")
    fitness = metrics.current_fitness(cfg, today)
    st.caption(f"From {fitness.source}.")
    st.dataframe(pd.DataFrame([
        {"zone": ZONE_LABEL[z], "per mile": format_pace(p),
         "per km": format_pace_km(p)}
        for z, p in fitness.paces.items()
    ]), hide_index=True, use_container_width=True)

    hr = __import__("src.athlete", fromlist=["hr_zones"]).hr_zones(
        cfg["athlete"].get("age"))
    if hr:
        st.markdown("**Heart-rate zones**")
        st.dataframe(pd.DataFrame([
            {"zone": ZONE_LABEL.get(z, z), "bpm": f"{lo}-{hi}"}
            for z, (lo, hi) in hr.items()
        ]), hide_index=True, use_container_width=True)
    else:
        st.caption("Add your age above to get heart-rate zones.")

    st.divider()
    st.markdown("**Calendar**")
    ics = Path(cfg["ics_path"])
    if ics.exists():
        st.caption(f"Feed at `{ics}` - subscribe in Calendar.app via "
                   f"File > New Calendar Subscription. It is rewritten every "
                   f"time the plan changes, and events keep stable IDs so weeks "
                   f"update instead of duplicating.")
        st.download_button("Download marathon.ics", ics.read_bytes(),
                           file_name="marathon.ics", mime="text/calendar")
    else:
        st.caption(
            f"ICS will be written to `{ics}` on the next plan rebuild. "
            "Change `ics_path` in config.yaml if you do not use Dropbox.")

    st.divider()
    if st.button("Rebuild the plan from scratch"):
        changed = regenerate(cfg)
        st.success(f"Rebuilt. {changed} adaptation(s) applied.")
        st.rerun()


# --- Main ------------------------------------------------------------------

def _handle_strava_oauth(cfg: dict) -> None:
    """Finish the OAuth redirect when Strava sends us back with ?code=."""
    params = st.query_params
    code = params.get("code")
    if not code:
        return
    if params.get("error"):
        st.error(f"Strava authorization failed: {params.get('error')}")
        st.query_params.clear()
        return
    try:
        strava.exchange_code(cfg, code)
        st.query_params.clear()
        st.session_state.pop("strava_synced_at", None)
        st.success("Strava connected. Syncing activities…")
        result = strava.sync_activities(cfg)
        if result.imported:
            regenerate(cfg)
        st.success(f"Strava linked. {result}.")
        st.rerun()
    except Exception as exc:
        st.query_params.clear()
        st.error(f"Could not finish Strava link: {exc}")


def _render_strava_panel(cfg: dict) -> None:
    scfg = strava.strava_cfg(cfg)
    with st.expander("API app credentials", expanded=not strava.is_configured(cfg)):
        st.markdown(strava.setup_help(scfg.get("redirect_uri") or strava.DEFAULT_REDIRECT))
        cols = st.columns(2)
        client_id = cols[0].text_input(
            "Client ID",
            value=str(scfg.get("client_id") or ""),
            help="From https://www.strava.com/settings/api")
        client_secret = cols[1].text_input(
            "Client Secret",
            value=str(scfg.get("client_secret") or ""),
            type="password")
        redirect = st.text_input(
            "Redirect URI",
            value=scfg.get("redirect_uri") or strava.DEFAULT_REDIRECT,
            help="Must match the app callback domain. Default works with "
                 "`streamlit run app.py` on port 8501.")
        auto_sync = st.checkbox(
            "Auto-sync when opening Log",
            value=bool(scfg.get("auto_sync_on_load", True)))
        if st.button("Save Strava credentials"):
            cfg.setdefault("strava", {})
            cfg["strava"]["client_id"] = int(client_id) if str(client_id).isdigit() \
                else (client_id.strip() or None)
            cfg["strava"]["client_secret"] = client_secret.strip() or None
            cfg["strava"]["redirect_uri"] = redirect.strip() or strava.DEFAULT_REDIRECT
            cfg["strava"]["auto_sync_on_load"] = auto_sync
            save_config(cfg)
            st.success("Saved.")
            st.rerun()

    if not strava.is_configured(cfg):
        st.caption("Add Client ID and Secret above to enable Connect.")
        return

    if strava.is_connected(cfg):
        label = strava.athlete_label(cfg) or "connected"
        last = strava.format_last_sync(cfg)
        st.success(
            f"Linked as **{label}**"
            + (f" · last sync {last}" if last else " · not synced yet"))
        cols = st.columns(3)
        if cols[0].button("Sync from Strava", type="primary"):
            try:
                with st.spinner("Pulling activities from Strava…"):
                    result = strava.sync_activities(cfg)
                st.session_state["strava_synced_at"] = datetime.now().isoformat()
                if result.imported:
                    regenerate(cfg)
                st.success(str(result))
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        if cols[1].button("Disconnect"):
            strava.disconnect(cfg)
            st.session_state.pop("strava_synced_at", None)
            st.rerun()
        if scfg.get("auto_sync_on_load", True) and not st.session_state.get(
                "strava_synced_at"):
            try:
                result = strava.sync_activities(cfg)
                st.session_state["strava_synced_at"] = datetime.now().isoformat()
                if result.imported:
                    regenerate(cfg)
                    st.info(f"Auto-synced from Strava: {result}")
                    st.rerun()
            except Exception as exc:
                st.warning(f"Auto-sync skipped: {exc}")
    else:
        st.link_button("Connect Strava", strava.authorize_url(cfg),
                       type="primary")
        st.caption(
            "Approve **View data about your private activities** so private "
            "runs are imported. You will be redirected back to this app.")


def main() -> None:
    cfg = get_config()
    today = date.today()
    _handle_strava_oauth(cfg)
    plan = get_plan(cfg)

    header(cfg, plan, today)

    status = metrics.status(cfg, plan, today)
    cols = st.columns(4)
    cols[0].metric("Projected", format_duration(status.projected_finish))
    cols[1].metric("Goal", cfg["race"]["goal_time"])
    cols[2].metric("Last 7 days", f"{status.acute_miles:g} mi")
    week = plan.week_for(today)
    if week:
        cols[3].metric("This week", f"{week.planned_miles:g} mi")
    elif today < plan.weeks[0].start_date:
        cols[3].metric("Starts", f"{plan.weeks[0].start_date:%d %b}")
    else:
        cols[3].metric("This week", "past the plan")

    if not status.on_track_for_sub4:
        st.info(
            f"Current evidence projects {format_duration(status.projected_finish)}, "
            f"outside {cfg['race']['must_beat']}. That is normal this far out - "
            f"the projection is dominated by weekly volume, which is exactly what "
            f"the next few months build.")

    pages = {
        "Schedule": page_schedule,
        "Log": page_log,
        "Progress": page_progress,
        "Trips": page_trips,
        "Gear": page_gear,
        "Settings": page_settings,
    }
    tabs = st.tabs(list(pages))
    for tab, page in zip(tabs, pages.values()):
        with tab:
            page(cfg, plan, today)


main()
