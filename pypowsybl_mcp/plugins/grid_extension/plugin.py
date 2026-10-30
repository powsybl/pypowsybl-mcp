#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Entry points of the grid extension plugin (see docs/plugins.md of the host).

``register_plugin_tools``
    The creation and removal tools, after checking the creation profiles
    against the installed pypowsybl, and ``/admin/creation-coverage``, which
    reports that check.

``register_plugin_resources``
    The ``grid-extension`` skill, as a resource and a prompt, under the same URI
    scheme as the host's own skills.
"""

from pathlib import Path

from cachetools import TTLCache
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.prompts import Prompt
from mcp.server.mcpserver.resources import FileResource
from starlette.requests import Request
from starlette.responses import JSONResponse

from pypowsybl_mcp.admin import refuse_unauthorized
from pypowsybl_mcp.plugins.grid_extension.creation import drift
from pypowsybl_mcp.plugins.grid_extension.creation_tools import register_creation_tools
from pypowsybl_mcp.plugins.grid_extension.removal_tools import register_removal_tools

SKILL_PATH = Path(__file__).parent / "skills" / "grid-extension.md"


def register_plugin_tools(mcp: MCPServer, pypowsybl_proxies: TTLCache) -> None:
    register_creation_tools(mcp, pypowsybl_proxies)
    register_removal_tools(mcp, pypowsybl_proxies)

    @mcp.custom_route("/admin/creation-coverage", methods=["GET"])
    async def creation_coverage(request: Request) -> JSONResponse:
        """How the creation profiles match the installed pypowsybl: the report
        of the check run when the creation tools were registered, or a fresh
        one, changing nothing, when they were not."""
        if (refusal := refuse_unauthorized(request)) is not None:
            return refusal
        return JSONResponse(drift.LAST_REPORT or drift.startup_check(apply=False))


def _skill_description(path: Path) -> str:
    """The description field of a skill's YAML frontmatter."""
    lines = path.read_text().splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("description:"):
                return line.removeprefix("description:").strip()
    return f"Instructions from the '{path.stem}' skill"


def register_plugin_resources(mcp: MCPServer, pypowsybl_proxies: TTLCache) -> None:
    description = _skill_description(SKILL_PATH)
    mcp.add_resource(
        FileResource(
            uri=f"skills://skills/{SKILL_PATH.stem}",
            path=SKILL_PATH.resolve(),
            name=SKILL_PATH.stem,
            description=description,
            mime_type="text/markdown",
        )
    )
    mcp.add_prompt(
        Prompt.from_function(
            SKILL_PATH.read_text, name=SKILL_PATH.stem, description=description
        )
    )
