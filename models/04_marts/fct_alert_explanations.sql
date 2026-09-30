-- The current explanation for each alert: the latest research run wins.
-- raw.alert_explanations keeps every run, so an older explanation is never
-- lost, only superseded. The numbers an explanation refers to stay in
-- fct_price_alerts; this table carries words, sources, and verdicts only.

select
    e.alert_id,
    e.research_unit_id,
    e.cause_summary,
    e.category,
    e.sources,
    e.confidence,
    e.confidence_reason,
    e.verifier_status,
    e.verifier_notes,
    e.model,
    e.run_id,
    e.researched_at
from {{ source('raw', 'alert_explanations') }} e
join {{ ref('fct_price_alerts') }} a using (alert_id)
qualify row_number() over (partition by e.alert_id order by e.researched_at desc) = 1
