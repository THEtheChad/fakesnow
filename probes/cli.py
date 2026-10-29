"""python -m probes: run cases against Snowflake and fakesnow and report where they differ."""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import snowflake.connector.errors

from probes.loader import DEFAULT_CASES, LoadError, load_cases, select
from probes.model import Case
from probes.outcome import redact
from probes.report import load_run, markdown, recorded_database, summary, write_json
from probes.runner import Report, TargetRun, compare_runs, run_target, shuffled
from probes.targets import FAKESNOW, FAKESNOW_SERVER, SNOWFLAKE, TARGETS, Names, SnowflakeConfig, TargetError

DEFAULT_DATABASE = "PROBES"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m probes", description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help=f"case files or directories (default: {DEFAULT_CASES})")
    parser.add_argument(
        "--reference",
        choices=TARGETS,
        default=SNOWFLAKE,
        help="what the targets are compared with (default: snowflake)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        metavar="JSON",
        help="use the reference's outcomes from an earlier run's --json report instead of running it",
    )
    parser.add_argument(
        "--target",
        dest="targets",
        action="append",
        choices=TARGETS,
        help=f"a target to compare with the reference, repeatable (default: {FAKESNOW} and {FAKESNOW_SERVER})",
    )
    parser.add_argument(
        "-k",
        dest="patterns",
        action="append",
        default=[],
        metavar="PATTERN",
        help="only cases matching: a reference (#1, owner/repo#1, unfiled), an id glob, or an id substring",
    )
    parser.add_argument("--markdown", type=Path, metavar="FILE", help="write a Markdown report")
    parser.add_argument("--json", type=Path, metavar="FILE", help="write a JSON report, reusable as a --baseline")
    parser.add_argument(
        "--strict-metadata", action="store_true", help="compare column names, scale and precision/length for every case"
    )
    parser.add_argument(
        "--shuffle",
        type=_seed,
        metavar="SEED",
        help="run the cases in a random order from SEED (an integer, or 'random'), the same on every target",
    )
    parser.add_argument(
        "--order",
        type=lambda text: [i for i in text.split(",") if i],
        metavar="ID,ID,...",
        help="run exactly these cases, in this order",
    )
    parser.add_argument("--list", action="store_true", help="list the selected cases and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each case as it runs")
    return parser.parse_args(argv)


def _seed(text: str) -> int:
    if text == "random":
        return random.SystemRandom().randrange(1, 2**31)
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer or 'random', not {text!r}") from None


def run_order(args: argparse.Namespace, loaded: Sequence[Case], selected: Sequence[Case]) -> list[Case]:
    """The cases to run, in the order to run them."""
    if args.order is not None:
        if args.patterns or args.shuffle is not None:
            raise LoadError("--order picks and orders the cases itself: don't combine it with -k or --shuffle")
        by_id = {c.id: c for c in loaded}
        if unknown := [i for i in args.order if i not in by_id]:
            raise LoadError(f"--order: no such case: {', '.join(unknown)}")
        if len(set(args.order)) != len(args.order):
            raise LoadError("--order: a case is listed twice")
        return [by_id[i] for i in args.order]
    return shuffled(selected, args.shuffle) if args.shuffle is not None else list(selected)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        loaded = load_cases(args.paths or [DEFAULT_CASES])
        order = run_order(args, loaded, select(loaded, args.patterns))
    except LoadError as e:
        print(f"error: {redact(str(e))}", file=sys.stderr)
        return 2
    if args.strict_metadata:
        strict = {c.id: replace(c, compare=replace(c.compare, names=True, scale=True, precision=True)) for c in order}
        order = [strict[c.id] for c in order]
    # reported in the order of the files, whichever order they ran in
    position = {c.id: i for i, c in enumerate(loaded)}
    cases = sorted(order, key=lambda c: position[c.id])
    if args.list:
        for case in order:
            print(f"{case.id:<48} {', '.join(case.all_refs):<14} {case.description}")
        return 0
    if not cases:
        print("error: no cases selected", file=sys.stderr)
        return 2

    targets: list[str] = [t for t in (args.targets or [FAKESNOW, FAKESNOW_SERVER]) if t != args.reference]
    if not targets:
        print("error: no targets to compare with the reference", file=sys.stderr)
        return 2
    try:
        report = run(cases, order, args.reference, targets, args.baseline, args.verbose, args.shuffle)
    except (TargetError, ValueError, OSError, snowflake.connector.errors.Error) as e:
        print(f"error: {redact(str(e))}", file=sys.stderr)
        return 2

    print(summary(report))
    if args.markdown:
        args.markdown.write_text(markdown(report))
        print(f"\nMarkdown report: {args.markdown}")
    if args.json:
        write_json(report, args.json)
        print(f"JSON report: {args.json}")
    return 1 if report.fails else 0


def run(
    cases: list[Case],
    order: list[Case],
    reference: str,
    targets: list[str],
    baseline: Path | None,
    verbose: bool,
    seed: int | None = None,
) -> Report:
    needs_snowflake = (reference == SNOWFLAKE and not baseline) or SNOWFLAKE in targets
    config = SnowflakeConfig.from_env() if needs_snowflake else None
    if config:
        print(f"Snowflake: {config.describe()}", file=sys.stderr)
    # fakesnow runs in the same database as the reference, so names in outcomes match
    database = recorded_database(baseline) if baseline else config.database if config else DEFAULT_DATABASE
    names = Names.new(database)
    if seed is not None:
        print(f"Running in random order: --shuffle {seed}", file=sys.stderr)

    def progress(target: str, case: Case) -> None:
        if verbose:
            print(f"  {target}: {case.id}", file=sys.stderr)

    def timed(target: str) -> TargetRun:
        start = time.monotonic()
        print(f"{target}: running {len(order)} cases in {names.database}.{names.schema}", file=sys.stderr)
        result = run_target(target, order, names, config, progress)
        print(f"{target}: done in {time.monotonic() - start:.0f}s", file=sys.stderr)
        return result

    reference_run = load_run(baseline, reference) if baseline else timed(reference)
    candidates = [timed(t) for t in targets]
    return compare_runs(cases, reference_run, candidates, names, seed, [c.id for c in order])
