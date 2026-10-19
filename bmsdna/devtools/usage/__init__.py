"""`bdt usage`: where do the tokens of Claude Code / codex sessions go, per step of the dev-workflow?

Reads the local session logs with DuckDB, attributes every model turn to a workflow step and prices it.
The heavy dependencies (duckdb, plotly) are the optional `usage` extra and only imported when a command runs.
"""
