#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Read IIDM extensions: the data a network file carries beside the core model.

An extension is a table indexed by element id (e.g. `detail` gives each load its
fixed / variable part, `entsoeCategory` each generator its ENTSO-E category).
This module reads them and joins them onto an element table, for the `attach`
argument of get_network_element_data and for the extension tools.
"""

from dataclasses import dataclass, field
from functools import lru_cache

import pandas as pd
import pypowsybl as pp
from loguru import logger

# attach token naming any extension: "extension:<name>" joins its columns as
# "<name>_<column>".
EXTENSION_TOKEN_PREFIX = "extension:"


@dataclass(frozen=True)
class ExtensionAlias:
    """A named attach token for an extension every study needs."""

    extension: str
    element_type: str
    # Column renames; a column not listed keeps its name.
    rename: dict[str, str] = field(default_factory=dict)


EXTENSION_ATTACH_ALIASES = {
    # Fixed / variable (thermosensitive, "affine") part of each load, in MW / MVar.
    "load_detail": ExtensionAlias("detail", "load"),
    # ENTSO-E category code of each generator (nuclear, PV, offshore wind...), as
    # set by the EntsoeCategory import post-processor.
    "entsoe_category": ExtensionAlias(
        "entsoeCategory", "generator", {"code": "entsoe_category_code"}
    ),
}


@dataclass(frozen=True)
class ExtensionJoin:
    """One extension to join onto an element table."""

    extension: str
    rename: dict[str, str] = field(default_factory=dict)
    prefix: str = ""


@lru_cache(maxsize=1)
def extension_names() -> tuple[str, ...]:
    """Every extension name this pypowsybl build can read."""
    return tuple(pp.network.get_extensions_names())


@lru_cache(maxsize=1)
def _extensions_information() -> pd.DataFrame:
    return pp.network.get_extensions_information()


def extension_description(name: str) -> str:
    info = _extensions_information()
    if name in info.index and "detail" in info.columns:
        return str(info.loc[name, "detail"])
    return ""


def check_extension_name(name: str) -> str:
    """`name` if pypowsybl knows the extension, else a ValueError naming close ones."""
    names = extension_names()
    if name in names:
        return name
    by_lower = {n.lower(): n for n in names}
    if name.lower() in by_lower:
        return by_lower[name.lower()]
    close = [n for n in names if name.lower() in n.lower()]
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise ValueError(
        f"Unknown extension '{name}'.{hint} list_network_extensions lists those "
        "the network carries."
    )


def is_extension_token(token: str) -> bool:
    return token in EXTENSION_ATTACH_ALIASES or token.startswith(EXTENSION_TOKEN_PREFIX)


def parse_extension_tokens(tokens, element_type: str) -> list[ExtensionJoin]:
    """The extension joins an attach list asks for, in a stable order.

    Raises ValueError for an alias used on the wrong element type or an unknown
    extension name.
    """
    joins = []
    for token in sorted(t for t in tokens if is_extension_token(t)):
        alias = EXTENSION_ATTACH_ALIASES.get(token)
        if alias is not None:
            if element_type != alias.element_type:
                raise ValueError(
                    f"attach token '{token}' applies to element_type="
                    f"'{alias.element_type}', not '{element_type}'"
                )
            joins.append(ExtensionJoin(alias.extension, dict(alias.rename)))
            continue
        name = check_extension_name(token[len(EXTENSION_TOKEN_PREFIX) :].strip())
        joins.append(ExtensionJoin(name, prefix=f"{name}_"))
    return joins


def read_extension(network, name: str) -> pd.DataFrame:
    """The extension table, empty (not an error) when no element carries it."""
    frame = network.get_extensions(name)
    if not isinstance(frame, pd.DataFrame):
        return pd.DataFrame()
    return frame


def attach_extension(elements_df: pd.DataFrame, network, join: ExtensionJoin):
    """Join one extension's columns onto an element table, by element id.

    Elements without the extension get null; the columns are added even when no
    element has it, so a condition on them selects nothing instead of failing on
    an unknown field.
    """
    if elements_df is None:
        return elements_df
    ext = read_extension(network, join.extension)
    if ext.index.nlevels > 1:
        raise ValueError(
            f"Extension '{join.extension}' is not indexed by element id; read it "
            "with get_network_extension_data"
        )
    out = elements_df.copy()
    for column in ext.columns:
        target = join.rename.get(column, f"{join.prefix}{column}")
        if target in out.columns:
            logger.debug(f"attach: column '{target}' already present, kept as is")
            continue
        values = out.index.map(ext[column])
        if pd.api.types.is_integer_dtype(ext[column]):
            # An element without the extension gets null, which would turn the
            # codes into floats (2 -> 2.0): keep them integers.
            values = pd.array(values, dtype="Int64")
        out[target] = values
    return out


def present_extensions(network) -> list[dict]:
    """The extensions at least one element of `network` carries, with counts."""
    present = []
    for name in extension_names():
        try:
            frame = read_extension(network, name)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.debug(f"Extension '{name}' could not be read: {e}")
            continue
        if len(frame):
            present.append(
                {
                    "name": name,
                    "count": len(frame),
                    "columns": [str(c) for c in frame.columns],
                    "description": extension_description(name),
                }
            )
    return present
