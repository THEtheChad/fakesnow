"""Compare a target's outcome with the reference's, and classify the result.

Normalisations, applied only when a case's Compare.normalise is on (the default). Each exists because the two
sides return the same thing in a different Python form; none hides a difference in behaviour:

1. Timezone-aware datetimes compare their instant AND UTC offset, independent of the tzinfo class.
   A naive datetime never equals an aware one. The run sets TIMEZONE to UTC on Snowflake.
2. The text of VARIANT, OBJECT and ARRAY columns (by Snowflake's type code) is compared as JSON tokens, so
   whitespace doesn't count: Snowflake indents it, fakesnow doesn't (upstream normalises the same way in its
   tests with tests.utils.indent). Key order, number spelling ("1.0" vs "1") and string contents still
   count, and the type codes are compared separately. Case semis.formatting keeps the raw text visible.

Python types are otherwise compared as returned: 1, 1.0 and Decimal('1') are three different results here,
because they behave differently in the caller's code (#199). Floats are compared by repr.

Run-specific names (the run's database and schema) are replaced by {database} and {schema} when an outcome is
captured, see probes.outcome.scrubber, so outcomes from different runs compare.
"""

from __future__ import annotations

import datetime
import enum
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from probes.model import Case, Compare, Expected
from probes.outcome import Column, ErrorInfo, Outcome, ResultSet, Step, encode

SEMI_STRUCTURED = frozenset({5, 9, 10})  # VARIANT, OBJECT, ARRAY


class Status(enum.Enum):
    MATCH = "match"
    UNEXPECTED = "unexpected difference"
    EXPECTED = "expected difference"
    NOW_MATCHES = "expected difference now matches"
    ERROR = "error"

    @property
    def fails(self) -> bool:
        return self in (Status.UNEXPECTED, Status.ERROR)


@dataclass(frozen=True)
class Verdict:
    status: Status
    differences: tuple[str, ...] = ()
    expected: Expected | None = None
    fingerprint: str | None = None


def classify(case: Case, reference: Outcome, candidate: Outcome, target: str, *, with_expected: bool) -> Verdict:
    """Compare a candidate's outcome with the reference's and decide the case's status for that target.

    with_expected: whether the case's expected differences apply, ie: the reference is Snowflake.
    """
    if broken := reference_problem(reference):
        return Verdict(Status.ERROR, (f"reference: {broken}",))
    if with_expected and reference.leaks:
        # What Snowflake leaks is the case's to clean up: fakesnow's leaks are compared like any other outcome.
        leaks = "; ".join(map(str, reference.leaks))
        if any(leak.not_the_runs for leak in reference.leaks):
            # a schema the case may never have touched: the run's database isn't the run's alone
            hint = (
                "another session using the database during the run could have done that (a live run needs the "
                "probe database to itself); if the case did, undo it in its teardown"
            )
        else:
            hint = "undo it in the case's teardown"
        return Verdict(Status.ERROR, (f"reference: leaked {leaks}: {hint}",))
    if candidate.crash:
        return Verdict(Status.ERROR, (f"harness: {candidate.crash}",))
    if candidate.leaks and reference.leaks is None:
        # Whether the reference leaked the same is unknown: not a difference to classify, let alone pin.
        leaked = f"{target} leaked {'; '.join(map(str, candidate.leaks))}"
        return Verdict(
            Status.ERROR, (f"reference: leaks not checked (recorded before the leak check), rerun it: {leaked}",)
        )
    differences = tuple(diff(case.compare, reference, candidate))
    expected = case.expected_for(target) if with_expected else None
    if differences:
        fingerprint = comparison_fingerprint(case, reference, candidate)
        pinned = expected and case.expected_outcomes.get(target) == fingerprint
        if expected and not pinned:
            differences += ("expected outcome is unpinned or changed; review both outcomes before updating the pin",)
        return Verdict(Status.EXPECTED if pinned else Status.UNEXPECTED, differences, expected, fingerprint)
    return Verdict(Status.NOW_MATCHES if expected else Status.MATCH, (), expected)


def reference_problem(reference: Outcome) -> str | None:
    """Why the reference outcome can't be compared against, if it can't: the case or the harness is broken."""
    if reference.crash:
        return reference.crash
    if reference.setup_error:
        return f"setup failed: {reference.setup_error.sql}: {reference.setup_error.error}"
    for step in reference.steps:
        if step.error and not step.error.database_error:
            return f"statement raised {step.error}"
    if reference.error and not reference.error.database_error:
        return f"probe raised {reference.error}"
    return returned_problem(reference.value)


def returned_problem(value: Any) -> str | None:
    """Captured Python errors on Snowflake indicate a broken probe even when returned via ctx.attempt."""
    if isinstance(value, ErrorInfo) and not value.database_error:
        return f"probe returned {value}"
    values = value.values() if isinstance(value, dict) else value if isinstance(value, list | tuple) else ()
    return next((problem for item in values if (problem := returned_problem(item))), None)


def diff(compare: Compare, reference: Outcome, candidate: Outcome) -> list[str]:
    """Differences between two outcomes, as one line each; empty when they match."""
    return diff_results_of(compare, reference, candidate) + diff_leaks(reference, candidate)


def diff_leaks(reference: Outcome, candidate: Outcome) -> list[str]:
    """Nothing when either side wasn't checked (an earlier run's): classify makes leaks against that an error."""
    if reference.leaks is None or candidate.leaks is None or compared_leaks(reference) == compared_leaks(candidate):
        return []
    leaked = "; ".join(map(str, candidate.leaks))
    if not reference.leaks:
        return [f"leaked {leaked}"]
    return [f"leaked {'; '.join(map(str, reference.leaks))} vs {leaked or 'nothing'}"]


def compared_leaks(outcome: Outcome) -> tuple[str, ...]:
    """An outcome's leaks as compared and pinned: whether the run undid or kept them isn't the target's behavior."""
    return tuple(leak.compared for leak in outcome.leaks or ())


def diff_results_of(compare: Compare, reference: Outcome, candidate: Outcome) -> list[str]:
    if reference.setup_error and candidate.setup_error:
        r, c = reference.setup_error, candidate.setup_error
        assert r.error and c.error
        return ([f"setup: failed at {r.sql} vs {c.sql}"] if r.sql != c.sql else []) + diff_errors(
            "setup", compare, r.error, c.error
        )
    if reference.setup_error or candidate.setup_error:
        step = candidate.setup_error or reference.setup_error
        assert step
        return [f"setup failed: {step.sql}: {step.error}" + ("" if candidate.setup_error else " vs succeeded")]
    out: list[str] = []
    if reference.error or candidate.error:
        out += diff_errors("raised", compare, reference.error, candidate.error)
    elif comparable(reference.value, compare) != comparable(candidate.value, compare):
        out.append(f"returned {show(reference.value)} vs {show(candidate.value)}")
    for i, (r, c) in enumerate(zip_longest(reference.steps, candidate.steps), start=1):
        if r is None or c is None:
            out.append(f"step {i}: {'not run' if r is None else 'ran'} vs {'not run' if c is None else 'ran'}")
            break
        out += diff_step(i, compare, r, c)
    return out


def diff_step(i: int, compare: Compare, reference: Step, candidate: Step) -> list[str]:
    where = f"step {i}"
    if reference.error or candidate.error:
        return diff_errors(where, compare, reference.error, candidate.error)
    assert reference.result and candidate.result
    return diff_results(where, compare, reference.result, candidate.result)


def diff_errors(where: str, compare: Compare, reference: ErrorInfo | None, candidate: ErrorInfo | None) -> list[str]:
    if reference is None or candidate is None:
        return [f"{where}: {_error_or_ok(reference)} vs {_error_or_ok(candidate)}"]
    out: list[str] = []
    if compare.error_codes and error_key(reference) != error_key(candidate):
        if reference.kind != candidate.kind or reference.database_error != candidate.database_error:
            out.append(f"{where}: exception {reference.kind} vs {candidate.kind}")
        else:
            out.append(f"{where}: error {_codes(reference)} vs {_codes(candidate)}")
    if compare.error_message and reference.message != candidate.message:
        out.append(f"{where}: message {reference.message!r} vs {candidate.message!r}")
    return out


def error_key(error: ErrorInfo) -> tuple[Any, ...]:
    # Exception classes affect callers' except clauses, even when numeric codes happen to match.
    return (error.errno, error.sqlstate, error.kind, error.database_error)


def outcome_changes(case: Case, before: Outcome, after: Outcome) -> list[str]:
    """How a case's outcome on one target changed between two runs of it, by the case's own comparison."""
    if before.crash or after.crash:
        return [] if before.crash == after.crash else [f"harness: {before.crash} vs {after.crash}"]
    return diff(case.compare, before, after)


def comparison_fingerprint(case: Case, reference: Outcome, candidate: Outcome) -> str:
    """Pin all compared data on BOTH sides, without truncating rows or depending on repr display limits."""

    def outcome_key(outcome: Outcome) -> Any:
        def step_key(step: Step) -> Any:
            return comparable(step.error if step.error else step.result, case.compare)

        key = (
            step_key(outcome.setup_error) if outcome.setup_error else None,
            tuple(step_key(step) for step in outcome.steps),
            comparable(outcome.value, case.compare),
            comparable(outcome.error, case.compare),
        )
        # only when there are leaks, so that pins from before the leak check still apply
        return (*key, ("leaks", compared_leaks(outcome))) if outcome.leaks else key

    body = (case.fingerprint(), repr(case.compare), outcome_key(reference), outcome_key(candidate))
    return hashlib.sha256(json.dumps(encode(body), sort_keys=True).encode()).hexdigest()


def _error_or_ok(error: ErrorInfo | None) -> str:
    return "succeeded" if error is None else f"error {_codes(error)}"


def _codes(error: ErrorInfo) -> str:
    return f"{error.errno} ({error.sqlstate})" if error.errno is not None else error.kind


def diff_results(where: str, compare: Compare, reference: ResultSet, candidate: ResultSet) -> list[str]:
    out: list[str] = []
    if len(reference.columns) != len(candidate.columns):
        out.append(f"{where}: {len(reference.columns)} columns vs {len(candidate.columns)}")
    if compare.types and _types(reference.columns) != _types(candidate.columns):
        out.append(f"{where}: types {show_types(reference.columns)} vs {show_types(candidate.columns)}")
    if compare.names and [c.name for c in reference.columns] != [c.name for c in candidate.columns]:
        out.append(f"{where}: names {[c.name for c in reference.columns]} vs {[c.name for c in candidate.columns]}")
    if compare.scale and _scales(reference.columns) != _scales(candidate.columns):
        out.append(f"{where}: scale {_scales(reference.columns)} vs {_scales(candidate.columns)}")
    if compare.precision and _precisions(reference.columns) != _precisions(candidate.columns):
        out.append(f"{where}: precision/length {_precisions(reference.columns)} vs {_precisions(candidate.columns)}")
    if compare.rows:
        json_columns = [c.type_code in SEMI_STRUCTURED for c in reference.columns]
        if comparable_rows(reference.rows, json_columns, compare) != comparable_rows(
            candidate.rows, json_columns, compare
        ):
            out.append(f"{where}: rows {show(list(reference.rows))} vs {show(list(candidate.rows))}")
    return out


def _types(columns: Sequence[Column]) -> list[int]:
    return [c.type_code for c in columns]


def _scales(columns: Sequence[Column]) -> list[int | None]:
    return [c.scale for c in columns]


def _precisions(columns: Sequence[Column]) -> list[int | None]:
    return [c.precision if c.precision is not None else c.internal_size for c in columns]


def show_types(columns: Sequence[Column]) -> str:
    return "[" + ", ".join(f"{c.type_name}({c.type_code})" for c in columns) + "]"


def show(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- normalisation


def comparable_rows(rows: Sequence[Sequence[Any]], json_columns: Sequence[bool], compare: Compare) -> Any:
    return tuple(
        tuple(comparable(v, compare, json_text=i < len(json_columns) and json_columns[i]) for i, v in enumerate(row))
        for row in rows
    )


def comparable(value: Any, compare: Compare, *, json_text: bool = False) -> Any:
    """A key for a value such that two keys are equal iff the values count as the same result."""
    normalise = compare.normalise
    match value:
        case str() if json_text and normalise:
            try:
                return ("json", _json_tokens(value))
            except ValueError:
                return ("str", value)
        case float():
            return ("float", repr(value))
        case datetime.datetime() if value.tzinfo is not None:
            if normalise:
                return ("instant", value.astimezone(datetime.UTC).replace(tzinfo=None), value.utcoffset())
            return ("aware", value.replace(tzinfo=None), value.utcoffset())
        case list() | tuple():
            return (type(value).__name__, tuple(comparable(v, compare) for v in value))
        case dict():
            items = [(comparable(k, compare), comparable(v, compare)) for k, v in value.items()]
            return ("dict", tuple(items))
        case ErrorInfo():
            return (
                "error",
                error_key(value) if compare.error_codes else None,
                value.message if compare.error_message else None,
            )
        case ResultSet():
            json_columns = [c.type_code in SEMI_STRUCTURED for c in value.columns]
            return (
                "result",
                len(value.columns),
                tuple(_scales(value.columns)) if compare.scale else None,
                tuple(_precisions(value.columns)) if compare.precision else None,
                tuple(_types(value.columns)) if compare.types else None,
                tuple(c.name for c in value.columns) if compare.names else None,
                comparable_rows(value.rows, json_columns, compare) if compare.rows else None,
            )
        case _:
            return (type(value).__name__, value)


def _json_tokens(text: str) -> Any:
    """Parse JSON keeping what's meaningful in its text: key order and how each number is written."""

    json.loads(text)  # Reject non-JSON text before tokenising.
    # Remove whitespace outside quoted strings only: escape spelling is observable too.
    return re.sub(r'"(?:\\.|[^"\\])*"|\s+', lambda m: m[0] if m[0].startswith('"') else "", text)
