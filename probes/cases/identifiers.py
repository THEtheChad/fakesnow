"""How names resolve: quoted and unquoted, in tables, columns, joins, CTEs and aliases."""

from probes import Case, Compare
from probes.cases._gaps import CASE_INSENSITIVE

MIXED = [
    'create or replace table "IdentMixed" ("Id" int, "Name" varchar)',
    "create or replace table ident_plain (id int, name varchar)",
    """insert into "IdentMixed" values (1, 'a'), (2, 'b')""",
    "insert into ident_plain values (1, 'x'), (3, 'y')",
]

CASES = [
    Case(
        id="identifiers.unquoted-name-lower-case-table",
        description="An unquoted name doesn't find a table created with a quoted lower-case name",
        refs=["#453"],
        setup=['create or replace table "ident_lower" (id int)'],
        sql="select * from ident_lower",
    ),
    Case(
        id="identifiers.quoted-mixed-case-column",
        description="A quoted mixed-case name doesn't find a column created unquoted",
        refs=["#454"],
        setup=["create or replace table ident_upc (entrydate int)"],
        sql='select "EntryDate" from ident_upc',
    ),
    Case(
        id="identifiers.quoted-upper-case-is-unquoted",
        description="A quoted upper-case name and an unquoted name are the same name",
        setup=['create or replace table "IDENT_UPPER" ("ID" int)', "insert into ident_upper values (1)"],
        sql=["select id from ident_upper", 'select "ID" from "IDENT_UPPER"'],
    ),
    Case(
        id="identifiers.join-quoted-and-unquoted",
        description="A join between a quoted mixed-case table and an unquoted one",
        setup=MIXED,
        sql='select m."Id", m."Name", p.name from "IdentMixed" m join ident_plain p on m."Id" = p.id order by 1',
    ),
    Case(
        id="identifiers.join-unquoted-name-for-mixed-case-column",
        description="In a join, an unquoted name doesn't find a quoted mixed-case column",
        setup=MIXED,
        sql='select m.id from "IdentMixed" m join ident_plain p on m."Id" = p.id',
    ),
    Case(
        id="identifiers.join-using-quoted-upper-and-unquoted",
        description="JOIN ... USING matches a quoted upper-case column with an unquoted one",
        setup=[
            'create or replace table ident_u1 ("ID" int, a int)',
            "create or replace table ident_u2 (id int, b int)",
            "insert into ident_u1 values (1, 10), (2, 20)",
            "insert into ident_u2 values (1, 100)",
        ],
        sql="select id, a, b from ident_u1 join ident_u2 using (id) order by id",
    ),
    Case(
        id="identifiers.cte-quoted-lower-name-unquoted-reference",
        description="An unquoted reference doesn't find a CTE named with a quoted lower-case name",
        sql='with "cte" as (select 1 as x) select x from cte',
        expected=CASE_INSENSITIVE,
    ),
    Case(
        id="identifiers.cte-quoted-lower-name-quoted-reference",
        description="A quoted lower-case CTE name, referenced quoted",
        sql='with "cte" as (select 1 as x) select x from "cte"',
    ),
    Case(
        id="identifiers.cte-unquoted-name-quoted-upper-reference",
        description="An unquoted CTE and column, referenced by their quoted upper-case names",
        sql='with cte as (select 1 as x) select "X" from "CTE"',
    ),
    Case(
        id="identifiers.cte-quoted-lower-column-unquoted-reference",
        description="An unquoted reference doesn't find a CTE column aliased with a quoted lower-case name",
        sql='with c as (select 1 as "a") select a from c',
    ),
    Case(
        id="identifiers.cte-quoted-lower-column-quoted-reference",
        description="A CTE column aliased with a quoted lower-case name, referenced quoted",
        sql='with c as (select 1 as "a") select "a" from c',
    ),
    Case(
        id="identifiers.cte-joined-to-table",
        description="A CTE with a quoted mixed-case column joined to an unquoted table",
        setup=MIXED,
        sql='with c as (select "Id" as "CteId" from "IdentMixed") select c."CteId", p.name '
        'from c join ident_plain p on c."CteId" = p.id',
    ),
    Case(
        id="identifiers.subquery-alias-case",
        description="An unquoted subquery alias is referenced in another case",
        sql="select T.x from (select 1 as x) t",
    ),
    Case(
        id="identifiers.unquoted-alias-quoted-lower-reference",
        description="A quoted lower-case reference doesn't find an unquoted alias",
        sql='select "t".x from (select 1 as x) t',
        expected=CASE_INSENSITIVE,
    ),
    Case(
        id="identifiers.result-column-names",
        description="Result column names: unquoted aliases upper-cased, quoted ones kept as written",
        compare=Compare(names=True),
        sql='select 1 as abc, 2 as "Abc", 3 as "a b"',
    ),
    Case(
        id="identifiers.table-column-names",
        description="select * names columns as created: unquoted upper-cased, quoted kept",
        compare=Compare(names=True),
        setup=['create or replace table ident_names (lower_col int, "MixedCol" int, "UPPER" int)'],
        sql="select * from ident_names",
    ),
    Case(
        id="identifiers.three-part-name",
        description="A fully qualified table name",
        setup=["create or replace table ident_q (x int)", "insert into ident_q values (1)"],
        sql=["select x from {database}.{schema}.ident_q", 'select x from "{database}"."{schema}"."IDENT_Q"'],
    ),
    Case(
        id="identifiers.identifier-function",
        description="IDENTIFIER() with a table name in a string",
        setup=["create or replace table ident_q (x int)", "insert into ident_q values (1)"],
        sql="select x from identifier('ident_q')",
    ),
    Case(
        id="identifiers.dollar-sign",
        description="Unquoted names may contain $",
        setup=["create or replace table ident$d (a$b int)", "insert into ident$d values (1)"],
        sql="select a$b from ident$d",
    ),
    Case(
        id="identifiers.quoted-spaces",
        description="Quoted table and column names with spaces",
        setup=['create or replace table "ident with space" ("a col" int)', 'insert into "ident with space" values (1)'],
        sql='select "a col" from "ident with space"',
    ),
    Case(
        id="identifiers.quoted-name-with-double-quote",
        description='A quoted name containing an escaped double quote ("")',
        setup=['create or replace table "ident""q" (x int)', 'insert into "ident""q" values (1)'],
        sql='select x from "ident""q"',
    ),
    Case(
        id="identifiers.order-by-alias",
        description="ORDER BY an unquoted alias referenced in another case",
        sql="select column1 as v from values (2), (1) order by V",
    ),
]
