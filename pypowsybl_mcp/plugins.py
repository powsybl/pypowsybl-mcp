#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Plugin discovery and loading.

Any installed Python package can extend this server by registering an entry
point in one (or both) of the groups below, resolving to a register
function with the same signature the host's own built-in modules use
(`register_*_tools(mcp, pypowsybl_proxies)`). No import of the plugin
package by name is needed on the host side — `pip install`ing is enough.
"""

import importlib.metadata
import os

from cachetools import TTLCache
from loguru import logger
from mcp.server.fastmcp import FastMCP

# Groups for plugins
PLUGIN_GROUP = "pypowsybl_mcp.plugins.v1"
RESOURCE_PLUGIN_GROUP = "pypowsybl_mcp.resources.v1"


def _disabled_plugin_names() -> set[str]:
    """Parse the MCP_DISABLED_PLUGINS env var into a set of entry-point names to skip."""
    return {
        name.strip()
        for name in os.getenv("MCP_DISABLED_PLUGINS", "").split(",")
        if name.strip()
    }


def discover_and_load_plugins(mcp: FastMCP, pypowsybl_proxies: TTLCache) -> None:
    """Load tool registrars declared under PLUGIN_GROUP by any installed package."""
    disabled = _disabled_plugin_names()
    for ep in importlib.metadata.entry_points(group=PLUGIN_GROUP):
        if ep.name in disabled:
            logger.info(
                f"Plugin '{ep.name}' disabled via MCP_DISABLED_PLUGINS, skipping."
            )
            continue
        try:
            register_plugin_tools = ep.load()
            register_plugin_tools(mcp, pypowsybl_proxies)
            logger.success(f"Loaded plugin '{ep.name}'.")
        except Exception as e:
            # STRICT ISOLATION: a failing plugin must never crash the host server.
            logger.error(f"Failed to load plugin '{ep.name}': {e}", exc_info=True)


def discover_and_load_resource_plugins(
    mcp: FastMCP, pypowsybl_proxies: TTLCache
) -> None:
    """Load resource/prompt registrars declared under RESOURCE_PLUGIN_GROUP by any installed package."""
    disabled = _disabled_plugin_names()
    for ep in importlib.metadata.entry_points(group=RESOURCE_PLUGIN_GROUP):
        if ep.name in disabled:
            logger.info(
                f"Plugin '{ep.name}' disabled via MCP_DISABLED_PLUGINS, skipping."
            )
            continue
        try:
            register_plugin_resources = ep.load()
            register_plugin_resources(mcp, pypowsybl_proxies)
            logger.success(f"Loaded resource/prompt plugin '{ep.name}'.")
        except Exception as e:
            logger.error(
                f"Failed to load resource/prompt plugin '{ep.name}': {e}", exc_info=True
            )
