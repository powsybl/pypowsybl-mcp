#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock

import pypowsybl as pp
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.creation_tools import CreationTools
from pypowsybl_mcp.tools.removal_tools import RemovalTools, register_removal_tools


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
def removal_tools(pypowsybl_proxies):
    return RemovalTools(pypowsybl_proxies)


@pytest.fixture
def creation_tools(pypowsybl_proxies):
    """Creation tools sharing the session, to build what is then removed."""
    return CreationTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.fixture
def bus_breaker_network(removal_tools):
    proxy = removal_tools.get_proxy("test-session")
    network = pp.network.create_ieee14()
    proxy.register_network("net1", network)
    return network


@pytest.fixture
def node_breaker_network(removal_tools):
    proxy = removal_tools.get_proxy("test-session")
    network = pp.network.create_four_substations_node_breaker_network()
    proxy.register_network("nb", network)
    return network


async def _datacenter_site(creation_tools, ctx):
    """A new site connected to IEEE14, as the creation tools would build it."""
    await creation_tools.create_substation("SUB_DC", country="FR", ctx=ctx)
    await creation_tools.create_voltage_level("VL_DC", "SUB_DC", 135.0, ctx=ctx)
    await creation_tools.create_load(
        "DATACENTER", "VL_DC_1_1", p0=300.0, q0=60.0, ctx=ctx
    )
    await creation_tools.create_line(
        "LINE_B4_DC", "B4", "VL_DC_1_1", r=0.5, x=5.0, ctx=ctx
    )


def test_register_removal_tools():
    mcp = MockMCP()
    proxies = TTLCache(maxsize=10, ttl=3600)
    register_removal_tools(mcp, proxies)
    assert "remove_network_elements" in mcp.tools
    assert "_remove_one" not in mcp.tools
    assert "_cascade_summary" not in mcp.tools


@pytest.mark.asyncio
async def test_remove_generator_with_its_bay(
    removal_tools, mock_ctx, bus_breaker_network
):
    result = await removal_tools.remove_network_elements(["B3-G"], ctx=mock_ctx)

    assert "removed generator 'B3-G' with its bay(s)" in result
    assert "B3-G" not in bus_breaker_network.get_generators().index


@pytest.mark.asyncio
async def test_remove_several_element_types_at_once(
    removal_tools, mock_ctx, bus_breaker_network
):
    transformer = bus_breaker_network.get_2_windings_transformers().index[0]

    result = await removal_tools.remove_network_elements(
        ["B2-L", "L1-2-1", transformer], ctx=mock_ctx
    )

    assert "removed load 'B2-L'" in result
    assert "removed line 'L1-2-1'" in result
    assert f"removed two_windings_transformer '{transformer}'" in result
    assert "B2-L" not in bus_breaker_network.get_loads().index
    assert "L1-2-1" not in bus_breaker_network.get_lines().index
    assert transformer not in bus_breaker_network.get_2_windings_transformers().index


@pytest.mark.asyncio
async def test_remove_load_bay_cleans_up_switches(
    removal_tools, mock_ctx, node_breaker_network
):
    """In node/breaker, the bay's switching equipment goes with the element."""
    switches_before = len(node_breaker_network.get_switches())

    result = await removal_tools.remove_network_elements(
        ["LD1"], network_id="nb", ctx=mock_ctx
    )

    assert "removed load 'LD1' with its bay(s)" in result
    assert len(node_breaker_network.get_switches()) < switches_before


@pytest.mark.asyncio
async def test_remove_hvdc_line_takes_its_converter_stations(
    removal_tools, mock_ctx, node_breaker_network
):
    result = await removal_tools.remove_network_elements(
        ["HVDC1"], network_id="nb", ctx=mock_ctx
    )

    assert "removed hvdc line 'HVDC1' with its converter stations" in result
    assert "HVDC1" not in node_breaker_network.get_hvdc_lines().index


@pytest.mark.asyncio
async def test_remove_voltage_level_needs_cascade(
    removal_tools, creation_tools, mock_ctx, bus_breaker_network
):
    await _datacenter_site(creation_tools, mock_ctx)

    result = await removal_tools.remove_network_elements(["VL_DC"], ctx=mock_ctx)

    assert "REFUSED voltage level 'VL_DC'" in result
    # The refusal quantifies what the cascade would take down.
    assert "connectable(s)" in result
    assert "LINE_B4_DC" in result
    assert "cascade=True" in result
    assert "VL_DC" in bus_breaker_network.get_voltage_levels().index
    assert "DATACENTER" in bus_breaker_network.get_loads().index


@pytest.mark.asyncio
async def test_remove_substation_needs_cascade(
    removal_tools, creation_tools, mock_ctx, bus_breaker_network
):
    await _datacenter_site(creation_tools, mock_ctx)

    result = await removal_tools.remove_network_elements(["SUB_DC"], ctx=mock_ctx)

    assert "REFUSED substation 'SUB_DC'" in result
    assert "VL_DC" in result
    assert "SUB_DC" in bus_breaker_network.get_substations().index


@pytest.mark.asyncio
async def test_cascade_removes_a_whole_site_in_any_order(
    removal_tools, creation_tools, mock_ctx, bus_breaker_network
):
    """Feeders are removed before the containers, whatever the order given."""
    await _datacenter_site(creation_tools, mock_ctx)

    result = await removal_tools.remove_network_elements(
        ["SUB_DC", "VL_DC", "DATACENTER", "LINE_B4_DC"], cascade=True, ctx=mock_ctx
    )

    assert "removed load 'DATACENTER'" in result
    assert "removed line 'LINE_B4_DC'" in result
    assert "removed voltage level 'VL_DC'" in result
    assert "removed substation 'SUB_DC'" in result
    assert "NOT FOUND" not in result
    assert "SUB_DC" not in bus_breaker_network.get_substations().index
    assert "VL_DC" not in bus_breaker_network.get_voltage_levels().index
    # The original case is intact and still solvable.
    assert pp.loadflow.run_ac(bus_breaker_network)[0].status == (
        pp.loadflow.ComponentStatus.CONVERGED
    )


@pytest.mark.asyncio
async def test_cascade_removes_substation_and_its_voltage_levels(
    removal_tools, creation_tools, mock_ctx, bus_breaker_network
):
    """A substation is removed by emptying it first."""
    await creation_tools.create_substation("SUB_DC", ctx=mock_ctx)
    await creation_tools.create_voltage_level("VL_DC", "SUB_DC", 135.0, ctx=mock_ctx)

    result = await removal_tools.remove_network_elements(
        ["SUB_DC"], cascade=True, ctx=mock_ctx
    )

    assert "removed substation 'SUB_DC' and its voltage level(s) VL_DC" in result
    assert "SUB_DC" not in bus_breaker_network.get_substations().index
    assert "VL_DC" not in bus_breaker_network.get_voltage_levels().index


@pytest.mark.asyncio
async def test_remove_reports_unknown_id_without_aborting(
    removal_tools, mock_ctx, bus_breaker_network
):
    result = await removal_tools.remove_network_elements(["NOPE", "B3-G"], ctx=mock_ctx)

    assert "NOT FOUND 'NOPE'" in result
    assert "removed generator 'B3-G'" in result
    assert "B3-G" not in bus_breaker_network.get_generators().index


@pytest.mark.asyncio
async def test_remove_refuses_the_network_itself(
    removal_tools, mock_ctx, bus_breaker_network
):
    result = await removal_tools.remove_network_elements(["ieee14cdf"], ctx=mock_ctx)

    assert "this is the network itself" in result
    assert "remove_network()" in result


@pytest.mark.asyncio
async def test_remove_accepts_a_single_id_as_string(
    removal_tools, mock_ctx, bus_breaker_network
):
    """Clients that pass one id instead of a list are tolerated."""
    result = await removal_tools.remove_network_elements("B3-G", ctx=mock_ctx)

    assert "removed generator 'B3-G'" in result


@pytest.mark.asyncio
async def test_remove_without_ids(removal_tools, mock_ctx, bus_breaker_network):
    result = await removal_tools.remove_network_elements(["", "  "], ctx=mock_ctx)

    assert "No element id provided" in result


@pytest.mark.asyncio
async def test_remove_invalidates_loadflow_results(
    removal_tools, mock_ctx, bus_breaker_network
):
    proxy = removal_tools.get_proxy("test-session")
    proxy.loadflow_results["net1"] = "stale results"

    await removal_tools.remove_network_elements(["B3-G"], ctx=mock_ctx)

    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_remove_without_network(removal_tools, mock_ctx):
    result = await removal_tools.remove_network_elements(["B3-G"], ctx=mock_ctx)

    assert "No network specified" in result


@pytest.mark.asyncio
async def test_remove_on_unknown_network(removal_tools, mock_ctx, bus_breaker_network):
    result = await removal_tools.remove_network_elements(
        ["B3-G"], network_id="unknown", ctx=mock_ctx
    )

    assert "Network 'unknown' not found" in result


@pytest.mark.asyncio
async def test_remove_falls_back_to_remove_elements(
    removal_tools, mock_ctx, node_breaker_network
):
    """Types with no dedicated removal (a switch here) go through remove_elements."""
    switch_id = node_breaker_network.get_switches().index[0]

    result = await removal_tools.remove_network_elements(
        [switch_id], network_id="nb", ctx=mock_ctx
    )

    assert f"removed switch '{switch_id}'" in result
    assert switch_id not in node_breaker_network.get_switches().index


@pytest.mark.asyncio
async def test_pypowsybl_rejection_is_reported_per_element(
    removal_tools, mock_ctx, bus_breaker_network, monkeypatch
):
    """One element refused by pypowsybl does not abort the rest of the batch."""
    real_remove = pp.network.remove_feeder_bays

    def selective_failure(network, connectable_ids, **kwargs):
        if "B2-L" in connectable_ids:
            raise pp.PyPowsyblError("removal refused")
        return real_remove(network, connectable_ids=connectable_ids, **kwargs)

    monkeypatch.setattr("pypowsybl.network.remove_feeder_bays", selective_failure)

    result = await removal_tools.remove_network_elements(["B2-L", "B3-G"], ctx=mock_ctx)

    assert "FAILED 'B2-L': removal refused" in result
    assert "removed generator 'B3-G'" in result
    assert "B2-L" in bus_breaker_network.get_loads().index
    assert "B3-G" not in bus_breaker_network.get_generators().index


@pytest.mark.asyncio
async def test_bus_view_bus_is_not_an_element_to_remove(
    removal_tools, mock_ctx, bus_breaker_network
):
    """'VL1_0' is computed from the topology: it is not an identifiable."""
    result = await removal_tools.remove_network_elements(["VL1_0"], ctx=mock_ctx)

    assert "NOT FOUND 'VL1_0'" in result
