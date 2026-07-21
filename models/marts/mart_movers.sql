with returns as (

    select
        ticker,
        trade_date,
        close_price,
        close_price / nullif(lag(close_price, 1)  over w, 0) - 1 as ret_1d,
        close_price / nullif(lag(close_price, 5)  over w, 0) - 1 as ret_5d,
        close_price / nullif(lag(close_price, 21) over w, 0) - 1 as ret_1m,
        row_number() over (partition by ticker order by trade_date desc) as rn
    from {{ ref('fct_prices') }}
    window w as (partition by ticker order by trade_date)

)

select
    r.ticker,
    w.name,
    w.sector,
    w.asset_class,
    r.trade_date as as_of_date,
    r.close_prices,  -- BUG: injected typo for the self-healing demo
    r.ret_1d,
    r.ret_5d,
    r.ret_1m
from returns r
join {{ ref('stg_watchlist') }} w using (ticker)
where r.rn = 1
order by w.sector, r.ret_1d desc
