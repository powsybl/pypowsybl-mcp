#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Canonical mapping from public element-type names to pypowsybl getters.

Several tools accept an ``element_type`` string from the MCP client and turn it
into a call on the network object. Keeping that mapping in one place avoids the
copies drifting apart (they previously supported slightly different sets and
aliases). Consumers that only accept a subset should derive their allow-list
from this dict rather than redefining the getter names.
"""

# Public element-type name -> name of the Network getter returning its table.
# Aliases (e.g. "transformers"/"2_windings_transformers", "svc") intentionally
# point at the same getter.
ELEMENT_TYPE_TO_GETTER: dict[str, str] = {
    "voltage_levels": "get_voltage_levels",
    "substations": "get_substations",
    "buses": "get_buses",
    "generators": "get_generators",
    "loads": "get_loads",
    "lines": "get_lines",
    "transformers": "get_2_windings_transformers",
    "2_windings_transformers": "get_2_windings_transformers",
    "3_windings_transformers": "get_3_windings_transformers",
    "hvdc_lines": "get_hvdc_lines",
    "shunt_compensators": "get_shunt_compensators",
    "static_var_compensators": "get_static_var_compensators",
    "svc": "get_static_var_compensators",
    "vsc_converter_stations": "get_vsc_converter_stations",
    "lcc_converter_stations": "get_lcc_converter_stations",
    "switches": "get_switches",
}
