with base as (

    select * from {{ ref('fct_prices') }}

),

calc as (

    select
        ticker,
        trade_date,
        close_price,
        lag(close_price) over w as prev_close,
        close_price / nullif(lag(close_price, 1)  over w, 0) - 1 as daily_return,
        close_price / nullif(lag(close_price, 5)  over w, 0) - 1 as ret_5d,
        close_price / nullif(lag(close_price, 21) over w, 0) - 1 as ret_1m,
        avg(close_price) over (
            partition by ticker order by trade_date
            rows between 4 preceding and current row
        ) as ma_5,
        avg(close_price) over (
            partition by ticker order by trade_date
            rows between 19 preceding and current row
        ) as ma_20,
        max(close_price) over (
            partition by ticker order by trade_date
            rows between unbounded preceding and current row
        ) as running_peak
    from base
    window w as (partition by ticker order by trade_date)

)

select
    ticker,
    trade_date,
    close_price,
    prev_close,
    daily_return,
    ret_5d,
    ret_1m,
    ma_5,
    ma_20,
    running_peak,
    close_price / nullif(running_peak, 0) - 1 as drawdown_from_peak
from calc
