install:
    uv sync --all-groups

lint:
    uv run ruff check bmsdna tests

fmt:
    uv run ruff format bmsdna tests

check:
    uv run ty check bmsdna

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
