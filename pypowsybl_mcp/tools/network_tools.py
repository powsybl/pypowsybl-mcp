#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import json
from datetime import UTC, datetime

import pandas as pd
import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.tools import NetworkNotFoundError, PyPowsyblTool
from pypowsybl_mcp.utils.element_data_filter import (
    apply_element_filter,
    attach_current_limits,
    attach_tap_changer_data,
)
from pypowsybl_mcp.utils.element_types import (
    ELEMENT_TYPE_TO_GETTER,
    element_type_enum,
    element_type_hint,
)
from pypowsybl_mcp.utils.pagination import (
    DEFAULT_PAGINATION_LIMIT,
    attach_pagination,
    paginate,
)
from pypowsybl_mcp.utils.user_session_management import get_session_id

# Specification driving modify_network. Each element type maps to the network
# getter used to check existence, a label used in messages, the update method
# to call, and the parameters that may be modified. Each parameter maps to
# (update_kwarg, display_name, unit) where display_name/unit only affect the
# success message.
MODIFY_NETWORK_SPEC: dict[str, dict] = {
    "generator": {
        "getter": "get_generators",
        "label": "Generator",
        "updater": "update_generators",
        "parameters": {
            "target_p": ("target_p", "target_p", "MW"),
            "target_v": ("target_v", "target_v", "p.u."),
        },
    },
    "load": {
        "getter": "get_loads",
        "label": "Load",
        "updater": "update_loads",
        "parameters": {
            "p0": ("p0", "p0", "MW"),
            "q0": ("q0", "q0", "MVAr"),
        },
    },
    "line": {
        "getter": "get_lines",
        "label": "Line",
        "updater": "update_lines",
        "parameters": {
            "r": ("r", "resistance", "Ω"),
            "x": ("x", "reactance", "Ω"),
        },
    },
}


def register_network_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    tools = NetworkTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


class NetworkTools(PyPowsyblTool):
    async def create_ieee_network(
        self,
        network_type: str,
        network_id: str,
        set_as_current: bool = True,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a standard IEEE test network for power system analysis.

        IEEE test networks are standardized power system models widely used for benchmarking
        and testing power system analysis algorithms. They provide realistic network topologies
        with predefined parameters.

        Args:
            network_type (str): Type of IEEE network to create. Valid options:
                - "IEEE14": 14-bus system (small test case)
                - "IEEE30": 30-bus system (medium test case)
                - "IEEE57": 57-bus system (medium-large test case)
                - "IEEE118": 118-bus system (large test case)
                - "IEEE300": 300-bus system (very large test case)
            network_id (str): Unique identifier for this network instance. Used to reference
                this network in subsequent operations. Must be unique across all loaded networks.
            set_as_current (bool, optional): If True, sets this network as the current active
                network for operations. Default: True.

        Returns:
            str: Success message with network details (number of buses) or error message.

        Example:
            create_ieee_network("IEEE14", "test_network_1", True)
            → "Successfully created IEEE14 network 'test_network_1' with 14 buses"

        Workflow:
            1. Creates the specified IEEE test network
            2. Registers it in the server's network registry
            3. Stores configuration metadata
            4. Sets as current network if requested

        Notes:
            - Network sizes range from 14 to 300 buses
            - Each network includes generators, loads, transmission lines, and transformers
            - IEEE14 and IEEE30 are recommended for quick testing
            - Larger networks (IEEE118, IEEE300) are better for scalability testing
        """
        logger.debug(f"Creating IEEE {network_type} network '{network_id}'")
        proxy = self.get_proxy(get_session_id(ctx))

        try:
            network_creators = {
                "IEEE14": pp.network.create_ieee14,
                "IEEE30": pp.network.create_ieee30,
                "IEEE57": pp.network.create_ieee57,
                "IEEE118": pp.network.create_ieee118,
                "IEEE300": pp.network.create_ieee300,
            }

            if network_type not in network_creators:
                return f"Unsupported network type: {network_type}. Available types: {list(network_creators.keys())}"

            # Create the network
            network = network_creators[network_type]()
            proxy.register_network(network_id, network, set_as_current)

            buses = network.get_buses()
            return f"Successfully created {network_type} network '{network_id}' with {len(buses)} buses"

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create network: {e}")
            return f"Failed to create network: {e!s}"

    async def switch_network(
        self, network_id: str, ctx: Context[ServerSession, None] = None
    ) -> str:
        """
        Switch the active network context to a different loaded network.

        Changes which network is targeted by operations that don't explicitly specify
        a network_id. Useful when working with multiple networks simultaneously.

        Args:
            network_id (str): ID of the network to switch to. Must be a network that's
                already loaded in the server (use list_networks() to see available networks).

        Returns:
            str: Confirmation message with summary of the switched network, or error message
                if the network doesn't exist.

        Example:
            switch_network("ieee_57_test")
            → "Switched to network 'ieee_57_test' - 57 buses, 7 generators, 42 loads"

        Use Cases:
            - Comparing results across different network configurations
            - Running the same analysis on multiple networks
            - Switching between base case and modified scenarios
            - Working with different time periods or forecasts

        Notes:
            - Does not affect other loaded networks
            - All subsequent operations without explicit network_id will use this network
            - Use get_network_info() to see details of the current network
            - Use list_networks() to see all available networks and which is current

        Related Tools:
            - list_networks(): See all loaded networks and which is current
            - get_network_info(): Get details about current or specific network
            - remove_network(): Remove a network from memory
        """
        logger.debug(f"Switching to network '{network_id}'")
        proxy = self.get_proxy(get_session_id(ctx))

        try:
            if network_id not in proxy.networks:
                available = list(proxy.networks.keys())
                return (
                    f"Network '{network_id}' not found. Available networks: {available}"
                )

            proxy.current_network_id = network_id
            proxy.current_network = proxy.networks[network_id]

            summary = proxy._get_network_summary(network_id)
            return f"Switched to network '{network_id}' - {summary.get('buses', 0)} buses, {summary.get('generators', 0)} generators, {summary.get('loads', 0)} loads"

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to switch network: {e}")
            return f"Failed to switch network: {e!s}"

    async def list_networks(self, ctx: Context[ServerSession, None] = None) -> str:
        """
        List all currently loaded networks with their status and key statistics.

        Provides an overview of all networks loaded in the server, showing which is
        currently active and whether analyses have been performed.

        Returns:
            str: Formatted list of networks with status indicators:
                - (CURRENT): Marks the currently active network
                - [LF✓]: Indicates loadflow has been successfully completed
                - Bus counts for each network

        Example Output:
            "Loaded networks:
              - ieee_14 (CURRENT) [LF✓]: 14 buses
              - ieee_30: 30 buses
              - my_custom_network [LF✓]: 57 buses"

        Status Indicators Explained:
            - (CURRENT): This network is targeted by operations without explicit network_id
            - [LF✓]: Loadflow analysis has been completed and results are cached
            - No marker: Network is loaded but not current, no loadflow run yet

        Use Cases:
            - Check what networks are available
            - Verify which network is currently active
            - See which networks have up-to-date analysis results
            - Decide whether to switch networks or load new ones
            - Manage memory by identifying networks to remove

        Notes:
            - Returns "No networks loaded" if server is empty
            - Very fast operation (metadata only, no computation)
            - Use after loading networks to verify success
            - Loadflow marker clears when network is modified

        Related Tools:
            - switch_network(): Change the active network
            - get_network_info(): Get detailed info about a specific network
            - remove_network(): Remove unneeded networks from memory
            - create_ieee_network(): Load a new IEEE test network
            - load_network_from_file(): Load from file

        Workflow Example:
            1. list_networks() → See what's loaded
            2. switch_network("target") → Change active network
            3. run_loadflow() → Analyze the network
            4. list_networks() → Verify [LF✓] marker appears
        """
        logger.debug("Listing loaded networks")
        proxy = self.get_proxy(get_session_id(ctx))

        try:
            if not proxy.networks:
                return "No networks loaded"

            result = "Loaded networks:\n"
            for network_id in proxy.networks:
                summary = proxy._get_network_summary(network_id)
                current_marker = (
                    " (CURRENT)" if network_id == proxy.current_network_id else ""
                )
                loadflow_marker = (
                    " [LF✓]" if summary.get("has_loadflow_results", False) else ""
                )

                result += f"  - {network_id}{current_marker}{loadflow_marker}: {summary.get('buses', '?')} buses\n"

            return result
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to list networks: {e}")
            return f"Failed to list networks: {e!s}"

    async def get_network_info(
        self, network_id: str | None = None, ctx: Context[ServerSession, None] = None
    ) -> str:
        """
        Get detailed information and statistics about a power system network.

        Retrieves comprehensive metadata about a network including topology statistics,
        component counts, and analysis status. Useful for understanding network structure
        before running analyses.

        Args:
            network_id (str, optional): ID of the network to inspect. If None, uses the
                current active network. Default: None.

        Returns:
            str: JSON-formatted string containing network information with fields like:
                - substations: identifiers of substations
                - buses: Number of buses/nodes
                - generators: Number of generation units
                - loads: Number of load points
                - lines: Number of transmission lines
                - 2_windings_transformers: Number of two-winding transformers
                - has_loadflow_results: Whether loadflow has been run
                - voltage_levels: Voltage level information

        Example:
            get_network_info("ieee_14")
            → JSON with detailed network statistics

        Use Cases:
            - Verify network loaded correctly
            - Check network size before analysis
            - Understand network topology
            - Confirm loadflow results are available
            - Compare network configurations

        Notes:
            - Returns information from network metadata, not from analysis results
            - Very fast operation (no computation required)
            - Use after loading or creating a network to verify success
            - Check has_loadflow_results field to see if loadflow has been executed

        Related Tools:
            - list_networks(): See all available networks
            - switch_network(): Change the active network
            - visualize_network(): Generate visual representation
        """
        try:
            proxy, network_id, _ = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Getting network info for network '{network_id}'")

        try:
            summary = proxy._get_network_summary(network_id)
            return json.dumps(summary, indent=2)

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to get network info: {e}")
            return f"Failed to get network info: {e!s}"

    async def modify_network(
        self,
        element_type: str,
        element_id: str,
        parameter: str,
        value: float,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Modify parameters of network elements (generators, loads, lines).

        Allows dynamic adjustment of network parameters for scenario analysis, optimization,
        or corrective actions. Commonly used to test different operating conditions or
        fix violations.

        **Important**: Modifying the network clears cached loadflow results. Run run_loadflow()
        again after modifications to compute updated power flows.

        Args:
            element_type (str): Type of element to modify. Valid options:
                - "generator": Generation units
                - "load": Load/demand points
                - "line": Transmission lines
            element_id (str): Unique ID of the specific element to modify. Use get_network_info()
                or query network data to find valid element IDs.
            parameter (str): Parameter to modify. Valid options depend on element_type:

                For "generator":
                    - "target_p": Active power setpoint (MW)
                    - "target_v": Voltage setpoint (p.u.)

                For "load":
                    - "p0": Active power demand (MW)
                    - "q0": Reactive power demand (MVAr)

                For "line":
                    - "r": Resistance (Ω)
                    - "x": Reactance (Ω)

            value (float): New value for the parameter (in units specified above)
            network_id (str, optional): Network to modify. If None, uses current network. Default: None.

        Returns:
            str: Confirmation message with modification details or error message.

        Example Usage:
            # Increase generator output
            modify_network("generator", "GEN_1", "target_p", 150.0)
            → "Updated generator 'GEN_1' target_p to 150.0 MW in network 'ieee_14'"

            # Adjust voltage setpoint
            modify_network("generator", "GEN_2", "target_v", 1.02)
            → "Updated generator 'GEN_2' target_v to 1.02 p.u. in network 'ieee_14'"

            # Change load demand
            modify_network("load", "LOAD_5", "p0", 75.5)
            → "Updated load 'LOAD_5' p0 to 75.5 MW in network 'ieee_14'"

            # Modify line impedance
            modify_network("line", "LINE_2_3", "x", 0.15)
            → "Updated line 'LINE_2_3' reactance to 0.15 Ω in network 'ieee_14'"

        Common Modifications:

            **Generator Adjustments**:
            - Increase/decrease generation (target_p) for economic dispatch
            - Adjust voltage control (target_v) to manage reactive power
            - Typical ranges: target_p (0 to Pmax), target_v (0.95-1.05 p.u.)

            **Load Variations**:
            - Model demand changes (p0) for different scenarios
            - Adjust power factor (q0/p0 ratio) for load characteristics
            - Negative values for generation-like loads (not typical)

            **Line Parameters**:
            - Modify impedance (r, x) for sensitivity analysis
            - Model line upgrades or aging
            - Changes affect power flow distribution

        Important Notes:
            - **Loadflow results are cleared** after modification
            - Must re-run loadflow to see effects of changes
            - Changes are in-memory only (use export_network to save)
            - Element IDs must exist in the network
            - Values should be physically realistic
            - Some parameters have implicit bounds

        Workflow for "What-If" Analysis:
            1. load or create network
            2. run_loadflow() → Get baseline results
            3. modify_network() → Change parameters
            4. run_loadflow() → See impact of changes
            5. check_voltage_violations() or run_security_analysis()
            6. Repeat steps 3-5 as needed
            7. export_network() to save interesting scenarios

        Parameter Units and Ranges:
            - Active power (P): MW (megawatts)
            - Reactive power (Q): MVAr (megavolt-ampere reactive)
            - Voltage (V): p.u. (per-unit, typically 0.9-1.1)
            - Resistance (R): Ω (ohms)
            - Reactance (X): Ω (ohms)

        Validation:
            - Element existence is checked
            - Parameter name is validated
            - Value ranges are not enforced (user responsibility)
            - Physical feasibility checked during loadflow

        Related Tools:
            - run_loadflow(): Required after modifications to compute new state
            - check_voltage_violations(): Verify modifications fixed violations
            - get_network_info(): Find element IDs and current values
            - export_network(): Save modified network
            - get_online_resource(class_object='network'): Look up the underlying
              pypowsybl network API (methods, signatures, parameters) instead of
              relying on prior knowledge

        Use Cases:
            - Sensitivity analysis (how changes affect power flows)
            - Scenario planning (peak load, generator outage)
            - Optimization studies (find best parameter values)
            - Corrective actions (fix violations)
            - Training and education (show cause-and-effect)
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Modifying network element parameter for network {network_id}")

        try:
            proxy.invalidate_loadflow(network_id)

            spec = MODIFY_NETWORK_SPEC.get(element_type)
            if spec is None:
                error = f"Unsupported element type '{element_type}'"
                logger.warning(error)
                return error

            elements = getattr(network, spec["getter"])()
            if element_id not in elements.index:
                error = (
                    f"{spec['label']} '{element_id}' not found "
                    f"in network '{network_id}'"
                )
                logger.warning(error)
                return error

            param_spec = spec["parameters"].get(parameter)
            if param_spec is None:
                error = f"Unsupported parameter '{parameter}' for {element_type}"
                logger.warning(error)
                return error

            update_kwarg, display_name, unit = param_spec
            getattr(network, spec["updater"])(
                id=[element_id], **{update_kwarg: [value]}
            )

            info = (
                f"Updated {element_type} '{element_id}' {display_name} "
                f"to {value} {unit} in network '{network_id}'"
            )
            logger.success(info)
            return info

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to modify network: {e}")
            return f"Failed to modify network: {e!s}"

    async def set_line_status(
        self,
        line_id: str,
        active: bool,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Activate or deactivate a transmission line in the network.

        This tool allows you to change the operational status of a transmission line,
        simulating line outages (disconnection) or bringing lines back into service.
        This is commonly used for contingency analysis, maintenance scheduling simulation,
        or testing system resilience.

        **Important**: Deactivating a line clears cached loadflow results. Run run_loadflow()
        again after modification to compute updated power flows with the line out of service.

        Args:
            line_id (str): Unique ID of the transmission line to activate/deactivate.
                Use get_network_info() or get_network_element_data(element_type="line",
                get_only_ids=True) to find valid line IDs.
            active (bool): Status to set for the line:
                - True: Activate the line (bring into service)
                - False: Deactivate the line (take out of service/disconnect)
            network_id (str, optional): Network to modify. If None, uses current network. Default: None.

        Returns:
            str: Confirmation message with line status change details or error message.

        Example Usage:
            # Deactivate a line (simulate outage)
            set_line_status("LINE_1_2", False)
            → "Line 'LINE_1_2' deactivated in network 'ieee_14'"

            # Reactivate a line (bring back into service)
            set_line_status("LINE_1_2", True)
            → "Line 'LINE_1_2' activated in network 'ieee_14'"

            # Deactivate line in specific network
            set_line_status("LINE_2_3", False, "ieee_30")
            → "Line 'LINE_2_3' deactivated in network 'ieee_30'"

        Common Use Cases:

            **Contingency Analysis**:
            - Simulate N-1 contingencies (single line outages)
            - Test system stability under line failures
            - Identify critical lines whose loss causes violations

            **Maintenance Planning**:
            - Simulate scheduled line maintenance
            - Verify system can operate with line out of service
            - Plan switching sequences for maintenance work

            **System Testing**:
            - Test protective relay coordination
            - Validate backup paths and redundancy
            - Assess cascading failure risks

            **Scenario Analysis**:
            - Compare system performance with/without specific lines
            - Evaluate new line additions (activate previously inactive lines)
            - Test emergency operating procedures

        Important Notes:
            - **Loadflow results are cleared** after line status change
            - Must re-run loadflow to see effects of line disconnection
            - Changes are in-memory only (use export_network to save)
            - Line ID must exist in the network
            - Deactivating critical lines may cause loadflow convergence issues
            - Deactivating a line may create islanded buses or sections

        Workflow for Contingency Analysis:
            1. load or create network
            2. run_loadflow() → Get baseline (N-0) results
            3. set_line_status(line_id, False) → Simulate outage
            4. run_loadflow() → Compute N-1 power flows
            5. check_voltage_violations() or run_security_analysis()
            6. set_line_status(line_id, True) → Restore line
            7. Repeat for other lines to test all N-1 contingencies

        Effects of Line Deactivation:
            - Power flow redistributes through other paths
            - Line loadings on parallel paths increase
            - Voltages may change at connected buses
            - System losses typically increase
            - May cause voltage or thermal violations
            - Could result in non-convergent loadflow if system splits

        Validation:
            - Line existence is checked before modification
            - Status change is applied immediately
            - No validation of system connectivity (user responsibility)
            - Loadflow will reveal if deactivation causes problems

        Related Tools:
            - run_loadflow(): Required after status change to compute new state
            - run_security_analysis(): Automated N-1 contingency testing
            - check_voltage_violations(): Verify line outage doesn't cause violations
            - get_network_info(): Find line IDs and current status
            - get_network_element_data(): Check detailed line information
            - export_network(): Save network with modified line status

        Use Cases:
            - N-1 contingency analysis (single element outages)
            - Maintenance outage planning and scheduling
            - System resilience and reliability testing
            - Emergency operating procedure validation
            - Grid expansion planning (test with/without new lines)
            - Training and education (demonstrate system response to outages)
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(
            f"Setting line '{line_id}' status to {active} in network {network_id}"
        )

        try:
            # Clear loadflow results since network topology is changing
            proxy.invalidate_loadflow(network_id)

            # Get the line and check if it exists
            lines = network.get_lines()
            if line_id not in lines.index:
                error = f"Line '{line_id}' not found in network '{network_id}'"
                logger.warning(error)
                return error

            # Update the line status using update_lines method
            network.update_lines(id=line_id, connected1=active, connected2=active)

            status_text = "activated" if active else "deactivated"
            info = f"Line '{line_id}' {status_text} in network '{network_id}'"
            logger.success(info)
            return info

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to set line status: {e}")
            return f"Failed to set line status: {e!s}"

    async def set_switch_status(
        self,
        switch_id: str,
        open: bool,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Open or close a switch in the network (breaker, disconnector, load-break switch).

        In a substation, switches control how busbars and equipment are connected.
        A closed switch (open=False) ties its two nodes together; an open switch
        (open=True) isolates them. This is different from set_line_status, which
        acts on transmission lines and transformers, not on internal substation
        switching equipment.

        Typical use cases: busbar coupling maneuvers, opening a disconnector for
        maintenance isolation, or testing how the network behaves after a
        switching sequence. Any change to switch status alters the topology, so
        cached loadflow results are cleared automatically — run_loadflow() must
        be called again to get updated power flows.

        Args:
            switch_id (str): ID of the switch to modify. Use
                get_network_element_data(element_type="switch", get_only_ids=True)
                or get_network_element_data(element_type="switch") to list
                available switches and check their current open status.
            open (bool): Target state — True to open (isolate), False to close
                (connect).
            network_id (str, optional): Network to modify. Defaults to the
                current network of the session.

        Returns:
            str: Confirmation message with switch status change details or
                error message.

        Example Usage:
            # Close a coupling breaker between two busbars
            set_switch_status("EGUZOP4EGUZO4COUPL DJOC", False)
            → "Switch 'EGUZOP4EGUZO4COUPL DJOC' closed in network 'my_network'"

            # Open a disconnector to isolate equipment
            set_switch_status("DISC_1", True)
            → "Switch 'DISC_1' opened in network 'ieee_14'"

        Important Notes:
            - Loadflow results are cleared after any switch status change
            - Must re-run loadflow to see topological effects on power flows
            - Changes are in-memory only (use export_network to persist)
            - Switch ID must exist in the network
            - For lines and transformers, use set_line_status instead

        Workflow for a switching maneuver:
            1. load or create network
            2. run_loadflow() → baseline (N) state
            3. set_switch_status(switch_id, open=...) → apply the maneuver
            4. run_loadflow() → post-switching power flows
            5. check_voltage_violations() or compare element loadings

        Related Tools:
            - run_loadflow(): required after status change to compute new state
            - set_line_status(): connect or disconnect lines and transformers
            - get_network_element_data(): inspect switch kind and current status
            - export_network(): save the network with modified switch states
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(
            f"Setting switch '{switch_id}' open={open} in network {network_id}"
        )

        # Topology changed: previously computed loadflow results are no longer valid.
        proxy.invalidate_loadflow(network_id)

        try:
            # Check that the switch exists before attempting to modify it.
            switches = network.get_switches()
            if switch_id not in switches.index:
                error = f"Switch '{switch_id}' not found in network '{network_id}'"
                logger.warning(error)
                return error

            network.update_switches(id=switch_id, open=open)

            status_text = "opened" if open else "closed"
            info = f"Switch '{switch_id}' {status_text} in network '{network_id}'"
            logger.success(info)
            return info

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to set switch status: {e}")
            return f"Failed to set switch status: {e!s}"

    async def set_tap_position(
        self,
        transformer_id: str,
        tap_position: int,
        tap_changer_type: str = "ratio",
        side: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Change the tap position of a transformer's tap changer.

        Transformers can carry a ratio tap changer (adjusts the voltage ratio,
        used for voltage control) and/or a phase tap changer (adjusts the phase
        shift, used to control active power flow). This tool moves the selected
        tap changer to a new position. Inspect the current position and the
        allowed range first with get_network_element_data(element_type=
        "two_windings_transformer" or "three_windings_transformer"), which now
        returns tap_position, tap_min, tap_max and regulated_side.

        **Important**: Changing a tap position clears cached loadflow results.
        Run run_loadflow() again to compute the updated power flows.

        Args:
            transformer_id (str): ID of the transformer to modify. Use
                get_network_element_data() (optionally with get_only_ids=True) to
                find valid transformer IDs.
            tap_position (int): New tap position. Must be within [tap_min,
                tap_max] of the selected tap changer.
            tap_changer_type (str, optional): Which tap changer to move,
                "ratio" (default) or "phase".
            side (str, optional): For three-winding transformers only, the leg
                that carries the tap changer: "ONE", "TWO" or "THREE". Ignored
                for two-winding transformers.
            network_id (str, optional): Network to modify. If None, uses current
                network. Default: None.

        Returns:
            str: Confirmation message with the change details or error message.

        Example Usage:
            # Move a two-winding transformer ratio tap changer to position 2
            set_tap_position("TWT", 2)
            → "Updated ratio tap position of transformer 'TWT' to 2 in network 'net'"

            # Move a phase tap changer
            set_tap_position("TWT", 10, tap_changer_type="phase")

            # Move the tap changer on leg TWO of a three-winding transformer
            set_tap_position("3WT_1", 17, side="TWO")

        Important Notes:
            - Loadflow results are cleared after the change
            - Must re-run loadflow to see the effect on power flows
            - Changes are in-memory only (use export_network to persist)
            - tap_position must be within [tap_min, tap_max]

        Related Tools:
            - get_network_element_data(): read current tap position and range
            - run_loadflow(): required after the change to compute new state
            - modify_network(): change generator/load/line parameters
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(
            f"Setting {tap_changer_type} tap position of '{transformer_id}' "
            f"to {tap_position} in network {network_id}"
        )

        kind = (tap_changer_type or "ratio").strip().lower()
        if kind not in ("ratio", "phase"):
            error = (
                f"Unsupported tap_changer_type '{tap_changer_type}'. "
                "Use 'ratio' or 'phase'."
            )
            logger.warning(error)
            return error

        try:
            if kind == "ratio":
                tap_changers = network.get_ratio_tap_changers()
                update = network.update_ratio_tap_changers
            else:
                tap_changers = network.get_phase_tap_changers()
                update = network.update_phase_tap_changers

            if transformer_id not in tap_changers.index:
                error = (
                    f"Transformer '{transformer_id}' has no {kind} tap changer "
                    f"in network '{network_id}'"
                )
                logger.warning(error)
                return error

            rows = tap_changers.loc[[transformer_id]]

            # Three-winding transformers expose one tap changer per leg, told
            # apart by the 'side' column, so a side is required to pick one.
            is_three_windings = (
                "side" in rows.columns
                and rows["side"].astype(str).str.len().gt(0).any()
            )
            if is_three_windings:
                if side is None:
                    error = (
                        f"Transformer '{transformer_id}' is a three-winding "
                        "transformer; 'side' (ONE, TWO or THREE) is required"
                    )
                    logger.warning(error)
                    return error
                side_normalized = str(side).strip().upper()
                row = rows[rows["side"] == side_normalized]
                if row.empty:
                    error = (
                        f"Transformer '{transformer_id}' has no {kind} tap "
                        f"changer on side '{side_normalized}'"
                    )
                    logger.warning(error)
                    return error
            else:
                row = rows

            low_tap = int(row["low_tap"].iloc[0])
            high_tap = int(row["high_tap"].iloc[0])
            if not low_tap <= tap_position <= high_tap:
                error = (
                    f"Tap position {tap_position} out of range "
                    f"[{low_tap}, {high_tap}] for transformer '{transformer_id}'"
                )
                logger.warning(error)
                return error

            # Changing the topology/state invalidates any cached loadflow.
            proxy.invalidate_loadflow(network_id)

            if is_three_windings:
                # A three-winding tap changer is addressed by (id, side); pass a
                # matching indexed DataFrame because 'side' is not modifiable.
                update_df = pd.DataFrame(
                    {"tap": [int(tap_position)]},
                    index=pd.MultiIndex.from_tuples(
                        [(transformer_id, side_normalized)], names=["id", "side"]
                    ),
                )
                update(update_df)
                info = (
                    f"Updated {kind} tap position of transformer "
                    f"'{transformer_id}' side '{side_normalized}' to "
                    f"{tap_position} in network '{network_id}'"
                )
            else:
                update(id=transformer_id, tap=int(tap_position))
                info = (
                    f"Updated {kind} tap position of transformer "
                    f"'{transformer_id}' to {tap_position} in network "
                    f"'{network_id}'"
                )

            logger.success(info)
            return info

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to set tap position: {e}")
            return f"Failed to set tap position: {e!s}"

    async def remove_network(
        self, network_id: str, ctx: Context[ServerSession, None] = None
    ) -> str:
        """
        Remove a network from server memory and free resources.

        Deletes a network and all associated data (loadflow results, cached analyses) from
        the server. Useful for memory management when working with multiple networks.

        Args:
            network_id (str): ID of the network to remove. Must be an existing network.

        Returns:
            str: Confirmation message listing remaining networks or error message.

        Example Usage:
            remove_network("old_scenario")
            → "Removed network 'old_scenario'. Remaining networks: ['ieee_14', 'ieee_30']"

        Behavior:
            - Removes network from memory immediately
            - Clears associated loadflow results
            - Clears associated security analysis results
            - If removed network was current, automatically switches to another network
            - If no networks remain, current network is set to None

        Auto-Switch Logic:
            If you remove the current network and others exist:
            - Server automatically switches to the first available network
            - You'll be notified which network is now current
            - No interruption to workflow

        Notes:
            - Operation cannot be undone
            - Network data is only removed from memory, not from disk
            - Original files (if loaded from file) are not affected
            - Use export_network() before removing if you want to save changes
            - Good practice for long-running sessions with many networks

        Memory Management:
            - Large networks can consume significant memory
            - Remove unused networks to free resources
            - Particularly important when working with many networks sequentially
            - Server performance improves with fewer loaded networks

        Safety:
            - Cannot remove a network that doesn't exist (returns error)
            - Safe to remove current network (auto-switches)
            - Removes all traces (no orphaned data)

        Related Tools:
            - list_networks(): See what networks are loaded before removing
            - export_network(): Save network before removing
            - switch_network(): Change current network before removing

        Use Cases:
            - Clean up after scenario analysis
            - Free memory for new networks
            - Remove test networks after experiments
            - Manage server resources in long sessions
            - Prepare for loading large networks

        Workflow Example:
            # Work with multiple scenarios
            create_ieee_network("IEEE14", "scenario_1")
            run_loadflow()
            export_network("scenario_1", "results_1.xiidm")

            # Clean up and move to next scenario
            remove_network("scenario_1")
            create_ieee_network("IEEE30", "scenario_2")
        """
        proxy = self.get_proxy(get_session_id(ctx))

        logger.debug(f"Removing network '{network_id}'")
        if network_id not in proxy.networks:
            return f"Network '{network_id}' not found"

        try:
            del proxy.networks[network_id]

            proxy.invalidate_loadflow(network_id)

            if proxy.current_network_id == network_id:
                if proxy.networks:
                    new_current = next(iter(proxy.networks.keys()))
                    proxy.current_network_id = new_current
                    proxy.current_network = proxy.networks[new_current]
                else:
                    proxy.current_network_id = None
                    proxy.current_network = None

            remaining = list(proxy.networks.keys())
            return f"Removed network '{network_id}'. Remaining networks: {remaining}"

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to remove network: {e}")
            return f"Failed to remove network: {e!s}"

    async def clone_variant(
        self,
        variant_id: str,
        base_variant_id: str = "InitialState",
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Create a new network variant by cloning an existing one.

        Variants allow you to maintain multiple versions of the same network in memory,
        each with different modifications, while keeping the original state intact.
        After creation, a network has only one variant called 'InitialState'.

        Args:
            variant_id (str): Unique identifier for the new variant.
            base_variant_id (str, optional): ID of the variant to clone from. Default: "InitialState".
            network_id (str, optional): ID of the network. If None, uses current network.

        Returns:
            str: Success message or error message.

        Example:
            clone_variant("peak_load_scenario", "InitialState")
            → "Variant 'peak_load_scenario' created from 'InitialState' in network 'ieee14'"
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id
        if network_id is None:
            return "No network currently loaded."

        try:
            network = proxy.networks[network_id]
            network.clone_variant(base_variant_id, variant_id)
            logger.info(
                f"Cloned variant '{variant_id}' from '{base_variant_id}' in network '{network_id}'"
            )
            return f"Variant '{variant_id}' created from '{base_variant_id}' in network '{network_id}'"
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to clone variant: {e}")
            return f"Failed to clone variant: {e!s}"

    async def set_working_variant(
        self,
        variant_id: str,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Switch the active working variant of a network.

        Subsequent modifications and analyses will be performed on the selected variant.

        Args:
            variant_id (str): ID of the variant to switch to.
            network_id (str, optional): ID of the network. If None, uses current network.

        Returns:
            str: Success message or error message.

        Example:
            set_working_variant("Variant")
            → "Switched to variant 'Variant' in network 'ieee14'"
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id
        if network_id is None:
            return "No network currently loaded."

        try:
            network = proxy.networks[network_id]
            network.set_working_variant(variant_id)
            # Clear cached loadflow results when switching variants as they might not apply
            proxy.invalidate_loadflow(network_id)

            logger.info(f"Switched to variant '{variant_id}' in network '{network_id}'")
            return f"Switched to variant '{variant_id}' in network '{network_id}'"
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to set working variant: {e}")
            return f"Failed to set working variant: {e!s}"

    async def get_working_variant(
        self,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Get the ID of the current working variant of a network.

        Args:
            network_id (str, optional): ID of the network. If None, uses current network.

        Returns:
            str: The ID of the current working variant.
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id
        if network_id is None:
            return "No network currently loaded."

        try:
            network = proxy.networks[network_id]
            variant_id = network.get_working_variant_id()
            return variant_id
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to get working variant: {e}")
            return f"Failed to get working variant: {e!s}"

    async def list_variants(
        self,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        List all available variants for a network.

        Args:
            network_id (str, optional): ID of the network. If None, uses current network.

        Returns:
            str: JSON-formatted list of variant IDs.
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id
        if network_id is None:
            return "No network currently loaded."

        try:
            network = proxy.networks[network_id]
            variants = network.get_variant_ids()
            current = network.get_working_variant_id()
            result = [{"id": v, "is_working": v == current} for v in variants]
            return json.dumps(result, indent=2)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to list variants: {e}")
            return f"Failed to list variants: {e!s}"

    async def remove_variant(
        self,
        variant_id: str,
        network_id: str | None = None,
        fallback_variant_id: str | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Remove a variant from a network.

        The 'InitialState' variant cannot be removed. If the working variant is removed,
        the network automatically switches back to 'InitialState'.

        Args:
            variant_id (str): ID of the variant to remove.
            network_id (str, optional): ID of the network. If None, uses current network.

        Returns:
            str: Success message or error message.
        """
        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)
        if network_id is None:
            network_id = proxy.current_network_id
        if network_id is None:
            return "No network currently loaded."

        if variant_id == "InitialState":
            return "The 'InitialState' variant cannot be removed."
        if fallback_variant_id is None:
            fallback_variant_id = "InitialState"

        try:
            network = proxy.networks[network_id]
            network.remove_variant(variant_id)
            network.set_working_variant(fallback_variant_id)
            logger.info(
                f"Removed variant '{variant_id}' from network '{network_id}'. Setting working variant to '{fallback_variant_id}'."
            )
            return f"Variant '{variant_id}' removed from network '{network_id}'. Setting working variant to '{fallback_variant_id}'."
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to remove variant: {e}")
            return f"Failed to remove variant: {e!s}"

    async def check_voltage_violations(
        self,
        network_id: str | None = None,
        min_voltage: float = 0.95,
        max_voltage: float = 1.05,
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Check for bus voltage violations against specified limits.

        Identifies all buses where voltage magnitude falls outside acceptable operating
        range. This is a critical check for power system security and equipment protection.
        Automatically runs loadflow if needed.

        Args:
            network_id (str, optional): Network to check. If None, uses current network. Default: None.
            min_voltage (float, optional): Minimum acceptable voltage in per-unit (p.u.).
                Typical values: 0.95-0.90. Default: 0.95 p.u.
            max_voltage (float, optional): Maximum acceptable voltage in per-unit (p.u.).
                Typical values: 1.05-1.10. Default: 1.05 p.u.
            limit (int, optional): Max violations per page. None = all violations.
            cursor (str | int, optional): Page offset (default 0). See pagination.nextCursor.

        Returns:
            str: JSON formatted string containing:
                - success (bool): Whether check completed successfully
                - network_id (str): Network identifier
                - loadflow_executed (bool): Whether loadflow was run automatically
                - voltage_limits (dict): Min and max voltage limits used
                - total_buses (int): Total number of buses checked
                - violation_count (int): Number of buses with violations
                - violations (list): Detailed violation information (paginated if limit set)
                - pagination (dict, optional): limit, cursor, total, nextCursor
                - error (str): Error message (if failed)

        Example Output:
            {
              "success": true,
              "network_id": "ieee_30",
              "loadflow_executed": false,
              "voltage_limits": {
                "min": 0.95,
                "max": 1.05
              },
              "total_buses": 30,
              "violation_count": 3,
              "violations": [
                {
                  "bus_name": "VL_5",
                  "voltage": 0.932,
                  "violation_type": "LOW_VOLTAGE"
                },
                {
                  "bus_name": "VL_8",
                  "voltage": 1.067,
                  "violation_type": "HIGH_VOLTAGE"
                }
              ]
            }
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            return json.dumps({"success": False, "error": str(e)}, indent=2)

        logger.debug(f"Checking voltage violations for network '{network_id}'")

        try:
            loadflow_executed = False

            # Check if loadflow needs to be run
            if network_id not in proxy.loadflow_results:
                loadflow_executed = True
                results = pp.loadflow.run_ac(network)
                all_converged = all(
                    result.status.name == "CONVERGED" for result in results
                )

                if not all_converged:
                    failed_components = [
                        {
                            "component_num": result.connected_component_num,
                            "status": result.status.name,
                        }
                        for result in results
                        if result.status.name != "CONVERGED"
                    ]
                    return json.dumps(
                        {
                            "success": False,
                            "network_id": network_id,
                            "loadflow_executed": True,
                            "loadflow_converged": False,
                            "failed_components": failed_components,
                            "error": "Load flow failed to converge",
                        },
                        indent=2,
                    )

                proxy.loadflow_results[network_id] = {
                    "converged": True,
                    "timestamp": datetime.now(UTC).isoformat(),
                }

            # Get buses and check for violations
            buses = network.get_buses()
            violations = []

            for _, bus in buses.iterrows():
                v_mag = float(bus["v_mag"])
                if v_mag < min_voltage or v_mag > max_voltage:
                    violation_type = (
                        "LOW_VOLTAGE" if v_mag < min_voltage else "HIGH_VOLTAGE"
                    )
                    violations.append(
                        {
                            "bus_name": bus["name"],
                            "voltage": round(v_mag, 3),
                            "violation_type": violation_type,
                            "deviation": round(
                                abs(
                                    v_mag
                                    - (
                                        min_voltage
                                        if v_mag < min_voltage
                                        else max_voltage
                                    )
                                ),
                                3,
                            ),
                        }
                    )

            try:
                violations_out, pagination = paginate(
                    violations, limit=limit, cursor=cursor
                )
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, indent=2)

            result = attach_pagination(
                {
                    "success": True,
                    "network_id": network_id,
                    "loadflow_executed": loadflow_executed,
                    "voltage_limits": {"min": min_voltage, "max": max_voltage},
                    "total_buses": len(buses),
                    "violation_count": len(violations),
                    "violations": violations_out,
                },
                pagination,
            )

            logger.info(
                f"Voltage check completed for network '{network_id}': {len(violations)} violations found"
            )
            return json.dumps(result, indent=2)

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to check voltage violations: {e}")
            return json.dumps(
                {"success": False, "network_id": network_id, "error": str(e)}, indent=2
            )

    async def get_network_element_data(
        self,
        network_id: str | None = None,
        variant_id="InitialState",
        element_type: str | None = None,
        compare_with_variant_id: str | None = None,
        mode: str | None = None,
        metric: str | None = None,
        filter_op: str | None = None,
        filter_value: float | str | None = None,
        sort: str = "desc",
        limit_kind: str = "permanent",
        get_only_ids: bool = False,
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Retrieve detailed information about specific network elements as JSON.

        Gets the complete data for a specific element type (substations, generators, lines, etc.)
        from a power system network or variant of the network. Returns all attributes and parameters for the elements
        in JSON format, suitable for detailed analysis or data export.

        Args:
            network_id (str, optional): Network to query. If None, uses current network. Default: None.
            variant_id (str, optional): Variant to query. If None, uses default variant_id. Default: 'InitialState'.
            element_type (str): Type of elements to retrieve. Supported types:
                - "voltage_level": Voltage level information
                - "substation": Substation information
                - "bus": Bus/node data with voltages
                - "generator": Generation units with power output
                - "load": Load points with consumption
                - "line": Transmission lines with ratings
                - "two_windings_transformer": Two-winding transformers
                  ("transformer" on its own always means this one)
                - "three_windings_transformer": Three-winding transformers
                - "hvdc_line": HVDC transmission lines
                - "shunt_compensator": Shunt compensation devices
                - "static_var_compensator": Static var compensators
                - "vsc_converter_station": VSC converter stations
                - "lcc_converter_station": LCC converter stations
                - "switch": Switching devices
                Any other pypowsybl element table is accepted too, named after
                its pypowsybl ElementType lowercased (e.g. "battery",
                "tie_line", "busbar_section"). Names are canonical and singular:
                no abbreviations or plural variants. An invalid name comes back
                with the full list and a suggestion.
            compare_with_variant_id (str, optional): Variant to compare with. If None, do not do comparison. Default: None.
            mode (str, optional): Use "filter" to keep only the elements that match a
                condition. Omit it (or use "list") to get the full element list. Default: None.
            metric (str, optional): Column name or derived metric to test. Derived metrics
                are "loading_percent", "p_abs" and "q_abs". Required when mode="filter".
            filter_value (float, optional): Threshold for the comparison. You choose
                this value; 90 or 100 are common examples for loading_percent but
                nothing is hard-coded.
            sort (str, optional): Order of the filtered rows: "desc" (default) or "asc".
            limit_kind (str, optional): For lines and 2-winding transformers only,
                when metric is loading_percent: which ampacity rating from
                get_operational_limits() to use as the denominator.

                pypowsybl stores both ratings in the same table (type CURRENT,
                value in amperes). They are distinguished by acceptable_duration:
                  - "permanent" (default): duration -1, the normal continuous limit.
                    Typical use: check loading in everyday N operation.
                  - "temporary": duration > 0, a higher limit allowed for a short
                    time after an outage. Typical use: check loading against the
                    N-1 rating when your network already reflects post-contingency
                    flows (from a prior loadflow or variant).

                permanent and temporary are often different values, but not always;
                if a line has no temporary row, we fall back to the permanent one.

                Important: this only selects which limit to divide by. It does not
                run a contingency study and does not change i1/i2 — currents always
                come from the network state you already loaded. Ignored for generators,
                loads, and other element types.
            limit (int, optional): How many elements per page. In list mode, None
                returns everything. In filter mode there is always a page: if you
                leave limit out, we fall back to the default page size so a wide
                filter cannot return a huge answer. matched_count always tells you
                how many elements matched; elements only holds the current page.
                Ask for a bigger limit, or move on with cursor, to see more.
            cursor (str | int, optional): Page offset. Use pagination.nextCursor for the next page.
            get_only_ids (bool, optional): When True, return only the element IDs
                for element_type instead of their full data — faster and cheaper
                for enumeration, validation or feeding IDs to other tools. The IDs
                reflect variant_id. The mode/metric/filter and compare_with_variant_id
                arguments are ignored in this mode. Default: False.

        Returns:
            str: JSON-formatted string. With get_only_ids=False (default): the
                complete element data with all attributes; if comparison is requested,
                only elements that differ between the two variants. With
                get_only_ids=True: a JSON array of IDs when limit is None, otherwise
                an object with element_ids + pagination.
                Returns error message if network not found or invalid element type.

        Example Usage:
            # Get all substations
            substations = get_network_element_data("ieee_14", "substation")

            # Get all generators
            generators = get_network_element_data("ieee_14", "generator")

            # Get lines from current network
            lines = get_network_element_data(None, "line")

            # Get only the generator IDs (replaces get_network_elements_ids)
            gen_ids = get_network_element_data("ieee_14", "generator", get_only_ids=True)
            → ["B1-G", "B2-G", ...]

        Use Cases:
            - compare variants from same network

        Returns Data Including:
            - Element IDs and names
            - Electrical parameters (voltage, power, impedance, etc.)
            - Connection topology
            - Operating limits
            - Status information
            - Geographic location (if available)

        Notes:
            - Returns all available attributes for each element type
            - Data structure varies by element type
            - Pass get_only_ids=True for just the IDs (no other attributes)
            - Data reflects current state (including loadflow results if run)
            - loading_percent on lines/transformers: max(|i1|, |i2|) in amperes,
              divided by the current limit picked via limit_kind (see above).
              get_lines() does not include that limit; we read it from
              get_operational_limits() and attach it as current_limit1 before
              filtering. Generators use |p| / max_p instead.

        Related Tools:
            - get_network_info(): Get network statistics summary
            - modify_network(): Modify element parameters
            - get_online_resource(class_object='network'): Look up the underlying
              pypowsybl network API (methods, signatures, parameters) instead of
              relying on prior knowledge
        """
        try:
            _, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Getting {element_type} data for network '{network_id}'")

        if element_type is None:
            msg = "Element type is required"
            logger.warning(msg)
            return msg

        try:
            if variant_id not in network.get_variant_ids():
                msg = f"Variant '{variant_id}' not found in network '{network_id}'"
                logger.warning(msg)
                return msg

            network.set_working_variant(variant_id)

            # Map element types to network methods
            element_methods = ELEMENT_TYPE_TO_GETTER

            if element_type not in element_methods:
                msg = (
                    f"Invalid element type '{element_type}'. "
                    f"{element_type_hint(element_type, element_methods)}"
                )
                logger.warning(msg)
                return msg

            method_name = element_methods[element_type]
            if not hasattr(network, method_name):
                msg = f"Method '{method_name}' not available for this network"
                logger.warning(msg)
                return msg

            method = getattr(network, method_name)

            # get_only_ids: return just the element IDs (the former
            # get_network_elements_ids tool), reflecting variant_id set above.
            # Uses the native get_elements_ids() enum fast path where available,
            # otherwise the getter's index. Skips enrichment, comparison and
            # filtering; the output shape matches the standalone tool exactly.
            if get_only_ids:
                fast_path_types = {
                    "generator",
                    "load",
                    "line",
                    "two_windings_transformer",
                }
                if element_type in fast_path_types:
                    element_ids = network.get_elements_ids(
                        element_type_enum(element_type)
                    )
                    logger.debug(
                        f"Retrieved {len(element_ids)} {element_type} IDs from "
                        f"network '{network_id}' using get_elements_ids()"
                    )
                else:
                    element_ids = method().index.tolist()
                    logger.debug(
                        f"Retrieved {len(element_ids)} {element_type} IDs from "
                        f"network '{network_id}' using {method_name}()"
                    )

                try:
                    ids_page, pagination = paginate(
                        element_ids, limit=limit, cursor=cursor
                    )
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, indent=2)

                if pagination is None:
                    return json.dumps(element_ids, indent=2)

                return json.dumps(
                    attach_pagination(
                        {
                            "network_id": network_id,
                            "element_type": element_type,
                            "element_ids": ids_page,
                        },
                        pagination,
                    ),
                    indent=2,
                )

            elements_df = method()

            # get_lines() returns i1/i2 (current in A) but not Imax. Imax is in
            # get_operational_limits(). We join it here so loading_percent works.
            if compare_with_variant_id is None and element_type in (
                "line",
                "two_windings_transformer",
            ):
                limits_df = None
                try:
                    limits_df = network.get_operational_limits()
                except (pp.PyPowsyblError, ValueError, KeyError) as e:
                    logger.debug(f"get_operational_limits failed: {e}")
                kind = (limit_kind or "permanent").strip().lower()
                elements_df = attach_current_limits(elements_df, limits_df, kind)

            # Transformer tables do not carry their tap-changer details. Join the
            # tap position, range and regulated side so the caller sees them in
            # one call (see set_tap_position() to change the position).
            if compare_with_variant_id is None and element_type in (
                "two_windings_transformer",
                "three_windings_transformer",
            ):
                ratio_df = None
                phase_df = None
                try:
                    ratio_df = network.get_ratio_tap_changers()
                except (pp.PyPowsyblError, ValueError, KeyError) as e:
                    logger.debug(f"get_ratio_tap_changers failed: {e}")
                try:
                    phase_df = network.get_phase_tap_changers()
                except (pp.PyPowsyblError, ValueError, KeyError) as e:
                    logger.debug(f"get_phase_tap_changers failed: {e}")
                elements_df = attach_tap_changer_data(
                    elements_df, ratio_df, phase_df, element_type
                )

            mode_normalized = (mode or "").strip().lower()

            if compare_with_variant_id is not None:
                if mode_normalized == "filter":
                    return json.dumps(
                        {
                            "success": False,
                            "error": "mode='filter' cannot be combined with compare_with_variant_id",
                        },
                        indent=2,
                    )
                if compare_with_variant_id not in network.get_variant_ids():
                    msg = f"Variant '{compare_with_variant_id}' not found in network '{network_id}'"
                    logger.warning(msg)
                    return msg
                network.set_working_variant(compare_with_variant_id)
                compare_df = method()
                elements_df = elements_df.compare(
                    compare_df, result_names=(variant_id, compare_with_variant_id)
                )

            total_elements = len(elements_df)
            logger.debug(
                f"Retrieved {total_elements} {element_type} from network '{network_id}'"
            )

            if mode_normalized == "filter":
                try:
                    filtered_df = apply_element_filter(
                        elements_df,
                        element_type=element_type,
                        metric=metric,
                        filter_op=filter_op,
                        filter_value=filter_value,
                        sort=sort,
                    )
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, indent=2)

                # Full number of matches, before we cut it into pages, so the
                # caller knows how many elements the filter found.
                matched_count = len(filtered_df)

                # The rows are already sorted, so a filter really means "give me
                # the most loaded ones". If no limit is asked for, we still stop at
                # the default page size: a wide filter such as loading_percent > 0
                # matches almost every line, and sending them all back would make a
                # far too big answer. You can always continue with cursor or ask
                # for a bigger limit.
                effective_limit = (
                    limit if limit is not None else DEFAULT_PAGINATION_LIMIT
                )

                try:
                    page_df, pagination = paginate(
                        filtered_df, limit=effective_limit, cursor=cursor
                    )
                except (ValueError, TypeError) as e:
                    return json.dumps({"success": False, "error": str(e)}, indent=2)

                payload = {
                    "success": True,
                    "network_id": network_id,
                    "variant_id": variant_id,
                    "element_type": element_type,
                    "metric": metric,
                    "filter_op": filter_op,
                    "filter_value": filter_value,
                    "sort": sort or "desc",
                    "limit_kind": (limit_kind or "permanent"),
                    "total_elements": total_elements,
                    "matched_count": matched_count,
                    "elements": json.loads(page_df.to_json(orient="index")),
                }
                payload = attach_pagination(payload, pagination)
                return json.dumps(payload, indent=2)

            if mode_normalized not in ("", "list"):
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Unsupported mode '{mode}'. Use 'filter' or omit mode.",
                    },
                    indent=2,
                )

            try:
                page_df, pagination = paginate(elements_df, limit=limit, cursor=cursor)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, indent=2)

            if pagination is None:
                return page_df.to_json(orient="index", indent=2)

            payload = attach_pagination(
                {
                    "network_id": network_id,
                    "variant_id": variant_id,
                    "element_type": element_type,
                    "elements": json.loads(page_df.to_json(orient="index")),
                },
                pagination,
            )
            return json.dumps(payload, indent=2)

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to get network element data: {e}")
            return f"Failed to get network element data: {e!s}"

    async def get_top_active_power_transit_lines(
        self,
        network_id: str | None = None,
        k: int = 10,
        flow_side: str = "from",
        ctx: Context[ServerSession, None] | None = None,
    ) -> str:
        """
        Returns the top K lines with the highest active power transit.

        Calculates the ranking based on the active power measured at the chosen side:
        - flow_side = "from" → p1 column (MW)
        - flow_side = "to"   → p2 column (MW)

        The sorting is performed on the absolute value |pX| to identify the highest
        transits, while returning the signed value for the chosen side.

        Args:
            network_id: Network identifier (default: current network).
            k: Number of elements to return (0 to 50).
            flow_side: Direction used for measurement ("from" => p1, "to" => p2).
            ctx: FastMCP context.

        Returns:
            str: JSON string containing a table with the following columns:
                 - id (line identifier)
                 - name (if available)
                 - from_bus_id, to_bus_id
                 - active_power_mw (signed value at the selected side)
                 - unit = "MW"
                 - flow_side (explicit description, e.g., "from (p1)")
        """
        try:
            _, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        try:
            # Normalize and cap K
            if k is None:
                k = 10
            try:
                k = int(k)
            except (ValueError, TypeError):
                k = 10
            k = max(k, 0)
            if k > 50:
                logger.debug(f"Parameter k={k} above 50, capping to 50")
                k = 50

            # Map direction -> column
            side = (flow_side or "from").strip().lower()
            col_map = {"from": "p1", "to": "p2"}
            if side not in col_map:
                logger.debug(
                    f"Unsupported flow_side='{flow_side}', defaulting to 'from'"
                )
                side = "from"
            p_col = col_map[side]

            # Retrieve lines; if p1/p2 are unavailable, attempt an AC loadflow
            lines = network.get_lines()
            need_lf = (p_col not in lines.columns) or lines[p_col].isna().all()
            if need_lf:
                try:
                    logger.debug("Running AC loadflow to populate p1/p2 columns")
                    pp.loadflow.run_ac(network)
                    lines = network.get_lines()  # refresh
                except (pp.PyPowsyblError, ValueError, KeyError) as e:
                    logger.warning(f"Loadflow run failed or not available: {e}")

            if p_col not in lines.columns:
                msg = (
                    f"Active power column '{p_col}' not available on lines; "
                    "ensure a loadflow has been run."
                )
                logger.warning(msg)
                return msg

            # Calculate absolute value score for sorting, keep signed value
            lines = lines.copy()
            # Ensure name and bus columns are present
            name_col = "name" if "name" in lines.columns else None
            if name_col is None:
                # Harmonize for easy concat later
                lines["name"] = None
                name_col = "name"
            for c in ("bus1_id", "bus2_id"):
                if c not in lines.columns:
                    lines[c] = None

            lines["score_abs_mw"] = lines[p_col].abs()
            lines_sorted = lines.sort_values(by="score_abs_mw", ascending=False)
            if k == 0:
                top = lines_sorted.iloc[0:0]
            else:
                top = lines_sorted.head(k)

            flow_side_desc = f"{side} ({p_col})"
            result = {
                "network_id": network_id,
                "k": int(k),
                "flow_side": flow_side_desc,
                "unit": "MW",
                "elements": [],
            }
            for lid, row in top.iterrows():
                result["elements"].append(
                    {
                        "id": lid,
                        "name": row.get(name_col, None),
                        "from_bus_id": row.get("bus1_id", None),
                        "to_bus_id": row.get("bus2_id", None),
                        "active_power_mw": float(row.get(p_col, 0.0))
                        if row.get(p_col) is not None
                        else 0.0,
                    }
                )

            return json.dumps(result, indent=2)

        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to compute top active power transit lines: {e}")
            return f"Failed to compute top active power transit lines: {e!s}"
