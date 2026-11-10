#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The enumerated values of the profiles, checked against pypowsybl.

Profiles hard-code the accepted values of their text attributes (an energy
source, a regulation mode...) because pypowsybl's metadata types those fields as
plain strings. The name checks of ``drift.py`` cannot see them, so two checks
keep them honest, both built on the same examples -- one valid element of every
type that has such a field:

:func:`rejected_value` (offline)
    Creates the example with a given value: pypowsybl must accept every value a
    profile lists, and every default. Run by
    ``tests/plugins/grid_extension/creation/test_enum_values.py``, it catches a
    value removed or renamed upstream.

:func:`java_enum` (offline)
    Creates the example with a bogus value, the profile's own check lifted, and
    reads the Java enum behind the field from pypowsybl's refusal (*No enum
    constant com.powsybl.iidm.network.EnergySource.NOT_A_VALUE*).
    ``scripts/creation_enums.py`` then reads that enum's constants from the
    powsybl-core sources of the version pypowsybl bundles and :func:`compare`
    them with the profile -- the one way to notice a value pypowsybl *added*.

:func:`check_accepted` runs the first check on a set of profiles, and is what
the startup check does after a pypowsybl upgrade; :func:`upstream_review` runs
the second, for the script and for the online check at startup.

What the comparison should not flag is recorded here: values the server adds
itself (``SERVER_ONLY``), values it knowingly leaves out (``LEFT_OUT``), and
values the profiles work around because the pinned version lacks them
(``WORKAROUNDS``).
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pypowsybl as pp
from pypowsybl import _pypowsybl as _pp

from pypowsybl_mcp.plugins.grid_extension.creation.derivations import LimitRows
from pypowsybl_mcp.plugins.grid_extension.creation.engine import CreationEngine
from pypowsybl_mcp.plugins.grid_extension.creation.profiles import PROFILES

#: the network the examples are created in: node/breaker, so the feeder
#: direction of a bay is meaningful, with two voltage levels in one substation
NETWORK = pp.network.create_four_substations_node_breaker_network
BOGUS = "NOT_A_VALUE"
EXAMPLE_ID = "ENUM_PROBE"


def _hvdc_line(engine: CreationEngine, network) -> dict:
    for station, bus in (("ENUM_VSC_1", "S1VL2_BBS1"), ("ENUM_VSC_2", "S3VL1_BBS")):
        engine.create(
            network,
            "vsc_converter_station",
            {"id": station, "bus": bus, "loss_factor": 1.0},
        )
    return {
        "converter_station1_id": "ENUM_VSC_1",
        "converter_station2_id": "ENUM_VSC_2",
        "r": 1.0,
        "nominal_v": 400.0,
        "max_p": 300.0,
        "target_p": 100.0,
    }


#: element type -> the attributes of a valid example, possibly after creating
#: what it needs; ids and connection points are those of ``NETWORK``
EXAMPLES: dict[str, Callable[[CreationEngine, object], dict]] = {
    "battery": lambda engine, network: {
        "bus": "S1VL2_BBS1",
        "target_p": 0.0,
        "max_p": 10.0,
    },
    "boundary_line": lambda engine, network: {"bus": "S3VL1_BBS", "r": 1.0, "x": 10.0},
    "generator": lambda engine, network: {
        "bus": "S1VL2_BBS1",
        "target_p": 10.0,
        "max_p": 50.0,
    },
    "hvdc_line": _hvdc_line,
    "lcc_converter_station": lambda engine, network: {
        "bus": "S1VL2_BBS1",
        "power_factor": 0.9,
        "loss_factor": 1.0,
    },
    "line": lambda engine, network: {
        "bus1": "S1VL2_BBS1",
        "bus2": "S3VL1_BBS",
        "r": 0.5,
        "x": 5.0,
    },
    "load": lambda engine, network: {"bus": "S1VL2_BBS1", "p0": 10.0},
    "operational_limits": lambda engine, network: {
        "element_id": "LINE_S2S3",
        "permanent_limit": 500.0,
    },
    "phase_tap_changer": lambda engine, network: {"element_id": "TWT"},
    "ratio_tap_changer": lambda engine, network: {"element_id": "TWT"},
    "shunt_compensator": lambda engine, network: {
        "bus": "S1VL2_BBS1",
        "b_per_section": 1e-4,
    },
    "static_var_compensator": lambda engine, network: {
        "bus": "S1VL2_BBS1",
        "b_min": -1e-3,
        "b_max": 1e-3,
        # both setpoints, so that every regulation mode has the one it needs
        "target_v": 400.0,
        "target_q": 0.0,
    },
    "two_windings_transformer": lambda engine, network: {
        "bus1": "S1VL1_BBS",
        "bus2": "S1VL2_BBS2",
        "r": 0.2,
        "x": 10.0,
    },
    "voltage_level": lambda engine, network: {"substation_id": "S1", "nominal_v": 63.0},
    "vsc_converter_station": lambda engine, network: {
        "bus": "S1VL2_BBS1",
        "loss_factor": 1.0,
    },
}


def _boundary_line_limits(engine: CreationEngine, network) -> dict:
    engine.create(
        network,
        "boundary_line",
        {"id": "ENUM_BOUNDARY", "bus": "S3VL1_BBS", "r": 1.0, "x": 10.0},
    )
    return {"element_id": "ENUM_BOUNDARY", "permanent_limit": 500.0}


#: (element type, field, value) -> the network and the example of a value that
#: only exists on some elements: a limit's side THREE is a leg of a
#: three-windings transformer, NONE the single side of a boundary line
VALUE_EXAMPLES: dict[
    tuple[str, str, str],
    tuple[Callable[[], object], Callable[[CreationEngine, object], dict]],
] = {
    ("operational_limits", "side", "THREE"): (
        pp.network.create_micro_grid_be_network,
        lambda engine, network: {
            "element_id": network.get_3_windings_transformers().index[0],
            "permanent_limit": 500.0,
        },
    ),
    ("operational_limits", "side", "NONE"): (NETWORK, _boundary_line_limits),
}

#: (element type, field) -> how to give a value that does not go top-level:
#: the type of a limit is a column of its rows
PLACEMENTS: dict[tuple[str, str], Callable[[str], dict]] = {
    ("operational_limits", "type"): lambda value: {
        "limits": [
            {"side": "ONE", "type": value, "value": 500.0, "acceptable_duration": -1}
        ]
    },
}

#: values a profile offers that are the server's own, not pypowsybl's
SERVER_ONLY: dict[tuple[str, str], frozenset[str]] = {
    # expanded into one row per side by the LimitRows derivation
    ("operational_limits", "side"): frozenset({"BOTH"}),
}

#: values pypowsybl has that the server knowingly does not offer, with why
_VOLTAGE_LIMITS = {
    value: "a voltage limit, which the limits group of a branch cannot carry: "
    "pypowsybl refuses it (Limit type ... does not exist)"
    for value in ("VOLTAGE", "VOLTAGE_ANGLE")
}
_UNDEFINED_DIRECTION = {
    "UNDEFINED": "means the feeder's position is not known, while a new bay is "
    "always placed on top or at the bottom of its busbar (BOTTOM by default)",
}
LEFT_OUT: dict[tuple[str, str], dict[str, str]] = {
    ("operational_limits", "type"): _VOLTAGE_LIMITS,
    ("operational_limits", "limit_type"): _VOLTAGE_LIMITS,
    # the feeder direction of every bay-created type, and of each branch end
    **{
        (element_type, name): _UNDEFINED_DIRECTION
        for element_type, profile in PROFILES.items()
        for name in profile.enums
        if name.startswith("direction")
    },
}

#: values the pinned pypowsybl lacks and a profile works around: if one
#: appears upstream, the workaround can go
WORKAROUNDS: dict[tuple[str, str], dict[str, str]] = {
    ("static_var_compensator", "regulation_mode"): {
        "OFF": "an SVC that should not act is created with regulating: false",
    },
    ("phase_tap_changer", "regulation_mode"): {
        "FIXED_TAP": "a phase shifter that should stay put is created with "
        "regulating: false",
    },
}


def enum_fields() -> list[tuple[str, str]]:
    """Every (element type, field) a profile gives accepted values for."""
    return sorted(
        (element_type, name)
        for element_type, profile in PROFILES.items()
        for name in profile.enums
    )


def _attempt(
    element_type: str,
    field: str | None = None,
    value: str | None = None,
    profiles=None,
) -> str | None:
    """Create the example, with ``field`` set to ``value`` if given.

    Returns pypowsybl's (or the engine's) refusal, or None when it is created.
    """
    profiles = PROFILES if profiles is None else profiles
    engine = CreationEngine(profiles=profiles)
    make_network, example = VALUE_EXAMPLES.get(
        (element_type, field, value), (NETWORK, EXAMPLES[element_type])
    )
    network = make_network()
    try:
        # an attachment's example names its target, which wins over this id
        attributes = {
            profiles[element_type].id_field: EXAMPLE_ID,
            **example(engine, network),
        }
        if field is not None:
            placement = PLACEMENTS.get((element_type, field))
            attributes.update(placement(value) if placement else {field: value})
        engine.create(network, element_type, attributes)
    except Exception as error:  # noqa: BLE001 - any refusal is the answer
        return str(error)
    return None


def rejected_value(
    element_type: str, field: str, value: str | None, profiles=None
) -> str | None:
    """Why ``value`` is refused for ``field``, or None when pypowsybl accepts it.

    With ``value`` None, the example is created with the field left to its
    default.
    """
    if value is None:
        return _attempt(element_type, profiles=profiles)
    return _attempt(element_type, field, value, profiles=profiles)


def check_accepted(profiles) -> dict:
    """Which listed values, and which defaults, pypowsybl refuses.

    Returns ``refused_values`` ({element type: {field: [values]}}),
    ``refused_defaults`` ({element type: error}: its example cannot even be
    created as is) and ``no_example`` (types with listed values but no example to
    check them with). About 80 small creations, a fraction of a second.
    """
    refused_values: dict[str, dict[str, list[str]]] = {}
    refused_defaults: dict[str, str] = {}
    no_example: list[str] = []
    for element_type, profile in sorted(profiles.items()):
        if not profile.enums:
            continue
        if element_type not in EXAMPLES:
            no_example.append(element_type)
            continue
        error = rejected_value(element_type, "", None, profiles=profiles)
        if error is not None:
            refused_defaults[element_type] = error
            continue
        for field, values in profile.enums.items():
            refused = [
                value
                for value in values
                if rejected_value(element_type, field, value, profiles=profiles)
            ]
            if refused:
                refused_values.setdefault(element_type, {})[field] = refused
    return {
        "refused_values": refused_values,
        "refused_defaults": refused_defaults,
        "no_example": no_example,
    }


def java_enum(element_type: str, field: str) -> str | None:
    """The Java enum pypowsybl checks ``field`` against, e.g.
    ``com.powsybl.iidm.network.StaticVarCompensator$RegulationMode``.

    None when the bogus value is accepted, or refused without naming an enum.
    """
    profile = PROFILES[element_type]
    lenient = replace(
        profile,
        enums={name: values for name, values in profile.enums.items() if name != field},
        # the sides of a limit are also checked against the element it goes to
        derivations=tuple(
            replace(item, sides={})
            if isinstance(item, LimitRows) and item.side_field == field
            else item
            for item in profile.derivations
        ),
    )
    error = _attempt(
        element_type, field, BOGUS, profiles={**PROFILES, element_type: lenient}
    )
    match = re.search(rf"No enum constant ([\w.$]+)\.{BOGUS}\b", error or "")
    return match.group(1) if match else None


def enum_constants(source: str, name: str) -> dict[str, bool] | None:
    """The constants of the Java enum ``name`` declared in ``source``.

    Maps each constant to whether it is ``@Deprecated``; None when the source
    declares no such enum. Handles constructor arguments (``HYDRO(false)``),
    constant bodies and comments.
    """
    text = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    match = re.search(rf"\benum\s+{re.escape(name)}\b[^{{;]*\{{", text)
    if match is None:
        return None

    pieces, current, depth = [], [], 0
    for char in text[match.end() :]:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break  # end of an enum with no members after its constants
            depth -= 1
        elif depth == 0 and char in ",;":
            pieces.append("".join(current))
            current = []
            if char == ";":
                break
            continue
        current.append(char)
    else:  # pragma: no cover - unterminated source
        return None
    pieces.append("".join(current))

    constants: dict[str, bool] = {}
    for piece in pieces:
        found = re.match(
            r"\s*((?:@\w+(?:\([^)]*\))?\s*)*)([A-Za-z_]\w*)", piece, flags=re.DOTALL
        )
        if found and found.group(2):
            constants[found.group(2)] = "@Deprecated" in found.group(1)
    return constants


def compare(
    element_type: str, field: str, offered: tuple[str, ...], upstream: dict[str, bool]
) -> dict[str, list[str]]:
    """What differs between a profile's values and the upstream enum's constants.

    ``added``: upstream values the profile neither offers nor knowingly leaves
    out -- to review. ``removed``: offered values upstream no longer has.
    ``deprecated``: offered values upstream deprecates. ``workaround_obsolete``:
    values a profile works around that upstream now has.
    """
    key = (element_type, field)
    server_only = SERVER_ONLY.get(key, frozenset())
    left_out = set(LEFT_OUT.get(key, {}))
    workarounds = set(WORKAROUNDS.get(key, {}))
    offered_upstream = set(offered) - server_only
    current = {name for name, deprecated in upstream.items() if not deprecated}
    return {
        "added": sorted(current - offered_upstream - left_out - workarounds),
        "removed": sorted(offered_upstream - set(upstream)),
        "deprecated": sorted(
            name for name in offered_upstream if upstream.get(name) is True
        ),
        "workaround_obsolete": sorted(workarounds & set(upstream)),
    }


# ---------------------------------------------------------------------------
# Upstream: the powsybl-core enums of the bundled version
# ---------------------------------------------------------------------------

RAW = "https://raw.githubusercontent.com/powsybl/powsybl-core/v{version}/{path}"
#: where powsybl-core keeps the sources of the packages the enums come from,
#: tried in order; the longest matching package wins
MODULES = {
    "com.powsybl.iidm.network.extensions": [
        "iidm/iidm-extensions/src/main/java",
        "iidm/iidm-api/src/main/java",
    ],
    "com.powsybl.iidm.network": ["iidm/iidm-api/src/main/java"],
}


def core_version() -> str:
    """The powsybl-core version the installed pypowsybl bundles."""
    for line in _pp.get_version_table().splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == "powsybl-core":
            return cells[1]
    raise LookupError("powsybl-core not found in pypowsybl's version table")


def source_paths(java_class: str) -> tuple[list[str], str] | None:
    """(candidate paths of the file declaring it, enum name), or None outside
    powsybl-core.

    A nested enum, ``StaticVarCompensator$RegulationMode``, is declared in its
    outer class's file.
    """
    outer, _, nested = java_class.partition("$")
    for package in sorted(MODULES, key=len, reverse=True):
        if outer.startswith(package + "."):
            file = f"{outer.replace('.', '/')}.java"
            name = nested or outer.rsplit(".", 1)[-1]
            return [f"{root}/{file}" for root in MODULES[package]], name
    return None


def read_source(path: str, version: str, local: Path | None = None) -> str | None:
    """A powsybl-core source file at a release, from GitHub or a local checkout."""
    if local is not None:
        file = local / path
        return file.read_text() if file.is_file() else None
    try:
        with urllib.request.urlopen(
            RAW.format(version=version, path=path), timeout=30
        ) as reply:
            return reply.read().decode()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def upstream_review(local: Path | None = None) -> dict:
    """Every enumerated field compared with its powsybl-core enum.

    Returns ``powsybl_core`` (the bundled version), ``fields`` (one entry per
    field: its ``java_enum``, a ``state`` of ok, review, unchecked or
    not_found, and the ``differences``), ``to_review`` (one line per thing to
    look at) and ``unchecked`` (fields whose enum is outside powsybl-core).
    """
    version = core_version()
    sources: dict[str, dict[str, bool] | None] = {}
    fields, to_review, unchecked = [], [], []
    for element_type, field in enum_fields():
        label = f"{element_type}.{field}"
        entry = {"field": label, "java_enum": java_enum(element_type, field)}
        fields.append(entry)
        if entry["java_enum"] is None:
            entry["state"] = "review"
            to_review.append(f"{label}: pypowsybl names no enum for it")
            continue
        location = source_paths(entry["java_enum"])
        if location is None:
            entry["state"] = "unchecked"
            unchecked.append(f"{label}: {entry['java_enum']} is not in powsybl-core")
            continue
        paths, name = location
        if entry["java_enum"] not in sources:
            sources[entry["java_enum"]] = None
            for path in paths:
                source = read_source(path, version, local)
                if source is not None:
                    sources[entry["java_enum"]] = enum_constants(source, name)
                    break
        upstream = sources[entry["java_enum"]]
        if upstream is None:
            entry["state"] = "not_found"
            to_review.append(
                f"{label}: {entry['java_enum']} not found in {', '.join(paths)}"
            )
            continue
        entry["differences"] = compare(
            element_type, field, PROFILES[element_type].enums[field], upstream
        )
        entry["state"] = "review" if any(entry["differences"].values()) else "ok"
        for kind, values in entry["differences"].items():
            if values:
                to_review.append(
                    f"{label}: {kind.replace('_', ' ')}: {', '.join(values)}"
                )
    return {
        "powsybl_core": version,
        "fields": fields,
        "to_review": to_review,
        "unchecked": unchecked,
    }
