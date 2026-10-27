"""Scalar and aggregate functions: conditionals, strings, LISTAGG."""

from probes import UNFILED, Case, Expected
from probes.cases._gaps import DECIMAL_NOT_INT, FLOAT_NUMBERS, SEMI_TYPES

NAMES = [
    "create or replace table f_names (g int, s varchar)",
    "insert into f_names values (1, 'b'), (1, 'a'), (1, 'a'), (2, 'c'), (2, null)",
]

CASES = [
    # --- conditionals
    Case(
        id="functions.iff",
        description="IFF with true, false and NULL conditions",
        sql="select iff(1 > 0, 'y', 'n'), iff(1 < 0, 'y', 'n'), iff(null, 'y', 'n')",
    ),
    Case(
        id="functions.nvl-family",
        description="NVL, NVL2, IFNULL, COALESCE, NULLIF and ZEROIFNULL",
        sql="select nvl(null, 1), nvl2(null, 'a', 'b'), nvl2(1, 'a', 'b'), ifnull(null, 'x'), coalesce(null, null, 3), "
        "nullif(1, 1), zeroifnull(null)",
    ),
    Case(
        id="functions.decode",
        description="DECODE matches values (NULL matches NULL), with and without a default",
        sql="select decode(2, 1, 'one', 2, 'two', 'other'), decode(null, null, 'null matched', 'no'), "
        "decode(3, 1, 'one')",
    ),
    Case(
        id="functions.case-mixed-types",
        description="CASE with integer and decimal branches",
        sql="select case when true then 1 else 1.5 end, case when false then 1 end",
    ),
    Case(
        id="functions.equal-null",
        description="EQUAL_NULL and IS DISTINCT FROM",
        sql="select equal_null(null, null), equal_null(1, null), null is distinct from null, 1 is distinct from null",
    ),
    # --- strings
    Case(
        id="functions.split-part",
        description="SPLIT_PART with positive, negative and out-of-range parts",
        sql="select split_part('a,b,c', ',', 2), split_part('a,b,c', ',', -1), split_part('a,b,c', ',', 5), "
        "split_part('a,b,c', ',', 0)",
    ),
    Case(
        id="functions.split",
        description="SPLIT returns an ARRAY",
        sql="select split('a,b,,c', ',') as parts",
        expected=SEMI_TYPES,
    ),
    Case(
        id="functions.regexp-substr",
        description="REGEXP_SUBSTR: first match, second occurrence, no match, and a group",
        sql="select regexp_substr('abc123def456', '[0-9]+'), regexp_substr('abc123def456', '[0-9]+', 1, 2), "
        "regexp_substr('abc', 'z'), regexp_substr('a1b2', '([a-z])([0-9])', 1, 2, 'e', 2)",
    ),
    Case(
        id="functions.regexp-replace-like",
        description="REGEXP_REPLACE, REGEXP_LIKE and RLIKE",
        sql="select regexp_replace('a1b2', '[0-9]', 'X'), regexp_like('abc', 'a.c'), rlike('abc', 'b'), "
        "regexp_count('a1b2c3', '[0-9]')",
    ),
    Case(
        id="functions.listagg",
        description="LISTAGG ... WITHIN GROUP, skipping NULLs",
        setup=NAMES,
        sql="select g, listagg(s, ',') within group (order by s) from f_names group by g order by g",
    ),
    Case(
        id="functions.listagg-distinct",
        description="LISTAGG(DISTINCT ...)",
        setup=NAMES,
        sql="select listagg(distinct s, '|') within group (order by s) from f_names where g = 1",
    ),
    Case(
        id="functions.listagg-no-separator",
        description="LISTAGG without a separator concatenates directly",
        setup=NAMES,
        sql="select listagg(s) within group (order by s) from f_names where g = 1",
        expected=Expected([UNFILED], "LISTAGG without a separator joins with ','; Snowflake with ''"),
    ),
    Case(
        id="functions.concat-null",
        description="|| and CONCAT with a NULL are NULL",
        sql="select 'a' || null as a, concat('a', null) as b, concat('a', 'b', 'c') as c",
    ),
    Case(
        id="functions.concat-ws-null",
        description="CONCAT_WS with a NULL argument is NULL",
        sql="select concat_ws('-', 'a', null, 'b'), concat_ws('-', 'a', 'b')",
    ),
    Case(
        id="functions.case-and-trim",
        description="UPPER, LOWER, INITCAP, TRIM, LPAD and RPAD",
        sql="select upper('aB'), lower('aB'), initcap('hello wORLD'), trim('  x  '), "
        "lpad('5', 3, '0'), rpad('5', 3, '*')",
    ),
    Case(
        id="functions.trim-characters",
        description="LTRIM, RTRIM and TRIM with characters to remove",
        sql="select ltrim('xxa', 'x') as l, rtrim('axx', 'x') as r, trim('xax', 'x') as t",
    ),
    Case(
        id="functions.substrings",
        description="SUBSTR, SUBSTRING with a negative start, LEFT and RIGHT",
        sql="select substr('hello', 2, 3), substring('hello', -3), left('hello', 2), right('hello', 2)",
    ),
    Case(
        id="functions.position-contains",
        description="POSITION, CHARINDEX with a start, CONTAINS, STARTSWITH and ENDSWITH",
        sql="select position('l' in 'hello'), charindex('l', 'hello', 4), contains('hello', 'ell'), "
        "startswith('hello', 'he'), endswith('hello', 'lo')",
    ),
    Case(
        id="functions.replace-repeat-reverse",
        description="REPLACE, REPEAT, REVERSE and TRANSLATE",
        sql="select replace('aXbX', 'X', '-'), repeat('ab', 2), reverse('abc'), translate('abc', 'ab', 'xy')",
    ),
    Case(
        id="functions.like-ilike",
        description="LIKE, ILIKE, LIKE ANY and an escape",
        sql="select 'Abc' like 'a%', 'Abc' ilike 'a%', 'abc' like any ('x%', 'a%'), 'a_c' like 'a\\\\_c'",
    ),
    Case(
        id="functions.to-varchar-numbers",
        description="TO_VARCHAR of numbers",
        sql="select to_varchar(1.5) as a, to_varchar(10) as b",
    ),
    Case(
        id="functions.to-char-number-format",
        description="TO_CHAR of a number with a format",
        sql="select to_char(1234.5, '999,999.00') as f",
        expected=Expected([UNFILED], "TO_CHAR ignores a number format"),
    ),
    Case(
        id="functions.md5",
        description="MD5 of a string",
        sql="select md5('a') as m",
    ),
    Case(
        id="functions.sha2",
        description="SHA2 of a string, 256 and 512 bits",
        sql="select sha2('a') as a, sha2('a', 512) as b",
        expected=Expected([UNFILED], "SHA2 isn't implemented"),
    ),
    # --- aggregates
    Case(
        id="functions.count-if-bool-agg",
        description="COUNT_IF, BOOLAND_AGG and BOOLOR_AGG",
        sql="select count_if(x > 1), booland_agg(x > 0), boolor_agg(x > 2) "
        "from (select column1 as x from values (1), (2))",
        expected=DECIMAL_NOT_INT,
    ),
    Case(
        id="functions.min-by-max-by",
        description="MIN_BY and MAX_BY",
        setup=NAMES,
        sql="select min_by(s, g), max_by(s, g) from f_names where s is not null",
    ),
    Case(
        id="functions.median-mode",
        description="MEDIAN and MODE of integers",
        sql="select median(x), mode(x) from (select column1 as x from values (1), (2), (2), (4))",
        expected=FLOAT_NUMBERS,
    ),
    Case(
        id="functions.generator",
        description="TABLE(GENERATOR(ROWCOUNT => 3)) with SEQ4",
        sql="select seq4() from table(generator(rowcount => 3)) order by 1",
    ),
]
