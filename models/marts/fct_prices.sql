{{
    config(
        materialized="incremental",
        unique_key="price_key",
        incremental_strategy="delete+insert"
    )
}}

with prices as (

    select * from {{ ref('stg_prices') }}

    {% if is_incremental() %}
    -- Only reprocess from the latest stored day onward. Combined with
    -- delete+insert on price_key, same-day re-runs refresh cleanly (idempotent).
    where trade_date >= (
        select coalesce(max(trade_date), date '1900-01-01') from {{ this }}
    )
    {% endif %}

)

select
    ticker || '|' || cast(trade_date as varchar) as price_key,
    ticker,
    trade_date,
    open_price,
    high_price,
    low_price,
    close_price,
    adj_close_price,
    volume,
    source,
    ingested_at
from prices
