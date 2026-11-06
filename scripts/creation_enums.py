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
from pathlib import Path

from pypowsybl_mcp.plugins.grid_extension.creation import enum_check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="fail on anything to review"
    )
    parser.add_argument(
        "--source", type=Path, help="a local powsybl-core checkout, instead of GitHub"
    )
    arguments = parser.parse_args()

    review = enum_check.upstream_review(arguments.source)
    version = review["powsybl_core"]
    origin = arguments.source or f"GitHub, tag v{version}"
    print(f"powsybl-core {version} (bundled by pypowsybl), sources from {origin}\n")

    for entry in review["fields"]:
        if entry["state"] not in ("ok", "review") or "differences" not in entry:
            continue
        short = re.sub(r"^com\.powsybl\.iidm\.network\.", "", entry["java_enum"])
        print(
            f"{'ok' if entry['state'] == 'ok' else 'REVIEW':7}{entry['field']:42}{short}"
        )
        for kind, values in entry["differences"].items():
            if values:
                print(f"         {kind.replace('_', ' ')}: {', '.join(values)}")

    if review["unchecked"]:
        print("\nNot checked (enums outside powsybl-core):")
        for line in review["unchecked"]:
            print(f"  {line}")
    if review["to_review"]:
        print(
            "\nTo review, in pypowsybl_mcp/plugins/grid_extension/creation/profiles.py"
        )
        print("(or enum_check.LEFT_OUT for a value deliberately not offered):")
        for line in review["to_review"]:
            print(f"  {line}")
    else:
        print("\nEvery accepted value matches the bundled powsybl-core.")
    return 1 if arguments.check and review["to_review"] else 0


if __name__ == "__main__":
    sys.exit(main())
