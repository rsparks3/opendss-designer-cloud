"""Plans: the limits a caller gets, and how much engine time they may use.

A plan is data, not code, so the numbers can be tuned from the ledger without
a release. ``plans.json`` in the repository root is the seed; ``GATEWAY_PLANS``
points at an edited copy. Only ``guest`` exists until accounts arrive.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Plan:
    id: str
    #: Shown in the banner as "<name> plan." and in limit messages as
    #: "the <name> plan".
    name: str
    #: Lower number dispatches first.
    priority: int
    #: Keys mirror the worker's limits header (maxNodes, maxTimeseriesCost, ...).
    limits: dict[str, float] = field(default_factory=dict)
    #: Engine-seconds allowed per period; None is unmetered.
    budget_seconds: float | None = None
    budget_period: str = "day"  # "day" | "month"
    #: Runs one caller may have in flight at once.
    concurrency: int = 1
    #: Which scheduler pool this plan draws from ("guest" is capped).
    pool: str = "guest"
    #: Banner text. Placeholders: {used}, {budget}, {period}.
    message: str = ""
    links: list[dict[str, str]] = field(default_factory=list)

    def period_key(self, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        return now.strftime("%Y-%m") if self.budget_period == "month" else now.strftime("%Y-%m-%d")

    def period_phrase(self) -> str:
        return "this month" if self.budget_period == "month" else "today"

    def describe(self, used_seconds: float) -> dict:
        """The ``plan`` block for the limits header."""
        message = self.message
        if self.budget_seconds:
            message = message.format(
                used=_minutes(used_seconds), budget=_minutes(self.budget_seconds),
                period=self.period_phrase())
        return {"name": self.name, "message": message or None, "links": self.links}

    def exhausted_message(self) -> str:
        return (f"You have used {_minutes(self.budget_seconds or 0)} of solver time "
                f"{self.period_phrase()}, which is all the {self.name} plan allows. "
                f"It resets at midnight UTC{' on the 1st' if self.budget_period == 'month' else ''}. "
                "Run OpenDSS Designer locally (pip install opendss-designer) for unlimited runs.")


def _minutes(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"


#: Seed plans; ``plans.json`` in the repository root is the same data and
#: what a deployment actually loads. Guest limits are deliberately tight and
#: the free plan is what the sign-in prompt promises. A solve takes a fraction
#: of a second and a yearly run a few seconds, so five minutes of guest time
#: is an afternoon of honest use and a short leash for a loop in a tab.
DEFAULT_PLANS: dict[str, Plan] = {
    "guest": Plan(
        id="guest", name="Guest", priority=10,
        limits={"maxNodes": 500, "maxEdges": 1000, "maxTimeseriesCost": 250_000,
                "engineResultTimeoutS": 30, "timeseriesTimeoutS": 30},
        budget_seconds=5 * 60, budget_period="day", concurrency=1, pool="guest",
        message="No account needed. {used} of {budget} of solver time used {period}. "
                "Sign in (free) for bigger circuits and longer runs.",
        links=[{"label": "Sign in", "url": "/auth/signin"}],
    ),
    "free": Plan(
        id="free", name="Free", priority=5,
        limits={"maxNodes": 1200, "maxEdges": 2400, "maxTimeseriesCost": 1_000_000,
                "engineResultTimeoutS": 90, "timeseriesTimeoutS": 90},
        budget_seconds=20 * 60, budget_period="month", concurrency=1, pool="member",
        message="{used} of {budget} of solver time used {period}.",
        links=[{"label": "Account", "url": "/account"}],
    ),
}


def load_plans(path: Path | None) -> dict[str, Plan]:
    if path is None:
        return dict(DEFAULT_PLANS)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    plans: dict[str, Plan] = {}
    for pid, spec in raw.items():
        plans[pid] = Plan(id=pid, **spec)
    for required in ("guest", "free"):
        if required not in plans:
            raise ValueError(f"plans file must define a '{required}' plan")
    return plans
