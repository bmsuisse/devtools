-- Workflow step markers. Each tool call may carry a marker: [phase, kind].
--   once         : counts only for the message that makes the call (e.g. a test run), the thread's
--                  sticky phase continues underneath
--   start        : thread begins following dev-workflow (skill call)
--   skill        : strong, applies even in non-workflow threads (review/design/plan skills, agent spawns)
--   review_agent : spawn of a bms-review-* agent; becomes review_full unless already in a review phase
--   cmd | edit   : applies in threads that follow the workflow
--   soft         : like cmd, but does not interrupt a design_review
-- Tune the patterns here; everything downstream is derived.
CREATE OR REPLACE TABLE tool_markers AS
WITH t AS (
    SELECT mid, tool_use_id, name,
           coalesce(json_extract_string(input, '$.command'), '') AS cmd,
           coalesce(json_extract_string(input, '$.file_path'), '') AS path,
           coalesce(json_extract_string(input, '$.skill'), '') AS skill,
           coalesce(json_extract_string(input, '$.args'), '') AS args,
           coalesce(json_extract_string(input, '$.subagent_type'), '') AS subagent_type,
           lower(coalesce(json_extract_string(input, '$.description'), '') || ' '
                 || coalesce(json_extract_string(input, '$.prompt'), '')) AS agent_text
    FROM tool_uses
),
m AS (
    SELECT t.mid, t.tool_use_id, t.name,
        CASE
            WHEN name = 'Skill' AND regexp_matches(skill, '(^|:)dev-workflow$') THEN ['setup', 'start']
            WHEN name = 'Skill' AND regexp_matches(skill, '(^|:)bms-plan-review$') THEN ['plan_review', 'skill']
            WHEN name = 'Skill' AND regexp_matches(skill, '(^|:)design-review$') THEN ['design_review', 'skill']
            WHEN name = 'Skill' AND regexp_matches(skill, '(^|:)bms-code-review$')
                THEN [CASE WHEN args LIKE '%--quick%' THEN 'review_quick' ELSE 'review_full' END, 'skill']
            WHEN name = 'Skill' AND regexp_matches(skill, '(^|:)code-review$')
                THEN [CASE WHEN regexp_matches(args, '^\s*low') THEN 'review_quick' ELSE 'review_full' END, 'skill']
            WHEN name IN ('Agent', 'Task') AND regexp_matches(subagent_type, 'bms-review-') THEN ['review_full', 'review_agent']
            WHEN name IN ('Agent', 'Task') AND regexp_matches(agent_text, 'design[- ]review') THEN ['design_review', 'skill']
            WHEN name = 'EnterWorktree' THEN ['setup', 'cmd']
            WHEN name IN ('EnterPlanMode', 'ExitPlanMode') THEN ['plan', 'cmd']
            WHEN name LIKE 'mcp__chrome-devtools__%' OR name LIKE 'mcp__omnigent__browser_%' THEN ['screenshots', 'cmd']
            WHEN name IN ('Edit', 'Write', 'MultiEdit', 'NotebookEdit') THEN
                CASE WHEN regexp_matches(path, '/scratchpad/|^/tmp/')
                     THEN CASE WHEN regexp_matches(path, 'plan[^/]*\.md$') THEN ['plan', 'edit'] END
                     ELSE ['implement', 'edit'] END
            WHEN name = 'Read' AND regexp_matches(lower(path), '\.(png|jpe?g|webp)$') THEN ['screenshots', 'soft']
            WHEN name = 'Bash' THEN
                CASE
                    -- codex has no Skill tool: a skill is "invoked" by reading its SKILL.md; edits go through apply_patch
                    WHEN regexp_matches(cmd, 'dev-workflow/SKILL\.md') THEN ['setup', 'start']
                    WHEN regexp_matches(cmd, 'bms-plan-review/SKILL\.md') THEN ['plan_review', 'skill']
                    WHEN regexp_matches(cmd, 'design-review/SKILL\.md') THEN ['design_review', 'skill']
                    WHEN regexp_matches(cmd, 'bms-code-review/SKILL\.md') THEN ['review_full', 'skill']
                    WHEN regexp_matches(cmd, 'apply_patch|\*\*\* (Update|Add) File:') THEN ['implement', 'edit']
                    WHEN regexp_matches(cmd, 'git worktree add|just worktree|bdt worktree|bdt issue take') THEN ['setup', 'cmd']
                    WHEN regexp_matches(cmd, 'bdt pr create') THEN ['plan', 'cmd']
                    WHEN regexp_matches(cmd, 'bdt pr comment.*(png|screenshot)') THEN ['screenshots', 'cmd']
                    WHEN regexp_matches(cmd, 'bdt pr (publish|status|info)') THEN ['publish_ci', 'cmd']
                    WHEN regexp_matches(cmd, 'bdt cleanup') THEN ['cleanup', 'cmd']
                    WHEN regexp_matches(cmd, '(^|[\s;&|(])codex (exec|review)') THEN ['review_full', 'cmd']
                    WHEN regexp_matches(cmd, 'pytest|vitest|jest|bdt lint|ruff|pyright|\bty check|\btsc\b|npm (run )?(test|lint|typecheck)|just (test|lint|check)')
                         AND NOT regexp_matches(cmd, 'screenshot') THEN ['test', 'once']
                    WHEN regexp_matches(cmd, 'playwright|screenshot|chromium') THEN ['screenshots', 'cmd']
                END
        END AS mk
    FROM t
)
SELECT m.mid, m.tool_use_id, mk[1] AS phase, mk[2] AS kind,
       -- evidence that a subagent is running the whole workflow itself
       (mk[2] = 'start' OR (t.name = 'Bash' AND regexp_matches(t.cmd,
            'bdt pr (create|publish)|git worktree add|just worktree|bdt worktree'))) AS wf_signal
FROM m JOIN t USING (tool_use_id) WHERE mk IS NOT NULL;
