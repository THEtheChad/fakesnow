from pathlib import Path

import pytest

from probes.loader import DEFAULT_CASES, LoadError, load_cases, select

CASE_FILE = """
from probes import Case

CASES = [
    Case(id="area.one", description="one", sql="select 1", refs=["#1"]),
    Case(id="area.two", description="two", sql="select 2"),
]
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_load_a_directory_recursively_skipping_underscored_files(tmp_path: Path):
    write(tmp_path / "sub" / "area.py", CASE_FILE)
    write(tmp_path / "_helpers.py", "raise RuntimeError('not a case file')")
    cases = load_cases([tmp_path])
    assert [c.id for c in cases] == ["area.one", "area.two"]
    assert cases[0].source == str((tmp_path / "sub" / "area.py").resolve())


def test_load_a_file(tmp_path: Path):
    assert len(load_cases([write(tmp_path / "area.py", CASE_FILE)])) == 2


def test_duplicate_ids_are_an_error(tmp_path: Path):
    write(tmp_path / "a.py", CASE_FILE)
    write(tmp_path / "b.py", CASE_FILE)
    with pytest.raises(LoadError, match=r"duplicate case id 'area\.one'"):
        load_cases([tmp_path])


def test_a_file_without_cases_is_an_error(tmp_path: Path):
    with pytest.raises(LoadError, match="should define CASES"):
        load_cases([write(tmp_path / "a.py", "X = 1")])


def test_an_invalid_case_is_an_error(tmp_path: Path):
    text = "from probes import Case\nCASES = [Case(id='Bad', description='d', sql='select 1')]"
    with pytest.raises(LoadError, match="CaseError"):
        load_cases([write(tmp_path / "a.py", text)])


def test_a_missing_path_is_an_error(tmp_path: Path):
    with pytest.raises(LoadError, match="no such file"):
        load_cases([tmp_path / "missing"])


def test_select_by_ref_glob_or_substring(tmp_path: Path):
    cases = load_cases([write(tmp_path / "area.py", CASE_FILE)])
    assert [c.id for c in select(cases, ["#1"])] == ["area.one"]
    assert [c.id for c in select(cases, ["area.t*"])] == ["area.two"]
    assert [c.id for c in select(cases, ["one", "two"])] == ["area.one", "area.two"]
    assert select(cases, []) == cases


def test_the_shipped_cases_load():
    cases = load_cases([DEFAULT_CASES])
    assert len(cases) > 300
    assert len(select(cases, ["keywords.*"])) == 144


def test_load_reviewed_expectation_sidecar(tmp_path: Path):
    path = tmp_path / "cases.py"
    path.write_text('from probes import Case\nCASES = [Case(id="a.b", description="d", sql="select 1")]')
    (tmp_path / "_expected.json").write_text('{"a.b": {"fakesnow": "' + "a" * 64 + '"}}')
    assert load_cases([path])[0].expected_outcomes == {"fakesnow": "a" * 64}


def test_invalid_expectation_sidecar_is_load_error(tmp_path: Path):
    path = tmp_path / "cases.py"
    path.write_text('from probes import Case\nCASES = [Case(id="a.b", description="d", sql="select 1")]')
    (tmp_path / "_expected.json").write_text('{"a.b": {"fakesnow": "not-a-fingerprint"}}')
    with pytest.raises(LoadError, match="invalid expectation pins"):
        load_cases([path])
