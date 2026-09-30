-- One row per ticker per trading day: the day's return scored against that
-- ticker's OWN trailing volatility, plus what its sector peers and the market
-- did the same day, and the resulting scope label.
--
-- The volatility window ends the day BEFORE the move. mart_momentum's
-- vol_daily uses every day in history, which is fine for a latest snapshot
-- but would let a later crash inflate the volatility used to judge an
-- earlier day. Alerts are dated events, so they only see the past.
--
-- Scope, checked in this order:
--   market_wide - the market series moved hard the same way the same day
--   sector      - enough of the ticker's sector peers moved with it
--   stock       - neither: the move belongs to this name
-- A sector- or market-wide move is researched once, not once per ticker.

{%- set by_ticker = "partition by ticker order by trade_date" %}
{%- set prior_window = "rows between " ~ var('alert_vol_window_days') ~ " preceding and 1 preceding" %}

with returns as (

    select
        r.ticker,
        r.trade_date,
        r.daily_return,
        w.sector,
        w.is_benchmark
    from {{ ref('int_daily_returns') }} r
    join {{ ref('stg_watchlist') }} w using (ticker)
    where r.daily_return is not null

),

scored as (

    select
        *,
        stddev_samp(daily_return) over ({{ by_ticker }} {{ prior_window }}) as trailing_vol,
        count(daily_return)       over ({{ by_ticker }} {{ prior_window }}) as vol_obs
    from returns

),

z as (

    select
        *,
        case
            when vol_obs >= {{ var('alert_min_history_days') }}
            then daily_return / nullif(trailing_vol, 0)
        end as z
    from scored

),

-- Leave-one-out: a ticker is never its own peer, so one huge mover cannot
-- make its own sector look like it moved.
peers as (

    select
        a.ticker,
        a.trade_date,
        count(b.ticker) as peer_n,
        avg(b.z)        as peer_avg_z,
        sum(case
                when b.z * a.z > 0 and abs(b.z) >= {{ var('scope_peer_z') }} then 1
                else 0
            end)        as peers_moving_with
    from z a
    left join z b
        on  b.sector = a.sector
        and b.trade_date = a.trade_date
        and b.ticker <> a.ticker
        and b.z is not null
    group by a.ticker, a.trade_date

),

market as (

    select trade_date, z as market_z
    from z
    where ticker = '{{ var('scope_market_ticker') }}'

)

select
    z.ticker,
    z.trade_date,
    z.sector,
    z.is_benchmark,
    z.daily_return,
    z.trailing_vol,
    z.vol_obs,
    z.z,
    p.peer_n,
    p.peer_avg_z,
    p.peers_moving_with,
    m.market_z,
    case
        when z.z is null then null
        when m.market_z * z.z > 0
             and abs(m.market_z) >= {{ var('scope_market_z') }} then 'market_wide'
        when p.peer_n > 0
             and p.peers_moving_with >= {{ var('scope_peer_share') }} * p.peer_n then 'sector'
        else 'stock'
    end as move_scope
from z
join peers p
    on p.ticker = z.ticker and p.trade_date = z.trade_date
-- Crypto trades on days the market series does not. Those days get no
-- market_z and can only be labelled sector or stock.
left join market m
    on m.trade_date = z.trade_date
