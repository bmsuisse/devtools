# bmsdna-devtools

Shared developer tooling for BMS projects that live in Azure DevOps: PR
build status, PR creation, git worktrees, a commit-and-push helper with
pre-flight checks, and Application Insights log queries. Consolidates
near-duplicate scripts that used to be copy-pasted across OneSales, ccmt2,
and MDMApp into one versioned package with a `bdt` CLI.

## Install

```bash
uv tool install bmsdna-devtools
```

Or as a project dependency: `uv add bmsdna-devtools`.

## `bdt pr status`

Find the Azure DevOps PR opened from the current branch and report build
status for every pipeline that ran against it (failed steps print their
logs inline).

```bash
bdt pr status [--target-branch main] [--wait]
```

Org/project/repo are auto-detected from `git remote get-url origin`
(handles SSH, `dev.azure.com` HTTPS, and `*.visualstudio.com` HTTPS forms).
Auth is an explicit PAT (`--pat` or `AZURE_DEVOPS_PAT` env var), falling
back to a short-lived token from the caller's own `az login` — never embed
a PAT literal in a script or CI file.

## `bdt pr create`

```bash
bdt pr create --target main   # or --target test
```

Thin wrapper around `az repos pr create` using the current branch as
source; org/project/repo are inferred by `az` itself from the git remote.
Extra arguments pass through, e.g. `bdt pr create --target main -- --title "..."`.

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
recently" rather than a queryable trace stream — no defaults are baked in
here either.

```bash
bdt logs fetch --webapp my-webapp --resource-group my-rg --slot production
bdt logs fetch --webapp my-webapp --resource-group my-rg --slot production --out logs/ --keep-archive
```
