# Design note: one read-only `QueryRunner`

A one-page design for deepening the warehouse-access plumbing. Written to be
understood and explained out loud, not just implemented. See `CONTEXT.md` for
the "harness" vocabulary.

## The problem

"Run SQL against the warehouse and get rows back" is copy-pasted across five
places, each redoing connect + read-only check + row formatting:

- `agent/tools.py:95-157` (Snowflake read path)
- `agent/report.py:75-142` (its own DuckDB backend; reaches into `tools._is_read_only`)
- `scripts/build_viz.py:60-133` (a third backend pair)
- `agent/evals.py:130-144` - worse, it reverse-parses the model-facing text back
  into values (`lines[1].split(" | ")`)

Evidence it is real duplication: `tools.py:150` and `report.py:123` are
byte-identical formatter lines. On top of that, the Snowflake key-loading
(`_private_key_der` + env-require) is written three times
(`tools.py:39-79`, `warehouse.py:119-177`, `build_viz.py:68-95`).

## The design

### `QueryRunner` - the deep module (the whole interface is two things)

```python
@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]        # real values, not a string to re-parse
    truncated: bool          # True if we hit the row cap
    error: str | None        # None means success
    @property
    def is_error(self) -> bool:
        return self.error is not None

class QueryRunner:
    def __init__(self, engine: str):      # "snowflake" or "duckdb"
        ...
    def run(self, sql: str) -> QueryResult:
        ...
```

Behind that small surface: connecting, the read-only guard (SELECT/WITH only,
one statement), the row cap, fetching, building `rows`. A caller learns one
method and gets all of it - that is the **leverage**.

- **Seam = the `engine` argument.** Two real **adapters**, `snowflake` (prod) and
  `duckdb` (offline). Two adapters means the seam is real, not hypothetical.
- **Read-only guard lives inside `run`** - a check inside the only thing that
  touches the warehouse cannot be bypassed. DuckDB also opens `read_only=True`
  (engine-enforced); Snowflake leans on the REPORTER grants.

### `render_for_model` - the model-facing text moves out

```python
def render_for_model(result: QueryResult) -> str:   # the "col | col" text shown to Claude
    ...
```

The agent does `render_for_model(runner.run(sql))`. `evals.py` does
`runner.run(sql).rows` and stops reverse-parsing text.

### `load_private_key` - one small helper (was written three times)

```python
def load_private_key(env_prefix: str = "SNOWFLAKE") -> bytes:
    """Read the PEM key path (+ optional passphrase) from env, return DER bytes."""
```

Used by the `QueryRunner` Snowflake adapter (reader) and by `warehouse.py`
(writer). One place to change when auth changes. It is an implementation detail,
not a public seam.

## Why this is a deep module

- **Deletion test:** delete `QueryRunner` and the connect/guard/format logic
  reappears across five callers. It earns its keep.
- **Small interface, lots behind it:** one method, `run(sql)`.
- **The interface is the test surface:** callers and tests cross the same seam.

## The payoff - the test story

The `duckdb` adapter is not a mock. It is a real second adapter against the
offline `market.duckdb`, opened `read_only=True`. Tests build
`QueryRunner("duckdb")`, call `.run(sql)`, and assert on `.rows` - no
credentials, the same code path the agent runs against Snowflake in prod.

## Scope

- **Core (ticket A):** `QueryRunner` + `QueryResult` + `render_for_model` in a
  new `query.py`; `load_private_key` in `warehouse.py`; delete the five copies.
- **Optional (ticket B):** unify the agent + report tool-loop into
  `run_conversation(system, first_message, on_turn) -> str`, where `on_turn` is
  the recorder (trace vs. evidence notebook). Do after ticket A. This supersedes
  `agent/agent.py:2-8`'s "deliberately visible loop" rationale, so it needs an
  ADR when done.

## Interview lines (the crib)

- **QueryRunner:** "Five places each connected, checked the SQL was read-only,
  ran it, and formatted rows - copy-pasted. I pulled it into one QueryRunner:
  give it SQL, get rows back, and the read-only guard is inside so nothing can
  bypass it. Snowflake in prod, DuckDB offline, same interface."
- **Key-loader:** "The code that loads the Snowflake key was pasted in three
  files. I made it one helper - one place to change when auth changes."
- **Testability:** "I didn't have to mock the warehouse. DuckDB is a real
  read-only version of the same interface, so my tests run the exact same code
  path the agent runs against Snowflake, just offline."
