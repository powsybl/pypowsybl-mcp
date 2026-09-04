#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import pypowsybl.network as pn
from pypowsybl import _pypowsybl as _pp

from pypowsybl_mcp.utils.element_types import (
    ELEMENT_TYPE_TO_GETTER,
    element_type_enum,
    element_type_hint,
)


def test_keys_are_lowercased_element_type_names():
    # The public name is the ElementType enum member lowercased; the value is
    # the get_* method that returns its table.
    for element_type, getter in ELEMENT_TYPE_TO_GETTER.items():
        assert element_type == element_type.lower()
        enum_member = getattr(_pp.ElementType, element_type.upper())
        assert element_type_enum(element_type) is enum_member
        assert callable(getattr(pn.Network, getter, None))


def test_no_handwritten_aliases():
    # Element types are named after the pypowsybl ElementType, so human-friendly
    # synonyms (and the old plural getter names) are not element types.
    for alias in ("transformers", "transformer", "svc", "2wt", "lines", "generators"):
        assert alias not in ELEMENT_TYPE_TO_GETTER


def test_covers_core_element_types():
    for element_type in (
        "generator",
        "load",
        "line",
        "bus",
        "switch",
        "two_windings_transformer",
        "three_windings_transformer",
        "static_var_compensator",
    ):
        assert element_type in ELEMENT_TYPE_TO_GETTER


def test_is_exhaustive_over_table_getters():
    # Anything pypowsybl can return as a whole table is exposed, not just the
    # handful of types the tools originally listed.
    for element_type in (
        "area",
        "battery",
        "boundary_line",
        "branch",
        "busbar_section",
        "identifiable",
        "injection",
        "terminal",
        "tie_line",
        "voltage_level",
    ):
        assert element_type in ELEMENT_TYPE_TO_GETTER
    assert len(ELEMENT_TYPE_TO_GETTER) > 30


def test_pluralised_getter_names_are_the_values_not_the_keys():
    # The value can no longer be reconstructed from the key: get_lines is the
    # getter for "line", get_2_windings_transformers for
    # "two_windings_transformer".
    assert ELEMENT_TYPE_TO_GETTER["line"] == "get_lines"
    assert (
        ELEMENT_TYPE_TO_GETTER["two_windings_transformer"]
        == "get_2_windings_transformers"
    )


def test_operational_limits_resolves_to_default_selected_variant():
    # get_operational_limits() with defaults returns the SELECTED limits, so the
    # runtime-observed key is "selected_operational_limits".
    assert "selected_operational_limits" in ELEMENT_TYPE_TO_GETTER
    assert (
        ELEMENT_TYPE_TO_GETTER["selected_operational_limits"]
        == "get_operational_limits"
    )


def test_excludes_getters_that_are_not_element_tables():
    # Need an argument, return something else than a table, or are deprecated.
    # These are ElementType names lowercased that have no bare table getter.
    for name in (
        "internal_connection",
        "minmax_reactive_limits",
        "operational_limits",
    ):
        assert name not in ELEMENT_TYPE_TO_GETTER


def test_element_type_enum_round_trips():
    for element_type in ("line", "generator", "two_windings_transformer"):
        enum_member = element_type_enum(element_type)
        assert isinstance(enum_member, _pp.ElementType)
        assert enum_member.name.lower() == element_type


def test_element_type_hint_suggests_the_canonical_name():
    hint = element_type_hint("transformer")
    assert "two_windings_transformer" in hint
    assert "Did you mean" in hint


def test_element_type_hint_lists_a_restricted_set():
    hint = element_type_hint("unrelated", ["line", "generator"])
    assert hint == "Supported types: line, generator"
