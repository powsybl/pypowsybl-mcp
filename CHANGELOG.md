# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-06-22

### Added

- Tests: Added unit tests for security tools' helper methods (`_parse_json_if_needed`, `_element_nominal_voltage`,
  `_build_contingencies_from_filter`, `_resolve_contingencies`) to improve coverage of edge cases and error handling.
- PyPowsybl MCP: `run_security_analysis` now returns `ranked_violations`, a single list of the violations with the most
  loaded line first, each already showing its loading and overload in percent. A `limit_type` filter (e.g. `CURRENT`),
  `top_violations` and `violation_types_breakdown` were added so the lines overloaded after an N-1 event can be listed
  in
  one call.
- PyPowsybl MCP: `get_overloaded_elements` in N-1 no longer comes back empty. It was reading the short summary, which
  does not carry the full list of violations; it now reads the detailed answer.
- PyPowsybl MCP: Added `get_overloaded_elements` wrapper to find overloaded elements in
  normal (N) or N-1 operation without chaining multiple tools manually.
- PyPowsybl MCP: Added `set_switch_status` tool to open or close network switches (breakers, disconnectors).
- PyPowsybl MCP: Added new tool `get_pypowsybl_version` to know which version of pypowsybl the MCP is using.
- PyPowsybl MCP: Added possibility to change Load Flow `provider` (in load flow computation and security analysis).
- PyPowsybl MCP: Added support for retrieving `voltage_levels` in `get_network_element_data` and
  `get_network_elements_ids` tools.

### Improved

- PyPowsybl MCP: Improved `get_network_element_data` with advanced filtering by `loading_percent` using
  both permanent and temporary operational limits (N and N-1).
- PyPowsybl MCP: Enhanced pagination support for large tool responses to prevent timeout and memory issues.
- Improved `code_export` functionality: Faster and more accurate Python script generation by focusing on tools actually
  mentioned in the action sequence; Updated LLM prompt for better core logic extraction from MCP tools and to avoid code
  duplication when tools are used multiple times.

### Fixed

- PyPowsybl MCP: `get_network_element_data` in `mode="filter"` now respects `limit`/`cursor` and returns the matches
  page
  by page instead of all at once. When no `limit` is given it falls back to the default page size (the rows are already
  sorted, so you get the most loaded ones). Before, a wide filter such as `loading_percent > 0` returned every line and
  the answer could get far too big. `matched_count` still tells you how many lines matched in total.
- PyPowsybl MCP: `networks` TTLCache in `PyPowsyblMCPServerProxy` is now thread-safe via a new `ThreadSafeTTLCache`
  subclass that wraps all read/write operations with a reentrant `threading.RLock`, covering all cache methods
  (`__getitem__`, `__setitem__`, `__delitem__`, `__contains__`, `__len__`, `__iter__`, `get`, `pop`, `popitem`,
  `setdefault`, `update`, `clear`, `expire`, `keys`, `values`, `items`).
- Tests: added concurrent access tests for `ThreadSafeTTLCache` (parallel writes correctness, mixed operations,
  `setdefault` atomicity).
- PyPowsybl MCP: HTTPS certificate verification for `load_network_from_url` is now configured globally at module
  initialization via `MCP_VERIFY_SSL` (default `false`), including Docker runtime default.

### Added

- Docs: added `getting_started.md` (install, configuration, first analysis, troubleshooting).
- Docs: added `architecture.md` (server components, tool groups, request lifecycle).
- Docs: added `tools_reference.md` (full catalogue of all MCP tools).
- Docs: added `configuration.md` (environment variables and TOML config files).
- Docs: added `mcp_client_integration.md` (Claude Desktop, Cursor, custom agent integration).
- README: updated documentation index to reference all new and existing docs.

### Changed

- PyPowsybl MCP: updated `IOTools` (`load_network_from_url`, `load_network_from_file`, `export_network`) to return a
  dictionary with `status` and `message` instead of a plain string for better consistency and error handling.

### Added

- PyPowsybl MCP: new tool `update_loadflow_params` to modify load flow parameters in the current session.
- Tests: added unit tests for `update_loadflow_params` and `restore_default_loadflow_param` tools.
- Tests: added comprehensive unit tests for `IOTools` (loading/exporting networks).
- Tests: added unit tests for `wrap_class_methods_with_mcp_tool` and `PyPowSyBlTool` in `tools.__init__.py`.

- Added functions to disntinguish pypowsybl versions.
- Docker support: added `Dockerfile` and `docker-compose.yml` for easy deployment.

## [0.5.1] - 2026-02-25

### Removed

- PyPowsybl MCP: snapshots functionality has been removed (duplicate of tabs and variants)

## [0.5.0] - 2026-02-25

### Added

- PyPowsybl MCP: new tool `clone_variant` returning cloning the working network with all its element as the
  original, but with a new ID.
- PyPowsybl MCP: new tool `set_working_variant` setting the working network to the variant with the given
  ID.
- PyPowsybl MCP: new tool `get_working_variant` returning the ID of the working network.
- PyPowsybl MCP: new tool `get_variant_list` returning a list of all variants for a given loaded network.
- PyPowsybl MCP: new tool `remove_variant` deleting a variant from the working network.

### Fixed

- PyPowsybl MCP: fixed `duplicate_session` and `set_session_id` tools missing authorization token in tests.
- PyPowsybl MCP: secured admin tools by requiring `MCP_AUTH_TOKEN` to be set in the environment.

### Changed

- PyPowsybl MCP: `get_network_element_data` adding option to return comparison of element of two variants of the same
  network.

## [0.4.2] - 2026-02-24

### Added

- PyPowsybl MCP: new tool `get_top_active_power_transit_lines` returning top-K lines by active power
  transit (MW), with explicit flow side (from/to) and unit.

## [0.4.0] - 2026-02-23

### Added

- Added support for sensitivity analysis in PyPowsybl MCP server, including:
    - `run_dc_sensitivity_analysis` for basic DC sensitivity and zones.
    - `run_ac_sensitivity_analysis` for branch flow and bus voltage sensitivities.
    - `run_psdf_analysis` for Phase Shift Distribution Factors.
    - `run_dcdf_analysis` for HVDC sensitivity.
    - `run_ptdf_analysis` for zone-to-zone Power Transfer Distribution Factors.
    - `run_custom_sensitivity_analysis` for granular control over function and variable types.

## [0.2.0] - 2026-01-09

### Added

- User session management for pypowsybl MCP server.
- Support for DC load flows.
- Export conversation history and download via links.

### Added

## [0.1.0] - 2024-12-23

### Added

- Initial release with Model Context Protocol (MCP) integration for pypowsybl.
