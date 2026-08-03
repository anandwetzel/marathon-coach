"""Session prescriptions for climbing, strength, and stretching.

Kept separate from the plan generator so calendar events, the dashboard, and
the CLI all share one source of truth for what to actually do in the gym.

Working weights are seeded for a ~70 kg climber who is strong but not a
powerlifter. Adjust up or down so the last reps of each set are hard but clean;
peak/taper weeks automatically drop loads to two thirds.
"""

from __future__ import annotations

STRENGTH_FULL = "lower + upper"
STRENGTH_UPPER = "upper + core"
STRENGTH_LIGHT = "mobility + core"
STRENGTH_RACE_WEEK = "mobility only (race week)"

# Starting working weights (kg). "DB" = dumbbell per hand.
LOADS = {
    "squat": 80,          # back squat 3x5
    "trap_bar": 100,      # trap-bar deadlift alternative to squat
    "rdl": 60,            # Romanian deadlift 3x8
    "split_squat_db": 20, # Bulgarian split squat, DB each hand
    "calf": 40,           # standing calf raise
    "pullup_add": 0,      # bodyweight; add kg if weighted
    "pulldown": 55,       # lat pulldown alternative to pull-ups
    "db_row": 28,         # one-arm dumbbell row
    "db_press": 22,       # dumbbell overhead press, each hand
    "face_pull": 15,      # cable / band equivalent
}


def climb_detail(*, race_week: bool = False) -> str:
    if race_week:
        return (
            "Optional only if you are still home.\n"
            "- easy climbing only, stop well before your limit\n"
            "- no hard projects, no campus, no big falls\n"
            "- skip entirely if travelling - a tweaked finger is survivable, "
            "a dropped landing on race week is not"
        )
    return (
        "Climb at the gym after work.\n"
        "- warm up 10-15 min on easy grades / traverses\n"
        "- main session 45-75 min: technique and 1-2 harder efforts\n"
        "- stop when form breaks down - fatigue here is fine, junk volume is not\n"
        "- hangboard only if fresh and not the day before a quality run\n"
        "\n"
        "Stretch after (5 min):\n"
        + _stretch_block("climbing")
    )


def strength_detail(focus: str) -> str:
    """Full session text: lifts in `- name setsxreps load` form, then stretches."""
    maintenance = "maintenance" in focus
    w = _loads(maintenance)
    note = (
        "Peak / taper weeks: loads already cut to ~⅔. Keep form crisp."
        if maintenance else
        "Last 1-2 reps of each set should be hard but clean. Add 2.5 kg when "
        "all sets feel solid."
    )

    if focus.startswith(STRENGTH_RACE_WEEK):
        return (
            "Race week - mobility only, ~15 min. Skip if travelling.\n"
            "- glute bridge 2x10 bodyweight\n"
            "- dead bug 2x6/side bodyweight\n"
            "- world's greatest stretch 2x4/side bodyweight\n"
            "\n"
            "Stretch:\n"
            + _stretch_block("race")
        )

    if focus.startswith(STRENGTH_LIGHT):
        return (
            "Light day - activation and mobility, nothing heavy "
            "(usually the day after a long run).\n"
            "- single-leg glute bridge 3x10/leg bodyweight\n"
            "- side-lying clamshell 3x12/side light band\n"
            "- dead bug 3x8/side bodyweight\n"
            "- bird dog 3x8/side bodyweight\n"
            "- calf raise 2x12 bodyweight\n"
            "\n"
            "Stretch:\n"
            + _stretch_block("lower")
        )

    if focus.startswith(STRENGTH_UPPER):
        pull = _pullup_line(w)
        return (
            "Upper + core - no heavy legs (long run is tomorrow).\n"
            f"{note}\n"
            f"- {pull}\n"
            f"- one-arm dumbbell row 3x8/arm {_kg(w['db_row'])}\n"
            f"- dumbbell overhead press 3x8 {_kg(w['db_press'])}/hand\n"
            f"- face pull 3x12 {_kg(w['face_pull'])}\n"
            "- push-up 3x10 bodyweight\n"
            "- side plank 3x30s/side bodyweight\n"
            "- hollow hold 3x25s bodyweight\n"
            "\n"
            "Stretch:\n"
            + _stretch_block("upper")
        )

    pull = _pullup_line(w)
    return (
        "Lower + upper - single-leg work matters most; running is a single-leg sport.\n"
        f"{note}\n"
        f"- back squat 3x5 {_kg(w['squat'])}  "
        f"(or trap-bar deadlift 3x5 {_kg(w['trap_bar'])})\n"
        f"- Romanian deadlift 3x8 {_kg(w['rdl'])}\n"
        f"- Bulgarian split squat 3x8/leg {_kg(w['split_squat_db'])} DB/hand\n"
        f"- standing calf raise 3x12 {_kg(w['calf'])}\n"
        "- single-leg glute bridge 3x10/leg bodyweight\n"
        f"- {pull}\n"
        "- dead bug 3x8/side bodyweight\n"
        "\n"
        "Stretch:\n"
        + _stretch_block("full")
    )


def post_run_stretch(kind: str) -> str:
    """Short stretch block for the end of a run session."""
    if kind in {"long", "tempo", "marathon_pace", "intervals", "race"}:
        return "Stretch after (5-8 min):\n" + _stretch_block("lower")
    return "Stretch after (3-5 min):\n" + _stretch_block("easy")


def rest_detail() -> str:
    return (
        "No running. Walk if you want, sleep well.\n"
        "\n"
        "Optional stretch / mobility (10 min):\n"
        + _stretch_block("full")
    )


def _loads(maintenance: bool) -> dict[str, float]:
    if not maintenance:
        return {k: float(v) for k, v in LOADS.items()}
    out: dict[str, float] = {}
    for key, value in LOADS.items():
        if value == 0:
            out[key] = 0.0
            continue
        scaled = value * 2 / 3
        step = 2.5 if key in {"squat", "trap_bar", "rdl", "calf", "pulldown"} else 1.0
        out[key] = max(step, round(scaled / step) * step)
    return out


def _kg(value: float) -> str:
    if value == int(value):
        return f"{int(value)} kg"
    return f"{value:g} kg"


def _pullup_line(w: dict[str, float]) -> str:
    add = w["pullup_add"]
    if add > 0:
        pull = f"pull-up 3x6-8 bodyweight + {_kg(add)}"
    else:
        pull = "pull-up 3x6-8 bodyweight"
    return f"{pull}  (or lat pulldown 3x8 {_kg(w['pulldown'])})"


def _stretch_block(which: str) -> str:
    blocks = {
        "easy": (
            "- standing quad stretch 45s/side\n"
            "- standing hamstring stretch 45s/side\n"
            "- calf against wall 45s/side"
        ),
        "lower": (
            "- pigeon or figure-4 60s/side\n"
            "- couch stretch (hip flexor) 60s/side\n"
            "- standing hamstring stretch 45s/side\n"
            "- calf against wall 45s/side\n"
            "- seated spiral twist 30s/side"
        ),
        "upper": (
            "- doorway chest stretch 45s/side\n"
            "- cross-body shoulder stretch 45s/side\n"
            "- thread-the-needle 45s/side\n"
            "- neck side bend 30s/side\n"
            "- child's pose 60s"
        ),
        "climbing": (
            "- forearm / wrist flexor stretch 45s/side\n"
            "- doorway chest stretch 45s/side\n"
            "- cross-body shoulder stretch 45s/side\n"
            "- hip flexor couch stretch 45s/side"
        ),
        "race": (
            "- gentle pigeon 45s/side\n"
            "- standing hamstring 30s/side\n"
            "- calf against wall 30s/side\n"
            "- child's pose 60s"
        ),
        "full": (
            "- pigeon or figure-4 60s/side\n"
            "- couch stretch (hip flexor) 60s/side\n"
            "- standing hamstring stretch 45s/side\n"
            "- calf against wall 45s/side\n"
            "- doorway chest stretch 45s/side\n"
            "- child's pose 60s"
        ),
    }
    return blocks[which]
