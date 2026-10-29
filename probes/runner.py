"""Run cases against targets and compare each target with the reference."""

from __future__ import annotations

import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from snowflake.connector.cursor import SnowflakeCursor

from probes.compare import Verdict, classify
from probes.context import ProbeContext
from probes.isolation import LeakCheck
from probes.model import Case
from probes.outcome import ErrorInfo, Outcome, Scrub, Step, redact, run_statement, scrub_error, scrub_value, scrubber
from probes.targets import SNOWFLAKE, Names, Session, SnowflakeConfig, open_target


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


def run_case(case: Case, session: Session, number: int = 1, leak_check: LeakCheck | None = None) -> Outcome:
    """Run the run's number-th case in a schema of its own, then reset the session and check for leaks."""
    names = session.enter_case(number)
    scrub = scrubber(names.placeholders)
    cur = session.conn.cursor()
    try:
        try:
            outcome = _run(case, session, cur, scrub)
        except Exception:
            outcome = Outcome(crash=scrub(redact(traceback.format_exc(limit=5))))
    finally:
        # Teardown also runs on KeyboardInterrupt. Reset before teardown so an aborted transaction
        # cannot prevent dropping resources. A reset failure makes the run unusable, not a match.
        session.conn.rollback()
        session.conn.autocommit(True)
        teardown = [run_statement(cur, session.names.render(t), t, scrub) for t in case.teardown]
        cur.close()
        dropped = session.leave_case()
    notes = [f"teardown failed: {step.sql}: {step.error}" for step in teardown if step.error]
    notes += [scrub(dropped)] if dropped else []
    outcome = replace(outcome, notes=tuple(notes)) if notes else outcome
    return replace(outcome, leaks=leak_check.after(case, scrub)) if leak_check else outcome


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
        return Outcome(error=scrub_error(ErrorInfo.from_exception(e), scrub))
    finally:
        ctx.close()
    return Outcome(value=scrub_value(value, scrub))


def run_target(
    target: str,
    cases: Sequence[Case],
    names: Names,
    snowflake_config: SnowflakeConfig | None = None,
    progress: Progress | None = None,
    *,
    cleanup: bool = True,
) -> TargetRun:
    """Run the cases in the order given. cleanup=False leaves the leaks the leak check finds in place."""
    with open_target(target, names, snowflake_config) as session:
        run = TargetRun(target, session.info)
        leak_check = LeakCheck(session, cleanup=cleanup)
        for number, case in enumerate(cases, start=1):
            if progress:
                progress(target, case)
            run.outcomes[case.id] = run_case(case, session, number, leak_check)
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
