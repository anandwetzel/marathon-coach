"""Gear recommendations, gated on the milestone in the plan that creates the need.

A flat shopping list is useless: most of this should not be bought now. Each
item is tied to the week that actually triggers it - the date the clocks change
and every run turns dark, the week long runs pass 90 minutes and fuel starts
mattering, the week cumulative mileage retires the first pair of shoes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .plan.daylight import first_dark_date
from .plan.generator import Plan

ESSENTIAL = "essential"
RECOMMENDED = "recommended"
OPTIONAL = "optional"

PRIORITY_ORDER = {ESSENTIAL: 0, RECOMMENDED: 1, OPTIONAL: 2}

# Long runs past this duration need practised in-run fuelling.
FUEL_MINUTES = 90
# Beyond this distance, carrying your own water stops being optional.
CARRY_MILES = 13.0
# Buy ahead of the trigger date by this much, so nothing arrives late.
LEAD_TIME = timedelta(days=14)


@dataclass
class GearItem:
    name: str
    category: str
    why: str
    price_low: float
    price_high: float
    priority: str
    retailers: tuple[str, ...] = ()
    trigger: str = ""
    due: date | None = None
    week_index: int | None = None

    @property
    def price_label(self) -> str:
        if self.price_low == self.price_high:
            return f"EUR {self.price_low:.0f}"
        return f"EUR {self.price_low:.0f}-{self.price_high:.0f}"

    @property
    def mid_price(self) -> float:
        return (self.price_low + self.price_high) / 2


def recommend(cfg: dict, plan: Plan, as_of: date | None = None) -> list[GearItem]:
    as_of = as_of or date.today()
    start = plan.weeks[0].start_date
    race_day = date.fromisoformat(plan.race_date)

    dark_day = first_dark_date(cfg, start, race_day)
    fuel_week = _first_week_where(plan, lambda s: s.duration_minutes >= FUEL_MINUTES)
    carry_week = _first_week_where(plan, lambda s: s.miles >= CARRY_MILES)
    shoe_week = _shoe_replacement_week(cfg, plan)
    winter_day = dark_day - timedelta(days=14) if dark_day else None

    items: list[GearItem] = [
        GearItem(
            name="Running shoes, properly fitted",
            category="Shoes",
            why=("Everything else is optional next to this. Get gait-analysed "
                 "rather than buying online - you are about to put 700+ miles "
                 "through one pair of shoes, most of it on wet pavement."),
            price_low=120, price_high=170, priority=ESSENTIAL,
            retailers=("Run2Day Amsterdam", "Runnersworld Amsterdam",
                       "Decathlon Amstelveen"),
            trigger="Before week 1", due=start, week_index=1,
        ),
        GearItem(
            name="Technical socks (4 pairs)",
            category="Clothing",
            why=("Cotton socks are the most common cause of blisters and the "
                 "cheapest problem to eliminate."),
            price_low=40, price_high=60, priority=ESSENTIAL,
            retailers=("Decathlon", "Run2Day"),
            trigger="Before week 1", due=start, week_index=1,
        ),
        GearItem(
            name="Foam roller and massage ball",
            category="Recovery",
            why=("You have flagged injury avoidance as the priority and you are "
                 "quadrupling your volume. Ten minutes on calves and quads after "
                 "the long run is the cheapest insurance available."),
            price_low=30, price_high=55, priority=ESSENTIAL,
            retailers=("Decathlon", "Bol.com"),
            trigger="Before week 1", due=start, week_index=1,
        ),
        GearItem(
            name="Anti-chafe balm and nipple guards",
            category="Comfort",
            why=("Irrelevant at 5 miles, unavoidable past 90 minutes in the wet. "
                 "Buy before you need it, not after."),
            price_low=12, price_high=22, priority=RECOMMENDED,
            retailers=("Run2Day", "Bol.com"),
            trigger=_week_trigger(fuel_week, "long runs pass 90 minutes"),
            due=_due_for(plan, fuel_week), week_index=fuel_week,
        ),
    ]

    if dark_day:
        items.extend([
            GearItem(
                name="Headlamp, 200+ lumens with rear red light",
                category="Visibility",
                why=(f"From {dark_day:%d %B} every weekday run after work "
                     f"finishes in the dark, and the Amsterdamse Bos has no "
                     f"lighting at all. This is a safety item, not a comfort "
                     f"one. Petzl Actik Core or Bindi are the usual picks."),
                price_low=45, price_high=75, priority=ESSENTIAL,
                retailers=("Bever", "Decathlon", "Run2Day"),
                trigger=f"Dark runs begin {dark_day:%d %b %Y}",
                due=dark_day - LEAD_TIME,
            ),
            GearItem(
                name="Reflective vest or high-vis jacket",
                category="Visibility",
                why=("Dutch cycle paths in the dark are busy and fast. Being "
                     "seen from behind matters more than seeing ahead."),
                price_low=18, price_high=40, priority=ESSENTIAL,
                retailers=("Decathlon", "Bever"),
                trigger=f"Dark runs begin {dark_day:%d %b %Y}",
                due=dark_day - LEAD_TIME,
            ),
        ])

    if winter_day:
        items.extend([
            GearItem(
                name="Waterproof, breathable running jacket",
                category="Clothing",
                why=("The marathon block runs December to April in the "
                     "Netherlands. Wind matters more than rain here - look for "
                     "something genuinely windproof that still vents."),
                price_low=80, price_high=160, priority=ESSENTIAL,
                retailers=("Bever", "Run2Day", "Decathlon"),
                trigger="Before the wet season", due=winter_day,
            ),
            GearItem(
                name="Thermal tights and two long-sleeve base layers",
                category="Clothing",
                why=("Most winter running here is 2-8 degrees and damp. Merino "
                     "or synthetic base layers, never cotton."),
                price_low=90, price_high=170, priority=ESSENTIAL,
                retailers=("Decathlon", "Run2Day", "Bever"),
                trigger="Before the wet season", due=winter_day,
            ),
            GearItem(
                name="Gloves and a buff or headband",
                category="Clothing",
                why=("Hands and ears are what actually make cold runs miserable. "
                     "Cheap, and they decide whether you go out at all."),
                price_low=25, price_high=50, priority=RECOMMENDED,
                retailers=("Decathlon", "Bever"),
                trigger="Before the wet season", due=winter_day,
            ),
        ])

    if fuel_week:
        items.append(GearItem(
            name="Energy gels, a box to test with",
            category="Fuelling",
            why=("From here every long run doubles as a fuelling rehearsal: one "
                 "gel roughly every 40 minutes. Try several brands now - "
                 "discovering your stomach hates one at mile 18 on the Gardesana "
                 "is a bad way to find out. Settle on a brand you can buy in the "
                 "Netherlands and carry your own to Italy; racing abroad is no "
                 "time to rely on whatever the course hands you."),
            price_low=35, price_high=60, priority=ESSENTIAL,
            retailers=("Run2Day", "Bol.com", "Decathlon"),
            trigger=_week_trigger(fuel_week, "long runs pass 90 minutes"),
            due=_due_for(plan, fuel_week), week_index=fuel_week,
        ))

    if carry_week:
        items.append(GearItem(
            name="Handheld bottle or running belt",
            category="Fuelling",
            why=("Past 13 miles you need to carry water and gels. The Bos has "
                 "drinking-water taps, but they are not on every route and not "
                 "all run in winter."),
            price_low=25, price_high=90, priority=RECOMMENDED,
            retailers=("Run2Day", "Decathlon", "Bever"),
            trigger=_week_trigger(carry_week, f"long runs pass {CARRY_MILES:g} miles"),
            due=_due_for(plan, carry_week), week_index=carry_week,
        ))

    if shoe_week:
        items.append(GearItem(
            name="Second pair of shoes",
            category="Shoes",
            why=(f"Your first pair reaches roughly "
                 f"{cfg['gear']['shoe_retire_miles']:g} miles around week "
                 f"{shoe_week}. Rotating two pairs lets the foam recover between "
                 f"runs and means a soaked pair never forces a missed session - "
                 f"which in a Dutch winter is the real reason to do it."),
            price_low=120, price_high=170, priority=ESSENTIAL,
            retailers=("Run2Day", "Runnersworld", "Hardloopaanbieding.nl"),
            trigger=_week_trigger(shoe_week, "first pair reaches its mileage"),
            due=_due_for(plan, shoe_week), week_index=shoe_week,
        ))

    taper_start = plan.weeks[-3].start_date if len(plan.weeks) >= 3 else None
    items.extend([
        GearItem(
            name="Race-day shoes (carbon plated)",
            category="Shoes",
            why=("Worth perhaps 2-3 minutes over the distance. Only buy these "
                 "if the projected finish is close to your target and you have "
                 "6+ weeks to run in them - never race in shoes you have not "
                 "trained in."),
            price_low=200, price_high=290, priority=OPTIONAL,
            retailers=("Run2Day", "Runnersworld"),
            trigger="6-8 weeks before race day, only if the target is close",
            due=race_day - timedelta(weeks=8),
        ),
        GearItem(
            name="Chest heart-rate strap",
            category="Tech",
            why=("Wrist heart rate on the Apple Watch drifts badly during "
                 "intervals and is useless while climbing with a tight grip. "
                 "Only needed if you want to train by heart rate rather than "
                 "pace and feel."),
            price_low=60, price_high=95, priority=OPTIONAL,
            retailers=("Bol.com", "Decathlon"),
            trigger="Any time", due=None,
        ),
        GearItem(
            name="Check your watch battery against 4 hours of GPS",
            category="Tech",
            why=("An older Apple Watch with GPS and the display on will not "
                 "always survive a 4-hour marathon. Test it on a long run in "
                 "the peak block - if it dies at 3 hours, low-power mode or an "
                 "alternative is a race-week problem you do not want."),
            price_low=0, price_high=0, priority=RECOMMENDED,
            retailers=(),
            trigger="Test during a peak-block long run",
            due=taper_start,
        ),
    ])

    items.sort(key=lambda i: (i.due or date.max, PRIORITY_ORDER[i.priority]))
    return items


def due_now(cfg: dict, plan: Plan, as_of: date | None = None,
            horizon_days: int = 21) -> list[GearItem]:
    as_of = as_of or date.today()
    cutoff = as_of + timedelta(days=horizon_days)
    return [i for i in recommend(cfg, plan, as_of)
            if i.due and i.due <= cutoff]


def budget_summary(items: list[GearItem], budget: float | None = None) -> dict:
    by_priority: dict[str, float] = {}
    for item in items:
        by_priority.setdefault(item.priority, 0.0)
        by_priority[item.priority] += item.mid_price

    essential = by_priority.get(ESSENTIAL, 0.0)
    recommended = by_priority.get(RECOMMENDED, 0.0)
    optional = by_priority.get(OPTIONAL, 0.0)
    summary = {
        "essential": round(essential),
        "recommended": round(recommended),
        "optional": round(optional),
        "total": round(essential + recommended + optional),
        "budget": budget,
    }
    if budget:
        summary["within_budget"] = essential + recommended <= budget
        summary["remaining"] = round(budget - essential - recommended)
    return summary


def _first_week_where(plan: Plan, predicate) -> int | None:
    for week in plan.weeks:
        for session in week.run_sessions:
            if not session.is_race and predicate(session):
                return week.index
    return None


def _shoe_replacement_week(cfg: dict, plan: Plan) -> int | None:
    """Week in which cumulative mileage retires the first pair."""
    limit = float(cfg["gear"]["shoe_retire_miles"])
    cumulative = 0.0
    for week in plan.weeks:
        cumulative += week.planned_miles
        if cumulative >= limit:
            return week.index
    return None


def _due_for(plan: Plan, week_index: int | None) -> date | None:
    if not week_index:
        return None
    for week in plan.weeks:
        if week.index == week_index:
            return week.start_date - LEAD_TIME
    return None


def _week_trigger(week_index: int | None, reason: str) -> str:
    if not week_index:
        return reason.capitalize()
    return f"Week {week_index}, when {reason}"
