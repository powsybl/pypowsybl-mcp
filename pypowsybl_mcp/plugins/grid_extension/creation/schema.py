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
perfectly well-formed field list), so the field lists are always read for a
call observed to exist: ``CREATE_CALLS`` and ``BAY_CALLS``, built below from the
calls pypowsybl actually makes.
"""

from __future__ import annotations

import contextlib
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

import pypowsybl.network as pn
from loguru import logger
from pypowsybl import _pypowsybl as _pp
from pypowsybl.network import Network, create_empty

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


@dataclass(frozen=True)
class CreationCall:
    """A pypowsybl call that creates elements, as observed on the installed version."""

    #: ``Network`` for a method of the network, ``pp.network`` for a function
    #: that takes the network as first argument (the bay helpers)
    owner: str
    function: str
    #: its DataFrame parameters, in the order of the metadata groups they fill
    frames: tuple[str, ...]
    #: for a bay helper, the NetworkModificationType its metadata comes from
    modification: str | None = None

    @property
    def name(self) -> str:
        return f"{self.owner}.{self.function}"


def _dataframe_parameters(function) -> tuple[str, ...]:
    """The DataFrame parameters of a call, in signature order.

    pypowsybl uses postponed annotations, so they read as strings; the order
    matches the groups of the call's metadata (the shunt, then its linear and
    non-linear sections; a tap changer, then its steps).
    """
    return tuple(
        parameter.name
        for parameter in inspect.signature(function).parameters.values()
        if "DataFrame" in str(parameter.annotation)
    )


def _required_count(function) -> int:
    return sum(
        1
        for parameter in inspect.signature(function).parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    )


def _observe(
    target, attribute: str, calls: dict[str, Callable], leading: tuple = ()
) -> dict[str, tuple]:
    """Call name -> the arguments it passed to ``target.attribute``.

    Each call is made once, with placeholder arguments, while that attribute is
    replaced by a spy that records its arguments and stops before the C layer
    -- the trick ``utils/element_types.py`` uses for the getters. It runs at
    import, before the server handles any request: the spy patches pypowsybl
    for the whole process while it is in place.
    """
    original = getattr(target, attribute, None)
    if original is None:
        # A private hook, so it may go in an upgrade. Without it the calls it
        # would reveal are unknown; the startup check reports what went.
        logger.error(
            f"{getattr(target, '__name__', target)}.{attribute} not found in this "
            "pypowsybl: the element types it would reveal cannot be created"
        )
        return {}

    captured: dict[str, tuple] = {}

    def spy(*args, **kwargs):
        captured["arguments"] = args
        raise RuntimeError("stopped before the C layer")

    seen: dict[str, tuple] = {}
    setattr(target, attribute, spy)
    try:
        for name, call in calls.items():
            captured.clear()
            placeholders = [None] * (_required_count(call) - len(leading))
            # The call always raises: from the spy, or from pypowsybl before it.
            with contextlib.suppress(Exception):
                call(*leading, *placeholders)
            if "arguments" in captured:
                seen[name] = captured["arguments"]
    finally:
        setattr(target, attribute, original)
    return seen


def _deprecated(call) -> bool:
    return ".. deprecated::" in (inspect.getdoc(call) or "")


def _create_calls() -> dict[str, CreationCall]:
    """Element type -> the ``Network.create_*`` method that creates it.

    Every such method funnels into ``Network._create_elements(element_type,
    ...)``, which the spy intercepts. ``create_extensions`` is left out: it
    attaches data to existing elements of any type.
    """
    network = create_empty()
    methods = {
        name: getattr(network, name)
        for name in sorted(dir(Network))
        if name.startswith("create_")
        and name != "create_extensions"
        and not _deprecated(getattr(Network, name))
    }
    seen = _observe(Network, "_create_elements", methods)
    return {
        arguments[1].name.lower(): CreationCall(
            "Network", name, _dataframe_parameters(getattr(Network, name))
        )
        for name, arguments in seen.items()
    }


def _bay_calls() -> dict[str, CreationCall]:
    """Element type -> the bay helper that creates it with its switchgear.

    The feeder bays (``create_load_bay``, ...) and the branch bays
    (``create_line_bays``, ...) all start by reading their metadata with
    ``get_network_modification_metadata_with_element_type(modification,
    element_type)``, which the spy intercepts: it gives both the element type
    and the modification whose metadata describes the call's attributes.
    """
    network = create_empty()
    functions = {
        name: getattr(pn, name)
        for name in sorted(dir(pn))
        if name.startswith("create_")
        and name.endswith(("_bay", "_bays"))
        and not _deprecated(getattr(pn, name))
    }
    seen = _observe(
        _pp,
        "get_network_modification_metadata_with_element_type",
        functions,
        leading=(network,),
    )
    return {
        element_type.name.lower(): CreationCall(
            "pp.network",
            name,
            _dataframe_parameters(functions[name]),
            modification=modification.name,
        )
        for name, (modification, element_type) in seen.items()
    }


def _topology_calls() -> dict[str, CreationCall]:
    """Topology operation -> the helper that performs it.

    The operations changing how existing elements are connected
    (``create_coupling_device``, ``connect_voltage_level_on_line``, ...) all
    start by reading their metadata with
    ``get_network_modification_metadata(modification)``, which the spy
    intercepts. An operation is named after its modification, lowercased --
    pypowsybl's own vocabulary. ``VOLTAGE_LEVEL_TOPOLOGY_CREATION`` is left out:
    it is part of creating a voltage level.
    """
    network = create_empty()
    functions = {
        name: getattr(pn, name)
        for name in sorted(dir(pn))
        if re.match(r"(create|connect|replace|revert)_", name)
        and not name.endswith(("_bay", "_bays"))
        and callable(getattr(pn, name))
        and list(inspect.signature(getattr(pn, name)).parameters)[:1] == ["network"]
        and not _deprecated(getattr(pn, name))
    }
    seen = _observe(
        _pp, "get_network_modification_metadata", functions, leading=(network,)
    )
    return {
        arguments[0].name.lower(): CreationCall(
            "pp.network",
            name,
            _dataframe_parameters(functions[name]),
            modification=arguments[0].name,
        )
        for name, arguments in seen.items()
        if arguments[0].name != "VOLTAGE_LEVEL_TOPOLOGY_CREATION"
    }


#: element type -> its ``Network.create_*`` call, observed on the installed version
CREATE_CALLS: dict[str, CreationCall] = _create_calls()
#: element type -> its bay helper (``pp.network.create_*_bay(s)``), observed likewise
BAY_CALLS: dict[str, CreationCall] = _bay_calls()
#: topology operation -> its helper (``pp.network.connect_voltage_level_on_line``...)
TOPOLOGY_CALLS: dict[str, CreationCall] = _topology_calls()


@cache
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
        except (_pp.PyPowsyblError, AttributeError, TypeError, ValueError):
            continue
        if groups and any(len(group) for group in groups):
            names.append(member.lower())
    return tuple(sorted(names))
