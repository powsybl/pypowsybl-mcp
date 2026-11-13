#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""How each element type is actually created in pypowsybl.

The calls themselves are observed, not written down: ``schema.CREATE_CALLS``
and ``schema.BAY_CALLS`` give, for every element type, the ``Network.create_*``
method or the bay helper that creates it and the names of its DataFrame
parameters. :class:`DerivedCall` turns any of them into an executor: it splits
the attributes over those DataFrames by metadata group, fills a table
attribute's rows into the DataFrame they belong to, and makes the call. Which
of the two calls a type uses follows from how it connects (see
``profiles.CreationProfile.executor``).

Injections and branches go through the *bay* helpers rather than the raw
dataframe API on purpose: a bay builds the switching equipment the hosting
voltage level requires (a breaker and a closed disconnector onto a busbar
section in node/breaker, a plain attachment in bus/breaker), which is what makes
one tool work on an IEEE test case and on a detailed grid file alike.

The one convention no derived call covers is the voltage level: two different
calls in a row, the second filling it with connection points.
:class:`VoltageLevel` keeps it explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pypowsybl as pp

from pypowsybl_mcp.plugins.grid_extension.creation.context import CreationError
from pypowsybl_mcp.plugins.grid_extension.creation.schema import (
    CreationCall,
    ElementSchema,
    Field,
    creation_schema,
    modification_schema,
    plain_modification_schema,
)


def resolve_call(call: str):
    """The pypowsybl callable a dotted name such as ``Network.create_loads`` or
    ``pp.network.create_load_bay`` designates, or None when it no longer exists.

    Profiles name their calls as strings, resolved only when used, so that an
    upgrade removing one does not stop the server from importing.
    """
    owner, _, name = call.rpartition(".")
    target = pp.network.Network if owner.endswith("Network") else pp.network
    return getattr(target, name, None)


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
    #: whether every row of the table attribute must give the same columns
    uniform_rows: bool = False

    def schema(self, element_type: str) -> ElementSchema:  # pragma: no cover
        raise NotImplementedError

    def execute(
        self, network, element_type: str, element_id: str, attributes: dict[str, Any]
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def generated_ids(self, network, element_type: str, element_id: str) -> list[str]:
        """Ids pypowsybl generated with the element, which later calls need."""
        return []

    def missing_call(self) -> str | None:
        """Name of a pypowsybl callable this executor needs but cannot find.

        The check that turns an upstream rename into a failing test instead of a
        runtime error in front of a user.
        """
        for call in self.calls:
            if resolve_call(call) is None:
                return call
        return None


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
        sections = int(topology.get("section_count") or 1)
        if sections > 1 and not topology.get("switch_kinds"):
            # One switch between consecutive sections; a disconnector is the
            # conservative choice, as a breaker implies protection equipment.
            topology["switch_kinds"] = ", ".join(["DISCONNECTOR"] * (sections - 1))

        network.create_voltage_levels(id=element_id, **level)
        try:
            pp.network.create_voltage_level_topology(network, id=element_id, **topology)
        except Exception:
            # Left in place, an empty voltage level could host nothing and its
            # id would make the corrected call fail as a duplicate.
            pp.network.remove_voltage_levels(network, voltage_level_ids=[element_id])
            raise

    def generated_ids(self, network, element_type, element_id):
        # The ids of the connection points are what every following creation
        # needs, and they are generated by pypowsybl, so report them.
        sections = network.get_busbar_sections()
        points = sections[sections["voltage_level_id"] == element_id].index.tolist()
        if not points:
            buses = network.get_bus_breaker_view_buses()
            points = buses[buses["voltage_level_id"] == element_id].index.tolist()
        return points


@dataclass(frozen=True)
class DerivedCall(Executor):
    """Any observed pypowsybl creation call, from its registry entry.

    The attributes are spread over the call's DataFrames by metadata group:
    group 0 is the main DataFrame (one row, keyed on the element's id), each
    further group a DataFrame of its own, passed only when something fills it
    (the linear model of a shunt, the generation part of a boundary line). The
    rows of ``rows_attribute`` fill the DataFrame of ``rows_group`` instead (the
    steps of a tap changer, the sections of a non-linear shunt), or, when that
    group is 0, the main DataFrame itself (the rows of a limits group, the
    points of a capability curve).
    """

    call: CreationCall
    rows_attribute: str | None = None
    rows_group: int = 1
    #: values a row of the table attribute gets for the columns it leaves out
    row_defaults: dict = field(default_factory=dict)
    #: whether the call adds to an existing element (limits, tap changers)
    attaches: bool = False
    #: only the main DataFrame, for a type created from its schema alone
    main_frame_only: bool = False

    @property
    def calls(self) -> tuple[str, ...]:
        return (self.call.name,)

    @property
    def uniform_rows(self) -> bool:
        # Rows of the main DataFrame become one list per column upstream, so a
        # column some rows leave out cannot be passed at all.
        return bool(self.rows_attribute) and self.rows_group == 0

    def schema(self, element_type: str) -> ElementSchema:
        schema = (
            modification_schema(element_type, self.call.modification)
            if self.call.modification
            else creation_schema(element_type)
        )
        if self.main_frame_only:
            return ElementSchema(fields=schema.group(0))
        return schema

    def describe(self) -> str:
        if self.call.modification == "CREATE_FEEDER_BAY":
            return (
                "created with its bay: connected to the bus, or to the busbar section "
                "through a breaker and a closed disconnector in node/breaker topology"
            )
        if self.call.modification:
            return "created with a feeder bay at each end"
        if self.attaches and self.rows_attribute and self.rows_group > 0:
            return (
                f"attached to an existing element with {self.call.name}(), with "
                f"its {self.rows_attribute} table"
            )
        if self.attaches:
            return f"attached to an existing element with {self.call.name}()"
        if self.rows_attribute:
            return f"created with its {self.rows_attribute} table"
        return f"created directly with {self.call.name}()"

    def execute(self, network, element_type, element_id, attributes):
        schema = self.schema(element_type)
        index = schema.index_name
        rows = attributes.get(self.rows_attribute) if self.rows_attribute else None
        taken: set[str] = {self.rows_attribute} if self.rows_attribute else set()
        frames: dict[str, pd.DataFrame] = {}

        extra = () if self.main_frame_only else self.call.frames[1:]
        for group, keyword in enumerate(extra, start=1):
            if self.rows_attribute and group == self.rows_group:
                if rows:
                    frames[keyword] = _frame(
                        [
                            {"id": element_id, **self.row_defaults, **row}
                            for row in rows
                        ],
                        index="id",
                    )
                continue
            names = {item.name for item in schema.group(group)} - {"id"}
            row = {name: value for name, value in attributes.items() if name in names}
            if row:
                taken |= set(row)
                frames[keyword] = _frame([{"id": element_id, **row}], index="id")

        if self.rows_attribute and self.rows_group == 0:
            # The rows are the main DataFrame itself, so an attribute given
            # once for the whole call (the group of a set of limits) goes on
            # every row; a row's own value wins.
            shared = {
                name: value
                for name, value in attributes.items()
                if name not in taken and name in schema.names and name != index
            }
            main = [
                {index: element_id, **self.row_defaults, **shared, **row}
                for row in rows or []
            ]
        else:
            main = [
                {
                    index: element_id,
                    **{
                        name: value
                        for name, value in attributes.items()
                        if name not in taken
                    },
                }
            ]
        frames[self.call.frames[0]] = _frame(main, index=index)

        if self.call.owner == "Network":
            getattr(network, self.call.function)(**frames)
        else:
            getattr(pp.network, self.call.function)(network, **frames)


def _main_frame(call: CreationCall) -> DerivedCall:
    return DerivedCall(call, main_frame_only=True)


@dataclass(frozen=True)
class AddToList(Executor):
    """One entry added to a list pypowsybl only takes whole.

    ``create_areas_voltage_levels`` and ``create_areas_boundaries`` replace an
    area's whole list with the rows they get, so one entry per call would leave
    only the last one. The new entry is sent alone first, which pypowsybl
    checks and completes with its defaults; then the entries the area had
    before are sent back with it.
    """

    call: CreationCall

    @property
    def calls(self) -> tuple[str, ...]:
        return (self.call.name, self.getter)

    @property
    def getter(self) -> str:
        """``Network.get_areas_voltage_levels`` for ``create_areas_voltage_levels``."""
        return f"{self.call.owner}.get_{self.call.function.removeprefix('create_')}"

    def schema(self, element_type: str) -> ElementSchema:
        return _main_frame(self.call).schema(element_type)

    def describe(self) -> str:
        return (
            f"added with {self.call.name}() to the entries the element already "
            "has, which that call would otherwise replace"
        )

    def _entries(self, network, element_type, element_id) -> list[dict]:
        table = getattr(network, self.getter.rpartition(".")[2])(all_attributes=True)
        schema = self.schema(element_type)
        columns = [
            name
            for name in schema.names
            if name != schema.index_name and name in table.columns
        ]
        return table.loc[table.index == element_id, columns].to_dict("records")

    def execute(self, network, element_type, element_id, attributes):
        before = self._entries(network, element_type, element_id)
        _main_frame(self.call).execute(network, element_type, element_id, attributes)
        if not before:
            return
        added = [
            entry
            for entry in self._entries(network, element_type, element_id)
            if entry not in before
        ]
        index = self.schema(element_type).index_name
        getattr(network, self.call.function)(
            _frame(
                [{index: element_id, **entry} for entry in before + added],
                index=index,
            )
        )


@dataclass(frozen=True)
class Unavailable(Executor):
    """The call a profile needs, missing from the installed pypowsybl.

    Never raised at import: the profile is then reported, and set aside, by the
    startup check (``drift.py``) instead of stopping the server.
    """

    element_type: str
    family: str

    @property
    def calls(self) -> tuple[str, ...]:
        return ()

    def _message(self) -> str:
        return f"the installed pypowsybl has no {self.family} for {self.element_type}"

    def missing_call(self) -> str | None:
        return self._message()

    def schema(self, element_type: str) -> ElementSchema:
        raise CreationError(self._message())

    def describe(self) -> str:
        return self._message()

    def execute(self, network, element_type, element_id, attributes):
        raise CreationError(self._message())
