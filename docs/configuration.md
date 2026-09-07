### Configuration Reference

This document describes all configuration options for the PyPowsybl MCP server.

---

#### Environment variables (`.env`)

Copy `.env.template` to `.env` at the project root and fill in the values. All variables are optional unless
marked **required**.

##### Server

| Variable             | Default     | Description                                                                                                                       |
|----------------------|-------------|-----------------------------------------------------------------------------------------------------------------------------------|
| `MCP_PORT`           | `9992`      | TCP port the MCP server listens on.                                                                                               |
| `MCP_PUBLIC_ADDRESS` | `localhost` | Hostname or IP embedded in download URLs returned by tools. Must be reachable from the LLM's environment.                         |
| `MCP_VERIFY_SSL`     | `false`     | Enable (`true`/`yes`/`1`/`on`/`enabled`) or disable (`false`/`no`/`0`/`off`/`disabled`) TLS certificate verification globally for HTTPS downloads (e.g. in `load_network_from_url`). Any other value is treated as disabled and logged as a warning. |
| `MCP_AUTH_TOKEN`     | *(unset)*   | Secret token protecting admin tools (`set_session_id`, `duplicate_session`). If unset, those tools are disabled.                  |
| `LOG_DIR`            | `logs`      | Directory where log files are written.                                                                                            |

##### LLM / OpenAI

| Variable               | Default   | Description                                                                       |
|------------------------|-----------|-----------------------------------------------------------------------------------|
| `OPENAI_API_KEY`       | *(unset)* | API key for the OpenAI-compatible endpoint. Required by `generate_python_script`. |
| `OPENAI_BASE_URL`      | *(unset)* | Base URL of the OpenAI-compatible endpoint (leave empty for api.openai.com).      |
| `OPENAI_DEFAULT_MODEL` | *(unset)* | Model name used by the code-generation agent (e.g. `gpt-4.1-mini`).               |
| `AI_TIMEOUT`           | `90`      | Timeout in seconds for LLM calls.                                                 |

##### Docker

| Variable                | Default   | Description                                                                 |
|-------------------------|-----------|-----------------------------------------------------------------------------|
| `HOST_UID`              | `1000`    | UID of the host user; used to set correct ownership on mounted volumes.     |
| `HOST_GID`              | `1000`    | GID of the host user; same purpose.                                         |
| `NO_PROXY` / `no_proxy` | *(unset)* | Passed through to the container to avoid proxy issues on internal networks. |

---

#### Load flow parameters (`pypowsybl_mcp/config/LF_default_parameters.toml`)

These are the default load flow parameters loaded at server startup. They can be overridden per session with
`update_loadflow_params` and reset with `restore_default_loadflow_param`.

| Parameter           | Default                            | Description                                                                                                                                                                          |
|---------------------|------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `voltage_init_mode` | `UNIFORM_VALUES`                   | Initial voltage values for the iterative solver. Options: `UNIFORM_VALUES`, `PREVIOUS_VALUES`, `DC_VALUES`.                                                                          |
| `balance_type`      | `PROPORTIONAL_TO_GENERATION_P_MAX` | Method for distributing the slack power. Options: `PROPORTIONAL_TO_GENERATION_P_MAX`, `PROPORTIONAL_TO_GENERATION_P`, `PROPORTIONAL_TO_LOAD`, `PROPORTIONAL_TO_GENERATION_AND_LOAD`. |
| `distributed_slack` | `true`                             | Whether to distribute the slack across generators instead of concentrating it on a single slack bus.                                                                                 |

---

#### Visualization parameters (`pypowsybl_mcp/config/visualization_default_parameters.toml`)

Default rendering options for diagrams. These are loaded at server startup and apply to all visualization tools.

##### `[network_area_diagram]`

| Parameter                    | Default | Description                                                                                  |
|------------------------------|---------|----------------------------------------------------------------------------------------------|
| `low_nominal_voltage_bound`  | `-1`    | Lower voltage bound (kV) for filtering elements in area diagrams. `-1` means no lower bound. |
| `high_nominal_voltage_bound` | `-1`    | Upper voltage bound (kV) for filtering elements in area diagrams. `-1` means no upper bound. |

##### `[sld]` (Single Line Diagram)

| Parameter              | Default | Description                                                                                |
|------------------------|---------|--------------------------------------------------------------------------------------------|
| `topological_coloring` | `true`  | Color diagram elements according to their topological state (energized, de-energized, …). |
| `active_power_unit`    | `MW`    | Unit used to display active power values on the diagram.                                   |
| `reactive_power_unit`  | `MVAR`  | Unit used to display reactive power values on the diagram.                                 |

---

#### Session / cache limits (hardcoded in `server.py` and `proxy.py`)

These values are not configurable via environment variables; change them in source if needed.

| Constant                | Value             | Description                                              |
|-------------------------|-------------------|----------------------------------------------------------|
| `MAX_NUMBER_OF_CLIENTS` | `100`             | Maximum number of concurrent sessions kept in memory.    |
| `CLIENT_SESSION_TTL`    | `86400 s` (1 day) | Time-to-live for an idle session proxy.                  |
| `MAX_NUMBER_OF_GRIDS`   | `10`              | Maximum number of networks per session.                  |
| `GRID_TTL`              | `86400 s` (1 day) | Time-to-live for an idle network inside a session proxy. |
