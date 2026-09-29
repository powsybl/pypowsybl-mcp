#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Per-attribute documentation, mined from the pypowsybl docstrings.

``pypowsybl`` documents creation attributes in its docstrings, in a regular
format::

    Valid attributes are:

    - **id**: the identifier of the new load
    - **p0**: active power load, in MW
    - **q0**: reactive power load, in MVar

This is the same text ``get_online_resource`` serves: the readthedocs pages are
Sphinx autodoc output of these docstrings. We read them locally with
``inspect.getdoc`` instead of fetching them, because that is offline and, more
importantly, it is the *installed* version -- the online documentation tracks
``latest``, which may not be the pinned one.

What mining is good for, measured over 270 creation fields on pypowsybl 1.15:

===============================  ========  ====================================
Extracted                        Coverage  Use
===============================  ========  ====================================
description                      87%       shown by ``describe_element_creation``
unit ("in MW", "in kV", ...)     26%       completes the units of the profile
marked "optional"                14%       unusable as a required/optional signal
enumerated values                3%        unusable, truncated at the source
                                           (``energy_source`` is documented as
                                           ``(HYDRO, NUCLEAR, ...)``)
===============================  ========  ====================================

So this module is **informational only**: it fills in descriptions and
completes units. Required-ness, enum values and rules stay hand-written in the
profiles, where they can be checked. A bay helper documents only the attributes
it adds and points at the underlying ``Network.create_x`` for the rest, so a
profile lists several functions and their bullets are merged.
"""

from __future__ import annotations

import inspect
import re
from functools import cache

# "    - **p0**: active power load, in MW"
_BULLET = re.compile(r"^\s*-\s*\*\*(?P<name>[A-Za-z0-9_]+)\*\*\s*:\s*(?P<text>.*)$")
_CONTINUATION = re.compile(r"^\s{4,}\S")
_UNIT = re.compile(
    r"\bin (MW|MVar|MVAr|kV|MVA|A|S|Ohm|ohm|degrees|seconds|percent|per unit)\b"
)
_UNIT_LABELS = {"MVar": "MVAr", "ohm": "Ohm"}


def _bullets(doc: str | None) -> dict[str, str]:
    """Attribute name -> description, from one docstring."""
    found: dict[str, str] = {}
    current: str | None = None
    for line in (doc or "").splitlines():
        match = _BULLET.match(line)
        if match:
            current = match.group("name")
            found[current] = match.group("text").strip()
        elif current and line.strip() and _CONTINUATION.match(line):
            found[current] = f"{found[current]} {line.strip()}"
        elif not line.strip():
            current = None
    return found


@cache
def attribute_docs(functions: tuple) -> dict[str, str]:
    """Merged attribute descriptions of several pypowsybl callables.

    Earlier functions win, so a bay helper's own wording takes precedence over
    the underlying dataframe API it refers to.
    """
    merged: dict[str, str] = {}
    for function in functions:
        if function is None:
            continue
        for name, text in _bullets(inspect.getdoc(function)).items():
            merged.setdefault(name, text)
    return merged


def unit_in(description: str | None) -> str | None:
    """The unit stated in a description, if it states one."""
    if not description:
        return None
    match = _UNIT.search(description)
    if not match:
        return None
    unit = match.group(1)
    return _UNIT_LABELS.get(unit, unit)
