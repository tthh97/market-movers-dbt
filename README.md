# Market Movers - a dbt analytics pipeline on Snowflake

**[Live dashboard &rarr;](https://tthh97.github.io/market-movers-dbt/)** - a fresh snapshot is published on every build; the landing page keeps the full archive.

A small, **fully reproducible** analytics-engineering project: pull daily prices
for a watchlist, model them through staging → intermediate → marts in dbt, and
surface *movers*, *momentum/drawdown*, and a *portfolio-bias* view. The nightly
runs against **Snowflake** with key-pair auth and least-privilege roles; the same
models also run on **DuckDB** with **zero credentials**, so you can clone it and
`dbt build` without an account.

The watchlist holds 18 names grouped into four sectors - **tech**, **industrials**,
**financials**, and **crypto** - plus two benchmarks (SPY, QQQ) kept as reference
series, for 20 tracked tickers total. A few names are flagged as personal holdings
(NVDA, CAT, JPM, BTC) for the portfolio-bias view. The marts answer: *what's moving,
which sector is hot, and how does my book behave relative to the Nasdaq?* Edit
`seeds/watchlist.csv` to change the universe.

> New: on a failed nightly build, an **assisted-triage** step captures a clean
> failure report for a human to act on. See
> [docs/pipeline-explainer.md](docs/pipeline-explainer.md) for the lineage, a
> plain-language walkthrough of every step, and a runnable failure demo.

## Stack

`yfinance` (ingest) → **Snowflake** (warehouse) or **DuckDB** (offline, single
file) → **dbt** (models + tests + freshness) → **GitHub Actions** (scheduled
daily refresh, with assisted triage on failure).

One switch, `DBT_TARGET`, picks the warehouse; `ingest.py` and the models are
identical either way.

## Architecture

```
yfinance ────────> raw.prices ────────> stg_prices ──────┐
(ingest.py)        (warehouse)                           ├──> fct_prices ──> int_daily_returns ──> int_latest_daily_returns ──┬──> mart_movers ──> mart_sector_overview
seeds/watchlist ───────────────────────> stg_watchlist ──┘   (incremental fact)                                               ├──> mart_momentum
                                                                                                                              └──> mart_portfolio_bias
```

- **staging** - typed, renamed, de-duplicated (views)
- **fct_prices** - incremental fact, one row per ticker per day (delete+insert on a surrogate key)
- **intermediate** - daily returns (1d/5d/1m), 5/20-day MAs, running peak, drawdown, plus a
  "latest row per ticker" view every mart's current-snapshot logic shares
- **marts** - analysis-ready tables (movers, momentum, portfolio bias, sector overview)

## Quickstart

```bash
pip install -r requirements.txt

# Option A - offline demo (synthetic data, no credentials needed)
python scripts/seed_sample.py
dbt build --profiles-dir .

# Option B - real market data
python ingest.py --period 6mo     # idempotent upsert into raw.prices
dbt build --profiles-dir .

# Explore
dbt docs generate --profiles-dir . && dbt docs serve --profiles-dir .
```

Inspect a mart directly:

```bash
python -c "import duckdb; print(duckdb.connect('market.duckdb').sql('select * from mart_movers order by ret_1d desc').df())"
```

## Project layout

Model folders are numbered in build order, because the layer names alone did not
predict it: `fct_prices` is a fact table but builds *before* the intermediate
models, since everything downstream reads from it rather than from staging.

```
.github/workflows/daily.yml   # the only entry point: offline check -> Snowflake refresh -> triage
ingest.py                     # real yfinance → raw.prices (idempotent)
warehouse.py                  # shared connection helper, so loader and models agree on the target
scripts/seed_sample.py        # synthetic → raw.prices (offline/demo)
scripts/inject_fault.py       # breaks the build on purpose, to demo triage (CI-guarded)
scripts/build_viz.py          # marts → one self-contained HTML dashboard (no CDN, no model call)

seeds/watchlist.csv           # node 1  - the tracked universe + holding/benchmark flags
models/01_staging/            # nodes 4-13  - stg_prices, stg_watchlist, sources + freshness
models/02_fact/               # node 14     - fct_prices, the incremental history everything reads
models/03_intermediate/       # nodes 18-23 - int_daily_returns, int_latest_daily_returns
models/04_marts/              # nodes 24-36 - mart_movers, mart_momentum, mart_portfolio_bias, mart_sector_overview
tests/                        # node 6      - singular test: no non-positive close prices

scripts/triage/               # runs only when a build fails:
  _triage_common.py           #   shared JSON-load/skip helpers
  capture_failure.py          #   writes failure_context.json (no AI, session 1)
  diagnose_failure.py         #   one Claude API call → proposed diagnosis (session 2)
  propose_fix.py              #   opens a human-approval GitHub issue in CI (session 3)

agent/                        # reads the marts, never writes to them:
  agent.py tools.py policy.py #   ask a question in English, get an answer plus the SQL behind it
  evals.py                    #   12 golden questions, expectations evaluated at run time
  report.py claims.py         #   the weekly report: writer → matcher → verifier → publication gate
  report_evals.py             #   the gate's own evals; the offline tier needs no key and no warehouse
```

## Design choices worth talking through

- **The dashboard is deterministic; only the narrative is written by a model.**
  `build_viz.py` computes every chart and figure in SQL and Python, so the page
  renders whether or not any model call succeeds. The prose block above the
  charts comes from `agent/report.py`, and only ever from a report that already
  passed the publication gate: a blocked draft has no path onto the page, not a
  degraded one. The page also refuses a narrative written against a different
  warehouse than the charts, so production prose can never sit on synthetic
  numbers.
- **Two surfaces, one generator.** The public Pages site is built from the
  synthetic seed - no credentials in the deploy job, no real positions on the
  open web - and says so in a banner. The same script renders production from
  Snowflake in the nightly build, where the output stays a private artifact.

- **Idempotent ingest.** Keyed on `(ticker, trade_date)`, so a same-day re-run
  refreshes rather than duplicates: a `MERGE` from a staging table on Snowflake,
  `INSERT OR REPLACE` on DuckDB. Prices are also validated on the way in, and a
  row with no usable close is dropped and counted rather than backfilled, since a
  fabricated close would be indistinguishable from a real one downstream.
- **Incremental fact.** `fct_prices` reprocesses only from the latest stored day
  onward, with delete+insert on a `ticker|date` surrogate key - the same
  incremental discipline used in production loads.
- **Tested + monitored.** 26 data tests - not_null, unique, accepted_values, a
  `relationships` FK from the fact to the watchlist, and a singular no-negative-close
  test - plus source-freshness thresholds.
- **Assisted-triage on failure.** When the nightly `dbt build` fails, three
  failure-only CI steps run in sequence: `scripts/triage/capture_failure.py` (no AI) turns
  dbt's artifacts into one clean `failure_context.json`; `scripts/triage/diagnose_failure.py`
  makes a single Claude API call for a **structured** diagnosis (likely cause,
  proposed fix, confidence, and safety flags); `scripts/triage/propose_fix.py` surfaces that
  diagnosis as a GitHub issue so a human is actually notified. It proposes; a human
  approves. Set `ANTHROPIC_API_KEY` (a `.env` locally - see `.env.example` - or a CI
  secret); the diagnosis step skips cleanly if it's unset. Never auto-fixes, never
  writes to `main`.
- **Portfolio-bias mart.** `mart_portfolio_bias` computes each holding's
  correlation to the Nasdaq (QQQ) and its current drawdown - a quick check on
  how much your "diversified" book is really just one beta.

## Demo: watch the triage layer catch, diagnose, and propose a fix

The assisted-triage layer is easiest to believe when you see it fire. Three
realistic faults can be injected on demand, so `main` stays green and no fault
ever lives on a branch:

| Fault | What it simulates | Test that catches it |
| --- | --- | --- |
| `accepted_values` | `sector` accepted-values list narrowed to drop `crypto`, so BTC-USD, ETH-USD and SOL-USD fail. Data drift: the warehouse gained a category the contract was never told about. | `accepted_values_stg_watchlist_sector` |
| `dup_ticker` | A second NVDA row in the watchlist. Upstream fault: the same instrument arriving twice from a source with no key. | `unique_stg_watchlist_ticker` |
| `renamed_column` | `int_daily_returns` renames a column mid-CTE, so the outer select references one that no longer exists. Schema drift: an upstream rename nobody propagated. | none - it errors at build time |

This used to be three long-lived `demo/*` branches. They had to be rebased after
every structural change, drifted stale, and left an open PR that put an
intentional break one click from `main`. That is not hypothetical: PR #7 was
merged by accident and had to be reverted. A dispatch input cannot be merged.

### Run it locally (about two minutes)

```bash
python scripts/inject_fault.py accepted_values --force  # breaks your working tree
DBT_TARGET=duckdb python scripts/seed_sample.py         # offline, no credentials
DBT_TARGET=duckdb dbt build --profiles-dir .            # RED:  PASS=22 ERROR=1 SKIP=13
python scripts/triage/capture_failure.py                # writes failure_context.json (no AI)
python scripts/triage/diagnose_failure.py               # one Claude call -> diagnosis.json
python scripts/triage/propose_fix.py                    # renders the human-approval report
git checkout -- .                                       # undo the fault
```

`--force` is required locally because the script edits tracked files in place.
CI does not need it, since `CI` is already set there. If the anchor text it
looks for has moved, the script exits non-zero rather than doing nothing: a
silent no-op would produce a green build and look like triage had missed a fault
that was never actually injected.

Applying the proposed fix instead of `git checkout` rebuilds green at `PASS=36`.

### Run it in CI (opens a real GitHub issue)

With `ANTHROPIC_API_KEY` set as a repo secret, dispatch the nightly workflow and
pick a fault:

```bash
gh workflow run daily.yml -f inject_fault=accepted_values
```

The run shows the whole shape of the pipeline in one place:

| Job | Outcome | Why |
| --- | --- | --- |
| `build-duckdb` | **failure** | The offline stage catches the fault with no credentials and no warehouse spend |
| `build-snowflake` | **skipped** | It is gated on the offline stage, so a known-broken build never reaches the warehouse |
| `triage` | **success** | Runs on `failure()` of either stage, so it still fires and files the diagnosis |

That skip is the point: the cheap check fails first, so production data is never
touched by a build already known to be broken.

**Live examples**, both auto-authored by `github-actions`, each carrying a structured
diagnosis, a confidence level, safety flags, and a human-approval checklist:

- `accepted_values` → [issue #6](https://github.com/tthh97/market-movers-dbt/issues/6)
  from [this run](https://github.com/tthh97/market-movers-dbt/actions/runs/30170585086)
- `dup_ticker` → [issue #9](https://github.com/tthh97/market-movers-dbt/issues/9)
  from [this run](https://github.com/tthh97/market-movers-dbt/actions/runs/30170684064)
- `renamed_column` → [issue #10](https://github.com/tthh97/market-movers-dbt/issues/10)
  from [this run](https://github.com/tthh97/market-movers-dbt/actions/runs/30171213913)

Those runs predate the switch to injected faults and were dispatched from the
old demo branches. The fault is identical either way, and everything from
`capture_failure.py` onward is unchanged.


The first two are test failures (`PASS=22 ERROR=1 SKIP=13`); the third errors at
build time, so the whole mart layer skips (`PASS=17 ERROR=1 SKIP=18`). Worth
comparing the diagnoses: the model error comes back **high** confidence with the
offending line named, while the two data-drift faults come back **medium** and ask
for a read-only inspection first. A code bug is knowable from the artifacts; a
surprising data value is not.

The pipeline never edits code and never self-heals; it proposes a fix for a human to
approve, and always leaves `main` untouched.



## Notes / honest caveats

- This is a **portfolio project**, not production. The marts are descriptive
  analytics, **not** trading or investment advice.
- History is durable on Snowflake, where `fct_prices` merges incrementally across
  runs. The DuckDB path is rebuilt from scratch each time and is meant for offline
  demos and CI logic checks, not as a record.
- CI authenticates to Snowflake as a dedicated `SERVICE` user with no
  `ACCOUNTADMIN` path, using key-pair auth and a different role per step: one that
  can write the raw landing schema, another that can build the analytics one.
  Warehouse identifiers come from CI secrets rather than source.
- yfinance is an unofficial Yahoo Finance interface; for anything you depend on,
  swap in a keyed provider (Alpha Vantage, Tiingo, Polygon) at the `ingest.py`
  layer without touching the models.

## Extension ideas

- Add `dbt_utils` + `dbt_expectations` for richer tests (recency, value ranges).
- Add a thin Streamlit or Power BI layer on top of the marts.
- Snapshot the watchlist with dbt snapshots to track changes over time.
