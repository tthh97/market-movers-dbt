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
    -- Reprocess a trailing WINDOW, not everything from the high-water mark
    -- onward. The MERGE upserts on price_key, so re-reading days already stored
    -- is free of duplicates; it just gives late data somewhere to land.
    --
    -- A bare `trade_date >= max(trade_date)` looks equivalent and is not. It
    -- discards anything arriving at or below the mark BEFORE the MERGE can see
    -- it, and the mark only ever moves forward, so such a row can never arrive.
    -- That cuts two ways and both were live in production: a new row below the
    -- line was never inserted, and a corrected row below the line was never
    -- updated, leaving merge_update_columns with nothing to do.
    --
    -- It bit here because the watchlist mixes trading calendars. Crypto trades
    -- 7 days a week and equities do not, so crypto always holds the global
    -- max(trade_date) and drags the mark past the equity calendar. A backfill
    -- on 2026-08-06 carried equity rows for 07-28 and 08-03; the mark had
    -- already reached 08-04, so 34 rows were dropped and 3 crypto corrections
    -- were blocked. Nothing failed: uniqueness and not-null both still hold on
    -- a table with holes, which is what assert_fct_prices_complete now catches.
    --
    -- The window bounds the exposure rather than removing it: a backfill older
    -- than the lookback is still dropped, and the completeness test is what
    -- makes that loud. 30 days is ~570 rows here, so the scan is not worth
    -- optimising.
    where trade_date >= (
        select coalesce(
            {{ dbt.dateadd('day', -var('fct_prices_lookback_days', 30), 'max(trade_date)') }},
            date '1900-01-01'
        )
        from {{ this }}
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
