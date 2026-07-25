-- Snowflake has no SQL-standard WINDOW clause, so the per-ticker ordering that
-- used to be a named window is a Jinja constant instead: still declared once,
-- still impossible for two columns to drift apart, but portable.
{%- set by_ticker = "partition by ticker order by trade_date" %}

with base as (

    select * from {{ ref('fct_prices') }}

),

calc as (

    select
        ticker,
        trade_date,
        closing_price,
        lag(close_price)     over ({{ by_ticker }}) as prev_close,
        close_price / nullif(lag(close_price, 1)  over ({{ by_ticker }}), 0) - 1 as daily_return,
        close_price / nullif(lag(close_price, 5)  over ({{ by_ticker }}), 0) - 1 as ret_5d,
        close_price / nullif(lag(close_price, 21) over ({{ by_ticker }}), 0) - 1 as ret_1m,
        avg(close_price) over (
            {{ by_ticker }}
            rows between 4 preceding and current row
        ) as ma_5,
        avg(close_price) over (
            {{ by_ticker }}
            rows between 19 preceding and current row
        ) as ma_20,
        max(close_price) over (
            {{ by_ticker }}
            rows between unbounded preceding and current row
        ) as running_peak
    from base

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
