"""Compare a target's outcome with the reference's, and classify the result.

Normalisations, applied only when a case's Compare.normalise is on (the default). Each exists because the two
sides return the same thing in a different Python form; none hides a difference in behaviour:

1. Timezone-aware datetimes are compared as instants (converted to UTC). The run sets TIMEZONE to UTC on
   Snowflake, so this mostly guards against equal instants carried in different tzinfo classes; offsets are
   not compared, so a case about TIMESTAMP_TZ offsets should select them explicitly, e.g. with TO_VARCHAR.
   A naive datetime never equals an aware one.
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
import json
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from probes.model import Case, Compare, Expected
from probes.outcome import Column, ErrorInfo, Outcome, ResultSet, Step

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


def classify(case: Case, reference: Outcome, candidate: Outcome, target: str, *, with_expected: bool) -> Verdict:
    """Compare a candidate's outcome with the reference's and decide the case's status for that target.

    with_expected: whether the case's expected differences apply, ie: the reference is Snowflake.
    """
    if broken := reference_problem(reference):
        return Verdict(Status.ERROR, (f"reference: {broken}",))
    if candidate.crash:
        return Verdict(Status.ERROR, (f"harness: {candidate.crash}",))
    differences = tuple(diff(case.compare, reference, candidate))
    expected = case.expected_for(target) if with_expected else None
    if differences:
        return Verdict(Status.EXPECTED if expected else Status.UNEXPECTED, differences, expected)
    return Verdict(Status.NOW_MATCHES if expected else Status.MATCH, (), expected)


def reference_problem(reference: Outcome) -> str | None:
    """Why the reference outcome can't be compared against, if it can't: the case or the harness is broken."""
    if reference.crash:
        return reference.crash
    if reference.setup_error:
        return f"setup failed: {reference.setup_error.sql}: {reference.setup_error.error}"
    if reference.error and not reference.error.database_error:
        return f"probe raised {reference.error}"
    return None


def diff(compare: Compare, reference: Outcome, candidate: Outcome) -> list[str]:
    """Differences between two outcomes, as one line each; empty when they match."""
    if candidate.setup_error:
        step = candidate.setup_error
        return [f"setup failed: {step.sql}: {step.error}"]
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
    if compare.error_codes and (reference.errno, reference.sqlstate) != (candidate.errno, candidate.sqlstate):
        out.append(f"{where}: error {_codes(reference)} vs {_codes(candidate)}")
    if compare.error_message and reference.message != candidate.message:
        out.append(f"{where}: message {reference.message!r} vs {candidate.message!r}")
    return out


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
                return ("instant", value.astimezone(datetime.UTC).replace(tzinfo=None))
            return ("aware", value.replace(tzinfo=None), value.utcoffset())
        case list() | tuple():
            return (type(value).__name__, tuple(comparable(v, compare) for v in value))
        case dict():
            items = [(comparable(k, compare), comparable(v, compare)) for k, v in value.items()]
            return ("dict", tuple(sorted(items, key=repr)))
        case ErrorInfo():
            return (
                "error",
                (value.errno, value.sqlstate) if compare.error_codes else None,
                value.message if compare.error_message else None,
            )
        case ResultSet():
            json_columns = [c.type_code in SEMI_STRUCTURED for c in value.columns]
            return (
                "result",
                tuple(_types(value.columns)) if compare.types else None,
                tuple(c.name for c in value.columns) if compare.names else None,
                comparable_rows(value.rows, json_columns, compare) if compare.rows else None,
            )
        case _:
            return (type(value).__name__, value)


def _json_tokens(text: str) -> Any:
    """Parse JSON keeping what's meaningful in its text: key order and how each number is written."""

    def number(token: str) -> tuple[str, str]:
        return ("number", token)

    return json.loads(
        text,
        parse_int=number,
        parse_float=number,
        parse_constant=number,
        object_pairs_hook=lambda pairs: ("object", tuple(pairs)),
    )
