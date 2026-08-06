#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from cachetools import TTLCache
from loguru import logger
from mcp.server import FastMCP

from pypowsybl_mcp.proxy import PyPowsyblMCPServerProxy


def wrap_class_methods_with_mcp_tool(
    obj, mcp: FastMCP, exclude: list[str] | None = None
):
    """
    Wrap all user-defined methods of an instance `obj` with mcp.tool(),
    skipping inherited methods, magic methods, and optionally some user-specified methods.

    Args:
        obj: the instance whose methods will be wrapped
        mcp: object that has a .tool() decorator
        exclude: list of method names to skip (default: None)
    """
    if exclude is None:
        exclude = []

    cls = obj.__class__

    for name, func in cls.__dict__.items():
        # Skip magic methods, excluded methods, and non-callables
        if not name.startswith("__") and callable(func) and name not in exclude:
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

    def register_tools_with_mcp(self, mcp: FastMCP, exclude: list[str] | None = None):
        """Register a MCP instance to be used for tool wrapping."""
        wrap_class_methods_with_mcp_tool(self, mcp, exclude)
