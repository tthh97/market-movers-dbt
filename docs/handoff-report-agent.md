# Handoff: writer + verifier weekly report agent for market-movers-dbt

> **RESOLVED 2026-08-08.** The blocker below was a sandbox problem, not a key
> problem: the same `agent/.env` key authenticates fine locally. The live path
> has now run end to end. It published on the fourth attempt, after three
> genuine blocks, and the writer and verifier prompts were tightened in
> response (window qualifiers on figures, ranking on the column being
> described, grounded verifier evidence). Evals now 18/18 with the live tier
> included. Everything below is kept as the original design record; where it
> says "untested" or "never run", read "was, on 2026-08-08".

**Status:** Code complete and offline-verified (10/10 gate evals pass, DuckDB backend tested against real marts). Live LLM path untested - sandbox couldn't authenticate to the Anthropic API. Nothing committed to git yet.
**Objective:** A weekly market report where fabrication is structurally unable to reach the reader: writer LLM drafts from warehouse queries, code + a second LLM fact-check every claim against the saved query results, and a mechanical gate blocks publication if anything fails.
**Immediate next action:** Run the live end-to-end demo locally: `cd agent && REPORT_TARGET=duckdb python report.py` (needs a valid `ANTHROPIC_API_KEY` in `agent/.env` - the key currently there returned 401 Unauthorized; verify/rotate it first).

## Current state

New files, all in the `market-movers-dbt` repo, none committed:

- `agent/claims.py` - deterministic matcher + gate. `check_draft(draft, notebook)` traces every number/date in the draft to the evidence notebook; `gate_decision(matcher_failures, verifier_claims)` is the publication rule. Pure stdlib.
- `agent/report.py` - orchestrator: `write_draft()` (writer LLM with `run_sql`, logs every query+rows to a notebook), `verify()` (second LLM call, returns claim list or `None`), `inject_fault()` (corrupts draft to demo the gate), `run()` (write → check → one rewrite → publish to `reports/YYYY-MM-DD-weekly-report.md` or fail to `reports/YYYY-MM-DD-gate-failure.json`). Two backends: `_SnowflakeBackend` (reuses `agent/tools.py` wholesale) and `_DuckDBBackend` (repo's `market.duckdb`, `read_only=True`, borrows `tools._is_read_only`).
- `agent/report_evals.py` - the gate's own evals. Offline tier (fixture notebooks + corrupted drafts, no key needed): 10/10 passing. Live tier (2 cases: `live-flipped-direction`, `live-unsupported-superlative`) skips cleanly without a key and has **never run**.
- `.github/workflows/weekly-report.yml` - Mon 10:00 UTC schedule (2h after daily-refresh lands Friday's close) + `workflow_dispatch` with `inject_fault` input. Stage 1 `gate-evals` (offline, no secrets) gates stage 2 `weekly-report` (Snowflake + API). Actions SHA-pinned matching `daily.yml`.
- `agent/README.md` - "## The weekly report agent" section appended (already documents all of the above).
- `docs/harness-roadmap.md` - earlier deliverable; phased harness plan for the whole repo (PR CI → unit tests → contracts/expectations → observability). Untouched by this work but the report agent effectively claims the "differentiator" slot discussed there.

Verified in sandbox: `python report_evals.py --offline` → 10/10 PASS; `_DuckDBBackend` returns real schema + mart rows and rejects `DROP` and `select 1; drop ...`; all three files compile. NOT verified: any real Claude call (writer, verifier, live evals), the CI workflow, Snowflake path.

## Decisions made (and why)

- **Numbers checked by code, not the verifier LLM** - an LLM checking an LLM can false-pass; two models agreeing is not a harness. `claims.py` does numeric/date tracing; the verifier is scoped to what code can't judge: direction words, superlatives, causal phrasing.
- **Matcher matches absolute values** ("fell 8.1%" passes against evidence `-0.0812`) - sign errors are direction errors, which are the verifier's job. Pinned by eval `flipped-direction-passes-matcher` so the division of labour can't erode silently.
- **Rounding to the draft's own precision + percent/fraction scale (×100, ÷100)** are the only allowed transforms between draft and evidence.
- **Gate fails closed** - `verifier_claims=None` (no key, parse failure) blocks publication. Pinned by eval `gate-fails-closed`.
- **One rewrite loop, then flag for human** - mirrors the repo's triage philosophy: propose, never self-heal. An injected fault is never rewritten into a publish (the demo is the catch, not the repair).
- **Documented exemptions in the matcher**: window terminology ("5-day", "20-day MA"), "top N", standalone years. Listed at the top of `claims.py` so the limitation is visible.
- **Flat files in `agent/`, not a subpackage** - matches the repo's existing layout (`tools.py`, `policy.py`, `evals.py`).
- **Evidence notebook format** = `[{"query": str, "result": str}]` with `result` in `tools.run_sql`'s pipe-delimited text format - both backends and the fixtures share it.
- **Workflow runs Monday 10:00 UTC** - the Monday 08:00 daily-refresh is what lands Friday's settled close; earlier and the week is missing its final session.
- **Report published as artifact, not commit** - workflow has `contents: read` only.

## Constraints & conventions (established earlier in the project)

- Repo voice: comments explain *why*, design choices are written down where they live.
- CI: third-party actions pinned to commit SHAs; secrets never interpolated into shell; cheap offline stage always gates the paid stage.
- Agents: read-only warehouse access enforced by grants/engine, not prompts; every number must come from a query result; no investment advice (`policy.SYSTEM_PROMPT` is reused as the writer's base).
- User context: portfolio/interview-first, solid SQL but new to dbt, no deadline. Prefers concise communication.

## Open threads

1. **Blocking: valid `ANTHROPIC_API_KEY`.** Both keys on disk (`agent/.env`, root `.env`) returned 401 from the sandbox. Could be rotation or sandbox-proxy interference - verify locally before debugging anything else.
2. **Live demo not yet run**: `REPORT_TARGET=duckdb python report.py`, then `python report.py --inject-fault fabricated_number` (must exit 1 with a gate-failure JSON), then `python report_evals.py` (live tier: 2 cases must pass). Expect writer prompt/parsing tweaks on first contact - `verify()` parses the first `{...}` block from the response; if the model wraps JSON in prose differently, loosen there.
3. **CI secrets to create** before the workflow can run: `SNOWFLAKE_AGENT_USER`, `SNOWFLAKE_AGENT_PRIVATE_KEY`, `SNOWFLAKE_ROLE_AGENT` (the read-only service identity from `agent/.env` - reuse it), plus existing `ANTHROPIC_API_KEY`, `SNOWFLAKE_ACCOUNT/WAREHOUSE/DATABASE/SCHEMA`.
4. **Commit + PR**: nothing is committed. Suggested: one PR for `claims.py` + `report.py` + `report_evals.py` + README, one for the workflow. Add `reports/*.json` to `.gitignore` consideration (gate-failure files) - user hasn't weighed in.
5. **Parallel, not blocking**: `docs/harness-roadmap.md` Phase 1 (PR-triggered CI + sqlfluff + dbt docs to Pages) is still the highest-leverage unstarted item in the repo and would give these new files CI coverage on their own PR.

## Dead ends (don't retry)

- **Running live API calls from the Cowork sandbox** - SOCKS proxy needed `socksio`, TLS needed `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`, and after both fixes the API still returned 401 with both keys. Run live tests on the user's machine.
- **A single verifier LLM checking everything including arithmetic** - rejected at design time; see Decisions.

## Definition of done

1. `REPORT_TARGET=duckdb python report.py` publishes `reports/<date>-weekly-report.md` with a query appendix, every figure traceable.
2. `--inject-fault fabricated_number` and `--inject-fault flipped_direction` both end blocked (exit 1, gate-failure JSON) - never published.
3. `python report_evals.py` passes all offline + live cases.
4. Files merged via PR; `weekly-report.yml` dispatched once with a fault (expected red, artifact shows the block) and once clean against Snowflake (green, report artifact).

---
**Pickup prompt** (paste into Claude Code, run from the `market-movers-dbt` repo root):

> You're continuing work on a writer+verifier weekly report agent in this dbt repo. It's code-complete and offline-verified: `agent/report.py` (writer LLM → deterministic matcher in `agent/claims.py` → verifier LLM → mechanical publication gate), `agent/report_evals.py` (offline tier 10/10 passing), and `.github/workflows/weekly-report.yml`. Full state, decisions, and remaining steps are in `docs/handoff-report-agent.md` - read that first. Start by verifying `ANTHROPIC_API_KEY` in `agent/.env` works (it returned 401 from the previous environment), then run the live demo: `cd agent && REPORT_TARGET=duckdb python report.py`, then both `--inject-fault` variants (must be blocked, exit 1), then `python report_evals.py` for the live verifier cases. Don't weaken the gate to make anything pass - it fails closed by design.
