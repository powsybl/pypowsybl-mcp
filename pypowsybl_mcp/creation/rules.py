#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Constraints a creation must satisfy, expressed as data.

These are the checks the pypowsybl metadata cannot express: a field required
only under a condition, an ordering between three values, a reactance that must
not be zero. Each rule names the fields it touches, so a test can verify that
they still exist in the introspected schema, and each one can describe itself,
so ``describe_element_creation`` explains the constraint before it is hit
rather than only when it fails.

Rules run after defaults and derivations, on the fully assembled attributes.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from pypowsybl_mcp.creation.context import BUS_BREAKER, CreationContext, CreationError


class Rule:
    """Base class: a constraint over the attributes of one creation."""

    #: attribute names the rule refers to, checked against the schema by a test
    fields: tuple[str, ...] = ()

    def describe(self) -> str:  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    def apply(self, context: CreationContext) -> None:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class RequiredIf(Rule):
    """``field`` must be given when ``when`` is true."""

    field: str
    when: str
    because: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.field, self.when)

    def describe(self) -> str:
        return f"'{self.field}' is required when '{self.when}' is true{self._why()}"

    def _why(self) -> str:
        return f" ({self.because})" if self.because else ""

    def apply(self, context: CreationContext) -> None:
        if context.truthy(self.when) and not context.has(self.field):
            raise CreationError(
                f"'{self.field}' is required when '{self.when}' is true{self._why()}"
            )


@dataclass(frozen=True)
class DefaultUnless(Rule):
    """Give ``field`` a value when it is missing and ``unless`` is false."""

    field: str
    value: object
    unless: str

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.field, self.unless)

    def describe(self) -> str:
        return f"'{self.field}' defaults to {self.value} when '{self.unless}' is false"

    def apply(self, context: CreationContext) -> None:
        if not context.truthy(self.unless) and not context.has(self.field):
            context.attributes[self.field] = self.value


@dataclass(frozen=True)
class Ordered(Rule):
    """Values must be non-decreasing, e.g. ``min_p <= target_p <= max_p``."""

    names: tuple[str, ...]

    @property
    def fields(self) -> tuple[str, ...]:
        return self.names

    def describe(self) -> str:
        return " <= ".join(f"'{name}'" for name in self.names)

    def apply(self, context: CreationContext) -> None:
        given = [
            (name, context.attributes[name]) for name in self.names if context.has(name)
        ]
        for (low_name, low), (high_name, high) in pairwise(given):
            if low > high:
                raise CreationError(
                    f"'{low_name}' ({low}) must not be greater than "
                    f"'{high_name}' ({high}): "
                    + " <= ".join(f"'{name}'" for name in self.names)
                    + " is required"
                )


@dataclass(frozen=True)
class NonZero(Rule):
    """A value that would make the load flow singular."""

    field: str
    because: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.field,)

    def describe(self) -> str:
        return f"'{self.field}' must not be 0{self._why()}"

    def _why(self) -> str:
        return f" ({self.because})" if self.because else ""

    def apply(self, context: CreationContext) -> None:
        if context.has(self.field) and context.attributes[self.field] == 0:
            raise CreationError(f"'{self.field}' must not be 0{self._why()}")


@dataclass(frozen=True)
class Positive(Rule):
    """A value that only makes sense above zero."""

    field: str

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.field,)

    def describe(self) -> str:
        return f"'{self.field}' must be positive"

    def apply(self, context: CreationContext) -> None:
        if context.has(self.field) and context.attributes[self.field] <= 0:
            raise CreationError(
                f"'{self.field}' must be positive, got {context.attributes[self.field]}"
            )


@dataclass(frozen=True)
class AtLeast(Rule):
    """An integer count with a floor, e.g. at least one busbar."""

    field: str
    minimum: int

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.field,)

    def describe(self) -> str:
        return f"'{self.field}' must be at least {self.minimum}"

    def apply(self, context: CreationContext) -> None:
        if context.has(self.field) and context.attributes[self.field] < self.minimum:
            raise CreationError(
                f"'{self.field}' must be at least {self.minimum}, got "
                f"{context.attributes[self.field]}"
            )


@dataclass(frozen=True)
class AtMost(Rule):
    """A value bounded by another attribute, e.g. sections in service."""

    field: str
    bound: str

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.field, self.bound)

    def describe(self) -> str:
        return f"'{self.field}' must not exceed '{self.bound}'"

    def apply(self, context: CreationContext) -> None:
        if (
            context.has(self.field)
            and context.has(self.bound)
            and context.attributes[self.field] > context.attributes[self.bound]
        ):
            raise CreationError(
                f"'{self.field}' ({context.attributes[self.field]}) must not "
                f"exceed '{self.bound}' ({context.attributes[self.bound]})"
            )


class DistinctEnds(Rule):
    """The two ends of a branch must be two different connection points."""

    fields: tuple[str, ...] = ()

    def describe(self) -> str:
        return "the two ends must be distinct connection points"

    def apply(self, context: CreationContext) -> None:
        side1, side2 = context.connection(0), context.connection(1)
        if side1 is not None and side2 is not None and side1.id == side2.id:
            raise CreationError(
                f"Both ends point at '{side1.id}': a branch must connect two "
                "distinct connection points"
            )


class SameSubstation(Rule):
    """Both ends must sit in one substation, as powsybl requires for a transformer."""

    fields: tuple[str, ...] = ()

    def describe(self) -> str:
        return "both ends must belong to voltage levels of the same substation"

    def apply(self, context: CreationContext) -> None:
        side1, side2 = context.connection(0), context.connection(1)
        if side1 is None or side2 is None:
            return
        if side1.substation_id != side2.substation_id:
            raise CreationError(
                f"A two-windings transformer must stay inside one substation, but "
                f"'{side1.voltage_level_id}' belongs to '{side1.substation_id}' and "
                f"'{side2.voltage_level_id}' to '{side2.substation_id}'. Create both "
                "voltage levels in the same substation, or connect the two "
                "substations with a line"
            )


class BusBreakerOnly(Rule):
    """The element has no bay creation upstream, so it needs a configured bus."""

    fields: tuple[str, ...] = ()

    def describe(self) -> str:
        return "only bus/breaker voltage levels are supported for this element type"

    def apply(self, context: CreationContext) -> None:
        point = context.connection(0)
        if point is not None and point.is_node_breaker:
            raise CreationError(
                f"Voltage level '{point.voltage_level_id}' is in "
                f"{point.topology_kind} topology, and pypowsybl provides no bay "
                f"creation for a {context.element_type}: it can only be created on "
                f"a bus of a {BUS_BREAKER} voltage level. Use "
                "generate_python_script() to build it with its own switching "
                "equipment"
            )


@dataclass(frozen=True)
class WarnIfDifferent(Rule):
    """Not an error: note it in the report when the two ends differ."""

    what: str
    message: str

    fields: tuple[str, ...] = ()

    def describe(self) -> str:
        return f"a note is added when the two ends have a different {self.what}"

    def apply(self, context: CreationContext) -> None:
        side1, side2 = context.connection(0), context.connection(1)
        if side1 is None or side2 is None:
            return
        if getattr(side1, self.what) != getattr(side2, self.what):
            context.notes.append(
                self.message.format(
                    side1=getattr(side1, self.what), side2=getattr(side2, self.what)
                )
            )


@dataclass(frozen=True)
class RequiredWhenEquals(Rule):
    """``field`` must be given when another attribute has a particular value."""

    field: str
    other: str
    value: object
    because: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.field, self.other)

    def describe(self) -> str:
        return f"'{self.field}' is required when '{self.other}' is '{self.value}'"

    def apply(self, context: CreationContext) -> None:
        if context.attributes.get(self.other) == self.value and not context.has(
            self.field
        ):
            because = f" ({self.because})" if self.because else ""
            raise CreationError(
                f"'{self.field}' is required when '{self.other}' is "
                f"'{self.value}'{because}"
            )


@dataclass(frozen=True)
class RowsRequired(Rule):
    """A table-shaped attribute that cannot be empty, e.g. the steps of a tap changer."""

    attribute: str
    minimum: int = 1
    because: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        return ()

    def describe(self) -> str:
        return f"'{self.attribute}' needs at least {self.minimum} entries"

    def apply(self, context: CreationContext) -> None:
        rows = context.attributes.get(self.attribute) or []
        if len(rows) < self.minimum:
            because = f" ({self.because})" if self.because else ""
            raise CreationError(
                f"'{self.attribute}' needs at least {self.minimum} entries, got "
                f"{len(rows)}{because}"
            )
