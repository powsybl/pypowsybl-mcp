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

from pypowsybl_mcp.tools.creation_tools import (
    CHEAT_SHEET,
    CreationTools,
    register_creation_tools,
)


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
def pypowsybl_proxies():
    return TTLCache(maxsize=10, ttl=3600)


@pytest.fixture
def creation_tools(pypowsybl_proxies):
    return CreationTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.fixture
def network(creation_tools):
    proxy = creation_tools.get_proxy("test-session")
    net = pp.network.create_ieee14()
    proxy.register_network("net1", net)
    return net


DATACENTER_SITE = [
    {"element_type": "substation", "id": "SUB_DC", "country": "FR"},
    {
        "element_type": "voltage_level",
        "id": "VL_DC",
        "substation_id": "SUB_DC",
        "nominal_v": 135.0,
        "low_voltage_limit": 128.0,
        "high_voltage_limit": 145.0,
    },
    {
        "element_type": "load",
        "id": "DATACENTER",
        "bus_or_busbar_section_id": "VL_DC_1_1",
        "p0": 300.0,
        "q0": 60.0,
    },
    {
        "element_type": "line",
        "id": "LINE_B4_DC",
        "bus_or_busbar_section_id_1": "B4",
        "bus_or_busbar_section_id_2": "VL_DC_1_1",
        "r": 0.5,
        "x": 5.0,
    },
    {
        "element_type": "operational_limits",
        "element_id": "LINE_B4_DC",
        "permanent_limit": 800.0,
    },
]


def test_register_creation_tools():
    mcp = MockMCP()
    register_creation_tools(mcp, TTLCache(maxsize=10, ttl=3600))

    assert set(mcp.tools) == {
        "describe_element_creation",
        "create_network_element",
        "create_network_elements",
    }


def test_the_cheat_sheet_is_inlined_in_the_tool_description():
    """The common cases must not need a describe_element_creation() round trip."""
    description = CreationTools.create_network_element.__doc__

    assert "{cheat_sheet}" not in description
    assert CHEAT_SHEET.strip() in description
    assert "bus_or_busbar_section_id" in description


@pytest.mark.asyncio
async def test_describe_lists_the_element_types(creation_tools, mock_ctx):
    listing = json.loads(await creation_tools.describe_element_creation(ctx=mock_ctx))

    names = [item["element_type"] for item in listing["element_types"]]
    assert {"load", "generator", "line", "operational_limits"} <= set(names)
    assert "create_network_elements" in listing["usage"]


@pytest.mark.asyncio
async def test_describe_one_element_type(creation_tools, mock_ctx):
    described = json.loads(
        await creation_tools.describe_element_creation("load", ctx=mock_ctx)
    )

    assert described["element_type"] == "load"
    assert [item["name"] for item in described["required"]] == ["p0"]
    assert described["required"][0]["unit"] == "MW"
    assert described["connection"]["attributes"] == ["bus_or_busbar_section_id"]


@pytest.mark.asyncio
async def test_describe_an_unknown_type_suggests_and_lists(creation_tools, mock_ctx):
    answer = await creation_tools.describe_element_creation("laod", ctx=mock_ctx)

    assert "Cannot describe that element type" in answer
    assert "'load'" in answer
    assert "supported_element_types" in answer


@pytest.mark.asyncio
async def test_create_one_element(creation_tools, mock_ctx, network):
    result = await creation_tools.create_network_element(
        "load",
        {"id": "DATACENTER", "bus_or_busbar_section_id": "B4", "p0": 300.0},
        ctx=mock_ctx,
    )

    assert "In network 'net1'" in result
    assert "created load 'DATACENTER'" in result
    assert network.get_loads().loc["DATACENTER", "p0"] == 300.0


@pytest.mark.asyncio
async def test_a_rejection_carries_the_descriptor(creation_tools, mock_ctx, network):
    """The mechanism that replaces a per-tool JSON schema."""
    result = await creation_tools.create_network_element(
        "load", {"id": "X", "bus_or_busbar_section_id": "B4"}, ctx=mock_ctx
    )

    assert "Failed to create a load" in result
    assert "needs 'p0'" in result
    # the descriptor comes back with the error, as JSON the caller can read
    hint = json.loads(result.split("\n", 1)[1])
    assert hint["element_type"] == "load"
    assert [item["name"] for item in hint["required"]] == ["p0"]


@pytest.mark.asyncio
async def test_create_clears_cached_loadflow_results(creation_tools, mock_ctx, network):
    proxy = creation_tools.get_proxy("test-session")
    proxy.loadflow_results["net1"] = "stale results"

    await creation_tools.create_network_element(
        "load", {"id": "X", "bus": "B4", "p0": 1.0}, ctx=mock_ctx
    )

    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_create_a_whole_site_in_one_call(creation_tools, mock_ctx, network):
    result = await creation_tools.create_network_elements(DATACENTER_SITE, ctx=mock_ctx)

    assert "created substation 'SUB_DC'" in result
    assert "connection points: VL_DC_1_1" in result
    assert "created load 'DATACENTER'" in result
    assert "set operational_limits on line 'LINE_B4_DC'" in result
    assert "Run run_loadflow()" in result
    assert "DATACENTER" in network.get_loads().index
    assert len(network.get_operational_limits().loc[["LINE_B4_DC"]]) == 2


@pytest.mark.asyncio
async def test_a_site_created_in_one_call_is_a_valid_network(
    creation_tools, mock_ctx, network
):
    """The end-to-end scenario the tools exist for."""
    await creation_tools.create_network_elements(
        [
            *DATACENTER_SITE[:2],
            {
                "element_type": "load",
                "id": "DATACENTER",
                "bus": "VL_DC_1_1",
                "p0": 80.0,
                "q0": 15.0,
            },
            DATACENTER_SITE[3],
            DATACENTER_SITE[4],
        ],
        ctx=mock_ctx,
    )

    results = pp.loadflow.run_ac(network)

    assert results[0].status == pp.loadflow.ComponentStatus.CONVERGED
    assert network.get_lines().loc["LINE_B4_DC", "p1"] > 0


@pytest.mark.asyncio
async def test_a_batch_with_a_typo_creates_nothing(creation_tools, mock_ctx, network):
    result = await creation_tools.create_network_elements(
        [
            {"element_type": "substation", "id": "SUB_DC"},
            {"element_type": "load", "id": "X", "bus": "B4", "pO": 1.0},
        ],
        ctx=mock_ctx,
    )

    assert "Nothing was created" in result
    assert "SUB_DC" not in network.get_substations().index


@pytest.mark.asyncio
async def test_a_batch_stopped_midway_says_how_far_it_got(
    creation_tools, mock_ctx, network
):
    result = await creation_tools.create_network_elements(
        [
            {"element_type": "substation", "id": "SUB_DC"},
            {"element_type": "load", "id": "X", "bus": "NOPE", "p0": 1.0},
        ],
        ctx=mock_ctx,
    )

    assert "created substation 'SUB_DC'" in result
    assert "STOPPED" in result
    assert "1 item(s) not created" in result
    assert "SUB_DC" in network.get_substations().index


@pytest.mark.asyncio
async def test_an_empty_batch(creation_tools, mock_ctx, network):
    assert "No element to create" in await creation_tools.create_network_elements(
        [], ctx=mock_ctx
    )


@pytest.mark.asyncio
async def test_without_a_network(creation_tools, mock_ctx):
    assert "No network specified" in await creation_tools.create_network_element(
        "load", {"id": "X", "bus": "B4", "p0": 1.0}, ctx=mock_ctx
    )
    assert "No network specified" in await creation_tools.create_network_elements(
        DATACENTER_SITE, ctx=mock_ctx
    )


@pytest.mark.asyncio
async def test_on_an_unknown_network(creation_tools, mock_ctx, network):
    result = await creation_tools.create_network_element(
        "load", {"id": "X", "bus": "B4", "p0": 1.0}, network_id="nope", ctx=mock_ctx
    )

    assert "Network 'nope' not found" in result


@pytest.mark.asyncio
async def test_a_pypowsybl_rejection_is_reported(
    creation_tools, mock_ctx, network, monkeypatch
):
    def failing(*args, **kwargs):
        raise pp.PyPowsyblError("bay refused")

    monkeypatch.setattr("pypowsybl.network.create_load_bay", failing)

    result = await creation_tools.create_network_element(
        "load", {"id": "X", "bus": "B4", "p0": 1.0}, ctx=mock_ctx
    )

    assert "Failed to create a load" in result
    assert "bay refused" in result


@pytest.mark.asyncio
async def test_a_pypowsybl_rejection_stops_a_batch(
    creation_tools, mock_ctx, network, monkeypatch
):
    def failing(*args, **kwargs):
        raise pp.PyPowsyblError("bay refused")

    monkeypatch.setattr("pypowsybl.network.create_load_bay", failing)

    result = await creation_tools.create_network_elements(
        [
            {"element_type": "substation", "id": "SUB_DC"},
            {"element_type": "load", "id": "X", "bus": "B4", "p0": 1.0},
        ],
        ctx=mock_ctx,
    )

    assert "STOPPED" in result
    assert "bay refused" in result
