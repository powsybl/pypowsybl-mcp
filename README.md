# PyPowsybl MCP Server

> **Prototype status.** This project is under active development and is not yet production-ready. APIs, tool
> signatures, and configuration options may change between releases without notice. Feedback and contributions are
> welcome, but use in critical production systems is discouraged until a stable release is declared.

`pypowsybl-mcp` exposes `pypowsybl` power-system capabilities through an MCP server, so LLM clients such as Codex,
Claude Desktop, Cursor, or custom agents can inspect networks, run studies, generate diagrams, and export results
through standard MCP tool calls.

The server is stateful: each MCP session keeps its own loaded networks, active variant, load-flow parameters, and
analysis context in memory. This makes multi-step investigations practical without reloading the same grid at every
turn.

## Features

- `30+` MCP tools grouped by domain: I/O, network management, visualization, load flow, security analysis,
  sensitivity analysis, session management, and code export.
- Load networks from local files or remote URLs, or create standard IEEE test networks directly from the MCP client.
- Inspect and modify network elements, open/close switches, manage variants, and compare alternative operating states.
- Run AC/DC load flows, adjust session-specific load-flow parameters, and switch load-flow providers when available.
- Run contingency and sensitivity studies, including N-1 security analysis, PTDF, DCDF, PSDF, and custom sensitivity
  workflows.
- Generate single-line diagrams and network-area diagrams, with downloadable artifacts served by the MCP server.
- Export the current network and generate standalone Python scripts that reproduce the session workflow.
- Keep per-session state with TTL-based caches so a client can work iteratively on the same study.
- Report what the server is holding through a read-only admin HTTP API (`/admin/health`, `/admin/sessions`):
  active sessions, their contents, their activity, and process memory.

## Documentation

- [Getting Started](docs/getting_started.md): install, configure, launch, and run a first analysis.
- [Architecture Overview](docs/architecture.md): server structure, tool groups, and request lifecycle.
- [Tools Reference](docs/tools_reference.md): catalogue of all MCP tools.
- [Configuration Reference](docs/configuration.md): environment variables and TOML defaults.
- [MCP Client Integration](docs/mcp_client_integration.md): connect Codex, Claude Desktop, Cursor, or a custom agent.
- [Sessions & State](docs/mcp_sessions.md): session lifecycle, cache TTL, and session pinning.
- [Monitoring](docs/monitoring.md): the admin HTTP API for active sessions and server health.
- [Plugins](docs/plugins.md): extend the server with your own tools, resources, and prompts.

## Prerequisites

- Python `3.11+`
- [`uv`](https://github.com/astral-sh/uv)
- A `pypowsybl` backend 
- Docker, if you want to run the server in a container
- An OpenAI-compatible API key only if you plan to use `generate_python_script`

## Installation

Create and activate a virtual environment:

```bash
uv venv --python 3.13
source .venv/bin/activate
```

Install the server (the open-source `pypowsybl` backend is included by default):

```bash
uv pip install .
```

## Configuration

Copy the template and adjust the values for your environment:

```bash
cp .env.template .env
```

Minimal local configuration:

```dotenv
MCP_PORT=9992
MCP_PUBLIC_ADDRESS=localhost
LOG_DIR=logs

# Required only for generate_python_script
OPENAI_API_KEY=sk-...
OPENAI_DEFAULT_MODEL=gpt-5.4
```

Useful variables:

- `MCP_PORT`: HTTP port used by the MCP server.
- `MCP_PUBLIC_ADDRESS`: host or IP embedded in generated download URLs. Use a value reachable from the MCP client.
- `MCP_AUTH_TOKEN`: protects admin-only session tools. Do not expose it to normal LLM clients unless you explicitly
  want them to access those tools.
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_DEFAULT_MODEL`: only needed for the `generate_python_script` tool.

See [Configuration Reference](docs/configuration.md) for the full list.

## Running the Server

### Local

From the repository root:

```bash
uv run python -m pypowsybl_mcp.server
```

If you already installed the package in an active environment, this also works:

```bash
python -m pypowsybl_mcp.server
```

The MCP endpoint is exposed over streamable HTTP at:

```text
http://localhost:9992/mcp
```

Generated files are served through temporary download URLs under:

```text
http://localhost:9992/download/<token>/<filename>
```

### Docker

```bash
docker compose up --build
```

Useful notes:

- Put network files in `./data`; they are mounted in the container as `/app/data`.
- Logs are written to `./logs`.
- Set `HOST_UID` and `HOST_GID` in `.env` if you need mounted files to keep the right ownership.

## Configure Codex

The PyPowsybl server must already be running before Codex can connect to it. The server listens on `MCP_PORT` when set, or `9992` by default.

Add the MCP server to Codex in either:

- `~/.codex/config.toml` for a user-level setup
- `.codex/config.toml` for a project-scoped setup

Example configuration:

```toml
[mcp_servers.pypowsybl]
url = "http://localhost:9992/mcp"
```

For a remote deployment, replace `localhost` with the real host or domain, use the port configured by `MCP_PORT`, and
make sure `MCP_PUBLIC_ADDRESS` is set to that same reachable address, otherwise download links returned by export and
visualization tools will not work from Codex.

After saving the configuration:

1. Restart Codex, or reload MCP settings if you are using a UI flow.
2. Open the project in Codex.
3. Ask Codex to use the `pypowsybl` tools, for example:

```text
Create an IEEE 14-bus network, run an AC load flow, and summarize the voltage profile.
```

Important:

- This server uses the `streamable-http` MCP transport, not `stdio`.
- Do not give Codex the `MCP_AUTH_TOKEN` in its MCP configuration unless you intentionally want Codex to access the
  admin-only session tools.

## Configure Claude

The PyPowsybl server must already be running before Claude can connect to it. The server listens on `MCP_PORT` when set, or `9992` by default.

Add the MCP server to Claude Desktop's configuration file, usually located at:

- `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS
- `%APPDATA%\Claude\claude_desktop_config.json` on Windows

Example configuration:

```json
{
  "mcpServers": {
    "pypowsybl": {
      "type": "streamable-http",
      "url": "http://localhost:9992/mcp"
    }
  }
}
```

For a remote deployment, replace `localhost` with the real host or domain, use the port configured by `MCP_PORT`, and
make sure `MCP_PUBLIC_ADDRESS` is set to that same reachable address, otherwise download links returned by export and
visualization tools will not work from Claude.

After saving the configuration:

1. Restart Claude Desktop so it reloads the MCP settings.
2. Confirm the `pypowsybl` tools appear in Claude's tool list.
3. Ask Claude to use the `pypowsybl` tools, for example:

```text
Create an IEEE 14-bus network, run an AC load flow, and summarize the voltage profile.
```

Important:

- This server uses the `streamable-http` MCP transport, not `stdio`. Claude Desktop versions that only support `stdio`
  cannot connect directly.
- Do not give Claude the `MCP_AUTH_TOKEN` in its MCP configuration unless you intentionally want Claude to access the
  admin-only session tools.

## Other MCP Clients

The same running server can be connected to other MCP clients that support streamable HTTP, such as:

- Cursor
- Custom Python agents using `openai-agents`

See [MCP Client Integration](docs/mcp_client_integration.md) for examples.

## Example Prompts

- `Load the network from /app/data/my_grid.xiidm and list all generators.`
- `Create an IEEE 118 network, run a DC load flow, and report overloaded lines.`
- `Run an N-1 security analysis on the current network using automatically generated contingencies.`
- `Show me a single-line diagram for substation S1.`
- `Export the current network and give me the download link.`
- `Generate a Python script that reproduces the current study.`

## API Documentation Skill

The server can fetch official pypowsybl API documentation on demand with two tools that both return the
markdown directly in a `content` field (so any agent can use them, even one that cannot issue
`resources/read`):

- `read_resource(resource_id)` — return a page **already cached** for the session, instantly; reports a
  cache miss otherwise.
- `get_online_resource(class_object, method_name)` — **download** a page and cache it. Call it with a module
  name (e.g. `network`) and an empty `method_name` to list the module's methods, then with a specific
  `method_name` to get its detailed documentation.

The recommended workflow is: try `read_resource` first, and fall back to `get_online_resource` only on a
cache miss. Pages are cached per session under the ids `{class_object}` (module page) and
`{class_object}-{method_name}` (method page), also reachable as `resources://temp/{id}` for clients that do
support `resources/read`.

The workflow instructions live in [`pypowsybl_mcp/skills/remote-resource.md`](pypowsybl_mcp/skills/remote-resource.md)
and are exposed in three ways so any agent can use them:

- **Tool description** — the `get_online_resource` tool description embeds the workflow, so agents without
  skill or prompt support can use it out of the box.
- **MCP prompt** — each skill file is registered as an MCP prompt. Clients like Claude Code surface it as a
  slash command (e.g. `/mcp__<server-name>__remote-resource`) to inject the instructions into the conversation.
- **MCP resource** — each skill file is also listed as a resource at `skills://skills/remote-resource`,
  readable by any client supporting `resources/read`.

For agents with native skill support (Claude Code, Claude Desktop), the file is written in the
[Agent Skills](https://code.claude.com/docs/en/skills) format: copy it to
`.claude/skills/remote-resource/SKILL.md` in your project (or `~/.claude/skills/` for all projects) and the
agent will trigger it automatically whenever pypowsybl API details are needed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to set up the environment, run tests, and submit pull requests.

## Development

Install the package with development tools (open-source backend is included by default):

```bash
uv pip install -e ".[dev]"
```

Then run the test suite and linter:

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

Tests live in `tests/` and the main source code lives in `pypowsybl_mcp/`.

## License

See [LICENSE.md](LICENSE.md).
