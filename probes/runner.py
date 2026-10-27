"""Run cases against targets and compare each target with the reference."""

from __future__ import annotations

import re
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from snowflake.connector.cursor import SnowflakeCursor

from probes.compare import Verdict, classify
from probes.context import ProbeContext
from probes.model import Case
from probes.outcome import ErrorInfo, Outcome, Scrub, Step, run_statement, scrub_value, scrubber
from probes.targets import SNOWFLAKE, Names, Session, SnowflakeConfig, open_target

_OPENS_TRANSACTION = re.compile(r"\s*(begin|start\s+transaction)\b", re.IGNORECASE)


@dataclass
class TargetRun:
    """Every case's outcome on one target."""

    target: str
    info: dict[str, str]
    outcomes: dict[str, Outcome] = field(default_factory=dict[str, Outcome])
    fingerprints: dict[str, str] = field(default_factory=dict[str, str])
    recorded: bool = False
    """Loaded from an earlier run's report rather than run now."""


@dataclass
class Report:
    cases: list[Case]
    reference: TargetRun
    candidates: list[TargetRun]
    verdicts: dict[str, dict[str, Verdict]]
    """case id -> target -> verdict"""
    names: Names

    @property
    def with_expected(self) -> bool:
        return self.reference.target == SNOWFLAKE

    @property
    def fails(self) -> bool:
        return any(v.status.fails for by_target in self.verdicts.values() for v in by_target.values())


Progress = Callable[[str, Case], None]


def run_case(case: Case, session: Session) -> Outcome:
    scrub = scrubber(session.names.placeholders)
    cur = session.conn.cursor()
    try:
        outcome = _run(case, session, cur, scrub)
    except Exception:
        outcome = Outcome(crash=traceback.format_exc(limit=5))
    teardown = [run_statement(cur, session.names.render(t), t, scrub) for t in case.teardown]
    notes = [f"teardown failed: {step.sql}: {step.error}" for step in teardown if step.error]
    try:
        if any(_OPENS_TRANSACTION.match(sql) for sql in [*case.setup, *case.statements]):
            # an explicit transaction the case left open (or aborted) would carry over into the next case
            session.conn.rollback()
        session.restore()
    except Exception as e:
        notes.append(f"couldn't reset the session: {e}")
    return replace(outcome, notes=tuple(notes)) if notes else outcome


def _run(case: Case, session: Session, cur: SnowflakeCursor, scrub: Scrub) -> Outcome:
    for template in case.setup:
        step = run_statement(cur, session.names.render(template), template, scrub)
        if step.error:
            return Outcome(setup_error=step)
    if case.run:
        return _run_python(case, session, scrub)
    steps: list[Step] = []
    for template in case.statements:
        step = run_statement(cur, session.names.render(template), template, scrub)
        steps.append(step)
        if step.error:
            break
    return Outcome(steps=tuple(steps))


def _run_python(case: Case, session: Session, scrub: Scrub) -> Outcome:
    assert case.run
    ctx = ProbeContext(session, scrub)
    try:
        value = case.run(session.conn, ctx)
    except Exception as e:
        return Outcome(error=ErrorInfo.from_exception(e))
    finally:
        ctx.close()
        # a probe may leave a transaction open or autocommit off
        session.conn.rollback()
        session.conn.autocommit(True)
    return Outcome(value=scrub_value(value, scrub))


def run_target(
    target: str,
    cases: Sequence[Case],
    names: Names,
    snowflake_config: SnowflakeConfig | None = None,
    progress: Progress | None = None,
) -> TargetRun:
    with open_target(target, names, snowflake_config) as session:
        run = TargetRun(target, session.info)
        for case in cases:
            if progress:
                progress(target, case)
            run.outcomes[case.id] = run_case(case, session)
            run.fingerprints[case.id] = case.fingerprint()
        return run


def missing_outcome(case: Case, reference: TargetRun) -> Outcome | None:
    """For a recorded reference: a crash outcome when it has nothing for this case as it's now written."""
    if case.id not in reference.outcomes:
        return Outcome(crash=f"not in the recorded {reference.target} run: rerun the case against {reference.target}")
    if reference.fingerprints.get(case.id) != case.fingerprint():
        return Outcome(crash=f"changed since the recorded {reference.target} run: rerun it against {reference.target}")
    return None


def compare_runs(cases: Sequence[Case], reference: TargetRun, candidates: Sequence[TargetRun], names: Names) -> Report:
    with_expected = reference.target == SNOWFLAKE
    verdicts: dict[str, dict[str, Verdict]] = {}
    for case in cases:
        ref_outcome = (reference.recorded and missing_outcome(case, reference)) or reference.outcomes[case.id]
        verdicts[case.id] = {
            c.target: classify(case, ref_outcome, c.outcomes[case.id], c.target, with_expected=with_expected)
            for c in candidates
        }
    return Report(list(cases), reference, list(candidates), verdicts, names)
