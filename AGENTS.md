# Coding Agent Guidelines

## Project Overview

`pypowsybl-mcp` is a project that integrates MCP (Model Context Protocol) servers for power grid operations,
specifically using `pypowsybl`.

## Development Rules

- Follow existing code style (indentation, naming conventions).
- Maintain documentation: `README.md` with new major features and update the `docs` directory if needed.
- Use `uv` for dependency management.
- Use `loguru` for logging.
- `ruff` is used for linting and formatting.
- Write code and comments in English.
- Use American English spelling (e.g. "color", "initialize", "catalog") in `README.md`, code comments, and the `docs/` directory.

## Git Workflow

- **Never push.** Do not run `git push` (nor `git push --force`, nor push tags) under any circumstance. Committing
  locally is fine; publishing is the maintainer's decision.
- **Never open a pull request.** Do not run `gh pr create` or otherwise open, edit, or merge a pull request. Leave the
  work on a local branch and report what is ready.
- **One single line per commit message.** A commit message is exactly one line — a short, imperative description of
  the change — followed by the human author's `Signed-off-by:` trailer. Nothing else: no body, no bullet list, no
  `Co-authored-by:` trailer.
- **Never mention the LLM.** No reference of any kind to the model, agent, or assistant that co-authored the code —
  not in the commit message, not in trailers, not in code comments, not in documentation.
- **Branch naming convention.** Create a branch for the change (never commit directly on `main`) and prefix its name
  with the type of work, followed by a short kebab-case description:
  - `feat/` — new feature (e.g. `feat/creation-tools`)
  - `fix/` — bug fix (e.g. `fix/per-unit-comparison`)
  - `refactor/` — restructuring with no behavior change
  - `docs/` — documentation only
  - `test/` — tests only
  - `chore/` — tooling, dependencies, CI
- Never commit real secrets in `.env`/`.env.*` files. When adding a new environment variable, document it (with a
  comment) in `.env.template` rather than only setting it locally.

## Testing

- Tests are located in the `tests/` directory.
- Use `pytest` for running tests.
- Always run relevant tests before submitting changes.

## Project Structure

- `pypowsybl_mcp/`: Main source directory.
- `tests/`: Project tests (pytest).
- `pyproject.toml`: Project metadata and dependencies.

