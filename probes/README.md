# probes

Cheap checks that run the same statements against real Snowflake and fakesnow, and report where they differ.

fakesnow's own tests say what fakesnow does. Probes say whether that's what Snowflake does: each case runs on
Snowflake and on fakesnow (in-process via `fakesnow.patch`, and through `fakesnow.server()` with the real
connector over HTTP), and the outcomes are compared. Adding a case is how you find out whether a piece of SQL
behaves the same, and keeping it is how you notice when that changes.

`probes/` isn't part of the `fakesnow` package and isn't shipped in its wheel. Its own tests (`probes/tests`) run
with the repo's pytest suite and don't need Snowflake.

## Running

From a fakesnow checkout, run `make install` (Python 3.11+ and uv required), then
`export PATH="$PWD/.venv/bin:$PATH"`. The dev environment includes the server and pandas dependencies.
For another project's cases, run from this checkout or put it on `PYTHONPATH`; installing the fakesnow wheel
alone does not install `probes`.

```sh
# fakesnow in-process against fakesnow's server: no Snowflake needed
python -m probes --reference fakesnow --target fakesnow-server

# against Snowflake (configured below), with reports
python -m probes --markdown report.md --json snowflake-run.json

# later, without touching Snowflake: reuse that run's Snowflake outcomes
python -m probes --baseline snowflake-run.json --target fakesnow

# a subset: by reference, id glob or id substring
python -m probes -k '#477' -k 'semis.*' -k merge
python -m probes -k unfiled --list

# in a random order (the seed is printed and reported), to find cases that depend on what ran before them
python -m probes --baseline snowflake-run.json --shuffle random
# which cases before ddl.show-schemas, in that order, change its outcome
python -m probes --find-polluter ddl.show-schemas --shuffle 1234
```

| option | |
|---|---|
| `PATH ...` | case files or directories (default: `probes/cases`) |
| `--reference` | `snowflake` (default), `fakesnow` or `fakesnow-server` |
| `--target` | repeatable; default `fakesnow` and `fakesnow-server` |
| `--strict-metadata` | compare column names, scale and precision/length for every case (also works with a baseline) |
| `--baseline JSON` | take the reference's outcomes from an earlier `--json` report |
| `-k PATTERN` | repeatable; `#123`, `owner/repo#123` or `unfiled` match references, `*?[` make an id glob, anything else is an id substring |
| `--shuffle SEED` | run the cases in a random order from `SEED` (an integer, or `random` to pick one), the same order on every target |
| `--order ID,ID,...` | run exactly these cases, in this order |
| `--find-polluter ID` | find the cases before `ID` (file order, `--shuffle` or `--order`) that change its outcome, see below |
| `--keep-leaks` | report leaks but leave them in place, see below |
| `--markdown FILE`, `--json FILE` | reports; the terminal always gets a summary |
| `--list` | list the selected cases |
| `-v` | print each case as it runs |

Exit code: 0 when nothing differs unexpectedly, 1 on an unexpected difference or an error, 2 when the run itself
can't start (bad case file, Snowflake not configured or unreachable). With `--find-polluter`: 0 when it found
polluters, 1 when nothing before the case changes it.

### Connecting to Snowflake

Nothing connects to an account unless configured, and the variables are prefixed so that `SNOWFLAKE_*` settings
in your shell for other tools aren't picked up by accident:

| variable | |
|---|---|
| `PROBES_SNOWFLAKE_ACCOUNT` | account identifier (or use `PROBES_SNOWFLAKE_CONNECTION_NAME`) |
| `PROBES_SNOWFLAKE_CONNECTION_NAME` | a connection in the connector's `connections.toml` |
| `PROBES_SNOWFLAKE_USER` | |
| `PROBES_SNOWFLAKE_DATABASE` | required: the run creates its schema here |
| `PROBES_SNOWFLAKE_WAREHOUSE`, `PROBES_SNOWFLAKE_ROLE` | optional |
| `PROBES_SNOWFLAKE_PRIVATE_KEY_FILE` | key-pair auth (sets the authenticator to `SNOWFLAKE_JWT`) |
| `PROBES_SNOWFLAKE_PRIVATE_KEY_FILE_PWD` | the key's passphrase, if any |
| `PROBES_SNOWFLAKE_PASSWORD`, `PROBES_SNOWFLAKE_AUTHENTICATOR` | other auth |

Connection parameters are excluded from reports. Diagnostics redact configured account/host, password,
key-file path/passphrase and connector-recognised tokens. Cases are trusted Python/SQL: do not put secrets
in case source, returned data or SQL literals, which are intentionally included in reports.

Each run creates `<database>.PROBES_<timestamp>_<random>`, and each case runs in a schema of its own named after it,
`<run schema>_C<n>` (see [Isolation](#isolation)). The run drops its schema at the end, also on exceptions and
KeyboardInterrupt, using an independent cleanup connection, along with any schema or database whose name starts
with the literal run schema prefix (that's how cases name anything they create outside their own schema). Snowflake sessions get `TIMEZONE = 'UTC'` (fakesnow's zone),
`STATEMENT_TIMEOUT_IN_SECONDS = 60` and `QUERY_TAG = 'fakesnow-probes'`. fakesnow runs in a database of the
same name, so names in outcomes line up. Database names must be simple unquoted SQL identifiers.
Cleanup requires a working Snowflake connection; process termination or network/account outages can still
require manual cleanup of the printed run schema.

### Cost

A full run is a few hundred cases on a few rows each, plus setup/reset/cleanup statements: typically a few
minutes against an X-Small warehouse, most of it round trips and metadata. Keep it that way:

- one behaviour per case, a handful of statements, a few rows: no generators of thousands of rows, no loops of
  statements;
- use an X-Small warehouse with a short auto-suspend (the run's statement timeout stops anything that runs away);
- iterate on fakesnow against a `--baseline` rather than rerunning Snowflake; a new or changed case needs a new
  Snowflake run (cases that aren't in the baseline report an error), and so do results you report.

Isolation adds three statements per case on Snowflake (create and drop the case's schema, and the leak check's
snapshot), and one more after cases that could change a session parameter, see [Isolation](#isolation).

## Statuses

For each case and fakesnow target:

| status | meaning | fails the run |
|---|---|---|
| match | same outcome as the reference | no |
| unexpected difference | differs without a mark, has no reviewed fingerprint, or the pinned behavior changed; a leak only on the target counts as a difference | yes |
| expected difference | differs, and both outcomes match the reviewed fingerprint for this target | no |
| expected difference now matches | marked, but matches: a fix landed, remove the mark | no |
| error | the case can't be judged: its setup or Python probe failed on the reference, it leaked on Snowflake, the harness crashed, or (with `--baseline`) the case is new or changed since the recorded run | yes |

Expected differences only apply when the reference is Snowflake (or a recorded Snowflake run). With
`--reference fakesnow`, any difference between in-process and server mode is unexpected, leaks included.

A leak (see [Isolation](#isolation)) is reported against the case that left it, naming what it left, and listed
in its own section of the terminal summary and the Markdown report:

| leaked on | status |
|---|---|
| Snowflake (run now, or recorded with leak data) | error, whatever the targets did: the case must undo it in its teardown |
| a fakesnow target only | a difference, like any other: unexpected unless marked and pinned (the pin covers the leaks) |
| a fakesnow target, with a recorded Snowflake run from before the leak check | a difference: the reference counts as not having leaked |

Cases that leak nothing keep their fingerprints and pins.

## Isolation

Each case runs in a fresh schema, `<run schema>_C<n>` for the run's n-th case, created before its setup and
dropped (with everything in it) after its teardown, on every target. `{schema}` is that schema, the main session
is in it when the case starts, `ctx.connect()` opens sessions in it, and outcomes show it as `{schema}`, so
tables, views, stages, sequences and temporary tables never reach another case and need no teardown.

What dropping the schema doesn't undo still needs a teardown: schemas and databases a case creates outside it
(named starting with `{schema}`), and changes to the main session (`ALTER SESSION`, `USE ROLE`, `USE WAREHOUSE`).
The runner already resets the rest after every case: it rolls back, turns autocommit on, and the next case's
`CREATE SCHEMA` makes that case's schema current, whatever `USE` came before.

After each case's teardown and schema drop, a leak check compares a snapshot of shared state with the one from
before the case (PolDet's approach, Gyori et al., ISSTA 2015). Anything new, gone or changed is a leak: the
runner undoes what it can, so later cases aren't affected, and reports it against the case.

| | Snowflake | fakesnow, fakesnow-server |
|---|---|---|
| schemas in the run's database (not other runs') | yes | yes |
| databases named after the run | yes | yes, but fakesnow can't drop a database, so one left behind stays (reported as not undone) |
| session parameters (`SHOW PARAMETERS IN SESSION`) | after Python probes and SQL with `ALTER SESSION`, `CALL`, `EXECUTE IMMEDIATE`, `BEGIN` or `DECLARE`: each SHOW is a round trip | no: fakesnow lists fixed values, and can't change any parameter but autocommit, which the runner resets |
| role and warehouse (as the connector reports them, no round trip) | yes | no: fakesnow has neither |
| temporary tables | dropped with the schema | fakesnow keeps them after their schema is dropped (case `ddl.drop-schema-drops-temporary-table`): the runner drops the ones a case created, without reporting them |

On Snowflake the runner only drops a leaked schema that is named after the run: the database may be shared, and a
schema with another name may not be the run's. Not covered on any target: session variables (`SET`), and objects
outside the run's database not named after it (roles, warehouses, other databases' schemas).

### Order-dependent cases

`--shuffle SEED` runs the cases in a random order from the seed, the same on every target, and puts the seed in
the summary and both reports (the JSON report also lists the order), so a run can be repeated exactly. With the
leak check, verdicts shouldn't depend on the order: if a case's does, something before it still reaches it.

`--find-polluter ID`, with the selection and order (`-k`, `--shuffle SEED`, `--order`) of the run where `ID`
changed, finds which cases before it change its outcome, like RSpec's `--bisect`. It runs the case alone, then
after the cases before it, and if its outcome changes, narrows those down by delta debugging to a smallest set
that still changes it, each trial on a fresh target. "Changes" is the case's own comparison (a case comparing
types only isn't changed by other values). It runs on one fakesnow `--target` (default `fakesnow`), never
Snowflake, and prints the polluters, how they change the case, and a command that reproduces it with `--order`.
It runs with the leak check's cleanup, as a normal run does, so it finds what the check misses; add
`--keep-leaks` to find cases whose leaks the check already reports.

## Adding a case

Cases are Python data in files that define `CASES`, grouped by area (`identifiers.py`, `types.py`,
`semistructured.py`, ...). Files starting with `_` are helpers, not case files.

```python
from probes import UNFILED, Case, Compare, Expected

CASES = [
    Case(
        id="semis.array-construct-aliased",  # unique, lower case
        description="ARRAY_CONSTRUCT with an alias",
        refs=["#477"],  # upstream issues/PRs the case relates to
        setup=["create or replace table semis_t (a array)"],  # run first, not compared; Snowflake must accept them
        sql="select array_construct(1, 2) as a",  # one statement or a list; each is compared
        compare=Compare(scale=True),  # what to compare, see below
        expected=Expected(["#477"], "fakesnow can't describe an aliased ARRAY_CONSTRUCT"),
    ),
]
```

Statements run in order and stop at the first that fails; each one's rows and column types (or errno and
SQLSTATE) are compared. Put what isn't the point of the case in `setup`. Each case has a schema of its own (see
[Isolation](#isolation)); prefix table names with the area anyway, so outcomes and reports are easy to read. SQL
may use `{database}` and `{schema}`; name anything created outside the case's schema starting with `{schema}`,
and undo it in `teardown` (statements always run after the case, errors ignored), or the leak check reports it.

`Compare` fields: `rows`, `types` (type codes), `names` (column names), and `error_codes` (errno and SQLSTATE)
are on by default. Built-in expression-value cases explicitly use `names=False` where generated labels differ;
projects using DictCursor/dataframes should keep names enabled. `scale`, `precision` (precision or text/binary
length), and exact `error_message` are off by default. These omissions can hide metadata or message differences:
a **match means only the enabled checks match**, not full connector equivalence. Every report lists omitted
checks. Use `--strict-metadata` to override metadata omissions without changing cases. Existing pins then stop
applying because the comparison contract changed. `normalise=False` preserves raw JSON formatting too.

Rows are always compared in order, including duplicate multiplicity. Use SQL `ORDER BY` for deterministic
results; there is no implicit set/multiset sorting. Python mappings retain insertion order too. Python-returned
`ResultSet` values use the same flags as SQL. Exceptions compare their class and whether they originated
in the database, so a probe bug cannot
silently match an unsupported-operation error.

### Python probes

For what SQL can't say (connector APIs, autocommit, write_pandas, several sessions), a case names a function
taking the connection and a `ProbeContext` and returning plain data, which is compared like rows:

```python
def other_session(conn, ctx: ProbeContext) -> dict:
    conn.cursor().execute("create or replace temporary table t (x int)")
    other = ctx.connect().cursor()  # another session in the case's schema, closed after the case
    return {"other": ctx.query(other, "select x from t")}  # a ResultSet, or the ErrorInfo it raised


Case(id="transactions.temp-other-session", description="...", run=other_session)
```

`ctx` also has `execute` (None or the error), `attempt(fn)` (fn's result or its error), `write_pandas` (the
connector's, or fakesnow's when patched), `sql` (fills in placeholders), `target`, `database` and `schema`. An
exception from the probe is its outcome; on Snowflake, an exception that isn't the connector's means the probe
is broken (status error). After a Python probe the runner rolls back and turns autocommit back on.

### Marking a known difference

Mark a difference expected only once you've confirmed it against Snowflake and it's fakesnow's (or sqlglot's)
behaviour, not the case's. Give a reference and the reason in a few words:

- `#123`: an upstream fakesnow issue or PR; `tobymao/sqlglot#8804` for another repo's; or a URL;
- `UNFILED`: confirmed, but there's no issue yet. The Markdown report lists these as issue candidates, and
  `-k unfiled` selects them.

`Expected(..., targets=["fakesnow"])` limits a mark to one target, e.g. a gap only in-process. A case can have
one mark per set of targets. Shared reasons live in `cases/_gaps.py`.

### Pinning an expected outcome

An `Expected` reference/reason alone **never accepts a difference**. After inspecting a live run's Markdown
and complete JSON outcomes, copy that target verdict's `fingerprint` from the JSON report into `_expected.json`
beside your case files (or the case's `expected_outcomes` mapping):

```json
{
  "my-project.case-id": {
    "fakesnow": "<64-character fingerprint from the reviewed verdict>",
    "fakesnow-server": "<that target's fingerprint>"
  }
}
```

Commit this file with the cases. The fingerprint covers the case definition, comparison flags, and complete
compared data on **both** Snowflake and the candidate, without report truncation. A changed value, error,
setup failure, number/order of rows, or enabled metadata check becomes unexpected. Disabled checks and
normalised formatting are excluded. A complete fix still reports `expected difference now matches`.
Never regenerate pins blindly to make a run green: inspect changed outcomes and update the reason/reference
as needed. Python case module edits invalidate that module's pins; editing this sidecar does not.

## Normalisations

Kept to differences in Python representation that aren't differences in behaviour; each is documented in
`probes/compare.py`:

1. Timezone-aware datetimes compare their instant and UTC offset, regardless of tzinfo class.
   A naive datetime never equals an aware one.
2. The text of VARIANT, OBJECT and ARRAY columns (by Snowflake's type code) is compared as JSON tokens, so
   whitespace doesn't count (Snowflake indents, fakesnow doesn't; upstream's tests normalise the same way).
   Key order and how numbers are written still count. `semis.formatting` keeps the raw text visible.
3. Run-specific names (the run's database and the case's schema) become `{database}` and `{schema}` when
   outcomes are captured, so outcomes from different runs compare; the run's own schema, which a case only sees
   in listings, becomes `{run_schema}`. Uppercase names become those placeholders; lowercase names become
   `{schema:lower}` or `{database:lower}`, and mixed casing stays literal, so a changed identifier case remains
   visible. This replaces any identical name, e.g. a role named like the database.

Nothing else: `1`, `1.0` and `Decimal('1')` are different results, as are `bytes` and `bytearray`, because
callers' code behaves differently with them (#199). No int/Decimal normalisation turned out to be needed: every
such difference seen so far is one users hit.

## Cases for another project

The runner takes any case files or directories, so a project can keep probes for its own SQL next to its code
and run them from a fakesnow checkout:

```sh
cd fakesnow && python -m probes ../my-project/probes --markdown ../my-project/probes-report.md
```

Their files import `probes` like the ones here. Tables a query needs go in `setup`; keep the data to a few rows.

## From a case to an upstream issue

1. Reduce the case to the smallest statement that shows the difference, and confirm it against Snowflake
   (`-k <id> --markdown out.md`).
2. Check it's fakesnow's: reproduce it with `fakesnow.patch()` alone, and look for an open issue
   (`gh issue list -R tekumara/fakesnow`). If it's sqlglot's (parsing or generating), it may belong there.
3. The Markdown section for the case has both outcomes side by side; the issue needs a minimal reproduction, what
   Snowflake returns (say which version you checked), and why it matters.
4. Once filed, replace `UNFILED` with the issue's reference. When a fix lands, the case reports "expected
   difference now matches": remove the mark.

## Layout

| | |
|---|---|
| `model.py` | `Case`, `Compare`, `Expected`, and their validation |
| `loader.py` | finding and loading case files, `-k` selection |
| `targets.py` | Snowflake configuration, the three targets, the run's schema |
| `runner.py`, `context.py` | running cases, and what a Python probe gets |
| `isolation.py` | the leak check |
| `polluter.py` | `--find-polluter` |
| `outcome.py` | capturing results and errors, and saving them as JSON |
| `compare.py` | normalisation, comparison and statuses |
| `report.py`, `cli.py` | terminal, Markdown and JSON reports; `python -m probes` |
| `cases/` | the cases, by area |
| `tests/` | the harness's own tests |

## Limitations

- A recorded outcome is reused only for the same case: its SQL, or for a Python probe its whole module (parsed,
  so formatting and comments don't count). Imported helpers and dependency versions are not fingerprinted;
  regenerate live baselines and review pins when those change.
- `fakesnow.server()` can only be called once per process (importing `fakesnow.server` shadows the function),
  so probes keeps a reference to it from import time. Every server in a process shares one fakesnow instance, so
  the fakesnow-server target drops the run's schemas when it closes (as `--find-polluter` starts one per trial);
  a database a case leaves behind stays, as fakesnow can't drop one.
- Runs are sequential, one target after another, in one session per target (plus any a probe opens).
