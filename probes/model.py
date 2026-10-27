"""Probe cases: what to run, what to compare, and which differences are expected."""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from snowflake.connector import SnowflakeConnection

    from probes.context import ProbeContext

ProbeFn = Callable[["SnowflakeConnection", "ProbeContext"], Any]
"""A Python probe: takes the target's connection and a context, returns a value to compare."""

_ID = re.compile(r"[a-z0-9][a-z0-9_.\-]*")
_REF = re.compile(r"#\d+|https?://\S+|unfiled")
UNFILED = "unfiled"
"""The reference for a difference confirmed against Snowflake that has no upstream issue yet."""


class CaseError(ValueError):
    """A case is malformed."""


@dataclass(frozen=True)
class Compare:
    """What to compare between Snowflake and fakesnow.

    rows: result rows, after the normalisations in probes.compare.
    types: each column's type code.
    names: each column's name. Off by default: fakesnow names unaliased expressions differently.
    scale: each column's scale: it decides whether a NUMBER comes back as int or Decimal.
    precision: each column's precision, and a text or binary column's length.
    error_codes: errno and sqlstate when a statement fails.
    error_message: the error message too. Off by default: messages are rarely word for word.
    normalise: apply the normalisations in probes.compare; off compares Python values as returned.
    """

    rows: bool = True
    types: bool = True
    names: bool = False
    scale: bool = False
    precision: bool = False
    error_codes: bool = True
    error_message: bool = False
    normalise: bool = True


@dataclass(frozen=True)
class Expected:
    """A difference from Snowflake that's known, with a reference to where it's tracked.

    targets: fakesnow targets the difference applies to; empty means all of them.
    """

    refs: Sequence[str]
    reason: str
    targets: Sequence[str] = ()

    def __post_init__(self) -> None:
        if not self.refs:
            raise CaseError(f"expected difference needs a reference (issue or PR): {self.reason!r}")
        if isinstance(self.refs, str):
            raise CaseError(f"refs must be a list of references, not a string: {self.refs!r}")
        for ref in self.refs:
            if not _REF.fullmatch(ref):
                raise CaseError(f"reference {ref!r} should be '#<number>', a URL or 'unfiled'")
        if not self.reason.strip():
            raise CaseError("expected difference needs a reason")


@dataclass(frozen=True)
class Case:
    """One probe.

    id: unique, lower case, e.g. "semis.array-construct-aliased".
    description: one line on what's being checked.
    sql: the statement(s) under test, each compared. Mutually exclusive with run.
    run: a Python probe, for what SQL alone can't express (connector APIs, several sessions, ...).
    setup: statements run first and not compared. Snowflake must accept them.
    teardown: statements always run last, errors ignored. Only for things outside the run's schema.
    refs: upstream issues or PRs the case relates to.
    compare: what to compare.
    expected: a known difference (or one per target).

    SQL may use the placeholders {database} and {schema}: the run's database and its unique schema.
    Name anything a case creates outside that schema starting with {schema}, so the run can clean it up.
    """

    id: str
    description: str
    sql: str | Sequence[str] = ()
    run: ProbeFn | None = None
    setup: Sequence[str] = ()
    teardown: Sequence[str] = ()
    refs: Sequence[str] = ()
    compare: Compare = Compare()
    expected: Expected | Sequence[Expected] | None = None
    source: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.id):
            raise CaseError(f"case id {self.id!r} should be lower case letters, digits, '.', '_' or '-'")
        if not self.description.strip():
            raise CaseError(f"{self.id}: description is empty")
        if bool(self.statements) == bool(self.run):
            raise CaseError(f"{self.id}: give exactly one of sql or run")
        for name in ("setup", "teardown", "refs"):
            if isinstance(getattr(self, name), str):
                raise CaseError(f"{self.id}: {name} must be a list, not a string")
        for ref in self.refs:
            if not _REF.fullmatch(ref):
                raise CaseError(f"{self.id}: reference {ref!r} should be '#<number>', a URL or 'unfiled'")
        expected = self.expectations
        if len({tuple(e.targets) for e in expected}) != len(expected):
            raise CaseError(f"{self.id}: more than one expected difference for the same targets")

    @property
    def statements(self) -> tuple[str, ...]:
        return (self.sql,) if isinstance(self.sql, str) else tuple(self.sql)

    @property
    def expectations(self) -> tuple[Expected, ...]:
        if self.expected is None:
            return ()
        return (self.expected,) if isinstance(self.expected, Expected) else tuple(self.expected)

    def expected_for(self, target: str) -> Expected | None:
        """The expected difference for a target: one naming it, else one for all targets."""
        expected = self.expectations
        return next((e for e in expected if target in e.targets), None) or next(
            (e for e in expected if not e.targets), None
        )

    @property
    def all_refs(self) -> tuple[str, ...]:
        refs = [*self.refs, *(r for e in self.expectations for r in e.refs)]
        return tuple(dict.fromkeys(refs))

    def fingerprint(self) -> str:
        """Identifies what the case runs, so a recorded outcome is only reused for the same probe.

        For a Python probe that's its whole module, parsed, so that formatting and comments don't count but a
        change to a helper it calls does.
        """
        body = None
        if self.run:
            module = inspect.getmodule(self.run)
            body = ast.dump(ast.parse(inspect.getsource(module or self.run)))
        parts = [repr(tuple(self.setup)), repr(self.statements), repr(tuple(self.teardown)), repr(body)]
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]
