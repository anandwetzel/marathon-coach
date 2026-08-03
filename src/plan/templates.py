"""Phase and microcycle definitions loaded from the YAML plan templates.

Templates describe a week as a set of *roles* (long, quality, medium, easy)
rather than named weekdays. The generator is what turns roles into dates, so
the same template works whether the long run lands on Saturday or Sunday.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROLE_LONG = "long"
ROLE_QUALITY = "quality"
ROLE_MEDIUM = "medium"
ROLE_EASY = "easy"
ROLE_OPTIONAL = "optional"

# Order in which sessions are sacrificed when a week has to shrink. The long
# run is protected hardest because it is the session marathon fitness rests on.
DROP_ORDER = [ROLE_OPTIONAL, ROLE_EASY, ROLE_MEDIUM, ROLE_QUALITY, ROLE_LONG]

QUALITY_KINDS = {"strides", "tempo", "marathon_pace", "intervals", "easy"}


@dataclass
class SessionTemplate:
    role: str
    miles: float
    zone: str = "easy"
    kind: str = "easy"
    reps: int | None = None
    rep_seconds: int | None = None
    tempo_minutes: int | None = None
    mp_miles: float | None = None
    mp_finish_miles: float | None = None
    is_race: bool = False
    optional: bool = False

    def scaled(self, factor: float) -> "SessionTemplate":
        """Proportionally resize a session, leaving races untouched."""
        if self.is_race:
            return self
        out = SessionTemplate(**self.__dict__)
        out.miles = round(self.miles * factor, 1)
        if self.tempo_minutes:
            out.tempo_minutes = max(8, int(round(self.tempo_minutes * factor)))
        if self.mp_miles:
            out.mp_miles = max(1.0, round(self.mp_miles * factor, 1))
        if self.mp_finish_miles:
            out.mp_finish_miles = max(1.0, round(self.mp_finish_miles * factor, 1))
        return out


@dataclass
class RaceInfo:
    name: str
    distance_miles: float
    distance_label: str = ""
    location: str = ""
    optional: bool = True
    start_time: str | None = None
    # True for a self-timed effort on a measured route instead of an organised race.
    is_trial: bool = False


@dataclass
class WeekTemplate:
    week: int
    total: float
    kind: str = "build"
    focus: str = ""
    note: str = ""
    sessions: list[SessionTemplate] = field(default_factory=list)
    race: RaceInfo | None = None

    @property
    def long_run(self) -> SessionTemplate | None:
        for s in self.sessions:
            if s.role == ROLE_LONG:
                return s
        return None

    @property
    def planned_miles(self) -> float:
        return round(sum(s.miles for s in self.sessions if not s.optional), 1)

    @property
    def is_recovery_week(self) -> bool:
        return self.kind in {"cutback", "taper"}


@dataclass
class PlanTemplate:
    name: str
    phase: str
    description: str
    weeks: list[WeekTemplate]

    def week(self, index: int) -> WeekTemplate:
        return self.weeks[index - 1]


def load_template(path: str | Path) -> PlanTemplate:
    with open(path) as f:
        raw = yaml.safe_load(f)

    weeks = [_parse_week(entry) for entry in raw["schedule"]]
    return PlanTemplate(
        name=raw["name"],
        phase=raw["phase"],
        description=raw.get("description", "").strip(),
        weeks=weeks,
    )


def load_all(cfg: dict) -> dict[str, PlanTemplate]:
    plans_dir = Path(cfg["plans_dir"])
    return {
        "base": load_template(plans_dir / "base_phase.yaml"),
        "marathon": load_template(plans_dir / "higdon_int1.yaml"),
    }


def _parse_week(entry: dict) -> WeekTemplate:
    sessions: list[SessionTemplate] = []
    raw_sessions = entry.get("sessions", {})

    for role in (ROLE_LONG, ROLE_QUALITY, ROLE_MEDIUM):
        if role in raw_sessions:
            sessions.append(_parse_session(role, raw_sessions[role]))

    for role in (ROLE_EASY, ROLE_OPTIONAL):
        for item in raw_sessions.get(role, []) or []:
            s = _parse_session(role, item)
            s.optional = role == ROLE_OPTIONAL
            sessions.append(s)

    race = None
    if entry.get("race"):
        race = RaceInfo(**entry["race"])

    return WeekTemplate(
        week=int(entry["week"]),
        total=float(entry["total"]),
        kind=entry.get("kind", "build"),
        focus=entry.get("focus", "").strip(),
        note=(entry.get("note") or "").strip(),
        sessions=sessions,
        race=race,
    )


def _parse_session(role: str, raw: dict) -> SessionTemplate:
    kind = raw.get("kind", "easy")
    if kind not in QUALITY_KINDS:
        raise ValueError(f"unknown session kind {kind!r} in role {role!r}")
    return SessionTemplate(
        role=role,
        miles=float(raw["miles"]),
        zone=raw.get("zone", "easy"),
        kind=kind,
        reps=raw.get("reps"),
        rep_seconds=raw.get("rep_seconds"),
        tempo_minutes=raw.get("tempo_minutes"),
        mp_miles=raw.get("mp_miles"),
        mp_finish_miles=raw.get("mp_finish_miles"),
        is_race=bool(raw.get("is_race", False)),
    )


# Percentage ramp rules break down at low mileage: 10% of a 9-mile week is
# under a mile, which is far more conservative than any coach would prescribe.
# Below LOW_VOLUME_MILES an absolute step is the meaningful limit.
LOW_VOLUME_MILES = 15.0
MIN_ABSOLUTE_STEP = 1.5

# Higdon-style plans are long-run heavy by design, and dropping to 4 run days
# pushes the share higher still. Flag only what is genuinely extreme.
LONG_RUN_SHARE_WARN = 0.50


def validate(template: PlanTemplate, max_increase: float = 0.10) -> list[str]:
    """Structural checks. Returns human-readable warnings rather than raising,
    because a few deliberate violations (the first week adding a run day, a
    long-run-heavy Higdon peak) are acceptable and worth surfacing, not fixing.
    """
    warnings: list[str] = []
    last_build_total = None

    for wk in template.weeks:
        if abs(wk.planned_miles - wk.total) > 0.35:
            warnings.append(
                f"{template.phase} week {wk.week}: sessions sum to "
                f"{wk.planned_miles} but total says {wk.total}"
            )

        long_run = wk.long_run
        if long_run and wk.total > 0 and not long_run.is_race:
            # Optional recovery runs count, since taking them is recommended.
            with_optional = wk.total + sum(s.miles for s in wk.sessions if s.optional)
            share = long_run.miles / with_optional
            if share > LONG_RUN_SHARE_WARN:
                warnings.append(
                    f"{template.phase} week {wk.week}: long run is "
                    f"{share:.0%} of weekly volume"
                )

        # Race weeks carry real load, so they anchor the ramp like a build week.
        # But a tune-up race week is deliberately reduced, and letting it lower
        # the anchor would make the following peak week look like a huge jump.
        if wk.kind in {"build", "peak", "race"}:
            if last_build_total:
                delta = wk.total - last_build_total
                increase = delta / last_build_total
                allowed = max(max_increase * last_build_total, 0.0)
                if last_build_total < LOW_VOLUME_MILES:
                    allowed = max(allowed, MIN_ABSOLUTE_STEP)
                if delta > allowed + 1e-9:
                    warnings.append(
                        f"{template.phase} week {wk.week}: +{increase:.1%} over "
                        f"last build week ({last_build_total} -> {wk.total})"
                    )
            if wk.kind == "race":
                last_build_total = max(last_build_total or 0.0, wk.total)
            else:
                last_build_total = wk.total

    return warnings
