-- Every assistant message with its workflow phase, actor, token counts and API-equivalent cost.
CREATE OR REPLACE TABLE pricing (model VARCHAR, input_usd DOUBLE, output_usd DOUBLE, cache_read_usd DOUBLE);
INSERT INTO pricing VALUES __PRICE_ROWS__;

CREATE OR REPLACE TABLE phases (phase VARCHAR, ord INTEGER, label VARCHAR);
INSERT INTO phases VALUES
    ('outside',       0,  'Outside workflow'),
    ('setup',         1,  'Setup & investigation (until draft PR)'),
    ('plan',          2,  'Plan'),
    ('plan_review',   3,  'Plan review'),
    ('implement',     4,  'Implement'),
    ('test',          5,  'Test / lint'),
    ('screenshots',   6,  'Screenshots'),
    ('design_review', 7,  'Design review'),
    ('review_quick',  8,  'Review (quick, mid-work)'),
    ('review_full',   9,  'Review (full, pre-publish)'),
    ('publish_ci',    10, 'Publish, CI & follow-ups'),
    ('cleanup',       11, 'Cleanup');

-- Estimated image tokens sitting in each thread's context when a message is sent.
CREATE OR REPLACE TABLE ctx_img AS
WITH ev AS (
    SELECT thread_id, ts, 0 AS is_msg, est_tokens AS tok, NULL::VARCHAR AS mid FROM images
    UNION ALL
    SELECT thread_id, ts, 1, 0, mid FROM msgs
)
SELECT mid, img_ctx_tok FROM (
    SELECT mid, is_msg,
           sum(tok) OVER (PARTITION BY thread_id ORDER BY ts, is_msg
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS img_ctx_tok
    FROM ev
) WHERE is_msg = 1;

CREATE OR REPLACE TABLE facts AS
WITH base AS (
    SELECT
        m.mid, m.thread_id, m.session_id, m.ts, m.is_sidechain, m.model, m.provider,
        p.phase,
        CASE WHEN th.thread_id IS NULL THEN 'main' ELSE 'subagent' END AS actor_kind,
        coalesce(th.agent_type, 'main') AS actor,
        th.description AS agent_desc,
        m.in_tok, m.out_tok, m.cache_read_tok, m.cache_write_tok, m.cache_write_1h_tok,
        coalesce(ci.img_ctx_tok, 0) AS img_ctx_tok,
        -- an unknown Claude model is priced like __FALLBACK_MODEL__; any other model stays unpriced (NULL cost)
        -- until you give it a price (--price MODEL=IN,OUT,CACHE_READ), instead of a made-up number
        coalesce(pr.input_usd, CASE WHEN m.model LIKE 'claude-%' THEN fb.input_usd END) AS pin,
        coalesce(pr.output_usd, CASE WHEN m.model LIKE 'claude-%' THEN fb.output_usd END) AS pout,
        coalesce(pr.cache_read_usd, CASE WHEN m.model LIKE 'claude-%' THEN fb.cache_read_usd END) AS pcr
    FROM msgs m
    JOIN msg_phase p USING (mid)
    LEFT JOIN threads th ON th.thread_id = m.thread_id
    LEFT JOIN ctx_img ci USING (mid)
    LEFT JOIN pricing pr ON pr.model = m.model
    LEFT JOIN pricing fb ON fb.model = '__FALLBACK_MODEL__'
)
SELECT *,
    pin IS NOT NULL AS priced,
    in_tok + out_tok + cache_read_tok + cache_write_tok AS total_tok,
    in_tok + out_tok + cache_write_tok AS fresh_tok,
    in_tok * pin / 1e6 AS cost_input,
    ((cache_write_tok - cache_write_1h_tok) * __CW5__ + cache_write_1h_tok * __CW1H__) * pin / 1e6 AS cost_cache_write,
    cache_read_tok * pcr / 1e6 AS cost_cache_read,
    out_tok * pout / 1e6 AS cost_output
FROM base;

ALTER TABLE facts ADD COLUMN cost DOUBLE;
UPDATE facts SET cost = cost_input + cost_cache_write + cost_cache_read + cost_output;

-- Share of the input-side cost attributable to images carried in context (estimate).
ALTER TABLE facts ADD COLUMN img_cost DOUBLE;
UPDATE facts SET img_cost =
    CASE WHEN in_tok + cache_read_tok + cache_write_tok > 0 THEN
        least(img_ctx_tok, in_tok + cache_read_tok + cache_write_tok)
        / (in_tok + cache_read_tok + cache_write_tok)::DOUBLE
        * (cost_input + cost_cache_write + cost_cache_read)
    ELSE 0 END;
