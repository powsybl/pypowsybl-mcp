#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from cachetools import TTLCache
from loguru import logger
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.proxy import PyPowsyblMCPServerProxy
from pypowsybl_mcp.utils.user_session_management import get_session_id


class NetworkNotFoundError(ValueError):
    """Raised when a network cannot be resolved from the current session state."""


def wrap_class_methods_with_mcp_tool(
    obj, mcp: FastMCP, exclude: list[str] | None = None
):
    """
    Wrap all public methods of an instance `obj` with mcp.tool(),
    skipping inherited methods, underscore-prefixed (private) methods, and
    optionally some user-specified methods.

    Methods whose name starts with an underscore are treated as internal
    helpers and are never exposed as tools, so private helpers do not need to
    be enumerated in `exclude`.

    Args:
        obj: the instance whose methods will be wrapped
        mcp: object that has a .tool() decorator
        exclude: list of additional public method names to skip (default: None)
    """
    if exclude is None:
        exclude = []

    cls = obj.__class__

    for name, func in cls.__dict__.items():
        # Skip private/underscore-prefixed methods, excluded methods, and non-callables
        if not name.startswith("_") and callable(func) and name not in exclude:
            logger.debug(f"Wrapping tool into MCP: {name}")
            bound_method = getattr(obj, name)
            wrapped = mcp.tool()(bound_method)
            setattr(obj, name, wrapped)


class PyPowsyblTool:
    """Superclass for PyPowsybl tools that need to interact with MCP."""

    def __init__(self, pypowsybl_proxies: TTLCache):
        self.pypowsybl_proxies = pypowsybl_proxies

    def get_proxy(self, session_id: str) -> PyPowsyblMCPServerProxy:
        """Retrieve the PyPowsyblMCPServerProxy instance for the given session."""
        if session_id not in self.pypowsybl_proxies:
            self.pypowsybl_proxies[session_id] = PyPowsyblMCPServerProxy()
        return self.pypowsybl_proxies[session_id]

    def resolve_network(self, ctx: Context, network_id: str | None = None):
        """Resolve and validate a network for the current session.

        Falls back to the session's current network when ``network_id`` is None.

        Args:
            ctx: The MCP request context (used to derive the session).
            network_id: Explicit network id, or None to use the current network.

        Returns:
            tuple: ``(proxy, network_id, network)`` where ``proxy`` is the
            session proxy, ``network_id`` is the resolved id and ``network`` is
            the corresponding network object.

        Raises:
            NetworkNotFoundError: If no network is selected or the id is unknown.
        """
        proxy = self.get_proxy(get_session_id(ctx))
        if network_id is None:
            network_id = proxy.current_network_id
        if network_id is None:
            raise NetworkNotFoundError(
                "No network specified and no current network selected"
            )
        if network_id not in proxy.networks:
            raise NetworkNotFoundError(f"Network '{network_id}' not found")
        return proxy, network_id, proxy.networks[network_id]

    def register_tools_with_mcp(self, mcp: FastMCP, exclude: list[str] | None = None):
        """Register a MCP instance to be used for tool wrapping."""
        wrap_class_methods_with_mcp_tool(self, mcp, exclude)
