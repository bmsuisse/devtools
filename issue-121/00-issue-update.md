PR [#122](https://github.com/bmsuisse/devtools/pull/122) implements `bdt ci plan` (bdt 0.50.0, published, CI running).

What it does: reads the Azure Pipelines context, decides which tests run (PR labels `test-<area>` / `test-shared` / `test-full` / `test-none`, legacy `e2e-*` aliased; without a label it infers from the changed files; any unreadable input runs everything), enforces the dev -> test -> main branch flow incl. the fix-branch check, and publishes `runTests` plus per tier `<tier>Run` / `<tier>Areas` / `<tier>Paths` as pipeline variables. `bdt pr create` generates its required `test_area` label group from the same folders, so the label list no longer has to be kept in sync by hand. Config is `[tool.bdt.ci.plan]`; a README section has the full decision order and a pipeline snippet.

Answers to the open questions: `test-none` is a first-class label that `bdt pr create` always accepts; the area table is inline in `pyproject.toml` (`tiers.<t>.areas`) for flat folders, otherwise areas are the sub-folders.

Deliberately not in this PR: GitHub Actions output, named plans for monorepos (only if a repo needs them), migrating OneSales / OneDocs / MDMApp / CCMT2 / OneData and updating the branching-strategy skill (follow-ups once 0.50.0 is released).
