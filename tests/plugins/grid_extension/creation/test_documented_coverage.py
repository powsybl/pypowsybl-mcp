#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The documented coverage must be the real one.

What the server can create depends on the profiles and on the installed
pypowsybl, both of which change. The tables in ``docs/grid_extension.md`` are
generated from them, and this test fails when they no longer match -- so a
pypowsybl upgrade that adds or renames an element type cannot leave the
documentation quietly wrong.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "creation_coverage.py"


def _load():
    spec = importlib.util.spec_from_file_location("creation_coverage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["creation_coverage"] = module
    spec.loader.exec_module(module)
    return module


def test_the_documented_coverage_matches_the_profiles():
    coverage = _load()

    assert coverage.current() == coverage.block(), (
        "docs/grid_extension.md no longer matches the profiles or the installed "
        "pypowsybl. Run: python scripts/creation_coverage.py --write"
    )


def test_every_supported_type_is_listed_in_the_documentation():
    from pypowsybl_mcp.plugins.grid_extension.creation.profiles import (
        PROFILES,
        UNPROFILED,
    )

    coverage = _load()
    documented = coverage.current()

    for name in [*PROFILES, *UNPROFILED]:
        assert f"`{name}`" in documented


def test_the_shipped_snapshot_matches_the_installed_pypowsybl():
    """The startup check compares against it, so it must be the real coverage."""
    from pypowsybl_mcp.plugins.grid_extension.creation.drift import (
        coverage_snapshot,
        load_snapshot,
    )

    assert load_snapshot() == coverage_snapshot(), (
        "pypowsybl_mcp/plugins/grid_extension/creation/coverage_snapshot.json is out of date. Run: "
        "python scripts/creation_coverage.py --write"
    )
