"""Derived training metrics: volume, workload ratio, pace trend, projected finish."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from . import log as logbook
from .athlete import (
    Fitness,
    MARATHON_MILES,
    baseline_fitness,
    build_fitness,
    fitness_from_training_pace,
    format_duration,
    format_pace,
    parse_duration,
)
from .plan.generator import Plan

ACUTE_DAYS = 7
CHRONIC_DAYS = 28

# How early to start nagging about an unclaimed race entry. Long enough that a
# capped race has not sold out, short enough that it is not background noise for
# most of the plan.
ENTRY_WARN_DAYS = 120
# ACWR compares the last week against a 28-day baseline, so it reports absurd
# ratios until that baseline exists. Below this much logged history it is
# withheld rather than shown as a false alarm.
MIN_HISTORY_DAYS = 21

# Enough recent aerobic runs to trust a median training pace over the seed.
TRAINING_FITNESS_LOOKBACK_DAYS = 28
TRAINING_FITNESS_MIN_RUNS = 3
TRAINING_FITNESS_MIN_MILES = 2.0
# RPE at or below this is treated as aerobic; harder efforts are skipped when
# enough easy runs exist so a tempo does not drag the median down.
TRAINING_FITNESS_EASY_RPE = 6


def _effort_from_rpe(rpe: float | None, fallback: str) -> str:
    if rpe is None or (isinstance(rpe, float) and pd.isna(rpe)):
        return fallback
    value = float(rpe)
    if value <= 4:
        return "easy"
    if value <= 6:
        return "moderate"
    return "hard"


@dataclass
class Status:
    as_of: date
    fitness: Fitness
    acwr: float | None
    acute_miles: float
    chronic_weekly_miles: float
    last_4_week_peak: float
    days_since_run: int | None
    pain_streak: int
    compliance_pct: float | None
    projected_finish: float
    goal_seconds: float
    must_beat_seconds: float
    warnings: list[str]

    @property
    def on_track_for_goal(self) -> bool:
        return self.projected_finish <= self.goal_seconds

    @property
    def on_track_for_sub4(self) -> bool:
        return self.projected_finish <= self.must_beat_seconds


def weekly_actual(cfg: dict) -> pd.DataFrame:
    """Miles actually run, grouped into Monday-anchored weeks."""
    df = logbook.load_sessions(cfg)
    if df.empty:
        return pd.DataFrame(columns=["week_start", "miles", "runs"])

    runs = df[df["kind"].isin(logbook.RUN_KINDS)].copy()
    if runs.empty:
        return pd.DataFrame(columns=["week_start", "miles", "runs"])

    runs["week_start"] = runs["date"].map(lambda d: d - timedelta(days=d.weekday()))
    grouped = runs.groupby("week_start").agg(
        miles=("miles", "sum"), runs=("miles", "count")).reset_index()
    return grouped.sort_values("week_start")


def plan_vs_actual(cfg: dict, plan: Plan) -> pd.DataFrame:
    """Planned against completed volume for every week of the plan."""
    actual = weekly_actual(cfg).set_index("week_start")
    rows = []
    for wk in plan.weeks:
        start = wk.start_date
        done = actual.loc[start] if start in actual.index else None
        rows.append({
            "week": wk.index,
            "phase": wk.phase,
            "week_start": start,
            "kind": wk.kind,
            "planned": wk.planned_miles,
            "actual": float(done["miles"]) if done is not None else 0.0,
            "runs_done": int(done["runs"]) if done is not None else 0,
        })
    df = pd.DataFrame(rows)
    df["delta"] = (df["actual"] - df["planned"]).round(1)
    return df


def load_in_window(cfg: dict, as_of: date, days: int) -> float:
    start = as_of - timedelta(days=days - 1)
    df = logbook.load_sessions(cfg, start=start, end=as_of)
    if df.empty:
        return 0.0
    return float(df[df["kind"].isin(logbook.RUN_KINDS)]["miles"].sum())


def acwr(cfg: dict, as_of: date) -> tuple[float | None, float, float]:
    """Acute:chronic workload ratio.

    Compares the last 7 days against the average week of the last 28. Above
    roughly 1.5 the injury risk climbs sharply; below 0.8 fitness is decaying.
    Returns (ratio, acute_miles, chronic_weekly_miles).
    """
    acute = load_in_window(cfg, as_of, ACUTE_DAYS)
    chronic_total = load_in_window(cfg, as_of, CHRONIC_DAYS)
    chronic_weekly = chronic_total / (CHRONIC_DAYS / 7)
    if chronic_weekly <= 0 or not has_enough_history(cfg, as_of):
        return None, acute, chronic_weekly
    return round(acute / chronic_weekly, 2), acute, chronic_weekly


def has_enough_history(cfg: dict, as_of: date) -> bool:
    df = logbook.load_sessions(cfg)
    runs = df[df["kind"].isin(logbook.RUN_KINDS)] if not df.empty else df
    if runs.empty:
        return False
    return (as_of - min(runs["date"])).days >= MIN_HISTORY_DAYS


def recent_peak_weekly(cfg: dict, as_of: date, weeks: int = 4) -> float:
    """Highest weekly volume in the recent past, used for the volume penalty."""
    actual = weekly_actual(cfg)
    if actual.empty:
        return float(cfg["start"]["current_weekly_miles"])
    cutoff = as_of - timedelta(weeks=weeks)
    recent = actual[actual["week_start"] >= cutoff]
    if recent.empty:
        return float(cfg["start"]["current_weekly_miles"])
    return float(recent["miles"].max())


def pain_streak(cfg: dict) -> int:
    """Consecutive most-recent runs logging pain at or above the threshold."""
    threshold = int(cfg["rules"]["pain_threshold"])
    df = logbook.load_sessions(cfg)
    if df.empty:
        return 0
    runs = df[df["kind"].isin(logbook.RUN_KINDS)].sort_values(["date", "id"])
    streak = 0
    for _, row in runs.iloc[::-1].iterrows():
        if int(row["pain"] or 0) >= threshold:
            streak += 1
        else:
            break
    return streak


def recent_training_runs(cfg: dict, as_of: date | None = None) -> pd.DataFrame:
    """Aerobic runs used to refresh training paces from the log."""
    as_of = as_of or date.today()
    start = as_of - timedelta(days=TRAINING_FITNESS_LOOKBACK_DAYS - 1)
    df = logbook.load_sessions(cfg, start=start, end=as_of)
    if df.empty:
        return pd.DataFrame(columns=["date", "pace", "miles", "rpe"])
    runs = df[
        (df["kind"] == logbook.KIND_RUN)
        & df["pace"].notna()
        & (df["miles"] >= TRAINING_FITNESS_MIN_MILES)
    ].copy()
    if runs.empty:
        return runs
    easy = runs[
        runs["rpe"].isna() | (runs["rpe"] <= TRAINING_FITNESS_EASY_RPE)
    ]
    if len(easy) >= TRAINING_FITNESS_MIN_RUNS:
        return easy.sort_values("date")
    return runs.sort_values("date")


def training_fitness_from_log(cfg: dict, as_of: date | None = None,
                              peak_weekly_miles: float | None = None
                              ) -> Fitness | None:
    """Estimate fitness from recent logged training paces.

    Returns None until enough aerobic runs exist. Disabled when
    ``start.adapt_paces_from_log`` is false so the seed stays locked.
    """
    if not cfg.get("start", {}).get("adapt_paces_from_log", True):
        return None
    as_of = as_of or date.today()
    runs = recent_training_runs(cfg, as_of)
    if len(runs) < TRAINING_FITNESS_MIN_RUNS:
        return None

    pace = float(runs["pace"].median())
    miles = float(runs["miles"].median())
    rpe_median = (
        float(runs["rpe"].dropna().median())
        if runs["rpe"].notna().any() else None
    )
    fallback = str(cfg["start"].get("current_effort", "moderate"))
    effort = _effort_from_rpe(rpe_median, fallback)
    peak = peak_weekly_miles if peak_weekly_miles is not None else recent_peak_weekly(
        cfg, as_of)
    return fitness_from_training_pace(
        pace,
        effort,
        peak,
        source=(
            f"recent training: median {format_pace(pace)} ({effort}) "
            f"over {len(runs)} runs"
        ),
        representative_miles=miles,
    )


def log_start_snapshot(cfg: dict, as_of: date | None = None) -> dict | None:
    """Suggested start fields from the log, for Settings display / apply."""
    as_of = as_of or date.today()
    runs = recent_training_runs(cfg, as_of)
    peak = recent_peak_weekly(cfg, as_of)
    weekly = weekly_actual(cfg)
    recent_weeks = weekly[weekly["week_start"] >= as_of - timedelta(weeks=4)]
    if runs.empty and recent_weeks.empty:
        return None

    snapshot: dict = {
        "current_weekly_miles": round(peak, 1),
    }
    if len(runs) >= TRAINING_FITNESS_MIN_RUNS:
        pace = float(runs["pace"].median())
        snapshot["current_easy_pace"] = format_duration(pace)
        snapshot["current_long_run_miles"] = round(float(runs["miles"].max()), 1)
        if runs["rpe"].notna().any():
            snapshot["current_effort"] = _effort_from_rpe(
                float(runs["rpe"].dropna().median()),
                str(cfg["start"].get("current_effort", "moderate")),
            )
        snapshot["run_count"] = len(runs)
    if not recent_weeks.empty:
        snapshot["current_runs_per_week"] = int(round(
            float(recent_weeks["runs"].mean())))
    return snapshot


def current_fitness(cfg: dict, as_of: date | None = None) -> Fitness:
    """Best available fitness estimate.

    Priority: logged race (hard evidence) → recent training paces from the
    log → self-reported starting point. Volume always tracks the recent peak
    so projections improve as the base grows even before the first tune-up.
    """
    as_of = as_of or date.today()
    peak = recent_peak_weekly(cfg, as_of)

    race = logbook.best_race(cfg)
    if race:
        return build_fitness(
            source=f"{race['label']} on {race['date']}",
            miles=race["miles"],
            seconds=race["duration_s"],
            peak_weekly_miles=peak,
        )

    from_log = training_fitness_from_log(cfg, as_of, peak_weekly_miles=peak)
    if from_log:
        return from_log

    fitness = baseline_fitness(cfg)
    fitness.peak_weekly_miles = peak
    return fitness


def pace_trend(cfg: dict, zone_max_pace: float | None = None) -> pd.DataFrame:
    """Easy-run pace over time, as a rough read on aerobic progress."""
    df = logbook.load_sessions(cfg)
    if df.empty:
        return pd.DataFrame(columns=["date", "pace", "miles"])
    runs = df[(df["kind"] == logbook.KIND_RUN) & df["pace"].notna()
              & (df["miles"] >= 2.0)].copy()
    if runs.empty:
        return pd.DataFrame(columns=["date", "pace", "miles"])
    if zone_max_pace:
        runs = runs[runs["pace"] >= zone_max_pace]
    return runs[["date", "pace", "miles", "rpe"]].sort_values("date")


def compliance(cfg: dict, plan: Plan, as_of: date) -> float | None:
    """Share of planned runs completed so far, by volume."""
    df = plan_vs_actual(cfg, plan)
    elapsed = df[df["week_start"] < as_of - timedelta(days=as_of.weekday())]
    if elapsed.empty or elapsed["planned"].sum() == 0:
        return None
    return round(100 * elapsed["actual"].sum() / elapsed["planned"].sum(), 1)


def status(cfg: dict, plan: Plan, as_of: date | None = None) -> Status:
    as_of = as_of or date.today()
    fitness = current_fitness(cfg, as_of)
    ratio, acute, chronic = acwr(cfg, as_of)
    streak = pain_streak(cfg)
    gap_days = logbook.days_since_last_run(cfg, as_of)

    goal = parse_duration(cfg["race"]["goal_time"])
    must = parse_duration(cfg["race"]["must_beat"])
    projected = fitness.predicted_marathon

    warnings: list[str] = []
    rules = cfg["rules"]
    if ratio and ratio > float(rules["acwr_flag"]):
        warnings.append(
            f"Workload ratio {ratio} is above {rules['acwr_flag']}. The last 7 days "
            f"({acute:.1f} mi) are well ahead of your 4-week average "
            f"({chronic:.1f} mi/wk). Hold volume flat this week.")
    if ratio and ratio < float(rules["acwr_floor"]) and chronic > 5:
        warnings.append(
            f"Workload ratio {ratio} is below {rules['acwr_floor']} - training has "
            f"dropped off relative to your recent norm.")
    if streak >= int(rules["pain_consecutive"]):
        warnings.append(
            f"{streak} consecutive runs logged pain at or above "
            f"{rules['pain_threshold']}. The plan has forced a cutback week; "
            f"see someone about it if it persists.")
    if gap_days is not None and gap_days >= int(rules["gap_repeat_days"]):
        warnings.append(f"{gap_days} days since the last run - the plan has re-based.")
    if projected > must:
        warnings.append(
            f"Projected finish {format_duration(projected)} is outside "
            f"{cfg['race']['must_beat']}. Still {_weeks_to(cfg, as_of)} weeks to "
            f"change that, and base volume moves this number most.")
    warnings.extend(_entry_warnings(cfg, as_of))

    return Status(
        as_of=as_of,
        fitness=fitness,
        acwr=ratio,
        acute_miles=round(acute, 1),
        chronic_weekly_miles=round(chronic, 1),
        last_4_week_peak=recent_peak_weekly(cfg, as_of),
        days_since_run=gap_days,
        pain_streak=streak,
        compliance_pct=compliance(cfg, plan, as_of),
        projected_finish=projected,
        goal_seconds=goal,
        must_beat_seconds=must,
        warnings=warnings,
    )


def _weeks_to(cfg: dict, as_of: date) -> int:
    return max(0, (cfg["race"]["date"] - as_of).days // 7)


def _entry_warnings(cfg: dict, as_of: date) -> list[str]:
    """Nag about the hard close date until the bib is marked bought."""
    race = cfg.get("race") or {}
    deadline = race.get("registration_deadline")
    if not deadline or race.get("registered"):
        return []
    days = (deadline - as_of).days
    name = race.get("name", "Goal race")
    url = race.get("registration_url", "")
    if days < 0:
        return [f"{name} entries closed on {deadline}."]
    if days <= ENTRY_WARN_DAYS:
        link = f" {url}" if url else ""
        return [
            f"{name} entries close {deadline} ({days} days) and are not marked "
            f"registered.{link} Set race.registered: true once done."
        ]
    return []


def projected_finish_series(cfg: dict, plan: Plan) -> pd.DataFrame:
    """How the projected finish would evolve if the plan is followed.

    Fitness is held at its current VDOT and only the volume penalty relaxes, so
    this shows the gain available from consistency alone - not from getting
    faster, which the tune-up races are there to measure.
    """
    fitness = current_fitness(cfg)
    rows = []
    for wk in plan.weeks:
        projected = build_fitness(
            source=fitness.source,
            miles=fitness.source_miles,
            seconds=fitness.source_seconds,
            peak_weekly_miles=wk.planned_miles,
        )
        rows.append({
            "week": wk.index,
            "week_start": wk.start_date,
            "planned_miles": wk.planned_miles,
            "projected_seconds": projected.predicted_marathon,
        })
    df = pd.DataFrame(rows)
    # Volume only helps once it has been held, so smooth over a 4-week window.
    df["projected_seconds"] = df["projected_seconds"].rolling(4, min_periods=1).mean()
    return df
