-- Persistent cache of parsed log records. Only new/changed transcript files are re-read (see pipeline.ingest);
-- everything derived from these tables (msgs, threads, markers, phases, facts, views) is rebuilt on every run.
-- Both providers (claude, codex) end up in the same shape: one row per model response in rec_assistant, its tool
-- calls normalised to Claude's {type: tool_use, name, input} so the step markers work for both.
CREATE TABLE IF NOT EXISTS cache_meta (key VARCHAR, value VARCHAR);
CREATE TABLE IF NOT EXISTS src_files (filename VARCHAR, size BIGINT, mtime_ns BIGINT);
CREATE TABLE IF NOT EXISTS rec_assistant (
    filename VARCHAR, provider VARCHAR, thread_id VARCHAR, session_id VARCHAR, ts TIMESTAMP, is_sidechain BOOLEAN,
    mid VARCHAR, model VARCHAR, in_tok BIGINT, out_tok BIGINT, cache_read_tok BIGINT,
    cache_write_tok BIGINT, cache_write_1h_tok BIGINT, tool_blocks JSON, cwd VARCHAR);
CREATE TABLE IF NOT EXISTS rec_images (
    filename VARCHAR, thread_id VARCHAR, ts TIMESTAMP, media_type VARCHAR, b64_len BIGINT, est_tokens BIGINT);
CREATE TABLE IF NOT EXISTS rec_sessions (filename VARCHAR, session_id VARCHAR, ai_title VARCHAR, cwd VARCHAR);
CREATE TABLE IF NOT EXISTS rec_cmds (filename VARCHAR, thread_id VARCHAR, session_id VARCHAR, ts TIMESTAMP);
-- codex only: one row per rollout file (= thread), with its parent for spawned subagents
CREATE TABLE IF NOT EXISTS rec_threads (
    filename VARCHAR, thread_id VARCHAR, session_id VARCHAR, parent_thread_id VARCHAR, start_ts TIMESTAMP,
    cwd VARCHAR, description VARCHAR);
