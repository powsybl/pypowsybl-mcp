#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from pypowsybl_mcp.admin import build_snapshot, register_admin_routes
from pypowsybl_mcp.utils.cachetools import ThreadSafeTTLCache
from pypowsybl_mcp.utils.instrumentation import instrument_tool_calls
from pypowsybl_mcp.utils.session_registry import SessionRegistry

TOKEN = "test-admin-token"


def _proxy(network_ids=(), current=None):
    """A stand-in for PyPowsyblMCPServerProxy: only what the API reads."""
    return SimpleNamespace(
        networks={network_id: MagicMock() for network_id in network_ids},
        current_network_id=current,
        loadflow_results={"lf1": object()},
        resources={},
        plugin_results={},
        lf_provider="OpenLoadFlow",
    )


@pytest.fixture
def proxies():
    return ThreadSafeTTLCache(maxsize=3, ttl=1000)


@pytest.fixture
def registry():
    return SessionRegistry()


@pytest.fixture
def client(proxies, registry, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    mcp = FastMCP("TestServer")
    register_admin_routes(mcp, proxies, registry)
    return TestClient(mcp.streamable_http_app())


def test_health_needs_no_token_and_hides_session_detail(client, proxies, registry):
    proxies["s1"] = _proxy()
    registry.touch("s1")

    payload = client.get("/admin/health").json()

    assert payload["status"] == "ok"
    assert payload["server"]["sessions"] == 1
    assert payload["server"]["max_sessions"] == 3
    assert payload["server"]["session_ttl_s"] == 1000
    assert "sessions" not in payload or isinstance(payload["sessions"], int)


def test_sessions_requires_the_token(client):
    assert client.get("/admin/sessions").status_code == 401
    assert (
        client.get(
            "/admin/sessions", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_sessions_is_disabled_without_a_configured_token(
    proxies, registry, monkeypatch
):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    mcp = FastMCP("TestServer")
    register_admin_routes(mcp, proxies, registry)

    response = TestClient(mcp.streamable_http_app()).get(
        "/admin/sessions", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 403
    assert "MCP_AUTH_TOKEN" in response.json()["error"]


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": f"Bearer {TOKEN}"}, {"X-Admin-Token": TOKEN}],
)
def test_sessions_reports_what_a_session_holds(client, proxies, registry, headers):
    proxies["s1"] = _proxy(network_ids=("ieee14", "ieee30"), current="ieee14")
    registry.touch("s1")
    registry.record_call("s1", "run_loadflow", duration_ms=100.0)
    registry.record_call("s1", "run_loadflow", duration_ms=300.0, error=True)

    response = client.get("/admin/sessions", headers=headers)

    assert response.status_code == 200
    session = response.json()["sessions"][0]
    assert session["session_id"] == "s1"
    assert session["current_network_id"] == "ieee14"
    assert {network["id"] for network in session["networks"]} == {"ieee14", "ieee30"}
    assert [n["current"] for n in session["networks"] if n["id"] == "ieee14"] == [True]
    assert session["tool_calls"] == 2
    assert session["errors"] == 1
    assert session["last_tool"] == "run_loadflow"
    assert session["total_duration_ms"] == 400.0
    assert session["avg_duration_ms"] == 200.0
    assert session["max_duration_ms"] == 300.0
    assert session["last_duration_ms"] == 300.0
    assert session["loadflow_results"] == 1
    # TTL is counted from creation, not from last use.
    assert 0 < session["expires_in_s"] <= 1000


def test_element_counts_are_opt_in(client, proxies, registry):
    proxy = _proxy(network_ids=("ieee14",), current="ieee14")
    proxy.networks["ieee14"].get_buses.return_value = [1, 2, 3]
    proxy.networks["ieee14"].get_lines.return_value = [1, 2]
    proxy.networks["ieee14"].get_generators.return_value = [1]
    proxy.networks["ieee14"].get_loads.return_value = []
    proxies["s1"] = proxy
    headers = {"Authorization": f"Bearer {TOKEN}"}

    default = client.get("/admin/sessions", headers=headers).json()
    assert "buses" not in default["sessions"][0]["networks"][0]

    full = client.get("/admin/sessions?networks=full", headers=headers).json()
    network = full["sessions"][0]["networks"][0]
    assert (network["buses"], network["lines"], network["generators"]) == (3, 2, 1)


def test_snapshot_reaps_expired_sessions(proxies, registry):
    clock = [0.0]
    proxies = ThreadSafeTTLCache(maxsize=3, ttl=10, timer=lambda: clock[0])
    proxies["s1"] = _proxy()
    registry.touch("s1")
    clock[0] = 11

    snapshot = build_snapshot(proxies, registry)

    assert snapshot["sessions"] == []
    assert snapshot["server"]["sessions"] == 0


@pytest.mark.asyncio
async def test_tool_calls_are_attributed_to_the_calling_session(registry):
    mcp = FastMCP("TestServer")

    @mcp.tool()
    def failing_tool() -> str:
        raise RuntimeError("boom")

    @mcp.tool()
    def working_tool() -> str:
        return "fine"

    instrument_tool_calls(mcp, registry)
    context = SimpleNamespace(session=SimpleNamespace(session_id="s1"))

    await mcp._tool_manager.call_tool("working_tool", {}, context=context)
    with pytest.raises(ToolError):
        await mcp._tool_manager.call_tool("failing_tool", {}, context=context)

    stats = registry.snapshot()["s1"]
    assert stats.tool_calls == 2
    assert stats.errors == 1
    assert stats.tools_used == {"working_tool": 1, "failing_tool": 1}
    # Both calls were timed, the one that raised included.
    assert stats.total_duration_ms > 0
    assert stats.last_duration_ms is not None


@pytest.mark.asyncio
async def test_instrumentation_is_applied_once(registry):
    mcp = FastMCP("TestServer")

    @mcp.tool()
    def working_tool() -> str:
        return "fine"

    instrument_tool_calls(mcp, registry)
    instrument_tool_calls(mcp, registry)
    context = SimpleNamespace(session=SimpleNamespace(session_id="s1"))

    await mcp._tool_manager.call_tool("working_tool", {}, context=context)

    assert registry.snapshot()["s1"].tool_calls == 1


@pytest.mark.asyncio
async def test_a_call_without_a_session_is_not_attributed(registry):
    mcp = FastMCP("TestServer")

    @mcp.tool()
    def working_tool() -> str:
        return "fine"

    instrument_tool_calls(mcp, registry)

    await mcp._tool_manager.call_tool("working_tool", {}, context=None)

    assert registry.snapshot() == {}
