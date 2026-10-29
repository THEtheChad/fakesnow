"""Report a run: a terminal summary, Markdown for a PR or issue, and JSON that a later run can reuse."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from probes.compare import Status, Verdict, show, show_types
from probes.model import UNFILED, Case
from probes.outcome import ErrorInfo, Outcome, ResultSet, Step, decode, encode
from probes.runner import Report, TargetRun
from probes.targets import SNOWFLAKE

JSON_FORMAT = "fakesnow-probes/1"

LABELS = {
    Status.MATCH: "match",
    Status.UNEXPECTED: "UNEXPECTED",
    Status.EXPECTED: "expected",
    Status.NOW_MATCHES: "now matches",
    Status.ERROR: "ERROR",
}


def counts(report: Report, target: str) -> dict[Status, int]:
    out = dict.fromkeys(Status, 0)
    for by_target in report.verdicts.values():
        out[by_target[target].status] += 1
    return out


def describe_target(run: TargetRun) -> str:
    info = run.info
    if run.target == SNOWFLAKE:
        text = f"Snowflake {info.get('version', '?')}"
    else:
        text = f"{run.target} {info.get('version', '?')}" + (f" ({info['commit']})" if "commit" in info else "")
    return text + (" (recorded)" if run.recorded else "")


def unfiled_differences(report: Report) -> list[tuple[Case, list[str], str]]:
    """Expected differences still differing whose reference is 'unfiled', with the targets they show on."""
    found: list[tuple[Case, list[str], str]] = []
    for case in report.cases:
        by_reason: dict[str, list[str]] = {}
        for target, v in report.verdicts[case.id].items():
            if v.status is Status.EXPECTED and v.expected and UNFILED in v.expected.refs:
                by_reason.setdefault(v.expected.reason, []).append(target)
        found += [(case, targets, reason) for reason, targets in by_reason.items()]
    return found


def describe_order(report: Report) -> str:
    if report.seed is not None:
        return f"in random order (--shuffle {report.seed})"
    if report.order != [c.id for c in report.cases]:
        return "in the order given (--order)"
    return "in file order"


def leaks(report: Report) -> list[tuple[Case, str, tuple[str, ...]]]:
    """Each case and target (the reference too, when it ran now) whose outcome reports leaks."""
    runs = [report.reference, *report.candidates]
    return [
        (case, run.target, outcome.leaks)
        for case in report.cases
        for run in runs
        if (outcome := run.outcomes.get(case.id)) and outcome.leaks
    ]


def summary(report: Report) -> str:
    lines = [f"{len(report.cases)} cases {describe_order(report)}; reference {describe_target(report.reference)}"]
    leaked = leaks(report)
    for run in report.candidates:
        c = counts(report, run.target)
        lines.append(
            f"  {describe_target(run)}: {c[Status.MATCH]} match, {c[Status.UNEXPECTED]} unexpected, "
            f"{c[Status.EXPECTED]} expected, {c[Status.NOW_MATCHES]} now match, {c[Status.ERROR]} errors, "
            f"{sum(target == run.target for _, target, _ in leaked)} leaked"
        )
    if unfiled := unfiled_differences(report):
        lines.append(f"  {len(unfiled)} expected differences have no upstream issue yet (refs: unfiled)")
    sections = [
        (Status.UNEXPECTED, "Unexpected differences"),
        (Status.NOW_MATCHES, "Expected differences that now match (remove the expected mark)"),
        (Status.ERROR, "Errors"),
    ]
    for status, title in sections:
        found = [
            (case, target, v)
            for case in report.cases
            for target, v in report.verdicts[case.id].items()
            if v.status is status
        ]
        if not found:
            continue
        lines += ["", f"{title}:"]
        for case, target, v in found:
            detail = v.differences[0].splitlines()[0] if v.differences else ", ".join(e for e in case.all_refs)
            lines.append(f"  {target:<16} {case.id}: {show(detail, 160)}")
    if leaked:
        lines += ["", "Leaks (shared state a case left changed; the runner undid it unless noted):"]
        lines += [f"  {target:<16} {case.id}: {show('; '.join(found), 160)}" for case, target, found in leaked]
    lines += ["", comparison_scope(report), leak_scope(report)]
    return "\n".join(lines)


def comparison_scope(report: Report) -> str:
    omitted = {
        name: sum(not getattr(c.compare, name) for c in report.cases)
        for name in ("rows", "types", "names", "scale", "precision", "error_codes", "error_message")
    }
    return (
        "Comparison scope: checks disabled in cases: "
        + ", ".join(f"{name}={count}" for name, count in omitted.items() if count)
        + ". A match covers only enabled checks; use --strict-metadata to check all column metadata."
    )


def leak_scope(report: Report) -> str:
    """What the leak check covered on each target, and where it didn't run (a recorded run from before it)."""
    parts: list[str] = []
    any_unchecked = False
    for run in [report.reference, *report.candidates]:
        outcomes = [o for c in report.cases if (o := run.outcomes.get(c.id))]
        unchecked = sum(o.leaks is None for o in outcomes)
        any_unchecked |= unchecked > 0
        scope = run.leak_check or "scope not recorded"
        if unchecked == len(outcomes):
            scope = "not checked (recorded before the leak check)"
        elif unchecked:
            scope += f"; not checked for {unchecked} cases (recorded before the leak check)"
        parts.append(f"{run.target}{' (recorded)' if run.recorded else ''}: {scope}")
    text = "Leak check: " + "; ".join(parts) + "."
    if any_unchecked:
        text += " A leak against an unchecked reference is an error until the reference is rerun."
    return text


# --- Markdown


def markdown(report: Report) -> str:
    targets = [r.target for r in report.candidates]
    out = [
        "# fakesnow probes",
        "",
        f"Run {time.strftime('%Y-%m-%d %H:%M %Z')}: {len(report.cases)} cases {describe_order(report)}, "
        f"reference {describe_target(report.reference)}, "
        f"compared with {', '.join(describe_target(r) for r in report.candidates)}.",
        "",
        comparison_scope(report),
        "",
        leak_scope(report),
        "",
        "| target | match | unexpected | expected | now matches | error |",
        "|---|---|---|---|---|---|",
    ]
    for target in targets:
        c = counts(report, target)
        out.append(f"| {target} | " + " | ".join(str(c[s]) for s in Status) + " |")
    if not report.with_expected:
        out += ["", "The reference isn't Snowflake, so expected differences don't apply."]

    if leaked := leaks(report):
        out += [
            "",
            "## Leaks",
            "",
            "Shared state a case left changed after its teardown and its schema was dropped. The runner undid it "
            "unless noted, so later cases weren't affected.",
            "",
            "| case | target | leaks |",
            "|---|---|---|",
        ]
        out += [f"| `{case.id}` | {target} | {_md_text('; '.join(found))} |" for case, target, found in leaked]

    if unfiled := unfiled_differences(report):
        out += [
            "",
            "## Confirmed differences with no upstream issue yet",
            "",
            "Marked expected with the reference `unfiled`: each is a candidate issue.",
            "",
            "| case | targets | reason |",
            "|---|---|---|",
        ]
        out += [f"| `{case.id}` | {', '.join(targets)} | {_md_text(reason)} |" for case, targets, reason in unfiled]

    differing = [c for c in report.cases if any(v.status is not Status.MATCH for v in report.verdicts[c.id].values())]
    matching = [c for c in report.cases if c not in differing]
    if differing:
        out += ["", "## Cases that don't match", "", "| case | " + " | ".join(targets) + " | refs |"]
        out.append("|---|" + "---|" * len(targets) + "---|")
        for case in differing:
            statuses = " | ".join(LABELS[report.verdicts[case.id][t].status] for t in targets)
            out.append(f"| `{case.id}` | {statuses} | {', '.join(case.all_refs)} |")
        for case in differing:
            out += ["", *case_section(report, case)]
    if matching:
        out += ["", f"<details><summary>Matching cases ({len(matching)})</summary>"]
        for case in matching:
            out += ["", *case_section(report, case)]
        out += ["", "</details>"]
    return "\n".join(out) + "\n"


def case_section(report: Report, case: Case) -> list[str]:
    out = [f"### `{case.id}`", "", case.description, ""]
    for target, verdict in report.verdicts[case.id].items():
        out.append(f"- {target}: **{verdict.status.value}**" + _expected_note(verdict))
        out += [f"  - {_md_text(d)}" for d in verdict.differences]
    if case.all_refs:
        out.append(f"- refs: {', '.join(case.all_refs)}")
    runs = [report.reference, *report.candidates]
    out += ["", "| | " + " | ".join(describe_target(r) for r in runs) + " |", "|---|" + "---|" * len(runs)]
    for label, cells in outcome_rows(case, [r.outcomes.get(case.id) for r in runs]):
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    return out


def _expected_note(verdict: Verdict) -> str:
    if not verdict.expected:
        return ""
    return f" ({', '.join(verdict.expected.refs)}: {_md_text(verdict.expected.reason)})"


def outcome_rows(case: Case, outcomes: Sequence[Outcome | None]) -> list[tuple[str, list[str]]]:
    """One row per setup failure, statement or Python result, one cell per target."""
    present = [o for o in outcomes if o is not None]
    rows: list[tuple[str, list[str]]] = []
    if any(o.crash for o in present):
        rows.append(
            ("harness", [_md_code(o.crash.strip().splitlines()[-1]) if o and o.crash else "" for o in outcomes])
        )
    if any(o.setup_error for o in present):
        rows.append(("setup", [_md_step(o.setup_error, case) if o and o.setup_error else "ok" for o in outcomes]))
    if case.run:
        rows.append((f"`{case.run.__name__}`", [_md_python(o, case) if o else "" for o in outcomes]))
    for i, sql in enumerate(case.statements):
        cells = [_md_step(o.steps[i], case) if o and i < len(o.steps) else "" for o in outcomes]
        rows.append((_md_code(sql), cells))
    rows += [("note", [_md_text(note) if o is p else "" for p in outcomes]) for o in present for note in o.notes]
    if any(o.leaks for o in present):
        rows.append(("leaks", [_md_text("; ".join(o.leaks)) if o and o.leaks else "" for o in outcomes]))
    return rows


def _md_step(step: Step, case: Case) -> str:
    if step.error:
        return _md_error(step.error)
    assert step.result
    return _md_result(step.result, case)


def _md_result(result: ResultSet, case: Case) -> str:
    parts = [_md_code(show(list(result.rows), 500)), _md_code(show_types(result.columns))]
    if case.compare.names:
        parts.append(_md_code(show([c.name for c in result.columns])))
    if case.compare.scale or case.compare.precision:
        sizes = [(c.precision if c.precision is not None else c.internal_size, c.scale) for c in result.columns]
        parts.append(_md_code("precision/length, scale: " + show(sizes)))
    return "<br>".join(parts)


def _md_error(error: ErrorInfo) -> str:
    return _md_code(show(str(error), 300))


def _md_python(outcome: Outcome, case: Case) -> str:
    if outcome.error:
        return "raised " + _md_error(outcome.error)
    return _md_code(show(outcome.value, 600))


def _md_code(text: str) -> str:
    text = " ".join(text.split("\n")).replace("`", "'").replace("|", "\\|")
    return f"`{text}`"


def _md_text(text: str) -> str:
    return " ".join(text.split("\n")).replace("|", "\\|")


# --- JSON


def to_json(report: Report) -> dict[str, Any]:
    return {
        "format": JSON_FORMAT,
        "names": {"database": report.names.database, "schema": report.names.schema},
        "reference": report.reference.target,
        "order": {"shuffle": report.seed, "cases": report.order},
        "runs": {
            run.target: {
                "info": run.info,
                "recorded": run.recorded,
                "leak check": run.leak_check,
                "outcomes": {
                    case_id: {"fingerprint": run.fingerprints.get(case_id), "outcome": encode(outcome)}
                    for case_id, outcome in run.outcomes.items()
                },
            }
            for run in [report.reference, *report.candidates]
        },
        "cases": [
            {
                "id": case.id,
                "description": case.description,
                "refs": list(case.all_refs),
                "source": case.source,
                "verdicts": {
                    target: {
                        "status": v.status.value,
                        "differences": list(v.differences),
                        "fingerprint": v.fingerprint,
                        **(
                            {"expected": {"refs": list(v.expected.refs), "reason": v.expected.reason}}
                            if v.expected
                            else {}
                        ),
                    }
                    for target, v in report.verdicts[case.id].items()
                },
            }
            for case in report.cases
        ],
    }


def write_json(report: Report, path: Path) -> None:
    path.write_text(json.dumps(to_json(report), indent=1) + "\n")


def load_run(path: Path, target: str = SNOWFLAKE) -> TargetRun:
    """A target's outcomes from an earlier run's JSON report, to compare against without running it again."""
    data = json.loads(path.read_text())
    if data.get("format") != JSON_FORMAT:
        raise ValueError(f"{path}: not a probes JSON report ({JSON_FORMAT})")
    if target not in data["runs"]:
        raise ValueError(f"{path}: has no {target} run, only {', '.join(data['runs'])}")
    run = data["runs"][target]
    return TargetRun(
        target,
        run["info"],
        outcomes={k: decode(v["outcome"]) for k, v in run["outcomes"].items()},
        fingerprints={k: v["fingerprint"] for k, v in run["outcomes"].items()},
        recorded=True,
        leak_check=run.get("leak check"),
    )


def recorded_database(path: Path) -> str:
    return json.loads(path.read_text())["names"]["database"]
