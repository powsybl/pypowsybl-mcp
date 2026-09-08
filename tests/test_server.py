#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from pypowsybl_mcp.server import register_tools


@pytest.fixture
def mcp():
    return FastMCP("TestServer")


def test_register_tools(mcp):
    with (
        patch("pypowsybl_mcp.server.register_io_tools") as mock_io,
        patch("pypowsybl_mcp.server.register_network_tools") as mock_network,
        patch("pypowsybl_mcp.server.register_visualization_tools") as mock_viz,
        patch("pypowsybl_mcp.server.register_loadflow_tools") as mock_lf,
        patch("pypowsybl_mcp.server.register_security_tools") as mock_sa,
        patch("pypowsybl_mcp.server.register_sensitivity_tools") as mock_sens,
        patch("pypowsybl_mcp.server.register_session_tools") as mock_session,
        patch("pypowsybl_mcp.server.register_code_tools") as mock_code,
        patch("pypowsybl_mcp.server.discover_and_load_plugins") as mock_plugins,
    ):
        register_tools(mcp)

        mock_io.assert_called_once()
        mock_network.assert_called_once()
        mock_viz.assert_called_once()
        mock_lf.assert_called_once()
        mock_sa.assert_called_once()
        mock_sens.assert_called_once()
        mock_session.assert_called_once()
        mock_code.assert_called_once()
        mock_plugins.assert_called_once()


@pytest.mark.asyncio
async def test_download_file_endpoint_handler():
    from pypowsybl_mcp.server import download_file_endpoint_handler

    mock_request = MagicMock()

    with patch("pypowsybl_mcp.server.download_file_endpoint") as mock_endpoint:
        mock_endpoint.return_value = MagicMock()
        await download_file_endpoint_handler(mock_request)
        mock_endpoint.assert_called_once_with(mock_request)


@pytest.mark.asyncio
async def test_skill_resources_and_prompts_registered():
    from pypowsybl_mcp.server import register_skill_resources_and_prompts

    test_mcp = FastMCP("TestServer")
    register_skill_resources_and_prompts(test_mcp)

    # Skills are exposed as concrete resources, discoverable via resources/list
    resources = await test_mcp.list_resources()
    by_uri = {str(r.uri): r for r in resources}
    assert "skills://skills/remote-resource" in by_uri
    # Description comes from the skill file frontmatter, not a generic fallback
    assert "pypowsybl" in by_uri["skills://skills/remote-resource"].description

    contents = list(await test_mcp.read_resource("skills://skills/remote-resource"))
    assert "get_online_resource" in contents[0].content

    # Skills are also exposed as prompts (slash commands in Claude Code)
    prompts = await test_mcp.list_prompts()
    assert "remote-resource" in [p.name for p in prompts]

    result = await test_mcp.get_prompt("remote-resource")
    assert "get_online_resource" in result.messages[0].content.text


@pytest.mark.asyncio
async def test_read_temp_resource():
    from pypowsybl_mcp.proxy import PyPowsyblMCPServerProxy
    from pypowsybl_mcp.server import pypowsybl_proxies, read_temp_resource

    resource_id = "test-resource"
    content = "# Test Content"
    session_id = "test-session"

    mock_ctx = MagicMock()
    mock_ctx.session.session_id = session_id

    proxy = PyPowsyblMCPServerProxy()
    proxy.resources[resource_id] = content
    pypowsybl_proxies[session_id] = proxy

    with patch(
        "pypowsybl_mcp.utils.user_session_management.get_session_id"
    ) as mock_get_session_id:
        mock_get_session_id.return_value = session_id
        result = read_temp_resource(resource_id, mock_ctx)
        assert result == content

    # Test not found
    with pytest.raises(ValueError, match="not found"):
        read_temp_resource("non-existent", mock_ctx)


def test_read_temp_resource_session_reattached_by_string_id():
    """Regression (issue #7, bug #5): a session reattached by its id as text
    must resolve to the same proxy.

    An orchestrator re-pins a generated session id (as it appears in the server
    log) onto a connection via set_session_id, which takes a str. If the cache
    is keyed by a UUID object instead, the lookup misses and the stored
    resources are unreachable.
    """
    from pypowsybl_mcp.proxy import PyPowsyblMCPServerProxy
    from pypowsybl_mcp.server import pypowsybl_proxies, read_temp_resource
    from pypowsybl_mcp.utils.user_session_management import get_session_id

    resource_id = "pypowsybl-network-load"
    content = "# How to load a network"

    ctx = MagicMock()
    del ctx.session.session_id
    generated_id = get_session_id(ctx)

    proxy = PyPowsyblMCPServerProxy()
    proxy.resources[resource_id] = content
    pypowsybl_proxies[generated_id] = proxy

    # A later connection pins that same id, read back as text.
    reattached_ctx = MagicMock()
    reattached_ctx.session.session_id = str(generated_id)

    try:
        assert read_temp_resource(resource_id, reattached_ctx) == content
    finally:
        pypowsybl_proxies.pop(generated_id, None)
        pypowsybl_proxies.pop(str(generated_id), None)
