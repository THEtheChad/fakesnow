import datetime
import decimal
import zoneinfo
from typing import Any

import duckdb
import pytest
import snowflake.connector.errors

from probes.outcome import Column, ErrorInfo, Outcome, ResultSet, Step, decode, encode, scrub_value, scrubber


def test_error_info_from_a_connector_error():
    e = snowflake.connector.errors.ProgrammingError(msg="Object 'T' does not exist", errno=2003, sqlstate="42S02")
    info = ErrorInfo.from_exception(e)
    assert (info.kind, info.errno, info.sqlstate, info.message) == (
        "ProgrammingError",
        2003,
        "42S02",
        "Object 'T' does not exist",
    )
    assert info.database_error


def test_error_info_fakesnow_errors_count_as_the_databases():
    assert ErrorInfo.from_exception(NotImplementedError("for column type INTEGER[]")).database_error
    assert ErrorInfo.from_exception(duckdb.ParserException("bad")).database_error


def test_error_info_other_errors_are_the_probes():
    info = ErrorInfo.from_exception(AttributeError("no attribute 'x'"))
    assert (info.kind, info.errno, info.database_error) == ("AttributeError", None, False)


def test_scrubber_preserves_casing_differences():
    scrub = scrubber({"PROBES_1_ABC": "{schema}", "DB": "{database}"})
    assert scrub("Object 'DB.PROBES_1_ABC.T' does not exist") == "Object '{database}.{schema}.T' does not exist"
    assert scrub("probes_1_abc_S2 and DBX") == "{schema:lower}_S2 and DBX"


def test_scrub_value_recurses():
    scrub = scrubber({"S1": "{schema}"})
    value = {"rows": [("S1", 1)], "error": ErrorInfo("E", 1, "1", "in S1")}
    assert scrub_value(value, scrub) == {"rows": [("{schema}", 1)], "error": ErrorInfo("E", 1, "1", "in {schema}")}


def test_encode_decode_round_trip():
    la = zoneinfo.ZoneInfo("America/Los_Angeles")
    values: list[Any] = [
        None,
        True,
        1,
        1.5,
        "x",
        decimal.Decimal("1.50"),
        datetime.datetime(2024, 1, 1, 10, tzinfo=la),
        datetime.datetime(2024, 1, 1, 10, tzinfo=datetime.timezone(datetime.timedelta(hours=2))),
        datetime.datetime(2024, 1, 1, 10),
        datetime.date(2024, 1, 1),
        datetime.time(10, 11),
        b"\xab",
        bytearray(b"\xab"),
        (1, [2, {"k": (3,)}]),
    ]
    decoded = decode(encode(values))
    assert decoded == values
    assert [type(v) for v in decoded] == [type(v) for v in values]
    assert [v.utcoffset() for v in decoded[6:9]] == [v.utcoffset() for v in values[6:9]]


def test_encode_decode_non_finite_float():
    assert repr(decode(encode([float("nan"), float("-inf")]))) == "[nan, -inf]"


def test_encode_decode_outcome():
    outcome = Outcome(
        steps=(
            Step("select 1", result=ResultSet((Column("1", 0, 38, 0),), ((1,),))),
            Step("select x", error=ErrorInfo("ProgrammingError", 904, "42000", "invalid identifier 'X'")),
        ),
        notes=("a note",),
    )
    assert decode(encode(outcome)) == outcome


def test_nested_resultset_names_are_scrubbed():
    value = ResultSet((Column("PROBES_ABC", 2),), (("PROBES_ABC",),))
    assert scrub_value({"result": value}, scrubber({"PROBES_ABC": "{schema}"})) == {
        "result": ResultSet((Column("{schema}", 2),), (("{schema}",),))
    }


def test_exception_diagnostics_redact_configured_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROBES_SNOWFLAKE_PASSWORD", "sentinel-password")
    monkeypatch.setenv("PROBES_SNOWFLAKE_PRIVATE_KEY_FILE_PWD", "sentinel-passphrase")
    info = ErrorInfo.from_exception(ValueError("failed using sentinel-password and sentinel-passphrase"))
    assert "sentinel" not in info.message
