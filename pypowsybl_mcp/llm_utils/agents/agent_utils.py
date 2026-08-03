#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from agents import Agent, ModelSettings, function_tool
from agents.extensions.models.litellm_model import LitellmModel
from agents.mcp import MCPServer


def create_agent(
    name: str,
    model_name: str,
    api_key: str,
    base_url: str,
    instructions: str,
    mcp_servers: list[MCPServer],
    tools: list[function_tool] = None,
    model_settings: ModelSettings = None,
    **kwargs,
) -> Agent:
    """Create an agent with the specified configuration"""
    model = LitellmModel(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
    )

    if not model_settings:
        model_settings = ModelSettings()

    agent = Agent(
        name=name,
        model=model,
        instructions=instructions,
        mcp_servers=mcp_servers,
        tools=tools,
        model_settings=model_settings,
        **kwargs,
    )

    return agent
