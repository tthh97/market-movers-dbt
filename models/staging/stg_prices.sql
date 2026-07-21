with source as (

    select * from {{ source('raw', 'prices') }}

),

renamed as (

    select
        ticker,
        cast(trade_date as date)        as trade_date,
        cast(open as double)            as open_price,
        cast(high as double)            as high_price,
        cast(low as double)             as low_price,
        cast(close as double)           as close_price,
        cast(adj_close as double)       as adj_close_price,
        cast(volume as bigint)          as volume,
        source,
        cast(ingested_at as timestamp)  as ingested_at,
        -- keep the most recently ingested row if a ticker-day is loaded twice
        row_number() over (
            partition by ticker, cast(trade_date as date)
            order by ingested_at desc
        ) as _row_rank
    from source

)

select
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
from renamed
where _row_rank = 1
  and close_price is not null
