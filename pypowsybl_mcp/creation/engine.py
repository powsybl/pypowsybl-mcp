#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""One creation pipeline, shared by every element type.

Whatever is being created, the same steps run in the same order::

    look the profile up
      -> normalise attribute names
      -> take the id out
      -> reject unknown or engine-managed attributes
      -> resolve the connection point(s), or the element being attached to
      -> check the required attributes and the enumerated values
      -> apply defaults, then derivations
      -> run the rules
      -> hand over to the executor
      -> report

Writing that once, instead of once per element type, is the point of the design:
a fix to the connection-point diagnostics, to the id check or to the report
applies to every element type at once, and a new element type is a profile
entry rather than a new implementation.

Errors carry the part of the descriptor that would have prevented them, so a
rejected call can correct itself without a second lookup -- the mechanism that
replaces the per-tool JSON schema of a one-tool-per-type surface.
"""

from __future__ import annotations

import difflib
from typing import Any

import pypowsybl as pp

from pypowsybl_mcp.creation.context import (
    NODE_BREAKER,
    ConnectionPoint,
    CreationContext,
    CreationError,
)
from pypowsybl_mcp.creation.doc_mining import attribute_docs, unit_in
from pypowsybl_mcp.creation.profiles import (
    PROFILES,
    Connection,
    CreationProfile,
)

#: everyday names accepted for the canonical pypowsybl attribute
ALIASES = {
    "bus": "bus_or_busbar_section_id",
    "bus_id": "bus_or_busbar_section_id",
    "busbar_section_id": "bus_or_busbar_section_id",
    "connection_point": "bus_or_busbar_section_id",
    "bus1": "bus_or_busbar_section_id_1",
    "bus2": "bus_or_busbar_section_id_2",
    "bus1_id": "bus_or_busbar_section_id_1",
    "bus2_id": "bus_or_busbar_section_id_2",
    "load_type": "type",
}

CONNECTION_ATTRIBUTE = {
    Connection.SINGLE: ("bus_or_busbar_section_id",),
    Connection.BUS: ("bus_or_busbar_section_id",),
    Connection.DOUBLE: ("bus_or_busbar_section_id_1", "bus_or_busbar_section_id_2"),
}

CONNECTION_HELP = (
    "A busbar section id in a node/breaker voltage level, or a bus of the "
    "bus/breaker view in a bus/breaker one. Not a bus of the bus view (the "
    "'VL1_0'-style ids load-flow results report)."
)


class CreationEngine:
    """Creates any element type from its profile and the introspected schema."""

    def __init__(self, profiles: dict[str, CreationProfile] | None = None):
        self.profiles = PROFILES if profiles is None else profiles

    # ------------------------------------------------------------------
    # Description
    # ------------------------------------------------------------------

    def profile_for(self, element_type: str) -> CreationProfile:
        name = (element_type or "").strip().lower()
        profile = self.profiles.get(name)
        if profile is None:
            supported = sorted(self.profiles)
            close = difflib.get_close_matches(name, supported, n=3, cutoff=0.4)
            suggestion = (
                f"Did you mean {', '.join(repr(item) for item in close)}? "
                if close
                else ""
            )
            raise CreationError(
                f"No element type '{element_type}' can be created. {suggestion}"
                "Call describe_element_creation() to list the supported types",
                hint={"supported_element_types": supported},
            )
        return profile

    def describe(self, element_type: str | None = None) -> dict:
        """The descriptor that replaces a per-tool JSON schema."""
        if not element_type:
            return {
                "element_types": [
                    {"element_type": name, "summary": profile.summary}
                    for name, profile in sorted(self.profiles.items())
                ],
                "usage": (
                    "create_network_element(element_type, attributes) creates one "
                    "element; create_network_elements(items) creates several in one "
                    "call, ordered so that containers come before what they contain. "
                    "Call describe_element_creation(element_type) for the attributes "
                    "of one type."
                ),
            }

        profile = self.profile_for(element_type)
        schema = profile.schema
        docs = attribute_docs(profile.docs)
        managed = set(profile.managed) | {profile.id_field}

        def field_entry(name: str, kind: str) -> dict:
            description = docs.get(name)
            entry: dict[str, Any] = {"name": name, "type": kind}
            unit = profile.units.get(name) or unit_in(description)
            if unit:
                entry["unit"] = unit
            if name in profile.enums:
                entry["values"] = list(profile.enums[name])
            if name in profile.defaults:
                entry["default"] = profile.defaults[name]
            if description:
                entry["description"] = description
            return entry

        derived = {derivation.produces for derivation in profile.derivations}
        required, optional = [], []
        for item in schema.fields:
            if item.name in managed or item.name == profile.rows_attribute:
                continue
            if item.group > 0 and profile.rows_attribute:
                # rows of the table attribute, described under it instead
                continue
            entry = field_entry(item.name, item.kind)
            if item.name in profile.required:
                required.append(entry)
            else:
                if item.name in derived:
                    entry["derived_when_missing"] = True
                optional.append(entry)

        for name, description in profile.virtual.items():
            entry: dict[str, Any] = {"name": name, "description": description}
            if name in profile.units:
                entry["unit"] = profile.units[name]
            if name in profile.defaults:
                entry["default"] = profile.defaults[name]
            if name in profile.enums:
                entry["values"] = list(profile.enums[name])
            (required if name in profile.required else optional).append(entry)

        descriptor: dict[str, Any] = {
            "element_type": profile.element_type,
            "summary": profile.summary,
            "created_by": ", ".join(profile.executor.calls),
            "how": profile.executor.describe(),
            "id_attribute": profile.id_field,
            "required": required,
            "optional": optional,
            "set_automatically": sorted(
                set(profile.managed) | derived - set(profile.required)
            ),
            "guidance": profile.guidance,
        }
        if profile.rows_attribute:
            descriptor["table_attribute"] = {
                "name": profile.rows_attribute,
                "columns": [
                    field_entry(item.name, item.kind)
                    for item in schema.group(1)
                    if item.name != schema.index_name
                ]
                or [
                    field_entry(item.name, item.kind)
                    for item in schema.fields
                    if item.name != schema.index_name and item.name not in managed
                ],
            }
        if profile.connection in CONNECTION_ATTRIBUTE:
            descriptor["connection"] = {
                "attributes": list(CONNECTION_ATTRIBUTE[profile.connection]),
                "help": CONNECTION_HELP,
            }
        elif profile.connection is Connection.TARGET:
            descriptor["attaches_to"] = {
                "id_attribute": profile.id_field,
                "element_types": sorted(profile.target_types),
            }
        if profile.derivations:
            descriptor["derived"] = [
                derivation.describe() for derivation in profile.derivations
            ]
        if profile.rules:
            descriptor["rules"] = [item.describe() for item in profile.rules]
        return descriptor

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create(self, network, element_type: str, attributes: dict) -> str:
        """Create one element and return the line to report for it."""
        profile = self.profile_for(element_type)
        given = self._normalise(profile, attributes)
        element_id = self._take_id(profile, given)
        self._reject_unknown(profile, given)

        context = CreationContext(
            network=network,
            element_type=profile.element_type,
            element_id=element_id,
            attributes=given,
        )
        self._resolve_connections(profile, context)
        self._check_required(profile, context)
        self._check_values(profile, context)

        for name, value in profile.defaults.items():
            context.attributes.setdefault(name, value)
        for derivation in profile.derivations:
            derivation.apply(context)
        for item in profile.rules:
            item.apply(context)

        payload = {
            name: value
            for name, value in context.attributes.items()
            if value is not None and name not in profile.virtual
        }
        profile.executor.execute(network, profile.element_type, element_id, payload)
        return self._report(profile, context, payload)

    def create_many(self, network, items: list[dict]) -> tuple[list[str], list[str]]:
        """Create several elements, ordered so dependencies come first.

        Every item is checked against its schema before anything is created, so
        a typo in the last one does not leave the first half applied. Checks
        that need the network -- an id already taken, a connection point that
        does not exist yet because an earlier item creates it -- can only run
        at the moment each element is created, so they still stop the batch
        where they fail, and the report says exactly how far it got.
        """
        prepared = []
        for position, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise CreationError(
                    f"Item {position} is not an object: each item is a dictionary "
                    "with 'element_type' and the attributes of that element"
                )
            payload = dict(item)
            element_type = payload.pop("element_type", None)
            if not element_type:
                raise CreationError(
                    f"Item {position} has no 'element_type': every item must name "
                    "the kind of element to create"
                )
            profile = self.profile_for(element_type)
            self._precheck(profile, payload, position)
            prepared.append((profile, payload, position))

        prepared.sort(key=lambda entry: (entry[0].order, entry[2]))

        done: list[str] = []
        failed: list[str] = []
        for profile, payload, position in prepared:
            try:
                done.append(self.create(network, profile.element_type, payload))
            except (CreationError, pp.PyPowsyblError, ValueError, KeyError) as error:
                failed.append(f"item {position} ({profile.element_type}): {error!s}")
                break
        return done, failed

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _normalise(self, profile: CreationProfile, attributes: dict) -> dict:
        if not isinstance(attributes, dict):
            raise CreationError(
                "'attributes' must be an object mapping attribute names to values"
            )
        schema_names = set(profile.schema.names)
        given: dict[str, Any] = {}
        for name, value in attributes.items():
            key = str(name).strip()
            if key not in schema_names and key in ALIASES:
                key = ALIASES[key]
            if key in ("id", "element_id") and key != profile.id_field:
                key = profile.id_field
            given[key] = value
        return given

    def _take_id(self, profile: CreationProfile, given: dict) -> str:
        element_id = given.pop(profile.id_field, None)
        if not element_id or not str(element_id).strip():
            what = (
                "the id of the element to attach to"
                if profile.connection is Connection.TARGET
                else "the id of the new element"
            )
            raise CreationError(
                f"'{profile.id_field}' is required: {what}",
                hint={"id_attribute": profile.id_field},
            )
        return str(element_id).strip()

    def _reject_unknown(self, profile: CreationProfile, given: dict) -> None:
        known = set(profile.schema.names) | set(profile.virtual)
        known.update(CONNECTION_ATTRIBUTE.get(profile.connection, ()))
        if profile.rows_attribute:
            known.add(profile.rows_attribute)
        managed = set(profile.managed)
        for name in given:
            if name in managed:
                raise CreationError(
                    f"'{name}' is set automatically for a {profile.element_type} and "
                    "must not be passed",
                    hint={"set_automatically": sorted(managed)},
                )
            if name not in known:
                close = difflib.get_close_matches(name, sorted(known), n=3, cutoff=0.5)
                suggestion = (
                    f" Did you mean {', '.join(repr(item) for item in close)}?"
                    if close
                    else ""
                )
                raise CreationError(
                    f"A {profile.element_type} has no attribute '{name}'.{suggestion}",
                    hint={"accepted_attributes": sorted(known - managed)},
                )

    def _resolve_connections(
        self, profile: CreationProfile, context: CreationContext
    ) -> None:
        if profile.connection is Connection.TARGET:
            context.target_type = self._check_target(profile, context)
            return

        names = CONNECTION_ATTRIBUTE.get(profile.connection)
        if not names:
            self._check_id_available(context)
            return

        points = []
        for name in names:
            value = context.attributes.get(name)
            if not value:
                raise CreationError(
                    f"'{name}' is required to connect a {profile.element_type}. "
                    + CONNECTION_HELP,
                    hint={"connection_attributes": list(names)},
                )
            points.append(resolve_connection_point(context.network, str(value)))
        context.connections = tuple(points)

        if profile.connection is Connection.BUS:
            # A virtual input for these types: pypowsybl takes the voltage level
            # and the bus separately, which the derivations fill in.
            for name in names:
                context.attributes.pop(name, None)
        self._check_id_available(context)

    def _check_target(self, profile: CreationProfile, context: CreationContext) -> str:
        identifiables = context.network.get_identifiables()
        if context.element_id not in identifiables.index:
            raise CreationError(
                f"Element '{context.element_id}' not found in this network. Use "
                "get_network_element_data() to list the existing ids"
            )
        target_type = str(identifiables.loc[context.element_id, "type"]).lower()
        if profile.target_types and target_type not in profile.target_types:
            raise CreationError(
                f"{profile.element_type} cannot be attached to a {target_type} "
                f"('{context.element_id}'). Accepted element types: "
                + ", ".join(sorted(profile.target_types)),
                hint={"accepted_target_types": sorted(profile.target_types)},
            )
        return target_type

    def _check_id_available(self, context: CreationContext) -> None:
        identifiables = context.network.get_identifiables()
        if context.element_id in identifiables.index:
            existing = str(identifiables.loc[context.element_id, "type"]).lower()
            raise CreationError(
                f"Id '{context.element_id}' is already used by a {existing} in this "
                "network; choose another id"
            )

    def _check_required(
        self, profile: CreationProfile, context: CreationContext
    ) -> None:
        missing = [
            name
            for name in profile.required
            if context.attributes.get(name) is None and name not in profile.defaults
        ]
        if missing:
            raise CreationError(
                f"A {profile.element_type} needs "
                + ", ".join(f"'{name}'" for name in missing)
                + (
                    f" ({', '.join(self._with_unit(profile, name) for name in missing)})"
                    if any(profile.units.get(name) for name in missing)
                    else ""
                ),
                hint=self.describe(profile.element_type),
            )

    def _with_unit(self, profile: CreationProfile, name: str) -> str:
        unit = profile.units.get(name)
        return f"{name} in {unit}" if unit else name

    def _check_values(self, profile: CreationProfile, context: CreationContext) -> None:
        schema = profile.schema
        for name, value in list(context.attributes.items()):
            if value is None:
                continue
            legal = profile.enums.get(name)
            if legal:
                normalised = str(value).strip().upper()
                if normalised not in legal:
                    raise CreationError(
                        f"'{normalised}' is not a valid {name} for a "
                        f"{profile.element_type}. Accepted values: " + ", ".join(legal),
                        hint={name: list(legal)},
                    )
                context.attributes[name] = normalised
                continue
            item = schema.field(name)
            if item is None or name == profile.rows_attribute:
                continue
            context.attributes[name] = _coerce(item, value, profile.element_type)

    def _report(
        self, profile: CreationProfile, context: CreationContext, payload: dict
    ) -> str:
        shown = []
        for name in profile.required:
            if name in payload:
                unit = profile.units.get(name, "")
                shown.append(f"{name}={payload[name]}{' ' + unit if unit else ''}")
        details = f" ({', '.join(shown)})" if shown else ""

        if profile.connection is Connection.TARGET:
            created = (
                f"set {profile.element_type} on {context.target_type} "
                f"'{context.element_id}'"
            )
            where = ""
        else:
            created = f"created {profile.element_type} '{context.element_id}'"
            points = [
                f"'{point.id}' in {point.voltage_level_id}"
                for point in context.connections
            ]
            if len(points) > 1:
                where = " between " + " and ".join(points)
            elif points:
                where = f" connected to {points[0]}"
            else:
                where = ""

        extra = profile.executor.report_extra(
            context.network, profile.element_type, context.element_id
        )
        notes = "".join(f". Note: {note}" for note in context.notes)
        return f"{created}{details}{where}{extra or ''}{notes}"

    def _precheck(self, profile: CreationProfile, payload: dict, position: int) -> None:
        """Schema-level checks that need no network, run before anything is created."""
        given = self._normalise(profile, payload)
        given.pop(profile.id_field, None)
        try:
            self._reject_unknown(profile, given)
        except CreationError as error:
            raise CreationError(f"item {position}: {error!s}", error.hint) from error
        if profile.id_field not in self._normalise(profile, payload):
            raise CreationError(
                f"item {position}: '{profile.id_field}' is required for a "
                f"{profile.element_type}"
            )


def _coerce(item, value, element_type: str):
    """Accept the loose types an MCP client may send, reject what cannot convert."""
    try:
        if item.kind == "number":
            return float(value)
        if item.kind == "integer":
            return int(value)
        if item.kind == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "yes", "1")
            return bool(value)
    except (TypeError, ValueError) as error:
        raise CreationError(
            f"'{item.name}' of a {element_type} must be a {item.kind}, got {value!r}"
        ) from error
    return value


def resolve_connection_point(network, point_id: str) -> ConnectionPoint:
    """Validate a bus or busbar section id and describe where it lives.

    pypowsybl only ever answers "Bus or busbar section X not found", which is of
    no help to a caller choosing ids. The three ways to get this wrong each get
    their own message, naming the ids that would have worked:

    * a *bus view* bus (``VL1_0``), which load-flow results report but which is
      computed from the topology and can host nothing;
    * a bus/breaker view bus of a node/breaker voltage level, where a bay has to
      be attached to a busbar section instead;
    * an id that is simply not there.
    """
    if not point_id or not point_id.strip():
        raise CreationError("A bus or busbar section id is required")

    busbar_sections = network.get_busbar_sections()
    voltage_levels = network.get_voltage_levels(all_attributes=True)

    def level(voltage_level_id: str) -> tuple[str, float, str | None]:
        row = voltage_levels.loc[voltage_level_id]
        return (
            str(row["topology_kind"]),
            float(row["nominal_v"]),
            str(row["substation_id"]) if row["substation_id"] else None,
        )

    if point_id in busbar_sections.index:
        voltage_level_id = str(busbar_sections.loc[point_id, "voltage_level_id"])
        kind, nominal_v, substation = level(voltage_level_id)
        return ConnectionPoint(point_id, voltage_level_id, substation, kind, nominal_v)

    configured = network.get_bus_breaker_view_buses()
    if point_id in configured.index:
        voltage_level_id = str(configured.loc[point_id, "voltage_level_id"])
        kind, nominal_v, substation = level(voltage_level_id)
        if kind == NODE_BREAKER:
            sections = busbar_sections[
                busbar_sections["voltage_level_id"] == voltage_level_id
            ].index.tolist()
            raise CreationError(
                f"'{point_id}' is a bus of the bus/breaker view of voltage level "
                f"'{voltage_level_id}', which is in {NODE_BREAKER} topology: an "
                "element must be connected to a busbar section there. Busbar "
                f"sections available: {', '.join(sections) or 'none'}"
            )
        return ConnectionPoint(point_id, voltage_level_id, substation, kind, nominal_v)

    bus_view = network.get_buses()
    if point_id in bus_view.index:
        voltage_level_id = str(bus_view.loc[point_id, "voltage_level_id"])
        candidates = _connection_points_of(network, voltage_level_id, voltage_levels)
        raise CreationError(
            f"'{point_id}' is a bus of the bus view (computed from the topology of "
            f"voltage level '{voltage_level_id}'), so no element can be attached to "
            f"it. Connect to one of these instead: {', '.join(candidates) or 'none'}"
        )

    raise CreationError(
        f"Bus or busbar section '{point_id}' not found in this network. Use "
        "get_network_element_data(element_type='busbar_section') for node/breaker "
        "voltage levels, or "
        "get_network_element_data(element_type='bus_from_bus_breaker_view') for "
        "bus/breaker ones"
    )


def _connection_points_of(network, voltage_level_id: str, voltage_levels) -> list[str]:
    if str(voltage_levels.loc[voltage_level_id, "topology_kind"]) == NODE_BREAKER:
        sections = network.get_busbar_sections()
        return sections[sections["voltage_level_id"] == voltage_level_id].index.tolist()
    buses = network.get_bus_breaker_view_buses()
    return buses[buses["voltage_level_id"] == voltage_level_id].index.tolist()
