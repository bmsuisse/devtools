-- All "v_" views only look at in-scope rows (phase <> 'outside') and report shares of that total.
-- `facts` keeps cost NULL for turns of a model without a price; the views count those as 0 (they say how many there
-- are: v_summary.unpriced_msgs) so every number below is a plain number.
CREATE OR REPLACE VIEW f AS
SELECT * REPLACE (coalesce(cost, 0) AS cost, coalesce(img_cost, 0) AS img_cost, coalesce(cost_input, 0) AS cost_input,
                  coalesce(cost_cache_write, 0) AS cost_cache_write, coalesce(cost_cache_read, 0) AS cost_cache_read,
                  coalesce(cost_output, 0) AS cost_output)
FROM facts WHERE phase <> 'outside';

CREATE OR REPLACE VIEW v_steps AS
SELECT p.ord, p.phase, p.label,
       count(*) AS msgs,
       count(DISTINCT f.session_id) AS sessions,
       sum(total_tok) AS tokens, sum(fresh_tok) AS fresh_tokens, sum(cost) AS cost,
       avg(in_tok + cache_read_tok + cache_write_tok) AS avg_context_tok,
       100 * sum(total_tok) / sum(sum(total_tok)) OVER () AS pct_tokens,
       100 * sum(fresh_tok) / sum(sum(fresh_tok)) OVER () AS pct_fresh_tokens,
       100 * sum(cost) / sum(sum(cost)) OVER () AS pct_cost,
       100 * sum(cost) FILTER (WHERE actor_kind = 'subagent') / nullif(sum(cost), 0) AS pct_of_step_via_subagents
FROM f JOIN phases p USING (phase)
GROUP BY p.ord, p.phase, p.label ORDER BY p.ord;

CREATE OR REPLACE VIEW v_steps_actor AS
SELECT p.ord, p.phase, p.label, actor_kind,
       count(*) AS msgs, sum(total_tok) AS tokens, sum(cost) AS cost,
       100 * sum(cost) / sum(sum(cost)) OVER () AS pct_cost
FROM f JOIN phases p USING (phase)
GROUP BY p.ord, p.phase, p.label, actor_kind ORDER BY p.ord, actor_kind;

CREATE OR REPLACE VIEW v_steps_token_kind AS
SELECT p.ord, p.phase, p.label, kind, cost
FROM (SELECT phase,
             sum(cost_cache_read) AS "cache read", sum(cost_cache_write) AS "cache write",
             sum(cost_input) AS "uncached input", sum(cost_output) AS "output"
      FROM f GROUP BY phase) s
UNPIVOT (cost FOR kind IN ("cache read", "cache write", "uncached input", "output"))
JOIN phases p USING (phase) ORDER BY p.ord;

-- Per-session shares: robust against one huge session dominating the totals.
CREATE OR REPLACE VIEW v_session_steps AS
SELECT f.session_id, coalesce(s.title, f.session_id) AS title, s.repo,
       p.ord, p.phase, p.label, sum(cost) AS cost,
       100 * sum(cost) / sum(sum(cost)) OVER (PARTITION BY f.session_id) AS pct_of_session
FROM f JOIN phases p USING (phase) LEFT JOIN sessions s USING (session_id)
GROUP BY f.session_id, s.title, s.repo, p.ord, p.phase, p.label;

CREATE OR REPLACE VIEW v_session_totals AS
SELECT f.session_id, coalesce(s.title, f.session_id) AS title, s.repo,
       min(ts) AS started, count(*) AS msgs, sum(total_tok) AS tokens, sum(cost) AS cost
FROM f LEFT JOIN sessions s USING (session_id)
GROUP BY ALL ORDER BY cost DESC;

-- Median of the per-session share, next to the share of the grand total.
CREATE OR REPLACE VIEW v_step_medians AS
WITH all_pairs AS (
    SELECT st.session_id, ph.ord, ph.phase, ph.label
    FROM (SELECT DISTINCT session_id FROM f) st CROSS JOIN phases ph WHERE ph.phase <> 'outside'
)
SELECT a.ord, a.phase, a.label,
       median(coalesce(v.pct_of_session, 0)) AS median_pct_of_session,
       avg(coalesce(v.pct_of_session, 0)) AS mean_pct_of_session,
       count(*) FILTER (WHERE v.pct_of_session IS NOT NULL) AS sessions_with_step
FROM all_pairs a LEFT JOIN v_session_steps v USING (session_id, phase)
GROUP BY ALL ORDER BY a.ord;

-- Subagents: what they were, which step spawned them, what they cost.
CREATE OR REPLACE VIEW v_agents AS
SELECT thread_id, any_value(session_id) AS session_id, any_value(actor) AS agent_type,
       any_value(agent_desc) AS description,
       mode(phase) AS dominant_phase, count(*) AS msgs, sum(total_tok) AS tokens, sum(cost) AS cost
FROM f WHERE actor_kind = 'subagent'
GROUP BY thread_id ORDER BY cost DESC;

CREATE OR REPLACE VIEW v_agent_types AS
SELECT agent_type, dominant_phase AS phase, count(*) AS agents, sum(cost) AS cost, avg(cost) AS avg_cost_per_agent,
       sum(tokens) AS tokens, 100 * sum(cost) / sum(sum(cost)) OVER () AS pct_cost_of_workflow
FROM v_agents GROUP BY agent_type, dominant_phase ORDER BY cost DESC;

-- Screenshot hypothesis: how much of the bill is images re-read from context on every later turn?
CREATE OR REPLACE VIEW v_images AS
SELECT p.ord, p.phase, p.label,
       sum(img_cost) AS img_carry_cost, sum(cost) AS cost,
       100 * sum(img_cost) / nullif(sum(cost), 0) AS pct_of_step_cost,
       100 * sum(img_cost) / sum(sum(cost)) OVER () AS pct_of_workflow_cost
FROM f JOIN phases p USING (phase)
GROUP BY p.ord, p.phase, p.label ORDER BY p.ord;

CREATE OR REPLACE VIEW v_providers AS
SELECT provider, count(*) AS msgs, sum(total_tok) AS tokens, sum(fresh_tok) AS fresh_tokens, sum(cost) AS cost,
       count(*) FILTER (WHERE NOT priced) AS unpriced_msgs,
       100 * sum(total_tok) / sum(sum(total_tok)) OVER () AS pct_tokens
FROM f GROUP BY provider ORDER BY tokens DESC;

CREATE OR REPLACE VIEW v_summary AS
SELECT count(DISTINCT session_id) AS sessions, count(DISTINCT thread_id) AS threads, count(*) AS msgs,
       sum(total_tok) AS tokens, sum(fresh_tok) AS fresh_tokens, sum(cost) AS cost,
       100 * sum(cost) FILTER (WHERE actor_kind = 'subagent') / sum(cost) AS pct_cost_subagents,
       (SELECT count(*) FROM images i WHERE i.thread_id IN (SELECT thread_id FROM f)) AS images,
       count(*) FILTER (WHERE NOT priced) AS unpriced_msgs,
       string_agg(DISTINCT model, ', ') FILTER (WHERE NOT priced) AS unpriced_models,
       min(ts) AS first_ts, max(ts) AS last_ts
FROM f;
