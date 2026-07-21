# Market Movers — a dbt + DuckDB analytics pipeline

A small, **fully reproducible** analytics-engineering project: pull daily prices
for a watchlist, model them through staging → intermediate → marts in dbt, and
surface *movers*, *momentum/drawdown*, and a *portfolio-bias* view. Runs locally
on DuckDB with **zero credentials** — clone it and `dbt build`.

The watchlist holds 20 names grouped into four sectors - **tech**, **industrials**,
**financials**, and **crypto** - plus two benchmarks (SPY, QQQ) kept as reference
series. A few names are flagged as personal holdings (NVDA, CAT, JPM, BTC) for the
portfolio-bias view. The marts answer: *what's moving, which sector is hot, and how
does my book behave relative to the Nasdaq?* Edit `seeds/watchlist.csv` to change the universe.

> New: on a failed nightly build, an **assisted-triage** step captures a clean
> failure report for a human to act on. See
> [docs/pipeline-explainer.md](docs/pipeline-explainer.md) for the lineage, a
> plain-language walkthrough of every step, and a runnable failure demo.

## Stack

`yfinance` (ingest) → **DuckDB** (warehouse, single file) → **dbt** (models +
tests + freshness) → **GitHub Actions** (scheduled daily refresh).

## Architecture

```
yfinance ──> raw.prices ──> stg_prices ─┐
(ingest.py)   (DuckDB)                  ├─> fct_prices ──> int_daily_returns ─┬─> mart_movers
              seeds/watchlist ──> stg_watchlist        (incremental fact)    ├─> mart_momentum
                                                                             ├─> mart_portfolio_bias
                                                                             └─> mart_movers ─> mart_sector_overview
```

- **staging** — typed, renamed, de-duplicated (views)
- **fct_prices** — incremental fact, one row per ticker per day (delete+insert on a surrogate key)
- **intermediate** — daily returns, 5/20-day MAs, running peak, drawdown
- **marts** — analysis-ready tables (movers, momentum, portfolio bias, sector overview)

## Quickstart

```bash
pip install -r requirements.txt

# Option A — offline demo (synthetic data, no network needed)
python scripts/seed_sample.py
dbt build --profiles-dir .

# Option B — real market data
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
scripts/capture_failure.py    # on a failed build, writes failure_context.json (no AI)
scripts/diagnose_failure.py   # one Claude API call → proposed diagnosis (session 2)
seeds/watchlist.csv           # the tracked universe + holding/benchmark flags
models/staging/               # stg_prices, stg_watchlist, sources + freshness
models/intermediate/          # int_daily_returns
models/marts/                 # fct_prices, mart_movers, mart_momentum, mart_portfolio_bias
tests/                        # singular test: no non-positive close prices
.github/workflows/daily.yml   # scheduled ingest + build + test + docs
```

## Design choices worth talking through

- **Idempotent ingest.** `INSERT OR REPLACE` on `(ticker, trade_date)` means a
  same-day re-run refreshes rather than duplicates.
- **Incremental fact.** `fct_prices` reprocesses only from the latest stored day
  onward, with delete+insert on a `ticker|date` surrogate key — the same
  incremental discipline used in production loads.
- **Tested + monitored.** 24 data tests - not_null, unique, accepted_values, a
  `relationships` FK from the fact to the watchlist, and a singular no-negative-close
  test - plus source-freshness thresholds.
- **Assisted-triage on failure.** When the nightly `dbt build` fails, two
  failure-only CI steps run: `scripts/capture_failure.py` (no AI) turns dbt's
  artifacts into one clean `failure_context.json`, then `scripts/diagnose_failure.py`
  makes a single Claude API call for a **structured** diagnosis (likely cause,
  proposed fix, confidence, and safety flags). It proposes; a human approves. Set
  `ANTHROPIC_API_KEY` (a `.env` locally - see `.env.example` - or a CI secret); the
  diagnosis step skips cleanly if it's unset. Never auto-fixes, never writes to `main`.
- **Portfolio-bias mart.** `mart_portfolio_bias` computes each holding's
  correlation to the Nasdaq (QQQ) and its current drawdown — a quick check on
  how much your "diversified" book is really just one beta.

## Resume bullets (use only what's true for you)

- Built a reproducible market-data pipeline (yfinance → DuckDB → dbt) modeling a
  multi-asset watchlist across staging/intermediate/marts layers, with 24
  automated dbt tests and source-freshness monitoring.
- Implemented an incremental dbt fact with delete+insert upserts on a
  ticker-date surrogate key for idempotent daily refreshes.
- Automated a scheduled daily refresh in GitHub Actions (ingest → build → test →
  docs), producing movers, momentum/drawdown, and portfolio-correlation marts.

## Notes / honest caveats

- This is a **portfolio project**, not production. The marts are descriptive
  analytics, **not** trading or investment advice.
- The CI job rebuilds from a fresh 6-month pull each run; it does not persist the
  DuckDB file between runs. To make the history durable, push the DB to a release
  artifact or move the warehouse to MotherDuck — a natural next step.
- yfinance is an unofficial Yahoo Finance interface; for anything you depend on,
  swap in a keyed provider (Alpha Vantage, Tiingo, Polygon) at the `ingest.py`
  layer without touching the models.

## Extension ideas

- Add `dbt_utils` + `dbt_expectations` for richer tests (recency, value ranges).
- Add a thin Streamlit or Power BI layer on top of the marts.
- Snapshot the watchlist with dbt snapshots to track changes over time.
