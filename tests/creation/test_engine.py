#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The creation pipeline, exercised against real pypowsybl networks."""

import pypowsybl as pp
import pytest

from pypowsybl_mcp.creation.context import CreationError
from pypowsybl_mcp.creation.engine import CreationEngine, resolve_connection_point


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

    assert "created substation 'S_DC'" in report
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
    assert "connection points: VL_DC_1_1" in report
    levels = bus_breaker.get_voltage_levels()
    assert levels.loc["VL_DC", "nominal_v"] == 135.0
    assert levels.loc["VL_DC", "low_voltage_limit"] == 128.0
    assert "VL_DC_1_1" in bus_breaker.get_bus_breaker_view_buses().index


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


# ---------------------------------------------------------------------------
# Injections and branches
# ---------------------------------------------------------------------------


def test_create_load_with_defaults(engine, bus_breaker):
    report = engine.create(
        bus_breaker, "load", {"id": "DC", "bus_or_busbar_section_id": "B4", "p0": 300.0}
    )

    assert "created load 'DC' (p0=300.0 MW) connected to 'B4' in VL4" in report
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
    # Enumerated values are accepted in any case and normalised.
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
    assert "created ground 'GND'" in report
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

    assert "between 'B4' in VL4 and 'VL_DC_1_1' in VL_DC" in report
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

    assert "created hvdc_line 'HVDC_NEW'" in report
    assert "HVDC_NEW" in node_breaker.get_hvdc_lines().index


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

    assert "set operational_limits on line 'LINE_DC'" in report
    limits = site.get_operational_limits()
    rows = limits[limits.index.get_level_values("element_id") == "LINE_DC"]
    # Permanent and temporary, on both sides, in the group the analyses read.
    assert len(rows) == 4
    assert set(rows.index.get_level_values("acceptable_duration")) == {-1, 1200}


def test_minmax_reactive_limits(engine, bus_breaker):
    engine.create(
        bus_breaker,
        "minmax_reactive_limits",
        {"id": "B1-G", "min_q": -100.0, "max_q": 100.0},
    )

    generators = bus_breaker.get_generators()
    assert generators.loc["B1-G", "min_q"] == -100.0
    assert generators.loc["B1-G", "max_q"] == 100.0


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

    assert "Note:" in report
    assert "different nominal voltages" in report
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
# Batches
# ---------------------------------------------------------------------------


def test_batch_is_ordered_by_dependency(engine, bus_breaker):
    """Items may be listed in any order: the engine sorts them."""
    done, failed = engine.create_many(
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

    assert not failed
    assert len(done) == 5
    assert "substation" in done[0]
    assert "voltage_level" in done[1]
    assert "operational_limits" in done[-1]
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


def test_batch_stops_where_the_network_refuses_and_says_so(engine, bus_breaker):
    done, failed = engine.create_many(
        bus_breaker,
        [
            {"element_type": "substation", "id": "S_NEW"},
            {"element_type": "load", "id": "L_NEW", "bus": "NOPE", "p0": 1.0},
        ],
    )

    assert len(done) == 1
    assert "item 2 (load)" in failed[0]
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
