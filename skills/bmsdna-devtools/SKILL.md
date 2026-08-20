---
name: bmsdna-devtools
description: >
  Use the `bdt` CLI (from the bmsdna-devtools package) instead of ad hoc git/az/gh
  commands or repo-local scripts for: checking PR build/check status, creating
  a PR, creating a git worktree, committing and pushing files (with pre-flight
  checks), and querying Azure logs. `bdt pr *` works against both Azure DevOps
  and GitHub — it auto-detects which one from the `origin` remote. Trigger
  whenever the user asks to check a build/PR status, create a PR, make a
  worktree, commit changes, or fetch/tail application logs in a repo that has
  bmsdna-devtools installed (check for `bdt` on PATH, or `bmsdna-devtools` in
  pyproject.toml, before assuming it applies).
---

# bmsdna-devtools — shared BMS developer tooling

A single `bdt` CLI replacing the near-duplicate `get_pr_build.py`,
`commit.py`, and `just worktree`/`just pr-main` recipes that used to be
copy-pasted across OneSales, ccmt2, and MDMApp. Install with
`uv tool install bmsdna-devtools` (global) or `uv add bmsdna-devtools`
(per-project).

`bdt pr *` auto-detects Azure DevOps vs. GitHub from the `origin` remote and
shells out to `az`/`gh` accordingly — you don't need to pick a backend
yourself, and there's no need to fall back to raw `gh pr create`/`gh pr
checks` for a GitHub-hosted repo, `bdt pr *` covers that too. Each external
CLI (`az`, `gh`) is only required for the commands that actually need it,
and is checked lazily with a clear "not found, install it here" error
rather than a raw traceback — if you see that error, tell the user which
CLI to install rather than trying to work around it.

## Checking PR build/check status

```bash
bdt pr status [--target-branch main] [--wait]
```

Finds the PR opened from the current branch and prints build/check status,
inlining failed-step logs. `--wait` polls until everything finishes (use
this after pushing, instead of guessing when CI is done). Exits non-zero if
anything failed — safe to gate a script on. If the PR has merge conflicts
(or, on Azure DevOps, failed/was rejected by policy), that's reported
immediately instead of waiting for builds that will never run — check the
error message for `mergeStatus=conflicts` (ADO) or `mergeable=CONFLICTING`
(GitHub) and tell the user to resolve conflicts rather than assuming CI is
just slow.

`--target-branch` only applies on Azure DevOps (its search API needs one to
find the right PR); on GitHub, `gh pr view` always resolves the PR for the
current branch regardless, so the flag is ignored there — the PR's actual
base branch is shown in the output instead.

Auth: Azure DevOps works with no setup if the caller is already `az
login`'d (only pass `--pat`/`AZURE_DEVOPS_EXT_PAT`/`AZURE_DEVOPS_PAT` if there's no `az` session
available); GitHub uses whatever `gh auth login` session is active.

## Creating a PR

```bash
bdt pr create --target main    # or --target test
```

Uses the current branch as source. On Azure DevOps this wraps `az repos pr
create`; on GitHub, `gh pr create --fill` (autofills title/body from commit
info, so it never blocks waiting on an interactive prompt). Extra args pass
straight through either way, e.g.
`bdt pr create --target main -- --title "..." --description "..."`.

After creating a PR, use `bdt pr status` (see above) to check whether the
CI build passes.

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
