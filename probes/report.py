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


def summary(report: Report) -> str:
    lines = [f"{len(report.cases)} cases; reference {describe_target(report.reference)}"]
    for run in report.candidates:
        c = counts(report, run.target)
        lines.append(
            f"  {describe_target(run)}: {c[Status.MATCH]} match, {c[Status.UNEXPECTED]} unexpected, "
            f"{c[Status.EXPECTED]} expected, {c[Status.NOW_MATCHES]} now match, {c[Status.ERROR]} errors"
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
    return "\n".join(lines)


# --- Markdown


def markdown(report: Report) -> str:
    targets = [r.target for r in report.candidates]
    out = [
        "# fakesnow probes",
        "",
        f"Run {time.strftime('%Y-%m-%d %H:%M %Z')}: {len(report.cases)} cases, "
        f"reference {describe_target(report.reference)}, "
        f"compared with {', '.join(describe_target(r) for r in report.candidates)}.",
        "",
        "| target | match | unexpected | expected | now matches | error |",
        "|---|---|---|---|---|---|",
    ]
    for target in targets:
        c = counts(report, target)
        out.append(f"| {target} | " + " | ".join(str(c[s]) for s in Status) + " |")
    if not report.with_expected:
        out += ["", "The reference isn't Snowflake, so expected differences don't apply."]

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
        "runs": {
            run.target: {
                "info": run.info,
                "recorded": run.recorded,
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
    )


def recorded_database(path: Path) -> str:
    return json.loads(path.read_text())["names"]["database"]
