-- One row per ticker: its most recent trading day from int_daily_returns.
-- Single source of truth for "latest row per ticker" - mart_movers,
-- mart_momentum, and mart_portfolio_bias all select from this instead of
-- each re-deriving their own row_number() over ticker/trade_date.

with ranked as (

    select
        *,
        row_number() over (partition by ticker order by trade_date desc) as rn
    from {{ ref('int_daily_returns') }}

)

select * exclude (rn)
from ranked
where rn = 1
