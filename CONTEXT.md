# Market Movers

A dbt pipeline that turns daily prices for a small watchlist into mover,
momentum, and portfolio-bias marts, with self-checking scaffolding around every
process that runs. This glossary fixes the terms that recur across the pipeline,
the CI, and the agents.

## Language

**Harness**:
The scaffolding around a process that makes it repeatable, guarded, and
self-checking - so a failure is loud and a bad output cannot silently ship. This
repo has four: the **test/CI harness** (dbt tests + gated CI), the **triage
harness** (failure capture, diagnosis, and proposed fix), the **agent harness**
(the read-only analytics agent's loop, tools, policy, and evals), and the
**report harness** (the weekly report's writer, matcher, verifier, and gate).
_Avoid_: framework, wrapper, pipeline (when the self-checking property is the point).

**Mover**:
A watchlist instrument ranked by its price move over a period. Benchmarks are
excluded when ranking movers.
_Avoid_: gainer, loser, stock (crypto and ETFs are movers too).

**Benchmark**:
An index proxy (SPY, QQQ) carried in the watchlist for comparison only, never
treated as a holding or ranked as a mover.
_Avoid_: holding, position, ticker (when the not-a-holding property is the point).

**Triage**:
The layer that runs only after a failed build to capture the failure, diagnose
it, and propose a fix. It proposes; it never applies.
_Avoid_: debugging, auto-fix, remediation.

**Propose-only**:
The standing rule shared by triage, the agent, and the report harness: each one
describes or proposes, and none of them writes to the warehouse or the codebase.
The nightly build is the only writer.
_Avoid_: read-only (which describes access, not the propose-not-apply discipline).
