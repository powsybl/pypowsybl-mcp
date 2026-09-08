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
def bus_breaker_network(creation_tools):
    """IEEE14: a bus/breaker network whose configured buses are B1..B14."""
    proxy = creation_tools.get_proxy("test-session")
    network = pp.network.create_ieee14()
    proxy.register_network("net1", network)
    return network


@pytest.fixture
def node_breaker_network(creation_tools):
    """A node/breaker network, where bays attach to busbar sections."""
    proxy = creation_tools.get_proxy("test-session")
    network = pp.network.create_four_substations_node_breaker_network()
    proxy.register_network("nb", network)
    return network


async def _new_voltage_level(creation_tools, ctx, **kwargs):
    """Create the substation and voltage level a new site needs."""
    await creation_tools.create_substation("SUB_DC", country="FR", ctx=ctx)
    return await creation_tools.create_voltage_level(
        "VL_DC", "SUB_DC", 135.0, ctx=ctx, **kwargs
    )


def test_register_creation_tools():
    mcp = MockMCP()
    proxies = TTLCache(maxsize=10, ttl=3600)
    register_creation_tools(mcp, proxies)
    assert "create_substation" in mcp.tools
    assert "create_voltage_level" in mcp.tools
    assert "create_load" in mcp.tools
    assert "create_generator" in mcp.tools
    assert "create_line" in mcp.tools
    # private helpers must not be exposed
    assert "_resolve_connection_point" not in mcp.tools
    assert "_ensure_id_available" not in mcp.tools


@pytest.mark.asyncio
async def test_create_substation(creation_tools, mock_ctx, bus_breaker_network):
    result = await creation_tools.create_substation(
        "SUB_DC", country="FR", tso="RTE", name="Datacenter site", ctx=mock_ctx
    )

    assert "Created substation 'SUB_DC'" in result
    substations = bus_breaker_network.get_substations()
    assert "SUB_DC" in substations.index
    assert substations.loc["SUB_DC", "country"] == "FR"


@pytest.mark.asyncio
async def test_create_voltage_level_creates_connection_points(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await _new_voltage_level(
        creation_tools, mock_ctx, low_voltage_limit=128.0, high_voltage_limit=145.0
    )

    assert "Created voltage level 'VL_DC'" in result
    # The ids of the created buses are reported: they are what the other
    # creation tools take as connection point.
    assert "VL_DC_1_1" in result
    voltage_levels = bus_breaker_network.get_voltage_levels()
    assert voltage_levels.loc["VL_DC", "nominal_v"] == 135.0
    assert voltage_levels.loc["VL_DC", "low_voltage_limit"] == 128.0
    buses = bus_breaker_network.get_bus_breaker_view_buses()
    assert "VL_DC_1_1" in buses.index


@pytest.mark.asyncio
async def test_create_voltage_level_multiple_busbars_node_breaker(
    creation_tools, mock_ctx, node_breaker_network
):
    await creation_tools.create_substation("SUB_NEW", ctx=mock_ctx)
    result = await creation_tools.create_voltage_level(
        "VL_NEW",
        "SUB_NEW",
        400.0,
        topology_kind="NODE_BREAKER",
        busbar_count=2,
        section_count=2,
        ctx=mock_ctx,
    )

    assert "Created voltage level 'VL_NEW'" in result
    sections = node_breaker_network.get_busbar_sections()
    created = sections[sections["voltage_level_id"] == "VL_NEW"]
    # 2 busbars x 2 sections
    assert len(created) == 4


@pytest.mark.asyncio
async def test_create_load_on_existing_bus(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_load(
        "DATACENTER", "B4", p0=300.0, q0=60.0, ctx=mock_ctx
    )

    assert "Created load 'DATACENTER' (300.0 MW, 60.0 MVAr)" in result
    assert "voltage level 'VL4'" in result
    loads = bus_breaker_network.get_loads()
    assert loads.loc["DATACENTER", "p0"] == 300.0
    assert loads.loc["DATACENTER", "q0"] == 60.0
    assert bool(loads.loc["DATACENTER", "connected"])


@pytest.mark.asyncio
async def test_create_load_on_node_breaker_busbar_section(
    creation_tools, mock_ctx, node_breaker_network
):
    switches_before = len(node_breaker_network.get_switches())

    result = await creation_tools.create_load(
        "DATACENTER", "S1VL2_BBS1", p0=150.0, network_id="nb", ctx=mock_ctx
    )

    assert "Created load 'DATACENTER'" in result
    loads = node_breaker_network.get_loads()
    assert loads.loc["DATACENTER", "voltage_level_id"] == "S1VL2"
    assert bool(loads.loc["DATACENTER", "connected"])
    # A bay was built: the load is attached to the busbar section through
    # switching equipment, which the caller never had to describe.
    assert len(node_breaker_network.get_switches()) > switches_before


@pytest.mark.asyncio
async def test_create_generator_with_voltage_regulation(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_generator(
        "SOLAR_DC",
        "B4",
        target_p=80.0,
        max_p=100.0,
        voltage_regulator_on=True,
        target_v=137.0,
        energy_source="SOLAR",
        ctx=mock_ctx,
    )

    assert "voltage regulation at 137.0 kV" in result
    generators = bus_breaker_network.get_generators()
    assert generators.loc["SOLAR_DC", "target_p"] == 80.0
    assert generators.loc["SOLAR_DC", "energy_source"] == "SOLAR"
    assert bool(generators.loc["SOLAR_DC", "voltage_regulator_on"])


@pytest.mark.asyncio
async def test_create_generator_without_regulation_defaults_target_q(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_generator(
        "GEN_DC", "B4", target_p=50.0, max_p=60.0, ctx=mock_ctx
    )

    assert "reactive setpoint 0.0 MVAr" in result
    generators = bus_breaker_network.get_generators()
    assert generators.loc["GEN_DC", "target_q"] == 0.0
    assert not bool(generators.loc["GEN_DC", "voltage_regulator_on"])


@pytest.mark.asyncio
async def test_create_line(creation_tools, mock_ctx, bus_breaker_network):
    await _new_voltage_level(creation_tools, mock_ctx)

    result = await creation_tools.create_line(
        "LINE_B4_DC", "B4", "VL_DC_1_1", r=0.5, x=5.0, ctx=mock_ctx
    )

    assert "Created line 'LINE_B4_DC'" in result
    lines = bus_breaker_network.get_lines()
    assert lines.loc["LINE_B4_DC", "voltage_level1_id"] == "VL4"
    assert lines.loc["LINE_B4_DC", "voltage_level2_id"] == "VL_DC"
    assert lines.loc["LINE_B4_DC", "x"] == 5.0
    assert bool(lines.loc["LINE_B4_DC", "connected1"])
    assert bool(lines.loc["LINE_B4_DC", "connected2"])


@pytest.mark.asyncio
async def test_create_line_warns_on_nominal_voltage_mismatch(
    creation_tools, mock_ctx, bus_breaker_network
):
    await creation_tools.create_substation("SUB_400", ctx=mock_ctx)
    await creation_tools.create_voltage_level("VL_400", "SUB_400", 400.0, ctx=mock_ctx)

    result = await creation_tools.create_line(
        "LINE_MIX", "B4", "VL_400_1_1", r=1.0, x=1.0, ctx=mock_ctx
    )

    assert "Created line 'LINE_MIX'" in result
    assert "different nominal voltages" in result
    assert "LINE_MIX" in bus_breaker_network.get_lines().index


@pytest.mark.asyncio
async def test_datacenter_connection_converges(
    creation_tools, mock_ctx, bus_breaker_network
):
    """The end-to-end scenario the creation tools exist for."""
    await _new_voltage_level(
        creation_tools, mock_ctx, low_voltage_limit=128.0, high_voltage_limit=145.0
    )
    await creation_tools.create_load(
        "DATACENTER", "VL_DC_1_1", p0=300.0, q0=60.0, ctx=mock_ctx
    )
    await creation_tools.create_line(
        "LINE_B4_DC", "B4", "VL_DC_1_1", r=0.5, x=5.0, ctx=mock_ctx
    )
    await creation_tools.create_generator(
        "SOLAR_DC",
        "VL_DC_1_1",
        target_p=80.0,
        max_p=100.0,
        voltage_regulator_on=True,
        target_v=137.0,
        ctx=mock_ctx,
    )

    results = pp.loadflow.run_ac(bus_breaker_network)

    assert results[0].status == pp.loadflow.ComponentStatus.CONVERGED
    # The new site draws its power through the new line.
    assert bus_breaker_network.get_lines().loc["LINE_B4_DC", "p1"] > 0


@pytest.mark.asyncio
async def test_creation_invalidates_loadflow_results(
    creation_tools, mock_ctx, bus_breaker_network
):
    proxy = creation_tools.get_proxy("test-session")
    proxy.loadflow_results["net1"] = "stale results"

    await creation_tools.create_load("DATACENTER", "B4", p0=10.0, ctx=mock_ctx)

    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_create_load_rejects_duplicate_id(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_load("B2-L", "B4", p0=10.0, ctx=mock_ctx)

    assert "already used by a load" in result


@pytest.mark.asyncio
async def test_create_load_rejects_bus_view_bus(
    creation_tools, mock_ctx, bus_breaker_network
):
    """'VL1_0' comes from the bus view and cannot host an element."""
    result = await creation_tools.create_load(
        "DATACENTER", "VL1_0", p0=10.0, ctx=mock_ctx
    )

    assert "bus of the bus view" in result
    # The message points at the id that would have worked.
    assert "B1" in result
    assert "DATACENTER" not in bus_breaker_network.get_loads().index


@pytest.mark.asyncio
async def test_create_load_rejects_bus_breaker_view_bus_in_node_breaker(
    creation_tools, mock_ctx, node_breaker_network
):
    result = await creation_tools.create_load(
        "DATACENTER", "S1VL2_0", p0=10.0, network_id="nb", ctx=mock_ctx
    )

    assert "NODE_BREAKER" in result
    assert "S1VL2_BBS1" in result


@pytest.mark.asyncio
async def test_create_load_rejects_unknown_connection_point(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_load(
        "DATACENTER", "NOPE", p0=10.0, ctx=mock_ctx
    )

    assert "Bus or busbar section 'NOPE' not found" in result


@pytest.mark.asyncio
async def test_create_load_rejects_empty_ids(
    creation_tools, mock_ctx, bus_breaker_network
):
    assert "id is required" in await creation_tools.create_load(
        "", "B4", p0=10.0, ctx=mock_ctx
    )
    assert "bus or busbar section id is required" in await creation_tools.create_load(
        "DATACENTER", "", p0=10.0, ctx=mock_ctx
    )


@pytest.mark.asyncio
async def test_create_voltage_level_rejects_unknown_substation(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_voltage_level(
        "VL_DC", "MISSING", 135.0, ctx=mock_ctx
    )

    assert "Substation 'MISSING' not found" in result


@pytest.mark.asyncio
async def test_create_voltage_level_rejects_unknown_topology_kind(
    creation_tools, mock_ctx, bus_breaker_network
):
    await creation_tools.create_substation("SUB_DC", ctx=mock_ctx)

    result = await creation_tools.create_voltage_level(
        "VL_DC", "SUB_DC", 135.0, topology_kind="BUS_BREAKR", ctx=mock_ctx
    )

    assert "Unsupported topology_kind" in result


@pytest.mark.asyncio
async def test_create_voltage_level_rejects_empty_topology(
    creation_tools, mock_ctx, bus_breaker_network
):
    await creation_tools.create_substation("SUB_DC", ctx=mock_ctx)

    result = await creation_tools.create_voltage_level(
        "VL_DC", "SUB_DC", 135.0, busbar_count=0, ctx=mock_ctx
    )

    assert "at least 1" in result


@pytest.mark.asyncio
async def test_create_generator_rejects_unreachable_setpoint(
    creation_tools, mock_ctx, bus_breaker_network
):
    too_high = await creation_tools.create_generator(
        "GEN_DC", "B4", target_p=100.0, max_p=50.0, ctx=mock_ctx
    )
    too_low = await creation_tools.create_generator(
        "GEN_DC", "B4", target_p=10.0, max_p=50.0, min_p=20.0, ctx=mock_ctx
    )

    assert "lower than target_p" in too_high
    assert "higher than target_p" in too_low
    assert "GEN_DC" not in bus_breaker_network.get_generators().index


@pytest.mark.asyncio
async def test_create_generator_requires_target_v_when_regulating(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_generator(
        "GEN_DC",
        "B4",
        target_p=50.0,
        max_p=60.0,
        voltage_regulator_on=True,
        ctx=mock_ctx,
    )

    assert "target_v (in kV) is required" in result
    # The message helps pick a value by naming the nominal voltage.
    assert "135.0 kV" in result


@pytest.mark.asyncio
async def test_create_line_rejects_identical_ends(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_line(
        "LINE_X", "B4", "B4", r=1.0, x=1.0, ctx=mock_ctx
    )

    assert "two distinct connection points" in result


@pytest.mark.asyncio
async def test_create_line_rejects_zero_reactance(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_line(
        "LINE_X", "B4", "B5", r=1.0, x=0.0, ctx=mock_ctx
    )

    assert "must not be 0" in result


@pytest.mark.asyncio
async def test_creation_without_network(creation_tools, mock_ctx):
    assert "No network specified" in await creation_tools.create_substation(
        "SUB_DC", ctx=mock_ctx
    )
    assert "No network specified" in await creation_tools.create_voltage_level(
        "VL_DC", "SUB_DC", 135.0, ctx=mock_ctx
    )
    assert "No network specified" in await creation_tools.create_load(
        "DATACENTER", "B4", p0=10.0, ctx=mock_ctx
    )
    assert "No network specified" in await creation_tools.create_generator(
        "GEN_DC", "B4", target_p=10.0, max_p=20.0, ctx=mock_ctx
    )
    assert "No network specified" in await creation_tools.create_line(
        "LINE_X", "B4", "B5", r=1.0, x=1.0, ctx=mock_ctx
    )


@pytest.mark.asyncio
async def test_creation_on_unknown_network(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_load(
        "DATACENTER", "B4", p0=10.0, network_id="unknown", ctx=mock_ctx
    )

    assert "Network 'unknown' not found" in result


@pytest.mark.asyncio
async def test_pypowsybl_error_is_reported(
    creation_tools, mock_ctx, bus_breaker_network
):
    """A rejection coming from pypowsybl itself is surfaced, not raised."""
    result = await creation_tools.create_substation(
        "SUB_DC", country="NOT_A_COUNTRY", ctx=mock_ctx
    )

    assert "Failed to create substation" in result
    assert "SUB_DC" not in bus_breaker_network.get_substations().index


@pytest.mark.asyncio
async def test_create_voltage_level_reports_partial_creation(
    creation_tools, mock_ctx, bus_breaker_network, monkeypatch
):
    """The voltage level survives a failing topology creation: say so."""
    await creation_tools.create_substation("SUB_DC", ctx=mock_ctx)

    def failing_topology(*args, **kwargs):
        raise pp.PyPowsyblError("topology refused")

    monkeypatch.setattr(
        "pypowsybl.network.create_voltage_level_topology", failing_topology
    )

    result = await creation_tools.create_voltage_level(
        "VL_DC", "SUB_DC", 135.0, ctx=mock_ctx
    )

    assert "was created but its connection points could not be" in result
    assert "VL_DC" in bus_breaker_network.get_voltage_levels().index


@pytest.mark.asyncio
async def test_position_order_is_reused_then_incremented(
    creation_tools, mock_ctx, node_breaker_network
):
    """An explicit feeder position is honored, the next one is derived from it."""
    await creation_tools.create_substation("SUB_NEW", ctx=mock_ctx)
    await creation_tools.create_voltage_level(
        "VL_NEW", "SUB_NEW", 400.0, topology_kind="NODE_BREAKER", ctx=mock_ctx
    )

    await creation_tools.create_generator(
        "GEN_NEW",
        "VL_NEW_1_1",
        target_p=100.0,
        max_p=120.0,
        position_order=42,
        direction="TOP",
        ctx=mock_ctx,
    )
    await creation_tools.create_load("LOAD_NEW", "VL_NEW_1_1", p0=10.0, ctx=mock_ctx)

    positions = node_breaker_network.get_extensions("position")
    assert positions.loc["GEN_NEW", "order"] == 42
    assert positions.loc["GEN_NEW", "direction"] == "TOP"
    # No position was given for the load: it goes after the existing ones.
    assert positions.loc["LOAD_NEW", "order"] > 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bay_function", "call", "expected"),
    [
        (
            "create_load_bay",
            lambda tools, ctx: tools.create_load("DATACENTER", "B4", p0=10.0, ctx=ctx),
            "Failed to create load",
        ),
        (
            "create_generator_bay",
            lambda tools, ctx: tools.create_generator(
                "GEN_DC", "B4", target_p=10.0, max_p=20.0, ctx=ctx
            ),
            "Failed to create generator",
        ),
        (
            "create_line_bays",
            lambda tools, ctx: tools.create_line(
                "LINE_X", "B4", "B5", r=1.0, x=1.0, ctx=ctx
            ),
            "Failed to create line",
        ),
    ],
)
async def test_bay_failures_are_reported(
    creation_tools,
    mock_ctx,
    bus_breaker_network,
    monkeypatch,
    bay_function,
    call,
    expected,
):
    def failing_bay(*args, **kwargs):
        raise pp.PyPowsyblError("bay refused")

    monkeypatch.setattr(f"pypowsybl.network.{bay_function}", failing_bay)

    result = await call(creation_tools, mock_ctx)

    assert expected in result
    assert "bay refused" in result


@pytest.mark.asyncio
async def test_create_substation_rejects_duplicate_id(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_substation("S1", ctx=mock_ctx)

    assert "already used by a substation" in result


@pytest.mark.asyncio
async def test_create_voltage_level_reports_pypowsybl_rejection(
    creation_tools, mock_ctx, bus_breaker_network
):
    await creation_tools.create_substation("SUB_DC", ctx=mock_ctx)

    result = await creation_tools.create_voltage_level(
        "VL_DC", "SUB_DC", -1.0, ctx=mock_ctx
    )

    assert "Failed to create voltage level" in result
    assert "VL_DC" not in bus_breaker_network.get_voltage_levels().index


@pytest.mark.asyncio
async def test_create_transformer(creation_tools, mock_ctx, bus_breaker_network):
    """A transformer links two voltage levels of the same new substation."""
    await creation_tools.create_substation("SUB_DC", ctx=mock_ctx)
    await creation_tools.create_voltage_level(
        "VL_DC_135", "SUB_DC", 135.0, ctx=mock_ctx
    )
    await creation_tools.create_voltage_level("VL_DC_63", "SUB_DC", 63.0, ctx=mock_ctx)

    result = await creation_tools.create_transformer(
        "TR_DC",
        "VL_DC_135_1_1",
        "VL_DC_63_1_1",
        r=0.2,
        x=10.0,
        rated_s=400.0,
        ctx=mock_ctx,
    )

    assert "Created transformer 'TR_DC' (135.0/63.0 kV" in result
    transformers = bus_breaker_network.get_2_windings_transformers()
    assert transformers.loc["TR_DC", "voltage_level1_id"] == "VL_DC_135"
    assert transformers.loc["TR_DC", "voltage_level2_id"] == "VL_DC_63"
    # Rated voltages default to the nominal voltages of both ends.
    assert transformers.loc["TR_DC", "rated_u1"] == 135.0
    assert transformers.loc["TR_DC", "rated_u2"] == 63.0
    assert transformers.loc["TR_DC", "rated_s"] == 400.0


@pytest.mark.asyncio
async def test_create_transformer_honors_explicit_rated_voltages(
    creation_tools, mock_ctx, bus_breaker_network
):
    await creation_tools.create_substation("SUB_DC", ctx=mock_ctx)
    await creation_tools.create_voltage_level(
        "VL_DC_135", "SUB_DC", 135.0, ctx=mock_ctx
    )
    await creation_tools.create_voltage_level("VL_DC_63", "SUB_DC", 63.0, ctx=mock_ctx)

    await creation_tools.create_transformer(
        "TR_DC",
        "VL_DC_135_1_1",
        "VL_DC_63_1_1",
        r=0.2,
        x=10.0,
        rated_u1=138.0,
        rated_u2=64.0,
        ctx=mock_ctx,
    )

    transformers = bus_breaker_network.get_2_windings_transformers()
    assert transformers.loc["TR_DC", "rated_u1"] == 138.0
    assert transformers.loc["TR_DC", "rated_u2"] == 64.0


@pytest.mark.asyncio
async def test_datacenter_behind_a_transformer_converges(
    creation_tools, mock_ctx, bus_breaker_network
):
    """The other connection pattern: a site with its own internal voltage."""
    await creation_tools.create_substation("SUB_DC", country="FR", ctx=mock_ctx)
    await creation_tools.create_voltage_level(
        "VL_DC_135", "SUB_DC", 135.0, ctx=mock_ctx
    )
    await creation_tools.create_voltage_level("VL_DC_63", "SUB_DC", 63.0, ctx=mock_ctx)
    await creation_tools.create_transformer(
        "TR_DC", "VL_DC_135_1_1", "VL_DC_63_1_1", r=0.2, x=10.0, ctx=mock_ctx
    )
    await creation_tools.create_load(
        "DATACENTER", "VL_DC_63_1_1", p0=100.0, q0=20.0, ctx=mock_ctx
    )
    await creation_tools.create_line(
        "LINE_B4_DC", "B4", "VL_DC_135_1_1", r=0.5, x=5.0, ctx=mock_ctx
    )

    results = pp.loadflow.run_ac(bus_breaker_network)

    assert results[0].status == pp.loadflow.ComponentStatus.CONVERGED
    # The load is fed through the transformer.
    assert bus_breaker_network.get_2_windings_transformers().loc["TR_DC", "p1"] > 0


@pytest.mark.asyncio
async def test_create_transformer_rejects_two_substations(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_transformer(
        "TR_X", "B4", "B5", r=0.2, x=10.0, ctx=mock_ctx
    )

    assert "must stay inside one substation" in result
    assert "TR_X" not in bus_breaker_network.get_2_windings_transformers().index


@pytest.mark.asyncio
async def test_create_transformer_rejects_invalid_geometry(
    creation_tools, mock_ctx, bus_breaker_network
):
    identical = await creation_tools.create_transformer(
        "TR_X", "B4", "B4", r=0.2, x=10.0, ctx=mock_ctx
    )
    no_reactance = await creation_tools.create_transformer(
        "TR_X", "B4", "B5", r=0.2, x=0.0, ctx=mock_ctx
    )

    assert "two distinct connection points" in identical
    assert "must not be 0" in no_reactance


@pytest.mark.asyncio
async def test_create_transformer_without_network(creation_tools, mock_ctx):
    result = await creation_tools.create_transformer(
        "TR_X", "B4", "B5", r=0.2, x=10.0, ctx=mock_ctx
    )

    assert "No network specified" in result


@pytest.mark.asyncio
async def test_create_transformer_reports_pypowsybl_rejection(
    creation_tools, mock_ctx, bus_breaker_network, monkeypatch
):
    await creation_tools.create_substation("SUB_DC", ctx=mock_ctx)
    await creation_tools.create_voltage_level(
        "VL_DC_135", "SUB_DC", 135.0, ctx=mock_ctx
    )
    await creation_tools.create_voltage_level("VL_DC_63", "SUB_DC", 63.0, ctx=mock_ctx)

    def failing_bay(*args, **kwargs):
        raise pp.PyPowsyblError("bay refused")

    monkeypatch.setattr(
        "pypowsybl.network.create_2_windings_transformer_bays", failing_bay
    )

    result = await creation_tools.create_transformer(
        "TR_DC", "VL_DC_135_1_1", "VL_DC_63_1_1", r=0.2, x=10.0, ctx=mock_ctx
    )

    assert "Failed to create transformer" in result


@pytest.fixture
def datacenter_site(creation_tools, mock_ctx, bus_breaker_network):
    """Fixture factory: a two-voltage site with a transformer and a line."""

    async def build():
        await creation_tools.create_substation("S_DC", country="FR", ctx=mock_ctx)
        await creation_tools.create_voltage_level("VL_A", "S_DC", 135.0, ctx=mock_ctx)
        await creation_tools.create_voltage_level("VL_B", "S_DC", 63.0, ctx=mock_ctx)
        await creation_tools.create_transformer(
            "TR", "VL_A_1_1", "VL_B_1_1", r=0.2, x=10.0, ctx=mock_ctx
        )
        await creation_tools.create_line(
            "LINE_DC", "B4", "VL_A_1_1", r=0.5, x=5.0, ctx=mock_ctx
        )
        await creation_tools.create_load(
            "DC", "VL_B_1_1", p0=80.0, q0=15.0, ctx=mock_ctx
        )
        return bus_breaker_network

    return build


@pytest.mark.asyncio
async def test_create_battery(creation_tools, mock_ctx, bus_breaker_network):
    result = await creation_tools.create_battery(
        "BESS", "B4", target_p=20.0, max_p=50.0, ctx=mock_ctx
    )

    assert "Created battery 'BESS' (20.0 MW in [-50.0, 50.0] MW)" in result
    batteries = bus_breaker_network.get_batteries()
    assert batteries.loc["BESS", "target_p"] == 20.0
    # min_p defaults to -max_p, i.e. symmetric charge/discharge.
    assert batteries.loc["BESS", "min_p"] == -50.0


@pytest.mark.asyncio
async def test_create_battery_rejects_unreachable_setpoint(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_battery(
        "BESS", "B4", target_p=100.0, max_p=50.0, ctx=mock_ctx
    )

    assert "outside [-50.0, 50.0] MW" in result
    assert "BESS" not in bus_breaker_network.get_batteries().index


@pytest.mark.asyncio
async def test_create_shunt_compensator(creation_tools, mock_ctx, bus_breaker_network):
    result = await creation_tools.create_shunt_compensator(
        "CAP", "B4", b_per_section=0.003, max_section_count=3, ctx=mock_ctx
    )

    assert "Created shunt compensator 'CAP' (capacitor, 1/3 section(s)" in result
    # The message translates susceptance into MVAr at the nominal voltage.
    assert "MVAr at 135.0 kV" in result
    shunts = bus_breaker_network.get_shunt_compensators()
    assert shunts.loc["CAP", "max_section_count"] == 3
    assert shunts.loc["CAP", "voltage_level_id"] == "VL4"


@pytest.mark.asyncio
async def test_create_shunt_compensator_reactor_and_validation(
    creation_tools, mock_ctx, bus_breaker_network
):
    reactor = await creation_tools.create_shunt_compensator(
        "REACT", "B4", b_per_section=-0.002, ctx=mock_ctx
    )
    too_many = await creation_tools.create_shunt_compensator(
        "CAP",
        "B4",
        b_per_section=0.002,
        max_section_count=2,
        section_count=5,
        ctx=mock_ctx,
    )

    assert "(reactor," in reactor
    assert "section_count (5) must be between 0 and max_section_count (2)" in too_many


@pytest.mark.asyncio
async def test_create_static_var_compensator(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_static_var_compensator(
        "SVC", "B4", b_min=-0.0126, b_max=0.0126, target_v=137.0, ctx=mock_ctx
    )

    assert "VOLTAGE regulation at 137.0 kV" in result
    svcs = bus_breaker_network.get_static_var_compensators()
    assert svcs.loc["SVC", "regulation_mode"] == "VOLTAGE"
    assert svcs.loc["SVC", "b_max"] == 0.0126


@pytest.mark.asyncio
async def test_create_static_var_compensator_validation(
    creation_tools, mock_ctx, bus_breaker_network
):
    inverted = await creation_tools.create_static_var_compensator(
        "SVC", "B4", b_min=0.01, b_max=-0.01, target_v=137.0, ctx=mock_ctx
    )
    no_target = await creation_tools.create_static_var_compensator(
        "SVC", "B4", b_min=-0.01, b_max=0.01, ctx=mock_ctx
    )
    no_target_q = await creation_tools.create_static_var_compensator(
        "SVC",
        "B4",
        b_min=-0.01,
        b_max=0.01,
        regulation_mode="REACTIVE_POWER",
        ctx=mock_ctx,
    )
    off_mode = await creation_tools.create_static_var_compensator(
        "SVC", "B4", b_min=-0.01, b_max=0.01, regulation_mode="OFF", ctx=mock_ctx
    )

    assert "must be lower than b_max" in inverted
    assert "target_v (in kV) is required" in no_target
    assert "target_q (in MVAr) is required" in no_target_q
    # There is no OFF mode in pypowsybl 1.15: the message says what to do instead.
    assert "regulating=False" in off_mode


@pytest.mark.asyncio
async def test_create_ground(creation_tools, mock_ctx, bus_breaker_network):
    result = await creation_tools.create_ground("GND", "B4", ctx=mock_ctx)

    assert "Created ground 'GND' on bus 'B4'" in result
    assert "GND" in bus_breaker_network.get_grounds().index


@pytest.mark.asyncio
async def test_create_ground_refused_in_node_breaker(
    creation_tools, mock_ctx, node_breaker_network
):
    """pypowsybl 1.15 has no ground bay, so node/breaker is out of reach."""
    result = await creation_tools.create_ground(
        "GND", "S1VL2_BBS1", network_id="nb", ctx=mock_ctx
    )

    assert "no bay creation for grounds" in result
    assert len(node_breaker_network.get_grounds()) == 0


@pytest.mark.asyncio
async def test_create_operational_limits(creation_tools, mock_ctx, datacenter_site):
    network = await datacenter_site()

    result = await creation_tools.create_operational_limits(
        "LINE_DC",
        permanent_limit=300.0,
        temporary_limit_values=[400.0],
        temporary_limit_durations=[1200],
        ctx=mock_ctx,
    )

    assert "Set CURRENT limits on line 'LINE_DC' (side(s) ONE, TWO)" in result
    assert "permanent 300.0 A" in result
    assert "temporary 400.0 A for 1200 s" in result
    limits = network.get_operational_limits()
    rows = limits[limits.index.get_level_values("element_id") == "LINE_DC"]
    # Permanent and temporary, on both sides, in the group the analyses read.
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_created_limits_make_the_branch_overloadable(
    creation_tools, mock_ctx, datacenter_site
):
    """The point of the tool: loading_percent needs a limit to divide by."""
    from pypowsybl_mcp.tools.network_tools import NetworkTools

    network = await datacenter_site()
    await creation_tools.create_operational_limits(
        "LINE_DC", permanent_limit=300.0, ctx=mock_ctx
    )
    pp.loadflow.run_ac(network)

    network_tools = NetworkTools(creation_tools.pypowsybl_proxies)
    raw = await network_tools.get_network_element_data(
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=50.0,
        ctx=mock_ctx,
    )

    assert "LINE_DC" in json.loads(raw)["elements"]


@pytest.mark.asyncio
async def test_create_operational_limits_validation(
    creation_tools, mock_ctx, datacenter_site
):
    await datacenter_site()

    wrong_element = await creation_tools.create_operational_limits(
        "DC", permanent_limit=100.0, ctx=mock_ctx
    )
    mismatched = await creation_tools.create_operational_limits(
        "LINE_DC", permanent_limit=100.0, temporary_limit_values=[200.0], ctx=mock_ctx
    )
    negative = await creation_tools.create_operational_limits(
        "LINE_DC", permanent_limit=-5.0, ctx=mock_ctx
    )
    bad_type = await creation_tools.create_operational_limits(
        "LINE_DC", permanent_limit=100.0, limit_type="VOLTAGE", ctx=mock_ctx
    )
    bad_side = await creation_tools.create_operational_limits(
        "LINE_DC", permanent_limit=100.0, side="LEFT", ctx=mock_ctx
    )

    assert "cannot be attached to a load ('DC')" in wrong_element
    assert "must have the same length" in mismatched
    assert "permanent_limit must be positive" in negative
    assert "Unsupported limit_type 'VOLTAGE'" in bad_type
    assert "Unsupported side 'LEFT'" in bad_side


@pytest.mark.asyncio
async def test_create_reactive_limits_range(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_reactive_limits(
        "B1-G", min_q=-100.0, max_q=100.0, ctx=mock_ctx
    )

    assert "min/max range [-100.0, 100.0] MVAr" in result
    generators = bus_breaker_network.get_generators()
    assert generators.loc["B1-G", "min_q"] == -100.0
    assert generators.loc["B1-G", "max_q"] == 100.0


@pytest.mark.asyncio
async def test_create_reactive_limits_curve(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_reactive_limits(
        "B1-G",
        p_points=[0.0, 50.0, 100.0],
        min_q_points=[-30.0, -40.0, -20.0],
        max_q_points=[30.0, 40.0, 20.0],
        ctx=mock_ctx,
    )

    assert "capability curve over 3 points from 0.0 to 100.0 MW" in result
    points = bus_breaker_network.get_reactive_capability_curve_points()
    assert len(points.loc["B1-G"]) == 3


@pytest.mark.asyncio
async def test_create_reactive_limits_validation(
    creation_tools, mock_ctx, bus_breaker_network
):
    wrong_element = await creation_tools.create_reactive_limits(
        "B2-L", min_q=-1.0, max_q=1.0, ctx=mock_ctx
    )
    nothing = await creation_tools.create_reactive_limits("B1-G", ctx=mock_ctx)
    both = await creation_tools.create_reactive_limits(
        "B1-G",
        min_q=-1.0,
        max_q=1.0,
        p_points=[0.0, 1.0],
        min_q_points=[-1.0, -1.0],
        max_q_points=[1.0, 1.0],
        ctx=mock_ctx,
    )
    inverted = await creation_tools.create_reactive_limits(
        "B1-G", min_q=5.0, max_q=-5.0, ctx=mock_ctx
    )
    single_point = await creation_tools.create_reactive_limits(
        "B1-G", p_points=[0.0], min_q_points=[-1.0], max_q_points=[1.0], ctx=mock_ctx
    )
    ragged = await creation_tools.create_reactive_limits(
        "B1-G",
        p_points=[0.0, 1.0],
        min_q_points=[-1.0],
        max_q_points=[1.0, 1.0],
        ctx=mock_ctx,
    )

    assert "cannot be attached to a load" in wrong_element
    assert "Nothing to set" in nothing
    assert "not both" in both
    assert "must not be greater than max_q" in inverted
    assert "at least two points" in single_point
    assert "same length" in ragged


@pytest.mark.asyncio
async def test_create_ratio_tap_changer(creation_tools, mock_ctx, datacenter_site):
    network = await datacenter_site()

    result = await creation_tools.create_ratio_tap_changer(
        "TR", regulating=True, target_v=63.0, ctx=mock_ctx
    )

    assert "17 steps from 0.900 to 1.100, starting at tap 8" in result
    assert "regulating 63.0 kV on side TWO" in result
    changers = network.get_ratio_tap_changers()
    assert changers.loc["TR", "low_tap"] == 0
    assert changers.loc["TR", "high_tap"] == 16
    assert changers.loc["TR", "tap"] == 8
    assert bool(changers.loc["TR", "regulating"])


@pytest.mark.asyncio
async def test_created_ratio_tap_changer_can_then_be_moved(
    creation_tools, mock_ctx, datacenter_site
):
    """create_transformer() + create_ratio_tap_changer() unlocks set_tap_position."""
    from pypowsybl_mcp.tools.network_tools import NetworkTools

    network = await datacenter_site()
    await creation_tools.create_ratio_tap_changer("TR", ctx=mock_ctx)

    network_tools = NetworkTools(creation_tools.pypowsybl_proxies)
    result = await network_tools.set_tap_position("TR", 12, ctx=mock_ctx)

    assert "Updated ratio tap position of transformer 'TR' to 12" in result
    assert network.get_ratio_tap_changers().loc["TR", "tap"] == 12


@pytest.mark.asyncio
async def test_create_ratio_tap_changer_with_explicit_ratios(
    creation_tools, mock_ctx, datacenter_site
):
    network = await datacenter_site()

    result = await creation_tools.create_ratio_tap_changer(
        "TR", rho_values=[0.95, 1.0, 1.05], tap=0, ctx=mock_ctx
    )

    assert "3 steps from 0.950 to 1.050, starting at tap 0" in result
    assert network.get_ratio_tap_changer_steps().loc["TR"].iloc[0]["rho"] == 0.95


@pytest.mark.asyncio
async def test_create_ratio_tap_changer_validation(
    creation_tools, mock_ctx, datacenter_site
):
    await datacenter_site()

    wrong_element = await creation_tools.create_ratio_tap_changer(
        "LINE_DC", ctx=mock_ctx
    )
    unknown = await creation_tools.create_ratio_tap_changer("NOPE", ctx=mock_ctx)
    no_target = await creation_tools.create_ratio_tap_changer(
        "TR", regulating=True, ctx=mock_ctx
    )
    too_few = await creation_tools.create_ratio_tap_changer(
        "TR", step_count=1, ctx=mock_ctx
    )
    bad_tap = await creation_tools.create_ratio_tap_changer(
        "TR", step_count=5, tap=9, ctx=mock_ctx
    )
    bad_side = await creation_tools.create_ratio_tap_changer(
        "TR", regulated_side="THREE", ctx=mock_ctx
    )
    # Nothing was created by any of the calls above, so this one succeeds...
    created = await creation_tools.create_ratio_tap_changer("TR", ctx=mock_ctx)
    # ... and only then is a second tap changer refused.
    duplicate = await creation_tools.create_ratio_tap_changer("TR", ctx=mock_ctx)

    assert "cannot be attached to a line" in wrong_element
    assert "Element 'NOPE' not found" in unknown
    assert "target_v (in kV) is required" in no_target
    assert "step_count must be at least 2" in too_few
    assert "out of range [0, 4]" in bad_tap
    assert "Unsupported regulated_side 'THREE'" in bad_side
    assert "Added a ratio tap changer" in created
    assert "already has a ratio tap changer" in duplicate


@pytest.mark.asyncio
async def test_create_phase_tap_changer(creation_tools, mock_ctx, datacenter_site):
    network = await datacenter_site()

    result = await creation_tools.create_phase_tap_changer(
        "TR",
        regulating=True,
        regulation_mode="ACTIVE_POWER_CONTROL",
        regulation_value=50.0,
        ctx=mock_ctx,
    )

    assert "17 steps from -10.0° to 10.0°, starting at tap 8" in result
    assert "regulating in ACTIVE_POWER_CONTROL mode at 50.0" in result
    changers = network.get_phase_tap_changers()
    assert changers.loc["TR", "regulation_mode"] == "ACTIVE_POWER_CONTROL"
    assert changers.loc["TR", "regulation_value"] == 50.0


@pytest.mark.asyncio
async def test_create_phase_tap_changer_validation(
    creation_tools, mock_ctx, datacenter_site
):
    await datacenter_site()

    fixed_tap = await creation_tools.create_phase_tap_changer(
        "TR", regulation_mode="FIXED_TAP", ctx=mock_ctx
    )
    no_value = await creation_tools.create_phase_tap_changer(
        "TR", regulating=True, ctx=mock_ctx
    )
    bad_angle = await creation_tools.create_phase_tap_changer(
        "TR", max_angle_degrees=0.0, ctx=mock_ctx
    )
    created = await creation_tools.create_phase_tap_changer(
        "TR", alpha_values=[-5.0, 0.0, 5.0], ctx=mock_ctx
    )
    duplicate = await creation_tools.create_phase_tap_changer("TR", ctx=mock_ctx)

    # No FIXED_TAP mode in 1.15: the message points at regulating=False.
    assert "regulating=False" in fixed_tap
    assert "regulation_value is required" in no_value
    assert "max_angle_degrees must be positive" in bad_angle
    assert "3 steps from -5.0° to 5.0°" in created
    assert "already has a phase tap changer" in duplicate


NO_NETWORK_CALLS = {
    "create_battery": lambda t, ctx: t.create_battery(
        "X", "B4", target_p=1.0, max_p=2.0, ctx=ctx
    ),
    "create_shunt_compensator": lambda t, ctx: t.create_shunt_compensator(
        "X", "B4", b_per_section=0.001, ctx=ctx
    ),
    "create_static_var_compensator": lambda t, ctx: t.create_static_var_compensator(
        "X", "B4", b_min=-0.01, b_max=0.01, target_v=135.0, ctx=ctx
    ),
    "create_ground": lambda t, ctx: t.create_ground("X", "B4", ctx=ctx),
    "create_operational_limits": lambda t, ctx: t.create_operational_limits(
        "L", permanent_limit=100.0, ctx=ctx
    ),
    "create_reactive_limits": lambda t, ctx: t.create_reactive_limits(
        "G", min_q=-1.0, max_q=1.0, ctx=ctx
    ),
    "create_ratio_tap_changer": lambda t, ctx: t.create_ratio_tap_changer(
        "TR", ctx=ctx
    ),
    "create_phase_tap_changer": lambda t, ctx: t.create_phase_tap_changer(
        "TR", ctx=ctx
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("call", NO_NETWORK_CALLS.values(), ids=NO_NETWORK_CALLS)
async def test_no_network_selected(creation_tools, mock_ctx, call):
    assert "No network specified" in await call(creation_tools, mock_ctx)


PYPOWSYBL_FAILURES = {
    "create_battery": (
        "pypowsybl.network.create_battery_bay",
        lambda t, ctx: t.create_battery("X", "B4", target_p=1.0, max_p=2.0, ctx=ctx),
        "Failed to create battery",
    ),
    "create_shunt_compensator": (
        "pypowsybl.network.create_shunt_compensator_bay",
        lambda t, ctx: t.create_shunt_compensator(
            "X", "B4", b_per_section=0.001, ctx=ctx
        ),
        "Failed to create shunt compensator",
    ),
    "create_static_var_compensator": (
        "pypowsybl.network.create_static_var_compensator_bay",
        lambda t, ctx: t.create_static_var_compensator(
            "X", "B4", b_min=-0.01, b_max=0.01, target_v=135.0, ctx=ctx
        ),
        "Failed to create static var compensator",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "call", "expected"),
    PYPOWSYBL_FAILURES.values(),
    ids=PYPOWSYBL_FAILURES,
)
async def test_bay_failures_of_new_injections_are_reported(
    creation_tools, mock_ctx, bus_breaker_network, monkeypatch, target, call, expected
):
    def failing(*args, **kwargs):
        raise pp.PyPowsyblError("bay refused")

    monkeypatch.setattr(target, failing)

    result = await call(creation_tools, mock_ctx)

    assert expected in result
    assert "bay refused" in result


@pytest.mark.asyncio
async def test_creation_failures_of_attachments_are_reported(
    creation_tools, mock_ctx, datacenter_site, monkeypatch
):
    """Rejections raised by pypowsybl itself are surfaced, not raised."""
    network = await datacenter_site()

    def failing(*args, **kwargs):
        raise pp.PyPowsyblError("refused")

    monkeypatch.setattr(network, "create_operational_limits", failing)
    monkeypatch.setattr(network, "create_minmax_reactive_limits", failing)
    monkeypatch.setattr(network, "create_ratio_tap_changers", failing)
    monkeypatch.setattr(network, "create_phase_tap_changers", failing)
    monkeypatch.setattr(network, "create_grounds", failing)

    assert "Failed to set operational limits" in (
        await creation_tools.create_operational_limits(
            "LINE_DC", permanent_limit=100.0, ctx=mock_ctx
        )
    )
    assert "Failed to set reactive limits" in (
        await creation_tools.create_reactive_limits(
            "B1-G", min_q=-1.0, max_q=1.0, ctx=mock_ctx
        )
    )
    assert "Failed to add ratio tap changer" in (
        await creation_tools.create_ratio_tap_changer("TR", ctx=mock_ctx)
    )
    assert "Failed to add phase tap changer" in (
        await creation_tools.create_phase_tap_changer("TR", ctx=mock_ctx)
    )
    assert "Failed to create ground" in (
        await creation_tools.create_ground("GND", "B4", ctx=mock_ctx)
    )


@pytest.mark.asyncio
async def test_curve_reactive_limits_failure_is_reported(
    creation_tools, mock_ctx, bus_breaker_network, monkeypatch
):
    def failing(*args, **kwargs):
        raise pp.PyPowsyblError("refused")

    monkeypatch.setattr(bus_breaker_network, "create_curve_reactive_limits", failing)

    result = await creation_tools.create_reactive_limits(
        "B1-G",
        p_points=[0.0, 10.0],
        min_q_points=[-1.0, -1.0],
        max_q_points=[1.0, 1.0],
        ctx=mock_ctx,
    )

    assert "Failed to set reactive limits" in result


@pytest.mark.asyncio
async def test_tap_changer_explicit_value_validation(
    creation_tools, mock_ctx, datacenter_site
):
    await datacenter_site()

    one_ratio = await creation_tools.create_ratio_tap_changer(
        "TR", rho_values=[1.0], ctx=mock_ctx
    )
    negative_ratio = await creation_tools.create_ratio_tap_changer(
        "TR", rho_values=[-1.0, 1.0], ctx=mock_ctx
    )
    bad_range = await creation_tools.create_ratio_tap_changer(
        "TR", range_percent=0.0, ctx=mock_ctx
    )
    one_angle = await creation_tools.create_phase_tap_changer(
        "TR", alpha_values=[0.0], ctx=mock_ctx
    )
    bad_phase_side = await creation_tools.create_phase_tap_changer(
        "TR", regulated_side="THREE", ctx=mock_ctx
    )

    assert "at least two ratios" in one_ratio
    assert "must be > 0" in negative_ratio
    assert "range_percent must be positive" in bad_range
    assert "at least two angles" in one_angle
    assert "Unsupported regulated_side 'THREE'" in bad_phase_side


@pytest.mark.asyncio
async def test_attachment_requires_a_non_empty_id(
    creation_tools, mock_ctx, bus_breaker_network
):
    result = await creation_tools.create_reactive_limits(
        "", min_q=-1.0, max_q=1.0, ctx=mock_ctx
    )

    assert "id is required" in result


@pytest.mark.asyncio
async def test_limits_and_reactive_edge_validations(
    creation_tools, mock_ctx, datacenter_site
):
    await datacenter_site()

    zero_duration = await creation_tools.create_operational_limits(
        "LINE_DC",
        permanent_limit=100.0,
        temporary_limit_values=[200.0],
        temporary_limit_durations=[0],
        ctx=mock_ctx,
    )
    alternative_group = await creation_tools.create_operational_limits(
        "LINE_DC", permanent_limit=100.0, group_name="WINTER", ctx=mock_ctx
    )
    half_range = await creation_tools.create_reactive_limits(
        "B1-G", min_q=-10.0, ctx=mock_ctx
    )
    half_curve = await creation_tools.create_reactive_limits(
        "B1-G", p_points=[0.0, 10.0], min_q_points=[-1.0, -1.0], ctx=mock_ctx
    )
    crossed_curve = await creation_tools.create_reactive_limits(
        "B1-G",
        p_points=[0.0, 10.0],
        min_q_points=[5.0, -1.0],
        max_q_points=[1.0, 1.0],
        ctx=mock_ctx,
    )
    no_sections = await creation_tools.create_shunt_compensator(
        "CAP", "B4", b_per_section=0.001, max_section_count=0, ctx=mock_ctx
    )

    assert "must be positive numbers of seconds" in zero_duration
    # An explicit group is not the active one, and the message says so.
    assert "not the active one" in alternative_group
    assert "Both min_q and max_q are required" in half_range
    assert "needs the three lists" in half_curve
    assert "min_q <= max_q" in crossed_curve
    assert "max_section_count must be at least 1" in no_sections
