#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import json
from datetime import UTC, datetime

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.tools import PyPowsyblTool
from pypowsybl_mcp.utils.user_session_management import get_session_id


def register_loadflow_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    tools = LoadflowTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


class LoadflowTools(PyPowsyblTool):
    async def get_loadflow_provider_info(
        self,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Get information about available load flow providers.

        Lists all load flow computation engines (providers) available in the current
        PyPowSyBl installation, along with the currently configured provider for this
        session and the system default provider.

        Each provider may support different algorithms and specific parameters.
        Use this tool to discover which providers are installed before choosing one
        with set_loadflow_provider.

        Args:
            ctx: FastMCP context (injected automatically).

        Returns:
            str: JSON formatted string containing:
                - success (bool): Whether the operation completed successfully
                - available_providers (list[str]): List of installed provider names
                - default_provider (str): The system-wide default provider
                - session_provider (str): The provider currently configured for this session
                    (empty string means using the system default)
                - providers_details (dict): For each provider, its specific parameters with
                    description, type, default value and possible values
                - error (str): Error message (if failed)
        """
        logger.debug("Getting loadflow provider information")
        session_id = get_session_id(ctx)

        try:
            available_providers = pp.loadflow.get_provider_names()
            default_provider = pp.loadflow.get_default_provider()
            session_provider = self.get_proxy(session_id).lf_provider

            # Get detailed parameters for each provider
            providers_details = {}
            for provider_name in available_providers:
                try:
                    params_df = pp.loadflow.get_provider_parameters(provider_name)
                    provider_params = {}
                    for param_name in params_df.columns:
                        provider_params[param_name] = {
                            "description": str(params_df.loc["description", param_name])
                            if "description" in params_df.index
                            else "",
                            "type": str(params_df.loc["type", param_name])
                            if "type" in params_df.index
                            else "",
                            "default": str(params_df.loc["default", param_name])
                            if "default" in params_df.index
                            else "",
                            "possible_values": str(
                                params_df.loc["possible_values", param_name]
                            )
                            if "possible_values" in params_df.index
                            else "",
                        }
                    providers_details[provider_name] = provider_params
                except Exception as e:
                    logger.warning(
                        f"Could not get parameters for provider '{provider_name}': {e}"
                    )
                    providers_details[provider_name] = {"error": str(e)}

            result = {
                "success": True,
                "available_providers": available_providers,
                "default_provider": default_provider,
                "session_provider": session_provider,
                "providers_details": providers_details,
            }

            logger.info(
                f"Loadflow provider info retrieved: {len(available_providers)} providers available"
            )
            return json.dumps(result, indent=2)

        except Exception as e:
            logger.exception(f"Failed to get loadflow provider info: {e}")
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    async def set_loadflow_provider(
        self,
        provider: str,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Set the load flow provider for the current session.

        Changes the computation engine used for subsequent load flow calculations
        in this session. The provider must be one of the available providers
        returned by get_loadflow_provider_info.

        This setting is per-session and does not affect other users or sessions.

        IMPORTANT: After calling this tool, always call get_loadflow_provider_info
        to verify that the provider has been correctly set for the current session.

        Args:
            provider (str): Name of the load flow provider to use. Must match one of the
                available providers exactly (case-sensitive). Use an empty string "" to
                reset to the system default provider.
                Common providers:
                    - "OpenLoadFlow": Open-source load flow (default)
                    - "DynaFlow": RTE's dynamic/static flow solver
            ctx: FastMCP context (injected automatically).

        Returns:
            str: JSON formatted string containing:
                - success (bool): Whether the provider was changed successfully
                - previous_provider (str): The provider that was configured before
                - new_provider (str): The newly configured provider
                - available_providers (list[str]): List of valid provider names (if failed)
                - error (str): Error message (if failed)
        """
        logger.debug(f"Setting loadflow provider to '{provider}'")
        session_id = get_session_id(ctx)

        try:
            # Allow empty string to reset to default
            if provider != "":
                available_providers = pp.loadflow.get_provider_names()
                if provider not in available_providers:
                    return json.dumps(
                        {
                            "success": False,
                            "error": f"Provider '{provider}' not found. Available providers: {available_providers}",
                            "available_providers": available_providers,
                        },
                        indent=2,
                    )

            previous_provider = self.get_proxy(session_id).lf_provider
            self.get_proxy(session_id).lf_provider = provider

            result = {
                "success": True,
                "previous_provider": previous_provider,
                "new_provider": provider,
            }

            logger.info(
                f"Loadflow provider changed from '{previous_provider}' to '{provider}'"
            )
            return json.dumps(result, indent=2)

        except Exception as e:
            logger.exception(f"Failed to set loadflow provider: {e}")
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    async def get_loadflow_params(
        self, ctx: Context[ServerSession, None] = None
    ) -> str:
        """Get PyPowsybl loadflow configuration."""
        logger.debug("Getting PyPowsybl loadflow configuration")
        session_id = get_session_id(ctx)

        try:  # Added try-except block
            return self.get_proxy(session_id).lf_params.to_json()
        except Exception as e:
            logger.error(f"Failed to get config: {e}")
            raise  # Re-raise the exception to be handled by MCP if necessary

    async def restore_default_loadflow_param(
        self,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """Restore default loadflow parameters"""
        session_id = get_session_id(ctx)

        logger.info("Restoring load flow parameters to default")
        try:
            self.get_proxy(session_id).init_lf_params_from_config()
            return json.dumps(
                {
                    "success": True,
                    "message": "Load flow parameters reinitialized",
                    "current_params": json.loads(
                        self.get_proxy(session_id).lf_params.to_json()
                    ),
                },
                indent=2,
            )
        except Exception as e:
            logger.exception(f"Failed to restore default load flow parameters: {e}")
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    async def update_loadflow_params(
        self,
        voltage_init_mode: str | None = None,
        transformer_voltage_control_on: bool | None = None,
        use_reactive_limits: bool | None = None,
        phase_shifter_regulation_on: bool | None = None,
        twt_split_shunt_admittance: bool | None = None,
        shunt_compensator_voltage_control_on: bool | None = None,
        read_slack_bus: bool | None = None,
        write_slack_bus: bool | None = None,
        distributed_slack: bool | None = None,
        balance_type: str | None = None,
        dc_use_transformer_ratio: bool | None = None,
        countries_to_balance: list[str] | None = None,
        component_mode: str | None = None,
        connected_component_mode: str | None = None,
        dc_power_factor: float | None = None,
        hvdc_ac_emulation: bool | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Update load flow parameters for the current session.

        This tool allows modifying parameters used for load flow calculations.
        The updated parameters will be used in subsequent run_loadflow calls.

        Args:
            voltage_init_mode (str, optional): The resolution starting point.
                Possible values: "UNIFORM_VALUES", "PREVIOUS_VALUES", "DC_VALUES".
            transformer_voltage_control_on (bool, optional): Simulate transformer voltage control.
            use_reactive_limits (bool, optional): Use reactive limits.
            phase_shifter_regulation_on (bool, optional): Simulate phase shifters regulation.
            twt_split_shunt_admittance (bool, optional): Split shunt admittance of transformers on both sides.
            shunt_compensator_voltage_control_on (bool, optional): Simulate voltage control of shunt compensators.
            read_slack_bus (bool, optional): Read slack bus from the network.
            write_slack_bus (bool, optional): Write selected slack bus to the network.
            distributed_slack (bool, optional): Distribute active power slack on the network.
            balance_type: str, optional): How to distribute active power slack.
                Possible values: "PROPORTIONAL_TO_LOAD", "PROPORTIONAL_TO_GENERATION_P",
                "PROPORTIONAL_TO_GENERATION_P_MAX", etc.
            dc_use_transformer_ratio (bool, optional): In DC mode, take into account transformer ratio.
            countries_to_balance (list[str], optional): List of countries participating to slack distribution.
                Used only if distributed_slack is True.
            component_mode (str, optional): Defines which network components should be computed.
                Possible values: "MAIN_SYNCHRONOUS", "MAIN_CONNECTED", "ALL_CONNECTED".
            connected_component_mode (str, optional): Deprecated, use parameter component_mode instead.
                Possible values: "MAIN", "ALL".
            dc_power_factor (float, optional): Power factor used to convert current limits into active
                power limits in DC calculations.
            hvdc_ac_emulation (bool, optional): Enable AC emulation of HVDC links.
        """
        session_id = get_session_id(ctx)
        logger.debug("Updating load flow parameters")

        try:
            params = {
                "voltage_init_mode": voltage_init_mode,
                "transformer_voltage_control_on": transformer_voltage_control_on,
                "use_reactive_limits": use_reactive_limits,
                "phase_shifter_regulation_on": phase_shifter_regulation_on,
                "twt_split_shunt_admittance": twt_split_shunt_admittance,
                "shunt_compensator_voltage_control_on": shunt_compensator_voltage_control_on,
                "read_slack_bus": read_slack_bus,
                "write_slack_bus": write_slack_bus,
                "distributed_slack": distributed_slack,
                "balance_type": balance_type,
                "dc_use_transformer_ratio": dc_use_transformer_ratio,
                "countries_to_balance": countries_to_balance,
                "component_mode": component_mode,
                "connected_component_mode": connected_component_mode,
                "dc_power_factor": dc_power_factor,
                "hvdc_ac_emulation": hvdc_ac_emulation,
            }
            filtered_params = {k: v for k, v in params.items() if v is not None}

            self.get_proxy(session_id).update_lf_params(**filtered_params)

            return json.dumps(
                {
                    "success": True,
                    "message": "Load flow parameters updated",
                    "current_params": json.loads(
                        self.get_proxy(session_id).lf_params.to_json()
                    ),
                },
                indent=2,
            )
        except Exception as e:
            logger.exception(f"Failed to update load flow parameters: {e}")
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    async def run_loadflow(
        self,
        network_id: str | None = None,
        dc: bool = False,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Execute power flow (loadflow) analysis on a network to compute steady-state operating point.

        Loadflow solves the power flow equations to determine voltage magnitudes
        and angles at all buses, as well as active and reactive power flows through branches.
        This is the fundamental analysis for power system operation and planning.
        Both AC (nonlinear) and DC (linear approximation) loadflows are supported.
        NB. This function uses the current active loadflow parameters.

        IMPORTANT: Before running a loadflow, always call get_loadflow_provider_info to
        verify that the current session provider is the one you intend to use. If it is
        not, use set_loadflow_provider to change it, then call get_loadflow_provider_info
        again to confirm the change.

        For pypowsybl loadflow API details not exposed here (other run modes,
        parameters, provider-specific options), call
        get_online_resource(class_object='loadflow') rather than relying on
        prior knowledge, which may be outdated.

        Args:
            network_id (str, optional): Network to analyze. If None, uses current network. Default: None.
            dc (bool, optional): Run DC loadflow instead of AC. Default: False.

        Returns:
            str: JSON formatted string containing:
                - success (bool): Whether loadflow converged
                - network_id (str): Network identifier
                - converged (bool): Convergence status
                - dc (bool): Whether DC loadflow was run
                - voltage_init_mode (str): Initialization mode used (for AC)
                - distributed_slack (bool): Slack distribution setting
                - balance_type (str): Power balance method
                - components (int): Number of components analyzed
                - total_buses (int): Total number of buses
                - sample_bus_voltages (list): First 10 bus voltages with details
                - failed_components (list): Components that failed (if any)
                - error (str): Error message (if failed)

        Example Output:
            {
              "success": true,
              "network_id": "ieee_14",
              "converged": true,
              "dc": false,
              "voltage_init_mode": "UNIFORM_VALUES",
              "distributed_slack": true,
              "balance_type": "PROPORTIONAL_TO_GENERATION_P_MAX",
              "components": 1,
              "total_buses": 14,
              "sample_bus_voltages": [
                {"name": "Bus 1", "v_mag": 1.060, "v_angle": 0.00},
                {"name": "Bus 2", "v_mag": 1.045, "v_angle": -4.98}
              ]
            }
        """
        session_id = get_session_id(ctx)

        if network_id is None:
            network_id = self.get_proxy(session_id).current_network_id
        logger.debug(
            f"Running {'DC' if dc else 'AC'} load flow for network '{network_id}'"
        )

        if network_id is None:
            return json.dumps(
                {
                    "success": False,
                    "error": "No network specified and no current network selected",
                },
                indent=2,
            )

        if network_id not in self.get_proxy(session_id).networks:
            return json.dumps(
                {"success": False, "error": f"Network '{network_id}' not found"},
                indent=2,
            )

        try:
            network = self.get_proxy(session_id).networks[network_id]

            # Get current LF config
            loadflow_params = self.get_proxy(session_id).lf_params

            # Use the session's active provider
            provider = self.get_proxy(session_id).lf_provider

            # Run loadflow
            if dc:
                results = pp.loadflow.run_dc(
                    network, loadflow_params, provider=provider
                )
            else:
                results = pp.loadflow.run_ac(
                    network, loadflow_params, provider=provider
                )

            # Check if all components converged
            all_converged = all(result.status.name == "CONVERGED" for result in results)

            # Store results
            self.get_proxy(session_id).loadflow_results[network_id] = {
                "converged": all_converged,
                "dc": dc,
                "provider": provider,
                "timestamp": datetime.now(UTC).isoformat(),
                "components": len(results),
            }

            # Get bus data
            buses = network.get_buses()

            # Prepare sample bus voltages
            sample_buses = []
            for i, (_, bus) in enumerate(buses.head(10).iterrows()):
                sample_buses.append(
                    {
                        "name": bus["name"],
                        "v_mag": round(float(bus["v_mag"]), 3),
                        "v_angle": round(float(bus["v_angle"]), 2),
                    }
                )

            if all_converged:
                result = {
                    "success": True,
                    "network_id": network_id,
                    "converged": True,
                    "dc": dc,
                    "components": len(results),
                    "total_buses": len(buses),
                    "sample_bus_voltages": sample_buses,
                    "remaining_buses": max(0, len(buses) - 10),
                }
                logger.info(f"Load flow completed for network '{network_id}'")
                return json.dumps(result, indent=2)
            else:
                # Show which components failed to converge
                failed_components = [
                    {
                        "component_num": result.connected_component_num,
                        "status": result.status.name,
                    }
                    for result in results
                    if result.status.name != "CONVERGED"
                ]

                result = {
                    "success": False,
                    "network_id": network_id,
                    "converged": False,
                    "dc": dc,
                    "components": len(results),
                    "failed_components": failed_components,
                }
                logger.error(f"Load flow failed for network '{network_id}'")
                return json.dumps(result, indent=2)

        except Exception as e:
            logger.exception(f"Failed to run load flow: {e}")
            return json.dumps(
                {"success": False, "network_id": network_id, "error": str(e)}, indent=2
            )
