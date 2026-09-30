-- One row per alert: a non-benchmark ticker whose move on a given day cleared
-- an enabled rule in the alert_rules seed. When several rules match the same
-- move (a 4.0 and a 2.5 threshold), the strictest one wins, so a move is one
-- alert with one severity.
--
-- A rule's group_id is "all", a sector from the watchlist, or a theme from
-- alert_themes. A ticker can belong to several themes. Every alert in history
-- is kept: capping how many get researched per night is the consumer's job.

with context as (

    select *
    from {{ ref('int_move_context') }}
    where z is not null
      and not is_benchmark

),

memberships as (

    select ticker, 'all' as group_id
    from {{ ref('stg_watchlist') }}
    where not is_benchmark

    union all

    select ticker, sector as group_id
    from {{ ref('stg_watchlist') }}
    where not is_benchmark

    union all

    select ticker, theme as group_id
    from {{ ref('alert_themes') }}

),

rules as (

    select *
    from {{ ref('alert_rules') }}
    where enabled
      and event_type = 'z_move'

),

matched as (

    select
        c.*,
        r.rule_id,
        r.event_type,
        r.severity,
        r.z_threshold,
        row_number() over (
            partition by c.ticker, c.trade_date, r.event_type
            order by r.z_threshold desc, r.rule_id
        ) as rn
    from context c
    join memberships g on g.ticker = c.ticker
    join rules r
        on  r.group_id = g.group_id
        and abs(c.z) >= r.z_threshold

)

select
    md5(ticker || '|' || cast(trade_date as varchar) || '|' || event_type) as alert_id,
    -- One cause is researched once: every alert on a market-wide day shares a
    -- unit, a sector-wide day is one unit per sector, a stock move is its own.
    md5(cast(trade_date as varchar) || '|' || case move_scope
        when 'market_wide' then 'market'
        when 'sector'      then sector
        else ticker
    end) as research_unit_id,
    ticker,
    sector,
    trade_date,
    event_type,
    rule_id,
    severity,
    z_threshold,
    daily_return,
    z,
    trailing_vol,
    move_scope,
    peer_n,
    peers_moving_with,
    peer_avg_z,
    market_z
from matched
where rn = 1
