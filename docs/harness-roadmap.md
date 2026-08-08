# Harness Roadmap

What to add to make this project fully production-grade, what to skip, and why.
Phased so each stage is finished and demoable before the next starts.

## What already exists (don't rebuild)

26 schema tests + 2 singular tests (including the completeness test), source
freshness, validated idempotent ingest, two-stage gated CI (DuckDB gates
Snowflake), assisted triage with fault injection, hardened CI (SHA-pinned
actions, least-privilege roles), agent guardrails + golden-question evals,
design-rationale docs. This baseline is already above most production pipelines.

## The gaps, ranked by leverage

### Phase 1 - Visibility (one weekend)

The project's biggest gap is not rigor, it's *visible* rigor. CI runs only on
schedule/dispatch, so a recruiter opening a PR sees no checks; docs are
generated in CI but only uploaded as an artifact nobody clicks.

1. **PR-triggered CI.** New `ci.yml` on `pull_request`: checkout →
   `seed_sample.py` → `dbt build` against DuckDB. Reuses the existing offline
   stage, needs zero credentials, runs in ~2 minutes. *Earns its place because
   green checks on PRs are the single most legible production signal a repo can
   show - and everything needed already exists.*
2. **sqlfluff lint** as a step in that same workflow, with a committed
   `.sqlfluff` config. *Standard on production teams, near-zero cost, and makes
   every future diff cleaner.*
3. **Publish dbt docs to GitHub Pages.** The docs are already generated
   nightly; add a Pages deploy step. *Clickable lineage graph without cloning -
   the highest view-per-effort artifact in the repo.*

**Done when:** a real PR shows green checks, and the README links a live docs
site.

### Phase 2 - Unit tests on the math (one weekend)

The project has zero dbt unit tests (dbt ≥1.8), and the metric SQL -
returns, moving averages, drawdown in `int_daily_returns`; QQQ correlation in
`mart_portfolio_bias` - is exactly where silent bugs live. Data tests check
shape; nothing currently checks the *math*.

4. **Unit tests with hand-computed fixtures**: e.g. five days of prices where
   the 1d/5d returns, running peak, and drawdown are known by hand; a small
   series with a known correlation. Put them in the model YAML under
   `unit_tests:`. Three or four cases, not exhaustive coverage. *Earns its
   place because it closes the one test category the project lacks, and it's
   the strongest interview differentiator - almost no portfolio dbt project
   has them.*

**Done when:** `dbt test --select test_type:unit` passes with 3-4 cases
covering returns, drawdown, and correlation, and the README's test count
paragraph mentions them.

### Phase 3 - Contracts + targeted expectations (one weekend)

5. **Model contracts on the marts** (`contract: enforced` + full column
   specs). *Earns its place because the marts have a real downstream consumer -
   the analytics agent reads them - so a contract protects an actual dependency,
   not a hypothetical one. Also catches the `renamed_column` fault class at
   compile time, which makes a nice narrative pair with the triage demo.*
6. **dbt-expectations, targeted** (already listed in the README's extension
   ideas): row-count-vs-yesterday anomaly on `fct_prices`,
   `expect_column_values_to_be_between` on prices/returns, recency on marts.
   Five or six checks, not a blanket. *Distribution-level validation is the one
   data-quality category the current tests don't cover - but applied
   surgically, because 40 boilerplate expectations dilute the signal of the 28
   deliberate tests already there.*

**Done when:** contracts are enforced on all four marts and a deliberately
mistyped column fails at compile; expectations run in the nightly build.

### Phase 4 - Observability (ongoing, optional)

7. **Elementary.** Test-result history, anomaly monitors, and an HTML
   monitoring report - publishable to the same Pages site. *The only genuinely
   new harness **category** left: everything so far checks a single run;
   Elementary tracks quality **over time**, which is what "monitoring" actually
   means for a batch pipeline. It also gives the triage bot richer context.
   Optional because it's the one item with real ongoing maintenance cost.*

**Done when:** the nightly run uploads an Elementary report and at least one
anomaly monitor has fired (use `inject_fault` to prove it).

## Deliberately skipped, and why

- **Orchestrator (Airflow/Dagster)** - GitHub Actions cron already provides
  scheduling, gating, retries-by-rerun, and alerting-by-issue. Migration is
  infra work with no new story.
- **Slim CI / state deferral** - solves a build-time problem this project
  doesn't have; the full offline build takes seconds. (Knowing *why* it's
  unnecessary here is itself an interview answer.)
- **Great Expectations standalone, Datadog-style monitoring, Kubernetes,
  dbt Cloud** - each duplicates something the repo already does with lighter
  tools, at this scale.
- **More triage/agent features** - those layers are already the project's
  differentiators; further investment there has diminishing returns compared to
  closing the unit-test and PR-CI gaps.

## Sequencing rule

One phase per PR (or a few small PRs) - which, once Phase 1 lands, means the
harness additions themselves run through the new PR CI. Each phase's "done
when" must hold before starting the next; an unfinished harness is worse than
an absent one.
