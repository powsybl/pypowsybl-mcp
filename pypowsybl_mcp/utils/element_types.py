#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Canonical mapping from public element-type names to pypowsybl getters.

Several tools accept an ``element_type`` string from the MCP client and turn it
into a call on the network object. That mapping is *derived* from pypowsybl
instead of being written by hand: every getter that returns a whole element
table is associated with the :class:`pypowsybl._pypowsybl.ElementType` it
resolves to, and the public name is that enum member lowercased
(``ElementType.TWO_WINDINGS_TRANSFORMER`` -> ``"two_windings_transformer"``,
whose getter is ``get_2_windings_transformers``).

Keying on the enum name rather than the getter name gives us three things:

* A direct, bidirectional link to ``ElementType``. Any consumer recovers the
  enum with ``getattr(ElementType, name.upper())`` (see :func:`element_type_enum`),
  so the same string drives both the dataframe getter and enum-based APIs such
  as ``Network.get_elements_ids`` -- no second hand-maintained lookup.
* No human-invented names. ``"transformers"`` and ``"svc"`` exist nowhere in
  pypowsybl; everyday wording is a documentation concern (the ``element-types``
  skill) and unknown names come back with a "did you mean" hint (see
  :func:`element_type_hint`).
* Exhaustiveness that follows the installed pypowsybl version: element types
  added or renamed upstream show up without a code change here.

The getter a plain ``method()`` call dispatches to is *observed at runtime*, not
parsed from source: a getter may route to different ElementType values
depending on its own defaults (``get_operational_limits`` ->
``SELECTED_OPERATIONAL_LIMITS`` unless ``show_inactive_sets=True``), so only
calling it with defaults reveals which enum a bare call resolves to.

Consumers that only accept a subset should derive their allow-list from this
dict rather than redefining the names.
"""

from __future__ import annotations

import difflib
import inspect
from collections.abc import Iterable

from pypowsybl import _pypowsybl as _pp
from pypowsybl.network import Network, create_empty


def _returns_a_table(signature: inspect.Signature) -> bool:
    """Tell whether a getter hands back a whole dataframe of elements.

    pypowsybl uses postponed annotations, so the return annotation reaches us
    as the string ``"DataFrame"`` rather than as the pandas class.
    """
    return "DataFrame" in str(signature.return_annotation)


def _needs_arguments(signature: inspect.Signature) -> bool:
    """Tell whether a getter needs an argument, e.g. get_elements(element_type).

    Those are not element types of their own: they either take the type as a
    parameter or address a single container (get_single_line_diagram, ...).
    """
    return any(
        parameter.name != "self"
        and parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for parameter in signature.parameters.values()
    )


def _table_getter_names() -> list[str]:
    """Names of the Network getters that return a whole element table with no args."""
    names: list[str] = []
    for name in dir(Network):
        if not name.startswith("get_"):
            continue
        getter = getattr(Network, name, None)
        if not callable(getter):
            continue
        try:
            signature = inspect.signature(getter)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        if not _returns_a_table(signature) or _needs_arguments(signature):
            continue
        # Deprecated getters are kept upstream as thin wrappers that warn; the
        # replacement is already exposed under its own name.
        if ".. deprecated::" in (inspect.getdoc(getter) or ""):
            continue
        names.append(name)
    return names


def _element_type_to_getter() -> dict[str, str]:
    """Lowercased ElementType name -> the get_* method returning its table.

    Every table getter funnels through ``Network.get_elements(element_type, ...)``,
    so we temporarily spy on it and call each getter with its defaults to observe
    which ``ElementType`` a bare call resolves to. The public key is that enum
    member's name lowercased.
    """
    captured: dict[str, _pp.ElementType] = {}
    original = Network.get_elements

    def spy(self, element_type, *args, **kwargs):
        captured["type"] = element_type
        return original(self, element_type, *args, **kwargs)

    network = create_empty()
    mapping: dict[str, str] = {}
    Network.get_elements = spy
    try:
        for getter_name in _table_getter_names():
            captured.pop("type", None)
            try:
                getattr(network, getter_name)()
            except (TypeError, ValueError, _pp.PyPowsyblError):  # pragma: no cover
                continue
            element_type = captured.get("type")
            if element_type is not None:
                mapping[element_type.name.lower()] = getter_name
    finally:
        Network.get_elements = original
    return mapping


ELEMENT_TYPE_TO_GETTER: dict[str, str] = _element_type_to_getter()


def element_type_enum(element_type: str) -> _pp.ElementType:
    """Return the ElementType enum member for a public element-type name.

    Keys of :data:`ELEMENT_TYPE_TO_GETTER` are ElementType names lowercased, so
    the enum is recovered directly. Raises KeyError-like AttributeError for an
    unknown name; callers should validate against ELEMENT_TYPE_TO_GETTER first.
    """
    return getattr(_pp.ElementType, element_type.upper())


def element_type_hint(
    element_type: str | None, supported: Iterable[str] | None = None
) -> str:
    """Build the tail of an "invalid element type" message.

    Lists the accepted names and, when the caller used an everyday word instead
    of a canonical one ("transformer" for "two_windings_transformer"), points at
    the closest matches so the model can retry without guessing.
    """
    candidates = (
        list(supported) if supported is not None else list(ELEMENT_TYPE_TO_GETTER)
    )
    hint = f"Supported types: {', '.join(candidates)}"
    close = difflib.get_close_matches(element_type or "", candidates, n=3, cutoff=0.5)
    if close:
        suggestions = ", ".join(repr(name) for name in close)
        hint = f"Did you mean one of {suggestions}? {hint}"
    return hint
