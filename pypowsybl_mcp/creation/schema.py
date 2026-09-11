#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The field list of a creation call, read from pypowsybl itself.

``pypowsybl`` exposes the attributes of every creation API through two C-API
calls, and both follow the installed version, so nothing in this module is
hand-written:

``get_network_elements_creation_dataframes_metadata(element_type)``
    the attributes of ``Network.create_<x>()``.

``get_network_modification_metadata_with_element_type(modification, element_type)``
    the attributes of the network-modification helpers -- in particular the
    ``create_<x>_bay()`` family, which is what the tools actually call so that
    the switching equipment required by the hosting topology gets built.

Both return *groups* of series, one per dataframe the call expects: a shunt
compensator comes back as three (the shunt, the linear model, the non-linear
model), a tap changer as two (the changer and its steps).

The metadata is descriptive, not authoritative: it says nothing about which
fields are required, and it will happily describe combinations that make no
sense (asking for the feeder-bay attributes of a *substation* returns a
perfectly well-formed field list). Deciding which call is legal for which
element type is the job of :mod:`pypowsybl_mcp.creation.profiles`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from pypowsybl import _pypowsybl as _pp

# SeriesMetadata.type is an int code; these are the four it can take.
KIND_BY_CODE = {0: "string", 1: "number", 2: "integer", 3: "boolean"}
PYTHON_TYPE_BY_KIND = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
}


@dataclass(frozen=True)
class Field:
    """One attribute of a creation call."""

    name: str
    kind: str
    is_index: bool
    group: int

    @property
    def python_type(self):
        return PYTHON_TYPE_BY_KIND[self.kind]


@dataclass(frozen=True)
class ElementSchema:
    """The fields of one creation call, in dataframe groups."""

    fields: tuple[Field, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    @property
    def index_name(self) -> str:
        """Name of the index field, i.e. the id the call is keyed on."""
        for field in self.fields:
            if field.is_index:
                return field.name
        # Every creation metadata seen so far has one; fall back to the first.
        return self.fields[0].name if self.fields else "id"

    def field(self, name: str) -> Field | None:
        for field in self.fields:
            if field.name == name:
                return field
        return None

    def group(self, index: int) -> tuple[Field, ...]:
        return tuple(field for field in self.fields if field.group == index)

    @property
    def group_count(self) -> int:
        return 1 + max((field.group for field in self.fields), default=0)


def _element_type(name: str) -> _pp.ElementType:
    """Resolve a public element-type name to the pypowsybl enum member.

    Public names are ``ElementType`` members lowercased, the convention already
    used by ``get_network_element_data`` and the ``element-types`` skill.
    """
    try:
        return getattr(_pp.ElementType, name.upper())
    except AttributeError as error:
        raise KeyError(f"Unknown element type '{name}'") from error


def _schema_from_groups(groups) -> ElementSchema:
    fields: list[Field] = []
    seen: set[str] = set()
    for group_index, group in enumerate(groups):
        for series in group:
            # A field repeated across groups (the id, which keys every
            # dataframe) is kept once, on the group where it first appears.
            if series.name in seen:
                continue
            seen.add(series.name)
            fields.append(
                Field(
                    name=series.name,
                    kind=KIND_BY_CODE.get(series.type, "string"),
                    is_index=bool(series.is_index),
                    group=group_index,
                )
            )
    return ElementSchema(fields=tuple(fields))


@cache
def creation_schema(element_type: str) -> ElementSchema:
    """Fields of ``Network.create_<element_type>s()``."""
    groups = _pp.get_network_elements_creation_dataframes_metadata(
        _element_type(element_type)
    )
    return _schema_from_groups(groups)


@cache
def modification_schema(element_type: str, modification_name: str) -> ElementSchema:
    """Fields of a network-modification helper, e.g. ``create_load_bay()``.

    ``modification_name`` is a ``NetworkModificationType`` member name such as
    ``CREATE_FEEDER_BAY`` or ``CREATE_LINE_FEEDER``.
    """
    modification = getattr(_pp.NetworkModificationType, modification_name)
    groups = _pp.get_network_modification_metadata_with_element_type(
        modification, _element_type(element_type)
    )
    return _schema_from_groups(groups)


@cache
def plain_modification_schema(modification_name: str) -> ElementSchema:
    """Fields of a modification that takes no element type, e.g. topology creation."""
    modification = getattr(_pp.NetworkModificationType, modification_name)
    return _schema_from_groups([_pp.get_network_modification_metadata(modification)])


def creatable_element_types() -> tuple[str, ...]:
    """Element types for which pypowsybl exposes creation metadata.

    The upper bound of what any generic creation tool could reach on the
    installed version. Which of them the server actually supports is decided by
    the profiles, not here.
    """
    names = []
    for member in dir(_pp.ElementType):
        if not member.isupper():
            continue
        try:
            groups = _pp.get_network_elements_creation_dataframes_metadata(
                getattr(_pp.ElementType, member)
            )
        except (_pp.PyPowsyblError, TypeError, ValueError):  # pragma: no cover
            continue
        if groups and any(len(group) for group in groups):
            names.append(member.lower())
    return tuple(sorted(names))
