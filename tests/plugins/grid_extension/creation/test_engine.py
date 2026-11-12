#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The creation pipeline, exercised against real pypowsybl networks."""

import pypowsybl as pp
import pytest

from pypowsybl_mcp.plugins.grid_extension.creation.context import (
    BatchRejected,
    CreationError,
    with_article,
)
from pypowsybl_mcp.plugins.grid_extension.creation.engine import (
    CreationEngine,
    Topology,
    resolve_connection_point,
)
from pypowsybl_mcp.plugins.grid_extension.creation.topology import TopologyEngine


@pytest.fixture
def engine():
    return CreationEngine()


@pytest.fixture
def bus_breaker():
    """IEEE14: configured buses are B1..B14, bus-view buses are VL1_0..."""
    return pp.network.create_ieee14()


@pytest.fixture
def node_breaker():
    return pp.network.create_four_substations_node_breaker_network()


@pytest.fixture
def site(engine, bus_breaker):
    """A new two-voltage site connected to IEEE14."""
    engine.create(bus_breaker, "substation", {"id": "S_DC", "country": "FR"})
    engine.create(
        bus_breaker,
        "voltage_level",
        {"id": "VL_A", "substation_id": "S_DC", "nominal_v": 135.0},
    )
    engine.create(
        bus_breaker,
        "voltage_level",
        {"id": "VL_B", "substation_id": "S_DC", "nominal_v": 63.0},
    )
    engine.create(
        bus_breaker,
        "two_windings_transformer",
        {"id": "TR", "bus1": "VL_A_1_1", "bus2": "VL_B_1_1", "r": 0.2, "x": 10.0},
    )
    engine.create(
        bus_breaker,
        "line",
        {"id": "LINE_DC", "bus1": "B4", "bus2": "VL_A_1_1", "r": 0.5, "x": 5.0},
    )
    return bus_breaker


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


def test_create_substation(engine, bus_breaker):
    report = engine.create(
        bus_breaker, "substation", {"id": "S_DC", "country": "FR", "tso": "RTE"}
    )

    assert "created substation 'S_DC'" in str(report)
    assert bus_breaker.get_substations().loc["S_DC", "country"] == "FR"


def test_create_voltage_level_reports_its_connection_points(engine, bus_breaker):
    engine.create(bus_breaker, "substation", {"id": "S_DC"})

    report = engine.create(
        bus_breaker,
        "voltage_level",
        {
            "id": "VL_DC",
            "substation_id": "S_DC",
            "nominal_v": 135.0,
            "low_voltage_limit": 128.0,
            "high_voltage_limit": 145.0,
        },
    )

    # The generated ids are what every following creation needs.
    assert "connection points: VL_DC_1_1" in str(report)
    levels = bus_breaker.get_voltage_levels()
    assert levels.loc["VL_DC", "nominal_v"] == 135.0
    assert levels.loc["VL_DC", "low_voltage_limit"] == 128.0
    assert "VL_DC_1_1" in bus_breaker.get_bus_breaker_view_buses().index


def test_a_voltage_level_whose_topology_fails_is_not_left_behind(engine, bus_breaker):
    engine.create(bus_breaker, "substation", {"id": "S_DC"})
    attributes = {
        "id": "VL_DC",
        "substation_id": "S_DC",
        "nominal_v": 135.0,
        "topology_kind": "NODE_BREAKER",
        "section_count": 2,
    }

    with pytest.raises(pp.PyPowsyblError):
        engine.create(
            bus_breaker, "voltage_level", {**attributes, "switch_kinds": "NOPE"}
        )

    assert "VL_DC" not in bus_breaker.get_voltage_levels().index
    # so the corrected call is not refused as a duplicate id
    engine.create(bus_breaker, "voltage_level", attributes)
    assert "VL_DC" in bus_breaker.get_voltage_levels().index


def test_create_node_breaker_voltage_level_with_several_busbars(engine, node_breaker):
    engine.create(node_breaker, "substation", {"id": "S_NEW"})

    engine.create(
        node_breaker,
        "voltage_level",
        {
            "id": "VL_NEW",
            "substation_id": "S_NEW",
            "nominal_v": 400.0,
            "topology_kind": "NODE_BREAKER",
            "aligned_buses_or_busbar_count": 2,
            "section_count": 2,
        },
    )

    sections = node_breaker.get_busbar_sections()
    assert len(sections[sections["voltage_level_id"] == "VL_NEW"]) == 4


def _uncoupled_notes(report) -> list[str]:
    return [note for note in report.notes if "not coupled" in note]


@pytest.mark.parametrize("topology_kind", ["BUS_BREAKER", "NODE_BREAKER"])
def test_several_busbars_come_with_a_note_that_they_are_not_coupled(
    engine, topology_kind
):
    """pypowsybl leaves parallel busbars apart: each is a node of its own."""
    network = pp.network.create_empty()
    engine.create(network, "substation", {"id": "S"})
    report = engine.create(
        network,
        "voltage_level",
        {
            "id": "V",
            "substation_id": "S",
            "nominal_v": 400.0,
            "topology_kind": topology_kind,
            "aligned_buses_or_busbar_count": 3,
            "section_count": 2,
        },
    )
    (note,) = _uncoupled_notes(report)
    assert "its 3 busbars are not coupled" in note
    assert "create_coupling_device" in note

    for load_id, point in (("LA", "V_1_1"), ("LB", "V_3_2")):
        engine.create(
            network,
            "load",
            {"id": load_id, "bus_or_busbar_section_id": point, "p0": 1.0},
        )
    buses = network.get_loads()["bus_id"]
    assert buses["LA"] != buses["LB"]

    # What the note says to do joins them: one coupler per extra busbar.
    for other in ("V_2_1", "V_3_1"):
        TopologyEngine().apply(
            network,
            "create_coupling_device",
            {
                "bus_or_busbar_section_id_1": "V_1_1",
                "bus_or_busbar_section_id_2": other,
            },
        )
    buses = network.get_loads()["bus_id"]
    assert buses["LA"] == buses["LB"]


def test_a_single_busbar_needs_no_coupling_note(engine, bus_breaker):
    engine.create(bus_breaker, "substation", {"id": "S_DC"})

    report = engine.create(
        bus_breaker,
        "voltage_level",
        {
            "id": "VL_DC",
            "substation_id": "S_DC",
            "nominal_v": 135.0,
            "section_count": 3,
        },
    )

    assert _uncoupled_notes(report) == []


# ---------------------------------------------------------------------------
# Injections and branches
# ---------------------------------------------------------------------------


def test_create_load_with_defaults(engine, bus_breaker):
    report = engine.create(
        bus_breaker, "load", {"id": "DC", "bus_or_busbar_section_id": "B4", "p0": 300.0}
    )

    assert "created load 'DC' (p0=300.0 MW) connected to 'B4' in VL4" in str(report)
    loads = bus_breaker.get_loads()
    assert loads.loc["DC", "p0"] == 300.0
    assert loads.loc["DC", "q0"] == 0.0
    assert bool(loads.loc["DC", "connected"])


def test_create_load_on_a_busbar_section_builds_the_bay(engine, node_breaker):
    switches_before = len(node_breaker.get_switches())

    engine.create(node_breaker, "load", {"id": "DC", "bus": "S1VL2_BBS1", "p0": 150.0})

    # The caller named a busbar section; the breaker and disconnector that
    # attach the load to it were built by the bay.
    assert len(node_breaker.get_switches()) > switches_before
    assert node_breaker.get_loads().loc["DC", "voltage_level_id"] == "S1VL2"


def test_create_generator_defaults_target_q_when_not_regulating(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "generator",
        {"id": "G", "bus": "B4", "target_p": 50.0, "max_p": 60.0},
    )

    generators = bus_breaker.get_generators()
    assert generators.loc["G", "target_q"] == 0.0
    assert not bool(generators.loc["G", "voltage_regulator_on"])


def test_create_generator_regulating(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "generator",
        {
            "id": "G",
            "bus": "B4",
            "target_p": 50.0,
            "max_p": 60.0,
            "voltage_regulator_on": True,
            "target_v": 137.0,
            "energy_source": "solar",
        },
    )

    generators = bus_breaker.get_generators()
    assert bool(generators.loc["G", "voltage_regulator_on"])
    # Enumerated values are accepted in any case and normalized.
    assert generators.loc["G", "energy_source"] == "SOLAR"


def test_create_battery_defaults_to_a_symmetric_range(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "battery",
        {"id": "BESS", "bus": "B4", "target_p": 20.0, "max_p": 50.0},
    )

    assert bus_breaker.get_batteries().loc["BESS", "min_p"] == -50.0


def test_create_shunt_compensator_uses_the_linear_model(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "shunt_compensator",
        {
            "id": "CAP",
            "bus": "B4",
            "b_per_section": 0.003,
            "max_section_count": 3,
        },
    )

    shunts = bus_breaker.get_shunt_compensators()
    assert shunts.loc["CAP", "max_section_count"] == 3
    assert shunts.loc["CAP", "voltage_level_id"] == "VL4"


def test_create_static_var_compensator(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "static_var_compensator",
        {"id": "SVC", "bus": "B4", "b_min": -0.01, "b_max": 0.01, "target_v": 137.0},
    )

    assert (
        bus_breaker.get_static_var_compensators().loc["SVC", "regulation_mode"]
        == "VOLTAGE"
    )


def test_create_ground_only_in_bus_breaker(engine, bus_breaker, node_breaker):
    report = engine.create(bus_breaker, "ground", {"id": "GND", "bus": "B4"})
    assert "created ground 'GND'" in str(report)
    assert "GND" in bus_breaker.get_grounds().index

    with pytest.raises(CreationError, match="no bay creation for a ground"):
        engine.create(node_breaker, "ground", {"id": "GND", "bus": "S1VL2_BBS1"})


def test_create_line(engine, bus_breaker):
    engine.create(bus_breaker, "substation", {"id": "S_DC"})
    engine.create(
        bus_breaker,
        "voltage_level",
        {"id": "VL_DC", "substation_id": "S_DC", "nominal_v": 135.0},
    )

    report = engine.create(
        bus_breaker,
        "line",
        {"id": "L", "bus1": "B4", "bus2": "VL_DC_1_1", "r": 0.5, "x": 5.0},
    )

    assert "between 'B4' in VL4 and 'VL_DC_1_1' in VL_DC" in str(report)
    lines = bus_breaker.get_lines()
    assert lines.loc["L", "voltage_level1_id"] == "VL4"
    assert lines.loc["L", "x"] == 5.0


def test_create_transformer_derives_the_rated_voltages(engine, site):
    transformers = site.get_2_windings_transformers()

    assert transformers.loc["TR", "rated_u1"] == 135.0
    assert transformers.loc["TR", "rated_u2"] == 63.0


def test_create_hvdc_link(engine, node_breaker):
    """A type the previous per-type surface never covered."""
    engine.create(
        node_breaker,
        "vsc_converter_station",
        {"id": "VSC_A", "bus": "S1VL2_BBS1", "loss_factor": 1.1},
    )
    engine.create(
        node_breaker,
        "vsc_converter_station",
        {"id": "VSC_B", "bus": "S2VL1_BBS", "loss_factor": 1.1},
    )

    report = engine.create(
        node_breaker,
        "hvdc_line",
        {
            "id": "HVDC_NEW",
            "converter_station1_id": "VSC_A",
            "converter_station2_id": "VSC_B",
            "r": 1.0,
            "nominal_v": 400.0,
            "max_p": 300.0,
            "target_p": 200.0,
        },
    )

    assert "created hvdc_line 'HVDC_NEW'" in str(report)
    assert "HVDC_NEW" in node_breaker.get_hvdc_lines().index


def test_a_non_linear_shunt_from_its_sections(engine, node_breaker):
    report = engine.create(
        node_breaker,
        "shunt_compensator",
        {
            "id": "SH_NL",
            "bus": "S1VL2_BBS1",
            "sections": [{"b": 1e-4}, {"b": 2.5e-4, "g": 0.0}],
            "section_count": 2,
        },
    )

    assert "created shunt_compensator 'SH_NL'" in str(report)
    shunt = node_breaker.get_shunt_compensators(all_attributes=True).loc["SH_NL"]
    assert shunt["model_type"] == "NON_LINEAR"
    # the susceptance of a section is the total with it and those before in
    assert shunt["b"] == pytest.approx(2.5e-4)
    sections = node_breaker.get_non_linear_shunt_compensator_sections().loc["SH_NL"]
    assert sections["g"].tolist() == [0.0, 0.0]


def test_a_shunt_is_linear_unless_sections_are_given(engine, node_breaker):
    engine.create(
        node_breaker,
        "shunt_compensator",
        {"id": "SH_L", "bus": "S1VL2_BBS1", "b_per_section": 1e-4},
    )

    shunt = node_breaker.get_shunt_compensators(all_attributes=True).loc["SH_L"]
    assert shunt["model_type"] == "LINEAR"


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        (
            {"b_per_section": 1e-4, "sections": [{"b": 1e-4}]},
            "cannot be given together",
        ),
        ({"sections": [{"g": 0.0}]}, "Row 1 of 'sections' has no 'b'"),
        (
            {"sections": [{"b": 1e-4}], "section_count": 2},
            "must not exceed the number of rows of 'sections'",
        ),
        ({}, "'b_per_section' is required"),
        ({"sections": [{"b": 1e-4, "bee": 1}]}, "no column 'bee'"),
    ],
    ids=["both-models", "section-without-b", "too-many-in-service", "no-model", "typo"],
)
def test_shunt_section_mistakes_are_caught(engine, node_breaker, attributes, message):
    with pytest.raises(CreationError, match=message):
        engine.create(
            node_breaker,
            "shunt_compensator",
            {"id": "SH_BAD", "bus": "S1VL2_BBS1", **attributes},
        )

    assert "SH_BAD" not in node_breaker.get_shunt_compensators().index


def test_a_boundary_line_is_created_with_its_bay(engine, node_breaker):
    switches = len(node_breaker.get_switches())

    report = engine.create(
        node_breaker,
        "boundary_line",
        {"id": "BL", "bus": "S1VL2_BBS1", "r": 1.0, "x": 10.0, "p0": 20.0},
    )

    assert "created boundary_line 'BL'" in str(report)
    assert len(node_breaker.get_switches()) > switches
    assert "BL" not in node_breaker.get_boundary_lines_generation().index


def test_a_boundary_line_with_a_regulating_generation_part(engine, node_breaker):
    engine.create(
        node_breaker,
        "boundary_line",
        {
            "id": "BL",
            "bus": "S3VL1_BBS",
            "r": 1.0,
            "x": 10.0,
            "min_p": 0.0,
            "max_p": 100.0,
            "target_p": 40.0,
            "voltage_regulator_on": True,
            "target_v": 400.0,
        },
    )

    generation = node_breaker.get_boundary_lines_generation().loc["BL"]
    assert generation["target_p"] == 40.0
    assert generation["voltage_regulator_on"]
    results = pp.loadflow.run_ac(node_breaker)
    assert results[0].status == pp.loadflow.ComponentStatus.CONVERGED


def test_a_partial_generation_part_is_refused(engine, node_breaker):
    with pytest.raises(CreationError, match="generation part needs 'min_p', 'max_p'"):
        engine.create(
            node_breaker,
            "boundary_line",
            {"id": "BL", "bus": "S1VL2_BBS1", "r": 1.0, "x": 10.0, "target_p": 5.0},
        )


def test_a_tie_line_comes_after_its_boundary_lines_in_a_batch(engine, node_breaker):
    batch = engine.create_many(
        node_breaker,
        [
            {
                "element_type": "tie_line",
                "id": "TL",
                "boundary_line1_id": "BL1",
                "boundary_line2_id": "BL2",
            },
            {
                "element_type": "boundary_line",
                "id": "BL1",
                "bus": "S1VL2_BBS1",
                "r": 1.0,
                "x": 10.0,
                "pairing_key": "XNODE",
            },
            {
                "element_type": "boundary_line",
                "id": "BL2",
                "bus": "S3VL1_BBS",
                "r": 1.0,
                "x": 10.0,
                "pairing_key": "XNODE",
            },
        ],
    )

    assert batch.success
    assert batch.items[-1]["element_type"] == "tie_line"
    assert "TL" in node_breaker.get_tie_lines().index


# ---------------------------------------------------------------------------
# Attached to an existing element
# ---------------------------------------------------------------------------


def test_operational_limits_make_a_branch_overloadable(engine, site):
    report = engine.create(
        site,
        "operational_limits",
        {
            "element_id": "LINE_DC",
            "permanent_limit": 300.0,
            "temporary_limits": [{"value": 400.0, "acceptable_duration": 1200}],
        },
    )

    assert "set operational_limits on line 'LINE_DC'" in str(report)
    limits = site.get_operational_limits()
    rows = limits[limits.index.get_level_values("element_id") == "LINE_DC"]
    # Permanent and temporary, on both sides, in the group the analyses read.
    assert len(rows) == 4
    assert set(rows.index.get_level_values("acceptable_duration")) == {-1, 1200}


def _limits_of(network, element_id):
    limits = network.get_operational_limits(
        all_attributes=True, show_inactive_sets=True
    ).reset_index()
    return limits[limits["element_id"] == element_id]


def test_operational_limits_go_in_the_group_named_once(engine, site):
    # given once for the call, the group used to be dropped for DEFAULT
    engine.create(
        site,
        "operational_limits",
        {"element_id": "LINE_DC", "permanent_limit": 300.0, "group_name": "WINTER"},
    )

    assert set(_limits_of(site, "LINE_DC")["group_name"]) == {"WINTER"}


@pytest.mark.parametrize(
    "attributes",
    [
        {"permanent_limit": 300.0, "group_name": "WINTER"},
        {
            "limits": [
                {"value": 300.0, "acceptable_duration": -1, "group_name": "WINTER"}
            ]
        },
    ],
)
def test_limits_in_a_named_group_come_with_a_note(engine, site, attributes):
    """The analyses only read the selected group, which a named one is not."""
    report = engine.create(
        site, "operational_limits", {"element_id": "LINE_DC", **attributes}
    )

    (note,) = [note for note in report.notes if "selected group" in note]
    assert "group(s) WINTER" in note
    assert "without group_name" in note
    selected = site.get_operational_limits().reset_index()
    assert "LINE_DC" not in set(selected["element_id"])


@pytest.mark.parametrize("group", [None, "DEFAULT"])
def test_limits_in_the_default_group_need_no_note(engine, site, group):
    attributes = {"element_id": "LINE_DC", "permanent_limit": 300.0}
    if group:
        attributes["group_name"] = group

    report = engine.create(site, "operational_limits", attributes)

    assert not [note for note in report.notes if "selected group" in note]


def test_messages_put_the_right_article_before_a_type(engine):
    assert with_article("load") == "a load"
    assert with_article("operational_limits") == "an operational_limits"
    assert with_article("integer") == "an integer"
    rules = engine.describe("operational_limits")["rules"]
    assert "'limits' needs at least 1 entry" in rules
    assert any("'group_name' is set to anything but 'DEFAULT'" in r for r in rules)


def test_explicit_limit_rows_take_the_side_named_once(engine, site):
    engine.create(
        site,
        "operational_limits",
        {
            "element_id": "LINE_DC",
            "side": "TWO",
            "limits": [{"value": 300.0, "acceptable_duration": -1}],
        },
    )
    engine.create(
        site,
        "operational_limits",
        {
            "element_id": "TR",
            "limits": [{"value": 500.0, "acceptable_duration": -1}],
        },
    )

    assert list(_limits_of(site, "LINE_DC")["side"]) == ["TWO"]
    # a transformer is rated on side ONE by default: side TWO follows the ratio
    assert list(_limits_of(site, "TR")["side"]) == ["ONE"]


def test_a_line_is_rated_on_both_ends_by_default(engine, site):
    engine.create(
        site, "operational_limits", {"element_id": "LINE_DC", "permanent_limit": 300.0}
    )

    assert sorted(_limits_of(site, "LINE_DC")["side"]) == ["ONE", "TWO"]


def test_a_transformer_is_rated_on_side_one_and_refuses_both(engine, site):
    # the same amps on both windings overloaded the low-voltage one
    engine.create(
        site, "operational_limits", {"element_id": "TR", "permanent_limit": 500.0}
    )
    assert list(_limits_of(site, "TR")["side"]) == ["ONE"]

    with pytest.raises(CreationError, match="side 'BOTH' cannot be used .* ratio"):
        engine.create(
            site,
            "operational_limits",
            {"element_id": "TR", "permanent_limit": 500.0, "side": "BOTH"},
        )
    with pytest.raises(CreationError, match="side 'BOTH' cannot be used"):
        engine.create(
            site,
            "operational_limits",
            {
                "element_id": "TR",
                "limits": [{"side": "BOTH", "value": 500.0, "acceptable_duration": -1}],
            },
        )
    with pytest.raises(CreationError, match="side 'THREE' does not exist"):
        engine.create(
            site,
            "operational_limits",
            {"element_id": "TR", "permanent_limit": 500.0, "side": "THREE"},
        )

    # side TWO in its own amps adds to side ONE rather than replacing it
    engine.create(
        site,
        "operational_limits",
        {"element_id": "TR", "permanent_limit": 1000.0, "side": "TWO"},
    )
    rows = _limits_of(site, "TR").set_index("side")["value"]
    assert rows.to_dict() == {"ONE": 500.0, "TWO": 1000.0}


def test_each_leg_of_a_three_windings_transformer_is_rated_on_its_own(engine):
    network = pp.network.create_micro_grid_be_network()
    leg_id = network.get_3_windings_transformers().index[0]

    # no leg is derived from another: a side must be named, and BOTH is refused
    with pytest.raises(CreationError, match="'side' is required .* one call per side"):
        engine.create(
            network,
            "operational_limits",
            {"element_id": leg_id, "permanent_limit": 50.0},
        )
    with pytest.raises(CreationError, match="side 'BOTH' cannot be used"):
        engine.create(
            network,
            "operational_limits",
            {"element_id": leg_id, "permanent_limit": 50.0, "side": "BOTH"},
        )

    engine.create(
        network,
        "operational_limits",
        {"element_id": leg_id, "permanent_limit": 50.0, "side": "THREE"},
    )
    rows = _limits_of(network, leg_id)
    third = rows[(rows["side"] == "THREE") & (rows["acceptable_duration"] == -1)]
    assert list(third["value"]) == [50.0]


def test_a_boundary_line_is_rated_on_its_single_side(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "boundary_line",
        {"id": "BL", "bus": "B4", "r": 1.0, "x": 10.0},
    )

    engine.create(
        bus_breaker,
        "operational_limits",
        {"element_id": "BL", "permanent_limit": 400.0},
    )
    assert list(_limits_of(bus_breaker, "BL")["side"]) == ["NONE"]

    # pypowsybl refused ONE and TWO with "must be NONE"; now refused up front
    with pytest.raises(CreationError, match="side 'ONE' does not exist .* NONE"):
        engine.create(
            bus_breaker,
            "operational_limits",
            {"element_id": "BL", "permanent_limit": 400.0, "side": "ONE"},
        )


def test_virtual_attributes_are_coerced_like_real_ones(engine, site):
    # a numeric string reached the rules uncoerced and crashed them
    engine.create(
        site,
        "operational_limits",
        {"element_id": "LINE_DC", "permanent_limit": "300"},
    )
    engine.create(site, "ratio_tap_changer", {"element_id": "TR", "step_count": "5"})

    limits = site.get_operational_limits()
    rows = limits[limits.index.get_level_values("element_id") == "LINE_DC"]
    assert set(rows["value"]) == {300.0}
    assert site.get_ratio_tap_changers().loc["TR", "high_tap"] == 4


def test_a_virtual_attribute_of_the_wrong_type_is_refused(engine, site):
    with pytest.raises(CreationError, match="'permanent_limit' .* must be a number"):
        engine.create(
            site,
            "operational_limits",
            {"element_id": "LINE_DC", "permanent_limit": "a lot"},
        )


@pytest.mark.parametrize(
    ("temporary", "message"),
    [
        ([600], "must be a list of objects"),
        (
            [{"value": 400.0}],
            "Row 1 of 'temporary_limits' has no 'acceptable_duration'",
        ),
        (
            [{"value": 400.0, "acceptable_duration": 60, "duraton": 1}],
            "no column 'duraton'",
        ),
        ([{"value": "high", "acceptable_duration": 60}], "must be a number"),
    ],
    ids=["not-objects", "missing-column", "unknown-column", "bad-type"],
)
def test_temporary_limits_are_checked_row_by_row(engine, site, temporary, message):
    with pytest.raises(CreationError, match=message):
        engine.create(
            site,
            "operational_limits",
            {
                "element_id": "LINE_DC",
                "permanent_limit": 300.0,
                "temporary_limits": temporary,
            },
        )


def test_batch_checks_virtual_tables_before_creating_anything(engine, site):
    with pytest.raises(CreationError, match="item 2: .*temporary_limits"):
        engine.create_many(
            site,
            [
                {"element_type": "substation", "id": "S_NEW"},
                {
                    "element_type": "operational_limits",
                    "element_id": "LINE_DC",
                    "permanent_limit": 300.0,
                    "temporary_limits": [{"value": 400.0}],
                },
            ],
        )

    assert "S_NEW" not in site.get_substations().index


def test_minmax_reactive_limits(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "minmax_reactive_limits",
        {"id": "B1-G", "min_q": -100.0, "max_q": 100.0},
    )

    generators = bus_breaker.get_generators()
    assert generators.loc["B1-G", "min_q"] == -100.0
    assert generators.loc["B1-G", "max_q"] == 100.0


def test_reactive_limits_tell_a_vsc_station_from_an_lcc_one(engine, node_breaker):
    # both are HVDC_CONVERTER_STATION to get_identifiables(): an LCC station
    # passed the check and pypowsybl then refused it, a VSC was never matched
    report = engine.create(
        node_breaker,
        "minmax_reactive_limits",
        {"element_id": "VSC1", "min_q": -10.0, "max_q": 10.0},
    )
    assert "on vsc_converter_station 'VSC1'" in str(report)

    with pytest.raises(
        CreationError, match="cannot be attached to a lcc_converter_station"
    ):
        engine.create(
            node_breaker,
            "minmax_reactive_limits",
            {"element_id": "LCC1", "min_q": -10.0, "max_q": 10.0},
        )


def test_limits_are_refused_on_a_tie_line_and_go_to_its_boundary_lines(engine):
    network = pp.network.create_eurostag_tutorial_example1_with_tie_lines_and_areas()
    with pytest.raises(CreationError, match="cannot be attached to a tie_line"):
        engine.create(
            network,
            "operational_limits",
            {"element_id": "NHV1_NHV2_1", "permanent_limit": 700.0},
        )

    for boundary_line in ("NHV1_XNODE1", "XNODE1_NHV2"):
        engine.create(
            network,
            "operational_limits",
            {"element_id": boundary_line, "permanent_limit": 700.0},
        )
    assert sorted(_limits_of(network, "NHV1_NHV2_1")["side"]) == ["ONE", "TWO"]


def test_reactive_capability_curve(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "reactive_capability_curve_point",
        {
            "element_id": "B1-G",
            "points": [
                {"p": 0.0, "min_q": -30.0, "max_q": 30.0},
                {"p": 50.0, "min_q": -40.0, "max_q": 40.0},
                {"p": 100.0, "min_q": -20.0, "max_q": 20.0},
            ],
        },
    )

    assert len(bus_breaker.get_reactive_capability_curve_points().loc["B1-G"]) == 3


def test_ratio_tap_changer_generates_its_steps(engine, site):
    engine.create(
        site,
        "ratio_tap_changer",
        {"element_id": "TR", "regulating": True, "target_v": 63.0},
    )

    changers = site.get_ratio_tap_changers()
    assert changers.loc["TR", "low_tap"] == 0
    assert changers.loc["TR", "high_tap"] == 16
    # commissioned on the neutral middle step
    assert changers.loc["TR", "tap"] == 8
    steps = site.get_ratio_tap_changer_steps().loc["TR"]
    assert steps.iloc[0]["rho"] == pytest.approx(0.9)
    assert steps.iloc[-1]["rho"] == pytest.approx(1.1)


def test_a_tap_changer_numbered_from_low_tap_starts_on_its_middle_step(engine, site):
    # positions run from low_tap: the middle of -8..8 is 0, not 8
    engine.create(site, "ratio_tap_changer", {"element_id": "TR", "low_tap": -8})

    changers = site.get_ratio_tap_changers()
    assert changers.loc["TR", "high_tap"] == 8
    assert changers.loc["TR", "tap"] == 0


def test_a_misspelled_step_column_is_rejected_not_dropped(engine, site):
    with pytest.raises(CreationError, match="no column 'rh0'. Did you mean 'rho'"):
        engine.create(
            site,
            "ratio_tap_changer",
            {"element_id": "TR", "steps": [{"rh0": 0.9}, {"rh0": 1.0}]},
        )

    assert "TR" not in site.get_ratio_tap_changers().index


def test_step_values_are_checked_and_coerced(engine, site):
    with pytest.raises(CreationError, match="'rho' of a ratio_tap_changer"):
        engine.create(
            site,
            "ratio_tap_changer",
            {"element_id": "TR", "steps": [{"rho": "high"}, {"rho": 1.0}]},
        )

    engine.create(
        site,
        "ratio_tap_changer",
        {"element_id": "TR", "steps": [{"rho": "0.95"}, {"rho": 1.05}]},
    )
    assert site.get_ratio_tap_changer_steps().loc["TR"].iloc[0]["rho"] == 0.95


def test_rows_must_be_a_list_of_objects(engine, site):
    with pytest.raises(CreationError, match="must be a list of objects"):
        engine.create(
            site, "ratio_tap_changer", {"element_id": "TR", "steps": [0.9, 1.1]}
        )


def test_ragged_curve_points_are_rejected_with_the_missing_column(engine, bus_breaker):
    with pytest.raises(CreationError, match="Row 2 of 'points' has no 'min_q'"):
        engine.create(
            bus_breaker,
            "reactive_capability_curve_point",
            {
                "element_id": "B1-G",
                "points": [
                    {"p": 0.0, "min_q": -100.0, "max_q": 100.0},
                    {"p": 500.0, "max_q": 50.0},
                ],
            },
        )


def test_ratio_tap_changer_accepts_explicit_steps(engine, site):
    engine.create(
        site,
        "ratio_tap_changer",
        {"element_id": "TR", "steps": [{"rho": 0.95}, {"rho": 1.0}, {"rho": 1.05}]},
    )

    steps = site.get_ratio_tap_changer_steps().loc["TR"]
    assert len(steps) == 3
    assert steps.iloc[0]["rho"] == pytest.approx(0.95)


def test_created_tap_changer_can_then_be_moved(engine, site):
    engine.create(site, "ratio_tap_changer", {"element_id": "TR"})

    site.update_ratio_tap_changers(id="TR", tap=12)

    assert site.get_ratio_tap_changers().loc["TR", "tap"] == 12


def test_phase_tap_changer(engine, site):
    engine.create(
        site,
        "phase_tap_changer",
        {
            "element_id": "TR",
            "regulating": True,
            "regulation_mode": "ACTIVE_POWER_CONTROL",
            "target_value": 50.0,
        },
    )

    changers = site.get_phase_tap_changers()
    assert changers.loc["TR", "regulation_mode"] == "ACTIVE_POWER_CONTROL"
    assert changers.loc["TR", "regulation_value"] == 50.0
    steps = site.get_phase_tap_changer_steps().loc["TR"]
    assert steps.iloc[0]["alpha"] == pytest.approx(-10.0)
    assert steps.iloc[-1]["alpha"] == pytest.approx(10.0)


REGULATING = {
    "ratio_tap_changer": {"regulating": True, "target_v": 63.0},
    "phase_tap_changer": {
        "regulating": True,
        "regulation_mode": "CURRENT_LIMITER",
        "target_value": 500.0,
    },
}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("ratio_tap_changer", "phase_tap_changer"),
        ("phase_tap_changer", "ratio_tap_changer"),
    ],
)
def test_only_one_tap_changer_of_a_transformer_regulates(engine, site, first, second):
    # pypowsybl refused the second one but left it in the network, and every
    # load flow then failed: "Voltage and phase control on same branch"
    engine.create(site, first, {"element_id": "TR", **REGULATING[first]})

    with pytest.raises(CreationError, match=f"already has a regulating {first}"):
        engine.create(site, second, {"element_id": "TR", **REGULATING[second]})

    second_table = (
        site.get_phase_tap_changers()
        if second == "phase_tap_changer"
        else site.get_ratio_tap_changers()
    )
    assert "TR" not in second_table.index
    assert pp.loadflow.run_ac(site)[0].status.name == "CONVERGED"


def test_the_regulation_can_be_handed_over_to_the_other_tap_changer(engine, site):
    engine.create(
        site,
        "ratio_tap_changer",
        {"element_id": "TR", "regulating": True, "target_v": 63.0},
    )
    # a fixed phase shifter beside a regulating ratio tap changer is fine
    engine.create(site, "phase_tap_changer", {"element_id": "TR"})

    engine.create(site, "ratio_tap_changer", {"element_id": "TR", "regulating": False})
    engine.create(
        site,
        "phase_tap_changer",
        {"element_id": "TR", **REGULATING["phase_tap_changer"]},
    )

    assert not site.get_ratio_tap_changers().loc["TR", "regulating"]
    assert site.get_phase_tap_changers().loc["TR", "regulating"]


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_datacenter_connection_converges(engine, site):
    engine.create(
        site, "load", {"id": "DC", "bus": "VL_B_1_1", "p0": 100.0, "q0": 20.0}
    )
    engine.create(
        site, "operational_limits", {"id": "LINE_DC", "permanent_limit": 300.0}
    )

    results = pp.loadflow.run_ac(site)

    assert results[0].status == pp.loadflow.ComponentStatus.CONVERGED
    # The site draws its power through the new line and the new transformer.
    assert site.get_lines().loc["LINE_DC", "p1"] > 0
    assert site.get_2_windings_transformers().loc["TR", "p1"] > 0


# ---------------------------------------------------------------------------
# Connection point diagnostics
# ---------------------------------------------------------------------------


def test_bus_view_bus_is_refused_with_the_ids_that_work(engine, bus_breaker):
    with pytest.raises(CreationError) as error:
        engine.create(bus_breaker, "load", {"id": "X", "bus": "VL1_0", "p0": 1.0})

    assert "bus of the bus view" in str(error.value)
    assert "B1" in str(error.value)


def test_bus_breaker_view_bus_is_refused_in_node_breaker(engine, node_breaker):
    with pytest.raises(CreationError) as error:
        engine.create(node_breaker, "load", {"id": "X", "bus": "S1VL2_0", "p0": 1.0})

    assert "NODE_BREAKER" in str(error.value)
    assert "S1VL2_BBS1" in str(error.value)


def test_unknown_connection_point(engine, bus_breaker):
    with pytest.raises(CreationError, match="not found in this network"):
        engine.create(bus_breaker, "load", {"id": "X", "bus": "NOPE", "p0": 1.0})


def test_missing_connection_point(engine, bus_breaker):
    with pytest.raises(CreationError, match="is required to connect"):
        engine.create(bus_breaker, "load", {"id": "X", "p0": 1.0})


class CountingNetwork:
    """A network that counts the tables read from it."""

    def __init__(self, network):
        self.network = network
        self.reads: dict[str, int] = {}

    def __getattr__(self, name):
        attribute = getattr(self.network, name)

        def counted(*args, **kwargs):
            self.reads[name] = self.reads.get(name, 0) + 1
            return attribute(*args, **kwargs)

        return counted


def test_both_ends_are_looked_up_in_the_same_tables(bus_breaker):
    network = CountingNetwork(bus_breaker)
    topology = Topology(network)

    first = resolve_connection_point(network, "B4", topology)
    second = resolve_connection_point(network, "B5", topology)

    assert (first.voltage_level_id, second.voltage_level_id) == ("VL4", "VL5")
    assert network.reads["get_busbar_sections"] == 1
    assert network.reads["get_bus_breaker_view_buses"] == 1
    # the bus view is only read to explain a mistake
    assert "get_buses" not in network.reads


def test_resolve_connection_point_rejects_an_empty_id(bus_breaker):
    with pytest.raises(CreationError, match="is required"):
        resolve_connection_point(bus_breaker, "")


# ---------------------------------------------------------------------------
# Schema-level validation
# ---------------------------------------------------------------------------


def test_unknown_element_type_suggests_a_close_one(engine, bus_breaker):
    with pytest.raises(CreationError) as error:
        engine.create(bus_breaker, "loed", {"id": "X"})

    assert "'load'" in str(error.value)
    assert error.value.hint["supported_element_types"]


def test_unknown_attribute_suggests_a_close_one(engine, bus_breaker):
    with pytest.raises(CreationError) as error:
        engine.create(bus_breaker, "load", {"id": "X", "bus": "B4", "pO": 1.0})

    assert "has no attribute 'pO'" in str(error.value)
    assert "'p0'" in str(error.value)
    assert "p0" in error.value.hint["accepted_attributes"]


def test_engine_managed_attributes_are_refused(engine, bus_breaker):
    with pytest.raises(CreationError, match="set automatically"):
        engine.create(
            bus_breaker, "load", {"id": "X", "bus": "B4", "p0": 1.0, "node": 3}
        )


def test_missing_required_attribute_carries_the_descriptor(engine, bus_breaker):
    with pytest.raises(CreationError) as error:
        engine.create(bus_breaker, "load", {"id": "X", "bus": "B4"})

    assert "needs 'p0'" in str(error.value)
    assert "p0 in MW" in str(error.value)
    # the descriptor comes with the rejection, so the call can be corrected
    assert error.value.hint["element_type"] == "load"
    assert [item["name"] for item in error.value.hint["required"]] == ["p0"]


def test_missing_id(engine, bus_breaker):
    with pytest.raises(CreationError, match="'id' is required"):
        engine.create(bus_breaker, "load", {"bus": "B4", "p0": 1.0})


def test_duplicate_id(engine, bus_breaker):
    with pytest.raises(CreationError, match="already used by a substation"):
        engine.create(bus_breaker, "substation", {"id": "S1"})


def test_invalid_enum_value_lists_the_accepted_ones(engine, bus_breaker):
    with pytest.raises(CreationError) as error:
        engine.create(
            bus_breaker, "load", {"id": "X", "bus": "B4", "p0": 1.0, "type": "WEIRD"}
        )

    assert "not a valid type" in str(error.value)
    assert "AUXILIARY" in str(error.value)


def test_value_of_the_wrong_type(engine, bus_breaker):
    with pytest.raises(CreationError, match="must be a number"):
        engine.create(bus_breaker, "load", {"id": "X", "bus": "B4", "p0": "much"})


def test_numeric_strings_are_accepted(engine, bus_breaker):
    engine.create(bus_breaker, "load", {"id": "X", "bus": "B4", "p0": "300"})

    assert bus_breaker.get_loads().loc["X", "p0"] == 300.0


@pytest.mark.parametrize("value", ["on", "Yes", 1, True])
def test_boolean_words_are_accepted(engine, bus_breaker, value):
    engine.create(
        bus_breaker,
        "generator",
        {
            "id": "G_NEW",
            "bus": "B4",
            "target_p": 10.0,
            "max_p": 20.0,
            "voltage_regulator_on": value,
            "target_v": 135.0,
        },
    )

    assert bus_breaker.get_generators().loc["G_NEW", "voltage_regulator_on"]


@pytest.mark.parametrize("value", ["ture", "regulating", 2])
def test_a_value_that_is_not_a_boolean_is_refused(engine, bus_breaker, value):
    # anything but a true word used to be read as false, silently
    with pytest.raises(CreationError, match="'voltage_regulator_on' .* a boolean"):
        engine.create(
            bus_breaker,
            "generator",
            {
                "id": "G_NEW",
                "bus": "B4",
                "target_p": 10.0,
                "max_p": 20.0,
                "voltage_regulator_on": value,
                "target_v": 135.0,
            },
        )


def test_attributes_must_be_an_object(engine, bus_breaker):
    with pytest.raises(CreationError, match="must be an object"):
        engine.create(bus_breaker, "load", ["p0"])


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_ordering_rule(engine, bus_breaker):
    with pytest.raises(CreationError, match="must not be greater than 'max_p'"):
        engine.create(
            bus_breaker,
            "generator",
            {"id": "G", "bus": "B4", "target_p": 100.0, "max_p": 50.0},
        )


def test_conditional_requirement(engine, bus_breaker):
    with pytest.raises(CreationError, match="'target_v' is required"):
        engine.create(
            bus_breaker,
            "generator",
            {
                "id": "G",
                "bus": "B4",
                "target_p": 10.0,
                "max_p": 50.0,
                "voltage_regulator_on": True,
            },
        )


def test_value_conditional_requirement(engine, bus_breaker):
    with pytest.raises(CreationError, match="'target_q' is required"):
        engine.create(
            bus_breaker,
            "static_var_compensator",
            {
                "id": "SVC",
                "bus": "B4",
                "b_min": -0.01,
                "b_max": 0.01,
                "regulation_mode": "REACTIVE_POWER",
            },
        )


def test_zero_reactance_is_refused(engine, bus_breaker):
    with pytest.raises(CreationError, match="'x' must not be 0"):
        engine.create(
            bus_breaker,
            "line",
            {"id": "L", "bus1": "B4", "bus2": "B5", "r": 1.0, "x": 0.0},
        )


def test_a_branch_needs_two_distinct_ends(engine, bus_breaker):
    with pytest.raises(CreationError, match="two distinct connection points"):
        engine.create(
            bus_breaker,
            "line",
            {"id": "L", "bus1": "B4", "bus2": "B4", "r": 1.0, "x": 1.0},
        )


def test_a_transformer_stays_inside_one_substation(engine, bus_breaker):
    with pytest.raises(CreationError, match="must stay inside one substation"):
        engine.create(
            bus_breaker,
            "two_windings_transformer",
            {"id": "TR", "bus1": "B4", "bus2": "B5", "r": 1.0, "x": 10.0},
        )


def test_bounded_value(engine, bus_breaker):
    with pytest.raises(CreationError, match="must not exceed 'max_section_count'"):
        engine.create(
            bus_breaker,
            "shunt_compensator",
            {
                "id": "CAP",
                "bus": "B4",
                "b_per_section": 0.01,
                "max_section_count": 2,
                "section_count": 5,
            },
        )


def test_positive_value(engine, bus_breaker):
    engine.create(bus_breaker, "substation", {"id": "S_DC"})

    with pytest.raises(CreationError, match="'nominal_v' must be positive"):
        engine.create(
            bus_breaker,
            "voltage_level",
            {"id": "VL", "substation_id": "S_DC", "nominal_v": -1.0},
        )


def test_minimum_count(engine, bus_breaker):
    engine.create(bus_breaker, "substation", {"id": "S_DC"})

    with pytest.raises(CreationError, match="must be at least 1"):
        engine.create(
            bus_breaker,
            "voltage_level",
            {
                "id": "VL",
                "substation_id": "S_DC",
                "nominal_v": 90.0,
                "aligned_buses_or_busbar_count": 0,
            },
        )


def test_rows_required(engine, bus_breaker):
    with pytest.raises(CreationError, match="at least 2 entries"):
        engine.create(
            bus_breaker,
            "reactive_capability_curve_point",
            {"element_id": "B1-G", "points": [{"p": 0.0, "min_q": -1.0, "max_q": 1.0}]},
        )


def test_different_nominal_voltages_are_noted_not_refused(engine, bus_breaker):
    engine.create(bus_breaker, "substation", {"id": "S400"})
    engine.create(
        bus_breaker,
        "voltage_level",
        {"id": "VL400", "substation_id": "S400", "nominal_v": 400.0},
    )

    report = engine.create(
        bus_breaker,
        "line",
        {"id": "LMIX", "bus1": "B4", "bus2": "VL400_1_1", "r": 1.0, "x": 10.0},
    )

    assert "Note:" in str(report)
    assert "different nominal voltages" in str(report)
    assert "LMIX" in bus_breaker.get_lines().index


def test_attachment_to_the_wrong_element_type(engine, bus_breaker):
    with pytest.raises(CreationError) as error:
        engine.create(
            bus_breaker, "operational_limits", {"id": "B2-L", "permanent_limit": 100.0}
        )

    assert "cannot be attached to a load" in str(error.value)
    assert "line" in error.value.hint["accepted_target_types"]


def test_attachment_to_a_missing_element(engine, bus_breaker):
    with pytest.raises(CreationError, match="not found in this network"):
        engine.create(
            bus_breaker,
            "minmax_reactive_limits",
            {"id": "NOPE", "min_q": -1, "max_q": 1},
        )


# ---------------------------------------------------------------------------
# Element types without a profile
# ---------------------------------------------------------------------------


def test_a_type_without_profile_is_created_from_its_schema(engine, bus_breaker):
    report = engine.create(
        bus_breaker, "bus", {"id": "B_NEW", "voltage_level_id": "VL4"}
    )

    assert "created bus with id='B_NEW'" in str(report)
    assert "has no profile" in str(report)
    assert "B_NEW" in bus_breaker.get_bus_breaker_view_buses().index


def test_schema_only_equipment_takes_part_in_the_load_flow(engine, bus_breaker):
    """A new bus, the breaker coupling it, and a load on it: still a valid grid."""
    batch = engine.create_many(
        bus_breaker,
        [
            # listed before the bus it needs: schema-only types come first
            {"element_type": "load", "id": "L_NEW", "bus": "B_NEW", "p0": 5.0},
            {"element_type": "bus", "id": "B_NEW", "voltage_level_id": "VL4"},
            {
                "element_type": "switch",
                "id": "SW_NEW",
                "voltage_level_id": "VL4",
                "bus1_id": "B4",
                "bus2_id": "B_NEW",
                "kind": "BREAKER",
                "open": "false",
            },
        ],
    )

    assert batch.success
    assert batch.items[-1]["element_type"] == "load"
    assert not bus_breaker.get_switches().loc["SW_NEW", "open"]
    results = pp.loadflow.run_ac(bus_breaker)
    assert results[0].status == pp.loadflow.ComponentStatus.CONVERGED
    assert bus_breaker.get_loads().loc["L_NEW", "p"] == pytest.approx(5.0)


def test_schema_only_types_skip_the_id_check(engine, node_breaker):
    """Their id may name an existing element: an area, a voltage level."""
    engine.create(node_breaker, "area", {"id": "A1", "area_type": "ControlArea"})
    engine.create(
        node_breaker, "area_voltage_levels", {"id": "A1", "voltage_level_id": "S1VL1"}
    )
    report = engine.create(
        node_breaker,
        "internal_connection",
        {"voltage_level_id": "S1VL1", "node1": 100, "node2": 101},
    )

    assert "voltage_level_id='S1VL1'" in str(report)
    assert "S1VL1" in node_breaker.get_areas_voltage_levels()["voltage_level_id"].values


def test_pypowsybl_decides_what_a_schema_only_type_lacks(engine, bus_breaker):
    with pytest.raises(pp.PyPowsyblError, match="already contains"):
        engine.create(bus_breaker, "bus", {"id": "B4", "voltage_level_id": "VL4"})


def test_schema_only_attributes_are_still_checked(engine, bus_breaker):
    with pytest.raises(CreationError, match="Did you mean 'voltage_level_id'"):
        engine.create(bus_breaker, "bus", {"id": "B_NEW", "voltage_level": "VL4"})
    with pytest.raises(
        CreationError, match="'node' of a busbar_section must be an integer"
    ):
        engine.create(
            bus_breaker,
            "busbar_section",
            {"id": "BBS", "voltage_level_id": "VL4", "node": "first"},
        )


def test_only_the_main_table_of_a_schema_only_type_is_accepted(bus_breaker):
    """A multi-dataframe type without a profile: only its first dataframe.

    ``boundary_line`` has a profile now, so its schema-only fallback is built
    explicitly: its generation part is a second dataframe.
    """
    from pypowsybl_mcp.plugins.grid_extension.creation.profiles import (
        schema_only_profile,
    )

    engine = CreationEngine(
        profiles={},
        unprofiled={"boundary_line": schema_only_profile("boundary_line")},
    )
    attributes = {
        "id": "BL",
        "voltage_level_id": "VL4",
        "bus_id": "B4",
        **{name: 0.0 for name in ("p0", "q0", "g", "b")},
        "r": 1.0,
        "x": 1.0,
    }
    with pytest.raises(CreationError, match="has no attribute 'min_p'"):
        engine.create(bus_breaker, "boundary_line", {**attributes, "min_p": 0.0})

    engine.create(bus_breaker, "boundary_line", attributes)
    assert "BL" in bus_breaker.get_boundary_lines().index
    assert "additional tables" in engine.describe("boundary_line")["guidance"]


def test_a_profile_wins_over_the_fallback(engine):
    from pypowsybl_mcp.plugins.grid_extension.creation.profiles import PROFILES

    for name in PROFILES:
        assert not engine.profile_for(name).schema_only


def test_describe_marks_the_schema_only_types(engine):
    listed = {item["element_type"]: item for item in engine.describe()["element_types"]}

    assert "schema only" in listed["switch"]["checked"]
    assert "checked" not in listed["load"]
    assert "schema only" in engine.describe("switch")["checked"]


def test_a_computed_type_is_told_apart_from_an_unknown_one(engine, bus_breaker):
    with pytest.raises(CreationError, match="pypowsybl computes it"):
        engine.create(bus_breaker, "branch", {"id": "X"})
    with pytest.raises(CreationError, match="does not support it yet"):
        engine.create(bus_breaker, "alias", {"id": "X"})
    with pytest.raises(CreationError, match="No element type 'swtich' exists"):
        engine.create(bus_breaker, "swtich", {"id": "X"})


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


def test_batch_is_ordered_by_dependency(engine, bus_breaker):
    """Items may be listed in any order: the engine sorts them."""
    batch = engine.create_many(
        bus_breaker,
        [
            {
                "element_type": "operational_limits",
                "element_id": "LINE_DC",
                "permanent_limit": 800.0,
            },
            {
                "element_type": "load",
                "id": "DATACENTER",
                "bus": "VL_DC_1_1",
                "p0": 300.0,
            },
            {
                "element_type": "line",
                "id": "LINE_DC",
                "bus1": "B4",
                "bus2": "VL_DC_1_1",
                "r": 0.5,
                "x": 5.0,
            },
            {
                "element_type": "voltage_level",
                "id": "VL_DC",
                "substation_id": "SUB_DC",
                "nominal_v": 135.0,
            },
            {"element_type": "substation", "id": "SUB_DC", "country": "FR"},
        ],
    )

    assert batch.success
    assert [item["element_type"] for item in batch.items] == [
        "substation",
        "voltage_level",
        "load",
        "line",
        "operational_limits",
    ]
    assert batch.created_ids == ["SUB_DC", "VL_DC", "DATACENTER", "LINE_DC"]
    assert batch.updated_ids == ["LINE_DC"]
    assert "DATACENTER" in bus_breaker.get_loads().index
    assert len(bus_breaker.get_operational_limits().loc[["LINE_DC"]]) == 2


def test_batch_checks_every_item_before_creating_anything(engine, bus_breaker):
    with pytest.raises(CreationError, match="item 2"):
        engine.create_many(
            bus_breaker,
            [
                {"element_type": "substation", "id": "S_NEW"},
                {"element_type": "load", "id": "L_NEW", "bus": "B4", "pO": 1.0},
            ],
        )

    assert "S_NEW" not in bus_breaker.get_substations().index


@pytest.mark.parametrize(
    "load",
    [
        {"id": "L_NEW", "bus": "B4"},  # p0 missing
        {"id": "L_NEW", "bus": "B4", "p0": 1.0, "type": "NOPE"},  # bad enum
        {"id": "L_NEW", "bus": "B4", "p0": "a lot"},  # bad type
    ],
    ids=["missing-required", "bad-enum", "bad-type"],
)
def test_batch_checks_values_before_creating_anything(engine, bus_breaker, load):
    with pytest.raises(CreationError, match="item 2"):
        engine.create_many(
            bus_breaker,
            [
                {"element_type": "substation", "id": "S_NEW"},
                {"element_type": "load", **load},
            ],
        )

    assert "S_NEW" not in bus_breaker.get_substations().index


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (
            {
                "element_type": "generator",
                "id": "G_NEW",
                "bus": "B4",
                "target_p": 50.0,
                "max_p": 10.0,
            },
            "'target_p' \\(50.0\\) must not be greater than 'max_p'",
        ),
        (
            {
                "element_type": "line",
                "id": "L_NEW",
                "bus1": "B4",
                "bus2": "B5",
                "r": 1.0,
                "x": 0.0,
            },
            "'x' must not be 0",
        ),
    ],
    ids=["ordering", "non-zero"],
)
def test_batch_checks_the_rules_before_creating_anything(
    engine, bus_breaker, item, message
):
    # rules that need no network used to stop the batch half way
    with pytest.raises(BatchRejected, match=f"item 2: {message}"):
        engine.create_many(
            bus_breaker, [{"element_type": "substation", "id": "S_NEW"}, item]
        )

    assert "S_NEW" not in bus_breaker.get_substations().index


def test_batch_checks_table_rows_before_creating_anything(engine, site):
    with pytest.raises(CreationError, match="item 2: Row 1 of 'steps'"):
        engine.create_many(
            site,
            [
                {"element_type": "substation", "id": "S_NEW"},
                {
                    "element_type": "ratio_tap_changer",
                    "element_id": "TR",
                    "steps": [{"rh0": 0.9}, {"rho": 1.1}],
                },
            ],
        )

    assert "S_NEW" not in site.get_substations().index


@pytest.mark.parametrize(
    "name", [True, {"text": "x"}, ["x"]], ids=["boolean", "object", "list"]
)
def test_batch_checks_string_types_before_creating_anything(engine, bus_breaker, name):
    # pypowsybl refused them only once the earlier items were created
    with pytest.raises(
        BatchRejected, match="item 2: 'name' of a substation must be a string"
    ):
        engine.create_many(
            bus_breaker,
            [
                {"element_type": "substation", "id": "S_NEW"},
                {"element_type": "substation", "id": "S_BAD", "name": name},
            ],
        )

    assert "S_NEW" not in bus_breaker.get_substations().index


def test_a_number_given_for_a_string_is_read_as_its_text(engine, bus_breaker):
    engine.create(bus_breaker, "substation", {"id": "S_NEW", "name": 12})

    assert bus_breaker.get_substations().loc["S_NEW", "name"] == "12"


@pytest.mark.parametrize(
    "node", [1.7, "1.5", True, float("inf")], ids=["fraction", "text", "boolean", "inf"]
)
def test_an_integer_is_neither_truncated_nor_read_from_a_boolean(
    engine, bus_breaker, node
):
    with pytest.raises(
        CreationError, match="'node' of a busbar_section must be an integer"
    ):
        engine.create(
            bus_breaker,
            "busbar_section",
            {"id": "BBS", "voltage_level_id": "VL4", "node": node},
        )


@pytest.mark.parametrize(
    "count", [2, 2.0, "2", "2.0"], ids=["int", "float", "text", "decimal-text"]
)
def test_a_whole_number_is_an_integer_whatever_its_type(engine, node_breaker, count):
    engine.create(node_breaker, "substation", {"id": "S_NEW"})

    engine.create(
        node_breaker,
        "voltage_level",
        {
            "id": "VL_NEW",
            "substation_id": "S_NEW",
            "nominal_v": 400.0,
            "topology_kind": "NODE_BREAKER",
            "aligned_buses_or_busbar_count": count,
        },
    )

    sections = node_breaker.get_busbar_sections()
    assert len(sections[sections["voltage_level_id"] == "VL_NEW"]) == 2


def test_a_number_is_not_read_from_a_boolean(engine, bus_breaker):
    with pytest.raises(CreationError, match="'p0' of a load must be a number"):
        engine.create(bus_breaker, "load", {"id": "LD_NEW", "bus": "B4", "p0": True})


def test_switch_kinds_may_be_a_list_of_strings(engine, node_breaker):
    engine.create(node_breaker, "substation", {"id": "S_NEW"})
    attributes = {
        "id": "VL_NEW",
        "substation_id": "S_NEW",
        "nominal_v": 400.0,
        "topology_kind": "NODE_BREAKER",
        "section_count": 2,
    }

    with pytest.raises(CreationError, match="'switch_kinds' .* must be a string"):
        engine.create(
            node_breaker, "voltage_level", {**attributes, "switch_kinds": [1]}
        )
    engine.create(
        node_breaker, "voltage_level", {**attributes, "switch_kinds": ["BREAKER"]}
    )

    assert "VL_NEW" in node_breaker.get_voltage_levels().index


def test_batch_rejects_ids_repeated_within_it(engine, bus_breaker):
    """Caught before anything is created, across types, every repeat at once."""
    with pytest.raises(BatchRejected) as rejection:
        engine.create_many(
            bus_breaker,
            [
                {"element_type": "substation", "id": "S_NEW"},
                {"element_type": "load", "id": "X", "bus": "B4", "p0": 1.0},
                {
                    "element_type": "generator",
                    "id": "X",
                    "bus": "B4",
                    "target_p": 1.0,
                    "max_p": 2.0,
                },
                {"element_type": "load", "id": " X ", "bus": "B5", "p0": 1.0},
            ],
        )

    errors = rejection.value.errors
    assert [error["item"] for error in errors] == [3, 4]
    assert (
        "id 'X' is already used by item 2 (load) of this batch" in errors[0]["problem"]
    )
    assert errors[1]["id"] == "X"
    assert "S_NEW" not in bus_breaker.get_substations().index


def test_batch_accepts_ids_that_name_an_element_twice(engine, node_breaker):
    """An attachment shares its element's id; an area may get several levels."""
    batch = engine.create_many(
        node_breaker,
        [
            {
                "element_type": "operational_limits",
                "element_id": "LINE_NEW",
                "permanent_limit": 800.0,
            },
            {
                "element_type": "line",
                "id": "LINE_NEW",
                "bus1": "S1VL2_BBS1",
                "bus2": "S3VL1_BBS",
                "r": 0.5,
                "x": 5.0,
            },
            {"element_type": "area", "id": "A1", "area_type": "ControlArea"},
            {
                "element_type": "area_voltage_levels",
                "id": "A1",
                "voltage_level_id": "S1VL2",
            },
            {
                "element_type": "area_voltage_levels",
                "id": "A1",
                "voltage_level_id": "S3VL1",
            },
        ],
    )

    assert batch.success
    assert batch.created_ids == ["A1", "LINE_NEW"]
    assert set(batch.updated_ids) == {"A1", "LINE_NEW"}
    # each row adds to the area: pypowsybl's call alone would keep only the last
    levels = node_breaker.get_areas_voltage_levels()
    assert sorted(levels.loc[["A1"], "voltage_level_id"]) == ["S1VL2", "S3VL1"]


def test_batch_stops_where_the_network_refuses_and_says_so(engine, bus_breaker):
    batch = engine.create_many(
        bus_breaker,
        [
            {"element_type": "substation", "id": "S_NEW"},
            {"element_type": "load", "id": "L_NEW", "bus": "NOPE", "p0": 1.0},
        ],
    )

    assert batch.created_ids == ["S_NEW"]
    assert batch.stopped_at == 2
    assert batch.items[-1]["status"] == "failed"
    assert batch.items[-1]["element_type"] == "load"
    # what came before is in the network, and the report says so
    assert "S_NEW" in bus_breaker.get_substations().index


def test_batch_rejects_an_item_without_an_element_type(engine, bus_breaker):
    with pytest.raises(CreationError, match="no 'element_type'"):
        engine.create_many(bus_breaker, [{"id": "X"}])


def test_batch_rejects_an_item_that_is_not_an_object(engine, bus_breaker):
    with pytest.raises(CreationError, match="not an object"):
        engine.create_many(bus_breaker, ["substation"])


def test_batch_rejects_an_item_without_an_id(engine, bus_breaker):
    with pytest.raises(CreationError, match="'id' is required"):
        engine.create_many(bus_breaker, [{"element_type": "substation"}])


def test_batch_rejects_a_blank_id_before_creating_anything(engine, bus_breaker):
    # a blank id passed the pre-check and stopped the batch at its item
    with pytest.raises(BatchRejected, match="item 2: 'id' is required"):
        engine.create_many(
            bus_breaker,
            [
                {"element_type": "substation", "id": "S_NEW"},
                {"element_type": "substation", "id": "  "},
            ],
        )

    assert "S_NEW" not in bus_breaker.get_substations().index


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------


def test_describe_lists_every_supported_type(engine):
    listing = engine.describe()

    names = [item["element_type"] for item in listing["element_types"]]
    assert "load" in names
    assert "hvdc_line" in names
    assert all(item["summary"] for item in listing["element_types"])


def test_describe_a_type_documents_its_attributes(engine):
    described = engine.describe("generator")

    required = {item["name"] for item in described["required"]}
    assert required == {"target_p", "max_p"}
    units = {item["name"]: item.get("unit") for item in described["required"]}
    assert units["target_p"] == "MW"

    optional = {item["name"]: item for item in described["optional"]}
    assert optional["energy_source"]["values"] == [
        "HYDRO",
        "NUCLEAR",
        "WIND",
        "THERMAL",
        "SOLAR",
        "OTHER",
    ]
    # descriptions are mined from the pypowsybl docstrings
    assert "active power" in optional["min_p"]["description"]
    assert "position_order" in described["set_automatically"]
    assert described["connection"]["attributes"] == ["bus_or_busbar_section_id"]
    assert any("target_v" in rule for rule in described["rules"])
    assert described["created_by"] == "pp.network.create_generator_bay"


def test_describe_documents_table_attributes(engine):
    described = engine.describe("ratio_tap_changer")

    assert described["table_attribute"]["name"] == "steps"
    columns = {item["name"] for item in described["table_attribute"]["columns"]}
    assert "rho" in columns
    assert described["attaches_to"]["element_types"] == ["two_windings_transformer"]


def test_describe_an_unknown_type(engine):
    with pytest.raises(CreationError, match="No element type"):
        engine.describe("wormhole")


@pytest.mark.parametrize(
    "element_type", ["dc_node", "dc_line", "dc_ground", "voltage_source_converter"]
)
def test_the_detailed_dc_model_is_not_offered(engine, element_type):
    # a dc_node and a dc_line were enough to make every AC load flow fail
    # ("acDcNetwork is set to false"), with no parameter to fix it here
    assert element_type not in engine.supported
    with pytest.raises(CreationError, match="not offered: .*acDcNetwork"):
        engine.profile_for(element_type)


def test_the_dc_model_types_are_read_from_the_schema():
    from pypowsybl_mcp.plugins.grid_extension.creation.profiles import (
        LEFT_OUT_TYPES,
        is_detailed_dc,
    )

    assert sorted(LEFT_OUT_TYPES) == [
        "dc_ground",
        "dc_line",
        "dc_node",
        "voltage_source_converter",
    ]
    # an HVDC link of the classic model is not part of it
    assert not is_detailed_dc("hvdc_line")
    assert not is_detailed_dc("vsc_converter_station")


def _ordered_batch(engine, network, items):
    result = engine.create_many(network, items)
    failed = [item for item in result.items if item["status"] == "failed"]
    assert result.success, failed
    return result


def test_schema_only_topology_can_be_listed_in_any_order(engine, bus_breaker):
    # all schema-only types shared one position, so a switch listed before its
    # buses stopped the batch with the first items already applied
    items = [
        {
            "element_type": "switch",
            "id": "SW_X",
            "voltage_level_id": "VL_X",
            "bus1_id": "BX_A",
            "bus2_id": "BX_B",
            "kind": "BREAKER",
        },
        {"element_type": "bus", "id": "BX_B", "voltage_level_id": "VL_X"},
        {"element_type": "bus", "id": "BX_A", "voltage_level_id": "VL_X"},
        {
            "element_type": "voltage_level",
            "id": "VL_X",
            "substation_id": "S_X",
            "topology_kind": "BUS_BREAKER",
            "nominal_v": 225.0,
        },
        {"element_type": "substation", "id": "S_X", "country": "FR"},
    ]

    _ordered_batch(engine, bus_breaker, items)

    assert "SW_X" in bus_breaker.get_switches().index


def test_area_bookkeeping_comes_after_the_equipment_it_names(engine, bus_breaker):
    # area_boundaries ran before the line it names: "Connectable does not exist"
    items = [
        {
            "element_type": "area_boundaries",
            "id": "A9",
            "boundary_type": "TERMINAL",
            "element": "L_NEW",
            "side": "ONE",
            "ac": True,
        },
        {"element_type": "area_voltage_levels", "id": "A9", "voltage_level_id": "VL4"},
        {
            "element_type": "line",
            "id": "L_NEW",
            "bus1": "B4",
            "bus2": "B5",
            "r": 1.0,
            "x": 10.0,
        },
        {"element_type": "area", "id": "A9", "area_type": "CONTROL_AREA"},
    ]

    _ordered_batch(engine, bus_breaker, items)

    boundaries = bus_breaker.get_areas_boundaries().reset_index()
    assert list(boundaries[boundaries["id"] == "A9"]["element"]) == ["L_NEW"]


def test_a_switch_naming_a_bus_of_another_voltage_level_is_refused_up_front(
    engine, bus_breaker
):
    # pypowsybl registered the id, then refused: the switch could not be reused,
    # removed, nor listed by get_switches(all_attributes=True)
    with pytest.raises(CreationError, match="'B5', which is not a bus of .*'VL4'"):
        engine.create(
            bus_breaker,
            "switch",
            {
                "id": "SW_X",
                "voltage_level_id": "VL4",
                "bus1_id": "B4",
                "bus2_id": "B5",
                "kind": "BREAKER",
            },
        )

    assert "SW_X" not in bus_breaker.get_identifiables().index
    bus_breaker.get_switches(all_attributes=True)  # still readable

    # the id is still free
    engine.create(bus_breaker, "bus", {"id": "B4_BIS", "voltage_level_id": "VL4"})
    engine.create(
        bus_breaker,
        "switch",
        {
            "id": "SW_X",
            "voltage_level_id": "VL4",
            "bus1_id": "B4",
            "bus2_id": "B4_BIS",
            "kind": "BREAKER",
        },
    )
    assert "SW_X" in bus_breaker.get_switches(all_attributes=True).index


def test_an_element_pypowsybl_keeps_after_refusing_it_is_reported(bus_breaker):
    # without the bus check, pypowsybl's own behavior: the id stays registered
    from dataclasses import replace

    from pypowsybl_mcp.plugins.grid_extension.creation.profiles import (
        PROFILES,
        UNPROFILED,
    )

    unchecked = CreationEngine(
        profiles=PROFILES,
        unprofiled={**UNPROFILED, "switch": replace(UNPROFILED["switch"], rules=())},
    )
    with pytest.raises(pp.PyPowsyblError, match="kept 'SW_X' in the network"):
        unchecked.create(
            bus_breaker,
            "switch",
            {
                "id": "SW_X",
                "voltage_level_id": "VL4",
                "bus1_id": "B4",
                "bus2_id": "B5",
                "kind": "BREAKER",
            },
        )


def test_a_refusal_that_keeps_nothing_is_left_as_is(engine, bus_breaker):
    with pytest.raises(pp.PyPowsyblError) as raised:
        engine.create(
            bus_breaker,
            "switch",
            {
                "id": "SW_X",
                "voltage_level_id": "VL4",
                "bus1_id": "B4",
                "bus2_id": "B4",
                "kind": "BREAKER",
            },
        )
    assert "kept" not in str(raised.value)


def _node_breaker_level(engine, network, level_id="VLX"):
    """A node/breaker voltage level created by the tool: its sections have a
    position, as create_voltage_level_topology gives them one."""
    engine.create(
        network,
        "voltage_level",
        {
            "id": level_id,
            "substation_id": "S1",
            "topology_kind": "NODE_BREAKER",
            "nominal_v": 400.0,
        },
    )
    return f"{level_id}_1_1"


def test_a_section_is_refused_where_the_others_have_a_position(engine, node_breaker):
    # without a position, every later feeder bay in the voltage level failed
    # with a bare java.lang.NullPointerException
    section = _node_breaker_level(engine, node_breaker)
    with pytest.raises(
        CreationError,
        match=r"'VLX' lays out its busbar sections by position.*"
        r"aligned_buses_or_busbar_count, section_count.*sections: VLX_1_1\.",
    ):
        engine.create(
            node_breaker,
            "busbar_section",
            {"id": "VLX_NEW", "voltage_level_id": "VLX", "node": 99},
        )
    assert "VLX_NEW" not in node_breaker.get_identifiables().index
    engine.create(node_breaker, "load", {"id": "L_X", "bus": section, "p0": 5.0})
    assert "L_X" in node_breaker.get_loads().index


def test_a_section_is_accepted_where_no_section_has_a_position(engine, node_breaker):
    # the sections of this grid have no position, and feeder bays work as is
    assert node_breaker.get_extensions("busbarSectionPosition").empty
    engine.create(
        node_breaker,
        "busbar_section",
        {"id": "S1VL2_NEW", "voltage_level_id": "S1VL2", "node": 90},
    )
    engine.create(node_breaker, "load", {"id": "L_X", "bus": "S1VL2_BBS1", "p0": 5.0})
    assert "L_X" in node_breaker.get_loads().index


def test_area_entries_add_to_what_the_area_has(engine):
    network = pp.network.create_eurostag_tutorial_example1_with_tie_lines_and_areas()
    engine.create(
        network, "area_voltage_levels", {"id": "Region_AB", "voltage_level_id": "VLGEN"}
    )  # already there: nothing changes
    engine.create(network, "area", {"id": "A_NEW", "area_type": "Market"})
    for level in ("VLGEN", "VLHV1"):
        engine.create(
            network, "area_voltage_levels", {"id": "A_NEW", "voltage_level_id": level}
        )
    engine.create(
        network,
        "area_boundaries",
        {
            "id": "ControlArea_A",
            "boundary_type": "TERMINAL",
            "element": "NGEN_NHV1",
            "side": "TWO",
            "ac": True,
        },
    )

    levels = network.get_areas_voltage_levels()
    assert sorted(levels.loc[["Region_AB"], "voltage_level_id"]) == [
        "VLGEN",
        "VLHV1",
        "VLHV2",
        "VLLOAD",
    ]
    assert sorted(levels.loc[["A_NEW"], "voltage_level_id"]) == ["VLGEN", "VLHV1"]
    boundaries = network.get_areas_boundaries(all_attributes=True).loc[
        ["ControlArea_A"]
    ]
    assert sorted(boundaries[["boundary_type", "element"]].values.tolist()) == [
        ["BOUNDARY_LINE", "NHV1_XNODE1"],
        ["BOUNDARY_LINE", "NHV1_XNODE2"],
        ["TERMINAL", "NGEN_NHV1"],
    ]


def test_a_voltage_level_in_an_area_of_the_same_type_is_refused_up_front(engine):
    # pypowsybl refused it, but only after listing the level in the second area
    network = pp.network.create_eurostag_tutorial_example1_with_tie_lines_and_areas()
    engine.create(network, "area", {"id": "A_NEW", "area_type": "ControlArea"})
    with pytest.raises(
        CreationError, match="'VLGEN' is already in area 'ControlArea_A', of the same"
    ):
        engine.create(
            network, "area_voltage_levels", {"id": "A_NEW", "voltage_level_id": "VLGEN"}
        )
    assert "A_NEW" not in network.get_areas_voltage_levels().index
