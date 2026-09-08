"""Single entry point to rebuild the plan from config + log."""

from __future__ import annotations

from .. import calendar_feed, log as logbook, metrics
from .adapt import AdaptResult, adapt_plan
from .generator import Plan, build_plan, save
from . import schedule as schedulemod


def rebuild_plan(cfg: dict, *, write_ics: bool = True) -> AdaptResult:
    """Build, adapt, apply schedule moves, and persist.

    Shared by the CLI and dashboard so both stay in lockstep.
    """
    fitness = metrics.current_fitness(cfg)
    plan = build_plan(
        cfg,
        fitness=fitness,
        long_run_overrides=logbook.long_run_overrides(cfg),
    )
    result = adapt_plan(cfg, plan)
    schedulemod.apply_schedule_moves(result.plan, cfg)
    save(result.plan, cfg["plan_path"])
    if write_ics:
        try:
            calendar_feed.write(result.plan, cfg)
        except OSError:
            pass
    return result


def build_adapted_plan(cfg: dict) -> Plan:
    """Build + adapt in memory without writing files (tests / dry runs)."""
    fitness = metrics.current_fitness(cfg)
    plan = build_plan(
        cfg,
        fitness=fitness,
        long_run_overrides=logbook.long_run_overrides(cfg),
    )
    return adapt_plan(cfg, plan).plan
