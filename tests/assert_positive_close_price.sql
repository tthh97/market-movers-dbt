-- Singular test: a clean price feed should never contain a non-positive close.
-- Returns offending rows; the test passes only when zero rows are returned.
select
    ticker,
    trade_date,
    close_price
from {{ ref('stg_prices') }}
where close_price <= 0
