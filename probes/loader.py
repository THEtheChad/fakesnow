"""Find and load cases: Python files that each define CASES, a list of probes.model.Case."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import sys
import types
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path

from probes.model import UNFILED, Case

DEFAULT_CASES = Path(__file__).parent / "cases"


class LoadError(Exception):
    """Cases couldn't be loaded."""


def case_files(paths: Iterable[Path]) -> list[Path]:
    """The case files in the given files and directories (recursively), skipping names starting with _."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(p for p in path.rglob("*.py") if not p.name.startswith("_"))
        elif path.is_file():
            files.append(path)
        else:
            raise LoadError(f"no such file or directory: {path}")
    return list(dict.fromkeys(f.resolve() for f in files))


def load_file(path: Path) -> list[Case]:
    # a module name unique to the file, so two projects' identities.py don't collide
    name = f"_probe_cases.{path.stem}_{hashlib.sha256(str(path).encode()).hexdigest()[:8]}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    try:
        # compiled from source rather than imported: a cached .pyc can miss an edit made within the same second
        exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    except Exception as e:
        raise LoadError(f"{path}: {type(e).__name__}: {e}") from e
    cases = getattr(module, "CASES", None)
    if not isinstance(cases, list | tuple) or not all(isinstance(c, Case) for c in cases):
        raise LoadError(f"{path}: should define CASES, a list of probes.Case")
    pins_path = path.parent / "_expected.json"
    try:
        pins = json.loads(pins_path.read_text()) if pins_path.exists() else {}
        return [replace(c, source=str(path), expected_outcomes=pins.get(c.id, c.expected_outcomes)) for c in cases]
    except (OSError, ValueError, AttributeError) as e:
        raise LoadError(f"{pins_path}: invalid expectation pins") from e


def load_cases(paths: Sequence[Path]) -> list[Case]:
    """Load every case under the given paths, checking ids are unique."""
    cases: list[Case] = []
    seen: dict[str, str] = {}
    for path in case_files(paths):
        for case in load_file(path):
            if case.id in seen:
                raise LoadError(f"duplicate case id {case.id!r} in {seen[case.id]} and {path}")
            seen[case.id] = str(path)
            cases.append(case)
    return cases


def select(cases: Sequence[Case], patterns: Sequence[str]) -> list[Case]:
    """Cases matching any pattern: a reference (#1, owner/repo#1 or unfiled), an id glob, or else an id substring."""
    if not patterns:
        return list(cases)

    def matches(case: Case, pattern: str) -> bool:
        if "#" in pattern or pattern == UNFILED:
            return pattern in case.all_refs
        if any(c in pattern for c in "*?["):
            return fnmatch.fnmatchcase(case.id, pattern)
        return pattern in case.id

    def selected(case: Case) -> bool:
        return any(matches(case, p) for p in patterns)

    return [c for c in cases if selected(c)]
