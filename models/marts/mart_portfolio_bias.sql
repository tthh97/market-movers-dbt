with holdings as (

    select ticker from {{ ref('stg_watchlist') }} where is_holding

),

benchmark as (

    -- Nasdaq-100 proxy used as the "risk asset" reference
    select trade_date, daily_return as bench_return
    from {{ ref('int_daily_returns') }}
    where ticker = 'QQQ'

),

holding_returns as (

    select i.ticker, i.trade_date, i.daily_return, i.drawdown_from_peak
    from {{ ref('int_daily_returns') }} i
    join holdings h using (ticker)

),

corr_calc as (

    select
        hr.ticker,
        corr(hr.daily_return, b.bench_return) as corr_to_nasdaq,
        count(*)                              as overlapping_days
    from holding_returns hr
    join benchmark b using (trade_date)
    group by hr.ticker

),

current_dd as (

    select ticker, drawdown_from_peak as current_drawdown
    from (
        select
            ticker,
            drawdown_from_peak,
            row_number() over (partition by ticker order by trade_date desc) as rn
        from holding_returns
    )
    where rn = 1

)

select
    c.ticker,
    w.name,
    c.corr_to_nasdaq,
    c.overlapping_days,
    d.current_drawdown
from corr_calc c
join {{ ref('stg_watchlist') }} w using (ticker)
left join current_dd d using (ticker)
order by c.corr_to_nasdaq desc
