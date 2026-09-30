-- Every rule must target a group that exists. A typo in alert_rules.group_id
-- would otherwise match no tickers and silently produce zero alerts.
with known as (
    select 'all' as group_id
    union
    select distinct sector from {{ ref('stg_watchlist') }} where not is_benchmark
    union
    select distinct theme from {{ ref('alert_themes') }}
)

select r.rule_id, r.group_id
from {{ ref('alert_rules') }} r
left join known k on k.group_id = r.group_id
where k.group_id is null
