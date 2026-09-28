#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import pytest
from mcp import Client, MCPError
from mcp.server.mcpserver import MCPServer
from mcp.types import METHOD_NOT_FOUND

from pypowsybl_mcp.utils.stateful_sessions import require_stateful_session


@pytest.fixture
def server():
    server = MCPServer("TestServer", middleware=[require_stateful_session])

    @server.tool()
    async def ping() -> str:
        return "pong"

    return server


@pytest.mark.asyncio
async def test_handshake_session_is_served(server):
    async with Client(server, mode="legacy") as client:
        result = await client.call_tool("ping", {})

    assert result.content[0].text == "pong"


@pytest.mark.asyncio
async def test_stateless_protocol_version_is_refused(server):
    async with Client(server, mode="2026-07-28") as client:
        with pytest.raises(MCPError, match="stateful MCP session") as excinfo:
            await client.call_tool("ping", {})

    assert excinfo.value.code == METHOD_NOT_FOUND
