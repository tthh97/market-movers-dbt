-- Every ticker-day the source holds must exist in the fact table.
--
-- The 26 shape tests (unique, not_null, accepted_values, relationships) all pass
-- on a table with holes: they check that the rows present are well-formed, never
-- that the rows expected are present. Production sat 34 rows short for eleven
-- days with a fully green build.
--
-- This is the assertion that makes a silent gap loud. It compares the fact table
-- against stg_prices, a view over current RAW, so it re-evaluates on every run
-- rather than encoding a fixed expected count that would rot.
--
-- Deliberately not a relationships test. That one checks fct_prices.ticker
-- resolves to a real watchlist entry - the opposite direction, and satisfied by
-- an empty table.

select
    s.ticker,
    s.trade_date
from {{ ref('stg_prices') }} s
left join {{ ref('fct_prices') }} f
    on  f.ticker     = s.ticker
    and f.trade_date = s.trade_date
where f.ticker is null
