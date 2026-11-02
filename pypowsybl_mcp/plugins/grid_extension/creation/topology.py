#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Topology operations: changing how existing elements are connected.

Coupling two busbars, inserting a substation on a line, tapping a line into
another and the reverts of those -- pypowsybl's network modifications that
cut, merge or reconnect lines rather than create one element. Their calls and
attribute lists are observed (``schema.TOPOLOGY_CALLS``); what pypowsybl does
not check is written down here, one :class:`TopologyOperation` each:

* a position along a line strictly between 0 and 100 % -- pypowsybl accepts
  150 % and gives the second half a negative impedance;
* a busbar at the line's nominal voltage -- pypowsybl connects a 12 kV bus onto
  a 135 kV line, and the load flow then fails;
* lines and connection points that exist, with the server's diagnostics rather
  than pypowsybl's (an unknown line is a bare ``NullPointerException``);
* the busbar of a tee replacement in the voltage level the removed branch leads
  to, and the attributes the reverts need to not crash.

After a revert, pypowsybl leaves the switches of the bays it no longer needs in
place, with nothing behind them; :class:`TopologyEngine` removes them.
"""

from __future__ import annotations

import difflib
import math
from dataclasses import dataclass, field
from typing import Any

import pypowsybl as pp

from pypowsybl_mcp.plugins.grid_extension.creation import rules as rule
from pypowsybl_mcp.plugins.grid_extension.creation.context import (
    NODE_BREAKER,
    CreationContext,
    CreationError,
)
from pypowsybl_mcp.plugins.grid_extension.creation.engine import (
    _coerce,
    resolve_connection_point,
)
from pypowsybl_mcp.plugins.grid_extension.creation.schema import (
    TOPOLOGY_CALLS,
    CreationCall,
    ElementSchema,
    plain_modification_schema,
)

# ---------------------------------------------------------------------------
# Checks pypowsybl does not make
# ---------------------------------------------------------------------------


def _line_voltage_levels(network, line_id: str) -> tuple[str, str]:
    line = network.get_lines().loc[line_id]
    return str(line["voltage_level1_id"]), str(line["voltage_level2_id"])


def _nominal_v(network, voltage_level_id: str) -> float:
    return float(network.get_voltage_levels().loc[voltage_level_id, "nominal_v"])


@dataclass(frozen=True)
class SameVoltage(rule.Rule):
    """The busbar must be at the nominal voltage of the line it connects to."""

    point: str
    line: str

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.point, self.line)

    def describe(self) -> str:
        return (
            f"the voltage level of '{self.point}' must be at the nominal voltage of "
            f"'{self.line}'"
        )

    def apply(self, context: CreationContext) -> None:
        point_id = context.attributes[self.point]
        line_id = context.attributes[self.line]
        point = resolve_connection_point(context.network, point_id)
        for end in _line_voltage_levels(context.network, line_id):
            line_v = _nominal_v(context.network, end)
            if not math.isclose(point.nominal_v, line_v, rel_tol=1e-3):
                raise CreationError(
                    f"'{point_id}' is in voltage level {point.voltage_level_id} at "
                    f"{point.nominal_v:g} kV, while line '{line_id}' is at "
                    f"{line_v:g} kV: a line cannot change voltage, a transformer is "
                    "needed to connect them"
                )


@dataclass(frozen=True)
class SameVoltageLevel(rule.Rule):
    """Two different connection points of one voltage level."""

    first: str
    second: str

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.first, self.second)

    def describe(self) -> str:
        return f"'{self.first}' and '{self.second}' must be two connection points of one voltage level"

    def apply(self, context: CreationContext) -> None:
        first = resolve_connection_point(
            context.network, context.attributes[self.first]
        )
        second = resolve_connection_point(
            context.network, context.attributes[self.second]
        )
        if first.id == second.id:
            raise CreationError(
                f"'{self.first}' and '{self.second}' are both '{first.id}': a coupling "
                "device joins two different busbars"
            )
        if first.voltage_level_id != second.voltage_level_id:
            raise CreationError(
                f"'{first.id}' is in voltage level {first.voltage_level_id} and "
                f"'{second.id}' in {second.voltage_level_id}: a coupling device joins "
                "two busbars of one voltage level"
            )


@dataclass(frozen=True)
class AtFarEnd(rule.Rule):
    """The busbar must be in the voltage level the removed branch of a tee leads to."""

    point: str
    removed: str
    tee_lines: tuple[str, str]

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.point, self.removed, *self.tee_lines)

    def describe(self) -> str:
        return (
            f"'{self.point}' must be in the voltage level '{self.removed}' leads to, "
            "away from the tee point"
        )

    def apply(self, context: CreationContext) -> None:
        network = context.network
        tee = set(_line_voltage_levels(network, context.attributes[self.tee_lines[0]]))
        tee &= set(_line_voltage_levels(network, context.attributes[self.tee_lines[1]]))
        removed = context.attributes[self.removed]
        far = set(_line_voltage_levels(network, removed)) - tee
        point = resolve_connection_point(network, context.attributes[self.point])
        if not tee:
            raise CreationError(
                f"'{context.attributes[self.tee_lines[0]]}' and "
                f"'{context.attributes[self.tee_lines[1]]}' do not meet: they are not "
                "the two sections of a tee"
            )
        if point.voltage_level_id not in far:
            raise CreationError(
                f"'{point.id}' is in voltage level {point.voltage_level_id}, but "
                f"'{removed}' leads from the tee point to {', '.join(sorted(far))}: the "
                "two sections are connected where the removed branch ended, so the "
                "busbar must be there"
            )


@dataclass(frozen=True)
class MeetAtAVoltageLevel(rule.Rule):
    """Two lines to merge must end at a common voltage level."""

    first: str
    second: str

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.first, self.second)

    def describe(self) -> str:
        return f"'{self.first}' and '{self.second}' must meet at a voltage level"

    def apply(self, context: CreationContext) -> None:
        first = context.attributes[self.first]
        second = context.attributes[self.second]
        common = set(_line_voltage_levels(context.network, first)) & set(
            _line_voltage_levels(context.network, second)
        )
        if not common:
            raise CreationError(
                f"'{first}' and '{second}' have no voltage level in common: only two "
                "lines meeting at one can be merged"
            )


# ---------------------------------------------------------------------------
# The operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopologyOperation:
    """What pypowsybl does not say about one topology operation."""

    #: named after its NetworkModificationType, lowercased
    operation: str
    summary: str
    guidance: str
    required: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    rules: tuple[rule.Rule, ...] = ()
    #: attributes naming a bus or busbar section, checked with diagnostics
    connection_points: tuple[str, ...] = ()
    #: attributes naming lines that must exist
    existing_lines: tuple[str, ...] = ()
    #: attributes naming elements the operation creates: their ids must be free
    new_ids: tuple[str, ...] = ()
    #: whether the switches it leaves with nothing behind them are removed
    removes_orphan_switches: bool = False

    @property
    def call(self) -> CreationCall | None:
        return TOPOLOGY_CALLS.get(self.operation)

    @property
    def schema(self) -> ElementSchema:
        if self.call is None:
            raise CreationError(
                f"the installed pypowsybl has no call for {self.operation}"
            )
        return plain_modification_schema(self.call.modification)


POSITION = rule.Between(
    "position_percent", 0.0, 100.0, "the place of the cut along the line, from side 1"
)

OPERATIONS: dict[str, TopologyOperation] = {
    operation.operation: operation
    for operation in (
        TopologyOperation(
            operation="create_coupling_device",
            summary=(
                "Couples two busbar sections (or buses) of one voltage level, with a "
                "breaker and its disconnectors."
            ),
            guidance=(
                "Couples two busbars so they can be operated as one node, or split "
                "again by opening the breaker. Both must be in the same voltage level."
            ),
            required=("bus_or_busbar_section_id_1", "bus_or_busbar_section_id_2"),
            connection_points=(
                "bus_or_busbar_section_id_1",
                "bus_or_busbar_section_id_2",
            ),
            rules=(
                SameVoltageLevel(
                    "bus_or_busbar_section_id_1", "bus_or_busbar_section_id_2"
                ),
            ),
        ),
        TopologyOperation(
            operation="connect_voltage_level_on_line",
            summary=(
                "Cuts a line and connects both halves to a busbar: inserts a substation "
                "on the line."
            ),
            guidance=(
                "The line is replaced by two lines, from its side 1 to the busbar and "
                "from the busbar to its side 2, its impedance split at "
                "position_percent (50 by default) and its limits copied to both. They "
                "are named line1_id and line2_id, '<line>_1' and '<line>_2' by default. "
                "The busbar must be at the line's nominal voltage. "
                "revert_connect_voltage_level_on_line undoes it."
            ),
            required=("bbs_or_bus_id", "line_id"),
            defaults={"position_percent": 50.0},
            units={"position_percent": "%"},
            connection_points=("bbs_or_bus_id",),
            existing_lines=("line_id",),
            new_ids=("line1_id", "line2_id"),
            rules=(POSITION, SameVoltage("bbs_or_bus_id", "line_id")),
        ),
        TopologyOperation(
            operation="create_line_on_line",
            summary=(
                "Taps a new line into an existing one: a tee point on the line, the new "
                "line running from it to a busbar."
            ),
            guidance=(
                "The line is cut at position_percent (50 by default) into line1_id and "
                "line2_id, joined at a fictitious voltage level (the tee point, "
                "'<line>_VL' by default), and new_line_id runs from there to the busbar. "
                "The new line has no limits: rate it with operational_limits. "
                "replace_tee_point_by_voltage_level_on_line turns the tee into a real "
                "connection at the busbar; revert_create_line_on_line undoes it."
            ),
            required=(
                "bbs_or_bus_id",
                "line_id",
                "new_line_id",
                "new_line_r",
                "new_line_x",
            ),
            defaults={
                "position_percent": 50.0,
                "new_line_g1": 0.0,
                "new_line_b1": 0.0,
                "new_line_g2": 0.0,
                "new_line_b2": 0.0,
            },
            units={
                "position_percent": "%",
                "new_line_r": "Ohm",
                "new_line_x": "Ohm",
                "new_line_g1": "S",
                "new_line_b1": "S",
                "new_line_g2": "S",
                "new_line_b2": "S",
            },
            connection_points=("bbs_or_bus_id",),
            existing_lines=("line_id",),
            new_ids=(
                "new_line_id",
                "line1_id",
                "line2_id",
                "fictitious_voltage_level_id",
                "fictitious_substation_id",
            ),
            rules=(
                POSITION,
                rule.NonZero(
                    "new_line_x", "a zero-impedance line makes the load flow singular"
                ),
                SameVoltage("bbs_or_bus_id", "line_id"),
            ),
        ),
        TopologyOperation(
            operation="replace_tee_point_by_voltage_level_on_line",
            summary=(
                "Replaces a tee point by a connection at a voltage level: the two line "
                "sections then end at its busbar, and the tapped branch goes."
            ),
            guidance=(
                "tee_point_line1 and tee_point_line2 are the two sections of the tapped "
                "line, tee_point_line_to_remove the branch from the tee point. The "
                "busbar must be in the voltage level that branch leads to: the sections "
                "are extended to it, becoming new_line1_id and new_line2_id, and the "
                "fictitious tee point is removed."
            ),
            required=(
                "tee_point_line1",
                "tee_point_line2",
                "tee_point_line_to_remove",
                "bbs_or_bus_id",
            ),
            connection_points=("bbs_or_bus_id",),
            existing_lines=(
                "tee_point_line1",
                "tee_point_line2",
                "tee_point_line_to_remove",
            ),
            new_ids=("new_line1_id", "new_line2_id"),
            rules=(
                AtFarEnd(
                    "bbs_or_bus_id",
                    "tee_point_line_to_remove",
                    ("tee_point_line1", "tee_point_line2"),
                ),
            ),
        ),
        TopologyOperation(
            operation="revert_connect_voltage_level_on_line",
            summary=(
                "Merges two lines back into one at the voltage level they meet, undoing "
                "connect_voltage_level_on_line."
            ),
            guidance=(
                "line1_id and line2_id are merged into line_id, which no longer goes "
                "through the voltage level they met at. Any two lines meeting at a "
                "voltage level can be merged, not only the halves of a cut. The switches "
                "that connected them there are removed."
            ),
            required=("line1_id", "line2_id", "line_id"),
            existing_lines=("line1_id", "line2_id"),
            new_ids=("line_id",),
            rules=(MeetAtAVoltageLevel("line1_id", "line2_id"),),
            removes_orphan_switches=True,
        ),
        TopologyOperation(
            operation="revert_create_line_on_line",
            summary=(
                "Undoes a tee: merges the two line sections back into one and removes "
                "the tapped line."
            ),
            guidance=(
                "line_to_be_merged1_id and line_to_be_merged2_id are merged into "
                "merged_line_id, line_to_be_deleted is removed with the fictitious tee "
                "point, and the switches that connected it at its busbar are removed."
            ),
            required=(
                "line_to_be_merged1_id",
                "line_to_be_merged2_id",
                "line_to_be_deleted",
                "merged_line_id",
            ),
            existing_lines=(
                "line_to_be_merged1_id",
                "line_to_be_merged2_id",
                "line_to_be_deleted",
            ),
            new_ids=("merged_line_id",),
            rules=(
                MeetAtAVoltageLevel("line_to_be_merged1_id", "line_to_be_merged2_id"),
            ),
            removes_orphan_switches=True,
        ),
    )
}


def operation_problems(operation: TopologyOperation) -> list[str]:
    """What, in `operation`, the installed pypowsybl no longer matches."""
    if operation.call is None:
        return [f"the installed pypowsybl has no call for {operation.operation}"]
    try:
        names = set(operation.schema.names)
    except Exception as error:  # noqa: BLE001 - any upstream change can surface here
        return [f"its pypowsybl metadata cannot be read: {error}"]
    referenced = {
        "required": operation.required,
        "defaults": tuple(operation.defaults),
        "units": tuple(operation.units),
        "connection points": operation.connection_points,
        "existing lines": operation.existing_lines,
        "new ids": operation.new_ids,
        "rules": tuple(name for item in operation.rules for name in item.fields),
    }
    return [
        f"{label} unknown to pypowsybl: {', '.join(sorted(set(values) - names))}"
        for label, values in referenced.items()
        if set(values) - names
    ]


# ---------------------------------------------------------------------------
# Applying an operation
# ---------------------------------------------------------------------------


def dangling_switches(network, voltage_level_id: str) -> set[str]:
    """Switches of a node/breaker voltage level with nothing at one end.

    Repeatedly takes away the switches ending at a node that holds no equipment
    and nothing else: what is left of a bay whose element is gone -- a breaker,
    then the disconnector behind it, down to the busbar.
    """
    levels = network.get_voltage_levels(all_attributes=True)
    if (
        voltage_level_id not in levels.index
        or levels.loc[voltage_level_id, "topology_kind"] != NODE_BREAKER
    ):
        return set()
    topology = network.get_node_breaker_topology(voltage_level_id)
    anchored = {
        node
        for node, connectable in topology.nodes["connectable_id"].items()
        if isinstance(connectable, str) and connectable
    }
    edges = {
        switch: (int(row["node1"]), int(row["node2"]))
        for switch, row in topology.switches.iterrows()
    }
    edges.update(
        {
            ("internal", index): (int(row["node1"]), int(row["node2"]))
            for index, row in topology.internal_connections.iterrows()
        }
    )
    dangling: set[str] = set()
    while True:
        degree: dict[int, int] = {}
        for first, second in edges.values():
            degree[first] = degree.get(first, 0) + 1
            degree[second] = degree.get(second, 0) + 1
        loose = [
            edge
            for edge, nodes in edges.items()
            if any(degree[node] == 1 and node not in anchored for node in nodes)
        ]
        if not loose:
            return dangling
        for edge in loose:
            del edges[edge]
            if isinstance(edge, str):
                dangling.add(edge)


def _real_containers(network) -> dict[str, str]:
    """The voltage levels and substations that are not fictitious, by id."""
    containers = {}
    for kind, frame in (
        ("voltage level", network.get_voltage_levels(all_attributes=True)),
        ("substation", network.get_substations(all_attributes=True)),
    ):
        containers.update(dict.fromkeys(frame.index[~frame["fictitious"]], kind))
    return containers


@dataclass(frozen=True)
class TopologyResult:
    operation: str
    methods: tuple[str, ...]
    created_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    switches_created: int
    switches_removed: int
    orphan_switches_removed: tuple[str, ...]
    notes: tuple[str, ...]
    message: str


class TopologyEngine:
    """Applies a topology operation after the checks pypowsybl does not make."""

    def __init__(self, operations: dict[str, TopologyOperation] | None = None):
        self.operations = OPERATIONS if operations is None else operations

    def operation_for(self, name: str) -> TopologyOperation:
        key = (name or "").strip().lower()
        operation = self.operations.get(key)
        if operation is not None:
            return operation
        close = difflib.get_close_matches(key, sorted(self.operations), n=3, cutoff=0.4)
        suggestion = (
            f" Did you mean {', '.join(repr(item) for item in close)}?" if close else ""
        )
        raise CreationError(
            f"No topology operation '{name}'.{suggestion}",
            hint={"operations": sorted(self.operations)},
        )

    def describe(self, name: str) -> dict[str, Any]:
        """The descriptor of one operation, as rejections carry it."""
        operation = self.operation_for(name)
        attributes = []
        for item in operation.schema.fields:
            entry: dict[str, Any] = {"name": item.name, "type": item.kind}
            if item.name in operation.units:
                entry["unit"] = operation.units[item.name]
            if item.name in operation.defaults:
                entry["default"] = operation.defaults[item.name]
            attributes.append(entry)
        return {
            "operation": operation.operation,
            "summary": operation.summary,
            "guidance": operation.guidance,
            "methods": [operation.call.name] if operation.call else [],
            "required": list(operation.required),
            "attributes": attributes,
            "checks": [item.describe() for item in operation.rules],
        }

    def apply(self, network, name: str, attributes: dict) -> TopologyResult:
        operation = self.operation_for(name)
        if not isinstance(attributes, dict):
            raise CreationError("'attributes' must be an object")
        schema = operation.schema
        given = {str(key).strip(): value for key, value in attributes.items()}

        known = set(schema.names)
        for key in given:
            if key not in known:
                close = difflib.get_close_matches(key, sorted(known), n=3, cutoff=0.5)
                suggestion = (
                    f" Did you mean {', '.join(repr(item) for item in close)}?"
                    if close
                    else ""
                )
                raise CreationError(
                    f"{operation.operation} has no attribute '{key}'.{suggestion}",
                    hint=self.describe(operation.operation),
                )
        for key, value in operation.defaults.items():
            given.setdefault(key, value)
        missing = [key for key in operation.required if given.get(key) is None]
        if missing:
            raise CreationError(
                f"{operation.operation} needs "
                + ", ".join(f"'{key}'" for key in missing),
                hint=self.describe(operation.operation),
            )
        try:
            for key, value in list(given.items()):
                if value is not None:
                    given[key] = _coerce(schema.field(key), value, operation.operation)
            self._check_network(operation, network, given)
            context = CreationContext(
                network=network,
                element_type=operation.operation,
                element_id="",
                attributes=given,
            )
            for item in operation.rules:
                item.apply(context)
        except CreationError as error:
            # every refusal carries the descriptor, so the call can be corrected
            if error.hint is None:
                error.hint = self.describe(operation.operation)
            raise

        watched = set()
        if operation.removes_orphan_switches:
            for key in operation.existing_lines:
                watched.update(_line_voltage_levels(network, given[key]))
        dangling_before = {
            level: dangling_switches(network, level) for level in watched
        }
        before = network.get_identifiables()["type"].astype(str)
        containers = _real_containers(network)

        getattr(pp.network, operation.call.function)(
            network, **{key: value for key, value in given.items() if value is not None}
        )

        orphans = sorted(
            switch
            for level in watched
            for switch in dangling_switches(network, level) - dangling_before[level]
        )
        if orphans:
            network.remove_elements(orphans)
        after = network.get_identifiables()["type"].astype(str)
        return self._result(operation, network, before, after, orphans, containers)

    def _check_network(self, operation, network, given) -> None:
        for key in operation.connection_points:
            if given.get(key) is not None:
                resolve_connection_point(network, str(given[key]))
        identifiables = network.get_identifiables()
        lines = network.get_lines()
        for key in operation.existing_lines:
            line_id = given.get(key)
            if line_id is None or line_id in lines.index:
                continue
            if line_id in identifiables.index:
                kind = str(identifiables.loc[line_id, "type"]).lower()
                raise CreationError(f"'{key}' names {kind} '{line_id}', not a line")
            raise CreationError(
                f"No line '{line_id}' ('{key}') in this network. Use "
                "get_network_element_data(element_type='line') to list them"
            )
        for key in operation.new_ids:
            new_id = given.get(key)
            if new_id is not None and new_id in identifiables.index:
                kind = str(identifiables.loc[new_id, "type"]).lower()
                raise CreationError(
                    f"'{key}' is '{new_id}', already used by a {kind} in this network; "
                    "choose another id"
                )

    def _result(
        self, operation, network, before, after, orphans, containers
    ) -> TopologyResult:
        created = sorted(set(after.index) - set(before.index))
        removed = sorted(set(before.index) - set(after.index))
        switches_created = [i for i in created if after[i] == "SWITCH"]
        switches_removed = [i for i in removed if before[i] == "SWITCH"]
        created_ids = tuple(i for i in created if after[i] != "SWITCH")
        removed_ids = tuple(i for i in removed if before[i] != "SWITCH")

        notes = []
        limits = network.get_operational_limits()
        for line_id in created_ids:
            if after[line_id] == "LINE" and line_id not in limits.index:
                notes.append(
                    f"line '{line_id}' has no operational limits, so it can never be "
                    "reported as overloaded: rate it with "
                    "create_network_element('operational_limits', ...)"
                )
        emptied = [i for i in removed_ids if i in containers]
        if emptied:
            # pypowsybl's reverts remove the voltage level they leave empty, and
            # its substation: a caller who built them for the cut may not expect it
            notes.append(
                "pypowsybl removed "
                + ", ".join(f"{containers[i]} '{i}'" for i in emptied)
                + ", left empty by the operation"
            )
        parts = []
        if created_ids:
            parts.append("created " + ", ".join(f"'{i}'" for i in created_ids))
        if removed_ids:
            parts.append("removed " + ", ".join(f"'{i}'" for i in removed_ids))
        if switches_created:
            parts.append(f"built {len(switches_created)} switch(es)")
        if orphans:
            parts.append(
                f"removed {len(orphans)} switch(es) left with nothing behind them"
            )
        message = f"{operation.operation}: " + ("; ".join(parts) or "nothing changed")
        return TopologyResult(
            operation=operation.operation,
            methods=(operation.call.name,),
            created_ids=created_ids,
            removed_ids=removed_ids,
            switches_created=len(switches_created),
            switches_removed=len(switches_removed),
            orphan_switches_removed=tuple(orphans),
            notes=tuple(notes),
            message=message,
        )
