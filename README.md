# bmsdna-devtools

Shared developer tooling for BMS projects: PR build/check status, PR
creation, git worktrees, a commit-and-push helper with pre-flight checks,
and Azure log queries. `bdt pr *` auto-detects whether the current repo's
`origin` remote is Azure DevOps or GitHub and uses `az`/`gh` accordingly.
Consolidates near-duplicate scripts that used to be copy-pasted across
OneSales, ccmt2, and MDMApp into one versioned package with a `bdt` CLI.

Requires `git` always, plus `az` (Azure DevOps commands, and all `bdt logs`
commands) and/or `gh` (GitHub commands) on PATH as needed — each is checked
lazily, only when a command actually needs it, with a clear error and an
install link if missing rather than a raw traceback. Works on Windows: CLI
shims (e.g. `az.cmd`) are resolved via `shutil.which` (which honors
`PATHEXT`) rather than shelling out, output is decoded as UTF-8 rather than
relying on the console's default codepage, and file arguments accept either
slash style.

## Install

```bash
uv tool install bmsdna-devtools
```

Or as a project dependency: `uv add bmsdna-devtools`.

## `bdt pr status`

Find the PR opened from the current branch and report build/check status
(failed steps print their logs inline). Works against Azure DevOps or
GitHub — whichever `origin` points at.

```bash
bdt pr status [--target-branch main] [--wait]
```

If the PR can't be merged, that's reported immediately instead of polling
for builds/checks that will never run — e.g. on Azure DevOps:
`PR #42 ('feat: widgets') has merge conflicts with the target branch (mergeStatus=conflicts)`;
on GitHub: `PR #42 ('feat: widgets') has merge conflicts with 'main' (mergeable=CONFLICTING)`.
Exit code 1 either way.

**Azure DevOps**: org/project/repo are auto-detected from
`git remote get-url origin` (handles SSH, `dev.azure.com` HTTPS, and
`*.visualstudio.com` HTTPS forms). Auth is an explicit PAT (`--pat` or
`AZURE_DEVOPS_PAT` env var), falling back to a short-lived token from the
caller's own `az login` — never embed a PAT literal in a script or CI file.
`--target-branch` selects which PR to look at (ADO's search API needs one).

**GitHub**: uses `gh`'s own auth (`gh auth login`) and always resolves the
PR opened from the current branch — `gh pr view` has no target-branch
filter, so `--target-branch` is ignored here; the PR's actual base branch
is shown in the output. Check status is computed from
`gh pr view --json statusCheckRollup` rather than `gh pr checks --json`,
since the latter flag isn't available in all `gh` releases.

## `bdt pr create`

```bash
bdt pr create --target main   # or --target test
```

Creates a PR from the current branch into `--target`. On Azure DevOps,
a thin wrapper around `az repos pr create` (org/project/repo inferred by
`az` itself from the git remote). On GitHub, `gh pr create --fill` (autofills
title/body from commit info so it never blocks on an interactive prompt).
Extra arguments pass through either way, e.g.
`bdt pr create --target main -- --title "..."`.

## `bdt worktree`

```bash
bdt worktree my-feature [--base dev] [--env-file .local_env] [--no-submodules] [--install "just install"]
```

Creates `.worktrees/<name>` branched from `--base`, initializes submodules
(unless `--no-submodules`), and copies an env file into the new worktree as
`.env` (auto-detects `.local_env` then `.env` if `--env-file` isn't given).

## `bdt commit`

```bash
bdt commit "feat(x): add widget support" file1.py file2.py [--json] [--no-verify] [--subrepo database]
```

Stages, commits, and pushes the given files. Pre-flight checks: files
exist, commit message looks like `type: description` (skip with
`--skip-message-check`), not on `main`/`master` (skip with `--allow-main`).
Retries once (re-`git add`) if a pre-commit hook reformats files. Pass
`--subrepo <dir>` (repeatable) for repos that vendor a submodule (e.g.
`database`) — files under that prefix are committed/pushed inside the
submodule first, then the bump is staged in the parent repo.

Set `IS_BMS_AI_SANDBOX=1` to skip the push step (commit only) — used when
an AI coding sandbox pushes on its own schedule separately.

`--json` emits a machine-readable result for AI-agent callers:

```json
{
  "success": true, "committed": true, "pushed": true,
  "message": "...", "files": ["..."], "commit_sha": "abc1234",
  "error": null, "hint": null
}
```

## `bdt logs roles` / `bdt logs tail`

Query Application Insights (KQL over `traces`/`exceptions`) via
`az monitor app-insights query`. No defaults are baked in — pass
`--resource-group`/`--app-insights` explicitly (or set
`AZURE_RESOURCE_GROUP`/`AZURE_APP_INSIGHTS`), since which Azure resource
"this repo" maps to isn't derivable from the git remote.

```bash
bdt logs roles --resource-group my-rg --app-insights my-app-insights --minutes 60
bdt logs tail --resource-group my-rg --app-insights my-app-insights --role my-service --level warning
```

## `bdt logs fetch`

Downloads the App Service log archive for a webapp/slot via
`az webapp log download`, unzips it, and writes every line matching a
common error/warning marker (`ERROR`, `CRITICAL`, `WARNING`, tracebacks,
`4xx`/`5xx`, `FAILED`, `FATAL`) to `<out>/<slot>_errors.log`. Simpler and
often preferable to the KQL commands above when you just want "what broke
recently" rather than a queryable trace stream.

The webapp/resource-group/slot come from a named environment configured in
the calling repo's `pyproject.toml`:

```toml
[tool.bdt.envs.prod]
webapp = "my-webapp"
resource_group = "my-rg"
slot = "production"
```

```bash
bdt logs fetch --env prod
bdt logs fetch --env prod --out logs/ --keep-archive
```
