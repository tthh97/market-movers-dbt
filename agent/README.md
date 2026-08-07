# The analytics agent

A read-only analytics agent over this project's Snowflake warehouse. Ask it a
question in English; it writes SQL, runs it, and explains the result.

It is the interactive third layer of the project, alongside the dbt pipeline and
the CI triage bot. All three share one rule: **they propose and describe, they
never write.**

```bash
cd agent
../.venv/bin/python agent.py "which sector moved most on the latest trading day?"
../.venv/bin/python evals.py    # 12 golden questions, expectations computed live
```

Run from this directory. `agent.py` and `evals.py` resolve both `.env` and their
sibling imports relative to their own file location, so the working directory has
to be `agent/`.

The root `requirements.txt` is a superset of what the agent needs, so the
project venv runs both the pipeline and the agent. The `.env` is **not** shared:
the agent connects as a dedicated read-only identity, never the transformer role
that builds the marts, and keeping the two credential files apart makes that
boundary a visible thing rather than a convention.

## The harness

| Component | File | What it owns |
|---|---|---|
| Tools | `tools.py` | `get_schema`, `run_sql`, and the guardrails inside them |
| Loop | `agent.py` | Turn cycle, stopping conditions, per-turn JSONL trace |
| Policy | `policy.py` | System prompt: deterministic SQL, no advice, date honesty |
| Skills | `skills/*.md` | Method files appended only when the question matches |
| Evals | `evals.py` | Golden questions whose answers come from the warehouse |

## The two rules

**Deterministic SQL computes; the LLM only narrates.** Every number the agent
states must come from a `run_sql` result in that conversation. No recalled
prices, no mental arithmetic. If a figure can't be queried, the agent says so.

**No investment advice.** It describes what the data shows. It does not
recommend, forecast, or characterise anything as a good or bad investment.

Both are enforced by eval cases (`policy-no-advice`, `policy-unknown-ticker`),
not just asserted in the prompt.

## Guardrails, weakest to strongest

1. **Statement shape** - `SELECT`/`WITH` only, one statement per call. Note that
   a naive `startswith("select")` check passes `select 1; drop table t`, so
   interior semicolons are rejected explicitly.
2. **Budgets** - 50 rows per result, 12 queries per question, 10 turns, 200K
   tokens. A confused agent stops being expensive quickly.
3. **Snowflake grants** - the real boundary. The agent connects as a dedicated
   `SERVICE` user with `DEFAULT_SECONDARY_ROLES = ()`, holding one read-only role
   (`USAGE` + `SELECT` on the analytics schema only). Verified: `CREATE TABLE`,
   `DELETE`, reading the raw landing schema, and `USE ROLE ACCOUNTADMIN` all fail
   with access-control errors.

Layers 1 and 2 exist so that obvious mistakes fail without waking the warehouse.
Layer 3 is what makes writes impossible.

## Data shape worth knowing

- 20 tickers: 15 equities across tech / industrials / financials, 3 crypto, and
  SPY + QQQ as **benchmarks, not holdings** - exclude them when ranking movers.
- **"Latest date" is not one date.** Crypto trades daily; equities and ETFs do
  not, and lag by a day or more. Always take the max from the rows you are
  reporting on, never a global `max(trade_date)`.
- The marts already carry volatility (`VOL_DAILY`), drawdown
  (`DRAWDOWN_FROM_PEAK`), moving averages, a volatility-scaled move
  (`DAILY_RETURN_Z`), and correlation to QQQ (`CORR_TO_NASDAQ`). What is missing
  is rolling-window volatility (30/60/90d) and cumulative period returns.

## Traces

One JSONL per run in `traces/`, one line per turn, plus a row in `summary.csv`.
Written as the run proceeds, so a hung run still leaves evidence. Each turn line
carries the stop reason, token counts, cache reads, and the tool calls made.

## A note on the eval suite

Expectations are SQL, not literals. The nightly refreshes the marts, so a
hardcoded "NVDA rose 3.43%" is wrong by morning and the suite starts failing for
reasons unrelated to the agent.

Three of the twelve cases failed on first run. All three were defects in the
*questions*, not the agent: two were ambiguous about which window or which
direction they meant, and one demanded an ISO date from an answer written in
prose. The lesson is in `evals.py` as a comment - an eval question must be at
least as precise as the query it is checked against.

## The weekly report agent

Same newsroom, different job: `report.py` produces the weekly column, with a
journalist and a fact-checker inside one program - two Claude calls with two
different job descriptions, run in sequence.

| Step | Who | What it owns |
|---|---|---|
| Write | LLM call 1 | Drafts the narrative from `run_sql` results; every query + rows saved to an evidence notebook |
| Match | `claims.py` (code) | Every number and date in the draft must trace to the notebook - rounding and percent/fraction scale allowed, nothing else |
| Verify | LLM call 2 | Fresh eyes, no memory of writing: checks direction words and superlatives claim-by-claim against the rows |
| Gate | `claims.py` (code) | Any unsupported claim blocks publication. One rewrite loop, then it flags. Fails **closed**: a draft whose verifier never ran does not publish |

The split matters: a model checking its own homework grades generously, and
two LLMs agreeing is not a harness. Code checks what code can check
(numbers); the second call checks only what code cannot (language).

```bash
python report.py                                  # Snowflake, same read-only role
REPORT_TARGET=duckdb python report.py             # offline demo, engine-enforced read_only
python report.py --inject-fault fabricated_number # watch the gate block it
python report_evals.py                            # the gate's own evals (offline tier needs no key)
```

The gate has its own evals (`report_evals.py`): fixture notebooks and
deliberately corrupted drafts - a fabricated figure, a fabricated date, a
flipped direction word, an unsupported "biggest" - each of which must be
blocked. The offline tier runs in CI (`weekly-report.yml`) as the cheap stage
gating the paid one, the same shape as `build-duckdb` gating
`build-snowflake`. Published reports land in `reports/` with the full query
appendix; a blocked run leaves a `*-gate-failure.json` instead.
