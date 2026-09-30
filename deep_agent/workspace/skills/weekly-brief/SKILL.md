---
name: weekly-brief
description: "Produce the weekly market brief for the watchlist: top and bottom movers over 5 days, sector performance, trend and drawdown flags, and how the holdings track QQQ, fact-checked and saved to /outputs. Use when asked for the weekly brief, a weekly report, a market recap, or a summary of the week."
---

# Weekly Brief

A multi-part write-up. Coordinate; let the analyst query and the checker check.

## 1. Plan

Write a todo list with the four sections below so the user can follow along.

## 2. Gather in parallel

Fire these as separate **warehouse-analyst** tasks in one turn, so they run
at the same time. Ask each for the as-of dates and the SQL behind each figure.

1. **Movers:** top 3 and bottom 3 non-benchmark tickers by `ret_5d`, with
   `daily_return_z` for each.
2. **Sectors:** every row of `mart_sector_overview`, 5-day and 1-day average
   returns and breadth.
3. **Trend flags:** tickers whose `trend_signal` is a downtrend or whose
   `drawdown_from_peak` is below -0.15, with both values.
4. **Holdings vs QQQ:** every row of `mart_portfolio_bias`.

## 3. Draft

Markdown, in this order:

- `# Weekly Brief - week to <equities as-of date>` and a one-sentence summary.
- `## Movers`, `## Sectors`, `## Trend and drawdown`, `## Holdings vs QQQ`,
  each 2-4 sentences or a small table built only from the analyst's figures.
- `## Sources`: the tables used and the as-of date per asset class.

Optionally chart the 5-day sector returns with `render_bar_chart`.

## 4. Fact-check

Hand the full draft and all four evidence blocks to **fact-checker** through
`/drafts/`, as the operating manual describes. Fix
exactly what it flags and re-check. Two FAILs: stop and show the user the
draft with the objections.

## 5. Save

After PASS, `write_file` to `/outputs/weekly-brief-<equities as-of date>.md`.
Tell the user the path, the headline, and that it passed the fact-check.
