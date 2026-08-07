{{
    config(
        materialized="incremental",
        unique_key="price_key",
        incremental_strategy="merge",
        merge_update_columns=[
            "open_price", "high_price", "low_price", "close_price",
            "adj_close_price", "volume", "source", "ingested_at"
        ]
    )
}}

with prices as (

    select * from {{ ref('stg_prices') }}

    {% if is_incremental() %}
    -- Only reprocess from the latest stored day onward. The MERGE then upserts
    -- on price_key, so a same-day re-run refreshes those rows in place rather
    -- than duplicating them (idempotent). trade_date >= max keeps the scan
    -- small; price_key is what actually decides insert-vs-update.
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
