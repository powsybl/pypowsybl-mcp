#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Generate the creation-coverage tables of ``docs/grid_extension.md``.

What the server can create is decided by the profiles, and what it *could*
create is decided by the installed pypowsybl. Both change, so the documentation
that states them is generated rather than written by hand:

    uv run python scripts/creation_coverage.py          # print the block
    uv run python scripts/creation_coverage.py --write  # update the docs
    uv run python scripts/creation_coverage.py --check  # fail if they differ

``--write`` also writes ``pypowsybl_mcp/plugins/grid_extension/creation/coverage_snapshot.json``, which
ships with the package: the server compares the installed pypowsybl with it at
startup (see ``pypowsybl_mcp/plugins/grid_extension/creation/drift.py``).

``--check`` runs as a test (``tests/creation/test_documented_coverage.py``), so
a pypowsybl upgrade that adds or renames an element type shows up as a failing
test naming exactly what the documentation no longer says.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pypowsybl_mcp.plugins.grid_extension.creation.drift import (
    SNAPSHOT_PATH,
    coverage_snapshot,
    load_snapshot,
    write_snapshot,
)
from pypowsybl_mcp.plugins.grid_extension.creation.profiles import PROFILES, UNPROFILED
from pypowsybl_mcp.plugins.grid_extension.creation.schema import creatable_element_types

DOCS = Path(__file__).resolve().parent.parent / "docs" / "grid_extension.md"
BEGIN = "<!-- begin generated creation coverage -->"
END = "<!-- end generated creation coverage -->"


def block() -> str:
    """The markdown between the generated markers."""
    import pypowsybl as pp

    supported = dict(sorted(PROFILES.items()))
    schema_only = dict(sorted(UNPROFILED.items()))
    everything = set(creatable_element_types())
    missing = sorted(everything - set(supported) - set(schema_only))

    lines = [
        BEGIN,
        "",
        (
            f"Generated from the profiles and from pypowsybl `{pp.__version__}` "
            "by `scripts/creation_coverage.py`."
        ),
        "",
        (
            f"**{len(supported)} element types have a profile**: required "
            "attributes, units, accepted values and rules are checked, and "
            "injections and branches are created with their switching equipment."
        ),
        "",
        "| `element_type` | pypowsybl call behind it | What it is |",
        "|----------------|--------------------------|------------|",
    ]
    for name, profile in supported.items():
        calls = ", ".join(f"`{call}`" for call in profile.executor.calls)
        lines.append(f"| `{name}` | {calls} | {profile.summary} |")

    lines += [
        "",
        (
            f"**{len(schema_only)} element types have no profile and are created "
            "from their schema only**: attribute names and types are checked, the "
            "rest is left to pypowsybl, and no switching equipment is built."
        ),
        "",
        "| `element_type` | pypowsybl call behind it | What it is |",
        "|----------------|--------------------------|------------|",
    ]
    for name, profile in schema_only.items():
        calls = ", ".join(f"`{call}`" for call in profile.executor.calls)
        lines.append(f"| `{name}` | {calls} | {profile.summary} |")

    lines += [""]
    if missing:
        lines += [
            (
                f"**{len(missing)} element type{'s' if len(missing) > 1 else ''} "
                "expose creation metadata but no `Network.create_*` call**, so "
                "`create_network_element` does not support them yet:"
            ),
            "",
            "```",
            ", ".join(missing),
            "```",
            "",
        ]
    lines += [
        (
            "Giving a schema-only type a profile is an entry in "
            "`pypowsybl_mcp/plugins/grid_extension/creation/profiles.py`, not a new tool; the profile "
            "then takes over from the fallback."
        ),
        "",
        END,
    ]
    return "\n".join(lines)


def current() -> str:
    text = DOCS.read_text()
    start, end = text.index(BEGIN), text.index(END) + len(END)
    return text[start:end]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="update the docs in place")
    parser.add_argument("--check", action="store_true", help="fail if they differ")
    arguments = parser.parse_args()

    generated = block()
    if arguments.write:
        text = DOCS.read_text()
        start, end = text.index(BEGIN), text.index(END) + len(END)
        DOCS.write_text(text[:start] + generated + text[end:])
        write_snapshot()
        print(f"updated {DOCS} and {SNAPSHOT_PATH}")
        return 0
    if arguments.check:
        if current() != generated or load_snapshot() != coverage_snapshot():
            print(
                "docs/grid_extension.md or the coverage snapshot no longer matches "
                "the profiles or the installed pypowsybl. Run: "
                "python scripts/creation_coverage.py --write"
            )
            return 1
        print("creation coverage documentation and snapshot are up to date")
        return 0
    print(generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
