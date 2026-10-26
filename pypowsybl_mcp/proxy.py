#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import copy
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import pypowsybl as pp
from loguru import logger
from pypowsybl.network import Network

from pypowsybl_mcp import (
    BALANCE_TYPES,
    COMPONENT_MODES,
    CONNECTED_COMPONENT_MODES,
    VOLTAGE_MODES,
)
from pypowsybl_mcp.utils.cachetools import ThreadSafeTTLCache
from pypowsybl_mcp.utils.report_utils import report_messages

MAX_NUMBER_OF_GRIDS = 10
GRID_TTL = 3600 * 24  # in secs, 1 day
# Documentation resources are much lighter than grids; exploring a single
# class page plus its methods can easily exceed the grid cache size.
MAX_NUMBER_OF_RESOURCES = 50
RESOURCE_TTL = 3600 * 24  # in secs, 1 day

LF_DEFAULT_PARAMETERS_PATH = (
    Path(__file__).resolve().parent / "config" / "LF_default_parameters.toml"
)
VISUALIZATION_DEFAULT_PARAMETERS_PATH = (
    Path(__file__).resolve().parent / "config" / "visualization_default_parameters.toml"
)


def _enum_from_name(key: str, name: str, mapping: dict[str, Any]) -> Any:
    """Return mapping[name], or raise a ValueError listing the accepted names."""
    if name not in mapping:
        raise ValueError(
            f"Invalid {key} '{name}'. Accepted values: {', '.join(mapping)}"
        )
    return mapping[name]


def _normalize_lf_enums(params: dict[str, Any]) -> None:
    """Convert enum-valued load flow parameters given by name, in place.

    An unknown name raises ValueError rather than being stored as None, which
    pypowsybl cannot convert and which would break every later load flow.

    connected_component_mode is deprecated in pypowsybl in favor of
    component_mode. It is converted here instead of being passed to
    pp.loadflow.Parameters, so pypowsybl's DeprecationWarning is not triggered
    and an explicitly provided component_mode is not overridden.
    """
    for key, mapping in (
        ("voltage_init_mode", VOLTAGE_MODES),
        ("balance_type", BALANCE_TYPES),
        ("component_mode", COMPONENT_MODES),
        ("connected_component_mode", CONNECTED_COMPONENT_MODES),
    ):
        if key in params and isinstance(params[key], str):
            params[key] = _enum_from_name(key, params[key], mapping)

    if "connected_component_mode" in params:
        connected_component_mode = params.pop("connected_component_mode")
        if connected_component_mode is not None and "component_mode" not in params:
            params["component_mode"] = (
                pp.loadflow.ComponentMode.MAIN_CONNECTED
                if connected_component_mode == pp.loadflow.ConnectedComponentMode.MAIN
                else pp.loadflow.ComponentMode.ALL_CONNECTED
            )


class UnsupportedLoadflowParametersError(ValueError):
    """The session's load flow provider rejected a set of parameters."""

    def __init__(self, provider: str, messages: list[str]):
        self.provider = provider
        self.messages = messages
        super().__init__(
            f"Load flow parameters are not supported by provider "
            f"'{provider or 'default'}'"
        )


class LoadflowConfigError(ValueError):
    """LF_default_parameters.toml cannot be turned into usable parameters."""


def check_lf_params(
    params: pp.loadflow.Parameters, provider: str, dc: bool = False
) -> list[str]:
    """Have a provider check load flow parameters with check_loadflow_parameters().

    pypowsybl's run_ac() and run_dc() force the dc attribute of the parameters,
    so the check does the same on a copy: dc selects the kind of run being
    checked, AC by default.

    Returns the provider's warnings, e.g. a parameter it ignores or a value it
    replaces; empty when there are none. Raises UnsupportedLoadflowParametersError
    when the provider rejects the parameters, and PyPowsyblError when a value is
    invalid.
    """
    candidate = copy.deepcopy(params)
    candidate.dc = dc

    report_node = pp.report.ReportNode()
    supported = pp.loadflow.check_loadflow_parameters(
        candidate, provider=provider, report_node=report_node
    )
    messages = report_messages(report_node)
    if not supported:
        raise UnsupportedLoadflowParametersError(provider, messages)
    return messages


def load_lf_config(path: Path | None = None) -> tuple[str, pp.loadflow.Parameters]:
    """Read the default load flow configuration file.

    Enum-valued parameters (voltage_init_mode, balance_type, component_mode
    and the deprecated connected_component_mode) are given by name, as with
    update_loadflow_params. Returns the configured provider and the load flow
    parameters. Raises LoadflowConfigError, naming the file, for an unknown
    enum name, for a key that pp.loadflow.Parameters does not take, and for a
    combination pypowsybl rejects.
    """
    path = path or LF_DEFAULT_PARAMETERS_PATH
    with path.open("rb") as f:
        loadflow_config = tomllib.load(f)
    # provider is not a parameter of pp.loadflow.Parameters
    provider = loadflow_config.pop("provider", "OpenLoadFlow")
    loadflow_config.setdefault("voltage_init_mode", "UNIFORM_VALUES")
    loadflow_config.setdefault("balance_type", "PROPORTIONAL_TO_GENERATION_P_MAX")
    try:
        _normalize_lf_enums(loadflow_config)
        params = pp.loadflow.Parameters(**loadflow_config)
    except (ValueError, TypeError, pp.PyPowsyblError) as e:
        raise LoadflowConfigError(f"{path}: {e}") from e
    return provider, params


def validate_lf_config(path: Path | None = None) -> None:
    """Check the default load flow configuration file before the server starts.

    Sessions read the file when they are created, on their first tool call, so
    a bad value would otherwise only show up then, in every session. Besides
    what load_lf_config() rejects, the provider must be installed and must
    accept the parameters for an AC run; its warnings are logged. Raises
    LoadflowConfigError.
    """
    path = path or LF_DEFAULT_PARAMETERS_PATH
    provider, params = load_lf_config(path)
    available_providers = pp.loadflow.get_provider_names()
    if provider and provider not in available_providers:
        raise LoadflowConfigError(
            f"{path}: unknown provider '{provider}'. "
            f"Available providers: {', '.join(available_providers)}"
        )
    try:
        warnings = check_lf_params(params, provider)
    except UnsupportedLoadflowParametersError as e:
        raise LoadflowConfigError(f"{path}: {e}: {'; '.join(e.messages)}") from e
    except (pp.PyPowsyblError, TypeError) as e:
        raise LoadflowConfigError(f"{path}: {e}") from e
    for warning in warnings:
        logger.warning(f"{path}: {warning}")


class PyPowsyblMCPServerProxy:
    """MCP server proxy for PyPowsybl integration with state management."""

    def __init__(self):
        self.networks: ThreadSafeTTLCache[str, Network] = ThreadSafeTTLCache(
            maxsize=MAX_NUMBER_OF_GRIDS, ttl=GRID_TTL
        )
        self.current_network_id: str | None = None
        self.current_network: Any | None = None
        # Init default loadflow configuration (this also sets self.lf_provider
        # from the TOML config, e.g. "provider = \"DynaFlow\"")
        self.init_lf_params_from_config()
        self.loadflow_results: ThreadSafeTTLCache[str, Any] = ThreadSafeTTLCache(
            maxsize=MAX_NUMBER_OF_GRIDS, ttl=GRID_TTL
        )
        self.resources: ThreadSafeTTLCache[str, Any] = ThreadSafeTTLCache(
            maxsize=MAX_NUMBER_OF_RESOURCES, ttl=RESOURCE_TTL
        )
        # Dedicated cache for plugin tools
        # kept separate from loadflow_results/security_results so a plugin can
        # never collide with the host's own results, or with another plugin's.
        self.plugin_results: ThreadSafeTTLCache[str, Any] = ThreadSafeTTLCache(
            maxsize=MAX_NUMBER_OF_GRIDS, ttl=GRID_TTL
        )
        with VISUALIZATION_DEFAULT_PARAMETERS_PATH.open("rb") as f:
            self.visualization_config = tomllib.load(f)
        logger.success("PyPowsybl MCP Server proxy initialized")

    def init_lf_params_from_config(self):
        """Initialize PyPowsybl loadflow parameters from configuration file."""
        self.lf_provider, self.lf_params = load_lf_config()

    def update_lf_params(self, **params) -> list[str]:
        """Update PyPowsybl loadflow parameters with new values.

        The changes are applied to a copy first, which the session's provider
        checks for an AC run (see check_lf_params). The session keeps its
        current parameters when the check fails, and when an enum-valued
        parameter is given an unknown name (raises ValueError).

        Returns the provider's warnings about the accepted parameters.
        """
        _normalize_lf_enums(params)

        candidate = copy.deepcopy(self.lf_params)
        for key, value in params.items():
            if hasattr(candidate, key):
                setattr(candidate, key, value)

        messages = self.check_lf_params(params=candidate)
        self.lf_params = candidate
        return messages

    def set_lf_provider(self, provider: str) -> list[str]:
        """Switch the session to another load flow provider.

        The new provider first checks the session's parameters for an AC run
        (see check_lf_params); the session keeps its current provider when the
        check fails. An empty string selects pypowsybl's default provider.

        Returns the new provider's warnings about the session's parameters.
        """
        messages = self.check_lf_params(provider=provider)
        self.lf_provider = provider
        return messages

    def check_lf_params(
        self,
        params: pp.loadflow.Parameters | None = None,
        provider: str | None = None,
        dc: bool = False,
    ) -> list[str]:
        """Run the module-level check_lf_params() for this session.

        params and provider default to the session's own.
        """
        return check_lf_params(
            self.lf_params if params is None else params,
            self.lf_provider if provider is None else provider,
            dc=dc,
        )

    def get_network(self, network_id: str) -> Network | None:
        """Thread-safe retrieval of a network by id."""
        return self.networks.get(network_id)

    def set_network(self, network_id: str, network: Network) -> None:
        """Thread-safe insertion of a network."""
        self.networks[network_id] = network

    def delete_network(self, network_id: str) -> None:
        """Thread-safe deletion of a network."""
        self.networks.pop(network_id, None)

    def network_ids(self) -> list[str]:
        """Thread-safe retrieval of all network ids."""
        return list(self.networks.keys())

    def has_network(self, network_id: str) -> bool:
        """Thread-safe check for network existence."""
        return network_id in self.networks

    def register_network(
        self, network_id: str, network: Network, set_as_current: bool = True
    ) -> None:
        """Store a network and optionally make it the current one.

        Uses the thread-safe accessor and centralizes the "set as current"
        bookkeeping shared by the network-creation and loading tools.
        """
        self.set_network(network_id, network)
        if set_as_current:
            self.current_network_id = network_id
            self.current_network = network

    def invalidate_loadflow(self, network_id: str) -> None:
        """Drop any cached loadflow result for a network.

        Called when a topology or parameter change makes previously computed
        results stale. A no-op when nothing is cached for the network.
        """
        self.loadflow_results.pop(network_id, None)

    def get_plugin_result(self, key: str) -> Any | None:
        """Thread-safe retrieval of a plugin-owned cached result by key."""
        return self.plugin_results.get(key)

    def set_plugin_result(self, key: str, value: Any) -> None:
        """Thread-safe insertion of a plugin-owned cached result."""
        self.plugin_results[key] = value

    def copy(self) -> "PyPowsyblMCPServerProxy":
        """Create a deep copy of the proxy state."""
        new_proxy = PyPowsyblMCPServerProxy()

        # Copy networks
        for network_id, network in self.networks.items():
            new_proxy.set_network(network_id, copy.deepcopy(network))

        # Copy current network selection
        new_proxy.current_network_id = self.current_network_id
        if self.current_network_id and new_proxy.has_network(self.current_network_id):
            new_proxy.current_network = new_proxy.get_network(self.current_network_id)

        # Copy LF configuration
        new_proxy.lf_params = copy.deepcopy(self.lf_params)

        # Copy loadflow results
        for network_id, results in self.loadflow_results.items():
            new_proxy.loadflow_results[network_id] = copy.deepcopy(results)

        # Copy plugin-owned results
        for key, value in self.plugin_results.items():
            new_proxy.plugin_results[key] = copy.deepcopy(value)

        # Copy the loadflow provider. init_lf_params_from_config() (run in
        # __init__) resets it to the TOML default, so a runtime change such as
        # switching to "DynaFlow" would otherwise be lost in the fork.
        new_proxy.lf_provider = self.lf_provider

        # Copy the temporary markdown/documentation resources cache, otherwise
        # resources://temp/... URIs return "resource not found" in the fork.
        for key, value in self.resources.items():
            new_proxy.resources[key] = copy.deepcopy(value)

        # Copy security-analysis results. This is a dynamic attribute set by
        # security_tools only after an analysis runs, so guard with hasattr.
        if hasattr(self, "security_results"):
            new_proxy.security_results = copy.deepcopy(self.security_results)

        # Copy visualization configuration
        new_proxy.visualization_config = copy.deepcopy(self.visualization_config)

        return new_proxy

    def _get_network_summary(self, network_id: str) -> dict[str, Any]:
        """Get summary information about a network."""
        if not self.has_network(network_id):
            return {"error": f"Network '{network_id}' not found"}

        try:
            network = self.get_network(network_id)
            substations = network.get_substations()
            buses = network.get_buses()
            generators = network.get_generators()
            loads = network.get_loads()
            lines = network.get_lines()
            transformers = network.get_2_windings_transformers()

            # Calculate totals
            total_generation = (
                generators["target_p"].sum() if len(generators) > 0 else 0
            )
            total_load = loads["p0"].sum() if len(loads) > 0 else 0

            return {
                "network_id": network_id,
                "substations": len(substations),
                "buses": len(buses),
                "generators": len(generators),
                "total_generation_mw": float(f"{total_generation:.2f}"),
                "loads": len(loads),
                "total_load_mw": float(f"{total_load:.2f}"),
                "lines": len(lines),
                "2_windings_transformers": len(transformers),
                "is_current": network_id == self.current_network_id,
                "has_loadflow_results": network_id in self.loadflow_results,
            }
        except (pp.PyPowsyblError, ValueError, KeyError, OSError) as e:
            logger.error(f"Error getting network summary: {e}")
            return {"error": f"Error getting network summary: {e!s}"}

    def create_network_visualization_bytes(self, network_id: str) -> bytes:
        """Create a network visualization and return as SVG bytes."""
        if not self.has_network(network_id):
            raise ValueError(f"Network '{network_id}' not found")

        network = self.get_network(network_id)

        # Get voltage bounds from args or use defaults
        low_nominal_voltage_bound = self.visualization_config[
            "network_area_diagram"
        ].get("low_nominal_voltage_bound", 90)
        high_nominal_voltage_bound = self.visualization_config[
            "network_area_diagram"
        ].get("high_nominal_voltage_bound", 240)

        try:
            # Create a temporary file for the SVG output
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".svg", delete=False
            ) as tmp_file:
                tmp_path = tmp_file.name

            try:
                # Use PyPowsybl's network area diagram to generate SVG
                network.write_network_area_diagram_svg(
                    tmp_path,
                    low_nominal_voltage_bound=low_nominal_voltage_bound,
                    high_nominal_voltage_bound=high_nominal_voltage_bound,
                )

                # Read the SVG file
                with open(tmp_path, "rb") as svg_file:
                    svg_data = svg_file.read()

                return svg_data

            finally:
                # Clean up temporary file
                tmp_path_obj = Path(tmp_path)
                if tmp_path_obj.exists():
                    tmp_path_obj.unlink()

        except (pp.PyPowsyblError, ValueError, KeyError, OSError) as e:
            logger.error(f"Error creating visualization: {e}")
            raise ValueError(f"Error creating visualization: {e!s}")

    def save_markdown_resource(self, content: str, resource_id: str) -> str:
        """
        Save markdown content in the TTLCache and return its MCP resource URI.

        """
        self.resources[resource_id] = content
        return f"resources://temp/{resource_id}"
