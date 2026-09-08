from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PATH_KEYS = ("db_path", "plan_path", "ics_path", "plans_dir", "overrides_path")

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Values that must be clock strings. YAML reads an unquoted "17:30" as the
# integer 1050 (sexagesimal), so these are normalised on the way in rather than
# trusted to be quoted correctly.
_TIME_FIELDS = {
    "constraints": ("work_start", "work_end", "weekday_earliest_start",
                    "weekend_start"),
    "race": ("start_time",),
}


def load_config(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    for key in _PATH_KEYS:
        cfg[key] = str(_resolve(cfg[key]))

    # Dashboard and CLI edits live in a separate file so this config keeps its
    # comments and is never machine-rewritten.
    overrides = _load_overrides(cfg["overrides_path"])
    if overrides:
        cfg = _deep_merge(cfg, overrides)
        for key in _PATH_KEYS:
            cfg[key] = str(_resolve(cfg[key]))

    Path(cfg["db_path"]).parent.mkdir(parents=True, exist_ok=True)

    for section, fields in _TIME_FIELDS.items():
        for field in fields:
            if field in cfg.get(section, {}):
                cfg[section][field] = _as_time_string(cfg[section][field])

    cfg["race"]["date"] = _as_date(cfg["race"]["date"])
    if cfg["race"].get("registration_deadline"):
        cfg["race"]["registration_deadline"] = _as_date(
            cfg["race"]["registration_deadline"])
    cfg["start"]["plan_start"] = _as_date(cfg["start"]["plan_start"])
    for trip in cfg.get("trips") or []:
        trip["start"] = _as_date(trip["start"])
        trip["end"] = _as_date(trip["end"])

    # A history of injuries is the single strongest predictor of the next one,
    # so it tightens the ramp rather than just being displayed.
    if cfg["athlete"].get("injury_history"):
        cfg["rules"]["injury_caution"] = True

    # "local" (or a missing timezone) means the laptop's IANA zone - so a friend
    # cloning the repo on a US machine gets America/... without editing YAML.
    loc = cfg.setdefault("location", {})
    tz = str(loc.get("timezone") or "local").strip()
    if tz.lower() in {"", "local", "system"}:
        loc["timezone"] = local_timezone_name()
    else:
        loc["timezone"] = tz

    gear = cfg.setdefault("gear", {})
    gear.setdefault("currency", "EUR")

    strava = cfg.setdefault("strava", {})
    strava.setdefault("redirect_uri", "http://localhost:8501/")
    strava.setdefault("auto_sync_on_load", True)
    strava.setdefault("lookback_days", 90)

    _normalize_cross_training(cfg)

    return cfg


def cross_training(cfg: dict) -> dict:
    """Display name + protected days for the non-running gym session.

    Internal session kind stays ``climb`` for schedule fingerprints / ICS UIDs;
    only the label the athlete sees is configurable (Climbing, Gym, Yoga, …).
    """
    return dict((cfg.get("constraints") or {}).get("cross_training") or {})


def cross_training_name(cfg: dict) -> str:
    return str(cross_training(cfg).get("name") or "Gym")


def cross_training_days(cfg: dict) -> list[str]:
    days = cross_training(cfg).get("days") or []
    return [str(d).lower() for d in days]


def _normalize_cross_training(cfg: dict) -> None:
    constraints = cfg.setdefault("constraints", {})
    xt = dict(constraints.get("cross_training") or {})
    if not xt.get("days"):
        xt["days"] = list(constraints.get("climbing_days") or [])
    if not xt.get("name"):
        xt["name"] = constraints.get("cross_training_name") or (
            "Climbing" if constraints.get("climbing_days") else "Gym")
    xt["days"] = [str(d).lower() for d in xt.get("days") or []]
    # Keep the legacy key in sync so older templates/comments still make sense.
    constraints["climbing_days"] = list(xt["days"])
    constraints["cross_training"] = xt


def local_timezone_name() -> str:
    """Best-effort IANA name for the machine running the app."""
    tz = datetime.now().astimezone().tzinfo
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    # Windows / exotic tzinfo objects sometimes only expose a display name.
    name = str(tz) if tz is not None else "UTC"
    return name if "/" in name else "UTC"


def currency_code(cfg: dict) -> str:
    return str((cfg.get("gear") or {}).get("currency") or "EUR").upper()


def save_config(cfg: dict, path: str | Path | None = None) -> Path:
    """Persist whatever differs from config.yaml into the overrides file.

    Only the delta is written, so config.yaml stays authoritative for anything
    the user has not deliberately changed - and keeps its comments.
    """
    base_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    with open(base_path) as f:
        base = yaml.safe_load(f)

    for key in _PATH_KEYS:
        base[key] = str(_resolve(base[key]))
    base["race"]["date"] = _as_date(base["race"]["date"])
    base["start"]["plan_start"] = _as_date(base["start"]["plan_start"])
    for trip in base.get("trips") or []:
        trip["start"] = _as_date(trip["start"])
        trip["end"] = _as_date(trip["end"])

    delta = _diff(_plain(base), _plain(cfg)) or {}
    # Path keys are resolved to absolute at load time and are infrastructure
    # rather than user settings, so they never belong in the overrides file.
    for key in _PATH_KEYS:
        delta.pop(key, None)

    target = Path(cfg["overrides_path"])
    target.parent.mkdir(parents=True, exist_ok=True)

    if not delta:
        target.unlink(missing_ok=True)
        return target

    header = ("# Written by the dashboard and CLI. Delete this file to fall back\n"
              "# to config.yaml. Only values that differ from config.yaml appear here.\n")
    with open(target, "w") as f:
        f.write(header)
        yaml.safe_dump(delta, f, sort_keys=False, allow_unicode=True,
                       default_flow_style=False)
    return target


def effective_increase_cap(cfg: dict) -> float:
    rules = cfg["rules"]
    if rules.get("injury_caution"):
        return float(rules["injury_caution_increase_pct"])
    return float(rules["max_weekly_increase_pct"])


# --- Internals -------------------------------------------------------------

def _resolve(value) -> Path:
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def _load_overrides(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _diff(base, current):
    """Nested difference: what in `current` departs from `base`."""
    if not isinstance(current, dict) or not isinstance(base, dict):
        return current if current != base else None

    out = {}
    for key, value in current.items():
        if key not in base:
            out[key] = value
            continue
        nested = _diff(base[key], value)
        if nested is not None:
            out[key] = nested
    return out or None


def _as_time_string(value) -> str:
    """Normalise a clock value, including YAML's sexagesimal integers."""
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, int):
        # YAML 1.1 read "17:30" as 17*60+30.
        return f"{value // 60:02d}:{value % 60:02d}"
    text = str(value).strip()
    hh, mm = (text.split(":") + ["0"])[:2]
    return f"{int(hh):02d}:{int(mm):02d}"


def _plain(obj):
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.date().isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return obj.strftime("%H:%M")
    return obj


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
