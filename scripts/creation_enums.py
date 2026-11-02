#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Compare the accepted values of the creation profiles with pypowsybl's enums.

The profiles hard-code the values of their text attributes (an energy source, a
regulation mode...). The offline test ``test_enum_values.py`` checks that
pypowsybl still accepts every one of them; this script finds what it cannot: a
value pypowsybl *added*, a value it now deprecates, a workaround that is no
longer needed. Run it when upgrading pypowsybl:

    uv run python scripts/creation_enums.py                 # report
    uv run python scripts/creation_enums.py --check         # fail on anything to review
    uv run python scripts/creation_enums.py --source ../powsybl-core   # offline

For each field, the Java enum behind it is found offline, from pypowsybl's own
refusal of a bogus value (see ``creation/enum_check.py``). Its constants are then
read from the powsybl-core sources of the version pypowsybl bundles -- fetched
from GitHub at that release tag, or read from a local checkout with
``--source``. A value worth knowing about but deliberately not offered goes in
``LEFT_OUT`` in ``enum_check.py``, with the reason, so it stops being reported.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from pypowsybl import _pypowsybl as _pp

from pypowsybl_mcp.plugins.grid_extension.creation import enum_check
from pypowsybl_mcp.plugins.grid_extension.creation.profiles import PROFILES

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
    raise SystemExit("powsybl-core not found in pypowsybl's version table")


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


def read_source(path: str, version: str, local: Path | None) -> str | None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="fail on anything to review"
    )
    parser.add_argument(
        "--source", type=Path, help="a local powsybl-core checkout, instead of GitHub"
    )
    arguments = parser.parse_args()

    version = core_version()
    origin = arguments.source or f"GitHub, tag v{version}"
    print(f"powsybl-core {version} (bundled by pypowsybl), sources from {origin}\n")

    sources: dict[str, dict[str, bool] | None] = {}
    to_review: list[str] = []
    unchecked: list[str] = []
    for element_type, field in enum_check.enum_fields():
        label = f"{element_type}.{field}"
        offered = PROFILES[element_type].enums[field]
        java_class = enum_check.java_enum(element_type, field)
        if java_class is None:
            to_review.append(f"{label}: pypowsybl names no enum for it")
            continue
        location = source_paths(java_class)
        if location is None:
            unchecked.append(f"{label}: {java_class} is not in powsybl-core")
            continue
        paths, name = location
        if java_class not in sources:
            sources[java_class] = None
            for path in paths:
                source = read_source(path, version, arguments.source)
                if source is not None:
                    sources[java_class] = enum_check.enum_constants(source, name)
                    break
        upstream = sources[java_class]
        if upstream is None:
            to_review.append(f"{label}: {java_class} not found in {', '.join(paths)}")
            continue

        differences = enum_check.compare(element_type, field, offered, upstream)
        short = re.sub(r"^com\.powsybl\.iidm\.network\.", "", java_class)
        state = "ok" if not any(differences.values()) else "REVIEW"
        print(f"{state:7}{label:42}{short}")
        for kind, values in differences.items():
            if values:
                line = f"{label}: {kind.replace('_', ' ')}: {', '.join(values)}"
                print(f"         {kind.replace('_', ' ')}: {', '.join(values)}")
                to_review.append(line)

    if unchecked:
        print("\nNot checked (enums outside powsybl-core):")
        for line in unchecked:
            print(f"  {line}")
    if to_review:
        print(
            "\nTo review, in pypowsybl_mcp/plugins/grid_extension/creation/profiles.py"
        )
        print("(or enum_check.LEFT_OUT for a value deliberately not offered):")
        for line in to_review:
            print(f"  {line}")
    else:
        print("\nEvery accepted value matches the bundled powsybl-core.")
    return 1 if arguments.check and to_review else 0


if __name__ == "__main__":
    sys.exit(main())
