#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import pypowsybl.network as pn

from pypowsybl_mcp.utils.element_types import (
    ELEMENT_TYPE_TO_GETTER,
    element_type_hint,
)


def test_getters_are_get_prefixed_methods_of_network():
    for element_type, getter in ELEMENT_TYPE_TO_GETTER.items():
        assert getter == f"get_{element_type}"
        assert callable(getattr(pn.Network, getter, None))


def test_no_handwritten_aliases():
    # Element types are named after the pypowsybl getter, so the human-friendly
    # synonyms we used to accept are not element types (see the element-types
    # skill for the wording-to-name translation).
    for alias in ("transformers", "svc", "2wt"):
        assert alias not in ELEMENT_TYPE_TO_GETTER


def test_covers_core_element_types():
    for element_type in (
        "generators",
        "loads",
        "lines",
        "buses",
        "switches",
        "2_windings_transformers",
        "3_windings_transformers",
        "static_var_compensators",
    ):
        assert element_type in ELEMENT_TYPE_TO_GETTER


def test_is_exhaustive_over_table_getters():
    # Anything pypowsybl can return as a whole table is exposed, not just the
    # handful of types the tools originally listed.
    for element_type in (
        "areas",
        "batteries",
        "boundary_lines",
        "branches",
        "busbar_sections",
        "identifiables",
        "injections",
        "terminals",
        "tie_lines",
        "voltage_levels",
    ):
        assert element_type in ELEMENT_TYPE_TO_GETTER
    assert len(ELEMENT_TYPE_TO_GETTER) > 30


def test_excludes_getters_that_are_not_element_tables():
    # Need an argument, return something else than a table, or are deprecated.
    for name in (
        "elements",
        "single_line_diagram",
        "variant_ids",
        "working_variant_id",
        "validation_level",
        "dangling_lines",
    ):
        assert name not in ELEMENT_TYPE_TO_GETTER


def test_element_type_hint_suggests_the_canonical_name():
    hint = element_type_hint("transformers")
    assert "2_windings_transformers" in hint
    assert "Did you mean" in hint


def test_element_type_hint_lists_a_restricted_set():
    hint = element_type_hint("unrelated", ["lines", "generators"])
    assert hint == "Supported types: lines, generators"
