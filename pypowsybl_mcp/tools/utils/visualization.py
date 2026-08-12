#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context
from pypowsybl import network as pn

from pypowsybl_mcp.tools import PyPowsyblTool
from pypowsybl_mcp.utils import DOWNLOAD_BASE_URL, DOWNLOAD_LINK_EXPIRY_SECONDS
from pypowsybl_mcp.utils.download_utils import generate_download_link
from pypowsybl_mcp.utils.user_session_management import get_session_id


def register_visualization_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    tools = VisualizationTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


class VisualizationTools(PyPowsyblTool):
    async def plot_substation_single_line_diagram(
        self,
        network_id: str | None = None,
        substation_id: str | None = None,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Generate a detailed single-line diagram (SLD) for a specific substation.

        Creates a standard electrical single-line diagram showing the detailed topology
        of a substation, including all equipment, bus configurations, and connections.
        This is the detailed engineering view used for operations and planning.

        Args:
            network_id (str, optional): Network containing the substation. If None, uses
                current network. Default: None.
            substation_id (str): ID of the substation to diagram. Required. Use
                get_network_info() to find available substation IDs.

        Returns:
            str: URL to the SVG image:
                "http://localhost:9992/download/TOKEN/substation_diagram.svg"

        Example Usage:
            # First, get network info to find substation IDs
            info = get_network_info("ieee_14")
            # Then plot specific substation
            diagram = plot_substation_single_line_diagram("ieee_14", "S1")

        Diagram Shows:
            - Bus bars and bus sections
            - Circuit breakers and disconnectors
            - Transformers with tap positions
            - Generators and their connections
            - Loads and their connections
            - Line connections and bay arrangements
            - Equipment status (open/closed)
            - Current flow directions (if loadflow run)
            - Voltage levels and ratings

        Standard Symbols:
            - Uses IEEE/IEC standard electrical symbols
            - Color coding for voltage levels
            - Status indication for switching devices

        Notes:
            - Requires valid substation_id from the network
            - More detailed than network-wide visualization
            - Essential for understanding substation topology
            - Used by operators for switching procedures
            - Useful for protection and control engineering

        Performance:
            - Fast generation (<1s) even for complex substations
            - Image size typically 200-500KB

        Common Errors:
            - "Substation ID is required": Must provide substation_id parameter
            - "Substation not found": Invalid substation_id for this network

        Related Tools:
            - visualize_network(): Full network topology view
            - get_network_info(): Find available substation IDs

        Use Cases:
            - Detailed substation analysis
            - Switching procedure planning
            - Protection scheme documentation
            - Training and education
            - Engineering design validation
        """
        session_id = get_session_id(ctx)

        if network_id is None:
            network_id = self.get_proxy(session_id).current_network_id
        logger.debug(
            f"Generating single-line diagram for substation '{substation_id}' in network '{network_id}'"
        )

        if network_id is None:
            msg = "No network specified and no current network selected"
            logger.warning(msg)
            return msg

        if network_id not in self.get_proxy(session_id).networks:
            msg = f"Network '{network_id}' not found"
            logger.warning(msg)
            return msg

        if substation_id is None:
            msg = "Substation ID is required for a substation single-line diagram."
            logger.warning(msg)
            return msg

        try:
            params = pn.SldParameters(
                **self.get_proxy(session_id).visualization_config["sld"]
            )
            sld = (
                self.get_proxy(session_id)
                .networks[network_id]
                .get_single_line_diagram(substation_id, parameters=params)
            )
            svg_data = sld.svg.encode("utf-8")

            filename = f"substation_{substation_id}_diagram.svg"
            link_info = generate_download_link(
                filename=filename,
                file_data=svg_data,
                download_base_url=DOWNLOAD_BASE_URL,
                expiry_seconds=DOWNLOAD_LINK_EXPIRY_SECONDS,
            )

            logger.debug(
                f"Generated single-line diagram URL for substation '{substation_id}' in network '{network_id}': {link_info['download_url']}"
            )
            return link_info["download_url"]
        except Exception as e:  # noqa: BLE001
            msg = f"Error generating single-line diagram: {e}"
            logger.error(msg)
            return msg

    async def visualize_network(
        self, network_id: str | None = None, ctx: Context[ServerSession, None] = None
    ) -> str:
        """
        Generate a visual network diagram showing the topology and key components.

        Creates a graphical representation of the power system network including buses,
        generators, loads, transmission lines, and transformers. Returns a URL pointing
        to an SVG image that can be displayed in compatible interfaces.

        Args:
            network_id (str, optional): Network to visualize. If None, uses current network. Default: None.

        Returns:
            str: URL to the SVG image:
                "http://localhost:9992/download/TOKEN/network_visualization.svg"

        Example Usage:
            result = visualize_network("ieee_14")
            # Result can be displayed directly in web interfaces or saved to file

        Visualization Includes:
            - Network topology and connectivity
            - Buses/substations (nodes)
            - Generators (with capacity indicators)
            - Loads (with demand indicators)
            - Transmission lines and transformers
            - Voltage levels (color-coded if loadflow has been run)

        Notes:
            - Image size depends on network complexity
            - Color coding shows voltage magnitudes if loadflow results exist
            - For large networks (100+ buses), diagram may be complex
            - Use plot_substation_single_line_diagram() for detailed substation views

        Performance:
            - Generation time: <1s for small networks, up to several seconds for large ones
            - Image typically 500KB-2MB depending on network size

        Related Tools:
            - plot_substation_single_line_diagram(): Detailed view of individual substations
            - get_network_info(): Get network statistics before visualizing

        Common Uses:
            - Verify network topology after loading
            - Present network structure in reports
            - Understand network connectivity
            - Identify network layout and structure
        """
        session_id = get_session_id(ctx)

        if network_id is None:
            network_id = self.get_proxy(session_id).current_network_id
        logger.debug(f"Generating visualization for network '{network_id}'")

        if network_id is None:
            msg = "No network specified and no current network selected"
            logger.warning(msg)
            return msg

        if network_id not in self.get_proxy(session_id).networks:
            msg = f"Network '{network_id}' not found"
            logger.warning(msg)
            return msg

        try:
            # Get SVG data as bytes
            image_data = self.get_proxy(session_id).create_network_visualization_bytes(
                network_id
            )

            filename = f"network_{network_id}_visualization.svg"
            link_info = generate_download_link(
                filename=filename,
                file_data=image_data,
                download_base_url=DOWNLOAD_BASE_URL,
                expiry_seconds=DOWNLOAD_LINK_EXPIRY_SECONDS,
            )

            logger.debug(
                f"Generated visualization URL for network '{network_id}': {link_info['download_url']}"
            )
            return link_info["download_url"]

        except Exception as e:  # noqa: BLE001
            msg = f"Failed to create visualization: {e}"
            logger.error(msg)
            return msg
