# Weekly market report - 2026-08-08

<!-- generated-from: duckdb -->

**Week ending July 24, 2026**

Nvidia led the watchlist for the week, climbing +8.5% on the five-day return to close at $200.71. JPMorgan Chase came in second at +5.7% on the week, followed by Alphabet at +4.5%, Ethereum at +4.4%, and Meta Platforms at +3.8% - all as of July 24.

Crypto was the strongest sector on the week, averaging +2.0% across its three names. Tech was second at +1.1% on the week, though the group was split: Nvidia, Alphabet, and Meta all gained, while Microsoft (-5.9% on the week) and Apple (-5.4% on the week) dragged. Financials fell -1.4% on the week as a group, and Industrials were the weakest sector, down -1.8% on the week.

The week's worst individual performers were GE Aerospace (-6.2% on the week) and Microsoft (-5.9% on the week), followed by Apple (-5.4% on the week) and Bank of America (-5.3% on the week). Goldman Sachs rounded out the bottom five at -4.5% on the week.

On the momentum side, Nvidia, Caterpillar, Alphabet, and Meta are all trading above their 20-day moving averages with uptrend signals as of July 24. The deepest drawdowns from peak belong to Solana (-47.6%) and Bitcoin (-44.9%), both flagged as downtrends despite Bitcoin posting a +0.7% gain on the week. Bank of America sits -30.6% from its peak and also carries a downtrend signal - notable given that financials saw modest single-day gains on July 24 even as the weekly picture was negative.

---

*Every figure above was checked against the queries below; publication is blocked mechanically if any check fails. Source: duckdb.*

## Evidence: queries run

1. `SELECT * FROM mart_movers ORDER BY ret_5d DESC`
2. `SELECT * FROM mart_sector_overview ORDER BY avg_ret_5d DESC`
3. `SELECT * FROM mart_momentum ORDER BY drawdown_from_peak ASC`
4. `-- Top and bottom 5-day movers explicitly ordered
SELECT ticker, name, sector, ret_5d, ret_1d, ret_1m, as_of_date
FROM mart_movers
ORDER BY ret_5d ASC
LIMIT 5`
5. `SELECT ticker, name, sector, ret_5d, ret_1d, ret_1m, as_of_date
FROM mart_movers
ORDER BY ret_5d ASC
LIMIT 5`
6. `-- Date range for the week: min and max as_of_date, and approx week start
SELECT MIN(as_of_date) as min_date, MAX(as_of_date) as max_date
FROM mart_movers`
7. `SELECT MIN(as_of_date) AS min_date, MAX(as_of_date) AS max_date FROM mart_movers`
8. `-- Get the week-start approximate date by subtracting 5 trading days from latest
SELECT ticker, name, sector, ret_5d, ret_1d, ret_1m, as_of_date
FROM mart_movers
ORDER BY ret_5d DESC
LIMIT 5`
9. `SELECT ticker, name, sector, ret_5d, ret_1d, ret_1m, as_of_date
FROM mart_movers
ORDER BY ret_5d DESC
LIMIT 5`
10. `-- Find approximate week start date from price history (5 trading days back from 2026-07-24)
SELECT DISTINCT trade_date FROM fct_prices
WHERE trade_date <= '2026-07-24'
ORDER BY trade_date DESC
LIMIT 6`
11. `SELECT DISTINCT trade_date FROM fct_prices
WHERE trade_date <= '2026-07-24'
ORDER BY trade_date DESC
LIMIT 6`
12. `SELECT ticker, name, trend_signal, drawdown_from_peak, ma_5, ma_20, close_price, as_of_date
FROM mart_momentum
WHERE trend_signal = 'uptrend'
ORDER BY drawdown_from_peak DESC`
13. `-- Confirm sector 5d rankings clearly
SELECT sector, ROUND(avg_ret_5d * 100, 2) AS avg_ret_5d_pct, ROUND(avg_ret_1d * 100, 2) AS avg_ret_1d_pct, ROUND(avg_ret_1m * 100, 2) AS avg_ret_1m_pct
FROM mart_sector_overview
ORDER BY avg_ret_5d DESC`
14. `-- Confirm META z-score and AAPL z-score for momentum note
SELECT ticker, name, daily_return_z, trend_signal, drawdown_from_peak, close_price, ma_5, ma_20
FROM mart_momentum
ORDER BY ABS(daily_return_z) DESC
LIMIT 6`
15. `SELECT sector, ROUND(avg_ret_5d * 100, 2) AS avg_ret_5d_pct, ROUND(avg_ret_1d * 100, 2) AS avg_ret_1d_pct, ROUND(avg_ret_1m * 100, 2) AS avg_ret_1m_pct FROM mart_sector_overview ORDER BY avg_ret_5d DESC`
16. `SELECT ticker, name, daily_return_z, trend_signal, drawdown_from_peak, close_price, ma_5, ma_20 FROM mart_momentum ORDER BY ABS(daily_return_z) DESC LIMIT 6`
17. `SELECT ticker, name, sector, asset_class, as_of_date, ret_1d, ret_5d, ret_1m FROM mart_movers ORDER BY ret_5d DESC`
18. `SELECT sector, avg_ret_5d, avg_ret_1d, top_ticker, top_ret_1d, bottom_ticker, bottom_ret_1d FROM mart_sector_overview ORDER BY avg_ret_5d DESC`
19. `SELECT ticker, name, sector, asset_class, as_of_date, ret_1d, ret_5d, ret_1m FROM mart_movers ORDER BY ret_5d ASC LIMIT 5`
20. `SELECT ticker, name, drawdown_from_peak, trend_signal, as_of_date FROM mart_momentum ORDER BY drawdown_from_peak ASC LIMIT 5`
21. `SELECT ticker, name, trend_signal, as_of_date, ma_5, ma_20, close_price FROM mart_momentum WHERE trend_signal = 'uptrend' ORDER BY close_price / ma_20 DESC LIMIT 8`
22. `SELECT MIN(as_of_date) as week_start, MAX(as_of_date) as week_end FROM mart_movers`
23. `SELECT m.ticker, m.name, m.sector, m.ret_5d,
       p_end.close_price AS close_end,
       p_end.trade_date AS date_end
FROM mart_movers m
JOIN fct_prices p_end ON m.ticker = p_end.ticker AND p_end.trade_date = m.as_of_date
ORDER BY m.ret_5d DESC
LIMIT 5`
24. `SELECT sector, ROUND(avg_ret_5d * 100, 2) AS week_ret_pct, ROUND(avg_ret_1d * 100, 2) AS day_ret_pct FROM mart_sector_overview ORDER BY avg_ret_5d DESC`
