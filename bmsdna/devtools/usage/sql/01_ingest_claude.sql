-- Claude Code: parse the transcript files listed in __FILES__ (new or changed since the last run) into the rec_* tables.
-- thread_id: the agent id for subagent files, otherwise the session id (= main thread).
CREATE OR REPLACE TEMP VIEW raw AS
SELECT
    filename, type, ts, sessionId AS session_id, isSidechain AS is_sidechain,
    CASE WHEN regexp_extract(filename, 'agent-([^/]+)\.jsonl$', 1) <> ''
         THEN regexp_extract(filename, 'agent-([^/]+)\.jsonl$', 1)
         ELSE sessionId END AS thread_id,
    cwd, aiTitle AS ai_title, message
FROM (
    SELECT *, timestamp AS ts
    FROM read_json(
        __FILES__,
        format = 'newline_delimited', union_by_name = true, ignore_errors = true,
        filename = true, maximum_object_size = 16777216,
        columns = {
            type: 'VARCHAR', timestamp: 'TIMESTAMP', sessionId: 'VARCHAR',
            isSidechain: 'BOOLEAN', cwd: 'VARCHAR', aiTitle: 'VARCHAR', message: 'JSON'
        }
    )
)
WHERE type IN ('assistant', 'user', 'ai-title');

-- Assistant records are written once per streamed content block; they are collapsed later (02_messages).
-- Only the tool_use blocks are kept from the content.
INSERT INTO rec_assistant
SELECT filename, 'claude', thread_id, session_id, ts, is_sidechain,
    json_extract_string(message, '$.id'),
    json_extract_string(message, '$.model'),
    coalesce(TRY_CAST(json_extract_string(message, '$.usage.input_tokens') AS BIGINT), 0),
    coalesce(TRY_CAST(json_extract_string(message, '$.usage.output_tokens') AS BIGINT), 0),
    coalesce(TRY_CAST(json_extract_string(message, '$.usage.cache_read_input_tokens') AS BIGINT), 0),
    coalesce(TRY_CAST(json_extract_string(message, '$.usage.cache_creation_input_tokens') AS BIGINT), 0),
    coalesce(TRY_CAST(json_extract_string(message, '$.usage.cache_creation.ephemeral_1h_input_tokens') AS BIGINT), 0),
    to_json(list_filter(from_json(json_extract(message, '$.content'), '["JSON"]'),
                        b -> json_extract_string(b, '$.type') = 'tool_use')),
    cwd
FROM raw
WHERE type = 'assistant' AND json_extract_string(message, '$.id') IS NOT NULL;

-- Images the model has been shown (tool results of Read on screenshots, MCP screenshots, pasted images).
-- Token estimate: width*height/750, capped at ~1600 (Claude downscales large images).
-- Only PNG headers are parsed for the size; anything else counts as 1500 tokens.
INSERT INTO rec_images
WITH blocks AS (
    SELECT filename, thread_id, ts, b
    FROM raw, unnest(from_json(json_extract(message, '$.content'), '["JSON"]')) AS u(b)
    WHERE type = 'user' AND json_type(json_extract(message, '$.content')) = 'ARRAY'
),
imgs AS (
    SELECT filename, thread_id, ts, c FROM blocks,
        unnest(CASE WHEN json_extract_string(b, '$.type') = 'tool_result'
                     AND json_type(json_extract(b, '$.content')) = 'ARRAY'
                    THEN from_json(json_extract(b, '$.content'), '["JSON"]') ELSE [] END) AS w(c)
    UNION ALL
    SELECT filename, thread_id, ts, b AS c FROM blocks
),
hdr AS (
    SELECT filename, thread_id, ts,
           json_extract_string(c, '$.source.media_type') AS media_type,
           length(json_extract_string(c, '$.source.data')) AS b64_len,
           hex(from_base64(substr(json_extract_string(c, '$.source.data'), 1, 32))) AS h
    FROM imgs WHERE json_extract_string(c, '$.type') = 'image'
)
SELECT filename, thread_id, ts, media_type, b64_len,
       CASE WHEN media_type = 'image/png' AND h LIKE '89504E47%' THEN
           CAST(least(1600, ceil(
               CAST(('0x' || substr(h, 33, 8)) AS BIGINT) * CAST(('0x' || substr(h, 41, 8)) AS BIGINT) / 750.0
           )) AS BIGINT)
       ELSE 1500 END AS est_tokens
FROM hdr;

INSERT INTO rec_sessions
SELECT filename, session_id, any_value(ai_title) FILTER (WHERE ai_title IS NOT NULL),
       any_value(cwd) FILTER (WHERE cwd IS NOT NULL)
FROM raw GROUP BY filename, session_id;

-- Slash-command invocations of the workflow (user typed /dev-workflow:dev-workflow).
INSERT INTO rec_cmds
SELECT filename, thread_id, session_id, min(ts)
FROM raw
WHERE type = 'user' AND CAST(message AS VARCHAR) LIKE '%<command-name>/dev-workflow%'
GROUP BY ALL;
