#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The introspected layer: what pypowsybl says about its own creation API."""

import pytest

from pypowsybl_mcp.creation.doc_mining import attribute_docs, unit_in
from pypowsybl_mcp.creation.schema import (
    creatable_element_types,
    creation_schema,
    modification_schema,
    plain_modification_schema,
)


def test_creation_schema_reads_the_fields_from_pypowsybl():
    schema = creation_schema("load")

    assert schema.index_name == "id"
    assert {"voltage_level_id", "bus_id", "p0", "q0"} <= set(schema.names)
    assert schema.field("p0").kind == "number"
    assert schema.field("node").kind == "integer"
    assert schema.field("id").is_index


def test_bay_schema_adds_the_connection_attributes():
    plain = set(creation_schema("load").names)
    bay = set(modification_schema("load", "CREATE_FEEDER_BAY").names)

    assert plain < bay
    assert {"bus_or_busbar_section_id", "position_order", "direction"} <= bay - plain


def test_branch_schemas_come_from_their_own_modification():
    line = set(modification_schema("line", "CREATE_LINE_FEEDER").names)

    assert {"bus_or_busbar_section_id_1", "bus_or_busbar_section_id_2"} <= line
    assert {"r", "x", "g1", "b1"} <= line


def test_multi_dataframe_calls_keep_their_groups():
    """A shunt takes three dataframes: the shunt, and two section models."""
    schema = modification_schema("shunt_compensator", "CREATE_FEEDER_BAY")

    assert schema.group_count == 3
    assert "b_per_section" in {item.name for item in schema.group(1)}
    assert "section_count" in {item.name for item in schema.group(0)}


def test_tap_changer_schema_has_a_row_group():
    schema = creation_schema("ratio_tap_changer")

    assert "rho" in {item.name for item in schema.group(1)}
    assert "target_v" in {item.name for item in schema.group(0)}


def test_plain_modification_schema():
    schema = plain_modification_schema("VOLTAGE_LEVEL_TOPOLOGY_CREATION")

    assert "aligned_buses_or_busbar_count" in schema.names
    assert "switch_kinds" in schema.names


def test_index_of_an_attachment_is_the_target():
    assert creation_schema("operational_limits").index_name == "element_id"


def test_unknown_element_type():
    with pytest.raises(KeyError):
        creation_schema("wormhole")


def test_creatable_element_types_follows_the_installed_version():
    types = creatable_element_types()

    assert "load" in types
    assert "two_windings_transformer" in types
    # the upper bound of what a generic tool could reach
    assert len(types) > 25


def test_docstring_mining_extracts_descriptions_and_units():
    import pypowsybl as pp

    docs = attribute_docs((pp.network.create_load_bay,))

    assert "active power" in docs["p0"]
    assert unit_in(docs["p0"]) == "MW"
    assert unit_in(docs["q0"]) == "MVAr"
    assert unit_in("the identifier of the new load") is None
    assert unit_in(None) is None


def test_docstring_mining_merges_several_functions():
    """A bay helper documents only what it adds, and points at the base API."""
    import pypowsybl as pp
    from pypowsybl.network import Network

    merged = attribute_docs((pp.network.create_line_bays, Network.create_lines))

    assert "bus_or_busbar_section_id_1" in merged  # from the bay helper
    assert "r" in merged  # from Network.create_lines
