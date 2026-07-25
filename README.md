# Market Movers - a dbt analytics pipeline on Snowflake

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

```
ingest.py                     # real yfinance → raw.prices (idempotent)
scripts/seed_sample.py        # synthetic → raw.prices (offline/demo)
scripts/_triage_common.py     # shared JSON-load/skip helpers for the triage scripts
scripts/capture_failure.py    # on a failed build, writes failure_context.json (no AI, session 1)
scripts/diagnose_failure.py   # one Claude API call → proposed diagnosis (session 2)
scripts/propose_fix.py        # surfaces the diagnosis for human approval, opens a GitHub issue in CI (session 3)
seeds/watchlist.csv           # the tracked universe + holding/benchmark flags
models/staging/               # stg_prices, stg_watchlist, sources + freshness
models/intermediate/          # int_daily_returns, int_latest_daily_returns
models/marts/                 # fct_prices, mart_movers, mart_momentum, mart_portfolio_bias, mart_sector_overview
tests/                        # singular test: no non-positive close prices
.github/workflows/daily.yml   # nightly: offline check -> Snowflake refresh -> triage on failure
```

## Design choices worth talking through

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
  failure-only CI steps run in sequence: `scripts/capture_failure.py` (no AI) turns
  dbt's artifacts into one clean `failure_context.json`; `scripts/diagnose_failure.py`
  makes a single Claude API call for a **structured** diagnosis (likely cause,
  proposed fix, confidence, and safety flags); `scripts/propose_fix.py` surfaces that
  diagnosis as a GitHub issue so a human is actually notified. It proposes; a human
  approves. Set `ANTHROPIC_API_KEY` (a `.env` locally - see `.env.example` - or a CI
  secret); the diagnosis step skips cleanly if it's unset. Never auto-fixes, never
  writes to `main`.
- **Portfolio-bias mart.** `mart_portfolio_bias` computes each holding's
  correlation to the Nasdaq (QQQ) and its current drawdown - a quick check on
  how much your "diversified" book is really just one beta.

## Demo: watch the triage layer catch, diagnose, and propose a fix

The assisted-triage layer is easiest to believe when you see it fire. Three demo
branches each carry one realistic fault, so `main` always stays green:

| Branch | Fault | Test that catches it |
| --- | --- | --- |
| `demo/triage` | `sector` accepted-values list narrowed to drop `crypto`, so BTC-USD, ETH-USD and SOL-USD fail. Data drift: the warehouse gained a category the contract was never told about. | `accepted_values_stg_watchlist_sector` |
| `demo/dup-ticker` | A second NVDA row in the watchlist. Upstream fault: the same instrument arriving twice from a source with no key. | `unique_stg_watchlist_ticker` |
| `demo/renamed-column` | `int_daily_returns` selects `closing_price`, which `fct_prices` does not have. Schema drift: an upstream rename nobody propagated. | none - it errors at build time |

### Run it locally (about two minutes)

```bash
git switch demo/triage                                 # the branch that carries the fault
DBT_TARGET=duckdb python scripts/seed_sample.py        # offline, no credentials
DBT_TARGET=duckdb dbt build --profiles-dir .           # RED:  PASS=22 ERROR=1 SKIP=13
python scripts/capture_failure.py                      # writes failure_context.json (no AI)
python scripts/diagnose_failure.py                     # one Claude call -> diagnosis.json
python scripts/propose_fix.py                          # renders the human-approval report
```

Then apply the proposed fix (add `crypto` back to the list in
`models/staging/_staging.yml`) and rebuild to confirm green:

```bash
DBT_TARGET=duckdb dbt build --profiles-dir .           # GREEN: PASS=36
```

### Run it in CI (opens a real GitHub issue)

With `ANTHROPIC_API_KEY` set as a repo secret, dispatch the nightly workflow on a
demo branch:

```bash
gh workflow run daily.yml --ref demo/triage
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

- `demo/triage` → [issue #6](https://github.com/tthh97/market-movers-dbt/issues/6)
  from [this run](https://github.com/tthh97/market-movers-dbt/actions/runs/30170585086)
- `demo/dup-ticker` → [issue #9](https://github.com/tthh97/market-movers-dbt/issues/9)
  from [this run](https://github.com/tthh97/market-movers-dbt/actions/runs/30170684064)
- `demo/renamed-column` → [issue #10](https://github.com/tthh97/market-movers-dbt/issues/10)
  from [this run](https://github.com/tthh97/market-movers-dbt/actions/runs/30171213913)


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
