---
name: sector-snapshot
description: "Summarize how each sector (tech, industrials, financials, crypto) did over 1 day, 5 days, and about a month, with breadth and best/worst names, plus a bar chart. Use when asked how sectors did, for a sector breakdown, or which part of the watchlist led or lagged."
---

# Sector Snapshot

## 1. Get the numbers

Ask **warehouse-analyst** for every row of `mart_sector_overview`
(benchmarks are already excluded there): `sector`, `n_names`, `avg_ret_1d`,
`avg_ret_5d`, `avg_ret_1m`, `pct_up_1d`, `top_ticker`, `top_ret_1d`,
`bottom_ticker`, `bottom_ret_1d`, with returns as percents rounded to 2
decimals, and the as-of date range of the names behind each sector (from
`mart_movers`).

## 2. Chart

Call `render_bar_chart` with the sectors as labels and the 1-day average
return in percent as values, `value_label` "avg 1-day return (%)", filename
`sector-returns-<as-of date>.png`. Pass the analyst's numbers unchanged.

## 3. Write

A short Markdown note: one headline sentence (which sector led and lagged, on
which date), a table of the analyst's figures, one line on breadth, and the
chart embedded as `![Average 1-day return by sector](sector-returns-<as-of date>.png)`.
Keep crypto's date separate if it differs from the equities' date. The
sectors cover every non-benchmark name, not just holdings.

## 4. Check and save

Hand the note and the analyst's evidence to **fact-checker** through
`/drafts/`, as the operating manual describes. After PASS,
`write_file` it to `/outputs/sector-snapshot-<as-of date>.md` and tell the
user the path and the headline.
