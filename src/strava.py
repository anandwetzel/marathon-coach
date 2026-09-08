"""Strava OAuth + activity sync into the training log.

Requires a free API application at https://www.strava.com/settings/api.
Tokens live in data/_strava_tokens.json (gitignored); client id/secret go in
Settings and persist via data/overrides.yaml.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import log as logbook

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"
DEAUTH_URL = "https://www.strava.com/oauth/deauthorize"

# activity:read_all covers private runs; read is not enough for "Only You".
DEFAULT_SCOPE = "read,activity:read_all"
DEFAULT_REDIRECT = "http://localhost:8501/"
# First connect pulls this far back; later syncs use last_sync with overlap.
INITIAL_LOOKBACK_DAYS = 90
SYNC_OVERLAP_HOURS = 36
PER_PAGE = 50
METERS_PER_MILE = logbook.METERS_PER_MILE


@dataclass
class SyncResult:
    imported: int = 0
    skipped: int = 0
    fetched: int = 0

    def __str__(self) -> str:
        return (f"fetched {self.fetched}, imported {self.imported}, "
                f"skipped {self.skipped}")


def tokens_path(cfg: dict) -> Path:
    return Path(cfg["db_path"]).resolve().parent / "_strava_tokens.json"


def strava_cfg(cfg: dict) -> dict:
    section = dict(cfg.get("strava") or {})
    section.setdefault("redirect_uri", DEFAULT_REDIRECT)
    section.setdefault("auto_sync_on_load", True)
    section.setdefault("lookback_days", INITIAL_LOOKBACK_DAYS)
    return section


def is_configured(cfg: dict) -> bool:
    s = strava_cfg(cfg)
    return bool(s.get("client_id") and s.get("client_secret"))


def load_tokens(cfg: dict) -> dict | None:
    path = tokens_path(cfg)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("access_token") or not data.get("refresh_token"):
        return None
    return data


def save_tokens(cfg: dict, data: dict) -> None:
    path = tokens_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def clear_tokens(cfg: dict) -> None:
    path = tokens_path(cfg)
    path.unlink(missing_ok=True)


def is_connected(cfg: dict) -> bool:
    return load_tokens(cfg) is not None


def athlete_label(cfg: dict) -> str | None:
    tokens = load_tokens(cfg)
    if not tokens:
        return None
    athlete = tokens.get("athlete") or {}
    parts = [athlete.get("firstname"), athlete.get("lastname")]
    name = " ".join(p for p in parts if p).strip()
    return name or (f"athlete {athlete['id']}" if athlete.get("id") else "connected")


def authorize_url(cfg: dict) -> str:
    s = strava_cfg(cfg)
    if not s.get("client_id"):
        raise ValueError("Set Strava client id in Settings first.")
    params = {
        "client_id": str(s["client_id"]),
        "response_type": "code",
        "redirect_uri": s.get("redirect_uri") or DEFAULT_REDIRECT,
        "approval_prompt": "auto",
        "scope": DEFAULT_SCOPE,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(cfg: dict, code: str) -> dict:
    s = strava_cfg(cfg)
    payload = {
        "client_id": str(s["client_id"]),
        "client_secret": str(s["client_secret"]),
        "code": code,
        "grant_type": "authorization_code",
    }
    data = _post_form(TOKEN_URL, payload)
    if "access_token" not in data:
        raise RuntimeError(data.get("message") or data.get("error")
                           or "Strava token exchange failed")
    save_tokens(cfg, {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": int(data.get("expires_at") or 0),
        "athlete": data.get("athlete") or {},
        "scope": data.get("scope") or DEFAULT_SCOPE,
        "last_sync_at": None,
    })
    return data


def disconnect(cfg: dict) -> None:
    tokens = load_tokens(cfg)
    if tokens:
        try:
            _post_form(DEAUTH_URL, {"access_token": tokens["access_token"]})
        except Exception:
            pass
    clear_tokens(cfg)


def access_token(cfg: dict) -> str:
    """Return a valid access token, refreshing when close to expiry."""
    tokens = load_tokens(cfg)
    if not tokens:
        raise RuntimeError("Strava is not connected.")
    expires_at = int(tokens.get("expires_at") or 0)
    now = int(datetime.now(timezone.utc).timestamp())
    if expires_at - now > 60:
        return tokens["access_token"]

    s = strava_cfg(cfg)
    data = _post_form(TOKEN_URL, {
        "client_id": str(s["client_id"]),
        "client_secret": str(s["client_secret"]),
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    })
    if "access_token" not in data:
        clear_tokens(cfg)
        raise RuntimeError(
            data.get("message") or "Strava refresh failed — reconnect in Log.")
    tokens.update({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or tokens["refresh_token"],
        "expires_at": int(data.get("expires_at") or 0),
    })
    if data.get("athlete"):
        tokens["athlete"] = data["athlete"]
    save_tokens(cfg, tokens)
    return tokens["access_token"]


def sync_activities(cfg: dict, *, after: datetime | None = None,
                    lookback_days: int | None = None) -> SyncResult:
    """Pull recent activities from Strava into the session log."""
    token = access_token(cfg)
    tokens = load_tokens(cfg) or {}
    s = strava_cfg(cfg)

    if after is None:
        last = tokens.get("last_sync_at")
        if last:
            after = datetime.fromisoformat(last) - timedelta(
                hours=SYNC_OVERLAP_HOURS)
        else:
            days = lookback_days or int(s.get("lookback_days")
                                        or INITIAL_LOOKBACK_DAYS)
            after = datetime.now(timezone.utc) - timedelta(days=days)

    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)

    result = SyncResult()
    page = 1
    with logbook.connect(cfg) as conn:
        while True:
            params = {
                "after": int(after.timestamp()),
                "page": page,
                "per_page": PER_PAGE,
            }
            batch = _get_json(
                f"{API_BASE}/athlete/activities?{urllib.parse.urlencode(params)}",
                token,
            )
            if not isinstance(batch, list) or not batch:
                break
            for activity in batch:
                result.fetched += 1
                parsed = activity_to_session(activity)
                if not parsed:
                    result.skipped += 1
                    continue
                if logbook.has_external_id(
                        cfg, parsed.get("external_id"), conn=conn):
                    result.skipped += 1
                    continue
                try:
                    logbook.log_session(
                        cfg, source="strava", conn=conn, **parsed)
                    result.imported += 1
                except sqlite3.IntegrityError:
                    result.skipped += 1
            if len(batch) < PER_PAGE:
                break
            page += 1
            if page > 40:
                break

    tokens["last_sync_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    save_tokens(cfg, tokens)
    return result


def activity_to_session(activity: dict[str, Any]) -> dict | None:
    """Map a Strava activity JSON object to log_session kwargs."""
    sport = (activity.get("sport_type") or activity.get("type") or "").strip()
    kind = _kind_for_sport(sport)
    if kind is None:
        return None

    when = _activity_date(activity)
    if when is None:
        return None

    meters = float(activity.get("distance") or 0)
    miles = round(meters / METERS_PER_MILE, 2) if meters else 0.0
    moving = activity.get("moving_time")
    duration = float(moving) if moving not in (None, "") else None

    # Strava workout_type 1 on a run = race.
    if kind == logbook.KIND_RUN and activity.get("workout_type") == 1:
        kind = logbook.KIND_RACE

    notes_bits = []
    if activity.get("trainer"):
        notes_bits.append("indoor / trainer")
    if activity.get("commute"):
        notes_bits.append("commute")
    elev = activity.get("total_elevation_gain")
    if elev and float(elev) >= 50:
        notes_bits.append(f"+{float(elev):.0f} m elev")

    activity_id = activity.get("id")
    return {
        "when": when,
        "kind": kind,
        "miles": miles,
        "duration": duration,
        "label": (activity.get("name") or "").strip(),
        "notes": "; ".join(notes_bits),
        "external_id": f"strava:{activity_id}" if activity_id else None,
        "rpe": _rpe_from_activity(activity),
    }


def _kind_for_sport(sport: str) -> str | None:
    key = sport.lower().replace(" ", "")
    mapping = {
        "run": logbook.KIND_RUN,
        "trailrun": logbook.KIND_RUN,
        "virtualrun": logbook.KIND_RUN,
        "treadmill": logbook.KIND_RUN,
        "rockclimbing": logbook.KIND_CLIMB,
        "climb": logbook.KIND_CLIMB,
        "weighttraining": logbook.KIND_STRENGTH,
        "workout": logbook.KIND_STRENGTH,
        "yoga": logbook.KIND_CROSS,
        "pilates": logbook.KIND_CROSS,
    }
    if key in mapping:
        return mapping[key]
    if "run" in key:
        return logbook.KIND_RUN
    if "climb" in key or "bould" in key:
        return logbook.KIND_CLIMB
    # Skip rides, swims, etc. — not part of the coach log by default.
    return None


def _activity_date(activity: dict) -> date | None:
    raw = activity.get("start_date_local") or activity.get("start_date")
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return logbook._parse_flexible_date(str(raw))


def _rpe_from_activity(activity: dict) -> int | None:
    """Strava perceived_exertion is 1-10 when the athlete set it."""
    value = activity.get("perceived_exertion")
    if value is None:
        return None
    try:
        return max(1, min(10, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _post_form(url: str, payload: dict) -> dict:
    body = urllib.parse.urlencode(
        {k: v for k, v in payload.items() if v is not None}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    return _read_json(req)


def _get_json(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json"},
    )
    return _read_json(req)


def _read_json(req: urllib.request.Request) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            payload = json.loads(detail)
            message = payload.get("message") or payload.get("error") or detail
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise RuntimeError(f"Strava HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Strava: {exc.reason}") from exc


def setup_help(redirect_uri: str = DEFAULT_REDIRECT) -> str:
    domain = urllib.parse.urlparse(redirect_uri).hostname or "localhost"
    return (
        "1. Open https://www.strava.com/settings/api and create an API app "
        "(or use an existing one).\n"
        f"2. Set **Authorization Callback Domain** to `{domain}` "
        f"(no `http://`, no port).\n"
        f"3. Paste Client ID and Client Secret below. Redirect URI defaults to "
        f"`{redirect_uri}`.\n"
        "4. Click **Connect Strava**, approve activity access, and you will "
        "land back here with a `?code=` in the URL — the app finishes the link "
        "automatically.\n"
        "5. Use **Sync from Strava** (or open Log with auto-sync on) to pull runs."
    )


def format_last_sync(cfg: dict) -> str | None:
    tokens = load_tokens(cfg)
    if not tokens or not tokens.get("last_sync_at"):
        return None
    try:
        stamp = datetime.fromisoformat(tokens["last_sync_at"])
        return stamp.strftime("%d %b %Y %H:%M")
    except ValueError:
        return str(tokens["last_sync_at"])
