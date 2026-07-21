with latest as (

    select
        *,
        row_number() over (partition by ticker order by trade_date desc) as rn
    from {{ ref('int_daily_returns') }}

),

vol as (

    select
        ticker,
        stddev_samp(daily_return) as vol_daily
    from {{ ref('int_daily_returns') }}
    group by ticker

)

select
    l.ticker,
    w.name,
    w.sector,
    w.asset_class,
    l.trade_date as as_of_date,
    l.close_price,
    l.ma_5,
    l.ma_20,
    case when l.ma_5 >= l.ma_20 then 'uptrend' else 'downtrend' end as trend_signal,
    l.drawdown_from_peak,
    v.vol_daily,
    -- how extreme is today's move vs this ticker's own daily volatility
    l.daily_return / nullif(v.vol_daily, 0) as daily_return_z
from latest l
join vol v using (ticker)
join {{ ref('stg_watchlist') }} w using (ticker)
where l.rn = 1
order by (l.ma_5 / nullif(l.ma_20, 0)) desc
