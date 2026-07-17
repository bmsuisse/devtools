---
name: bmsdna-devtools
description: >
  Use the `bdt` CLI (from the bmsdna-devtools package) instead of ad hoc git/az/gh
  commands or repo-local scripts for: checking Azure DevOps PR build status,
  creating an Azure DevOps PR, creating a git worktree, committing and pushing
  files (with pre-flight checks), and querying Application Insights logs.
  Trigger whenever the user asks to check a build/PR status, create a PR,
  make a worktree, commit changes, or fetch/tail application logs in a repo
  that has bmsdna-devtools installed (check for `bdt` on PATH, or `bmsdna-devtools`
  in pyproject.toml, before assuming it applies).
---

# bmsdna-devtools — shared BMS developer tooling

A single `bdt` CLI replacing the near-duplicate `get_pr_build.py`,
`commit.py`, and `just worktree`/`just pr-main` recipes that used to be
copy-pasted across OneSales, ccmt2, and MDMApp. Install with
`uv tool install bmsdna-devtools` (global) or `uv add bmsdna-devtools`
(per-project).

Before using any command below, confirm it actually applies: `bdt pr *` and
the Azure DevOps auth flow only work in a repo whose `origin` remote points
at Azure DevOps (`dev.azure.com` / `ssh.dev.azure.com` / `*.visualstudio.com`).
For a repo hosted on GitHub, use `gh pr create` / `gh pr checks` instead.

## Checking PR build status

```bash
bdt pr status [--target-branch main] [--wait]
```

Finds the PR opened from the current branch into `--target-branch`, prints
each pipeline's latest build, and inlines failed-step logs. `--wait` polls
every 30s until every pipeline finishes (use this after pushing, instead of
guessing when CI is done). Exits non-zero if any pipeline failed — safe to
gate a script on.

Auth: works with no setup if the caller is already `az login`'d. Only pass
`--pat`/`AZURE_DEVOPS_PAT` if there's no `az` session available.

## Creating an Azure DevOps PR

```bash
bdt pr create --target main    # or --target test
```

Uses the current branch as source. Extra args pass straight through to
`az repos pr create`, e.g. `bdt pr create --target main -- --title "..." --description "..."`.

## Creating a worktree

```bash
bdt worktree <name> [--base dev] [--install "just install"]
```

Creates `.worktrees/<name>`, inits submodules if `.gitmodules` exists,
copies an env file in (auto-detects `.local_env` then `.env`), and runs
`--install` inside the new worktree if given. Prefer this over raw
`git worktree add` so env-file copying and submodule init aren't forgotten.

## Committing and pushing

```bash
bdt commit "feat(x): add widget support" file1.py file2.py
```

Use this instead of raw `git commit && git push` in any repo where it's
the established convention (check the repo's AGENTS.md/CLAUDE.md — several
BMS repos mandate it). It runs pre-flight checks (files exist, message
looks like `type: description`, not on `main`/`master`) and retries once
if a pre-commit hook reformats files. Pass `--json` when calling
programmatically from an agent loop — the schema is documented in
`bdt commit --help`. Pass `--subrepo database` (repeatable) for repos that
vendor a git submodule under that path.

## Application Insights logs

```bash
bdt logs roles --resource-group <rg> --app-insights <name> --minutes 60
bdt logs tail --resource-group <rg> --app-insights <name> --role <role> --level warning
```

`logs roles` lists which `cloud_RoleName`s have recent traffic — run this
first if you don't already know the role name to filter `logs tail` by.
No defaults are baked in (unlike the other commands, this can't infer
anything from the git remote) — always pass `--resource-group`/`--app-insights`
or set `AZURE_RESOURCE_GROUP`/`AZURE_APP_INSIGHTS`.

For "what broke recently" without a KQL query, `bdt logs fetch --webapp
<name> --resource-group <rg> --slot <slot>` downloads the App Service log
archive and extracts error/warning lines to `logs/<slot>_errors.log` —
often the faster path when you don't need a filterable trace stream.
