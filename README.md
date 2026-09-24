# bmsdna-devtools

Shared developer tooling for BMS projects: PR build/check status, PR
creation, issue/work item creation and comments, git worktrees (creation and
merged-worktree/orphaned-test-DB cleanup), a commit-and-push helper with
pre-flight checks, and Azure log queries.
`bdt pr *` and `bdt issue *` auto-detect whether the current repo's `origin`
remote is Azure DevOps or GitHub and use `az`/`gh` accordingly.
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

With `--wait`, a build/check that's paused on a manual approval (an Azure
Pipelines stage's Checkpoint.Approval, or a GitHub Actions deployment
protection rule) ends the wait instead of polling forever — it prints which
stage/check needs a reviewer plus a link to act on it, and exits **3**
(`bdt pr watch-deploy --wait`, below, behaves the same way). If another
build/check has already failed, that's reported instead (exit 1) even when
one is also waiting on approval.

**Azure DevOps**: org/project/repo are auto-detected from
`git remote get-url origin` (handles SSH, `dev.azure.com` HTTPS, and
`*.visualstudio.com` HTTPS forms). Auth is an explicit PAT (`--pat` or
`AZURE_DEVOPS_EXT_PAT`/`AZURE_DEVOPS_PAT` env var), falling back to a short-lived token from the
caller's own `az login` — never embed a PAT literal in a script or CI file.
`--target-branch` selects which PR to look at (ADO's search API needs one).

**GitHub**: uses `gh`'s own auth (`gh auth login`) and always resolves the
PR opened from the current branch — `gh pr view` has no target-branch
filter, so `--target-branch` is ignored here; the PR's actual base branch
is shown in the output. Check status is computed from
`gh pr view --json statusCheckRollup` rather than `gh pr checks --json`,
since the latter flag isn't available in all `gh` releases.

## `bdt pr retry`

Retry only the **failed** job(s)/stage(s) of the most recent build/run for the
PR opened from the current branch — not a whole new build/run. Whenever `bdt
pr status` reports a failure, it prints a hint to run this.

```bash
bdt pr retry [--target-branch main]
```

No build/run ID needed — like `bdt pr status`, it resolves the PR (and its
latest build/run per pipeline/workflow) from the current branch.

**Azure DevOps**: uses the `retry=true` query parameter on the "Update
Build" REST API
(`PATCH .../_apis/build/builds/{buildId}?retry=true&api-version=7.1`), which
reschedules whichever stage(s)/job(s) failed on the previous attempt (plus
anything depending on them) in place — distinct from queuing a brand new
build. One retry call per pipeline that has a failed build for the PR.
`--target-branch` selects which PR to look at, same as `bdt pr status`.

**GitHub**: uses `gh run rerun <run-id> --failed`, which reruns only the
failed job(s) (and their dependents) of a workflow run — the run ID(s) are
found automatically from the PR's failing checks. Checks that aren't backed
by a GitHub Actions run (e.g. a legacy commit status from an external CI)
can't be retried this way and are skipped; if none of the failing checks are
retryable, the command exits with an error.

## `bdt pr watch-deploy`

Many pipelines have a second build/stage that only runs on the *target*
branch once a PR merges into it, and that's the one that actually deploys.
`bdt pr status` only watches builds/checks tied to the PR itself (its
merge/source ref or GitHub's `statusCheckRollup`), so it never sees that
second build. `bdt pr watch-deploy` does: it finds the most recent build/
workflow run triggered *directly* on `--target-branch` and reports its
status the same way `pr status` does (failed steps print their logs inline).

```bash
bdt pr watch-deploy [--target-branch main] [--wait]
```

After `bdt pr status` reports the PR's build/checks succeeded, if a build/
run has already been triggered on the target branch (e.g. the merge already
kicked one off), it prints a hint suggesting this command. That check is
best-effort — a failure reading it (auth, permissions, network) is silently
skipped rather than blocking or crashing `pr status`.

**Azure DevOps**: looks up builds via the same `_apis/build/builds` endpoint
`pr status` uses, just queried by `branchName=refs/heads/<target-branch>`
instead of the PR's merge/source ref.

**GitHub**: uses `gh run list --branch <target-branch> --event push`, which
selects workflow runs triggered by a push to that branch — as opposed to a
`pull_request`-triggered run for some still-open PR targeting the same
branch. Failed steps are printed via `gh run view <id> --log-failed`.

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

PRs are created as **drafts by default**; a successful create always prints
the PR's link plus `bdt pr publish` (which abstracts over the host) to mark
it ready for review. Pass `--no-draft` to open it ready for review
immediately instead.

`--label` (repeatable) applies labels to the PR on either host: on GitHub
these map to `gh pr create --label`, so the label must already exist on the
repo (`gh label create`); on Azure DevOps they map to `az repos pr create
--labels`, which are freeform and get created on the fly. A successful
create prints the PR's web/GUI link (not just the REST API URL Azure DevOps'
`az` output otherwise gives you).

`[tool.bdt.pr.required_labels]` in pyproject.toml can require at least one
label from each named group before the PR is created — checked locally
(no `gh`/`az` call happens if a group isn't satisfied):

```toml
[tool.bdt.pr.required_labels]
type = ["bug", "feature", "chore"]
risk = ["breaking", "non-breaking"]
```

With the above, `bdt pr create --label feature --label breaking` passes,
but `bdt pr create --label feature` fails with a message naming the unmet
group (`risk`) and its allowed choices.

`[tool.bdt.pr.scope_labels]` in pyproject.toml can auto-apply a label based on
the "scope" of the HEAD commit's conventional-commit subject (the
`type(scope): description` format `bdt commit` itself expects, e.g.
`feat(customers): ...`):

```toml
[tool.bdt.pr.scope_labels]
customers = "e2e-customers"
billing = "e2e-billing"
```

With the above, creating a PR whose HEAD commit is `feat(customers): add
widget` automatically adds the `e2e-customers` label (merged with, not
replacing, any explicit `--label`). No match, no `[tool.bdt.pr.scope_labels]`
table, or a commit subject without a `(scope)` all mean no label gets added —
opt-in, so repos that don't configure it see no change in behavior.

If `--target` has a build policy configured (an Azure DevOps Build policy,
or a GitHub branch protection rule requiring status checks), a successful
create prints a reminder to run `bdt pr status` afterward to check whether
the CI build passes. This is a best-effort check — failures reading policy
config (auth, permissions) fail open and simply skip the reminder.

## `bdt issue create` / `update` / `delete`, `bdt issue comment add` / `update` / `delete`

```bash
bdt issue create --title "Nightly job fails" --description "..." --type Bug --screenshot before.png
bdt issue update 1234 --state Resolved --tag fixed
bdt issue delete 1234 --yes

bdt issue comment add 1234 --message "Repro'd, see attached" --screenshot repro.png
bdt issue comment update 1234 5678 --message "Actually, see the second screenshot"
bdt issue comment delete 1234 5678 --yes
```

Creates/updates/deletes an issue (GitHub) or work item (Azure DevOps), and
adds/edits/deletes comments on one, auto-detected from `origin` like `bdt pr
*`. `bdt issue comment add` prints the new comment's ID so you can pass it to
`update`/`delete` later.

Destructive commands (`issue delete`, `issue comment delete`) require an
explicit `--yes` — there's no interactive confirmation prompt, since `bdt` is
also invoked by AI-agent callers that can't answer one.

**Azure DevOps**: `--type` selects the work item type on `create` (`Bug`,
`Task`, `User Story`, ... — whatever the project's process defines; default
`Bug`). `--tag` sets/replaces the full tag list (repeatable; omit on
`update` to leave tags unchanged). `--state` (`update` only) sets
`System.State`, e.g. `Active`, `Resolved`, `Closed`. `--screenshot` uploads
each image as a work item attachment (visible in the Attachments tab) and
posts a comment embedding them inline with Markdown — the Description field
defaults to HTML via the REST API, where a raw `![]()` would just show as
literal text, but the work item Discussion/Comments control has always
rendered Markdown. `issue delete` soft-deletes to the project's Recycle Bin
(restorable, not permanent).

`--board <team>` sets the work item's Area Path to that Azure Boards team's
default, so it shows up on that team's board — a CLI flag beats
`[tool.bdt.ado].board` in `pyproject.toml`, which beats filing under the
project's root area:

```toml
[tool.bdt.ado]
board = "My Team"
```

On `update`, `--board` only moves the item when you pass it explicitly — it
never falls back to `pyproject.toml`, so an unrelated field update (e.g.
just `--title`) can't silently relocate the item to a different board.

**GitHub**: a thin wrapper around `gh issue create` / `edit` / `delete` /
`comment`. `--label` adds a label on `create`, or adds/removes one on
`update` (paired with `--remove-label`); labels must already exist in the
repo. `--screenshot` pushes images to a `pr-assets` branch (same trick `bdt
pr create --screenshot` uses, since GitHub has no API for uploading an image
into an issue) and appends them to the issue body / comment as Markdown.
`issue delete` is **permanent** — GitHub has no recycle bin for issues.
Comment update/delete go through `gh api` directly (`gh issue` has no
subcommand for editing/deleting an arbitrary comment by ID). Extra arguments
to `bdt issue create` pass through to `gh issue create`, e.g.
`bdt issue create --title "..." -- --assignee @me`.

`--board <name>` on `create` adds the issue to that GitHub Projects (v2)
board by title (needs a token with the `project` scope — `gh auth refresh -s
project`). On `search`, `--board` (title or number) scopes results to issues
currently on that board; since `gh issue list` has no board filter of its
own, this fetches a wider raw pool and filters it down client-side, so a
board with few matches may return fewer results than `--limit` asks for. A
CLI flag beats `[tool.bdt.github].board` in `pyproject.toml`:

```toml
[tool.bdt.github]
board = "Roadmap"
```

## `bdt worktree`

```bash
bdt worktree my-feature [--base dev] [--env-file .local_env] [--no-submodules] [--install "just install"]
```

Creates `.worktrees/<name>` branched from `--base`, initializes submodules
(unless `--no-submodules`), and copies an env file into the new worktree as
`.env` (auto-detects `.local_env` then `.env` if `--env-file` isn't given).

## `bdt cleanup worktrees` / `bdt cleanup orphaned-dbs` / `bdt cleanup db` / `bdt cleanup worktree`

```bash
bdt cleanup worktrees [root] [--remote origin] [--keep-dbs] [--yes]
bdt cleanup orphaned-dbs [root] [--include-caution] [--yes]
bdt cleanup db [path] [--confirm]
bdt cleanup worktree [path] [--keep-db] [--confirm]
```

The first two recursively scan every git repo under `root` (default: `.`)
for worktrees. `bdt cleanup worktrees` prunes the ones fully merged into
`<remote>/main`/`<remote>/test` (falling back to local `main`/`test` if no
such remote refs exist) — e.g. a tree of `.worktrees/<branch>` directories
accumulated across several repos over time. `bdt cleanup orphaned-dbs` has no
`--remote`/merge-status notion at all: it just finds pgdevkit test DBs with
no matching *live* git worktree, regardless of whether that worktree was
ever merged anywhere. Like `bdt issue delete`, neither command has an
interactive prompt — both only ever *print* what they would remove/drop;
pass `--yes` to actually do it.

`bdt cleanup db` / `bdt cleanup worktree` instead target one specific,
still-live worktree — `path` defaults to `.`, so both are meant to be run
from inside the worktree in question, regardless of its merge status.
`cleanup db` only drops that worktree's own pgdevkit test DB(s), leaving the
worktree itself alone; `cleanup worktree` removes the worktree too (and,
unless `--keep-db`, its DB(s) along with it) — refusing the main checkout, a
protected branch (`main`/`test`), a locked worktree, or a dirty one
(submodules included). Since these two act on a single worktree a human
picked out by hand, rather than scanning for candidates, they default to an
interactive `y/N` confirmation instead of `--yes`; pass `--confirm` to skip
it for non-interactive use.

Every one of these four commands, before dropping any database, additionally
requires typing `yes` at an interactive prompt whenever `--pg-host` isn't
`localhost`/`127.0.0.1`/`::1` — this specific check has no flag to bypass it
(not even `--yes`/`--confirm`), so a script or agent can never drop a
database on a shared/remote Postgres instance without a human confirming it
directly.

The DB-naming algorithm and orphan detection are entirely
[pgdevkit](https://github.com/bmsuisse/pgdevkit)'s own
(`pgdevkit.testdb.workspace_db_names()` /
`pgdevkit.testdb.find_orphaned_dbs()`) — this used to be a hand-rolled
reimplementation here (to avoid an import), which risked drifting out of
sync with pgdevkit's actual naming; now that pgdevkit exposes both directly,
bdt just calls them. If a repo's root `pyproject.toml` has a
`[tool.pgdevkit].engine = "postgres"` (the default once `[tool.pgdevkit]`
exists at all), removing one of its worktrees also drops the Postgres test
database(s) pgdevkit created for that branch — pass `--keep-dbs` to skip
that. A DB whose name ends in a bare branch name
(`main`/`test`/`dev`/`head`/`i18n`, rather than a slugified feature branch)
is flagged `⚠ possibly a standing reference DB` and excluded even with
`--yes`, since that might be an intentional baseline DB rather than an
orphaned leftover — pass `--include-caution` too if you've verified it
really is safe to drop. That flagging (bdt's own heuristic, not pgdevkit's)
is the one bit of naming-adjacent logic still here.

A repo can additionally own **sibling** test DBs (e.g. a second DB for a
vendored mock service) and **nested** ones (an unrelated per-branch DB, under
a different pgdevkit project name, that happens to share the same branch).
Siblings are pgdevkit's own concern now — configure
`[tool.pgdevkit].extra_db_suffixes` in the *consuming* repo (see pgdevkit's
README) and both `ensure_testdb`-side tooling and `bdt cleanup` pick it up
automatically. Nested projects have no pgdevkit equivalent (it's a wholly
separate project name/pyproject.toml, not a literal suffix of the same
project's DB), so that stays configured here:

```toml
[tool.bdt.worktree]
db_nested_projects = ["akeneo_editor"]  # a wholly separate per-branch DB,
                                         # its own (possibly section-less)
                                         # pyproject.toml, sharing this
                                         # worktree's branch
```

`--pg-port`/`--pg-user` (all four commands; env vars `PGPORT`/`PGUSER`, no
fallback to `$USER`/`$LOGNAME` — those are set in virtually every shell,
which would make the pgdevkit-user default below never fire) default to
*pgdevkit's own* test-container port/user, not the OS user or Postgres'
standard `5432` — `bdt cleanup orphaned-dbs`'s listing step always connects
via pgdevkit's own
`PGDEVKIT_TESTDB_*`-driven resolution (it's calling straight into pgdevkit),
so its own `psql`-based DROP step defaults to matching that, rather than
silently targeting a different Postgres instance than the one that was just
queried. Pass `--pg-port`/`--pg-user` explicitly if your setup deliberately
differs.

## `bdt commit`

```bash
bdt commit "feat(x): add widget support" file1.py file2.py [--json] [--no-verify] [--subrepo database]
```

Stages, commits, and pushes the given files. Pre-flight checks: files
exist, commit message follows [Conventional Commits](https://www.conventionalcommits.org)
(`type(scope): description`, skip with `--skip-message-check`), not on
`main`/`master` (skip with `--allow-main`). Retries once (re-`git add`) if a
pre-commit hook reformats files. Pass `--subrepo <dir>` (repeatable) for
repos that vendor a submodule (e.g. `database`) — files under that prefix
are committed/pushed inside the submodule first, then the bump is staged in
the parent repo.

The built-in commit types are `feat`, `fix`, `docs`, `style`, `refactor`,
`perf`, `test`, `build`, `ci`, `chore`, `revert`. A repo can accept
additional types on top of those under `[tool.bdt.commit]` in
`pyproject.toml`:

```toml
[tool.bdt.commit]
types = ["sql", "infra"]
```

Scope (the `(x)` in `feat(x): ...`) is unrestricted by default — any scope,
or none at all, is accepted. A repo can opt into restricting it to a fixed
list under the same table:

```toml
[tool.bdt.commit]
scopes = ["api", "ui", "db"]
```

Once configured, a message that *names* a scope must use one from the
list — but a message with no scope at all is still always accepted; this
doesn't make a scope mandatory.

If the pushed commit's type is `feat` and the current branch's PR is
already published (not a draft), it's converted back to draft — a feature
needs a fresh review pass before CI/merge, not just whatever review
happened before the commit existed. Run `bdt pr publish` when it's ready
again. Pass `--target`/`--pat` to resolve the PR on Azure DevOps (GitHub
always resolves the current branch's PR directly).

Set `IS_BMS_AI_SANDBOX=1` to skip the push step (commit only) — used when
an AI coding sandbox pushes on its own schedule separately. The draft
conversion above only runs after an actual push, so it's skipped in
sandbox mode too.

`--json` emits a machine-readable result for AI-agent callers:

```json
{
  "success": true, "committed": true, "pushed": true,
  "message": "...", "files": ["..."], "commit_sha": "abc1234",
  "error": null, "hint": null, "commit_type": "feat", "commit_scope": "x"
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
the calling repo's `pyproject.toml`. `slot` is optional — omit it for an
app's default/production slot (no `--slot` is passed to `az`); set it for a
named deployment slot:

```toml
[tool.bdt.envs.prod]
webapp = "my-webapp"
resource_group = "my-rg"

[tool.bdt.envs.test]
webapp = "my-webapp"
resource_group = "my-rg"
slot = "test"
```

```bash
bdt logs fetch --env prod
bdt logs fetch --env prod --out logs/ --keep-archive
```

## Releasing

Bump `version` in `pyproject.toml` as part of your PR, same as any other
change. Once that PR merges to `main` and the `Python Test` workflow passes
for that commit, `.github/workflows/auto-release.yml` automatically tags it
`vX.Y.Z`, cuts a GitHub Release (skipping if that version was already
released, e.g. a merge that didn't touch the version), and dispatches
`python-publish.yml` to publish it to PyPI — no manual release step, and no
extra secret to configure. Two non-obvious GitHub Actions quirks shaped
this (see the comments at the top of `auto-release.yml` for the full
reasoning, since both were hit and confirmed the hard way):

- A release created with the default `GITHUB_TOKEN` does **not** trigger
  other workflows' `release: published` listeners (an anti-recursion
  safeguard) — `workflow_dispatch` is the documented exception, so
  `auto-release.yml` dispatches `python-publish.yml` directly (`gh workflow
  run`) instead of relying on the release to cascade into it.
- `python-publish.yml` deliberately stays a plain, directly-triggered
  top-level workflow rather than something `auto-release.yml` calls via
  `workflow_call`: PyPI's OIDC trusted publishing does not support
  reusable/called workflows and silently rejects the token in that shape.

`workflow_dispatch` (or an actual GitHub UI release) on `python-publish.yml`
still works as a manual fallback if you ever need to re-publish a version
without going through `auto-release.yml`.
