#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Generate the creation-coverage tables of ``docs/tools_reference.md``.

What the server can create is decided by the profiles, and what it *could*
create is decided by the installed pypowsybl. Both change, so the documentation
that states them is generated rather than written by hand:

    uv run python scripts/creation_coverage.py          # print the block
    uv run python scripts/creation_coverage.py --write  # update the docs
    uv run python scripts/creation_coverage.py --check  # fail if they differ

``--check`` runs as a test (``tests/creation/test_documented_coverage.py``), so
a pypowsybl upgrade that adds or renames an element type shows up as a failing
test naming exactly what the documentation no longer says.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pypowsybl_mcp.creation.profiles import PROFILES
from pypowsybl_mcp.creation.schema import creatable_element_types

DOCS = Path(__file__).resolve().parent.parent / "docs" / "tools_reference.md"
BEGIN = "<!-- begin generated creation coverage -->"
END = "<!-- end generated creation coverage -->"


def block() -> str:
    """The markdown between the generated markers."""
    import pypowsybl as pp

    supported = dict(sorted(PROFILES.items()))
    everything = set(creatable_element_types())
    missing = sorted(everything - set(supported))

    lines = [
        BEGIN,
        "",
        (
            f"Generated from the profiles and from pypowsybl `{pp.__version__}` "
            "by `scripts/creation_coverage.py`."
        ),
        "",
        f"**{len(supported)} element types can be created**",
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
            f"**{len(missing)} element types expose creation metadata but have no "
            "profile yet**, so `create_network_element` refuses them:"
        ),
        "",
        "```",
        ", ".join(missing),
        "```",
        "",
        (
            "Adding one is a profile entry in "
            "`pypowsybl_mcp/creation/profiles.py`, not a new tool. The ones left "
            "out are either topology primitives that the connection-point model "
            "covers already (`bus`, `busbar_section`, `switch`, "
            "`internal_connection`), boundary and area bookkeeping "
            "(`boundary_line`, `tie_line`, `area*`, `alias`), the detailed DC grid "
            "(`dc_*`, `voltage_source_converter`), or "
            "`three_windings_transformer`, which has no bay creation upstream."
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
        print(f"updated {DOCS}")
        return 0
    if arguments.check:
        if current() != generated:
            print(
                "docs/tools_reference.md no longer matches the profiles or the "
                "installed pypowsybl. Run: python scripts/creation_coverage.py --write"
            )
            return 1
        print("creation coverage documentation is up to date")
        return 0
    print(generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
