---
name: mover-analysis
description: "Explain a single price move or rank the biggest movers with context: size against the ticker's own volatility, against its sector, and against its trend. Use when asked what moved, the biggest movers, whether a move is unusual, or why something jumped or dropped."
---

# Mover Analysis

A move is only interesting relative to something. A 3% day is ordinary for
crypto and large for a bank, so never present a raw percentage alone.

## 1. Get the move and its context in one delegation

Ask **warehouse-analyst** for, per ticker in question (benchmarks excluded
from rankings unless the user asked about them):

- The return and its as-of date (`mart_movers`: `ret_1d`, `ret_5d`, `ret_1m`).
- **Against its own volatility:** `mart_momentum.daily_return_z`. Near 1 is a
  normal day for that name. Above 2 is unusual.
- **Against its peers:** the sector's `avg_ret_1d` and `pct_up_1d` (breadth)
  from `mart_sector_overview`.
- **Against its trend:** `trend_signal` and `drawdown_from_peak` from
  `mart_momentum`.

## 2. Read it

- Up 2% with the sector up 2% and breadth near 1.0: it moved with its sector.
- Up 2% in a flat sector with breadth near 0.3: specific to that name.
- A jump inside a deep drawdown is a different story from one at a peak.

## 3. Say what, not why

The data has prices, not news. If asked why, give the shape of the move and
state plainly that the data does not contain the cause.

## 4. Check, then answer

Hand the draft answer and the analyst's evidence to **fact-checker** through
`/drafts/`, as the operating manual describes. After PASS, reply with the
answer alone: lead with the finding and the as-of date. Do not mention the
check or the drafts.
