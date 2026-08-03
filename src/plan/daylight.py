"""Sunset calculation for the training location, and daylight tagging of sessions.

Running happens after work, the Amsterdamse Bos is unlit, and sunset in
Amstelveen falls below 17:00 from late October to mid-February. That covers
essentially the whole marathon block, so whether a session lands in daylight is
a scheduling input, not a footnote.

Implements the NOAA sunrise/sunset equation directly to avoid a dependency;
accurate to well under a minute at these latitudes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Solar altitude at which the sun is considered set, including refraction and
# the solar disc radius.
SUNSET_ALTITUDE = -0.833
CIVIL_TWILIGHT_ALTITUDE = -6.0

LIT = "lit"
MARGINAL = "marginal"
DARK = "dark"

DAYLIGHT_LABEL = {
    LIT: "daylight",
    MARGINAL: "fading light",
    DARK: "dark",
}


@dataclass
class SunTimes:
    sunrise: datetime
    sunset: datetime
    civil_dusk: datetime
    civil_dawn: datetime


def _julian_from_date(d: date) -> float:
    return d.toordinal() + 1721424.5


def _datetime_from_julian(jd: float, tz: ZoneInfo) -> datetime:
    unix = (jd - 2440587.5) * 86400.0
    return datetime.fromtimestamp(unix, tz=timezone.utc).astimezone(tz)


def _solar_event(d: date, lat: float, lon: float, tz: ZoneInfo,
                 altitude: float) -> tuple[datetime | None, datetime | None]:
    """Return (rise, set) for the given solar altitude, or (None, None) when the
    sun never reaches it (polar day or night - not a concern at 52N, but the
    guard keeps the maths honest).
    """
    n = round(_julian_from_date(d) - 2451545.0 + 0.0008)
    # Mean solar noon. East of Greenwich the sun crosses the meridian earlier in
    # UTC, so an east-positive longitude subtracts here.
    j_star = n - lon / 360.0

    mean_anomaly = (357.5291 + 0.98560028 * j_star) % 360.0
    m_rad = math.radians(mean_anomaly)
    center = (1.9148 * math.sin(m_rad)
              + 0.0200 * math.sin(2 * m_rad)
              + 0.0003 * math.sin(3 * m_rad))
    ecliptic_lon = (mean_anomaly + center + 180.0 + 102.9372) % 360.0
    lambda_rad = math.radians(ecliptic_lon)

    j_transit = (2451545.0 + j_star
                 + 0.0053 * math.sin(m_rad)
                 - 0.0069 * math.sin(2 * lambda_rad))

    sin_decl = math.sin(lambda_rad) * math.sin(math.radians(23.4397))
    decl = math.asin(sin_decl)

    lat_rad = math.radians(lat)
    numerator = math.sin(math.radians(altitude)) - math.sin(lat_rad) * math.sin(decl)
    denominator = math.cos(lat_rad) * math.cos(decl)
    cos_hour_angle = numerator / denominator
    if cos_hour_angle > 1 or cos_hour_angle < -1:
        return None, None

    hour_angle = math.degrees(math.acos(cos_hour_angle))
    j_set = j_transit + hour_angle / 360.0
    j_rise = j_transit - hour_angle / 360.0
    return _datetime_from_julian(j_rise, tz), _datetime_from_julian(j_set, tz)


def sun_times(d: date, cfg: dict) -> SunTimes:
    loc = cfg["location"]
    tz = ZoneInfo(loc["timezone"])
    lat, lon = float(loc["lat"]), float(loc["lon"])

    rise, set_ = _solar_event(d, lat, lon, tz, SUNSET_ALTITUDE)
    dawn, dusk = _solar_event(d, lat, lon, tz, CIVIL_TWILIGHT_ALTITUDE)

    # At 52N the sun always crosses these altitudes; fall back to the sunset
    # times if it somehow does not, so callers never see None.
    return SunTimes(
        sunrise=rise,
        sunset=set_,
        civil_dawn=dawn or rise,
        civil_dusk=dusk or set_,
    )


def session_start(d: date, cfg: dict) -> time:
    """When a session realistically begins, given a 08:00-17:00 workday."""
    constraints = cfg["constraints"]
    key = "weekend_start" if d.weekday() >= 5 else "weekday_earliest_start"
    hh, mm = str(constraints[key]).split(":")[:2]
    return time(int(hh), int(mm))


def classify(d: date, start: time, duration_minutes: float, cfg: dict) -> str:
    """Tag a session lit, marginal, or dark."""
    tz = ZoneInfo(cfg["location"]["timezone"])
    sun = sun_times(d, cfg)
    begin = datetime.combine(d, start, tzinfo=tz)
    end = begin + timedelta(minutes=duration_minutes)

    buffer = timedelta(minutes=float(cfg["daylight"]["dark_buffer_min"]))

    # A session that starts before dawn is as dark as one that ends after dusk.
    if begin < sun.civil_dawn:
        return DARK
    if end <= sun.sunset - buffer:
        return LIT
    if end <= sun.civil_dusk:
        return MARGINAL
    return DARK


def first_dark_date(cfg: dict, start: date, end: date,
                    duration_minutes: float = 40.0) -> date | None:
    """First weekday on which a typical after-work run finishes in the dark.

    Drives the headlamp purchase trigger in the gear module.
    """
    d = start
    while d <= end:
        if d.weekday() < 5:
            if classify(d, session_start(d, cfg), duration_minutes, cfg) == DARK:
                return d
        d += timedelta(days=1)
    return None


def daylight_note(tag: str, d: date, cfg: dict) -> str:
    sun = sun_times(d, cfg)
    sunset_str = sun.sunset.strftime("%H:%M")
    if tag == LIT:
        return f"Daylight, sunset {sunset_str}"
    if tag == MARGINAL:
        return f"Fading light, sunset {sunset_str} - reflective gear"
    return f"Dark, sunset {sunset_str} - headlamp and reflective gear"
