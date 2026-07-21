select
    l.ticker,
    w.name,
    w.sector,
    w.asset_class,
    l.trade_date as as_of_date,
    l.close_price,
    l.daily_return as ret_1d,
    l.ret_5d,
    l.ret_1m
from {{ ref('int_latest_daily_returns') }} l
join {{ ref('stg_watchlist') }} w using (ticker)
order by w.sector, l.daily_return desc
