from typing import Any

import pytest

from probes import Case, Expected
from probes.model import CaseError


def test_case_statements_from_a_string_or_a_list():
    assert Case(id="a.b", description="d", sql="select 1").statements == ("select 1",)
    assert Case(id="a.b", description="d", sql=["select 1", "select 2"]).statements == ("select 1", "select 2")


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"id": "Bad Id", "sql": "select 1"}, "lower case"),
        ({"id": "a.b", "description": " ", "sql": "select 1"}, "description is empty"),
        ({"id": "a.b"}, "exactly one of sql or run"),
        ({"id": "a.b", "sql": "select 1", "run": lambda conn, ctx: None}, "exactly one of sql or run"),
        ({"id": "a.b", "sql": "select 1", "setup": "create table t (x int)"}, "setup must be a list"),
        ({"id": "a.b", "sql": "select 1", "refs": ["473"]}, "should be '#<number>'"),
    ],
)
def test_case_validation(kwargs: dict[str, Any], message: str):
    with pytest.raises(CaseError, match=message):
        arguments: dict[str, Any] = {"description": "d", **kwargs}
        Case(**arguments)


def test_expected_needs_a_reference_and_a_reason():
    with pytest.raises(CaseError, match="needs a reference"):
        Expected([], "a reason")
    with pytest.raises(CaseError, match="needs a reason"):
        Expected(["#1"], "")
    with pytest.raises(CaseError, match="not a string"):
        Expected("#1", "a reason")


def test_expected_for_prefers_the_targets_own():
    general = Expected(["#1"], "everywhere")
    server = Expected(["#2"], "server only", targets=["fakesnow-server"])
    case = Case(id="a.b", description="d", sql="select 1", expected=[general, server])
    assert case.expected_for("fakesnow-server") is server
    assert case.expected_for("fakesnow") is general
    assert case.all_refs == ("#1", "#2")


def test_expected_for_none_when_only_another_target():
    server = Expected(["#2"], "server only", targets=["fakesnow-server"])
    case = Case(id="a.b", description="d", sql="select 1", expected=server)
    assert case.expected_for("fakesnow") is None


def test_two_expected_for_the_same_targets_is_an_error():
    with pytest.raises(CaseError, match="more than one"):
        Case(id="a.b", description="d", sql="select 1", expected=[Expected(["#1"], "x"), Expected(["#2"], "y")])


def test_fingerprint_follows_the_sql():
    one = Case(id="a.b", description="d", sql="select 1")
    assert one.fingerprint() == Case(id="a.c", description="other", sql="select 1").fingerprint()
    assert one.fingerprint() != Case(id="a.b", description="d", sql="select 2").fingerprint()
    assert one.fingerprint() != Case(id="a.b", description="d", sql="select 1", setup=["select 0"]).fingerprint()


def test_references_to_other_repos_and_unfiled():
    case = Case(id="a.b", description="d", sql="select 1", refs=["#1", "tobymao/sqlglot#8804", "https://x.y/z"])
    assert Expected(["unfiled"], "no issue yet").refs == ["unfiled"]
    assert case.all_refs == ("#1", "tobymao/sqlglot#8804", "https://x.y/z")
