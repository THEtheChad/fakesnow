"""Query shapes: FLATTEN and LATERAL, window functions and QUALIFY, MERGE, GROUP BY ALL."""

from probes import Case, Compare, Expected
from probes.cases._gaps import DECIMAL_NOT_INT, SUM_FLOAT

FLAT = [
    "create or replace table q_flat (id int, tags variant)",
    """insert into q_flat select 1, parse_json('["a", "b"]') union all select 2, parse_json('[]')""",
]
WIN = [
    "create or replace table q_win (g varchar, v int)",
    "insert into q_win values ('a', 1), ('a', 2), ('b', 3), ('b', 3)",
]
MERGE = [
    "create or replace table q_tgt (id int, v varchar)",
    "create or replace table q_src (id int, v varchar)",
    "insert into q_tgt values (1, 'old'), (2, 'gone')",
    "insert into q_src values (1, 'new'), (2, null), (3, 'added')",
]

CASES = [
    # --- FLATTEN
    Case(
        id="queries.flatten-array",
        description="TABLE(FLATTEN(...)) of an array: VALUE and INDEX",
        sql="select f.value, f.index from table(flatten(input => parse_json('[1, 2]'))) f order by f.index",
    ),
    Case(
        id="queries.flatten-object",
        description="FLATTEN of an object: KEY and VALUE",
        sql="""select f.key, f.value from table(flatten(parse_json('{"b": 1, "a": 2}'))) f order by f.key""",
    ),
    Case(
        id="queries.flatten-all-columns",
        description="All of FLATTEN's columns: SEQ, KEY, PATH, INDEX, VALUE, THIS",
        sql="select seq, key, path, index, value, this from table(flatten(parse_json('[10]')))",
        expected=Expected(
            ["#487"], "FLATTEN's SEQ is a large arbitrary number; Snowflake numbers the input rows from 1"
        ),
    ),
    Case(
        id="queries.lateral-flatten",
        description="LATERAL FLATTEN over a table's column; an empty array gives no rows",
        setup=FLAT,
        sql="select t.id, f.value::string as tag from q_flat t, lateral flatten(input => t.tags) f order by 1, 2",
    ),
    Case(
        id="queries.lateral-flatten-outer",
        description="LATERAL FLATTEN with OUTER => TRUE keeps rows with an empty array",
        setup=FLAT,
        sql="select t.id, f.value::string as tag from q_flat t, lateral flatten(input => t.tags, outer => true) f "
        "order by 1, 2",
        expected=Expected(["#487"], "FLATTEN's OUTER => TRUE is ignored, so rows with an empty array are dropped"),
    ),
    Case(
        id="queries.flatten-nested-path",
        description="FLATTEN with PATH into a nested array",
        sql="""select f.value from table(flatten(input => parse_json('{"a": {"b": [7, 8]}}'), path => 'a.b')) f """
        "order by f.index",
        expected=Expected(["#487"], "FLATTEN's PATH => is ignored"),
    ),
    # --- window functions and QUALIFY
    Case(
        id="queries.qualify-row-number",
        description="QUALIFY ROW_NUMBER() keeps the top row per group",
        setup=WIN,
        sql="select g, v from q_win qualify row_number() over (partition by g order by v desc) = 1 order by g",
    ),
    Case(
        id="queries.qualify-alias",
        description="QUALIFY on a window function's alias",
        setup=WIN,
        sql="select g, v, row_number() over (partition by g order by v) as rn from q_win qualify rn = 1 order by g",
    ),
    Case(
        id="queries.window-rank",
        description="RANK and DENSE_RANK with ties",
        setup=WIN,
        sql="select g, v, rank() over (order by v), dense_rank() over (order by v) from q_win order by v, g",
        # Expression labels vary; this case checks values/types.
        compare=Compare(names=False),
    ),
    Case(
        id="queries.window-lag-lead",
        description="LAG and LEAD with a default",
        setup=WIN,
        sql="select v, lag(v) over (order by v, g), lead(v, 1, 0) over (order by v, g) from q_win order by v, g",
        # Expression labels vary; this case checks values/types.
        compare=Compare(names=False),
    ),
    Case(
        id="queries.window-running-sum",
        description="A running SUM over an INT column",
        refs=["#199"],
        setup=WIN,
        sql="select g, v, sum(v) over (partition by g order by v rows between unbounded preceding and current row) "
        "from q_win order by g, v",
        expected=SUM_FLOAT,
        # Expression labels vary; this case checks values/types.
        compare=Compare(names=False),
    ),
    Case(
        id="queries.window-count-partition",
        description="COUNT(*) OVER (PARTITION BY ...) and FIRST_VALUE",
        setup=WIN,
        sql="select g, v, count(*) over (partition by g), first_value(v) over (partition by g order by v) "
        "from q_win order by g, v",
        # Expression labels vary; this case checks values/types.
        compare=Compare(names=False),
    ),
    Case(
        id="queries.group-by-all",
        description="GROUP BY ALL",
        setup=WIN,
        sql="select g, count(*) from q_win group by all order by g",
        # Expression labels vary; this case checks values/types.
        compare=Compare(names=False),
    ),
    Case(
        id="queries.values-clause",
        description="SELECT from VALUES, with COLUMN1 and COLUMN2",
        sql="select column1, column2 from values (1, 'a'), (2, 'b') order by column1",
    ),
    Case(
        id="queries.limit-offset",
        description="LIMIT ... OFFSET",
        setup=WIN,
        sql="select v from q_win order by v limit 2 offset 1",
    ),
    # --- MERGE
    Case(
        id="queries.merge-update-insert",
        description="MERGE with WHEN MATCHED UPDATE and WHEN NOT MATCHED INSERT",
        setup=MERGE,
        sql=[
            "merge into q_tgt t using q_src s on t.id = s.id "
            "when matched then update set t.v = s.v when not matched then insert (id, v) values (s.id, s.v)",
            "select id, v from q_tgt order by id",
        ],
        expected=DECIMAL_NOT_INT,
    ),
    Case(
        id="queries.merge-delete",
        description="MERGE with WHEN MATCHED AND ... DELETE",
        setup=MERGE,
        sql=[
            "merge into q_tgt using q_src on q_tgt.id = q_src.id "
            "when matched and q_src.v is null then delete when matched then update set v = q_src.v",
            "select id, v from q_tgt order by id",
        ],
        expected=DECIMAL_NOT_INT,
    ),
    Case(
        id="queries.merge-duplicate-source",
        description="MERGE fails when two source rows update the same target row",
        setup=[*MERGE, "insert into q_src values (1, 'again')"],
        sql="merge into q_tgt using q_src on q_tgt.id = q_src.id when matched then update set v = q_src.v",
        expected=Expected(
            ["#492"], "MERGE doesn't fail when two source rows match one target row (Snowflake: 100090 Duplicate row)"
        ),
    ),
    Case(
        id="queries.merge-aliases-matched-only",
        description="MERGE with table aliases and only a WHEN MATCHED clause",
        setup=MERGE,
        sql=[
            "merge into q_tgt t using q_src s on t.id = s.id when matched then update set t.v = s.v",
            "select id, v from q_tgt order by id",
        ],
        expected=Expected(
            ["#492"], "MERGE with a source alias and no WHEN NOT MATCHED clause fails: binder error on the alias"
        ),
    ),
]
