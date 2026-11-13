#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The topology operations, on pypowsybl's sample networks.

The four-substations network is node/breaker, with every voltage level but S1VL1
at 400 kV: S4VL1_BBS can be inserted on LINE_S2S3 or tapped from it. IEEE 14 is
bus/breaker, with buses at 135 kV and, behind the transformers, at 12 kV.
"""

import dataclasses

import pandas as pd
import pypowsybl as pp
import pytest

from pypowsybl_mcp.plugins.grid_extension.creation import schema
from pypowsybl_mcp.plugins.grid_extension.creation.context import CreationError
from pypowsybl_mcp.plugins.grid_extension.creation.topology import (
    OPERATIONS,
    TopologyCallError,
    TopologyEngine,
    dangling_switches,
    operation_problems,
)

CUT = {"bbs_or_bus_id": "S4VL1_BBS", "line_id": "LINE_S2S3"}
MERGE = {"line1_id": "LINE_S2S3_1", "line2_id": "LINE_S2S3_2", "line_id": "LINE_S2S3"}
TAP = {
    "bbs_or_bus_id": "S4VL1_BBS",
    "line_id": "LINE_S2S3",
    "new_line_id": "TAP",
    "new_line_r": 1.0,
    "new_line_x": 10.0,
}
UNTAP = {
    "line_to_be_merged1_id": "LINE_S2S3_1",
    "line_to_be_merged2_id": "LINE_S2S3_2",
    "line_to_be_deleted": "TAP",
    "merged_line_id": "LINE_S2S3",
}


@pytest.fixture
def engine():
    return TopologyEngine()


@pytest.fixture
def network():
    return pp.network.create_four_substations_node_breaker_network()


def _lines(network) -> dict[str, tuple[str, str]]:
    lines = network.get_lines()
    return {
        line_id: (row.voltage_level1_id, row.voltage_level2_id)
        for line_id, row in lines.iterrows()
    }


def _converges(network) -> bool:
    return (
        pp.loadflow.run_ac(network)[0].status == pp.loadflow.ComponentStatus.CONVERGED
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_every_operation_matches_the_installed_pypowsybl():
    problems = {
        name: found
        for name, operation in OPERATIONS.items()
        if (found := operation_problems(operation))
    }

    assert not problems


def test_every_pypowsybl_topology_modification_is_offered():
    """A new one upstream fails here: offer it, or say why not."""
    assert set(schema.TOPOLOGY_CALLS) == set(OPERATIONS)


def test_an_operation_naming_what_pypowsybl_lacks_has_problems():
    operation = dataclasses.replace(
        OPERATIONS["connect_voltage_level_on_line"], required=("line_id", "gone")
    )

    assert operation_problems(operation) == ["required unknown to pypowsybl: gone"]
    assert (
        "has no call"
        in operation_problems(
            dataclasses.replace(operation, operation="no_such_operation")
        )[0]
    )


def test_describe_carries_what_a_caller_needs(engine):
    descriptor = engine.describe("create_line_on_line")

    assert descriptor["methods"] == ["pp.network.create_line_on_line"]
    assert "new_line_x" in descriptor["required"]
    position = next(
        item for item in descriptor["attributes"] if item["name"] == "position_percent"
    )
    assert position == {
        "name": "position_percent",
        "type": "number",
        "unit": "%",
        "default": 50.0,
    }
    assert any("nominal voltage" in check for check in descriptor["checks"])


# ---------------------------------------------------------------------------
# Applying them
# ---------------------------------------------------------------------------


def test_a_substation_inserted_on_a_line_and_removed_again(engine, network):
    switches = len(network.get_switches())

    cut = engine.apply(network, "connect_voltage_level_on_line", CUT)

    assert cut.created_ids == ("LINE_S2S3_1", "LINE_S2S3_2")
    assert cut.removed_ids == ("LINE_S2S3",)
    assert cut.switches_created == 4
    assert _lines(network)["LINE_S2S3_1"] == ("S2VL1", "S4VL1")
    assert _lines(network)["LINE_S2S3_2"] == ("S4VL1", "S3VL1")
    assert _converges(network)

    merged = engine.apply(network, "revert_connect_voltage_level_on_line", MERGE)

    assert merged.created_ids == ("LINE_S2S3",)
    assert _lines(network)["LINE_S2S3"] == ("S2VL1", "S3VL1")
    # since pypowsybl 1.16 the revert removes the bays of the two halves at
    # S4VL1 itself: the orphan cleanup finds nothing left
    assert merged.orphan_switches_removed == ()
    assert len(network.get_switches()) == switches
    assert not dangling_switches(network, "S4VL1")
    assert "left with nothing behind them" not in merged.message
    assert _converges(network)


def test_the_impedance_is_split_at_the_position(engine, network):
    x = network.get_lines().loc["LINE_S2S3", "x"]

    engine.apply(
        network, "connect_voltage_level_on_line", {**CUT, "position_percent": 40}
    )

    lines = network.get_lines()
    assert lines.loc["LINE_S2S3_1", "x"] == pytest.approx(0.4 * x)
    assert lines.loc["LINE_S2S3_2", "x"] == pytest.approx(0.6 * x)


def test_a_line_cut_in_two_keeps_its_limits(engine, network):
    network.create_loading_limits(
        pd.DataFrame.from_records(
            index="element_id",
            data=[
                {
                    "element_id": "LINE_S2S3",
                    "name": "permanent_limit",
                    "side": side,
                    "type": "CURRENT",
                    "value": 1000.0,
                    "acceptable_duration": -1,
                }
                for side in ("ONE", "TWO")
            ],
        )
    )

    cut = engine.apply(network, "connect_voltage_level_on_line", CUT)

    assert not cut.notes
    limited = set(network.get_loading_limits().index.get_level_values(0))
    assert {"LINE_S2S3_1", "LINE_S2S3_2"} <= limited


def test_a_line_without_limits_is_noted(engine, network):
    cut = engine.apply(network, "connect_voltage_level_on_line", CUT)

    assert len(cut.notes) == 2
    assert "no operational limits" in cut.notes[0]


def test_a_tap_and_its_revert(engine, network):
    switches = len(network.get_switches())

    tap = engine.apply(network, "create_line_on_line", TAP)

    assert set(tap.created_ids) == {"LINE_S2S3_1", "LINE_S2S3_2", "LINE_S2S3_VL", "TAP"}
    assert _lines(network)["TAP"] == ("LINE_S2S3_VL", "S4VL1")
    assert _converges(network)

    untap = engine.apply(network, "revert_create_line_on_line", UNTAP)

    assert set(untap.removed_ids) == set(tap.created_ids)
    # since pypowsybl 1.16 the revert removes the tapped line's bay itself
    assert untap.orphan_switches_removed == ()
    assert len(network.get_switches()) == switches


def test_a_tee_point_replaced_by_the_busbar(engine, network):
    engine.apply(network, "create_line_on_line", TAP)

    replaced = engine.apply(
        network,
        "replace_tee_point_by_voltage_level_on_line",
        {
            "tee_point_line1": "LINE_S2S3_1",
            "tee_point_line2": "LINE_S2S3_2",
            "tee_point_line_to_remove": "TAP",
            "bbs_or_bus_id": "S4VL1_BBS",
        },
    )

    assert set(replaced.removed_ids) == {"LINE_S2S3_VL", "TAP"}
    assert _lines(network)["LINE_S2S3_1"] == ("S2VL1", "S4VL1")
    assert _lines(network)["LINE_S2S3_2"] == ("S4VL1", "S3VL1")
    assert _converges(network)


def test_a_coupling_device(engine, network):
    coupled = engine.apply(
        network,
        "create_coupling_device",
        {
            "bus_or_busbar_section_id_1": "S1VL2_BBS1",
            "bus_or_busbar_section_id_2": "S1VL2_BBS2",
        },
    )

    assert coupled.switches_created == 3
    assert coupled.message == "create_coupling_device: built 3 switch(es)"


def test_a_cut_on_a_bus_breaker_network_and_its_revert(engine):
    network = pp.network.create_ieee14()
    network.create_substations(id="S_NEW")
    network.create_voltage_levels(
        id="VL_NEW", substation_id="S_NEW", topology_kind="BUS_BREAKER", nominal_v=135
    )
    network.create_buses(id="B_NEW", voltage_level_id="VL_NEW")

    cut = engine.apply(
        network,
        "connect_voltage_level_on_line",
        {"bbs_or_bus_id": "B_NEW", "line_id": "L2-3-1"},
    )

    # a bus and a breaker per half, between it and B_NEW
    assert cut.created_ids == ("L2-3-1_1", "L2-3-1_1_BUS", "L2-3-1_2", "L2-3-1_2_BUS")
    assert cut.switches_created == 2
    assert _converges(network)

    merged = engine.apply(
        network,
        "revert_connect_voltage_level_on_line",
        {"line1_id": "L2-3-1_1", "line2_id": "L2-3-1_2", "line_id": "L2-3-1"},
    )

    # pypowsybl removes the voltage level left empty, and its substation
    assert {"VL_NEW", "S_NEW", "B_NEW"} <= set(merged.removed_ids)
    (note,) = [note for note in merged.notes if "left empty" in note]
    assert "voltage level 'VL_NEW'" in note
    assert "substation 'S_NEW'" in note
    assert _converges(network)


def test_a_fictitious_tee_point_removed_is_not_noted(engine, network):
    engine.apply(network, "create_line_on_line", TAP)

    untap = engine.apply(network, "revert_create_line_on_line", UNTAP)

    assert "LINE_S2S3_VL" in untap.removed_ids
    assert not any("left empty" in note for note in untap.notes)


# ---------------------------------------------------------------------------
# What is refused before pypowsybl is called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "attributes", "expected"),
    [
        (
            "connect_voltage_level_on_line",
            {**CUT, "position_percent": 150},
            "strictly between 0.0 and 100.0",
        ),
        (
            "connect_voltage_level_on_line",
            {**CUT, "line_id": "NO_SUCH_LINE"},
            "No line 'NO_SUCH_LINE'",
        ),
        (
            "connect_voltage_level_on_line",
            {**CUT, "line_id": "TWT"},
            "names two_windings_transformer 'TWT', not a line",
        ),
        (
            "connect_voltage_level_on_line",
            {**CUT, "line1_id": "LINE_S3S4"},
            "already used by a line",
        ),
        (
            "connect_voltage_level_on_line",
            {**CUT, "bbs_or_bus_id": "S1VL1_BBS"},
            "a line cannot change voltage",
        ),
        (
            "connect_voltage_level_on_line",
            {**CUT, "postion_percent": 40},
            "Did you mean 'position_percent'",
        ),
        (
            "create_line_on_line",
            {**TAP, "new_line_x": 0},
            "new_line_x",
        ),
        (
            "revert_connect_voltage_level_on_line",
            {"line1_id": "LINE_S2S3", "line2_id": "LINE_S3S4"},
            "needs 'line_id'",
        ),
        (
            "create_coupling_device",
            {
                "bus_or_busbar_section_id_1": "S1VL2_BBS1",
                "bus_or_busbar_section_id_2": "S4VL1_BBS",
            },
            "two busbars of one voltage level",
        ),
        (
            "create_coupling_device",
            {
                "bus_or_busbar_section_id_1": "S1VL2_BBS1",
                "bus_or_busbar_section_id_2": "S1VL2_BBS1",
            },
            "two different busbars",
        ),
        ("connect_voltge_level_on_line", CUT, "Did you mean"),
    ],
    ids=[
        "position",
        "unknown-line",
        "not-a-line",
        "id-taken",
        "voltage",
        "typo",
        "zero-impedance",
        "revert-without-merged-id",
        "coupling-across-levels",
        "coupling-with-itself",
        "operation-typo",
    ],
)
def test_refused_before_anything_changes(engine, network, name, attributes, expected):
    before = network.get_identifiables().index.tolist()

    with pytest.raises(CreationError, match=expected) as raised:
        engine.apply(network, name, attributes)

    assert network.get_identifiables().index.tolist() == before
    assert raised.value.hint


def test_a_bus_of_the_bus_view_is_refused(engine, network):
    with pytest.raises(CreationError, match="busbar section there"):
        engine.apply(
            network,
            "connect_voltage_level_on_line",
            {**CUT, "bbs_or_bus_id": "S4VL1_0"},
        )


def test_a_12_kv_bus_cannot_be_inserted_on_a_135_kv_line(engine):
    network = pp.network.create_ieee14()

    with pytest.raises(
        CreationError, match="at 12 kV, while line 'L2-3-1' is at 135 kV"
    ):
        engine.apply(
            network,
            "connect_voltage_level_on_line",
            {"bbs_or_bus_id": "B14", "line_id": "L2-3-1"},
        )


# ---------------------------------------------------------------------------
# Bus/breaker leftovers and the ids pypowsybl derives
# ---------------------------------------------------------------------------

BB_CUT = {"bbs_or_bus_id": "B_NEW", "line_id": "L2-3-1"}
BB_MERGE = {"line1_id": "L2-3-1_1", "line2_id": "L2-3-1_2", "line_id": "L2-3-1"}
BB_TAP = {**BB_CUT, "new_line_id": "TAP", "new_line_r": 1.0, "new_line_x": 10.0}
BB_UNTAP = {
    "line_to_be_merged1_id": "L2-3-1_1",
    "line_to_be_merged2_id": "L2-3-1_2",
    "line_to_be_deleted": "TAP",
    "merged_line_id": "L2-3-1",
}


@pytest.fixture
def bus_breaker_site():
    """IEEE 14 with a 135 kV site whose bus B_NEW holds a load, so a revert
    does not leave the voltage level empty (pypowsybl would remove it whole)."""
    network = pp.network.create_ieee14()
    network.create_substations(id="S_NEW")
    network.create_voltage_levels(
        id="VL_NEW", substation_id="S_NEW", topology_kind="BUS_BREAKER", nominal_v=135
    )
    network.create_buses(id="B_NEW", voltage_level_id="VL_NEW")
    network.create_loads(
        id="LOAD_NEW", voltage_level_id="VL_NEW", bus_id="B_NEW", p0=10, q0=2
    )
    return network


def _switches_of(network, voltage_level_id) -> set[str]:
    switches = network.get_switches()
    return set(switches.index[switches["voltage_level_id"] == voltage_level_id])


def test_a_bus_breaker_cut_reverted_leaves_no_switch_behind(engine, bus_breaker_site):
    network = bus_breaker_site
    switches = _switches_of(network, "VL_NEW")
    engine.apply(network, "connect_voltage_level_on_line", BB_CUT)

    merged = engine.apply(network, "revert_connect_voltage_level_on_line", BB_MERGE)

    assert len(merged.orphan_switches_removed) == 2
    assert _switches_of(network, "VL_NEW") == switches
    # pypowsybl cannot remove a bus: the two it created stay, and the reply says so
    (note,) = [note for note in merged.notes if "cannot remove a bus" in note]
    assert "'L2-3-1_1_BUS' (in VL_NEW)" in note
    assert "'L2-3-1_2_BUS' (in VL_NEW)" in note
    assert _converges(network)


def test_a_bus_breaker_tap_reverted_leaves_no_switch_behind(engine, bus_breaker_site):
    network = bus_breaker_site
    engine.apply(network, "create_line_on_line", BB_TAP)

    untap = engine.apply(network, "revert_create_line_on_line", BB_UNTAP)

    assert untap.orphan_switches_removed == ("L2-3-1_SW",)
    assert not _switches_of(network, "VL_NEW")
    assert any("'L2-3-1_BUS' (in VL_NEW)" in note for note in untap.notes)
    assert _converges(network)


def test_a_coupler_to_the_busbar_of_a_reverted_cut_stays(engine, bus_breaker_site):
    network = bus_breaker_site
    network.create_buses(id="B_SPARE", voltage_level_id="VL_NEW")
    network.create_switches(
        id="COUPLER",
        voltage_level_id="VL_NEW",
        bus1_id="B_NEW",
        bus2_id="B_SPARE",
        kind="BREAKER",
    )
    engine.apply(network, "connect_voltage_level_on_line", BB_CUT)

    merged = engine.apply(network, "revert_connect_voltage_level_on_line", BB_MERGE)

    assert "COUPLER" not in merged.orphan_switches_removed
    assert _switches_of(network, "VL_NEW") == {"COUPLER"}


def test_tapping_a_bus_breaker_line_again_is_refused_before_any_change(
    engine, bus_breaker_site
):
    network = bus_breaker_site
    engine.apply(network, "create_line_on_line", BB_TAP)
    engine.apply(network, "revert_create_line_on_line", BB_UNTAP)
    before = network.get_identifiables().index.tolist()

    # pypowsybl would cut the line, then fail on the bus left by the first tap
    with pytest.raises(CreationError, match="'L2-3-1_BUS'.*cannot remove a bus"):
        engine.apply(network, "create_line_on_line", BB_TAP)

    assert network.get_identifiables().index.tolist() == before


@pytest.mark.parametrize(
    ("name", "attributes", "taken"),
    [
        ("connect_voltage_level_on_line", BB_CUT, "L2-3-1_2"),
        ("connect_voltage_level_on_line", BB_CUT, "L2-3-1_1_BUS"),
        (
            "connect_voltage_level_on_line",
            {**BB_CUT, "line1_id": "A", "line2_id": "B"},
            "B_BUS",
        ),
        ("create_line_on_line", BB_TAP, "L2-3-1_1"),
        ("create_line_on_line", BB_TAP, "L2-3-1_VL"),
        ("create_line_on_line", BB_TAP, "L2-3-1_SW"),
    ],
    ids=[
        "section",
        "bus",
        "bus-of-a-named-section",
        "tap-section",
        "tee-point",
        "tap-switch",
    ],
)
def test_an_id_pypowsybl_would_derive_is_checked_first(
    engine, bus_breaker_site, name, attributes, taken
):
    network = bus_breaker_site
    network.create_substations(id=taken)
    before = network.get_identifiables().index.tolist()

    with pytest.raises(CreationError, match=f"pypowsybl would create '{taken}'"):
        engine.apply(network, name, attributes)

    assert network.get_identifiables().index.tolist() == before


def test_a_bus_breaker_id_does_not_block_a_node_breaker_cut(engine, network):
    # in node/breaker pypowsybl builds switches, with ids of its own, not a bus
    network.create_substations(id="LINE_S2S3_1_BUS")

    cut = engine.apply(network, "connect_voltage_level_on_line", CUT)

    assert "LINE_S2S3_1" in cut.created_ids


def test_a_failure_after_a_partial_change_says_what_changed(
    engine, network, monkeypatch
):
    real = pp.network.create_line_on_line

    def fails_half_way(network, **kwargs):
        real(network, **kwargs)
        raise pp.PyPowsyblError("refused half way")

    monkeypatch.setattr(pp.network, "create_line_on_line", fails_half_way)

    with pytest.raises(TopologyCallError, match="refused half way") as raised:
        engine.apply(network, "create_line_on_line", TAP)

    changes = raised.value.changes
    assert changes.removed_ids == ("LINE_S2S3",)
    assert set(changes.created_ids) == {
        "LINE_S2S3_1",
        "LINE_S2S3_2",
        "LINE_S2S3_VL",
        "TAP",
    }
    assert changes.switches_created == 2
