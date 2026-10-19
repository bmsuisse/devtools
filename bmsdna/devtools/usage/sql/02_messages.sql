-- One row per subagent: who spawned it (the Agent tool_use id), what type, how deep.
CREATE OR REPLACE TABLE agent_meta AS
SELECT
    regexp_extract(filename, 'agent-([^/]+)\.meta\.json$', 1) AS thread_id,
    agentType AS agent_type,
    description,
    toolUseId AS parent_tool_use_id,
    spawnDepth AS depth,
    model AS requested_model
FROM __META_SOURCE__;

-- Session labels (title + repo) for readable per-session tables.
CREATE OR REPLACE TABLE sessions AS
SELECT
    session_id,
    any_value(ai_title) FILTER (WHERE ai_title IS NOT NULL) AS title,
    regexp_extract(any_value(cwd) FILTER (WHERE cwd IS NOT NULL),
                   '/(?:projects/)?([^/]+?)(?:/\.worktrees/.*)?$', 1) AS repo
FROM rec_sessions
GROUP BY session_id;

CREATE OR REPLACE TABLE workflow_commands AS
SELECT thread_id, session_id, min(ts) AS ts FROM rec_cmds GROUP BY ALL;

-- Assistant records are written once per streamed content block, with a growing
-- output_tokens. Collapse them to one row per API message (mid): keep the final usage.
-- A message copied into several files (forks) is kept once, preferring the main thread.
CREATE OR REPLACE TABLE msgs AS
SELECT
    mid,
    arg_min(thread_id, (ts, is_sidechain)) AS thread_id,
    arg_min(session_id, (ts, is_sidechain)) AS session_id,
    arg_min(is_sidechain, (ts, is_sidechain)) AS is_sidechain,
    arg_min(provider, (ts, is_sidechain)) AS provider,
    arg_min(cwd, (ts, is_sidechain)) AS cwd,
    min(ts) AS ts,
    arg_max(model, out_tok) AS model,
    arg_max(in_tok, out_tok) AS in_tok,
    max(out_tok) AS out_tok,
    arg_max(cache_read_tok, out_tok) AS cache_read_tok,
    arg_max(cache_write_tok, out_tok) AS cache_write_tok,
    arg_max(cache_write_1h_tok, out_tok) AS cache_write_1h_tok
FROM rec_assistant
WHERE provider IN (__PROVIDERS__)
GROUP BY mid;

CREATE OR REPLACE TABLE tool_uses AS
SELECT DISTINCT ON (tool_use_id) mid, tool_use_id, name, input
FROM (
    SELECT mid,
           json_extract_string(b, '$.id') AS tool_use_id,
           json_extract_string(b, '$.name') AS name,
           json_extract(b, '$.input') AS input
    FROM rec_assistant, unnest(from_json(tool_blocks, '["JSON"]')) AS u(b)
)
WHERE mid IN (SELECT mid FROM msgs);

CREATE OR REPLACE TABLE images AS SELECT thread_id, ts, media_type, b64_len, est_tokens FROM rec_images;

-- Claude Bash calls that start a codex run (codex exec / codex review): the usage of that run is only in codex's
-- own logs, so it gets linked to the call below.
CREATE OR REPLACE TABLE codex_launches AS
SELECT m.mid, m.ts, m.cwd, lower(json_extract_string(t.input, '$.command')) AS cmd
FROM tool_uses t JOIN msgs m USING (mid)
WHERE m.provider = 'claude' AND t.name = 'Bash'
  AND regexp_matches(lower(json_extract_string(t.input, '$.command')), '(^|[\s;&|(])codex (exec|review|e )');

-- Spawned threads and the message that spawned them (parent_mid): Claude subagents via the Agent call's tool_use id,
-- codex subagents via their parent thread (the parent's latest message before they started), codex runs
-- launched from Claude via the launching call (same directory, started within 15 minutes after it).
-- Threads without a row here are main threads.
CREATE OR REPLACE TABLE threads AS
SELECT am.thread_id, 'claude' AS provider, am.agent_type, am.description, tu.mid AS parent_mid
FROM agent_meta am LEFT JOIN tool_uses tu ON tu.tool_use_id = am.parent_tool_use_id
UNION ALL
SELECT ct.thread_id, 'codex', 'codex-subagent', ct.description, pm.mid
FROM rec_threads ct ASOF LEFT JOIN msgs pm ON pm.thread_id = ct.parent_thread_id AND ct.start_ts >= pm.ts
WHERE ct.parent_thread_id IS NOT NULL
UNION ALL
SELECT thread_id, 'codex', 'codex', description, mid FROM (
    SELECT ct.thread_id, ct.description, l.mid,
           row_number() OVER (PARTITION BY ct.thread_id ORDER BY l.ts DESC) AS rn
    FROM rec_threads ct JOIN codex_launches l
      ON ct.parent_thread_id IS NULL
     AND ct.start_ts >= l.ts AND ct.start_ts < l.ts + INTERVAL 15 MINUTE
     AND (l.cwd = ct.cwd OR contains(l.cmd, lower(ct.cwd)))
) WHERE rn = 1;

-- A codex thread belongs to the session of whatever (transitively) launched it, so a review that codex ran for a
-- Claude session is counted in that session.
UPDATE msgs SET session_id = ts.session_id
FROM (
    WITH RECURSIVE chain(thread_id, session_id) AS (
        SELECT t.thread_id, p.session_id
        FROM threads t JOIN msgs p ON p.mid = t.parent_mid
        WHERE t.provider = 'codex' AND p.provider = 'claude'
        UNION
        SELECT t.thread_id, c.session_id
        FROM threads t JOIN msgs p ON p.mid = t.parent_mid JOIN chain c ON c.thread_id = p.thread_id
        WHERE t.provider = 'codex'
    )
    SELECT thread_id, any_value(session_id) AS session_id FROM chain GROUP BY thread_id
) ts
WHERE msgs.thread_id = ts.thread_id;

