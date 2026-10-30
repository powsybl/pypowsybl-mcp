#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The plugin as the host sees it: entry points, tools, skill and admin route."""

import importlib.metadata

import pytest
from cachetools import TTLCache
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from pypowsybl_mcp.plugins import (
    PLUGIN_GROUP,
    RESOURCE_PLUGIN_GROUP,
    discover_and_load_plugins,
)
from pypowsybl_mcp.plugins.grid_extension import plugin
from pypowsybl_mcp.plugins.grid_extension.creation import drift

TOKEN = "test-admin-token"
TOOLS = {
    "describe_element_creation",
    "create_network_element",
    "create_network_elements",
    "remove_network_elements",
}


@pytest.fixture
def proxies():
    return TTLCache(maxsize=10, ttl=3600)


@pytest.fixture
def client(proxies, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(drift, "LAST_REPORT", None)
    mcp = MCPServer("TestServer")
    plugin.register_plugin_tools(mcp, proxies)
    return TestClient(mcp.streamable_http_app())


@pytest.mark.parametrize("group", [PLUGIN_GROUP, RESOURCE_PLUGIN_GROUP])
def test_the_plugin_is_installed_under_both_groups(group):
    """Installed with the server, so discovered without any configuration."""
    entry_points = {ep.name: ep for ep in importlib.metadata.entry_points(group=group)}

    assert (
        entry_points["grid_extension"].module
        == "pypowsybl_mcp.plugins.grid_extension.plugin"
    )


@pytest.mark.asyncio
async def test_the_host_loads_the_creation_and_removal_tools(proxies, monkeypatch):
    monkeypatch.delenv("MCP_DISABLED_PLUGINS", raising=False)
    mcp = MCPServer("TestServer")

    discover_and_load_plugins(mcp, proxies)

    assert TOOLS <= {tool.name for tool in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_the_plugin_can_be_disabled(proxies, monkeypatch):
    monkeypatch.setenv("MCP_DISABLED_PLUGINS", "grid_extension")
    mcp = MCPServer("TestServer")

    discover_and_load_plugins(mcp, proxies)

    assert not TOOLS & {tool.name for tool in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_the_skill_is_a_resource_and_a_prompt(proxies):
    mcp = MCPServer("TestServer")

    plugin.register_plugin_resources(mcp, proxies)

    resources = {str(item.uri) for item in await mcp.list_resources()}
    assert "skills://skills/grid-extension" in resources
    assert "grid-extension" in {item.name for item in await mcp.list_prompts()}


def test_creation_coverage_requires_the_token(client):
    assert client.get("/admin/creation-coverage").status_code == 401


def test_creation_coverage_serves_the_startup_report(client, monkeypatch):
    monkeypatch.setattr(drift, "LAST_REPORT", {"status": "changed"})

    response = client.get(
        "/admin/creation-coverage", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "changed"}


def test_creation_coverage_without_a_startup_check(client, monkeypatch):
    """No stored report: a fresh one, which changes nothing."""
    monkeypatch.setattr(drift, "LAST_REPORT", None)

    payload = client.get(
        "/admin/creation-coverage", headers={"X-Admin-Token": TOKEN}
    ).json()

    assert payload["status"] == "verified"
    assert payload["pypowsybl_version"] == payload["verified_against"]
    assert drift.LAST_REPORT is None
