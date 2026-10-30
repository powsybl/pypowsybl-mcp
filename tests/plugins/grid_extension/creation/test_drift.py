#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The startup check of the creation profiles against the installed pypowsybl.

Upgrades are simulated by editing the recorded snapshot or a copy of a profile,
and every check runs on copies of the registries, never on the shared ones.
"""

import dataclasses
import json

import pypowsybl as pp
import pytest
from pypowsybl.network import Network

from pypowsybl_mcp.plugins.grid_extension.creation import drift, schema
from pypowsybl_mcp.plugins.grid_extension.creation.engine import CreationEngine
from pypowsybl_mcp.plugins.grid_extension.creation.executors import (
    BranchBays,
    FeederBay,
)
from pypowsybl_mcp.plugins.grid_extension.creation.profiles import PROFILES, UNPROFILED


@pytest.fixture
def registries():
    return dict(PROFILES), dict(UNPROFILED)


@pytest.fixture(autouse=True)
def keep_the_last_report(monkeypatch):
    """A check run with apply=True stores its report: do not leak it."""
    monkeypatch.setattr(drift, "LAST_REPORT", None)


def _snapshot_file(tmp_path, snapshot):
    path = tmp_path / "coverage_snapshot.json"
    path.write_text(json.dumps(snapshot))
    return path


def test_the_installed_version_is_verified(registries, tmp_path):
    path = _snapshot_file(tmp_path, drift.coverage_snapshot())

    report = drift.startup_check(*registries, snapshot_path=path)

    assert report["status"] == "verified"
    assert report["verified_against"] == pp.__version__
    assert not report["version_changed"]
    assert report["changes"] == {"element_types": {}, "attributes": {}}
    assert not report["disabled_profiles"]
    assert report["element_types"]["profiled"] == len(PROFILES)
    assert drift.LAST_REPORT is report


def test_an_upgrade_is_reported_with_what_changed(registries, tmp_path):
    recorded = drift.coverage_snapshot()
    recorded["pypowsybl_version"] = "1.14.0"
    del recorded["schema_only"]["switch"]  # a type the new version brings
    recorded["unsupported"].append("dangling_line")  # one it no longer has
    recorded["profiled"]["load"].remove("p0")  # an attribute it adds
    recorded["profiled"]["load"].append("load_type")  # one it drops

    report = drift.startup_check(
        *registries, snapshot_path=_snapshot_file(tmp_path, recorded)
    )

    assert report["status"] == "changed"
    assert report["version_changed"]
    assert report["verified_against"] == "1.14.0"
    moved = report["changes"]["element_types"]
    assert moved["switch"] == {"was": None, "now": "schema_only"}
    assert moved["dangling_line"] == {"was": "unsupported", "now": None}
    assert report["changes"]["attributes"]["load"] == {
        "added": ["p0"],
        "removed": ["load_type"],
    }


def test_a_profile_out_of_date_falls_back_to_schema_only(registries, tmp_path):
    profiles, unprofiled = registries
    profiles["load"] = dataclasses.replace(
        profiles["load"], required=("p0", "renamed_upstream")
    )

    report = drift.startup_check(
        profiles, unprofiled, snapshot_path=_snapshot_file(tmp_path, {})
    )

    assert report["status"] == "profiles disabled"
    assert "renamed_upstream" in report["disabled_profiles"]["load"][0]
    assert "load" not in profiles
    assert unprofiled["load"].schema_only
    # the type can still be created, the way pypowsybl takes it
    network = pp.network.create_ieee14()
    created = CreationEngine(profiles, unprofiled).create(
        network,
        "load",
        {"id": "L_NEW", "voltage_level_id": "VL4", "bus_id": "B4", "p0": 1.0, "q0": 0},
    )
    assert "has no profile" in created
    assert "L_NEW" in network.get_loads().index


def test_without_apply_nothing_is_changed_or_kept(registries, tmp_path):
    profiles, unprofiled = registries
    profiles["load"] = dataclasses.replace(profiles["load"], managed=("gone",))

    report = drift.startup_check(
        profiles, unprofiled, snapshot_path=_snapshot_file(tmp_path, {}), apply=False
    )

    assert "load" in report["disabled_profiles"]
    assert not profiles["load"].schema_only
    assert drift.LAST_REPORT is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"executor": FeederBay("create_load_bay_v2")}, "create_load_bay_v2 no longer"),
        ({"docs": ("Network.create_loads_v2",)}, "create_loads_v2, documenting"),
        (
            {"executor": BranchBays("create_line_bays", "NO_SUCH_MODIFICATION")},
            "schema cannot be read",
        ),
    ],
    ids=["executor-call", "documentation-call", "schema"],
)
def test_the_problems_name_what_is_missing(change, expected):
    profile = dataclasses.replace(PROFILES["load"], **change)

    problems = drift.profile_problems(profile)

    assert any(expected in problem for problem in problems), problems


def test_a_missing_snapshot_is_reported(registries, tmp_path):
    report = drift.startup_check(*registries, snapshot_path=tmp_path / "absent.json")

    assert report["status"] == "no snapshot"
    assert report["verified_against"] is None


def test_the_method_map_survives_a_missing_private_hook(monkeypatch):
    """Without the hook nothing is schema-only, but the server still starts."""
    monkeypatch.delattr(Network, "_create_elements")

    assert schema._creation_methods() == {}
