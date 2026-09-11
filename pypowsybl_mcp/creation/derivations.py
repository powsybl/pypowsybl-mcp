#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Values the engine computes so the caller does not have to supply them.

A derivation fills one attribute when it was not given, from the network or
from the other attributes: the feeder position of a bay, the rated voltages of
a transformer, the charging limit of a battery, the steps of a tap changer.
Each one names what it produces, so a test can check that the attribute still
exists in the introspected schema, and describes itself for
``describe_element_creation``.

Derivations run after the defaults and before the rules, so a rule sees the
complete picture.
"""

from __future__ import annotations

from dataclasses import dataclass

from pypowsybl_mcp.creation.context import CreationContext

# Position orders only have to be unique inside a voltage level, so a global
# maximum is a safe (if generous) starting point.
POSITION_ORDER_STEP = 10
DEFAULT_POSITION_ORDER = 10


class Derivation:
    """Base class: fills one attribute when the caller left it out."""

    #: attribute the derivation produces, checked against the schema by a test.
    #: Annotated but not assigned, so subclasses that declare it without a
    #: default do not inherit one and stay ordered correctly as dataclasses.
    produces: str

    def describe(self) -> str:  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    def apply(self, context: CreationContext) -> None:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class PositionOrder(Derivation):
    """The ConnectablePosition order of a new bay, in node/breaker topology.

    pypowsybl refuses a bay without one ("Position order is null for attachment
    in node-breaker voltage level"), and the number only has to be unique inside
    its voltage level, so the highest order in the network plus a step is always
    safe. In bus/breaker topology the attribute is meaningless and dropped.
    """

    produces: str
    side: int = 0

    def describe(self) -> str:
        return (
            f"'{self.produces}' is derived from the existing feeder positions "
            "(node/breaker voltage levels only)"
        )

    def apply(self, context: CreationContext) -> None:
        point = context.connection(self.side)
        if point is None or not point.is_node_breaker:
            context.attributes.pop(self.produces, None)
            return
        if context.has(self.produces):
            return
        context.attributes[self.produces] = _next_position_order(context.network)


def _next_position_order(network) -> int:
    positions = network.get_extensions("position")
    if positions.empty or "order" not in positions.columns:
        return DEFAULT_POSITION_ORDER
    orders = positions["order"].dropna()
    if orders.empty:
        return DEFAULT_POSITION_ORDER
    return int(orders.max()) + POSITION_ORDER_STEP


@dataclass(frozen=True)
class DirectionOnlyInNodeBreaker(Derivation):
    """Drop the feeder direction where it has no meaning."""

    produces: str
    side: int = 0

    def describe(self) -> str:
        return f"'{self.produces}' applies to node/breaker voltage levels only"

    def apply(self, context: CreationContext) -> None:
        point = context.connection(self.side)
        if point is None or not point.is_node_breaker:
            context.attributes.pop(self.produces, None)


@dataclass(frozen=True)
class FromConnection(Derivation):
    """Copy a property of a resolved connection point into an attribute.

    Used by the element types pypowsybl creates without a bay, which take the
    voltage level and the bus as plain attributes.
    """

    produces: str
    attribute: str
    side: int = 0

    def describe(self) -> str:
        return (
            f"'{self.produces}' is taken from the connection point ({self.attribute})"
        )

    def apply(self, context: CreationContext) -> None:
        if context.has(self.produces):
            return
        point = context.connection(self.side)
        if point is not None:
            context.attributes[self.produces] = getattr(point, self.attribute)


@dataclass(frozen=True)
class NominalVoltage(Derivation):
    """Default a rated voltage to the nominal voltage of the end it sits on.

    The ratio of a transformer is carried by rated_u1 / rated_u2, so defaulting
    them to the two nominal voltages gives the transformer the ratio its
    position in the network implies, instead of a meaningless 1:1.
    """

    produces: str
    side: int

    def describe(self) -> str:
        return (
            f"'{self.produces}' defaults to the nominal voltage of side {self.side + 1}"
        )

    def apply(self, context: CreationContext) -> None:
        if context.has(self.produces):
            return
        point = context.connection(self.side)
        if point is not None:
            context.attributes[self.produces] = point.nominal_v


@dataclass(frozen=True)
class Negated(Derivation):
    """Default one bound to the opposite of another, e.g. a symmetric battery."""

    produces: str
    source: str

    def describe(self) -> str:
        return f"'{self.produces}' defaults to -'{self.source}'"

    def apply(self, context: CreationContext) -> None:
        if context.has(self.produces) or not context.has(self.source):
            return
        context.attributes[self.produces] = -float(context.attributes[self.source])


@dataclass(frozen=True)
class TapSteps(Derivation):
    """Generate the steps of a tap changer from a range, when none are given.

    Writing a step table by hand is the part of tap changer creation most likely
    to be got wrong, and the usual on-load tap changer is a regular ladder: a
    ratio changer spans +/- a few percent around 1, a phase shifter +/- a few
    degrees around 0. The caller can still pass the steps explicitly.
    """

    produces: str = "steps"
    count_field: str = "step_count"
    span_field: str = "range_percent"
    stepped: str = "rho"
    around: float = 1.0
    scale: float = 0.01

    def describe(self) -> str:
        return (
            f"'{self.produces}' are generated from '{self.count_field}' and "
            f"'{self.span_field}' when not given explicitly"
        )

    def apply(self, context: CreationContext) -> None:
        if context.attributes.get(self.produces):
            return
        count = int(context.attributes.get(self.count_field) or 0)
        span = float(context.attributes.get(self.span_field) or 0.0)
        if count < 2 or span <= 0:
            return
        half = span * self.scale
        step = 2 * half / (count - 1)
        values = [-half + index * step for index in range(count)]
        context.attributes[self.produces] = [
            {self.stepped: self.around + value} for value in values
        ]


@dataclass(frozen=True)
class MiddleTap(Derivation):
    """Start a tap changer on its neutral (middle) step.

    A transformer is normally commissioned in the middle of its range, and the
    middle step of a generated ladder is the neutral one.
    """

    produces: str = "tap"
    rows: str = "steps"

    def describe(self) -> str:
        return f"'{self.produces}' defaults to the middle step"

    def apply(self, context: CreationContext) -> None:
        if context.has(self.produces):
            return
        steps = context.attributes.get(self.rows) or []
        if steps:
            context.attributes[self.produces] = len(steps) // 2


@dataclass(frozen=True)
class LimitRows(Derivation):
    """Turn a permanent rating (and optional temporary ones) into limit rows.

    The pypowsybl shape is one row per (side, duration): a permanent limit is a
    row with an acceptable duration of -1, a temporary one a row with its
    duration in seconds, and powsybl rejects temporary limits unless a permanent
    one is present in the same call -- a second call *replaces* the group rather
    than adding to it. Expressing that as rows by hand is easy to get wrong, so
    the usual case is expressed as a rating plus a list of temporary ratings.
    """

    produces: str = "limits"
    permanent_field: str = "permanent_limit"
    temporary_field: str = "temporary_limits"
    side_field: str = "side"
    type_field: str = "limit_type"

    def describe(self) -> str:
        return (
            f"'{self.produces}' rows are built from '{self.permanent_field}', "
            f"'{self.temporary_field}', '{self.side_field}' and "
            f"'{self.type_field}'"
        )

    def apply(self, context: CreationContext) -> None:
        if context.attributes.get(self.produces):
            return
        permanent = context.attributes.get(self.permanent_field)
        if permanent is None:
            return
        sides = context.attributes.get(self.side_field) or "BOTH"
        sides = ["ONE", "TWO"] if sides == "BOTH" else [sides]
        kind = context.attributes.get(self.type_field) or "CURRENT"
        rows = []
        for side in sides:
            rows.append(
                {
                    "side": side,
                    "name": "permanent_limit",
                    "type": kind,
                    "value": float(permanent),
                    "acceptable_duration": -1,
                }
            )
            for temporary in context.attributes.get(self.temporary_field) or []:
                duration = int(temporary["acceptable_duration"])
                rows.append(
                    {
                        "side": side,
                        "name": temporary.get("name") or f"{duration}s",
                        "type": kind,
                        "value": float(temporary["value"]),
                        "acceptable_duration": duration,
                    }
                )
        context.attributes[self.produces] = rows
