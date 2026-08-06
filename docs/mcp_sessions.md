### MCP sessions & server-side state

This document explains how **state** and **sessions** are maintained in the PyPowsybl MCP server.

#### What "session" means in MCP (in this codebase)

MCP tool calls are executed with a `Context` object (FastMCP) that contains a `session` object. That session object
is the anchor point used by the server to keep **per-client** state.

The session management utilities live in `pypowsybl_mcp/utils/user_session_management.py`:

- `check_session_id(ctx)` — if `ctx.session.session_id` does not exist, generates a new UUID and stores it on
  `ctx.session.session_id`.
- `get_session_id(ctx)` — ensures a `session_id` exists and returns it.
- `get_session_info(ctx)` — returns session ID, request ID, and client ID for debugging.

This is an **in-memory, server-side** session identifier. It is not automatically the same as a user identity or a
chat conversation ID. It is simply a stable key for the lifetime of the MCP `ServerSession`.

#### Per-session proxies (stateful server design)

The server (`pypowsybl_mcp/server.py`) keeps state per session by mapping `session_id` → `PyPowsyblMCPServerProxy`.

A global TTL cache is defined at server startup:

```python
MAX_NUMBER_OF_CLIENTS = 100
CLIENT_SESSION_TTL = 3600 * 24  # 1 day
pypowsybl_proxies: ThreadSafeTTLCache[str, PyPowsyblMCPServerProxy] = (
    ThreadSafeTTLCache(maxsize=MAX_NUMBER_OF_CLIENTS, ttl=CLIENT_SESSION_TTL)
)
```

The cache is a `ThreadSafeTTLCache` (from `pypowsybl_mcp/utils/cachetools.py`), a `TTLCache` subclass that wraps
all read/write operations with a reentrant `threading.RLock` to ensure safe concurrent access.

This cache is passed to every tool registration function:

```python
register_network_tools(mcp, pypowsybl_proxies)
register_loadflow_tools(mcp, pypowsybl_proxies)
# ... etc.
```

Each tool handler follows the same pattern:

1. `session_id = get_session_id(ctx)`
2. `proxy = self.get_proxy(session_id)` — creates a new `PyPowsyblMCPServerProxy` on first access
3. Execute operations via that proxy

This means the server keeps long-lived objects in memory (loaded IIDM networks, analysis parameters, cached results)
**without requiring the client to resend everything on each tool call**.

#### What the proxy holds

`PyPowsyblMCPServerProxy` (`pypowsybl_mcp/proxy.py`) is the per-session state container. Each instance holds:

- `networks: ThreadSafeTTLCache[str, Network]` — loaded power networks, keyed by network ID
  (`MAX_NUMBER_OF_GRIDS=10`, `GRID_TTL=1 day`); thread-safe for concurrent tool calls
- `current_network_id` / `current_network` — the currently active network for operations
- `loadflow_results: ThreadSafeTTLCache[str, Any]` — cached load flow results, same TTL/size limits as networks;
  thread-safe
- `loadflow_params` — load flow parameters (initialized from `config/LF_default_parameters.toml`)
- `visualization_config` — visualization parameters (initialized from `config/visualization_default_parameters.toml`)

There are therefore **two levels of TTL caching**:

| Level                  | Cache                                      | Max size    | TTL   |
|------------------------|--------------------------------------------|-------------|-------|
| Session                | `pypowsybl_proxies` (`ThreadSafeTTLCache`) | 50 sessions | 1 day |
| Network (inside proxy) | `proxy.networks` (`ThreadSafeTTLCache`)    | 10 networks | 1 day |

#### Admin tools: session management

Two protected admin tools are registered in `pypowsybl_mcp/tools/utils/session.py`. They are guarded by an
`MCP_AUTH_TOKEN` environment variable and **must never be called directly by an LLM**.

- `set_session_id(authorization_token, session_id, ctx)` — overrides the session ID for the current MCP connection.
  Useful when an external orchestrator needs to bind an MCP connection to a specific pre-existing session.

- `duplicate_session(authorization_token, source_session_id, target_session_id, overwrite, ctx)` — deep-copies the
  proxy state (networks, parameters, results) from one session to another. Useful for forking a conversation while
  preserving the full power system state.

#### Lifecycle: when state is created and when it disappears

1. **Creation** — on the first tool call for a given MCP session, `session_id` is assigned (if missing), then a
   `PyPowsyblMCPServerProxy` is created and stored in `pypowsybl_proxies`.

2. **Reuse** — subsequent tool calls in the same MCP session reuse the same proxy instance, so state (loaded
   networks, parameters, results) persists across calls.

3. **Eviction / cleanup** — the `ThreadSafeTTLCache` removes a session proxy automatically after `CLIENT_SESSION_TTL`
   without
   access, and enforces the `MAX_NUMBER_OF_CLIENTS` limit. Eviction is an in-memory cleanup mechanism; it is not a
   transactional "logout" and should be treated as best-effort. Individual networks inside a proxy are also subject
   to their own TTL (`GRID_TTL`).

#### Implications & pitfalls

- **Memory / resource usage** — stateful servers are convenient but can keep large objects alive. TTL and max-size
  limits at both the session and network level are important safety valves.

- **Client reconnections create new sessions** — if the MCP client disconnects and reconnects (or the server
  restarts), the server-side session ID changes. Any in-memory state tied to the old `session_id` is lost, even if
  the client-side conversation history still exists. Use `set_session_id` (with a stable external ID) to mitigate
  this if needed.

- **Session ≠ user identity** — a "session" here is a transport/connection concept. If you need per-user isolation
  or persistence, you must explicitly design for it (e.g., pass a user identifier via `set_session_id`, store state
  to disk, or manage mappings at the application layer).

- **Concurrency** — if multiple tool calls happen concurrently within the same session, they share the same proxy
  instance. Both `pypowsybl_proxies` and `proxy.networks` use `ThreadSafeTTLCache`, which protects all cache
  operations with a reentrant lock. Proxy-level business logic (e.g. read-modify-write sequences) may still require
  additional synchronization if atomicity across multiple operations is needed.

#### Adding a new tool

When registering a new tool, follow the established pattern:

```python
def register_my_tools(mcp: FastMCP, pypowsybl_proxies: ThreadSafeTTLCache):
    @mcp.tool()
    async def my_tool(..., ctx: Context[ServerSession, None] = None) -> str:
        session_id = get_session_id(ctx)
        proxy = pypowsybl_proxies.get(session_id) or PyPowsyblMCPServerProxy()
        pypowsybl_proxies[session_id] = proxy
        # ... use proxy
```

When adding a new MCP server, decide explicitly:

- whether tools should be **stateless** (client passes all required inputs on each call), or
- **stateful** (server keeps per-session objects + TTL/limits + clear lifecycle expectations).
