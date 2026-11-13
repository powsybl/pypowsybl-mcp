#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import json
from unittest.mock import MagicMock

import pypowsybl as pp
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.plugins.grid_extension.creation.topology import OPERATIONS
from pypowsybl_mcp.plugins.grid_extension.topology_tools import (
    TopologyTools,
    register_topology_tools,
)

KEYS = {
    "success",
    "network_id",
    "operation",
    "methods",
    "created_ids",
    "removed_ids",
    "switches",
    "notes",
    "errors",
    "message",
    "loadflow_invalidated",
}
CUT = {"bbs_or_bus_id": "S4VL1_BBS", "line_id": "LINE_S2S3", "position_percent": 40}


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
def tools():
    return TopologyTools(TTLCache(maxsize=10, ttl=3600))


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.fixture
def network(tools):
    network = pp.network.create_four_substations_node_breaker_network()
    tools.get_proxy("test-session").register_network("nb", network)
    return network


def test_register_topology_tools():
    mcp = MockMCP()
    register_topology_tools(mcp, TTLCache(maxsize=10, ttl=3600))

    assert set(mcp.tools) == {"modify_network_topology"}


def test_the_description_lists_every_operation_with_its_attributes():
    description = TopologyTools.modify_network_topology.__doc__

    for name, operation in OPERATIONS.items():
        assert f"{name}: {operation.summary}" in description
        assert f"required: {', '.join(operation.required)}" in description
    assert "{operations}" not in description
    assert '{"bbs_or_bus_id": "S4VL1_BBS"' in description


@pytest.mark.asyncio
async def test_a_cut_and_its_revert(tools, mock_ctx, network):
    proxy = tools.get_proxy("test-session")
    proxy.invalidate_loadflow = MagicMock()

    cut = json.loads(
        await tools.modify_network_topology(
            "connect_voltage_level_on_line", CUT, ctx=mock_ctx
        )
    )

    assert set(cut) == KEYS
    assert cut["success"]
    assert cut["network_id"] == "nb"
    assert cut["methods"] == ["pp.network.connect_voltage_level_on_line"]
    assert cut["created_ids"] == ["LINE_S2S3_1", "LINE_S2S3_2"]
    assert cut["removed_ids"] == ["LINE_S2S3"]
    assert cut["switches"] == {"created": 4, "removed": 0, "orphans_removed": []}
    assert cut["loadflow_invalidated"]
    proxy.invalidate_loadflow.assert_called_with("nb")

    merged = json.loads(
        await tools.modify_network_topology(
            "revert_connect_voltage_level_on_line",
            {
                "line1_id": "LINE_S2S3_1",
                "line2_id": "LINE_S2S3_2",
                "line_id": "LINE_S2S3",
            },
            ctx=mock_ctx,
        )
    )

    assert merged["success"]
    assert merged["created_ids"] == ["LINE_S2S3"]
    # since pypowsybl 1.16 the revert removes the bays itself: no orphans left
    assert merged["switches"]["removed"] == 4
    assert merged["switches"]["orphans_removed"] == []
    assert "LINE_S2S3" in network.get_lines().index


@pytest.mark.asyncio
async def test_a_refusal_carries_the_descriptor(tools, mock_ctx, network):
    proxy = tools.get_proxy("test-session")
    proxy.invalidate_loadflow = MagicMock()

    reply = json.loads(
        await tools.modify_network_topology(
            "connect_voltage_level_on_line",
            {**CUT, "bbs_or_bus_id": "S1VL1_BBS"},
            ctx=mock_ctx,
        )
    )

    assert set(reply) == KEYS
    assert not reply["success"]
    assert not reply["loadflow_invalidated"]
    assert reply["methods"] == ["pp.network.connect_voltage_level_on_line"]
    (error,) = reply["errors"]
    assert "a line cannot change voltage" in error["problem"]
    assert error["hint"]["required"] == ["bbs_or_bus_id", "line_id"]
    assert reply["message"].startswith("Failed to apply connect_voltage_level_on_line")
    proxy.invalidate_loadflow.assert_not_called()
    assert "LINE_S2S3" in network.get_lines().index


@pytest.mark.asyncio
async def test_an_unknown_operation_lists_the_others(tools, mock_ctx, network):
    reply = json.loads(
        await tools.modify_network_topology("connect_vl_on_line", CUT, ctx=mock_ctx)
    )

    assert not reply["success"]
    assert reply["methods"] == []
    (error,) = reply["errors"]
    assert "Did you mean 'connect_voltage_level_on_line'" in error["problem"]
    assert error["hint"]["operations"] == sorted(OPERATIONS)


@pytest.mark.asyncio
async def test_a_pypowsybl_failure_invalidates_the_load_flow(
    tools, mock_ctx, network, monkeypatch
):
    proxy = tools.get_proxy("test-session")
    proxy.invalidate_loadflow = MagicMock()

    def refuse(*args, **kwargs):
        raise pp.PyPowsyblError("half way")

    monkeypatch.setattr(pp.network, "connect_voltage_level_on_line", refuse)

    reply = json.loads(
        await tools.modify_network_topology(
            "connect_voltage_level_on_line", CUT, ctx=mock_ctx
        )
    )

    assert not reply["success"]
    assert reply["loadflow_invalidated"]
    assert reply["errors"] == [{"problem": "half way", "hint": None}]
    assert reply["created_ids"] == reply["removed_ids"] == []
    assert reply["message"] == "Failed to apply connect_voltage_level_on_line: half way"
    proxy.invalidate_loadflow.assert_called_with("nb")


@pytest.mark.asyncio
async def test_a_failure_lists_what_pypowsybl_had_already_changed(
    tools, mock_ctx, network, monkeypatch
):
    real = pp.network.connect_voltage_level_on_line

    def fails_half_way(network, **kwargs):
        real(network, **kwargs)
        raise pp.PyPowsyblError("half way")

    monkeypatch.setattr(pp.network, "connect_voltage_level_on_line", fails_half_way)

    reply = json.loads(
        await tools.modify_network_topology(
            "connect_voltage_level_on_line", CUT, ctx=mock_ctx
        )
    )

    assert set(reply) == KEYS
    assert not reply["success"]
    assert reply["created_ids"] == ["LINE_S2S3_1", "LINE_S2S3_2"]
    assert reply["removed_ids"] == ["LINE_S2S3"]
    assert reply["switches"]["created"] == 4
    assert "pypowsybl had already changed the network" in reply["message"]
    assert "not as it was before this call" in reply["notes"][0]
    assert reply["loadflow_invalidated"]


@pytest.mark.asyncio
async def test_without_a_network(tools, mock_ctx):
    reply = json.loads(
        await tools.modify_network_topology(
            "connect_voltage_level_on_line", CUT, ctx=mock_ctx
        )
    )

    assert set(reply) == KEYS
    assert not reply["success"]
    assert reply["errors"]
