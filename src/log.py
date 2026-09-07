"""Session logging: sqlite storage, manual entry, and Strava CSV / GPX import.

Runs are recorded from an Apple Watch and pushed to Strava by hand, so manual
entry is the primary path and imports are a convenience, not a dependency.
"""

from __future__ import annotations

import csv
import math
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .athlete import format_duration, format_pace, parse_duration

KM_PER_MILE = 1.609344
METERS_PER_MILE = 1609.344

KIND_RUN = "run"
KIND_RACE = "race"
KIND_CLIMB = "climb"
KIND_STRENGTH = "strength"
KIND_CROSS = "cross"

RUN_KINDS = (KIND_RUN, KIND_RACE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    kind          TEXT NOT NULL,     -- run | race | climb | strength | cross
    miles         REAL DEFAULT 0,
    duration_s    REAL,
    rpe           INTEGER,           -- 1-10 perceived effort
    pain          INTEGER DEFAULT 0, -- 0-5; 3+ repeated forces a cutback
    pain_location TEXT,
    label         TEXT,              -- race name, or what the session was
    notes         TEXT,
    source        TEXT DEFAULT 'manual',
    external_id   TEXT UNIQUE,       -- dedupes repeated imports
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions (date);

CREATE TABLE IF NOT EXISTS plan_overrides (
    week_index     INTEGER PRIMARY KEY,
    long_run_day   TEXT
);

CREATE TABLE IF NOT EXISTS schedule_moves (
    fingerprint    TEXT PRIMARY KEY,
    to_date        TEXT NOT NULL
);
"""


def connect(cfg: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg["db_path"])
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log_session(cfg: dict, when: date, kind: str = KIND_RUN, miles: float = 0.0,
                duration: str | float | None = None, rpe: int | None = None,
                pain: int = 0, pain_location: str = "", label: str = "",
                notes: str = "", source: str = "manual",
                external_id: str | None = None) -> int:
    duration_s = parse_duration(duration) if duration not in (None, "") else None
    with connect(cfg) as conn:
        cur = conn.execute(
            """INSERT OR REPLACE INTO sessions
               (date, kind, miles, duration_s, rpe, pain, pain_location,
                label, notes, source, external_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (when.isoformat(), kind, float(miles or 0), duration_s, rpe,
             int(pain or 0), pain_location, label, notes, source, external_id,
             datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def delete_session(cfg: dict, session_id: int) -> None:
    with connect(cfg) as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def delete_sessions(cfg: dict, *, source: str | None = None,
                    min_miles: float | None = None) -> int:
    """Bulk-delete sessions. Returns how many rows were removed."""
    clauses: list[str] = []
    params: list = []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if min_miles is not None:
        clauses.append("miles >= ?")
        params.append(float(min_miles))
    if not clauses:
        raise ValueError("Refusing to delete without a filter")
    where = " AND ".join(clauses)
    with connect(cfg) as conn:
        cur = conn.execute(f"DELETE FROM sessions WHERE {where}", params)
        return cur.rowcount


def repair_meter_distances(cfg: dict, *, min_miles: float = 100.0) -> int:
    """Fix sessions whose distance was imported as meters-labeled-as-miles.

    Returns how many rows were rewritten.
    """
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT id, miles FROM sessions WHERE miles >= ?",
            (float(min_miles),)).fetchall()
        for row in rows:
            fixed = round(float(row["miles"]) / METERS_PER_MILE, 2)
            conn.execute("UPDATE sessions SET miles = ? WHERE id = ?",
                         (fixed, row["id"]))
        return len(rows)


def get_session(cfg: dict, session_id: int) -> dict | None:
    with connect(cfg) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    return dict(row)


def has_external_id(cfg: dict, external_id: str | None) -> bool:
    if not external_id:
        return False
    with connect(cfg) as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE external_id = ? LIMIT 1",
            (external_id,)).fetchone()
    return row is not None


def update_session(cfg: dict, session_id: int, when: date, kind: str = KIND_RUN,
                   miles: float = 0.0, duration: str | float | None = None,
                   rpe: int | None = None, pain: int = 0,
                   pain_location: str = "", label: str = "",
                   notes: str = "") -> None:
    duration_s = parse_duration(duration) if duration not in (None, "") else None
    with connect(cfg) as conn:
        cur = conn.execute(
            """UPDATE sessions SET
                   date = ?, kind = ?, miles = ?, duration_s = ?, rpe = ?,
                   pain = ?, pain_location = ?, label = ?, notes = ?
               WHERE id = ?""",
            (when.isoformat(), kind, float(miles or 0), duration_s, rpe,
             int(pain or 0), pain_location, label, notes, session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"No session with id {session_id}")


def load_sessions(cfg: dict, start: date | None = None,
                  end: date | None = None) -> pd.DataFrame:
    query = "SELECT * FROM sessions"
    params: list = []
    clauses = []
    if start:
        clauses.append("date >= ?")
        params.append(start.isoformat())
    if end:
        clauses.append("date <= ?")
        params.append(end.isoformat())
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY date, id"

    with connect(cfg) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return _empty_frame()

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["miles"] = df["miles"].fillna(0.0)
    df["pain"] = df["pain"].fillna(0).astype(int)
    df["pace"] = df.apply(
        lambda r: (r["duration_s"] / r["miles"]) if r["miles"] and r["duration_s"]
        else None, axis=1)
    return df


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "id", "date", "kind", "miles", "duration_s", "rpe", "pain",
        "pain_location", "label", "notes", "source", "external_id",
        "created_at", "pace",
    ])


def last_run_date(cfg: dict, on_or_before: date | None = None) -> date | None:
    df = load_sessions(cfg, end=on_or_before)
    runs = df[df["kind"].isin(RUN_KINDS) & (df["miles"] > 0)]
    if runs.empty:
        return None
    return max(runs["date"])


def days_since_last_run(cfg: dict, as_of: date) -> int | None:
    """Days since the last run, ignoring anything dated in the future.

    Sessions can legitimately be logged ahead of time, and counting those would
    report a negative gap and suppress the re-entry rules.
    """
    last = last_run_date(cfg, on_or_before=as_of)
    if last is None:
        return None
    return max(0, (as_of - last).days)


def best_race(cfg: dict, since: date | None = None) -> dict | None:
    """The most recent logged race, used to recalibrate training paces."""
    df = load_sessions(cfg, start=since)
    races = df[(df["kind"] == KIND_RACE) & (df["miles"] > 0)
               & df["duration_s"].notna()]
    if races.empty:
        return None
    row = races.sort_values("date").iloc[-1]
    return {
        "date": row["date"],
        "miles": float(row["miles"]),
        "duration_s": float(row["duration_s"]),
        "label": row["label"] or "race",
    }


def set_long_run_override(cfg: dict, week_index: int, day: str | None) -> None:
    with connect(cfg) as conn:
        if day:
            conn.execute(
                "INSERT OR REPLACE INTO plan_overrides (week_index, long_run_day)"
                " VALUES (?,?)", (week_index, day.lower()))
        else:
            conn.execute("DELETE FROM plan_overrides WHERE week_index = ?",
                         (week_index,))


def long_run_overrides(cfg: dict) -> dict[int, str]:
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT week_index, long_run_day FROM plan_overrides").fetchall()
    return {r["week_index"]: r["long_run_day"] for r in rows}


def set_schedule_move(cfg: dict, fingerprint: str, to_date: date | None) -> None:
    with connect(cfg) as conn:
        if to_date is None:
            conn.execute(
                "DELETE FROM schedule_moves WHERE fingerprint = ?",
                (fingerprint,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO schedule_moves (fingerprint, to_date)"
                " VALUES (?,?)", (fingerprint, to_date.isoformat()))


def schedule_moves(cfg: dict) -> dict[str, str]:
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT fingerprint, to_date FROM schedule_moves").fetchall()
    return {r["fingerprint"]: r["to_date"] for r in rows}


def clear_schedule_moves(cfg: dict) -> None:
    with connect(cfg) as conn:
        conn.execute("DELETE FROM schedule_moves")


# --- Imports ---------------------------------------------------------------

# Anything above this as a "km" or "mi" reading is almost certainly meters
# (Strava's activities.csv often stores Distance in meters).
_DISTANCE_METERS_FLOOR = 100.0

# Strava's export uses different headers across locales and export versions.
_DATE_COLS = ["Activity Date", "activity date", "date"]
_TYPE_COLS = ["Activity Type", "activity type", "type"]
_NAME_COLS = ["Activity Name", "activity name", "name"]
_DIST_COLS = ["Distance", "distance", "Distance (km)", "Distance (m)"]
_TIME_COLS = ["Moving Time", "Elapsed Time", "moving time", "elapsed time"]
_ID_COLS = ["Activity ID", "activity id", "id"]


def import_strava_csv(cfg: dict, path: str | Path,
                      distance_unit: str = "auto") -> tuple[int, int]:
    """Import Strava's activities.csv. Returns (imported, skipped).

    ``distance_unit`` is ``auto`` (detect meters vs km vs mi), ``m``, ``km``,
    or ``mi``. Prefer auto — many exports store Distance in meters even when
    the Strava website shows kilometres.
    """
    imported = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            parsed = _parse_strava_row(row, distance_unit)
            if not parsed:
                skipped += 1
                continue
            try:
                log_session(cfg, source="strava", **parsed)
                imported += 1
            except sqlite3.IntegrityError:
                skipped += 1
    return imported, skipped


def distance_to_miles(value: float, unit: str = "auto") -> float:
    """Convert a Strava distance field to miles.

    Auto mode: values ≥ 100 are treated as meters (typical CSV export);
    smaller values are treated as kilometres (website-style decimals).
    """
    unit = (unit or "auto").lower().strip()
    if unit in {"m", "meter", "meters", "metre", "metres"}:
        return value / METERS_PER_MILE
    if unit in {"mi", "mile", "miles"}:
        if value >= _DISTANCE_METERS_FLOOR:
            # Guardrail: user picked miles but the file is clearly meters.
            return value / METERS_PER_MILE
        return value
    if unit in {"km", "kilometer", "kilometers", "kilometre", "kilometres"}:
        if value >= _DISTANCE_METERS_FLOOR:
            return value / METERS_PER_MILE
        return value / KM_PER_MILE
    # auto
    if value >= _DISTANCE_METERS_FLOOR:
        return value / METERS_PER_MILE
    # Sub-100 decimals from the website-style export are kilometres.
    return value / KM_PER_MILE


def _pick(row: dict, candidates: list[str]) -> str | None:
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _parse_strava_row(row: dict, distance_unit: str) -> dict | None:
    raw_date = _pick(row, _DATE_COLS)
    if not raw_date:
        return None
    when = _parse_flexible_date(raw_date)
    if not when:
        return None

    activity = (_pick(row, _TYPE_COLS) or "").strip().lower()
    kind = {
        "run": KIND_RUN, "trail run": KIND_RUN, "treadmill": KIND_RUN,
        "rock climbing": KIND_CLIMB, "climbing": KIND_CLIMB,
        "weight training": KIND_STRENGTH, "workout": KIND_STRENGTH,
    }.get(activity)
    if kind is None:
        kind = KIND_RUN if "run" in activity else KIND_CROSS

    miles = 0.0
    raw_dist = _pick(row, _DIST_COLS)
    if raw_dist:
        try:
            value = float(str(raw_dist).replace(",", ""))
            miles = distance_to_miles(value, distance_unit)
        except ValueError:
            miles = 0.0

    duration = None
    raw_time = _pick(row, _TIME_COLS)
    if raw_time:
        try:
            duration = float(raw_time)      # Strava exports seconds
        except ValueError:
            try:
                duration = parse_duration(raw_time)
            except ValueError:
                duration = None

    activity_id = _pick(row, _ID_COLS)
    return {
        "when": when,
        "kind": kind,
        "miles": round(miles, 2),
        "duration": duration,
        "label": (_pick(row, _NAME_COLS) or "").strip(),
        "external_id": f"strava:{activity_id}" if activity_id else None,
    }


def _parse_flexible_date(value: str) -> date | None:
    value = value.strip()
    formats = [
        "%b %d, %Y, %I:%M:%S %p", "%d %b %Y, %H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def import_gpx(cfg: dict, path: str | Path) -> dict:
    """Import a single GPX track, deriving distance from trackpoint geometry."""
    tree = ET.parse(path)
    root = tree.getroot()
    ns = {"g": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    point_path = ".//g:trkpt" if ns else ".//trkpt"

    points: list[tuple[float, float, datetime | None]] = []
    for pt in root.findall(point_path, ns):
        lat, lon = float(pt.get("lat")), float(pt.get("lon"))
        time_el = pt.find("g:time", ns) if ns else pt.find("time")
        stamp = None
        if time_el is not None and time_el.text:
            try:
                stamp = datetime.fromisoformat(time_el.text.replace("Z", "+00:00"))
            except ValueError:
                stamp = None
        points.append((lat, lon, stamp))

    if len(points) < 2:
        raise ValueError("GPX file has no usable track")

    metres = sum(
        _haversine(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        for i in range(1, len(points))
    )
    stamps = [p[2] for p in points if p[2]]
    duration = (stamps[-1] - stamps[0]).total_seconds() if len(stamps) >= 2 else None
    when = stamps[0].date() if stamps else date.today()

    miles = metres / 1609.344
    log_session(cfg, when=when, kind=KIND_RUN, miles=round(miles, 2),
                duration=duration, source="gpx",
                external_id=f"gpx:{Path(path).stem}")
    return {"date": when, "miles": round(miles, 2), "duration_s": duration}


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def summarise(row: pd.Series) -> str:
    """One-line description of a logged session (no database id)."""
    day = row["date"]
    day_str = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    bits = [f"{day_str}:", str(row["kind"])]
    miles = row.get("miles")
    if miles and float(miles):
        bits.append(f"{float(miles):g} mi")
    if row.get("duration_s") and pd.notna(row["duration_s"]):
        bits.append(format_duration(row["duration_s"]))
    pace = row.get("pace")
    if pace is not None and pd.notna(pace):
        try:
            bits.append(format_pace(float(pace)))
        except (TypeError, ValueError):
            bits.append(str(pace))
    if row.get("label"):
        bits.append(f"({row['label']})")
    return " ".join(bits)


def entry_label(row: pd.Series) -> str:
    """Short picker label: date: type distance."""
    day = row["date"]
    day_str = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    kind = str(row["kind"])
    miles = float(row["miles"] or 0)
    if miles:
        return f"{day_str}: {kind} {miles:g} mi"
    return f"{day_str}: {kind}"
