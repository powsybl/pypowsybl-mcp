#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import asyncio
import os
import tempfile
import urllib.request
from urllib.parse import urlparse

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.tools import PyPowsyblTool
from pypowsybl_mcp.utils import DOWNLOAD_BASE_URL, DOWNLOAD_LINK_EXPIRY_SECONDS
from pypowsybl_mcp.utils.download_utils import generate_download_link
from pypowsybl_mcp.utils.user_session_management import get_session_id


def register_io_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    io_tools = IOTools(pypowsybl_proxies)
    io_tools.register_tools_with_mcp(mcp)


def _download_to_file(url: str, dest_path: str) -> None:
    with urllib.request.urlopen(url) as response, open(dest_path, "wb") as tmp_file:
        tmp_file.write(response.read())


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class Status:
    SUCCESS = "success"
    ERROR = "error"


class IOTools(PyPowsyblTool):
    async def load_network_from_url(
        self,
        url: str,
        network_id: str,
        set_as_current: bool = True,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> dict:
        """
        Download and load a power system network from a URL.

        Fetches a grid file from a remote URL, saves it temporarily, and loads it into
        memory. Useful for accessing networks hosted on web servers, file shares, or
        cloud storage services.

        Args:
            url (str): Full URL pointing to the network file. Must be publicly accessible
                or accessible with the server's credentials. Supports HTTP and HTTPS.
                Examples:
                    - "https://example.com/networks/grid_model.xiidm"
                    - "http://data.server.org/grids/2024/network.uct"
            network_id (str): Unique identifier for the loaded network.
            set_as_current (bool, optional): If True, sets as the active network. Default: True.

        Supported Formats:
            Same as load_network_from_file: XIIDM, UCTE, MATPOWER, PSS/E, RAW, CGMES

        Returns:
            dict: Contains status (success or error) and associated message with network details or error message.

        Example:
            load_network_from_url(
                "https://example.com/grids/ieee14.xiidm",
                "remote_network",
                True
            )
            → "Successfully loaded network 'remote_network' from https://example.com/grids/ieee14.xiidm with 14 buses"

        Workflow:
                1. Downloads file from URL to temporary directory
            2. Loads network from temporary file
            3. Registers network in server
            4. Cleans up temporary file
            5. Sets as current network if requested

        Notes:
            - Temporary files are automatically deleted after loading
            - Download size is not limited - be cautious with very large files
            - Network connection must be available
            - Filename is extracted from URL path for proper format detection

        Common Errors:
            - URLError: URL is unreachable or invalid
            - HTTPError: HTTP error (404, 403, 500, etc.)
            - ParseError: Downloaded file format is invalid
            - TimeoutError: Download took too long
        """
        logger.debug(f"Loading network from URL {url} as '{network_id}'")
        proxy = self.get_proxy(get_session_id(ctx))

        try:
            # Define temporary directory
            tmp_dir = tempfile.gettempdir()

            # Extract file name from URL (the URL is expected to be correctly formatted)
            file_name = os.path.basename(urlparse(url).path)

            # Create the fill path of the temporary file inside the temporary directory
            tmp_path = os.path.join(tmp_dir, file_name)

            # Download and save the remote file (blocking I/O off the event loop)
            await asyncio.to_thread(_download_to_file, url, tmp_path)

            try:
                # Load the network from the temporary file
                network = pp.network.load(tmp_path)
            finally:
                # Clean up the temp file
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError as cleanup_err:
                        logger.warning(
                            f"Could not remove temp file {tmp_path}: {cleanup_err}"
                        )

            # Register the loaded network
            proxy.register_network(network_id, network, set_as_current)

            buses = network.get_buses()
            return {
                "status": Status.SUCCESS,
                "message": f"Successfully loaded network '{network_id}' from {url} with {len(buses)} buses",
            }

        except (pp.PyPowsyblError, OSError, ValueError) as e:
            logger.error(f"Failed to load network from URL: {e}")
            return {
                "status": Status.ERROR,
                "message": f"Failed to load network from URL: {e!s}",
            }

    async def load_network_from_file(
        self,
        path: str,
        network_id: str,
        set_as_current: bool = True,
        ctx: Context[ServerSession, None] = None,
    ) -> dict:
        """
        Load a power grid network from a local file path on the server.

        This tool loads a grid from a variety of standard formats. Useful for loading
        grids already stored on the server's filesystem.

        Args:
            path (str): Full local path to the network file on the server.
                Examples:
                    - "/data/networks/grid_model.xiidm"
                    - "C:\\Grids\\IEEE14.raw"
            network_id (str): Unique identifier for the loaded network.
            set_as_current (bool, optional): If True, sets as the active network. Default: True.

        Supported Formats:
            XIIDM, UCTE, MATPOWER, PSS/E, RAW, CGMES

        Returns:
            dict: Contains status (success or error) and associated message with network details or error message.

        Example:
            load_network_from_file(
                "/home/user/grids/ieee14.xiidm",
                "local_network",
                True
            )
            → "Successfully loaded network 'local_network' from /home/user/grids/ieee14.xiidm with 14 buses"
        """
        logger.debug(f"Loading network from file {path} as '{network_id}'")
        proxy = self.get_proxy(get_session_id(ctx))

        try:
            if not os.path.exists(path):
                return {
                    "status": Status.ERROR,
                    "message": f"Error: File not found at path: {path}",
                }

            # Load the network from the file
            network = pp.network.load(path)

            # Register the loaded network
            proxy.register_network(network_id, network, set_as_current)

            buses = network.get_buses()
            return {
                "status": Status.SUCCESS,
                "message": f"Successfully loaded network '{network_id}' from {path} with {len(buses)} buses",
            }

        except (pp.PyPowsyblError, OSError, ValueError) as e:
            logger.error(f"Failed to load network from file: {e}")
            return {
                "status": Status.ERROR,
                "message": f"Failed to load network from file: {e!s}",
            }

    async def export_network(
        self,
        network_id: str | None = None,
        file_name: str | None = None,
        format_type: str = "XIIDM",
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> dict:
        """
        Export a network to a file in standard power system format.

        Saves the current state of a network to disk, preserving all topology, parameters,
        and analysis results. Useful for sharing networks, archiving scenarios, or
        transferring to other tools.

        Args:
            network_id (str, optional): Network to export. If None, uses current network. Default: None.
            file_name (str, optional): Output file name. If None, generates filename from
                network_id and format (e.g., "network_name.xiidm"). Default: None.
            format_type (str, optional): Export format. Options:
                - "XIIDM": PowSyBl XML format (recommended, preserves all data)
                - "UCTE": Pan-European network format
                - "CGMES": European TSO standard
                Additional formats may be available depending on PowSyBl configuration.
                Default: "XIIDM".

        Returns:
            dict: Contains status (success or error) and message with download link or error message.

        Example Usage:
            export_network("ieee_14", "/data/exports/ieee14_modified.xiidm", "XIIDM")
            → {"status": "success", "message": "http://localhost:9992/download/TOKEN/ieee14_modified.xiidm"}

            export_network()  # Exports current network with auto-generated filename
            → {"status": "success", "message": "http://localhost:9992/download/TOKEN/my_network.xiidm"}

        Format Recommendations:
            - XIIDM: Best for PowSyBl workflows, preserves all information
            - UCTE: For exchange with European TSO systems
            - CGMES: For compliance with European standards (ENTSO-E)

        What Gets Exported:
            - Complete network topology
            - All component parameters (generators, loads, lines, etc.)
            - Voltage levels and ratings
            - Control settings and limits
            - Current state (including loadflow results if in format)
            - Extensions and custom attributes (format-dependent)

        Notes:
            - Creates or overwrites file at specified path
            - Parent directory must exist
            - File permissions must allow writing
            - Some formats may not preserve all PowSyBl-specific attributes
            - Exported files can be re-loaded with load_network_from_file()

        File Naming:
            - If file_path is None, uses: "{network_id}.{extension}"
            - Extension determined by format_type (e.g., .xiidm, .uct)
            - Relative paths are relative to server working directory

        Common Use Cases:
            - Save modified networks after parameter changes
            - Archive scenario variants
            - Share networks with colleagues
            - Create network libraries
            - Backup before major modifications
            - Export to other analysis tools

        Related Tools:
            - load_network_from_file(): Reload exported networks
            - modify_network(): Modify before exporting
            - get_network_info(): Verify network state before exporting
        """
        session_id = get_session_id(ctx)

        if network_id is None:
            network_id = self.get_proxy(session_id).current_network_id
        logger.debug(f"Exporting network '{network_id}' to {file_name}")

        if network_id is None:
            return {
                "status": Status.ERROR,
                "message": "No network specified and no current network selected",
            }

        if network_id not in self.get_proxy(session_id).networks:
            return {
                "status": Status.ERROR,
                "message": f"Network '{network_id}' not found",
            }

        if file_name is None:
            filename = f"{network_id}.{format_type.lower()}"
        else:
            filename = os.path.basename(file_name)

        try:
            network = self.get_proxy(session_id).networks[network_id]

            # Create a temporary file to save the network
            with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                tmp_path = tmp_file.name

            try:
                # Save network to the temporary file
                network.save(tmp_path, format=format_type)

                # Read the file data (blocking I/O off the event loop)
                file_data = await asyncio.to_thread(_read_file_bytes, tmp_path)

                # Generate download link
                link_info = generate_download_link(
                    filename=filename,
                    file_data=file_data,
                    download_base_url=DOWNLOAD_BASE_URL,
                    expiry_seconds=DOWNLOAD_LINK_EXPIRY_SECONDS,
                )

                logger.info(
                    f"Exported network '{network_id}' to download link: {link_info['download_url']}"
                )
                return {
                    "status": Status.SUCCESS,
                    "message": link_info["download_url"],
                }

            finally:
                # Clean up the initial temporary file
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        except (pp.PyPowsyblError, OSError) as e:
            logger.error(f"Failed to export network: {e}")
            return {
                "status": Status.ERROR,
                "message": f"Failed to export network: {e!s}",
            }
