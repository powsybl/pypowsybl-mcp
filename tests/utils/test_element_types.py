#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from pypowsybl_mcp.utils.element_types import ELEMENT_TYPE_TO_GETTER


def test_getters_are_get_prefixed_methods():
    assert all(getter.startswith("get_") for getter in ELEMENT_TYPE_TO_GETTER.values())


def test_aliases_share_a_getter():
    # Aliases must resolve to the same getter as their canonical name.
    assert (
        ELEMENT_TYPE_TO_GETTER["transformers"]
        == ELEMENT_TYPE_TO_GETTER["2_windings_transformers"]
        == "get_2_windings_transformers"
    )
    assert (
        ELEMENT_TYPE_TO_GETTER["svc"]
        == ELEMENT_TYPE_TO_GETTER["static_var_compensators"]
        == "get_static_var_compensators"
    )


def test_covers_core_element_types():
    for element_type in ("generators", "loads", "lines", "buses", "switches"):
        assert element_type in ELEMENT_TYPE_TO_GETTER
