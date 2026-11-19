#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import asyncio
import os
import shutil
import tempfile
import urllib.request
from urllib.parse import urlparse

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from pypowsybl_mcp.tools import PyPowsyblTool
from pypowsybl_mcp.tools.utils.import_options import (
    build_load_arguments,
    describe_import_options,
    explain_load_error,
    resolve_boundary_set,
)
from pypowsybl_mcp.utils import DOWNLOAD_BASE_URL, DOWNLOAD_LINK_EXPIRY_SECONDS
from pypowsybl_mcp.utils.download_utils import generate_download_link
from pypowsybl_mcp.utils.user_session_management import get_session_id


def register_io_tools(mcp: MCPServer, pypowsybl_proxies: TTLCache):
    io_tools = IOTools(pypowsybl_proxies)
    io_tools.register_tools_with_mcp(mcp)


def _download_to_file(url: str, dest_path: str) -> None:
    with urllib.request.urlopen(url) as response, open(dest_path, "wb") as tmp_file:
        tmp_file.write(response.read())


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _file_name_of(url: str, fallback: str) -> str:
    """The file name a URL ends with, or `fallback` when its path has none."""
    return os.path.basename(urlparse(url).path) or fallback


def _remove_tree(path: str) -> None:
    """Remove a temporary directory, logging (not raising) what cannot be."""
    if not path or not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path)
    except OSError as cleanup_err:
        logger.warning(f"Could not remove temp directory {path}: {cleanup_err}")


class Status:
    SUCCESS = "success"
    ERROR = "error"


class IOTools(PyPowsyblTool):
    async def load_network_from_url(
        self,
        url: str,
        network_id: str,
        set_as_current: bool = True,
        parameters: dict | None = None,
        post_processors: list[str] | None = None,
        boundary_set: str | None = None,
        boundary_url: str | None = None,
        ctx: Context = None,  # MCPServer injects this
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

        Import options (all optional; see get_import_options for what a format accepts):
            parameters (dict, optional): Importer parameters by full name, e.g.
                {"iidm.import.cgmes.naming-strategy": "cgmes"}. Values may be
                strings, booleans, numbers or lists. An unknown name is an error.
            post_processors (list[str], optional): Post-processors to run, CGMES
                ones (e.g. "EntsoeCategory", which tags each generator with its
                ENTSO-E category, read back with the entsoe_category attach token)
                and generic ones (e.g. "geoJsonImporter") alike.
            boundary_set (str, optional): Name of a CGMES boundary set the server
                provides (listed by get_import_options).
            boundary_url (str, optional): URL of a CGMES boundary set (.zip or
                .xml) to download. Exclusive with boundary_set. Without either,
                the boundary files may also sit in the CGMES archive itself.

        Supported Formats:
            Same as load_network_from_file: XIIDM, UCTE, MATPOWER, PSS/E, RAW, CGMES

        Returns:
            dict: status (success or error) and a message; on success also
            source_format (the format PowSyBl recognized) and the options applied.

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

        # A unique temporary directory, so concurrent downloads never collide
        # on the same file name; removed with everything in it once loaded.
        tmp_dir = tempfile.mkdtemp()
        try:
            boundary_location, boundary = await self._boundary_location(
                boundary_set, boundary_url, tmp_dir
            )
            load_args = build_load_arguments(
                parameters, post_processors, boundary_location, boundary
            )

            # The importer is chosen from the file name, so it is kept as in the
            # URL (the URL is expected to be correctly formatted).
            tmp_path = os.path.join(tmp_dir, _file_name_of(url, "downloaded_network"))
            # Download and save the remote file (blocking I/O off the event loop)
            await asyncio.to_thread(_download_to_file, url, tmp_path)

            network = pp.network.load(
                tmp_path,
                parameters=load_args.parameters or None,
                post_processors=load_args.post_processors or None,
            )
            proxy.register_network(network_id, network, set_as_current)
            return self._loaded(network_id, network, url, load_args)

        except (pp.PyPowsyblError, OSError, ValueError) as e:
            message = explain_load_error(e, bool(boundary_set or boundary_url))
            logger.error(f"Failed to load network from URL: {message}")
            return {
                "status": Status.ERROR,
                "message": f"Failed to load network from URL: {message}",
            }
        finally:
            _remove_tree(tmp_dir)

    async def _boundary_location(
        self, boundary_set: str | None, boundary_url: str | None, tmp_dir: str
    ) -> tuple[str | None, str | None]:
        """The server path of the requested CGMES boundary set, and its label.

        A downloaded set gets its own directory: PowSyBl reads a boundary
        location that is a directory or an archive, not a lone XML file.
        """
        if boundary_set and boundary_url:
            raise ValueError("Pass boundary_set or boundary_url, not both")
        if boundary_set:
            return resolve_boundary_set(boundary_set), f"boundary_set '{boundary_set}'"
        if boundary_url:
            boundary_dir = os.path.join(tmp_dir, "boundary")
            os.makedirs(boundary_dir)
            file_name = _file_name_of(boundary_url, "boundary.zip")
            boundary_path = os.path.join(boundary_dir, file_name)
            await asyncio.to_thread(_download_to_file, boundary_url, boundary_path)
            return boundary_dir, f"boundary_url ({file_name})"
        return None, None

    @staticmethod
    def _loaded(network_id: str, network, source: str, load_args) -> dict:
        """The answer of a successful load."""
        buses = network.get_buses()
        result = {
            "status": Status.SUCCESS,
            "message": f"Successfully loaded network '{network_id}' from {source} with {len(buses)} buses",
            "network_id": network_id,
            "source_format": network.source_format,
        }
        result.update(load_args.summary())
        return result

    async def load_network_from_file(
        self,
        path: str,
        network_id: str,
        set_as_current: bool = True,
        parameters: dict | None = None,
        post_processors: list[str] | None = None,
        boundary_set: str | None = None,
        boundary_url: str | None = None,
        ctx: Context = None,
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

        Import options (all optional; see get_import_options for what a format accepts):
            parameters (dict, optional): Importer parameters by full name, e.g.
                {"iidm.import.cgmes.naming-strategy": "cgmes"}. Values may be
                strings, booleans, numbers or lists. An unknown name is an error.
            post_processors (list[str], optional): Post-processors to run, CGMES
                ones (e.g. "EntsoeCategory", which tags each generator with its
                ENTSO-E category, read back with the entsoe_category attach token)
                and generic ones (e.g. "geoJsonImporter") alike.
            boundary_set (str, optional): Name of a CGMES boundary set the server
                provides (listed by get_import_options).
            boundary_url (str, optional): URL of a CGMES boundary set (.zip or
                .xml) to download. Exclusive with boundary_set. Without either,
                the boundary files may also sit in the CGMES archive itself.

        Supported Formats:
            XIIDM, UCTE, MATPOWER, PSS/E, RAW, CGMES

        Returns:
            dict: status (success or error) and a message; on success also
            source_format (the format PowSyBl recognized) and the options applied.

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

        if not os.path.exists(path):
            return {
                "status": Status.ERROR,
                "message": f"Error: File not found at path: {path}",
            }

        tmp_dir = tempfile.mkdtemp()
        try:
            boundary_location, boundary = await self._boundary_location(
                boundary_set, boundary_url, tmp_dir
            )
            load_args = build_load_arguments(
                parameters, post_processors, boundary_location, boundary
            )
            network = pp.network.load(
                path,
                parameters=load_args.parameters or None,
                post_processors=load_args.post_processors or None,
            )
            proxy.register_network(network_id, network, set_as_current)
            return self._loaded(network_id, network, path, load_args)

        except (pp.PyPowsyblError, OSError, ValueError) as e:
            message = explain_load_error(e, bool(boundary_set or boundary_url))
            logger.error(f"Failed to load network from file: {message}")
            return {
                "status": Status.ERROR,
                "message": f"Failed to load network from file: {message}",
            }
        finally:
            _remove_tree(tmp_dir)

    async def get_import_options(self, format: str | None = None) -> dict:
        """List the options a network can be loaded with.

        Use it before load_network_from_url / load_network_from_file when the
        file needs more than a plain load: a CGMES boundary set, a post-processor
        such as EntsoeCategory (ENTSO-E category of each generator), or an
        importer parameter.

        Args:
            format (str, optional): An import format (e.g. "CGMES", "XIIDM",
                "UCTE"; case-insensitive) to also list that importer's
                parameters. None = only the format-independent lists.

        Returns:
            dict: status, plus
              - formats: the import formats this server reads;
              - file_extensions: the file extensions those formats are read from;
              - post_processors: {"cgmes": [...], "generic": [...]};
              - boundary_sets: names of the CGMES boundary sets the server
                provides, for the boundary_set argument (may be empty);
              - default_boundary_set: whether a CGMES load given no boundary set
                still finds one at PowSyBl's default location;
              - with a format, parameters: [{name, description, type, default,
                possible_values}] (the boundary location is not listed: it is
                set with boundary_set / boundary_url).
        """
        try:
            return {"status": Status.SUCCESS, **describe_import_options(format)}
        except (pp.PyPowsyblError, ValueError) as e:
            return {"status": Status.ERROR, "message": str(e)}

    async def export_network(
        self,
        network_id: str | None = None,
        file_name: str | None = None,
        format_type: str = "XIIDM",
        ctx: Context = None,  # MCPServer injects this
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

        except (pp.PyPowsyblError, OSError, ValueError) as e:
            logger.error(f"Failed to export network: {e}")
            return {
                "status": Status.ERROR,
                "message": f"Failed to export network: {e!s}",
            }
