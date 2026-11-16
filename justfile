install:
    uv sync --all-extras --all-groups

lint:
    uv run ruff check bmsdna tests
    uv run ruff format --check bmsdna tests

fmt:
    uv run ruff check --fix bmsdna tests
    uv run ruff format bmsdna tests

check:
    uv run ty check bmsdna

# Run every pre-commit hook (prek.toml) on all files; install the git hook once with `prek install`.
hooks:
    prek run --all-files

test:
    uv run -m pytest -q

test-all: lint check test

build:
    uv build

# New worktree under .worktrees/<name> branched from base (default main); installs deps via `just install`.
worktree name base="main":
    uv run bdt worktree {{name}} --base {{base}} --install "just install"

# Create a GitHub PR from the current branch (this repo lives on GitHub, not Azure DevOps).
pr *args:
    gh pr create --fill {{args}}
