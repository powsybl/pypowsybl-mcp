#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Import options of the network loading tools.

PowSyBl reads a grid file with importer parameters (e.g. the CGMES boundary set
location) and post-processors (e.g. the `EntsoeCategory` one, which tags the
generating units whose CGMES type maps to an ENTSO-E category). This module
turns what a client asks for into the `parameters` / `post_processors` arguments
of `pypowsybl.network.load`, and describes the options a client may choose from.

Two rules hold whatever the client sends:

- PowSyBl silently ignores an unknown parameter name, so the names are checked
  here: a misspelled option must fail loudly instead of loading the file without
  it. A known name that belongs to another format is ignored just as silently;
  the format is only known once the file is read, so that case is reported as a
  warning after the load (`load_warnings`).
- `iidm.import.cgmes.boundary-location` is a path on the server's disk. A client
  never sets it directly; it names a boundary set the server provides
  (`CGMES_BOUNDARY_SETS_DIR`) or a URL the server downloads it from.
"""

import json
import os
import zipfile
from dataclasses import dataclass, field
from functools import cache, lru_cache

import pandas as pd
import pypowsybl as pp
from loguru import logger

BOUNDARY_LOCATION_PARAMETER = "iidm.import.cgmes.boundary-location"
CGMES_POST_PROCESSORS_PARAMETER = "iidm.import.cgmes.post-processors"

# Directory holding the boundary sets the server offers: each entry (a
# directory of boundary profiles or a .zip of them) is one set, named after the
# entry. A lone .xml file is not one: PowSyBl only reads a boundary location
# that is a directory or an archive.
BOUNDARY_SETS_DIR_ENV = "CGMES_BOUNDARY_SETS_DIR"


@dataclass
class LoadArguments:
    """What `pypowsybl.network.load` is called with, besides the file."""

    parameters: dict[str, str] = field(default_factory=dict)
    post_processors: list[str] = field(default_factory=list)
    # How the boundary set was given ("boundary_set '<name>'", "boundary_url"),
    # for the answer: the location itself is a server path, never echoed.
    boundary: str | None = None

    def client_parameters(self) -> dict[str, str]:
        """The importer parameters the client named (not those set for it)."""
        hidden = (BOUNDARY_LOCATION_PARAMETER, CGMES_POST_PROCESSORS_PARAMETER)
        return {k: v for k, v in self.parameters.items() if k not in hidden}

    def summary(self, boundary_used: bool = True) -> dict:
        """The options as reported back to the client.

        `boundary_used` False leaves the boundary out: PowSyBl did not read it
        (the file ships its own boundary profiles, or is not CGMES).
        """
        shown = self.client_parameters()
        post_processors = _as_list(
            self.parameters.get(CGMES_POST_PROCESSORS_PARAMETER)
        ) + list(self.post_processors)
        out = {}
        if shown:
            out["parameters"] = shown
        if post_processors:
            out["post_processors"] = post_processors
        if self.boundary and boundary_used:
            out["boundary"] = self.boundary
        return out


def _as_list(value) -> list:
    """Possible values as a list, whatever pypowsybl handed them over as."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text or text == "nan":
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [v.strip() for v in text.split(",") if v.strip()]


@lru_cache(maxsize=1)
def import_formats() -> tuple[str, ...]:
    return tuple(pp.network.get_import_formats())


@lru_cache(maxsize=1)
def import_extensions() -> tuple[str, ...]:
    """The file extensions the importers read, lowercase and without the dot."""
    return tuple(
        sorted({ext.lower() for ext in pp.network.get_import_supported_extensions()})
    )


@cache
def _format_parameters(fmt: str) -> pd.DataFrame:
    return pp.network.get_import_parameters(fmt)


@lru_cache(maxsize=1)
def known_import_parameters() -> dict[str, str]:
    """Every importer parameter name, with the format declaring it."""
    known: dict[str, str] = {}
    for fmt in import_formats():
        try:
            frame = _format_parameters(fmt)
        except pp.PyPowsyblError as e:  # pragma: no cover - depends on the build
            logger.debug(f"No import parameters for format {fmt}: {e}")
            continue
        for name in frame.index:
            known.setdefault(str(name), fmt)
    return known


@lru_cache(maxsize=1)
def cgmes_post_processors() -> tuple[str, ...]:
    """The post-processors of the CGMES importer (e.g. EntsoeCategory)."""
    if "CGMES" not in import_formats():
        return ()
    frame = _format_parameters("CGMES")
    if CGMES_POST_PROCESSORS_PARAMETER not in frame.index:
        return ()
    return tuple(
        _as_list(frame.loc[CGMES_POST_PROCESSORS_PARAMETER, "possible_values"])
    )


@lru_cache(maxsize=1)
def generic_post_processors() -> tuple[str, ...]:
    """The post-processors run after any importer (e.g. geoJsonImporter)."""
    return tuple(pp.network.get_import_post_processors())


def resolve_format(fmt: str) -> str:
    """The import format named `fmt`, matched case-insensitively."""
    by_lower = {f.lower(): f for f in import_formats()}
    resolved = by_lower.get(str(fmt).strip().lower())
    if resolved is None:
        raise ValueError(
            f"Unknown import format '{fmt}'. Available: {', '.join(import_formats())}"
        )
    return resolved


def _to_parameter_value(value) -> str:
    """PowSyBl parameters are strings; lists are comma-separated."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v) for v in value)
    return str(value)


def build_load_arguments(
    parameters: dict | None = None,
    post_processors: list[str] | None = None,
    boundary_location: str | None = None,
    boundary: str | None = None,
) -> LoadArguments:
    """Check the requested options and turn them into `pp.network.load` arguments.

    `post_processors` mixes both kinds: a CGMES one (EntsoeCategory, ...) goes
    into the CGMES importer's parameter, a generic one (geoJsonImporter, ...)
    into the `post_processors` argument. Raises ValueError on an unknown name.
    """
    args = LoadArguments()
    known = known_import_parameters()

    cgmes_pp: list[str] = []
    for name, value in (parameters or {}).items():
        name = str(name).strip()
        if name == BOUNDARY_LOCATION_PARAMETER:
            raise ValueError(
                f"'{BOUNDARY_LOCATION_PARAMETER}' cannot be set directly: pass "
                "boundary_set (a boundary set the server provides, see "
                "get_import_options) or boundary_url instead."
            )
        if name not in known:
            raise ValueError(
                f"Unknown import parameter '{name}'. Call get_import_options(format) "
                "for the parameters of a format."
            )
        if name == CGMES_POST_PROCESSORS_PARAMETER:
            # Merged with the post_processors argument below.
            raw = value if isinstance(value, (list, tuple)) else _as_list(value)
            cgmes_pp.extend(str(v) for v in raw)
            continue
        args.parameters[name] = _to_parameter_value(value)

    cgmes_known = cgmes_post_processors()
    generic_known = generic_post_processors()
    for name in post_processors or []:
        name = str(name).strip()
        if name in cgmes_known:
            cgmes_pp.append(name)
        elif name in generic_known:
            if name not in args.post_processors:
                args.post_processors.append(name)
        else:
            available = sorted(set(cgmes_known) | set(generic_known))
            raise ValueError(
                f"Unknown post-processor '{name}'. Available: {', '.join(available)}"
            )

    for name in cgmes_pp:
        if name not in cgmes_known:
            raise ValueError(
                f"Unknown CGMES post-processor '{name}'. "
                f"Available: {', '.join(cgmes_known)}"
            )
    if cgmes_pp:
        args.parameters[CGMES_POST_PROCESSORS_PARAMETER] = ",".join(
            dict.fromkeys(cgmes_pp)
        )

    if boundary_location:
        args.parameters[BOUNDARY_LOCATION_PARAMETER] = boundary_location
        args.boundary = boundary
    return args


# What PowSyBl says when a CGMES model references boundary objects it cannot
# find - a message that does not mention the boundary set at all.
_MISSING_BOUNDARY_MARKERS = ("BaseVoltage", "boundary", "TopologicalNode", "Boundary")


def explain_load_error(error: Exception, boundary_given: bool) -> str:
    """The load error, with a hint when a CGMES boundary set is likely missing."""
    message = str(error)
    if not boundary_given and any(m in message for m in _MISSING_BOUNDARY_MARKERS):
        message += (
            " - this CGMES model probably needs its boundary set: pass boundary_set"
            " (see get_import_options) or boundary_url, or put the boundary files"
            " in the same archive as the CGMES profiles."
        )
    return message


# What the IIDM importers report the format they read as, and the IIDM format
# name it stands for.
_IIDM_REPORT_PREFIX = "core.iidm.serde."
_IIDM_IMPORTED_NETWORK_KEY = "core.iidm.serde.importedNetwork"
_IIDM_FORMATS = {"XML": "XIIDM", "JSON": "JIIDM", "BIN": "BIIDM"}


def _report_nodes(node: dict):
    yield node
    for child in node.get("children", []) or []:
        yield from _report_nodes(child)


def import_format_of(network, report_json: str | None) -> str | None:
    """The import format that read `network`, or None when it cannot be told.

    `network.source_format` is not enough: an IIDM file keeps the format the
    network first came from (an XIIDM saved from a CGMES load says CGMES). The
    IIDM importers leave their mark in the load report; any other importer
    sets `source_format` to its own name.
    """
    try:
        root = json.loads(report_json or "{}").get("reportRoot") or {}
    except (TypeError, ValueError):
        root = {}
    iidm = False
    for node in _report_nodes(root):
        key = str(node.get("messageKey") or "")
        if key == _IIDM_IMPORTED_NETWORK_KEY:
            fmt = ((node.get("values") or {}).get("format") or {}).get("value")
            return _IIDM_FORMATS.get(str(fmt).upper(), "XIIDM")
        iidm = iidm or key.startswith(_IIDM_REPORT_PREFIX)
    if iidm:
        return "XIIDM"
    source_format = getattr(network, "source_format", None)
    return source_format if source_format in import_formats() else None


# The header profiles of the CGMES boundary files, EQBD and TPBD, in CGMES
# 2.4.15 (".../EquipmentBoundary/3/1") and CGMES 3 (".../EquipmentBoundary-EU/3.0")
# alike. The header sits at the top of the file.
_BOUNDARY_PROFILE_MARKERS = (b"EquipmentBoundary", b"TopologyBoundary")
_HEADER_BYTES = 64 * 1024


def _is_boundary_profile(head: bytes) -> bool:
    return any(marker in head for marker in _BOUNDARY_PROFILE_MARKERS)


def _file_is_boundary_profile(path: str) -> bool:
    with open(path, "rb") as f:
        return _is_boundary_profile(f.read(_HEADER_BYTES))


def ships_boundary(path: str) -> bool:
    """Whether the CGMES model at `path` carries its own boundary profiles.

    PowSyBl then reads those and ignores the boundary set it is given. It tells
    a boundary file by the profile its header declares, not by its name (one may
    well be named `..._EQ.xml`), so the same test is made here.
    """
    try:
        if os.path.isdir(path):
            return any(
                _file_is_boundary_profile(os.path.join(path, name))
                for name in os.listdir(path)
                if name.lower().endswith(".xml")
                and os.path.isfile(os.path.join(path, name))
            )
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".xml"):
                        continue
                    with archive.open(info) as entry:
                        if _is_boundary_profile(entry.read(_HEADER_BYTES)):
                            return True
            return False
        if path.lower().endswith(".xml"):
            return _file_is_boundary_profile(path)
    except (OSError, zipfile.BadZipFile) as e:
        logger.debug(f"Could not look for boundary profiles in {path}: {e}")
    return False


def load_warnings(
    load_args: LoadArguments, import_format: str | None, ships_own_boundary: bool
) -> tuple[list[str], bool]:
    """What PowSyBl silently ignored in a successful load, and whether it used
    the boundary set it was given.

    A parameter of another format, a CGMES post-processor or a boundary set on
    a non-CGMES file, and a boundary set given for a CGMES model that ships its
    own boundary profiles are all dropped without a word from PowSyBl.
    """
    warnings: list[str] = []
    if import_format is None or import_format not in import_formats():
        return warnings, bool(load_args.boundary)

    try:
        accepted = set(_format_parameters(import_format).index)
    except pp.PyPowsyblError as e:  # pragma: no cover - depends on the build
        logger.debug(f"No import parameters for format {import_format}: {e}")
        accepted = set()
    ignored = [name for name in load_args.client_parameters() if name not in accepted]
    if ignored:
        warnings.append(
            f"Ignored by the {import_format} importer, which does not take them: "
            f"{', '.join(ignored)}."
        )

    is_cgmes = import_format == "CGMES"
    cgmes_post_processors = _as_list(
        load_args.parameters.get(CGMES_POST_PROCESSORS_PARAMETER)
    )
    if cgmes_post_processors and not is_cgmes:
        warnings.append(
            f"CGMES post-processor(s) not run on a {import_format} file: "
            f"{', '.join(cgmes_post_processors)}."
        )

    boundary_used = bool(load_args.boundary)
    if load_args.boundary:
        if not is_cgmes:
            boundary_used = False
            warnings.append(
                f"{load_args.boundary} not used: a boundary set only applies to a "
                f"CGMES file, not to a {import_format} one."
            )
        elif ships_own_boundary:
            boundary_used = False
            warnings.append(
                f"{load_args.boundary} not used: the CGMES model ships its own "
                "boundary profiles, which PowSyBl reads instead."
            )
    return warnings, boundary_used


def boundary_sets_dir() -> str | None:
    path = os.environ.get(BOUNDARY_SETS_DIR_ENV, "").strip()
    return path or None


def list_boundary_sets() -> list[str]:
    """Names of the boundary sets the server provides, sorted."""
    root = boundary_sets_dir()
    if not root or not os.path.isdir(root):
        return []
    names = []
    for entry in os.listdir(root):
        if entry.startswith("."):
            continue
        path = os.path.join(root, entry)
        if os.path.isdir(path) or entry.lower().endswith(".zip"):
            names.append(entry)
    return sorted(names)


def resolve_boundary_set(name: str) -> str:
    """The server path of boundary set `name`.

    Only a name `list_boundary_sets` returns is accepted, so a client can never
    point the importer at another place on the server's disk.
    """
    available = list_boundary_sets()
    if name not in available:
        if not available:
            raise ValueError(
                f"Unknown boundary set '{name}': this server provides none "
                f"({BOUNDARY_SETS_DIR_ENV} is not set or empty). Pass boundary_url, "
                "or put the boundary files in the same archive as the CGMES profiles."
            )
        raise ValueError(
            f"Unknown boundary set '{name}'. Available: {', '.join(available)}"
        )
    # A directory or a .zip: PowSyBl reads both, and pointing at the archive
    # itself keeps the other sets of the directory out of the load.
    return os.path.join(boundary_sets_dir(), name)


def default_boundary_set_available() -> bool:
    """Whether a CGMES load given no boundary set still finds one.

    PowSyBl then reads its default boundary location (`~/.itools/CGMES/boundary`
    unless configured otherwise): a set is there if it is a .zip, or a directory
    holding at least one .xml or .zip file - the only ones PowSyBl reads, so a
    README alone does not count. Only the answer is reported, never the path.
    """
    if "CGMES" not in import_formats():
        return False
    frame = _format_parameters("CGMES")
    if BOUNDARY_LOCATION_PARAMETER not in frame.index:
        return False
    location = str(frame.loc[BOUNDARY_LOCATION_PARAMETER, "default"] or "").strip()
    if not location or location == "nan":
        return False
    if os.path.isfile(location):
        return location.lower().endswith(".zip")
    if os.path.isdir(location):
        return any(
            entry.lower().endswith((".xml", ".zip"))
            and os.path.isfile(os.path.join(location, entry))
            for entry in os.listdir(location)
        )
    return False


def _parameter_rows(fmt: str) -> list[dict]:
    frame = _format_parameters(fmt)
    rows = []
    for name, row in frame.iterrows():
        if name == BOUNDARY_LOCATION_PARAMETER:
            continue  # boundary_set / boundary_url
        default = row.get("default")
        rows.append(
            {
                "name": str(name),
                "description": str(row.get("description", "")),
                "type": str(row.get("type", "")),
                "default": "" if pd.isna(default) else str(default),
                "possible_values": _as_list(row.get("possible_values")),
            }
        )
    return rows


def describe_import_options(fmt: str | None = None) -> dict:
    """The options a client can load a network with.

    Without a format, the lists that do not depend on one; with a format, also
    that importer's parameters.
    """
    out = {
        "formats": list(import_formats()),
        "file_extensions": sorted(pp.network.get_import_supported_extensions()),
        "post_processors": {
            "cgmes": list(cgmes_post_processors()),
            "generic": list(generic_post_processors()),
        },
        "boundary_sets": list_boundary_sets(),
        "default_boundary_set": default_boundary_set_available(),
    }
    if fmt is not None:
        resolved = resolve_format(fmt)
        out["format"] = resolved
        out["parameters"] = _parameter_rows(resolved)
    return out
