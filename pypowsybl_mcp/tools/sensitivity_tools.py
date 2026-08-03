#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import json
from typing import Any

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.tools import PyPowsyblTool
from pypowsybl_mcp.utils.pagination import attach_pagination, paginate
from pypowsybl_mcp.utils.user_session_management import get_session_id


def register_sensitivity_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    tools = SensitivityTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp, exclude=["_handle_sensitivity_result"])


class SensitivityTools(PyPowsyblTool):
    def _handle_sensitivity_result(
        self,
        result,
        matrix_id: str = "m",
        limit: int | None = None,
        cursor: str | int | None = None,
    ) -> str:
        """Format sensitivity matrices (markdown or paginated JSON)."""
        try:
            sensitivity_matrix = result.get_sensitivity_matrix(matrix_id)
            reference_matrix = result.get_reference_matrix(matrix_id)

            if limit is None:
                output = []
                output.append("### Sensitivity Analysis Results")
                output.append(f"Matrix ID: {matrix_id}")
                output.append("\n#### Reference Matrix (Initial Values)")
                output.append(reference_matrix.to_markdown())
                output.append("\n#### Sensitivity Matrix")
                output.append(sensitivity_matrix.to_markdown())
                return "\n".join(output)

            ref_page, pagination = paginate(
                reference_matrix, limit=limit, cursor=cursor
            )
            sens_page, _ = paginate(sensitivity_matrix, limit=limit, cursor=cursor)
            payload = attach_pagination(
                {
                    "matrix_id": matrix_id,
                    "reference_matrix": json.loads(ref_page.to_json(orient="split")),
                    "sensitivity_matrix": json.loads(sens_page.to_json(orient="split")),
                },
                pagination,
            )
            return json.dumps(payload, indent=2)
        except ValueError as e:
            return json.dumps({"success": False, "error": str(e)}, indent=2)
        except Exception as e:
            logger.error(f"Error formatting sensitivity results: {e}")
            return f"Error formatting sensitivity results: {str(e)}"

    async def run_dc_sensitivity_analysis(
        self,
        branches_ids: list[str],
        variables_ids: list[str],
        network_id: str | None = None,
        matrix_id: str = "m",
        distributed_slack: bool = False,
        zones: list[dict[str, Any]] | None = None,
        contingencies: list[str] | None = None,
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Perform DC sensitivity analysis on a network.

        DC sensitivity analysis computes the linear dependency of active power flows on branches
        with respect to variables like active power injections (generators, loads) or phase shifters.
        It is faster than AC analysis and suitable for large networks or screening.

        Related Tools:
        - Use `get_network_elements_ids(element_type='Line')` to find branches for `branches_ids`.
        - Use `get_network_elements_ids(element_type='Generator')` or 'Load' for `variables_ids`.
        - Use `run_loadflow` with `dc=True` to check the initial state.
        - Use `get_online_resource(class_object='sensitivity')` to look up the underlying
          pypowsybl sensitivity API (zone/factor types, signatures) instead of relying on
          prior knowledge.

        Common Workflows:
        1. Identify overloaded lines using `run_loadflow`.
        2. Use `run_dc_sensitivity_analysis` to find which generators or loads most affect those lines.
        3. Adjust those injections using `modify_network` to alleviate congestion.

        Args:
            branches_ids (list[str]): List of branch IDs for which flow sensitivity should be computed.
            variables_ids (list[str]): List of variable IDs (e.g., 'LOAD', 'GEN', or specific element IDs).
            network_id (str, optional): The ID of the network to analyze.
            matrix_id (str, optional): The ID to name the sensitivity matrix. Defaults to "m".
            distributed_slack (bool, optional): Whether to use distributed slack. Defaults to False.
            zones (list[dict], optional): List of zone definitions. Each zone should be a dict with:
                - "id" (str): Zone ID.
                - "type" (str): Zone type ('country', 'empty', or 'glsk').
                - "country" (str, optional): Country code (for 'country' type).
                - "key_type" (str, optional): Zone key type ('GENERATOR_TARGET_P', 'GENERATOR_MAX_P', 'LOAD_P0').
                - "injections" (list[str], optional): List of injection IDs to add to the zone.
            contingencies (list[str], optional): List of element IDs for single-element contingencies.
            limit (int, optional): Maximum rows per matrix (paginated JSON). None = full markdown.
            cursor (str | int, optional): Page offset for matrix rows.
            ctx (Context, optional): FastMCP context.

        Example zones:
            [{"id": "FR", "type": "country", "country": "FR"}]
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id

        if network_id not in proxy.networks:
            return f"Error: Network '{network_id}' not found."

        network = proxy.networks[network_id]

        try:
            analysis = pp.sensitivity.create_dc_analysis()

            # Handle zones
            if zones:
                pp_zones = []
                for zone_def in zones:
                    z_type = zone_def.get("type", "empty")
                    z_id = zone_def.get("id")
                    if z_type == "country":
                        country = zone_def.get("country")
                        key_type_str = zone_def.get("key_type", "GENERATOR_TARGET_P")
                        key_type = getattr(pp.sensitivity.ZoneKeyType, key_type_str)
                        z = pp.sensitivity.create_country_zone(
                            network, country, key_type
                        )
                    elif z_type == "empty":
                        z = pp.sensitivity.create_empty_zone(z_id)
                    else:
                        continue

                    # Add extra injections if specified
                    for inj_id in zone_def.get("injections", []):
                        z.add_injection(inj_id)

                    pp_zones.append(z)
                analysis.set_zones(pp_zones)

            # Handle contingencies
            if contingencies:
                for cont_id in contingencies:
                    analysis.add_single_element_contingency(cont_id)

            # Add factor matrix
            # Variables can be special types or specific element IDs
            analysis.add_branch_flow_factor_matrix(
                branches_ids=branches_ids,
                variables_ids=variables_ids,
                matrix_id=matrix_id,
            )

            params = pp.loadflow.Parameters(distributed_slack=distributed_slack)
            result = analysis.run(network, params)

            return self._handle_sensitivity_result(
                result, matrix_id, limit=limit, cursor=cursor
            )

        except Exception as e:
            logger.error(f"DC Sensitivity Analysis failed: {e}")
            return f"DC Sensitivity Analysis failed: {str(e)}"

    async def run_ac_sensitivity_analysis(
        self,
        network_id: str | None = None,
        branch_flow_factors: dict[str, Any] | None = None,
        bus_voltage_factors: dict[str, Any] | None = None,
        matrix_id: str = "m",
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Perform AC sensitivity analysis on a network.

        AC sensitivity analysis allows computing sensitivities of branch flows or bus voltages
        with respect to injections or regulating equipments, taking into account reactive power
        and voltage magnitudes. It is more accurate than DC analysis but computationally more expensive.

        Related Tools:
        - Use `get_network_elements_ids(element_type='Bus')` to find buses for `bus_voltage_factors`.
        - Use `run_loadflow` to check the initial AC state.
        - Use `get_online_resource(class_object='sensitivity')` to look up the underlying
          pypowsybl sensitivity API (factor types, signatures) instead of relying on
          prior knowledge.

        Common Workflows:
        1. Detect voltage violations using `check_voltage_violations`.
        2. Use `run_ac_sensitivity_analysis` with `bus_voltage_factors` to identify which generators'
           target voltages have the most influence on the violated buses.
        3. Update generator setpoints using `modify_network` to resolve violations.

        Args:
            network_id (str, optional): The ID of the network to analyze.
            branch_flow_factors (dict, optional): Dict specifying branch flow factors:
                - "branches_ids" (list[str]): List of branch IDs.
                - "variables_ids" (list[str]): List of variable IDs (e.g., 'LOAD', 'GEN').
            bus_voltage_factors (dict, optional): Dict specifying bus voltage factors:
                - "bus_ids" (list[str]): List of bus IDs.
                - "target_voltage_ids" (list[str]): List of IDs for target voltage control (e.g., 'GEN').
            matrix_id (str, optional): The ID to name the sensitivity matrix. Defaults to "m".
            limit (int, optional): Maximum rows per matrix (paginated JSON). None = full markdown.
            cursor (str | int, optional): Page offset.
            ctx (Context, optional): FastMCP context.
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id

        if network_id not in proxy.networks:
            return f"Error: Network '{network_id}' not found."

        network = proxy.networks[network_id]

        try:
            analysis = pp.sensitivity.create_ac_analysis()

            if branch_flow_factors:
                analysis.add_branch_flow_factor_matrix(
                    branches_ids=branch_flow_factors["branches_ids"],
                    variables_ids=branch_flow_factors["variables_ids"],
                    matrix_id=matrix_id,
                )

            if bus_voltage_factors:
                analysis.add_bus_voltage_factor_matrix(
                    bus_ids=bus_voltage_factors["bus_ids"],
                    target_voltage_ids=bus_voltage_factors["target_voltage_ids"],
                    matrix_id=matrix_id,
                )

            result = analysis.run(network)
            return self._handle_sensitivity_result(
                result, matrix_id, limit=limit, cursor=cursor
            )

        except Exception as e:
            logger.error(f"AC Sensitivity Analysis failed: {e}")
            return f"AC Sensitivity Analysis failed: {str(e)}"

    async def run_psdf_analysis(
        self,
        branches_ids: list[str],
        phase_shifter_ids: list[str],
        network_id: str | None = None,
        matrix_id: str = "psdf",
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Compute Phase Shift Distribution Factors (PSDF).

        PSDF measures the sensitivity of branch active power flows to changes in phase shifter angles.
        This is essential for identifying which phase shifters can be used to control power flow
        on specific branches.

        Related Tools:
        - Use `get_network_elements_ids(element_type='PhaseShifterTransformer')` to find phase shifters.
        - Use `modify_network` to apply the calculated angle changes.

        Common Workflows:
        1. Identify a congested branch.
        2. Run `run_psdf_analysis` for that branch and all available phase shifters.
        3. Determine the required angle adjustment on the most sensitive phase shifter to reduce the flow.

        Args:
            branches_ids (list[str]): List of branch IDs to monitor.
            phase_shifter_ids (list[str]): List of phase shifter (transformer) IDs to vary.
            network_id (str, optional): The ID of the network to analyze.
            matrix_id (str, optional): The ID to name the sensitivity matrix. Defaults to "psdf".
            limit (int, optional): Maximum rows per matrix. None = full markdown.
            cursor (str | int, optional): Page offset.
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id

        if network_id not in proxy.networks:
            return f"Error: Network '{network_id}' not found."

        network = proxy.networks[network_id]

        try:
            analysis = pp.sensitivity.create_dc_analysis()
            analysis.add_branch_flow_factor_matrix(
                branches_ids=branches_ids,
                variables_ids=phase_shifter_ids,
                matrix_id=matrix_id,
            )
            result = analysis.run(network)
            return self._handle_sensitivity_result(
                result, matrix_id, limit=limit, cursor=cursor
            )
        except Exception as e:
            logger.error(f"PSDF Analysis failed: {e}")
            return f"PSDF Analysis failed: {str(e)}"

    async def run_dcdf_analysis(
        self,
        branches_ids: list[str],
        hvdc_line_ids: list[str],
        network_id: str | None = None,
        matrix_id: str = "dcdf",
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Compute HVDC active power sensitivity (DCDF).

        DCDF measures the sensitivity of branch active power flows to changes in HVDC active power set points.
        It helps in understanding how HVDC links can be used for congestion management in the AC network.

        Related Tools:
        - Use `get_network_elements_ids(element_type='HvdcLine')` to find HVDC lines.
        - Use `modify_network` to adjust HVDC power set points.

        Common Workflows:
        1. Identify congestion in the AC grid.
        2. Run `run_dcdf_analysis` to see how adjusting HVDC link power would redistribute the AC flows.
        3. Re-dispatch HVDC links accordingly.

        Args:
            branches_ids (list[str]): List of branch IDs to monitor.
            hvdc_line_ids (list[str]): List of HVDC line IDs to vary.
            network_id (str, optional): The ID of the network to analyze.
            matrix_id (str, optional): The ID to name the sensitivity matrix. Defaults to "dcdf".
            limit (int, optional): Maximum rows per matrix. None = full markdown.
            cursor (str | int, optional): Page offset.
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id

        if network_id not in proxy.networks:
            return f"Error: Network '{network_id}' not found."

        network = proxy.networks[network_id]

        try:
            analysis = pp.sensitivity.create_dc_analysis()
            analysis.add_branch_flow_factor_matrix(
                branches_ids=branches_ids,
                variables_ids=hvdc_line_ids,
                matrix_id=matrix_id,
            )
            result = analysis.run(network)
            return self._handle_sensitivity_result(
                result, matrix_id, limit=limit, cursor=cursor
            )
        except Exception as e:
            logger.error(f"DCDF Analysis failed: {e}")
            return f"DCDF Analysis failed: {str(e)}"

    async def run_ptdf_analysis(
        self,
        branches_ids: list[str],
        source_zone: str | dict[str, Any],
        target_zone: str | dict[str, Any],
        network_id: str | None = None,
        matrix_id: str = "ptdf",
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Compute Power Transfer Distribution Factors (PTDF) between two zones.

        PTDF measures the sensitivity of branch active power flows to a power transfer from
        a source zone to a target zone. This is typically used in cross-border capacity
        calculations or to assess the impact of large-scale power exchanges.

        Related Tools:
        - Use `list_networks` to see available networks that might have predefined zones.
        - Use `run_dc_sensitivity_analysis` for more granular (element-level) sensitivities.

        Common Workflows:
        1. Define two zones (e.g., "Country A" and "Country B").
        2. Run `run_ptdf_analysis` to find the "critical branches" for a transfer between these zones.
        3. Use the results to determine the maximum safe transfer capacity (NTC).

        Args:
            branches_ids (list[str]): List of branch IDs to monitor.
            source_zone (str | dict): Source zone ID or definition.
            target_zone (str | dict): Target zone ID or definition.
            network_id (str, optional): The ID of the network to analyze.
            matrix_id (str, optional): The ID to name the sensitivity matrix. Defaults to "ptdf".
            limit (int, optional): Maximum rows per matrix. None = full markdown.
            cursor (str | int, optional): Page offset.

        Zone definition (dict):
            - "id" (str): Zone ID.
            - "type" (str): 'country' or 'empty'.
            - "country" (str, optional): Country code for 'country' type.
            - "key_type" (str, optional): 'GENERATOR_TARGET_P', 'GENERATOR_MAX_P', 'LOAD_P0'.
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id

        if network_id not in proxy.networks:
            return f"Error: Network '{network_id}' not found."

        network = proxy.networks[network_id]

        def resolve_zone(zone_def, network):
            if isinstance(zone_def, str):
                return pp.sensitivity.create_empty_zone(zone_def), zone_def
            z_type = zone_def.get("type", "empty")
            z_id = zone_def.get("id")
            if z_type == "country":
                country = zone_def.get("country")
                key_type_str = zone_def.get("key_type", "GENERATOR_TARGET_P")
                key_type = getattr(pp.sensitivity.ZoneKeyType, key_type_str)
                return pp.sensitivity.create_country_zone(
                    network, country, key_type
                ), z_id
            return pp.sensitivity.create_empty_zone(z_id), z_id

        try:
            analysis = pp.sensitivity.create_dc_analysis()
            s_z, s_id = resolve_zone(source_zone, network)
            t_z, t_id = resolve_zone(target_zone, network)

            analysis.set_zones([s_z, t_z])
            # PTDF is (Source -> Target)
            analysis.add_branch_flow_factor_matrix(
                branches_ids=branches_ids,
                variables_ids=[(s_id, t_id)],
                matrix_id=matrix_id,
            )

            params = pp.loadflow.Parameters(distributed_slack=False)
            result = analysis.run(network, params)
            return self._handle_sensitivity_result(
                result, matrix_id, limit=limit, cursor=cursor
            )
        except Exception as e:
            logger.error(f"PTDF Analysis failed: {e}")
            return f"PTDF Analysis failed: {str(e)}"

    async def run_custom_sensitivity_analysis(
        self,
        functions_ids: list[str],
        variables_ids: list[str],
        sensitivity_function_type: str,
        sensitivity_variable_type: str = "AUTO_DETECT",
        network_id: str | None = None,
        matrix_id: str = "custom",
        ac: bool = False,
        contingencies: list[str] | None = None,
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Perform a custom sensitivity analysis by specifying function and variable types.

        This tool provides maximum flexibility for advanced users to compute sensitivities
        between any supported function types (outputs like branch flows, bus voltages, reactive power)
        and variable types (inputs like active/reactive injections, tap positions).

        Use Cases:
        - Sensitivity of reactive power on a branch to a generator's target voltage.
        - Sensitivity of bus voltage to a shunt compensator's status.

        For the exact accepted values of `sensitivity_function_type` and
        `sensitivity_variable_type` (the pypowsybl SensitivityFunctionType /
        SensitivityVariableType enums), call
        get_online_resource(class_object='sensitivity') rather than guessing
        from prior knowledge.

        Args:
            functions_ids (list[str]): List of element IDs for the sensitivity functions.
            variables_ids (list[str]): List of element IDs for the sensitivity variables.
            sensitivity_function_type (str): E.g., 'BRANCH_ACTIVE_POWER_2', 'BUS_VOLTAGE_MAGNITUDE',
                                             'BRANCH_REACTIVE_POWER_1'.
            sensitivity_variable_type (str): E.g., 'INJECTION_ACTIVE_POWER', 'INJECTION_REACTIVE_POWER',
                                             'GENERATOR_TARGET_P', 'AUTO_DETECT'.
            network_id (str, optional): The ID of the network to analyze.
            matrix_id (str, optional): The ID to name the sensitivity matrix.
            ac (bool, optional): Use AC analysis if True, DC if False.
            contingencies (list[str], optional): List of element IDs for single-element
                contingencies. When provided, results include post-contingency sensitivities
                in addition to the pre-contingency (N) state.
            limit (int, optional): Maximum rows per matrix. None = full markdown.
            cursor (str | int, optional): Page offset.
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id

        if network_id not in proxy.networks:
            return f"Error: Network '{network_id}' not found."

        network = proxy.networks[network_id]

        try:
            if ac:
                analysis = pp.sensitivity.create_ac_analysis()
            else:
                analysis = pp.sensitivity.create_dc_analysis()

            f_type = getattr(
                pp.sensitivity.SensitivityFunctionType, sensitivity_function_type
            )
            v_type = getattr(
                pp.sensitivity.SensitivityVariableType, sensitivity_variable_type
            )

            if contingencies:
                for cont_id in contingencies:
                    analysis.add_single_element_contingency(cont_id)
                contingency_context_type = pp.sensitivity.ContingencyContextType.ALL
            else:
                contingency_context_type = pp.sensitivity.ContingencyContextType.NONE

            analysis.add_factor_matrix(
                functions_ids=functions_ids,
                variables_ids=variables_ids,
                contingencies_ids=[],
                contingency_context_type=contingency_context_type,
                matrix_id=matrix_id,
                sensitivity_function_type=f_type,
                sensitivity_variable_type=v_type,
            )

            result = analysis.run(network)
            return self._handle_sensitivity_result(
                result, matrix_id, limit=limit, cursor=cursor
            )
        except Exception as e:
            logger.error(f"Custom Sensitivity Analysis failed: {e}")
            return f"Custom Sensitivity Analysis failed: {str(e)}"
