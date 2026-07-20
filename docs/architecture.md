### Architecture Overview

This document describes the internal architecture of the PyPowsybl MCP server.

#### High-level picture

```
┌─────────────────────────────────────────────────────────────┐
│                        MCP Client                           │
│          (Claude Desktop, Cursor, custom agent…)            │
└────────────────────────────┬────────────────────────────────┘
                             │  streamable-HTTP  (MCP protocol)
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                  FastMCP server  (server.py)                 │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Tool registry (8 groups)                │   │
│  │  io · network · visualization · loadflow             │   │
│  │  security · sensitivity · session · code_export      │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │   ThreadSafeTTLCache[session_id → Proxy]            │   │
│  │   max 50 sessions · TTL 24 h                        │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  /download/{token}/{filename}  (custom HTTP route)          │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│              PyPowsyblMCPServerProxy  (proxy.py)            │
│                                                             │
│  networks: ThreadSafeTTLCache[network_id → pp.Network]      │
│  current_network_id / current_network                       │
│  loadflow_results: ThreadSafeTTLCache[id → Any]             │
│  loadflow_params  (from LF_default_parameters.toml)         │
│  visualization_config  (from visualization_default_…toml)   │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
                    pypowsybl / pypowsybl-rte
```

#### Key components

| Component                 | File                                             | Role                                       |
|---------------------------|--------------------------------------------------|--------------------------------------------|
| `FastMCP` server          | `pypowsybl_mcp/server.py`                        | Entry point, tool registration, HTTP route |
| `PyPowsyblMCPServerProxy` | `pypowsybl_mcp/proxy.py`                         | Per-session state container                |
| Tool groups               | `pypowsybl_mcp/tools/`                           | Business logic, one class per domain       |
| Session utilities         | `pypowsybl_mcp/utils/user_session_management.py` | Session ID lifecycle                       |
| Download utilities        | `pypowsybl_mcp/utils/download_utils.py`          | Temporary token-based file download        |
| LLM agents                | `pypowsybl_mcp/llm_utils/agents/`                | Optional code-generation agent             |

#### Tool groups

| Group             | Module                         | Main responsibilities                                                   |
|-------------------|--------------------------------|-------------------------------------------------------------------------|
| **io**            | `tools/utils/io.py`            | Load networks from file or URL; export networks                         |
| **network**       | `tools/network_tools.py`       | Create IEEE networks; inspect, modify, and manage networks and variants |
| **visualization** | `tools/utils/visualisation.py` | Single-line diagrams; network area diagrams                             |
| **loadflow**      | `tools/loadflow_tools.py`      | Run AC/DC load flow; manage load flow parameters                        |
| **security**      | `tools/security_tools.py`      | N-1 security analysis; contingency list generation                      |
| **sensitivity**   | `tools/sensitivity_tools.py`   | DC/AC sensitivity, PSDF, DCDF, PTDF analyses                            |
| **session**       | `tools/utils/session.py`       | Admin tools: set/duplicate session (token-protected)                    |
| **code_export**   | `tools/utils/code_export.py`   | Generate standalone Python scripts from session history                 |

#### Request lifecycle

```
MCP tool call
    │
    ├─ 1. FastMCP deserializes arguments and injects Context
    │
    ├─ 2. Tool handler calls get_session_id(ctx)
    │       └─ assigns a UUID to ctx.session.session_id if missing
    │
    ├─ 3. Tool handler calls self.get_proxy(session_id)
    │       └─ creates a new PyPowsyblMCPServerProxy if not in cache
    │       └─ stores it in pypowsybl_proxies[session_id]
    │
    ├─ 4. Business logic executes via the proxy
    │       └─ reads/writes proxy.networks, proxy.current_network, …
    │
    └─ 5. Returns a string result to the MCP client
```

#### Transport

The server uses the **streamable-HTTP** MCP transport (not stdio). It listens on `0.0.0.0` at the port defined by
`MCP_PORT` (default `9992`). A custom `GET /download/{token}/{filename}` route serves temporary file downloads
generated by export or visualization tools.

#### Stateful vs stateless design

The server is **stateful**: loaded networks, load flow results, and parameters persist in memory for the lifetime of
the session proxy (up to 24 h of inactivity). This avoids re-sending large grid files on every tool call.

See [mcp_sessions.md](mcp_sessions.md) for the full session and TTL-cache design.

#### LLM code-generation agent

`pypowsybl_mcp/llm_utils/agents/code_generation.py` contains an optional OpenAI-Agents-based sub-agent that can
generate standalone `pypowsybl` Python scripts from a natural-language description. It is invoked by the
`generate_python_script` MCP tool and requires `OPENAI_API_KEY` / `OPENAI_BASE_URL` to be configured.
