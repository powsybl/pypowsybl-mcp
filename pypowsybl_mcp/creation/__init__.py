#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Introspection-driven creation of network elements.

One generic MCP tool creates any kind of element instead of one tool per type.
That is possible because ``pypowsybl`` describes its own creation API, and it is
*usable* because a thin declarative overlay supplies what the description leaves
out. The package is layered accordingly:

``schema``
    What ``pypowsybl`` tells us: the field names, their types and how they are
    grouped into dataframes, read from the C-API metadata. Nothing here is
    hand-written, so this layer follows the installed pypowsybl version.

``doc_mining``
    What ``pypowsybl`` documents: the per-attribute descriptions (and the units
    stated in them) parsed out of the docstrings of the very functions the
    executors call. Informational only -- it never changes behaviour.

``rules`` / ``derivations``
    The semantics metadata cannot carry: conditional requirements, orderings,
    and the values the engine computes on the caller's behalf.

``profiles``
    The overlay proper: for each element type, which pypowsybl call to use,
    which fields are required, which are managed by the engine, the legal enum
    values, the rules and a paragraph of guidance. ~20 lines per element type,
    and the only part that has to be maintained by hand.

``engine``
    One pipeline shared by every element type: normalise, validate, resolve the
    connection points, apply defaults and derivations, run the rules, dispatch
    to the executor, report.

See ``docs/generic_element_creation_design.md`` for the reasoning, and
``tests/creation/test_profiles_consistency.py`` for the check that keeps the
overlay honest against the installed pypowsybl.
"""
