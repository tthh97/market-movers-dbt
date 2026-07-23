# Snowflake Setup — market-movers migration

Runbook for standing up Snowflake and pointing dbt + Claude Code at it.
Drop this in the repo root so Claude Code can read it as context.

---

## 0. Before you click signup

The trial is 30 days from account creation, and the clock is the binding
constraint (not the credits). Start it on a day you can work continuously.

At signup:

- **Cloud/region**: AWS `ap-southeast-1` (Singapore). Keeps latency low and
  avoids cross-region egress. Note the per-credit rate shown at signup —
  it is higher than the US East headline figure.
- **Edition**: trial usually starts on Enterprise. Fine for now. When it
  converts to pay-as-you-go, drop to **Standard** — zero-copy cloning works
  on Standard, so Slim CI is unaffected.
- **Account identifier**: after signup, note your `<orgname>-<account_name>`.
  This is the `account` value dbt needs. Find it under Snowsight →
  bottom-left account menu → "Copy account identifier".

---

## 1. Account bootstrap SQL

Run in a Snowsight worksheet as `ACCOUNTADMIN`.

```sql
USE ROLE ACCOUNTADMIN;

-- Cost guardrail FIRST, before anything can run.
CREATE RESOURCE MONITOR IF NOT EXISTS RM_PORTFOLIO
  WITH CREDIT_QUOTA = 20
  FREQUENCY = MONTHLY
  START_TIMESTAMP = IMMEDIATELY
  TRIGGERS
    ON 50  PERCENT DO NOTIFY
    ON 80  PERCENT DO NOTIFY
    ON 100 PERCENT DO SUSPEND
    ON 110 PERCENT DO SUSPEND_IMMEDIATE;

CREATE WAREHOUSE IF NOT EXISTS WH_XS
  WAREHOUSE_SIZE      = 'XSMALL'
  AUTO_SUSPEND        = 60          -- seconds. non-negotiable.
  AUTO_RESUME         = TRUE
  INITIALLY_SUSPENDED = TRUE
  RESOURCE_MONITOR    = RM_PORTFOLIO;

CREATE DATABASE IF NOT EXISTS MARKET_MOVERS;
CREATE SCHEMA   IF NOT EXISTS MARKET_MOVERS.RAW;        -- EL landing zone
CREATE SCHEMA   IF NOT EXISTS MARKET_MOVERS.ANALYTICS;  -- dbt prod target

CREATE ROLE IF NOT EXISTS LOADER;       -- writes RAW only
CREATE ROLE IF NOT EXISTS TRANSFORMER;  -- reads RAW, owns ANALYTICS
CREATE ROLE IF NOT EXISTS REPORTER;     -- reads ANALYTICS only
```

Grants:

```sql
GRANT USAGE ON WAREHOUSE WH_XS TO ROLE LOADER;
GRANT USAGE ON WAREHOUSE WH_XS TO ROLE TRANSFORMER;
GRANT USAGE ON WAREHOUSE WH_XS TO ROLE REPORTER;

GRANT USAGE ON DATABASE MARKET_MOVERS TO ROLE LOADER;
GRANT USAGE ON DATABASE MARKET_MOVERS TO ROLE TRANSFORMER;
GRANT USAGE ON DATABASE MARKET_MOVERS TO ROLE REPORTER;

-- LOADER: write to RAW
GRANT USAGE, CREATE TABLE ON SCHEMA MARKET_MOVERS.RAW TO ROLE LOADER;

-- TRANSFORMER: read RAW, own ANALYTICS, and create its own dev/CI schemas
GRANT USAGE ON SCHEMA MARKET_MOVERS.RAW TO ROLE TRANSFORMER;
GRANT SELECT ON ALL TABLES    IN SCHEMA MARKET_MOVERS.RAW TO ROLE TRANSFORMER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA MARKET_MOVERS.RAW TO ROLE TRANSFORMER;
GRANT ALL ON SCHEMA MARKET_MOVERS.ANALYTICS TO ROLE TRANSFORMER;
GRANT CREATE SCHEMA ON DATABASE MARKET_MOVERS TO ROLE TRANSFORMER;

-- REPORTER: read-only on marts
GRANT USAGE ON SCHEMA MARKET_MOVERS.ANALYTICS TO ROLE REPORTER;
GRANT SELECT ON ALL TABLES       IN SCHEMA MARKET_MOVERS.ANALYTICS TO ROLE REPORTER;
GRANT SELECT ON FUTURE TABLES    IN SCHEMA MARKET_MOVERS.ANALYTICS TO ROLE REPORTER;
GRANT SELECT ON FUTURE VIEWS     IN SCHEMA MARKET_MOVERS.ANALYTICS TO ROLE REPORTER;

GRANT ROLE LOADER, TRANSFORMER, REPORTER TO ROLE SYSADMIN;
```

The three-role split is small effort and it is the thing interviewers ask
about when they say "how do you handle access control?"

---

## 2. Key-pair auth (do this, not passwords)

Snowflake is phasing out password auth for programmatic users through 2026 —
service users must move to key-pair, OAuth, PAT, or WIF. If you set this up
with a password now you will be redoing it, and CI will break at some point.
Do it once, correctly.

Generate a key pair locally:

```bash
mkdir -p ~/.snowflake && chmod 700 ~/.snowflake
cd ~/.snowflake

# Unencrypted — simplest for CI. Keep the file out of the repo.
openssl genrsa 2048 \
  | openssl pkcs8 -topk8 -inform PEM -nocrypt -out dbt_key.p8
openssl rsa -in dbt_key.p8 -pubout -out dbt_key.pub
chmod 600 dbt_key.p8
```

Get the public key as a single line (no header/footer):

```bash
grep -v '^-----' ~/.snowflake/dbt_key.pub | tr -d '\n'
```

Create the service users, pasting that string in:

```sql
USE ROLE ACCOUNTADMIN;

-- Local development
CREATE USER IF NOT EXISTS DBT_DEV
  TYPE              = SERVICE
  RSA_PUBLIC_KEY    = '<paste-single-line-key>'
  DEFAULT_ROLE      = TRANSFORMER
  DEFAULT_WAREHOUSE = WH_XS;
GRANT ROLE TRANSFORMER TO USER DBT_DEV;

-- GitHub Actions (generate a SECOND key pair for this one)
CREATE USER IF NOT EXISTS DBT_CI
  TYPE              = SERVICE
  RSA_PUBLIC_KEY    = '<paste-ci-key>'
  DEFAULT_ROLE      = TRANSFORMER
  DEFAULT_WAREHOUSE = WH_XS;
GRANT ROLE TRANSFORMER TO USER DBT_CI;
```

`TYPE = SERVICE` matters — it exempts these users from the MFA prompts that
apply to human users, which is exactly what you want for automated runs.
Your own Snowsight login stays a human user with MFA.

Separate keys for dev and CI so you can rotate or revoke one without
killing the other.

---

## 3. dbt connection

Install the adapter:

```bash
source .venv/bin/activate
pip install dbt-snowflake
```

`profiles.yml` in the project directory (you run `--profiles-dir .`):

```yaml
market_movers:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user: DBT_DEV
      private_key_path: "{{ env_var('SNOWFLAKE_PRIVATE_KEY_PATH') }}"
      role: TRANSFORMER
      database: MARKET_MOVERS
      warehouse: WH_XS
      schema: dbt_dev          # your personal dev schema
      threads: 4
      client_session_keep_alive: false

    prod:
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user: "{{ env_var('SNOWFLAKE_USER') }}"
      private_key_path: "{{ env_var('SNOWFLAKE_PRIVATE_KEY_PATH') }}"
      role: TRANSFORMER
      database: MARKET_MOVERS
      warehouse: WH_XS
      schema: ANALYTICS
      threads: 4
```

Env vars, not literals — same file works locally and in CI.

```bash
# .envrc or shell profile — NOT committed
export SNOWFLAKE_ACCOUNT="ORGNAME-ACCOUNTNAME"
export SNOWFLAKE_USER="DBT_DEV"
export SNOWFLAKE_PRIVATE_KEY_PATH="$HOME/.snowflake/dbt_key.p8"
```

Add to `.gitignore`:

```
.envrc
*.p8
*.pub
```

Verify:

```bash
dbt debug --profiles-dir .
```

---

## 4. What actually changes in the dbt project

Most of it ports untouched. The parts that need attention:

| Area | DuckDB | Snowflake |
|---|---|---|
| Source data | local file / DuckDB table | `MARKET_MOVERS.RAW` tables |
| Load step | DuckDB write from Python | `write_pandas` via `snowflake-connector-python` |
| Identifiers | case-insensitive-ish | UPPERCASE by default — watch quoting |
| Date functions | DuckDB dialect | `DATEADD`, `DATE_TRUNC`, `TO_DATE` |
| `dbt_utils` | mostly fine | mostly fine, re-run `dbt deps` |
| Incremental | limited | `incremental_strategy='merge'` with `unique_key` |

The new material worth building (this is what moves the resume, not the
port itself):

1. **Incremental models** on the fact layer with `merge` strategy —
   daily prices are a genuine incremental case, not a contrived one.
2. **Slim CI** on PRs: clone prod schema (zero-copy, free), run
   `dbt build --select state:modified+ --defer --state ./prod-manifest`.
   Requires downloading the prod `manifest.json` artifact from the nightly
   run — that is the piece people get stuck on.
3. **Triage layer carries Snowflake context** — add `query_id` and
   warehouse to the failure report so the Claude diagnosis has real
   warehouse context, not just compiled SQL.

---

## 5. Claude Code workflow

Add a `CLAUDE.md` at repo root so every session starts with the rules
instead of you re-typing them:

```markdown
# market-movers

## Environment
- venv, not conda. Activate before any dbt command.
- Always: `dbt build --profiles-dir .`
- Snowflake target `dev` writes to schema `dbt_dev`. Never run against
  `prod` locally.
- Credentials come from env vars. Never write account, user, or key paths
  into tracked files.

## Snowflake conventions
- XS warehouse only. Do not resize.
- Never `CREATE WAREHOUSE` or alter the resource monitor from code.
- Models use `TRANSFORMER` role. RAW is read-only from dbt.
- Identifiers uppercase; quote only when unavoidable.

## Cost rules
- No materialized views, search optimization, Snowpipe, or Cortex.
- Prefer `dbt build --select <model>+` over full builds while iterating.
```

Give Claude Code a way to actually query Snowflake, so it can verify its own
work instead of guessing at row counts:

```bash
pip install snowflake-cli
snow connection add --connection-name mm \
  --account "$SNOWFLAKE_ACCOUNT" --user DBT_DEV \
  --private-key-file "$SNOWFLAKE_PRIVATE_KEY_PATH" \
  --role TRANSFORMER --warehouse WH_XS --database MARKET_MOVERS
snow sql -c mm -q "select current_version()"
```

Now Claude Code can run `snow sql -c mm -q "..."` through bash to check
results, inspect schemas, and confirm a model built correctly. That single
capability changes it from writing SQL blind to closing the loop.

There is also a Snowflake MCP server (Snowflake Labs) if you would rather
expose the warehouse as tools than shell out — worth a look, but the CLI
route is fewer moving parts and works today.

**Cost discipline while working with Claude Code**: it is easy to run
dozens of exploratory queries in a session. Each resume bills a 60-second
minimum. With `AUTO_SUSPEND = 60` and back-to-back queries the warehouse
stays warm, so this is a small effect — but the resource monitor is your
real backstop. Check `SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY`
after the first week to see actual burn against the estimate.

---

## 6. Order of operations

1. Signup, note account identifier
2. Run bootstrap SQL (monitor first)
3. Generate keys, create SERVICE users
4. `dbt debug` green
5. Port the Python loader: yfinance → `MARKET_MOVERS.RAW`
6. `dbt build` green against `dev`
7. Nightly GitHub Actions job → `prod`, upload `manifest.json` as artifact
8. Slim CI on PRs with clone + defer
9. Triage layer picks up Snowflake query IDs
10. Resource monitor sanity check after week one
