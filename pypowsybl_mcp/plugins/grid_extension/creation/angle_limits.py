#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Voltage angle limits: pypowsybl creates them but cannot remove them.

A voltage angle limit holds the terminals of the two elements it names. Once one
of them is removed -- or replaced, as the topology operations replace the lines
they cut, tap or merge -- the limit points at a removed terminal and every AC
load flow fails with "Cannot access bus of removed equipment" for the rest of the
session. pypowsybl (1.16) has no call to remove or update such a limit, and
``Network.remove_elements`` does not find it.

So ``voltage_angle_limits`` is not offered for creation (``LEFT_OUT_TYPES``), and
the removal and topology tools refuse to remove or replace an element a limit
names: such limits still come with networks loaded from a file.
"""

from collections.abc import Iterable

import pypowsybl as pp

#: what a refusal says happens next, and what the caller can do instead
CONSEQUENCE = (
    "pypowsybl cannot remove a voltage angle limit, and once an element it names "
    "is gone every AC load flow fails ('Cannot access bus of removed equipment'). "
    "Keep the element, or reload the network from a file without the limit"
)


def angle_limits_naming(network, element_ids: Iterable[str]) -> dict[str, list[str]]:
    """Element id -> the voltage angle limits that name it, among ``element_ids``."""
    ids = set(element_ids)
    if not ids:
        return {}
    try:
        limits = network.get_voltage_angle_limits()
    except (AttributeError, pp.PyPowsyblError):  # pragma: no cover - no such limits
        return {}
    named: dict[str, set[str]] = {}
    for column in ("from_element_id", "to_element_id"):
        if column not in limits.columns:  # pragma: no cover - defensive
            continue
        for limit_id, element_id in limits[column].items():
            if element_id in ids:
                named.setdefault(element_id, set()).add(str(limit_id))
    return {element_id: sorted(ids) for element_id, ids in sorted(named.items())}


def describe_naming(named: dict[str, list[str]]) -> str:
    """Each named element with its limits: 'L1' (named by voltage angle limit 'VAL')."""
    return ", ".join(
        f"'{element_id}' (named by voltage angle limit "
        + ", ".join(f"'{limit}'" for limit in limits)
        + ")"
        for element_id, limits in named.items()
    )
