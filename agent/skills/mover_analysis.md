A move is only interesting relative to something. A 3% day is unremarkable for a
crypto asset and a large one for a utility, so never present a raw percentage as
though it speaks for itself.

Give a move context along at least one of these axes, all of which are already
computed in the marts - query them rather than deriving them yourself:

- **Against the ticker's own volatility.** `MART_MOMENTUM.DAILY_RETURN_Z` is the
  day's return divided by that ticker's daily standard deviation. A z near 1 is a
  normal day for that name; a z above 2 is genuinely unusual. This is the single
  most useful number for "is this move big?"
- **Against its peers.** `MART_SECTOR_OVERVIEW.AVG_RET_1D` gives the sector's
  average and `PCT_UP_1D` the breadth - the share of names in that sector that
  rose. A stock up 2% in a sector up 2% with breadth near 1.0 moved *with* its
  sector; the same 2% in a flat sector with breadth near 0.3 is specific to that
  name.
- **Against its own trend.** `MART_MOMENTUM.TREND_SIGNAL` compares the 5-day and
  20-day moving averages, and `DRAWDOWN_FROM_PEAK` says how far the price sits
  below its running high. A jump inside a deep drawdown is a different story from
  one at a peak.

Two cautions specific to this watchlist:

- **SPY and QQQ are benchmarks, not holdings.** They are reference series. Exclude
  them when ranking "biggest movers" unless the user asked about benchmarks, and
  say that you did.
- **You can say what moved, not why.** This data is prices and returns. It has no
  news, filings, or events in it, so a cause is not something you can read out of
  it. If asked why something moved, give the shape of the move - size relative to
  its own volatility, whether the sector moved with it, where it sits against its
  trend - and state plainly that the data does not contain the reason.
