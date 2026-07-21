with movers as (

    -- Benchmarks (SPY, QQQ) are reference series, not a real sector; exclude them
    -- so this rolls up to exactly the four tracked sectors.
    select * from {{ ref('mart_movers') }}
    where sector <> 'benchmark'

)

select
    sector,
    count(*)                                                    as n_names,
    avg(ret_1d)                                                 as avg_ret_1d,
    avg(ret_5d)                                                 as avg_ret_5d,
    avg(ret_1m)                                                 as avg_ret_1m,
    -- breadth: share of names up on the day
    sum(case when ret_1d > 0 then 1 else 0 end) * 1.0 / count(*) as pct_up_1d,
    -- best and worst name in the sector today
    max_by(ticker, ret_1d)                                      as top_ticker,
    max(ret_1d)                                                 as top_ret_1d,
    min_by(ticker, ret_1d)                                      as bottom_ticker,
    min(ret_1d)                                                 as bottom_ret_1d
from movers
group by sector
order by avg_ret_1d desc
