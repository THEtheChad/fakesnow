"""Find the earlier cases that change a case's outcome (its polluters), like RSpec's --bisect.

The victim runs alone, then after the cases that ran before it in some order; if its outcome changes, delta
debugging (Zeller and Hildebrandt's ddmin) narrows those cases down to a smallest set that still changes it. Every
trial runs on a fresh target, and "changes" uses the case's own comparison (probes.compare.outcome_changes), so a
case that compares types only isn't changed by different values.

Each trial runs a few hundred cases at most: fakesnow only, never Snowflake.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from probes.compare import outcome_changes
from probes.model import Case
from probes.outcome import Outcome
from probes.runner import run_target
from probes.targets import SNOWFLAKE, Names, TargetError


@dataclass(frozen=True)
class Search:
    alone: Outcome
    after_all: Outcome
    """The victim's outcome after every case before it."""
    changes: list[str]
    """How after_all differs from alone; empty when nothing before the victim changes it."""
    polluters: list[Case]
    """A smallest set of the cases before the victim, in their order, that changes its outcome."""
    after_polluters: list[str]
    """How the victim's outcome after just the polluters differs from alone."""
    trials: int


Log = Callable[[str], None]
T = TypeVar("T")


def find_polluters(
    victim: Case, before: Sequence[Case], target: str, database: str, *, cleanup: bool = True, log: Log | None = None
) -> Search:
    """Search the cases before the victim for the ones that change its outcome on a fakesnow target."""
    if target == SNOWFLAKE:
        raise TargetError("polluter search runs the cases many times: use a fakesnow target, not snowflake")
    trials: dict[tuple[str, ...], Outcome] = {}

    def outcome_after(cases: Sequence[Case]) -> Outcome:
        key = tuple(c.id for c in cases)
        if key not in trials:
            run = run_target(target, [*cases, victim], Names.new(database), cleanup=cleanup)
            trials[key] = run.outcomes[victim.id]
            if log:
                changed = "changed" if outcome_changes(victim, alone, trials[key]) else "unchanged"
                log(f"  trial {len(trials)}: after {len(cases)} cases: {changed}")
        return trials[key]

    alone = run_target(target, [victim], Names.new(database), cleanup=cleanup).outcomes[victim.id]
    if unstable := outcome_changes(victim, alone, outcome_after([])):
        raise TargetError(f"{victim.id}'s outcome changes between runs on its own: {unstable[0]}")
    after_all = outcome_after(before)
    changes = outcome_changes(victim, alone, after_all)
    if not changes:
        return Search(alone, after_all, [], [], [], len(trials))
    polluters = ddmin(list(before), lambda cases: bool(outcome_changes(victim, alone, outcome_after(cases))))
    after_polluters = outcome_changes(victim, alone, outcome_after(polluters))
    return Search(alone, after_all, changes, polluters, after_polluters, len(trials))


def ddmin(items: list[T], fails: Callable[[list[T]], bool]) -> list[T]:
    """A 1-minimal subset of items, in their order, for which fails is true, given that it is for all of them.

    Zeller and Hildebrandt, "Simplifying and Isolating Failure-Inducing Input" (2002): try each of n chunks, then
    each complement, and split finer when neither fails.
    """
    n = 2
    while len(items) >= 2:
        size = len(items) / n
        chunks = [items[round(i * size) : round((i + 1) * size)] for i in range(n)]
        if found := next((c for c in chunks if fails(c)), None):
            items, n = found, 2
            continue
        complements = [[x for j, c in enumerate(chunks) if j != i for x in c] for i in range(n)]
        if n > 2 and (found := next((c for c in complements if fails(c)), None)):
            items, n = found, max(n - 1, 2)
            continue
        if n >= len(items):
            break
        n = min(n * 2, len(items))
    return items
