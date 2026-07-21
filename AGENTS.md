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

## Testing

- Tests are located in the `tests/` directory.
- Use `pytest` for running tests.
- Always run relevant tests before submitting changes.

## Project Structure

- `pypowsybl_mcp/`: Main source directory.
- `tests/`: Project tests (pytest).
- `pyproject.toml`: Project metadata and dependencies.

