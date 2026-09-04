#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""
PyPowsybl FastMCP Server

A Model Context Protocol server for interacting with PyPowsybl power system networks.
Provides tools for network management, analysis execution, and grid visualization.
Uses FastMCP for simplified server implementation.
"""

import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts import Prompt
from mcp.server.fastmcp.resources import FileResource
from starlette.requests import Request
from starlette.responses import Response

from pypowsybl_mcp import DEFAULT_PORT
from pypowsybl_mcp.admin import register_admin_routes
from pypowsybl_mcp.plugins import (
    discover_and_load_plugins,
    discover_and_load_resource_plugins,
)
from pypowsybl_mcp.proxy import PyPowsyblMCPServerProxy
from pypowsybl_mcp.tools.loadflow_tools import register_loadflow_tools
from pypowsybl_mcp.tools.network_tools import register_network_tools
from pypowsybl_mcp.tools.security_tools import register_security_tools
from pypowsybl_mcp.tools.sensitivity_tools import (
    register_sensitivity_tools,
)
from pypowsybl_mcp.tools.utils.code_export import register_code_tools
from pypowsybl_mcp.tools.utils.io import register_io_tools
from pypowsybl_mcp.tools.utils.resources import register_resource_tools
from pypowsybl_mcp.tools.utils.session import register_session_tools
from pypowsybl_mcp.tools.utils.visualization import register_visualization_tools
from pypowsybl_mcp.utils.cachetools import ThreadSafeTTLCache
from pypowsybl_mcp.utils.download_utils import (
    download_file_endpoint,
)
from pypowsybl_mcp.utils.instrumentation import instrument_tool_calls
from pypowsybl_mcp.utils.session_registry import SESSIONS

# instantiate an MCP server client
mcp = FastMCP(
    "PyPowsybl MCP Server",
    port=int(os.getenv("MCP_PORT", DEFAULT_PORT)),
    host="0.0.0.0",
)

# Create pypowsybl proxy instances
MAX_NUMBER_OF_CLIENTS = 100
CLIENT_SESSION_TTL = 3600 * 24  # in secs, 1 day
# Thread-safe: tool calls run in worker threads while the admin API reads the
# same cache from the event loop.
pypowsybl_proxies: ThreadSafeTTLCache[str, PyPowsyblMCPServerProxy] = (
    ThreadSafeTTLCache(maxsize=MAX_NUMBER_OF_CLIENTS, ttl=CLIENT_SESSION_TTL)
)

# Resources
SKILLS_DIR = Path(__file__).parent / "skills"


def _skill_description(skill_path: Path) -> str:
    """Extract the description field from a skill file's YAML frontmatter."""
    lines = skill_path.read_text().splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("description:"):
                return line.removeprefix("description:").strip()
    return f"Instructions from the '{skill_path.stem}' skill"


def _make_skill_reader(skill_path: Path):
    def read_skill() -> str:
        return skill_path.read_text()

    return read_skill


def register_skill_resources_and_prompts(mcp: FastMCP):
    """Expose each skill file as a listable MCP resource and as an MCP prompt.

    Concrete resources (unlike URI templates) appear in resources/list, so
    clients can discover the skills without knowing their names in advance.
    Prompts are surfaced by clients such as Claude Code as slash commands,
    letting the user inject the skill instructions into the conversation.
    """
    for skill_path in sorted(SKILLS_DIR.glob("*.md")):
        description = _skill_description(skill_path)
        mcp.add_resource(
            FileResource(
                uri=f"skills://skills/{skill_path.stem}",
                path=skill_path.resolve(),
                name=skill_path.stem,
                description=description,
                mime_type="text/markdown",
            )
        )
        mcp.add_prompt(
            Prompt.from_function(
                _make_skill_reader(skill_path),
                name=skill_path.stem,
                description=description,
            )
        )


# Resources
register_skill_resources_and_prompts(mcp)
discover_and_load_resource_plugins(mcp, pypowsybl_proxies)


@mcp.resource("resources://temp/{resource_id}", mime_type="text/markdown")
def read_temp_resource(resource_id: str, ctx: Context) -> str:
    """Serve a temporary markdown resource stored in the TTL cache."""
    from pypowsybl_mcp.utils.user_session_management import get_session_id

    session_id = get_session_id(ctx)
    proxy = pypowsybl_proxies.get(session_id)
    if proxy is None:
        raise ValueError(f"Session '{session_id}' not found.")

    content = proxy.resources.get(resource_id)
    if content is None:
        raise ValueError(f"Markdown resource '{resource_id}' not found or has expired.")
    return content


# Tools
def register_tools(mcp: FastMCP):
    """Register all available tools with the MCP server."""
    register_io_tools(mcp, pypowsybl_proxies)
    register_network_tools(mcp, pypowsybl_proxies)
    register_visualization_tools(mcp, pypowsybl_proxies)
    register_loadflow_tools(mcp, pypowsybl_proxies)
    register_security_tools(mcp, pypowsybl_proxies)
    register_sensitivity_tools(mcp, pypowsybl_proxies)
    register_session_tools(mcp, pypowsybl_proxies)
    register_code_tools(mcp, pypowsybl_proxies)
    register_resource_tools(mcp, pypowsybl_proxies)
    discover_and_load_plugins(mcp, pypowsybl_proxies)


# HTTP endpoint to serve download links
@mcp.custom_route("/download/{token}/{filename}", methods=["GET"])
async def download_file_endpoint_handler(request: Request) -> Response:
    """HTTP endpoint to download files using temporary tokens."""
    return await download_file_endpoint(request)


# Read-only monitoring API (/admin/health, /admin/sessions) and the per-session
# call accounting it reports. Registered at import time, like the routes above:
# both have to exist before `mcp.run()` builds the HTTP app.
register_admin_routes(mcp, pypowsybl_proxies)
instrument_tool_calls(mcp, SESSIONS)


if __name__ == "__main__":
    register_tools(mcp)
    mcp.run(transport="streamable-http")
