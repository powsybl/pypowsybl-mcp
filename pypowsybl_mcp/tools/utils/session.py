#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import os

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.utils.session_registry import SESSIONS


def register_session_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    # ----------------------------------- ADMIN FUNCTIONS
    @mcp.tool()
    async def get_pypowsybl_version(
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Get the version of the pypowsybl library being used by the MCP server.

        Returns:
            str: The pypowsybl version string.
        """
        version = pp.__version__
        logger.info(f"Pypowsybl version requested: {version}")
        return f"pypowsybl version: {version}"

    @mcp.tool()
    async def set_session_id(
        authorization_token: str,
        session_id: str,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Set the active session ID for the current MCP connection.
        This tool shall NEVER be called directly by a LLM and is protected by an authorization token.

        Subsequent tool calls from this connection will use this session ID
        to access the corresponding state.

        Args:
            authorization_token (str): used to protect the use of this tool,
            session_id (str): The unique identifier for the session.
        """
        if (
            not os.getenv("MCP_AUTH_TOKEN")
            or os.getenv("MCP_AUTH_TOKEN") != authorization_token
        ):
            return "Invalid authorization token, you cannot not use this function"
        if ctx and ctx.session:
            ctx.session.session_id = session_id
            logger.info(f"Session ID set to {session_id} for current connection")
            return f"Session ID set to {session_id}"
        return "Error: Could not set session ID (no context)"

    @mcp.tool()
    async def duplicate_session(
        authorization_token: str,
        source_session_id: str,
        target_session_id: str,
        overwrite: bool = True,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Duplicate the state of one session into another.
        This tool shall NEVER be called directly by a LLM and is protected by an authorization token.

        This is useful for forking a conversation while preserving the power system
        state (loaded networks, variants, etc.).

        Args:
            authorization_token (str): used to protect the use of this tool,
            source_session_id (str): The session ID to copy from.
            target_session_id (str): The session ID to copy to.
            overwrite (bool, optional): Whether to overwrite the target session if it already exists. Default: True.
        """
        if (
            not os.getenv("MCP_AUTH_TOKEN")
            or os.getenv("MCP_AUTH_TOKEN") != authorization_token
        ):
            return "Invalid authorization token, you cannot not use this function"

        if source_session_id not in pypowsybl_proxies:
            return f"Error: Source session '{source_session_id}' not found"

        if target_session_id in pypowsybl_proxies and not overwrite:
            return f"Error: Target session '{target_session_id}' already exists and overwrite is False"

        source_proxy = pypowsybl_proxies[source_session_id]
        target_proxy = source_proxy.copy()

        pypowsybl_proxies[target_session_id] = target_proxy
        # Record the fork now, so the admin API reports the target's real
        # creation time instead of guessing it the first time a tool runs.
        SESSIONS.touch(target_session_id)
        logger.info(
            f"Duplicated session '{source_session_id}' to '{target_session_id}'"
        )
        return f"Session '{source_session_id}' duplicated to '{target_session_id}'"

    # ----------------------------------- ADMIN FUNCTIONS
