#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""How each element type is actually created in pypowsybl.

The metadata says *what* a creation takes, never *how* to call it: a load goes
through ``pypowsybl.network.create_load_bay(network, **attrs)``, a shunt through
the same family but with two DataFrames, a substation through
``Network.create_substations(**attrs)`` with no bay at all, and a voltage level
through two calls in a row. Executors carry that knowledge, one class per
calling convention, and each one also exposes the schema of the call it makes --
so the field list still comes from pypowsybl even though the call does not.

Injections and branches go through the *bay* helpers rather than the raw
dataframe API on purpose: a bay builds the switching equipment the hosting
voltage level requires (a breaker and a closed disconnector onto a busbar
section in node/breaker, a plain attachment in bus/breaker), which is what makes
one tool work on an IEEE test case and on a detailed grid file alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pypowsybl as pp

from pypowsybl_mcp.creation.schema import (
    ElementSchema,
    Field,
    creation_schema,
    modification_schema,
    plain_modification_schema,
)


def _frame(rows: list[dict], index: str) -> pd.DataFrame:
    """Build the indexed DataFrame the dataframe-based APIs expect."""
    return pd.DataFrame.from_records(index=index, data=rows)


def _merge(*schemas: ElementSchema) -> ElementSchema:
    """Merge schemas of a composite call, keeping the first definition of a field."""
    fields: list[Field] = []
    seen: set[str] = set()
    for schema in schemas:
        for item in schema.fields:
            if item.name in seen:
                continue
            seen.add(item.name)
            fields.append(item)
    return ElementSchema(fields=tuple(fields))


class Executor:
    """Base class: the pypowsybl call behind one element type."""

    #: set by subclasses, used by the consistency test and by the descriptor
    calls: tuple[str, ...] = ()

    def schema(self, element_type: str) -> ElementSchema:  # pragma: no cover
        raise NotImplementedError

    def execute(
        self, network, element_type: str, element_id: str, attributes: dict[str, Any]
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def report_extra(self, network, element_type: str, element_id: str) -> str | None:
        """Anything worth adding to the report after a successful creation."""
        return None

    def missing_call(self) -> str | None:
        """Name of a pypowsybl callable this executor needs but cannot find.

        The check that turns an upstream rename into a failing test instead of a
        runtime error in front of a user.
        """
        for call in self.calls:
            owner, _, name = call.rpartition(".")
            target = pp.network.Network if owner.endswith("Network") else pp.network
            if not hasattr(target, name):
                return call
        return None


@dataclass(frozen=True)
class FeederBay(Executor):
    """An injection created with its bay: ``create_<x>_bay(network, **attrs)``."""

    function: str

    @property
    def calls(self) -> tuple[str, ...]:
        return (f"pp.network.{self.function}",)

    def schema(self, element_type: str) -> ElementSchema:
        return modification_schema(element_type, "CREATE_FEEDER_BAY")

    def describe(self) -> str:
        return (
            "created with its bay: connected to the bus, or to the busbar section "
            "through a breaker and a closed disconnector in node/breaker topology"
        )

    def execute(self, network, element_type, element_id, attributes):
        getattr(pp.network, self.function)(network, id=element_id, **attributes)


@dataclass(frozen=True)
class BranchBays(Executor):
    """A branch created with a bay at each end."""

    function: str
    modification: str

    @property
    def calls(self) -> tuple[str, ...]:
        return (f"pp.network.{self.function}",)

    def schema(self, element_type: str) -> ElementSchema:
        return modification_schema(element_type, self.modification)

    def describe(self) -> str:
        return "created with a feeder bay at each end"

    def execute(self, network, element_type, element_id, attributes):
        getattr(pp.network, self.function)(network, id=element_id, **attributes)


@dataclass(frozen=True)
class Raw(Executor):
    """An element pypowsybl creates without switching equipment."""

    method: str

    @property
    def calls(self) -> tuple[str, ...]:
        return (f"Network.{self.method}",)

    def schema(self, element_type: str) -> ElementSchema:
        return creation_schema(element_type)

    def describe(self) -> str:
        return f"created directly with Network.{self.method}()"

    def execute(self, network, element_type, element_id, attributes):
        index = self.schema(element_type).index_name
        getattr(network, self.method)(**{index: element_id}, **attributes)


@dataclass(frozen=True)
class VoltageLevel(Executor):
    """A voltage level plus the connection points inside it.

    Two calls: the voltage level itself, then the topology that fills it with
    buses (bus/breaker) or busbar sections (node/breaker). A voltage level
    without connection points can host nothing, so the pair is one operation.
    """

    calls: tuple[str, ...] = (
        "Network.create_voltage_levels",
        "pp.network.create_voltage_level_topology",
    )

    def schema(self, element_type: str) -> ElementSchema:
        return _merge(
            creation_schema(element_type),
            plain_modification_schema("VOLTAGE_LEVEL_TOPOLOGY_CREATION"),
        )

    def describe(self) -> str:
        return (
            "created together with its connection points (buses in bus/breaker, "
            "busbar sections in node/breaker)"
        )

    def execute(self, network, element_type, element_id, attributes):
        topology_names = set(
            plain_modification_schema("VOLTAGE_LEVEL_TOPOLOGY_CREATION").names
        )
        topology = {
            name: value
            for name, value in attributes.items()
            if name in topology_names and name != "id"
        }
        level = {
            name: value for name, value in attributes.items() if name not in topology
        }
        network.create_voltage_levels(id=element_id, **level)

        sections = int(topology.get("section_count") or 1)
        if sections > 1 and not topology.get("switch_kinds"):
            # One switch between consecutive sections; a disconnector is the
            # conservative choice, as a breaker implies protection equipment.
            topology["switch_kinds"] = ", ".join(["DISCONNECTOR"] * (sections - 1))
        pp.network.create_voltage_level_topology(network, id=element_id, **topology)

    def report_extra(self, network, element_type, element_id):
        # The ids of the connection points are what every following creation
        # needs, and they are generated by pypowsybl, so report them.
        sections = network.get_busbar_sections()
        points = sections[sections["voltage_level_id"] == element_id].index.tolist()
        if not points:
            buses = network.get_bus_breaker_view_buses()
            points = buses[buses["voltage_level_id"] == element_id].index.tolist()
        return f", connection points: {', '.join(points)}" if points else None


@dataclass(frozen=True)
class MultiFrameBay(Executor):
    """A bay whose call takes several DataFrames, e.g. a shunt and its model.

    ``extra_frames`` maps the keyword of each additional DataFrame to the schema
    group holding its fields; those fields are single-row, so the caller passes
    them flat like any other attribute and they are split out here.
    """

    function: str
    main_frame: str
    extra_frames: tuple[tuple[str, int], ...]
    fixed: dict = field(default_factory=dict)

    @property
    def calls(self) -> tuple[str, ...]:
        return (f"pp.network.{self.function}",)

    def schema(self, element_type: str) -> ElementSchema:
        return modification_schema(element_type, "CREATE_FEEDER_BAY")

    def describe(self) -> str:
        return "created with its bay, from several dataframes"

    def execute(self, network, element_type, element_id, attributes):
        schema = self.schema(element_type)
        frames = {}
        taken: set[str] = set()
        for keyword, group in self.extra_frames:
            names = {item.name for item in schema.group(group)} - {"id"}
            row = {name: value for name, value in attributes.items() if name in names}
            taken |= set(row)
            frames[keyword] = _frame([{"id": element_id, **row}], index="id")
        main = {name: value for name, value in attributes.items() if name not in taken}
        frames[self.main_frame] = _frame(
            [{"id": element_id, **self.fixed, **main}], index="id"
        )
        getattr(pp.network, self.function)(network, **frames)


@dataclass(frozen=True)
class RowGroup(Executor):
    """A call taking a header DataFrame plus a table of rows, e.g. tap changers.

    The rows live in schema group 1 and are supplied as a list of dictionaries
    under ``rows_attribute``; every row is completed with the columns the API
    expects, defaulted to the neutral value.
    """

    method: str
    rows_attribute: str
    row_defaults: dict = field(default_factory=dict)

    @property
    def calls(self) -> tuple[str, ...]:
        return (f"Network.{self.method}",)

    def schema(self, element_type: str) -> ElementSchema:
        return creation_schema(element_type)

    def describe(self) -> str:
        return f"created with its {self.rows_attribute} table"

    def execute(self, network, element_type, element_id, attributes):
        schema = self.schema(element_type)
        rows = attributes.get(self.rows_attribute) or []
        header = {
            name: value
            for name, value in attributes.items()
            if name != self.rows_attribute
        }
        row_names = {item.name for item in schema.group(1)} - {"id"}
        table = [
            {
                "id": element_id,
                **{name: self.row_defaults.get(name, 0.0) for name in row_names},
                **{name: value for name, value in row.items() if name in row_names},
            }
            for row in rows
        ]
        getattr(network, self.method)(
            _frame([{"id": element_id, **header}], index="id"),
            _frame(table, index="id"),
        )


@dataclass(frozen=True)
class Attachment(Executor):
    """Attributes attached to an element that already exists, e.g. limits.

    The "id" of the call is the id of the target element, and several rows may
    describe one target (the sides and durations of a limits group), so the
    caller supplies them under ``rows_attribute``.
    """

    method: str
    rows_attribute: str

    @property
    def calls(self) -> tuple[str, ...]:
        return (f"Network.{self.method}",)

    def schema(self, element_type: str) -> ElementSchema:
        return creation_schema(element_type)

    def describe(self) -> str:
        return f"attached to an existing element with Network.{self.method}()"

    def execute(self, network, element_type, element_id, attributes):
        schema = self.schema(element_type)
        index = schema.index_name
        rows = attributes.get(self.rows_attribute)
        if rows is None:
            rows = [
                {
                    name: value
                    for name, value in attributes.items()
                    if name != self.rows_attribute
                }
            ]
        columns = {name for name in schema.names if name != index}
        payload: dict[str, list] = {index: []}
        for row in rows:
            payload[index].append(element_id)
            for name in columns:
                if name in row:
                    payload.setdefault(name, []).append(row[name])
        # Only pass the columns every row provided; a ragged call is rejected
        # upstream with a much less helpful message.
        complete = {
            name: values
            for name, values in payload.items()
            if len(values) == len(payload[index])
        }
        getattr(network, self.method)(**complete)
