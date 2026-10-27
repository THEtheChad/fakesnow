import datetime
import decimal

from probes import Case, Compare, Expected
from probes.compare import Status, classify, comparable, diff
from probes.outcome import Column, ErrorInfo, Outcome, ResultSet, Step

TEXT, VARIANT, FIXED = 2, 5, 0
NORMALISE = Compare()
STRICT = Compare(normalise=False)


def result(*rows: tuple, types: tuple[int, ...] = (FIXED,), names: tuple[str, ...] | None = None) -> Outcome:
    columns = tuple(Column(n, t) for n, t in zip(names or [f"C{i}" for i in range(len(types))], types, strict=True))
    return Outcome(steps=(Step("select", result=ResultSet(columns, rows)),))


def error(errno: int | None, sqlstate: str | None = None, message: str = "m") -> Outcome:
    return Outcome(steps=(Step("select", error=ErrorInfo("ProgrammingError", errno, sqlstate, message)),))


# --- normalisation


def test_aware_datetimes_compare_as_instants():
    utc = datetime.datetime(2024, 1, 1, 10, tzinfo=datetime.UTC)
    plus2 = datetime.datetime(2024, 1, 1, 12, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
    assert comparable(utc, NORMALISE) == comparable(plus2, NORMALISE)
    assert comparable(utc, STRICT) != comparable(plus2, STRICT)


def test_naive_and_aware_datetimes_differ():
    naive = datetime.datetime(2024, 1, 1, 10)
    assert comparable(naive, NORMALISE) != comparable(naive.replace(tzinfo=datetime.UTC), NORMALISE)


def test_int_decimal_and_float_differ():
    assert len({comparable(1, NORMALISE), comparable(decimal.Decimal(1), NORMALISE), comparable(1.0, NORMALISE)}) == 3
    assert comparable(True, NORMALISE) != comparable(1, NORMALISE)


def test_json_text_ignores_whitespace_only_in_semi_structured_columns():
    snowflake, fakesnow = result(('{\n  "a": 1\n}',), types=(VARIANT,)), result(('{"a":1}',), types=(VARIANT,))
    assert diff(NORMALISE, snowflake, fakesnow) == []
    assert diff(STRICT, snowflake, fakesnow)
    assert diff(NORMALISE, result(('{\n  "a": 1\n}',), types=(TEXT,)), result(('{"a":1}',), types=(TEXT,)))


def test_json_text_keeps_key_order_and_number_spelling():
    def differs(a: str, b: str) -> bool:
        return bool(diff(NORMALISE, result((a,), types=(VARIANT,)), result((b,), types=(VARIANT,))))

    assert differs('{"a": 1, "b": 2}', '{"b": 2, "a": 1}')
    assert differs("1.0", "1")
    assert not differs("[1, 2]", "[1,2]")


# --- differences


def test_identical_results_match():
    assert diff(NORMALISE, result((1, "a"), types=(FIXED, TEXT)), result((1, "a"), types=(FIXED, TEXT))) == []


def test_type_codes_differ():
    assert diff(NORMALISE, result((1,), types=(FIXED,)), result((1,), types=(TEXT,))) == [
        "step 1: types [FIXED(0)] vs [TEXT(2)]"
    ]
    assert diff(Compare(types=False), result((1,), types=(FIXED,)), result((1,), types=(TEXT,))) == []


def test_names_are_compared_only_when_asked():
    a, b = result((1,), names=("ABC",)), result((1,), names=("abc",))
    assert diff(NORMALISE, a, b) == []
    assert diff(Compare(names=True), a, b) == ["step 1: names ['ABC'] vs ['abc']"]


def test_rows_differ():
    assert diff(NORMALISE, result((1,)), result((decimal.Decimal(1),))) == ["step 1: rows [(1,)] vs [(Decimal('1'),)]"]


def test_errors_compare_codes_not_messages_by_default():
    assert diff(NORMALISE, error(2003, "42S02", "one"), error(2003, "42S02", "two")) == []
    assert diff(Compare(error_message=True), error(2003, "42S02", "one"), error(2003, "42S02", "two")) == [
        "step 1: message 'one' vs 'two'"
    ]
    assert diff(NORMALISE, error(2003, "42S02"), error(1003, "42000")) == ["step 1: error 2003 (42S02) vs 1003 (42000)"]


def test_error_versus_success():
    assert diff(NORMALISE, error(2003, "42S02"), result((1,))) == ["step 1: error 2003 (42S02) vs succeeded"]


def test_fewer_steps():
    two = Outcome(steps=(*result((1,)).steps, *result((2,)).steps))
    assert diff(NORMALISE, two, result((1,))) == ["step 2: ran vs not run"]


def test_candidate_setup_failure_is_a_difference():
    failed = Outcome(setup_error=error(1003, "42000").steps[0])
    assert diff(NORMALISE, result((1,)), failed) == ["setup failed: select: ProgrammingError 1003 (42000): m"]


def test_python_values_and_errors():
    assert diff(NORMALISE, Outcome(value={"a": [1]}), Outcome(value={"a": [1]})) == []
    assert diff(NORMALISE, Outcome(value={"a": [1]}), Outcome(value={"a": [1.0]})) == [
        "returned {'a': [1]} vs {'a': [1.0]}"
    ]
    raised = Outcome(error=ErrorInfo("NotImplementedError", None, None, "x"))
    assert diff(NORMALISE, Outcome(value=1), raised) == ["raised: succeeded vs error NotImplementedError"]


# --- classification

CASE = Case(id="a.b", description="d", sql="select")
EXPECTED = Case(id="a.b", description="d", sql="select", expected=Expected(["#1"], "known"))


def test_classify_statuses():
    same, other = result((1,)), result((2,))
    assert classify(CASE, same, same, "fakesnow", with_expected=True).status is Status.MATCH
    assert classify(CASE, same, other, "fakesnow", with_expected=True).status is Status.UNEXPECTED
    assert classify(EXPECTED, same, other, "fakesnow", with_expected=True).status is Status.EXPECTED
    assert classify(EXPECTED, same, same, "fakesnow", with_expected=True).status is Status.NOW_MATCHES


def test_classify_ignores_expected_without_snowflake():
    same, other = result((1,)), result((2,))
    assert classify(EXPECTED, same, other, "fakesnow", with_expected=False).status is Status.UNEXPECTED
    assert classify(EXPECTED, same, same, "fakesnow", with_expected=False).status is Status.MATCH


def test_classify_expected_for_another_target_is_unexpected():
    case = Case(id="a.b", description="d", sql="select", expected=Expected(["#1"], "x", targets=["fakesnow-server"]))
    assert classify(case, result((1,)), result((2,)), "fakesnow", with_expected=True).status is Status.UNEXPECTED


def test_classify_broken_reference_is_an_error():
    setup_failed = Outcome(setup_error=error(1003, "42000").steps[0])
    probe_bug = Outcome(error=ErrorInfo("AttributeError", None, None, "x", database_error=False))
    for reference in (setup_failed, probe_bug, Outcome(crash="Traceback")):
        assert classify(CASE, reference, result((1,)), "fakesnow", with_expected=True).status is Status.ERROR


def test_classify_candidate_crash_is_an_error():
    assert classify(CASE, result((1,)), Outcome(crash="boom"), "fakesnow", with_expected=True).status is Status.ERROR


def test_status_fails():
    assert {s for s in Status if s.fails} == {Status.UNEXPECTED, Status.ERROR}
