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
`<run schema>_C<hash>` (see [Isolation](#isolation)). The run drops its schema at the end, also on exceptions and
KeyboardInterrupt, using an independent cleanup connection, along with any schema or database whose name starts
with the literal run schema prefix (that's how cases name anything they create outside their own schema). Snowflake sessions get `TIMEZONE = 'UTC'` (fakesnow's zone),
`STATEMENT_TIMEOUT_IN_SECONDS = 60`, `QUERY_TAG = 'fakesnow-probes'` and `AUTOCOMMIT = TRUE`, all set in the session
at login (so the runner turning autocommit back on after a case isn't a leak). fakesnow runs in a database of the
same name, so names in outcomes line up. Database names must be simple unquoted SQL identifiers.

A live run needs the probe database to itself: nothing else should create or drop schemas in it during the run.
The leak check lists every schema in the database, so one another session creates or drops while a case runs is
reported against that case, as a schema "not named after the run", and makes the case an error. The runner never
drops or recreates such a schema, and the error says another session could have been the cause.
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

Isolation adds two statements per case on Snowflake (create and drop the case's schema), three SHOWs after a case
that could change a schema, database or temporary table, and three more round trips (two SHOWs and
`CURRENT_SECONDARY_ROLES()`) after one that could change the session; the runner also resets the session (rollback,
autocommit) only after those, and again after a teardown that could change it. For the 384 cases as of this
writing: 768 statements for the schemas, 117 and 99 after the 39 and 33 cases that need them, and 66 resets
instead of 768 (1050 in all). See [Isolation](#isolation).

## Statuses

For each case and fakesnow target:

| status | meaning | fails the run |
|---|---|---|
| match | same outcome as the reference | no |
| unexpected difference | differs without a mark, has no reviewed fingerprint, or the pinned behavior changed; a leak only on the target counts as a difference (when the reference's leaks were checked) | yes |
| expected difference | differs, and both outcomes match the reviewed fingerprint for this target | no |
| expected difference now matches | marked, but matches: a fix landed, remove the mark | no |
| error | the case can't be judged: its setup or Python probe failed on the reference, it leaked on Snowflake, the harness crashed, (with `--baseline`) the case is new or changed since the recorded run, or it leaked on the target and the recorded reference's leaks weren't checked | yes |

Expected differences only apply when the reference is Snowflake (or a recorded Snowflake run). With
`--reference fakesnow`, any difference between in-process and server mode is unexpected, leaks included.

A leak (see [Isolation](#isolation)) is reported against the case that left it, naming what it left, and listed
in its own section of the terminal summary and the Markdown report, marked with what the runner did about it:
`undone`, `kept` (with `--keep-leaks`), `not undone` (it can't be: a dropped or replaced table, say), `undoing it
failed` (the undo statement raised), or `undoing it didn't take` (it ran, but the snapshot after it still found the
leak). A leak is `undone` only when that snapshot, which the runner takes anyway as the next case's "before", no
longer finds it, so the reports' "later cases ran without them", said only when every leak was undone, is checked
rather than assumed (for what the leak check lists: what a table holds, say, isn't). The JSON report records each
leak's fate; one recorded before fates were shows its leaks as recorded, claiming neither.

| leaked on | status |
|---|---|
| Snowflake (run now, or recorded with leak data) | error, whatever the targets did: the case must undo it in its teardown (for a schema not named after the run, the error says another session using the database could have left or dropped it instead) |
| a fakesnow target only | a difference, like any other: unexpected unless marked and pinned (the pin covers the leaks) |
| a fakesnow target, with a recorded reference from before the leak check | error: whether the reference leaked the same is unknown, so it can't be classified (or pinned) as a difference; rerun the case against the reference |

Cases that leak nothing keep their fingerprints and pins. For those that do, what's compared is what leaked and,
when the runner couldn't undo it, why: `(not undone)`, `(undoing it failed: <codes>)` or `(undoing it didn't
take)`; never `undone` or `kept`. So a pin holds with or without `--keep-leaks` for a leak that's undone, and for one
with no possible undo, which carries `(not undone)` either way. It doesn't for a leak whose undo fails or doesn't
take: with `--keep-leaks` the runner doesn't try, so it's compared without the suffix. The summary and the Markdown report end with what the leak check covered on each target, including "not
checked" for a recorded reference from before it.

## Isolation

Each case runs in a fresh schema, `<run schema>_C<hash>` (the first 8 hex digits of the SHA-256 of the case's id:
the same name in every run, order and selection, and as long as any other case's), created before its setup and
dropped (with everything in it) after its teardown, on every target. `{schema}` is that schema, the main session
is in it when the case starts, `ctx.connect()` opens sessions in it, and outcomes show it as `{schema}`, so
tables, views, stages, sequences and temporary tables never reach another case and need no teardown.

What dropping the schema doesn't undo still needs a teardown: schemas and databases a case creates outside it
(named starting with `{schema}`), and changes to the main session (`ALTER SESSION`, `SET`). No case may change the
session's role, secondary roles or warehouse: a case with `USE ROLE`, `USE SECONDARY ROLES` or `USE WAREHOUSE` in
its setup, statements or teardown doesn't load (a Python probe can't be checked that way; the leak check reports
it). The runner resets the rest: after a case that could have changed the session (see below) it rolls back and
turns autocommit on, before the teardown (so an aborted transaction can't stop it) and again after a teardown that
could have changed the session, and the next case's `CREATE SCHEMA` makes that case's schema current, whatever
`USE` came before.

After each case's teardown and schema drop, a leak check compares a snapshot of shared state with the one from
before the case (PolDet's approach, Gyori et al., ISSTA 2015). Anything new, gone or changed is a leak: the
runner undoes what it can, checks in the next snapshot that each undo took, and reports it against the case.
Something gone (a schema, database or temporary table that was there before the case, left by an earlier one), or
a temporary table replaced under the same name (dropped and created again, or `CREATE OR REPLACE`), is reported but
never undone: what it held went with it, and the runner never tries to put its rows back.

| | Snowflake | fakesnow, fakesnow-server |
|---|---|---|
| schemas in the run's database (not other runs') | `SHOW TERSE SCHEMAS` | yes |
| databases named after the run | `SHOW TERSE DATABASES LIKE` | yes, but fakesnow can't drop a database, so one left behind stays (reported as `undoing it failed`, or `kept` with `--keep-leaks`) |
| the main session's temporary tables, by name and identity (one replaced under the same name is a change) | in the run's database, `SHOW TABLES` (`TERSE` lists a temporary table's kind as `TABLE`), identified by its `created_on` | yes, also those whose schema was dropped (see below), identified by DuckDB's table OID (kept by `INSERT` and `ALTER`, new on replacement) |
| session parameters, by value, type and whether set in the session (an inherited value set in the session, or one set in the session unset, is a leak even when the value stays the same) | `SHOW PARAMETERS IN SESSION`; one that was inherited (`level` not `SESSION`) is undone with `ALTER SESSION UNSET`, one set in the session with `ALTER SESSION SET` | no: fakesnow lists fixed values, and can't change any parameter but autocommit, which the runner resets |
| session variables (`SET`), by value (NULL included) and type | `SHOW VARIABLES`, undone with `UNSET`, or `SET` to a literal of the same type (`CAST(NULL AS VARCHAR)` for NULL: `SHOW VARIABLES` lists every NULL as text, whatever its type) | unchecked: fakesnow's `SHOW VARIABLES` lists none, even when some are set; the reports say so |
| role and warehouse (as the connector reports them, no round trip) | yes | no: fakesnow has neither |
| secondary roles | `CURRENT_SECONDARY_ROLES()` (a context function, no warehouse), only where parameters are listed; undone with `USE SECONDARY ROLES ALL` or `NONE` when that's what it was, else reported only | no: fakesnow has none |

A parameter or variable of a type the runner can't write a literal of (or a value unlike its type) is reported,
not undone.

Every SHOW on Snowflake is a metadata round trip (no warehouse needed), so there they run only after a case that
could change what they list, judged from its statements with comments, strings and quoted names stripped:

- a Python probe, or a statement the scan can't place (more than one statement, an unterminated string or comment,
  anything but a keyword first), could change anything;
- `SELECT`, `WITH`, `VALUES`, `SHOW`, `DESC[RIBE]`, `EXPLAIN`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `COPY`,
  `TRUNCATE`, `LIST`/`LS`, `GET`, `PUT`, `REMOVE`/`RM`, `COMMENT`, `COMMIT`, `ROLLBACK` and `USE` change nothing
  listed: none can set a parameter or variable, start a transaction or turn autocommit off, or create or drop a
  schema, database or table outside the case's schema (`USE` changes the current schema, which the next case
  resets). Except a `WITH` that mentions `PROCEDURE` or `CALL` (`WITH p AS PROCEDURE ... CALL p()` runs anything;
  none of the 6 cases with a `WITH` does), and `USE ROLE`, `USE SECONDARY ROLES` and `USE WAREHOUSE`, which no
  case may have but are judged to change anything anyway;
- `CREATE`, `DROP` and `UNDROP` can't change the session either, and change what's listed only when they name a
  schema, database or temporary table (`SCHEMA`, `DATABASE`, `TEMPORARY`, `TEMP` or `VOLATILE`);
- anything else could change anything: `ALTER` (`ALTER SESSION`, `USER` and `ACCOUNT` set parameters), `SET`,
  `UNSET`, `BEGIN`, `CALL`, `EXECUTE IMMEDIATE`, `DECLARE`, `GRANT`, ...

A `DROP TABLE` doesn't say whether its table is temporary, so while a temporary table the runner couldn't drop
is still there, Snowflake's snapshot lists objects after every case, to see if one dropped it.
fakesnow's snapshot is in-process (or local) and lists everything after every case. With `--keep-leaks`, Snowflake's
does too: a later case could undo a leak left in place without the snapshot noticing.

DuckDB keeps all temporary tables in one catalog, so fakesnow's outlive the schema they belong to (case
`ddl.drop-schema-drops-temporary-table`). When a case's schema is dropped, the runner drops the fakesnow
temporary tables created in it, as Snowflake would have, without reporting them (if that fails, the case's outcome
says so, and the leak check reports and undoes them). Any other temporary table is the leak check's, whether its
schema still exists (the run's, say: a leak on Snowflake too) or was dropped (a leak only on fakesnow, so a
difference), and is left in place with `--keep-leaks`: the runner never drops what isn't the current case's.
fakesnow's own temporary table (its column metadata) isn't listed: it's known by its OID, from before the run's
first statement, not by its `_fs_` prefix, which a case's table can share.

On Snowflake the runner only drops a leaked schema or temporary table that is named after the run (a temporary
table, by its schema): the database may be shared, a schema with another name may not be the run's, and `DROP
TABLE` drops a permanent table of the same name if the temporary one is gone. Not covered on any target: objects
outside the run's database not named after it (roles, warehouses, other databases' schemas), and what a temporary
table holds (rows a later case adds to one left in place aren't a change; replacing it is).

### Order-dependent cases

`--shuffle SEED` runs the cases in a random order from the seed, the same on every target, and puts the seed in
the summary and both reports (the JSON report also lists the order), so a run can be repeated exactly. With the
leak check, verdicts shouldn't depend on the order: if a case's does, something before it still reaches it.

`--find-polluter ID`, with the selection and order (`-k`, `--shuffle SEED`, `--order`) of the run where `ID`
changed, finds which cases before it change its outcome, like RSpec's `--bisect`. It runs the case alone, then
after the cases before it, and if its outcome changes, narrows those down by delta debugging to a smallest set
that still changes it, each trial on a fresh target (for `fakesnow-server`, a fakesnow instance of its own). "Changes" is the case's own comparison (a case comparing
types only isn't changed by other values). It runs on one fakesnow `--target` (default `fakesnow`), never
Snowflake, and prints the polluters, how they change the case, and a command that reproduces it with `--order`.
It runs with the leak check's cleanup, as a normal run does, so it finds what the check misses; add
`--keep-leaks` to find cases whose leaks the check already reports. With `--baseline`, it runs in the recorded run's
database, as a normal run against that baseline does (otherwise `PROBES`), and the command it prints keeps
`--baseline`.

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
  so probes keeps a reference to it from import time. Every server in a process serves one fakesnow instance
  (`fakesnow.server.shared_fs`), so the fakesnow-server target swaps in a fresh instance of its own while it's
  open: nothing one run leaves there (fakesnow can't drop a database) reaches the next, a `--find-polluter` trial
  say. On the way out it closes and forgets that instance's server sessions (the server keeps any it isn't told to
  delete), and no other. The swap isn't reentrant: two fakesnow-server targets open at once, other than nested,
  would serve each other's logins. Targets run one at a time.
- A recorded outcome is reused for a Python probe only while its module is unchanged, so marking a difference in a
  module with Python probes (editing `expected=`) needs those probes rerun against Snowflake.
- Runs are sequential, one target after another, in one session per target (plus any a probe opens).
