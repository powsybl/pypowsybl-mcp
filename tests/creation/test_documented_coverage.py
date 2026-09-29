#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The documented coverage must be the real one.

What the server can create depends on the profiles and on the installed
pypowsybl, both of which change. The tables in ``docs/tools_reference.md`` are
generated from them, and this test fails when they no longer match -- so a
pypowsybl upgrade that adds or renames an element type cannot leave the
documentation quietly wrong.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "creation_coverage.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("creation_coverage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["creation_coverage"] = module
    spec.loader.exec_module(module)
    return module


def test_the_documented_coverage_matches_the_profiles():
    coverage = _load()

    assert coverage.current() == coverage.block(), (
        "docs/tools_reference.md no longer matches the profiles or the installed "
        "pypowsybl. Run: python scripts/creation_coverage.py --write"
    )


def test_every_supported_type_is_listed_in_the_documentation():
    from pypowsybl_mcp.creation.profiles import PROFILES

    coverage = _load()
    documented = coverage.current()

    for name in PROFILES:
        assert f"`{name}`" in documented
