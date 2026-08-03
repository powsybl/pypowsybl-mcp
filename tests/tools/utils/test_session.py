#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import os
from unittest.mock import MagicMock, patch

import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.utils.session import register_session_tools


class MockContext:
    def __init__(self, session_id="test-session"):
        self.session = MagicMock()
        self.session.session_id = session_id


class MockMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


@pytest.fixture
def mcp():
    return MockMCP()


@pytest.fixture
def pypowsybl_proxies():
    return TTLCache(maxsize=10, ttl=3600)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.mark.asyncio
async def test_set_session_id_success(mcp, pypowsybl_proxies, mock_ctx):
    register_session_tools(mcp, pypowsybl_proxies)

    set_session_id_func = mcp.tools["set_session_id"]

    with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
        result = await set_session_id_func(
            authorization_token="secret-token", session_id="new-session", ctx=mock_ctx
        )

        assert result == "Session ID set to new-session"
        assert mock_ctx.session.session_id == "new-session"


@pytest.mark.asyncio
async def test_set_session_id_invalid_token(mcp, pypowsybl_proxies, mock_ctx):
    register_session_tools(mcp, pypowsybl_proxies)
    set_session_id_func = mcp.tools["set_session_id"]

    with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
        result = await set_session_id_func(
            authorization_token="wrong-token", session_id="new-session", ctx=mock_ctx
        )

        assert "Invalid authorization token" in result
        assert mock_ctx.session.session_id == "test-session"


@pytest.mark.asyncio
async def test_duplicate_session_success(mcp, pypowsybl_proxies, mock_ctx):
    register_session_tools(mcp, pypowsybl_proxies)
    duplicate_session_func = mcp.tools["duplicate_session"]

    mock_proxy = MagicMock()
    mock_proxy.copy.return_value = MagicMock()
    pypowsybl_proxies["source-session"] = mock_proxy

    with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
        result = await duplicate_session_func(
            authorization_token="secret-token",
            source_session_id="source-session",
            target_session_id="target-session",
            ctx=mock_ctx,
        )

        assert "duplicated to 'target-session'" in result
        assert "target-session" in pypowsybl_proxies
        mock_proxy.copy.assert_called_once()


@pytest.mark.asyncio
async def test_duplicate_session_source_not_found(mcp, pypowsybl_proxies, mock_ctx):
    register_session_tools(mcp, pypowsybl_proxies)
    duplicate_session_func = mcp.tools["duplicate_session"]

    with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
        result = await duplicate_session_func(
            authorization_token="secret-token",
            source_session_id="non-existent",
            target_session_id="target-session",
            ctx=mock_ctx,
        )

        assert "Error: Source session 'non-existent' not found" in result


@pytest.mark.asyncio
async def test_duplicate_session_target_exists_no_overwrite(
    mcp, pypowsybl_proxies, mock_ctx
):
    register_session_tools(mcp, pypowsybl_proxies)
    duplicate_session_func = mcp.tools["duplicate_session"]

    pypowsybl_proxies["source-session"] = MagicMock()
    pypowsybl_proxies["target-session"] = MagicMock()

    with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
        result = await duplicate_session_func(
            authorization_token="secret-token",
            source_session_id="source-session",
            target_session_id="target-session",
            overwrite=False,
            ctx=mock_ctx,
        )

        assert "already exists and overwrite is False" in result


@pytest.mark.asyncio
async def test_set_session_id_no_context(mcp, pypowsybl_proxies):
    register_session_tools(mcp, pypowsybl_proxies)
    set_session_id_func = mcp.tools["set_session_id"]

    with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
        result = await set_session_id_func(
            authorization_token="secret-token", session_id="new-session", ctx=None
        )

        assert result == "Error: Could not set session ID (no context)"


@pytest.mark.asyncio
async def test_duplicate_session_invalid_token(mcp, pypowsybl_proxies, mock_ctx):
    register_session_tools(mcp, pypowsybl_proxies)
    duplicate_session_func = mcp.tools["duplicate_session"]

    with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
        result = await duplicate_session_func(
            authorization_token="wrong-token",
            source_session_id="source-session",
            target_session_id="target-session",
            ctx=mock_ctx,
        )

        assert "Invalid authorization token" in result


@pytest.mark.asyncio
async def test_get_pypowsybl_version(mcp, pypowsybl_proxies):
    register_session_tools(mcp, pypowsybl_proxies)
    get_version_func = mcp.tools["get_pypowsybl_version"]

    with patch("pypowsybl.__version__", "1.2.3"):
        result = await get_version_func()
        assert "pypowsybl version: 1.2.3" in result
