### MCP Client Integration

This document explains how to connect an MCP client to the PyPowsybl MCP server.

---

#### Prerequisites

The server must be running and reachable before configuring any client. See the README for startup instructions.

Default endpoint: `http://localhost:9992/mcp`

Use the same port as `MCP_PORT` in every client URL. If `MCP_PORT` is not set, the server defaults to `9992`.

The server uses the **streamable-HTTP** MCP transport. Clients that only support the `stdio` transport cannot
connect directly.

---

#### Codex

Add the server to Codex configuration in either `~/.codex/config.toml` or `.codex/config.toml`:

```toml
[mcp_servers.pypowsybl]
url = "http://localhost:9992/mcp"
```

Restart Codex after saving the file.

Use the same host or domain in both the Codex URL and `MCP_PUBLIC_ADDRESS` when the server runs remotely, otherwise
file download links returned by the server will not be reachable from Codex.

Do not pass `MCP_AUTH_TOKEN` to Codex unless you intentionally want Codex to access the admin-only session tools.

---

#### Claude Desktop

Add the server to `claude_desktop_config.json` (usually at
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

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

Restart Claude Desktop after saving the file. The PyPowsybl tools will appear in the tool list.

---

#### Cursor

Open **Settings → MCP** and add a new server entry:

```json
{
  "pypowsybl": {
    "type": "streamable-http",
    "url": "http://localhost:9992/mcp"
  }
}
```

---

#### Custom Python agent (openai-agents)

```python
from agents.mcp import MCPServerStreamableHttp
from agents import Agent, Runner

async def main():
    async with MCPServerStreamableHttp(
        name="pypowsybl",
        params={"url": "http://localhost:9992/mcp"},
    ) as mcp_server:
        agent = Agent(
            name="power-grid-agent",
            instructions="You are a power system expert. Use the pypowsybl tools to answer questions.",
            mcp_servers=[mcp_server],
        )
        result = await Runner.run(agent, "Load the IEEE14 network and run a load flow.")
        print(result.final_output)
```

---

#### Remote server (Docker or VM)

When the server runs on a remote host, replace `localhost` with the actual address, use the port configured by
`MCP_PORT`, and make sure `MCP_PUBLIC_ADDRESS` in `.env` is set to the same address so that download URLs are routable
from the client:

```
MCP_PUBLIC_ADDRESS=192.168.1.50
MCP_PORT=9992
```

Client URL: `http://192.168.1.50:9992/mcp`

---

#### Session pinning (multi-turn conversations)

By default each new MCP connection gets a fresh session. To reuse an existing session across reconnections (e.g.
after a client restart), call the admin tool `set_session_id` with a stable identifier before any other tool call:

```
set_session_id(authorization_token="<MCP_AUTH_TOKEN>", session_id="my-stable-id")
```

This requires `MCP_AUTH_TOKEN` to be set in `.env`. See [mcp_sessions.md](mcp_sessions.md) for the full session
lifecycle.

---

#### Verifying the connection

Once connected, ask the agent to list available networks:

```
list_networks()
```

An empty list (`[]`) confirms the server is reachable and the session is initialized.

To verify the server is up without an MCP client, use curl on the configured port (`9992` by default):

```bash
curl -i http://localhost:9992/mcp
```

A non-empty JSON response, or an HTTP `406` response telling you the client must accept `text/event-stream`, confirms
that the MCP endpoint is running.
