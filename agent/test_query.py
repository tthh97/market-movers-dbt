"""
Tests for agent/query.py - the shared read-only QueryRunner.

The point of the DuckDB adapter is that it is a real second adapter, not a
mock: these tests run the exact code path the agent runs against Snowflake in
production, just offline against the repo's market.duckdb with no credentials.

    python3 agent/test_query.py        # run directly
    pytest agent/test_query.py         # or under pytest

The guard and render tests need nothing. The tests that open DuckDB need the
marts built first (scripts/seed_sample.py then a DuckDB `dbt build`); they skip
with a note if market.duckdb is absent rather than failing spuriously.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import query  # noqa: E402

DUCKDB = os.path.join(ROOT, "market.duckdb")


def test_guard_rejects_non_reads():
    assert query.read_only_error("select 1") is None
    assert query.read_only_error("with x as (select 1) select * from x") is None
    for bad in ("drop table mart_movers", "delete from raw.prices", "update t set a=1",
                "insert into t values (1)", "create table t (a int)"):
        msg = query.read_only_error(bad)
        assert msg and msg.startswith("ERROR:"), bad
    # An interior semicolon is a second statement - reject it.
    assert query.read_only_error("select 1; drop table t").startswith("ERROR:")
    # Empty.
    assert query.read_only_error("   ").startswith("ERROR:")


def test_run_refuses_writes_without_connecting():
    # run() guards before it connects, so a write is rejected even with no
    # database present - the guard cannot be bypassed by reaching the engine.
    result = query.QueryRunner("duckdb").run("drop table mart_movers")
    assert result.is_error
    assert result.error.startswith("ERROR:")
    assert result.rows == []


def test_unknown_engine_fails_loudly():
    try:
        query.QueryRunner("mysql")
    except SystemExit:
        return
    raise AssertionError("expected SystemExit for an unknown engine")


def test_render_for_model():
    ok = query.QueryResult(columns=["ticker", "ret"], rows=[("NVDA", 0.03), ("AAPL", None)])
    assert query.render_for_model(ok) == "ticker | ret\nNVDA | 0.03\nAAPL | NULL"
    assert query.render_for_model(query.QueryResult(rows=[])) == "(0 rows)"
    assert query.render_for_model(query.QueryResult(error="SQL ERROR: boom")) == "SQL ERROR: boom"
    trunc = query.QueryResult(columns=["n"], rows=[(1,)], truncated=True)
    assert "truncated at 1 rows" in query.render_for_model(trunc)


# --- DuckDB adapter: real engine, real rows, no credentials -----------------

def _skip_if_no_db() -> bool:
    if not os.path.exists(DUCKDB):
        print("  SKIP (market.duckdb not built - run seed_sample.py + a duckdb dbt build)")
        return True
    return False


def test_duckdb_returns_typed_rows():
    if _skip_if_no_db():
        return
    r = query.QueryRunner("duckdb").run("select 42 as answer, 'x' as label")
    assert not r.is_error, r.error
    assert r.columns == ["answer", "label"]
    assert r.rows == [(42, "x")]


def test_duckdb_reads_a_mart():
    if _skip_if_no_db():
        return
    runner = query.QueryRunner("duckdb")
    r = runner.run("select count(*) as n from mart_movers")
    assert not r.is_error, r.error
    assert r.rows[0][0] > 0
    runner.close()


def test_duckdb_truncation():
    if _skip_if_no_db():
        return
    r = query.QueryRunner("duckdb").run("select * from range(100)", limit=5)
    assert not r.is_error, r.error
    assert len(r.rows) == 5
    assert r.truncated is True


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001 - a test runner reports, it does not raise
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
