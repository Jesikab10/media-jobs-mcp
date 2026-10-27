# Contributing

1. Branch from `main` (one branch per change; `main` is protected).
2. `uv sync`, then make the change with tests.
3. Before opening a PR, all of these must pass locally (CI runs the same):

   ```bash
   uv run ruff format --check . && uv run ruff check . && uv run mypy
   uv run pytest
   uv run python -m media_jobs_mcp.evals
   ```

4. Open a PR using the template. "Verification" lists the commands you ran and what you saw.

Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

A change that adds an MCP tool includes: the scope it requires, annotations
(`read_only_hint` / `destructive_hint`), a contract test, and an eval case if the agent uses it.
