#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Canonical mapping from public element-type names to pypowsybl getters.

Several tools accept an ``element_type`` string from the MCP client and turn it
into a call on the network object. That mapping is *derived* from the pypowsybl
``Network`` class instead of being written by hand: every getter that returns a
whole element table becomes one supported element type, named exactly like the
getter minus its ``get_`` prefix (``get_2_windings_transformers`` ->
``2_windings_transformers``).

Deriving it gives us two things a handwritten dict could not:

* No human-invented names. ``"transformers"`` used to be accepted as a synonym
  of ``"2_windings_transformers"`` (and ``"svc"`` of
  ``"static_var_compensators"``); those names exist nowhere in pypowsybl, yet
  every consumer had to special-case them. Everyday wording is now a
  documentation concern, handled by the ``element-types`` skill, and unknown
  names come back with a "did you mean" hint (see
  :func:`element_type_hint`).
* Exhaustiveness that follows the installed pypowsybl version: element types
  added upstream (or renamed, as ``dangling_lines`` -> ``boundary_lines``) show
  up without a code change here.

Consumers that only accept a subset should derive their allow-list from this
dict rather than redefining the getter names.
"""

from __future__ import annotations

import difflib
import inspect
from collections.abc import Iterable

from pypowsybl.network import Network


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


def _table_getters() -> dict[str, str]:
    """Public element-type name -> name of the Network getter returning its table."""
    getters: dict[str, str] = {}
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
        # replacement is already in the mapping under its own name.
        if ".. deprecated::" in (inspect.getdoc(getter) or ""):
            continue
        getters[name.removeprefix("get_")] = name
    return getters


ELEMENT_TYPE_TO_GETTER: dict[str, str] = _table_getters()


def element_type_hint(
    element_type: str | None, supported: Iterable[str] | None = None
) -> str:
    """Build the tail of an "invalid element type" message.

    Lists the accepted names and, when the caller used an everyday word instead
    of a canonical one ("transformers" for "2_windings_transformers"), points at
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
