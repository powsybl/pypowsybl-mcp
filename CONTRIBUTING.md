# Contributing to pypowsybl-mcp

Thank you for your interest in contributing. This document explains how to set up your environment, run tests, and
submit changes.

## Project status

This project is in **prototype phase**. Core interfaces may change without prior notice. If you plan a large
contribution, open an issue first to discuss the design before writing code.

## Reporting issues

Use the GitHub issue tracker. When reporting a bug, please include:

- Your OS and Python version.
- The version of `pypowsybl` you are using.
- The MCP client you are connecting with.
- Minimal steps to reproduce the issue.
- The full error message or traceback.

## Setting up a development environment

```bash
git clone https://github.com/powsybl/pypowsybl-mcp.git
cd pypowsybl-mcp

uv venv --python 3.13
source .venv/bin/activate

# Install with dev dependencies (open-source pypowsybl backend included by default)
uv pip install -e ".[dev]"
```

> **RTE users**: after the step above, run `./install_rte.sh --dev` to swap to the RTE-internal backend.

Copy the environment template and fill in any values you need:

```bash
cp .env.template .env
```

## Running the tests

```bash
uv run pytest
```

To run a single test file:

```bash
uv run pytest tests/tools/test_loadflow_tools.py -v
```

## Code style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Run both checks before submitting:

```bash
uv run ruff check .
uv run ruff format .
```

CI will reject PRs that fail either check.

## Submitting a pull request

1. Fork the repository and create a feature branch from `main`.
2. Make your changes. Add or update tests for any new behaviour.
3. Run the full test suite and linter locally (see above).
4. Open a pull request against `main`. Describe what the change does and why.
5. Reference any related issues with `Fixes #<number>` in the PR description.

### Commit style

Use short, imperative commit messages (`add X`, `fix Y`, `remove Z`). One logical change per commit.

## Adding a new MCP tool

Follow the pattern used in the existing tool files under `pypowsybl_mcp/tools/`:

1. Add the tool as an `async def` method on the relevant tool class.
2. Register it by passing the class instance to `wrap_class_methods_with_mcp_tool` in `server.py`, or register it
   directly with `@mcp.tool()`.
3. Add a row to `docs/tools_reference.md`.
4. Write at least one test in `tests/tools/`.

See `docs/mcp_sessions.md` for the session and proxy pattern all tools must follow.

## License

By contributing, you agree that your contributions will be licensed under the [Mozilla Public License 2.0](LICENSE.md).
