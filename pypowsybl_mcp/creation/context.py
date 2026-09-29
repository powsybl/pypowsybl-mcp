#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Values shared by the rules, derivations and executors of one creation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NODE_BREAKER = "NODE_BREAKER"
BUS_BREAKER = "BUS_BREAKER"


class CreationError(ValueError):
    """A creation request that is inconsistent with the network or the schema.

    The message is written to be handed back to the caller as is. ``hint``
    carries the part of the element descriptor that would have prevented the
    mistake, so a rejected call can correct itself without a second lookup.
    """

    def __init__(self, message: str, hint: dict | None = None):
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class ConnectionPoint:
    """A validated bus or busbar section an element can be attached to."""

    id: str
    voltage_level_id: str
    substation_id: str | None
    topology_kind: str
    nominal_v: float

    @property
    def is_node_breaker(self) -> bool:
        return self.topology_kind == NODE_BREAKER


@dataclass
class CreationContext:
    """Everything a rule, a derivation or an executor needs about one creation."""

    network: Any
    element_type: str
    element_id: str
    attributes: dict[str, Any]
    connections: tuple[ConnectionPoint, ...] = ()
    #: element the creation attaches to, for limits and tap changers
    target_type: str | None = None
    notes: list[str] = field(default_factory=list)

    def connection(self, side: int = 0) -> ConnectionPoint | None:
        """The connection point of a side, 0-based, or None when there is none."""
        if side < len(self.connections):
            return self.connections[side]
        return None

    def has(self, name: str) -> bool:
        """Whether an attribute was given a non-null value."""
        return self.attributes.get(name) is not None

    def truthy(self, name: str) -> bool:
        return bool(self.attributes.get(name))
