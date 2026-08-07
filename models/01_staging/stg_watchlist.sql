select
    ticker,
    name,
    sector,
    asset_class,
    is_holding,
    is_benchmark
from {{ ref('watchlist') }}
