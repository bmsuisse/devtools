## Summary

Implements the "static checks" half of [bmsuisse/skills#52](https://github.com/bmsuisse/skills/issues/52): a new `bdt lint` command that runs local, fast static checks before the (future) skill-driven code-review pass.

- **SQL rules** — AST scan of every `.execute()`-style call: requires `load_sql()`/a `.sql` file, a psycopg t-string, or `psycopg.sql` for anything beyond trivial inline SQL (per the `postgres-best-practices` skill); flags f-string/`.format()`/concatenation SQL as an injection risk; validates literal SQL text with `sqlglot` (postgres dialect) before applying any rule, so non-SQL `.execute()` calls (duckdb, subprocess, etc.) aren't false-positived; flags positional `%s` params and forbidden join patterns (`RIGHT JOIN`/`LATERAL JOIN`/`CROSS APPLY`, matching the `prek` skill's `check_files.py`).
- **Tooling config check** — verifies the consuming repo declares `ty`, `ruff`, `pytest` and has `pytest` + `prek.toml` configured; bypassable (never a hard block).

Calibrated against real-world `.execute()` patterns surveyed in OneSales and CCMT2 (copied into test fixtures, not scanned live).

## Test plan
- [ ] `bdt lint` unit tests (SQL rule engine + tooling check)
- [ ] Manual run against OneSales/CCMT2 to sanity-check signal/noise
- [ ] CI (ruff, ty, pytest)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
