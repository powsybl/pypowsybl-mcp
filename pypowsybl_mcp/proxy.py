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


class PyPowsyblMCPServerProxy:
    """FastMCP Server proxy for PyPowsybl integration with state management."""

    def __init__(self):
        self.networks: ThreadSafeTTLCache[str, Network] = ThreadSafeTTLCache(
            maxsize=MAX_NUMBER_OF_GRIDS, ttl=GRID_TTL
        )
        self.current_network_id: str | None = None
        self.current_network: Any | None = None
        # Init default loadflow configuration
        self.init_lf_params_from_config()
        # Empty string means system default provider
        self.lf_provider: str = "OpenLoadFlow"
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
        with LF_DEFAULT_PARAMETERS_PATH.open("rb") as f:
            loadflow_config = tomllib.load(f)
        self.lf_provider = loadflow_config.get("provider", "OpenLoadFlow")
        pp_lf_cfg = {
            key: value
            for key, value in loadflow_config.items()
            if key
            != "provider"  # Remove provider if it's there as it's not a parameter of pp.loadflow.Parameters
        }
        pp_lf_cfg["voltage_init_mode"] = VOLTAGE_MODES.get(
            pp_lf_cfg.get("voltage_init_mode", "UNIFORM_VALUES")
        )
        # Convert balance type from string
        pp_lf_cfg["balance_type"] = BALANCE_TYPES.get(
            pp_lf_cfg.get("balance_type", "PROPORTIONAL_TO_GENERATION_P_MAX")
        )

        self.lf_params = pp.loadflow.Parameters(**pp_lf_cfg)

    def update_lf_params(self, **params):
        """Update PyPowsybl loadflow parameters with new values."""
        # Convert voltage_init_mode from string if provided
        if "voltage_init_mode" in params and isinstance(
            params["voltage_init_mode"], str
        ):
            params["voltage_init_mode"] = VOLTAGE_MODES.get(params["voltage_init_mode"])

        # Convert balance_type from string if provided
        if "balance_type" in params and isinstance(params["balance_type"], str):
            params["balance_type"] = BALANCE_TYPES.get(params["balance_type"])

        # Convert component_mode from string if provided
        if "component_mode" in params and isinstance(params["component_mode"], str):
            params["component_mode"] = COMPONENT_MODES.get(params["component_mode"])

        # connected_component_mode is deprecated in pypowsybl in favor of
        # component_mode. Convert it ourselves instead of setting it on
        # lf_params, so we don't trigger pypowsybl's DeprecationWarning and
        # don't override an explicitly provided component_mode.
        if "connected_component_mode" in params:
            connected_component_mode = params.pop("connected_component_mode")
            if isinstance(connected_component_mode, str):
                connected_component_mode = CONNECTED_COMPONENT_MODES.get(
                    connected_component_mode
                )
            if connected_component_mode is not None and "component_mode" not in params:
                params["component_mode"] = (
                    pp.loadflow.ComponentMode.MAIN_CONNECTED
                    if connected_component_mode
                    == pp.loadflow.ConnectedComponentMode.MAIN
                    else pp.loadflow.ComponentMode.ALL_CONNECTED
                )

        for key, value in params.items():
            if hasattr(self.lf_params, key):
                setattr(self.lf_params, key, value)

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
                "transformers": len(transformers),
                "is_current": network_id == self.current_network_id,
                "has_loadflow_results": network_id in self.loadflow_results,
            }
        except Exception as e:
            logger.error(f"Error getting network summary: {e}")
            return {"error": f"Error getting network summary: {str(e)}"}

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

        except Exception as e:
            logger.error(f"Error creating visualization: {e}")
            raise ValueError(f"Error creating visualization: {str(e)}")

    def save_markdown_resource(self, content: str, resource_id: str) -> str:
        """
        Save markdown content in the TTLCache and return its MCP resource URI.

        """
        self.resources[resource_id] = content
        return f"resources://temp/{resource_id}"
