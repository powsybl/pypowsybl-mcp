#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import os

from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.llm_utils.agents.code_generation import generate_code_from_macro
from pypowsybl_mcp.tools import PyPowsyblTool
from pypowsybl_mcp.utils import DOWNLOAD_BASE_URL, DOWNLOAD_LINK_EXPIRY_SECONDS
from pypowsybl_mcp.utils.download_utils import generate_download_link


def register_code_tools(mcp: FastMCP, pypowsybl_proxies):
    tools = CodeTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


class CodeTools(PyPowsyblTool):
    async def generate_python_script(
        self,
        actions: str,
        script_name: str = "my_script.py",
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Generate a standalone Python script from a sequence of MCP tool actions.

        Takes a list of MCP tool calls (tool names and parameters) and generates
        executable Python code that performs the same operations directly using
        PyPowsybl, without requiring the MCP server.

        Args:
            actions (str): A string describing a list of actions to convert to code. Each action references
                - tool_name: Name of the MCP tool (e.g., "create_ieee_network")
                - parameters: Parameters to pass to the tool
            script_name (str, optional): Base name for the generated script. Default: "my_script.py"

        Returns:
            str: URL to download the generated Python script.

        Example Usage:
            actions = "
                1. Appeler l'outil pypowsybl__create_ieee_network avec les paramètres : {"network_type":"IEEE14","network_id":"ieee_14_test","set_as_current":true}
                2. Appeler l'outil pypowsybl__run_loadflow avec les paramètres : {"network_id":"ieee_14_test"}
            ]"

        Generated Script Features:
            - Standalone executable Python code
            - No MCP server dependency
            - Error handling
            - Result printing
            - Can be customized after generation

        Notes:
            - Review and test generated code before production use
            - Before writing any pypowsybl call whose exact signature or
              parameters you are unsure of, look it up with
              get_online_resource(class_object=...) instead of relying on prior
              knowledge, which may be outdated.
        """
        logger.debug(f"Generating Python script from following actions:\n{actions}")

        # Collect source code from all relevant tool files
        tools_dir = os.path.dirname(os.path.dirname(__file__))
        reference_code = ""
        # Identify which tools are mentioned in the actions
        # Actions are typically like: "Appeler l'outil pypowsybl__create_ieee_network"
        import re

        mentioned_tools = set(re.findall(r"pypowsybl__(\w+)", actions))

        def _get_relevant_content(directory, prefix=""):
            content = ""
            for filename in os.listdir(directory):
                if (
                    filename.endswith(".py")
                    and filename != "__init__.py"
                    and filename != "code_export.py"
                ):
                    file_path = os.path.join(directory, filename)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            file_content = f.read()
                            # If tools are mentioned, only include files that contain those tools
                            if not mentioned_tools or any(
                                tool in file_content for tool in mentioned_tools
                            ):
                                # Strip imports and boilerplate to save tokens
                                lines = file_content.splitlines()
                                filtered_lines = [
                                    li
                                    for li in lines
                                    if not li.startswith(
                                        ("import ", "from ", "#", '"""')
                                    )
                                    or "def " in li
                                    or "class " in li
                                ]
                                content += f"\n\n# Source from {prefix}{filename}:\n"
                                content += "\n".join(filtered_lines)
                    except OSError as e:
                        logger.warning(f"Could not read {file_path}: {e}")
            return content

        reference_code += _get_relevant_content(tools_dir)
        reference_code += _get_relevant_content(os.path.dirname(__file__), "utils/")

        output = await generate_code_from_macro(
            actions=actions, reference_code=reference_code
        )
        if output.startswith("# Error:"):
            raise RuntimeError(output)

        # Generate download link
        link_info = generate_download_link(
            filename=script_name,
            file_data=output.encode("utf-8"),
            download_base_url=DOWNLOAD_BASE_URL,
            expiry_seconds=DOWNLOAD_LINK_EXPIRY_SECONDS,
        )
        return link_info["download_url"]
