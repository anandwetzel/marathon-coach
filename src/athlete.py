"""Athlete profile, training pace zones, and fitness estimation.

Goal pace is derived here rather than assumed. The config records what the
athlete *wants* (`race.goal_time`); this module reports what current evidence
actually supports, so the two can be compared honestly week to week.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MILE_M = 1609.344
MARATHON_MILES = 26.2188
HALF_MILES = 13.1094

# Fraction of VDOT each training zone is run at (Daniels' Running Formula).
ZONE_PCT = {
    "recovery": 0.65,
    "easy": 0.72,
    "marathon": 0.84,
    "threshold": 0.88,
    "interval": 0.975,
    "repetition": 1.05,
}

ZONE_LABEL = {
    "recovery": "Recovery",
    "easy": "Easy",
    "marathon": "Marathon pace",
    "threshold": "Threshold / tempo",
    "interval": "Interval (VO2max)",
    "repetition": "Repetition / strides",
}

# Riegel's endurance exponent. 1.06 is the classic fit across race distances.
RIEGEL_EXPONENT = 1.06

# Weekly mileage at or above which a marathon prediction needs no penalty.
FULL_VOLUME_MILES = 40.0
LOW_VOLUME_MILES = 10.0
MAX_VOLUME_PENALTY = 0.15

# How hard a self-reported training pace actually is, as a fraction of VO2max.
# Low-volume runners typically report a moderate effort as their "normal" pace,
# so assuming it is genuinely easy inflates the fitness estimate badly.
EFFORT_PCT = {
    "easy": 0.72,
    "moderate": 0.80,
    "hard": 0.86,
}


def parse_pace(value: str) -> float:
    """'8:30' -> 510.0 seconds per mile."""
    return parse_duration(value)


def parse_duration(value: str | float | int) -> float:
    """Accept 'H:MM:SS', 'MM:SS', or a raw number of seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    parts = [p.strip() for p in str(value).split(":")]
    if not all(parts):
        raise ValueError(f"cannot parse duration: {value!r}")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_pace(sec_per_mile: float) -> str:
    return f"{format_duration(sec_per_mile)}/mi"


def format_pace_km(sec_per_mile: float) -> str:
    return f"{format_duration(sec_per_mile / 1.609344)}/km"


def pace_both(sec_per_mile: float) -> str:
    return f"{format_pace(sec_per_mile)} ({format_pace_km(sec_per_mile)})"


def riegel(known_seconds: float, known_miles: float, target_miles: float,
           exponent: float = RIEGEL_EXPONENT) -> float:
    """Predict time at a new distance from a known performance."""
    if known_miles <= 0 or target_miles <= 0:
        raise ValueError("distances must be positive")
    return known_seconds * (target_miles / known_miles) ** exponent


def volume_penalty(peak_weekly_miles: float) -> float:
    """VDOT and Riegel both assume distance-appropriate training; the marathon
    punishes a thin aerobic base far more than shorter races do. Scale the
    prediction up when weekly volume does not yet support the distance.
    """
    peak = max(0.0, float(peak_weekly_miles))
    if peak >= FULL_VOLUME_MILES:
        return 1.0
    if peak <= LOW_VOLUME_MILES:
        return 1.0 + MAX_VOLUME_PENALTY
    span = FULL_VOLUME_MILES - LOW_VOLUME_MILES
    return 1.0 + (FULL_VOLUME_MILES - peak) / span * MAX_VOLUME_PENALTY


def predict_marathon_riegel(known_seconds: float, known_miles: float,
                            peak_weekly_miles: float) -> float:
    """Riegel marathon prediction, kept as a cross-check against the VDOT model."""
    raw = riegel(known_seconds, known_miles, MARATHON_MILES)
    return raw * volume_penalty(peak_weekly_miles)


def pct_vo2max(minutes: float) -> float:
    """Fraction of VO2max sustainable for a race of the given duration."""
    return (0.8
            + 0.1894393 * math.exp(-0.012778 * minutes)
            + 0.2989558 * math.exp(-0.1932605 * minutes))


def vo2_at_velocity(metres_per_min: float) -> float:
    return -4.60 + 0.182258 * metres_per_min + 0.000104 * metres_per_min ** 2


def velocity_at_vo2(vo2: float) -> float:
    """Invert the VO2 cost-of-running curve (positive root of the quadratic)."""
    a, b, c = 0.000104, 0.182258, -(4.60 + vo2)
    disc = b * b - 4 * a * c
    if disc < 0:
        raise ValueError("no real velocity for that VO2")
    return (-b + math.sqrt(disc)) / (2 * a)


def vdot_from_performance(miles: float, seconds: float) -> float:
    minutes = seconds / 60.0
    metres = miles * MILE_M
    velocity = metres / minutes
    return vo2_at_velocity(velocity) / pct_vo2max(minutes)


def paces_from_vdot(vdot: float) -> dict[str, float]:
    """Training paces in seconds per mile for each zone."""
    paces = {}
    for zone, pct in ZONE_PCT.items():
        velocity = velocity_at_vo2(vdot * pct)  # metres per minute
        paces[zone] = MILE_M / velocity * 60.0
    return paces


def vdot_for_target(miles: float, seconds: float) -> float:
    """The VDOT a goal performance would require."""
    return vdot_from_performance(miles, seconds)


def hr_zones(age: int | None, resting_hr: int | None = None) -> dict[str, tuple[int, int]]:
    """Heart-rate zones from the Nes age formula, which fits adults better than
    the 220-age rule. Returns empty when age is unknown.
    """
    if not age:
        return {}
    hr_max = round(211 - 0.64 * age)
    bands = {
        "recovery": (0.60, 0.70),
        "easy": (0.70, 0.78),
        "marathon": (0.78, 0.85),
        "threshold": (0.85, 0.91),
        "interval": (0.91, 0.98),
    }
    if resting_hr:
        # Karvonen reserve method when resting HR is known.
        reserve = hr_max - resting_hr
        return {
            zone: (round(resting_hr + lo * reserve), round(resting_hr + hi * reserve))
            for zone, (lo, hi) in bands.items()
        }
    return {zone: (round(lo * hr_max), round(hi * hr_max)) for zone, (lo, hi) in bands.items()}


@dataclass
class Fitness:
    """A fitness snapshot derived from one supporting performance."""

    vdot: float
    source: str
    source_miles: float
    source_seconds: float
    peak_weekly_miles: float
    paces: dict[str, float] = field(default_factory=dict)

    @property
    def marathon_potential(self) -> float:
        """What this VDOT is worth if fully marathon-trained. Aspirational, not
        a prediction of race day at current volume.
        """
        return race_time_for_vdot(self.vdot, MARATHON_MILES)

    @property
    def predicted_marathon(self) -> float:
        """Realistic finish time today, discounted for the current aerobic base."""
        return self.marathon_potential * volume_penalty(self.peak_weekly_miles)

    @property
    def predicted_marathon_riegel(self) -> float:
        return predict_marathon_riegel(self.source_seconds, self.source_miles,
                                       self.peak_weekly_miles)

    @property
    def predicted_marathon_pace(self) -> float:
        return self.predicted_marathon / MARATHON_MILES

    @property
    def predicted_half(self) -> float:
        return race_time_for_vdot(self.vdot, HALF_MILES)

    def gap_to(self, goal_seconds: float) -> float:
        """Positive means the prediction is slower than the goal."""
        return self.predicted_marathon - goal_seconds


def build_fitness(source: str, miles: float, seconds: float,
                  peak_weekly_miles: float) -> Fitness:
    vdot = vdot_from_performance(miles, seconds)
    return Fitness(
        vdot=vdot,
        source=source,
        source_miles=miles,
        source_seconds=seconds,
        peak_weekly_miles=peak_weekly_miles,
        paces=paces_from_vdot(vdot),
    )


def fitness_from_training_pace(
    pace_sec_per_mile: float,
    effort: str,
    peak_weekly_miles: float,
    *,
    source: str | None = None,
    representative_miles: float = 5.0,
) -> Fitness:
    """Fitness from a training pace read at a sub-maximal effort.

    A training run is not a race: reading a conversational pace as race-effort
    easy would overstate VDOT by roughly 5 points and set every zone too fast.
    """
    effort_key = str(effort or "moderate").lower()
    pct = EFFORT_PCT.get(effort_key, EFFORT_PCT["moderate"])
    velocity = MILE_M / (pace_sec_per_mile / 60.0)
    vdot = vo2_at_velocity(velocity) / pct
    paces = paces_from_vdot(vdot)
    five_k_miles = 3.10686
    five_k_seconds = race_time_for_vdot(vdot, five_k_miles)
    label = source or (
        f"training: {format_pace(pace_sec_per_mile)} ({effort_key}) "
        f"for {representative_miles:.0f} mi"
    )
    return Fitness(
        vdot=vdot,
        source=label,
        source_miles=five_k_miles,
        source_seconds=five_k_seconds,
        peak_weekly_miles=peak_weekly_miles,
        paces=paces,
    )


def baseline_fitness(cfg: dict) -> Fitness:
    """Fitness implied by the self-reported starting point in config.yaml."""
    start = cfg["start"]
    pace = parse_pace(start["current_easy_pace"])
    miles = float(start["current_long_run_miles"])
    effort = str(start.get("current_effort", "moderate")).lower()
    return fitness_from_training_pace(
        pace,
        effort,
        float(start["current_weekly_miles"]),
        source=f"baseline: {format_pace(pace)} ({effort}) for {miles:.0f} mi",
        representative_miles=miles,
    )


def _race_time_for_vdot(vdot: float, miles: float) -> float:
    """Time this VDOT implies at a race distance. Solved iteratively because
    the sustainable fraction of VO2max itself depends on the finish time.
    """
    metres = miles * MILE_M
    minutes = metres / velocity_at_vo2(vdot * 0.9) # seed
    for _ in range(60):
        velocity = velocity_at_vo2(vdot * pct_vo2max(minutes))
        new_minutes = metres / velocity
        if abs(new_minutes - minutes) < 1e-6:
            minutes = new_minutes
            break
        minutes = new_minutes
    return minutes * 60.0


def race_time_for_vdot(vdot: float, miles: float) -> float:
    return _race_time_for_vdot(vdot, miles)


def goal_paces(cfg: dict) -> dict[str, float]:
    """Marathon pace implied by the goal time and by the must-beat time."""
    distance = float(cfg["race"]["distance_miles"])
    return {
        "goal": parse_duration(cfg["race"]["goal_time"]) / distance,
        "must_beat": parse_duration(cfg["race"]["must_beat"]) / distance,
    }
