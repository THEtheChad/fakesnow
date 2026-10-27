# probes

Cheap checks that run the same statements against real Snowflake and fakesnow, and report where they differ.

fakesnow's own tests say what fakesnow does. Probes say whether that's what Snowflake does: each case runs on
Snowflake and on fakesnow (in-process via `fakesnow.patch`, and through `fakesnow.server()` with the real
connector over HTTP), and the outcomes are compared. Adding a case is how you find out whether a piece of SQL
behaves the same, and keeping it is how you notice when that changes.

`probes/` isn't part of the `fakesnow` package and isn't shipped in its wheel. Its own tests (`probes/tests`) run
with the repo's pytest suite and don't need Snowflake.

## Running

From a fakesnow checkout with its virtualenv:

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
```

| option | |
|---|---|
| `PATH ...` | case files or directories (default: `probes/cases`) |
| `--reference` | `snowflake` (default), `fakesnow` or `fakesnow-server` |
| `--target` | repeatable; default `fakesnow` and `fakesnow-server` |
| `--baseline JSON` | take the reference's outcomes from an earlier `--json` report |
| `-k PATTERN` | repeatable; `#123`, `owner/repo#123` or `unfiled` match references, `*?[` make an id glob, anything else is an id substring |
| `--markdown FILE`, `--json FILE` | reports; the terminal always gets a summary |
| `--list` | list the selected cases |
| `-v` | print each case as it runs |

Exit code: 0 when nothing differs unexpectedly, 1 on an unexpected difference or an error, 2 when the run itself
can't start (bad case file, Snowflake not configured or unreachable).

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

Secrets never reach the output: the terminal shows the user, role, warehouse, database and auth method, and the
reports show only Snowflake's version.

Each run creates `<database>.PROBES_<timestamp>_<random>` (which becomes the current schema) and drops it at the
end, also on failure, along with any schema or database whose name starts with the run's schema (that's how
cases name anything they create outside it). Snowflake sessions get `TIMEZONE = 'UTC'` (fakesnow's zone),
`STATEMENT_TIMEOUT_IN_SECONDS = 60` and `QUERY_TAG = 'fakesnow-probes'`. fakesnow runs in a database of the
same name, so names in outcomes line up.

### Cost

A full run is a few hundred statements on a few rows each: about three minutes against an X-Small warehouse,
most of it round trips and metadata. Keep it that way:

- one behaviour per case, a handful of statements, a few rows: no generators of thousands of rows, no loops of
  statements;
- use an X-Small warehouse with a short auto-suspend (the run's statement timeout stops anything that runs away);
- iterate on fakesnow against a `--baseline` rather than rerunning Snowflake; a new or changed case needs a new
  Snowflake run (cases that aren't in the baseline report an error), and so do results you report.

## Statuses

For each case and fakesnow target:

| status | meaning | fails the run |
|---|---|---|
| match | same outcome as the reference | no |
| unexpected difference | differs, and isn't marked | yes |
| expected difference | differs, and is marked as a known difference | no |
| expected difference now matches | marked, but matches: a fix landed, remove the mark | no |
| error | the case can't be judged: its setup or Python probe failed on the reference, the harness crashed, or (with `--baseline`) the case is new or changed since the recorded run | yes |

Expected differences only apply when the reference is Snowflake (or a recorded Snowflake run). With
`--reference fakesnow`, any difference between in-process and server mode is unexpected.

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
SQLSTATE) are compared. Put what isn't the point of the case in `setup`. Use `create or replace` and table names
prefixed with the area, since cases in a run share its schema. SQL may use `{database}` and `{schema}`; name
anything created outside the run's schema starting with `{schema}`, and drop it in `teardown` (statements always
run after the case, errors ignored).

`Compare` fields: `rows`, `types` (type codes) and `error_codes` (errno and SQLSTATE) are on by default;
`names` (column names, off because fakesnow names unaliased expressions differently), `scale`, `precision`
(precision, or a text column's length) and `error_message` are off; `normalise=False` turns the normalisations
below off.

### Python probes

For what SQL can't say (connector APIs, autocommit, write_pandas, several sessions), a case names a function
taking the connection and a `ProbeContext` and returning plain data, which is compared like rows:

```python
def other_session(conn, ctx: ProbeContext) -> dict:
    conn.cursor().execute("create or replace temporary table t (x int)")
    other = ctx.connect().cursor()  # another session in the run's schema, closed after the case
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

## Normalisations

Kept to differences in Python representation that aren't differences in behaviour; each is documented in
`probes/compare.py`:

1. Timezone-aware datetimes are compared as instants (offsets aren't compared; `timestamps.tz-offset-kept`
   compares them with `normalise=False`). A naive datetime never equals an aware one.
2. The text of VARIANT, OBJECT and ARRAY columns (by Snowflake's type code) is compared as JSON tokens, so
   whitespace doesn't count (Snowflake indents, fakesnow doesn't; upstream's tests normalise the same way).
   Key order and how numbers are written still count. `semis.formatting` keeps the raw text visible.
3. Run-specific names (the run's database and schema) become `{database}` and `{schema}` when outcomes are
   captured, so outcomes from different runs compare. This replaces any identical name, e.g. a role named
   like the database.

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
| `outcome.py` | capturing results and errors, and saving them as JSON |
| `compare.py` | normalisation, comparison and statuses |
| `report.py`, `cli.py` | terminal, Markdown and JSON reports; `python -m probes` |
| `cases/` | the cases, by area |
| `tests/` | the harness's own tests |

## Limitations

- A recorded outcome is reused only for the same case: its SQL, or for a Python probe its whole module (parsed,
  so formatting and comments don't count).
- `fakesnow.server()` can only be called once per process (importing `fakesnow.server` shadows the function),
  so probes keeps a reference to it from import time.
- Runs are sequential, one target after another, in one session per target (plus any a probe opens).
