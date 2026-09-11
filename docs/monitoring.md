### Monitoring: the admin HTTP API

The server is stateful — each MCP session holds its own networks, results and parameters in memory — but until a
session is asked something, none of that is visible from outside. This API answers, at any time and without opening
an MCP session: **how many sessions exist, how old they are, what each one holds, and how much traffic it carries.**

It can be used by any external monitoring dashboard.

---

#### Why HTTP routes rather than MCP tools

The session management tools (`set_session_id`, `duplicate_session`) are MCP tools; these are not, on purpose:

- an MCP tool appears in `tools/list`, so the LLM sees it and may call it — and "how loaded is this server?" is not a
  question a session's own conversation should be answering;
- a monitoring client must be able to ask without opening an MCP session, otherwise observing the server would itself
  create a session and change what is being observed.

---

#### Endpoints

| Route             | Auth  | Purpose                                                             |
|-------------------|-------|---------------------------------------------------------------------|
| `/admin/health`   | none  | Liveness, versions, uptime, session count and capacity. No session detail. |
| `/admin/sessions` | token | Every session held, with its contents and activity.                 |

Both are `GET`, read-only, and served by the same HTTP app as `/mcp` (so on the same host and port).

##### Authentication

`/admin/sessions` reuses **`MCP_AUTH_TOKEN`**, the token that already protects the admin tools. When it is unset, the
route is disabled (`403`), exactly as those tools are.

```bash
curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" http://localhost:9992/admin/sessions
curl -H "X-Admin-Token: $MCP_AUTH_TOKEN"        http://localhost:9992/admin/sessions
```

The token is deliberately **not** accepted as a query parameter, where it would end up in access logs and browser
history.

`/admin/health` needs no token: it exposes no session identifier and nothing about the studies being run, so it can
back a container health check or an uptime probe. Neither route should be exposed to the public internet.

##### `GET /admin/health`

```json
{
  "status": "ok",
  "server": {
    "name": "pypowsybl-mcp", "version": "0.1.0.dev1", "pypowsybl_version": "1.15.0",
    "uptime_s": 48213.0, "started_at": "2026-09-04T08:12:31+00:00", "rss_mb": 1842.5,
    "sessions": 7, "max_sessions": 100, "session_ttl_s": 86400,
    "evictions": {"expired": 3, "capacity": 0}
  },
  "generated_at": "2026-09-04T21:36:44+00:00"
}
```

##### `GET /admin/sessions`

Same `server` block, plus one entry per session, most recently used first:

```json
{
  "sessions": [
    {
      "session_id": "8f1c…", "created_at": "…", "created_at_estimated": false,
      "last_seen": "…", "age_s": 3612.0, "idle_s": 42.0, "expires_in_s": 82788.0,
      "tool_calls": 37, "errors": 1,
      "last_tool": "run_loadflow", "last_tool_at": "…",
      "total_duration_ms": 48210.4, "avg_duration_ms": 1303.0,
      "max_duration_ms": 9840.2, "last_duration_ms": 412.7,
      "tools_used": {"run_loadflow": 12, "get_network_info": 9},
      "current_network_id": "ieee14",
      "networks": [{"id": "ieee14", "current": true}],
      "loadflow_results": 3, "resources": 7, "plugin_results": 0,
      "lf_provider": "OpenLoadFlow"
    }
  ]
}
```

`?networks=full` adds `buses`, `lines`, `generators` and `loads` per network. It is **opt-in** because it
materializes a dataframe per element type: negligible on a test network, decidedly not on a real one.

---

#### How the numbers are produced

`pypowsybl_proxies` knows *which* sessions exist, but a `TTLCache` records neither when an entry appeared, nor when it
was last used, nor what it lost. `pypowsybl_mcp/utils/session_registry.py` keeps that bookkeeping beside the cache:

| Field                                | Where it comes from                                                            |
|--------------------------------------|--------------------------------------------------------------------------------|
| `created_at`, `last_seen`            | `SessionRegistry.touch()`, called from `PyPowsyblTool.get_proxy()`              |
| `tool_calls`, `errors`, `tools_used` | a wrapper around FastMCP's tool manager (`utils/instrumentation.py`)            |
| `*_duration_ms`                      | the same wrapper, timing each call and accumulating a total, a max and the last |
| `evictions`                          | reconciliation: whatever the cache no longer holds is counted and dropped, expiry or capacity told apart by age |
| `rss_mb`                             | `/proc/self/statm` (Linux); `null` elsewhere — no new dependency                |

`get_proxy()` is the single choke point every tool group inherits, which is why a per-session view costs nothing more
than a dict lookup on the hot path. Nothing is persisted: the whole registry describes the running process and dies
with it.

Two behaviors worth knowing when reading the output:

- **`expires_in_s` is a countdown from creation, not an idle timeout.** `cachetools` sets a TTL at insertion and
  reading an entry does not renew it, so a session is dropped `CLIENT_SESSION_TTL` after it was created however busy
  it has been since. `idle_s` is the one that tells you whether anyone is still there.
- **Durations are accumulated, not sampled.** The registry keeps a running total, the worst call and the last one, so
  `avg_duration_ms` is the mean over the session's whole life — a session that was slow this morning and fast since
  still reads as slow. There is no percentile and no per-tool timing: that would mean keeping every call, which this
  registry deliberately does not do. `max_duration_ms` is the one to watch for a stuck tool.
- **Expiry is lazy.** `cachetools` only reaps on access, so a session whose TTL elapsed hours ago still counts in
  `len(cache)`. Both routes call `expire()` before reporting, which means *reading the API is also what keeps the
  eviction counters moving.*

`evictions.expired` counts sessions dropped after their TTL; `evictions.capacity` counts sessions pushed out by
`MAX_NUMBER_OF_CLIENTS` before reaching it — the second is the one to alert on, since it means someone's study
disappeared mid-conversation while the server still had room in time but not in slots.

The two are told apart by the session's **age**, not by how long it went unused: a `TTLCache` times an entry from its
insertion, so a session dropped after living at least `CLIENT_SESSION_TTL` expired however busy it was in its last
seconds, and only one dropped sooner can have been evicted for capacity. The one case this cannot resolve is a session
with `created_at_estimated` (adopted by the registry rather than seen created, e.g. through `duplicate_session`): its
real insertion was earlier than the registry believes, so its expiry may be counted as a capacity eviction.

---

#### Correlating with a client

The session id reported here is the one the client set through `set_session_id`. Correlation with a real user id
shall be made by the client calling this MCP server.
