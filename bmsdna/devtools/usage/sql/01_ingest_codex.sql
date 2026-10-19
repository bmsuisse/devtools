-- codex: parse the rollout files listed in __FILES__ (new or changed since the last run) into the rec_* tables.
-- A rollout file is one thread. Spawned subagents are separate files that name their parent thread.
-- Token semantics (checked against real logs): total = input + output, cached_input is part of input and
-- reasoning is part of output.
CREATE OR REPLACE TEMP VIEW craw AS
SELECT filename, type, ordinal, timestamp AS ts, payload
FROM read_json(
    __FILES__,
    format = 'newline_delimited', union_by_name = true, ignore_errors = true,
    filename = true, maximum_object_size = 16777216,
    columns = {type: 'VARCHAR', timestamp: 'TIMESTAMP', ordinal: 'BIGINT', payload: 'JSON'}
);

CREATE OR REPLACE TEMP TABLE cfirst AS
SELECT filename,
       regexp_replace(substr(any_value(json_extract_string(payload, '$.item.content[0].text'))
                             FILTER (WHERE json_extract_string(payload, '$.item.type') = 'UserMessage'), 1, 100),
                      '\s+', ' ', 'g') AS first_message
FROM craw WHERE type = 'event_msg' GROUP BY filename;

CREATE OR REPLACE TEMP TABLE cmeta AS
SELECT m.filename,
       json_extract_string(payload, '$.id') AS thread_id,
       coalesce(json_extract_string(payload, '$.session_id'), json_extract_string(payload, '$.id')) AS session_id,
       json_extract_string(payload, '$.source.subagent.thread_spawn.parent_thread_id') AS parent_thread_id,
       coalesce(json_extract_string(payload, '$.thread_source') = 'subagent', false) AS is_sidechain,
       json_extract_string(payload, '$.cwd') AS cwd,
       ts AS start_ts,
       coalesce(nullif(regexp_extract(json_extract_string(payload, '$.agent_path'), '([^/]+)$', 1), ''), f.first_message) AS description,
       f.first_message
FROM craw m LEFT JOIN cfirst f USING (filename)
WHERE type = 'session_meta';

-- One row per model response, with the model that was active (latest turn_context before it).
CREATE OR REPLACE TEMP TABLE cusage AS
SELECT u.filename, u.ordinal, u.ts,
       json_extract_string(u.payload, '$.response_id') AS response_id,
       coalesce(TRY_CAST(json_extract_string(u.payload, '$.usage.input_tokens') AS BIGINT), 0) AS input_tokens,
       coalesce(TRY_CAST(json_extract_string(u.payload, '$.usage.cached_input_tokens') AS BIGINT), 0) AS cached_tokens,
       coalesce(TRY_CAST(json_extract_string(u.payload, '$.usage.cache_write_input_tokens') AS BIGINT), 0) AS cache_write_tokens,
       coalesce(TRY_CAST(json_extract_string(u.payload, '$.usage.output_tokens') AS BIGINT), 0) AS output_tokens,
       c.model
FROM (SELECT * FROM craw WHERE type = 'token_usage_record') u
ASOF LEFT JOIN (SELECT filename, ordinal, json_extract_string(payload, '$.model') AS model
                FROM craw WHERE type = 'turn_context') c
  ON u.filename = c.filename AND u.ordinal >= c.ordinal;

-- Tool calls, normalised to Claude's tool_use shape so the same markers work: shell and JS "exec" calls become
-- Bash (the whole script is the command, the marker regexes just look for substrings), spawn_agent becomes Agent.
CREATE OR REPLACE TEMP TABLE citems AS
SELECT filename, ordinal,
    CASE
        WHEN json_extract_string(payload, '$.type') = 'custom_tool_call' THEN
            json_object('type', 'tool_use', 'id', json_extract_string(payload, '$.call_id'), 'name', 'Bash',
                        'input', json_object('command', json_extract_string(payload, '$.input')))
        WHEN json_extract_string(payload, '$.name') = 'spawn_agent' THEN
            json_object('type', 'tool_use', 'id', json_extract_string(payload, '$.call_id'), 'name', 'Agent',
                        'input', json_object('subagent_type', '', 'prompt', '', 'description',
                            coalesce(json_extract_string(json_extract_string(payload, '$.arguments'), '$.task_name'), '')))
        WHEN json_extract_string(payload, '$.name') IN ('exec_command', 'shell', 'local_shell') THEN
            json_object('type', 'tool_use', 'id', json_extract_string(payload, '$.call_id'), 'name', 'Bash',
                        'input', json_object('command', coalesce(
                            json_extract_string(json_extract_string(payload, '$.arguments'), '$.cmd'),
                            json_extract_string(payload, '$.arguments'))))
        ELSE
            json_object('type', 'tool_use', 'id', json_extract_string(payload, '$.call_id'),
                        'name', json_extract_string(payload, '$.name'), 'input', json_object())
    END AS blk
FROM craw
WHERE type = 'response_item' AND json_extract_string(payload, '$.type') IN ('function_call', 'custom_tool_call');

-- A response's tool calls are logged before its usage record: give each call to the next usage record.
CREATE OR REPLACE TEMP TABLE cblocks AS
SELECT i.filename, u.ordinal AS response_ordinal, to_json(list(i.blk ORDER BY i.ordinal)) AS blocks
FROM citems i ASOF JOIN cusage u ON i.filename = u.filename AND i.ordinal <= u.ordinal
GROUP BY i.filename, u.ordinal;

INSERT INTO rec_assistant
SELECT u.filename, 'codex', m.thread_id, m.session_id, u.ts, m.is_sidechain, u.response_id, u.model,
       greatest(u.input_tokens - u.cached_tokens - u.cache_write_tokens, 0), u.output_tokens,
       u.cached_tokens, u.cache_write_tokens, 0,
       coalesce(b.blocks, '[]'::JSON), m.cwd
FROM cusage u
JOIN cmeta m USING (filename)
LEFT JOIN cblocks b ON b.filename = u.filename AND b.response_ordinal = u.ordinal
WHERE u.response_id IS NOT NULL;

INSERT INTO rec_threads
SELECT filename, thread_id, session_id, parent_thread_id, start_ts, cwd, description FROM cmeta;

INSERT INTO rec_sessions
SELECT filename, session_id, first_message, cwd FROM cmeta WHERE NOT is_sidechain;
