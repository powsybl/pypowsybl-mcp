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
from pypowsybl_mcp.plugins.grid_extension.creation.context import CreationError
from pypowsybl_mcp.plugins.grid_extension.creation.engine import CreationEngine
from pypowsybl_mcp.plugins.grid_extension.creation.executors import (
    DerivedCall,
    Unavailable,
)
from pypowsybl_mcp.plugins.grid_extension.creation.profiles import PROFILES, UNPROFILED
from pypowsybl_mcp.plugins.grid_extension.creation.topology import OPERATIONS


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
    assert report["changes"] == {
        "element_types": {},
        "attributes": {},
        "bays": {},
        "topology_operations": {},
    }
    assert report["topology_operations"] == {
        "offered": sorted(OPERATIONS),
        "disabled": {},
    }
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
    assert "has no profile" in str(created)
    assert "L_NEW" in network.get_loads().index


def _with_coal(profile, as_default=False):
    """A generator profile listing a value pypowsybl does not know."""
    enums = {
        **profile.enums,
        "energy_source": (*profile.enums["energy_source"], "COAL"),
    }
    defaults = {**profile.defaults, "energy_source": "COAL"} if as_default else None
    return dataclasses.replace(
        profile, enums=enums, defaults=defaults or profile.defaults
    )


def _upgraded(tmp_path):
    recorded = drift.coverage_snapshot()
    recorded["pypowsybl_version"] = "1.14.0"
    return _snapshot_file(tmp_path, recorded)


def test_an_upgrade_drops_a_value_pypowsybl_refuses(registries, tmp_path):
    profiles, unprofiled = registries
    profiles["generator"] = _with_coal(profiles["generator"])

    report = drift.startup_check(
        profiles, unprofiled, snapshot_path=_upgraded(tmp_path)
    )

    assert report["enum_values"]["checked"]
    assert report["enum_values"]["refused_values"] == {
        "generator": {"energy_source": ["COAL"]}
    }
    assert "COAL" not in profiles["generator"].enums["energy_source"]
    assert "generator" not in report["disabled_profiles"]
    # a caller now gets the server's refusal, not pypowsybl's
    with pytest.raises(CreationError, match="not a valid energy_source"):
        CreationEngine(profiles, unprofiled).create(
            pp.network.create_ieee14(),
            "generator",
            {
                "id": "G",
                "bus": "B4",
                "target_p": 1.0,
                "max_p": 2.0,
                "energy_source": "COAL",
            },
        )


def test_an_upgrade_disables_a_profile_whose_defaults_are_refused(registries, tmp_path):
    profiles, unprofiled = registries
    profiles["generator"] = _with_coal(profiles["generator"], as_default=True)

    report = drift.startup_check(
        profiles, unprofiled, snapshot_path=_upgraded(tmp_path)
    )

    assert "refuses its defaults" in report["disabled_profiles"]["generator"][0]
    assert "generator" not in profiles
    assert unprofiled["generator"].schema_only


def test_on_the_checked_version_the_values_are_not_probed(registries, tmp_path):
    path = _snapshot_file(tmp_path, drift.coverage_snapshot())

    report = drift.startup_check(*registries, snapshot_path=path)

    assert not report["enum_values"]["checked"]


def test_the_online_check_reports_in_the_background(registries, tmp_path):
    drift.startup_check(*registries, snapshot_path=_snapshot_file(tmp_path, {}))
    checkout = tmp_path / "powsybl-core"
    source = checkout / "iidm/iidm-api/src/main/java/com/powsybl/iidm/network"
    source.mkdir(parents=True)
    (source / "EnergySource.java").write_text(
        "public enum EnergySource { HYDRO, NUCLEAR, WIND, THERMAL, SOLAR, OTHER, TIDAL }"
    )

    drift.start_upstream_enum_check(checkout).join(timeout=60)

    upstream = drift.LAST_REPORT["enum_values"]["upstream"]
    assert upstream["status"] == "done"
    assert "generator.energy_source: added: TIDAL" in upstream["to_review"]


def test_a_failing_online_check_does_not_get_in_the_way(
    registries, tmp_path, monkeypatch
):
    drift.startup_check(*registries, snapshot_path=_snapshot_file(tmp_path, {}))

    def offline(local=None):
        raise OSError("no route to host")

    monkeypatch.setattr(drift.enum_check, "upstream_review", offline)

    drift.start_upstream_enum_check().join(timeout=60)

    assert drift.LAST_REPORT["enum_values"]["upstream"] == {
        "status": "failed",
        "error": "no route to host",
    }


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
        (
            {
                "special_executor": DerivedCall(
                    schema.CreationCall(
                        "pp.network", "create_load_bay_v2", ("df",), "CREATE_FEEDER_BAY"
                    )
                )
            },
            "create_load_bay_v2 no longer",
        ),
        (
            {"special_executor": Unavailable("load", "bay helper")},
            "has no bay helper for load",
        ),
        ({"docs": ("Network.create_loads_v2",)}, "create_loads_v2, documenting"),
        (
            {
                "special_executor": DerivedCall(
                    schema.CreationCall(
                        "pp.network",
                        "create_line_bays",
                        ("df",),
                        "NO_SUCH_MODIFICATION",
                    )
                )
            },
            "schema cannot be read",
        ),
    ],
    ids=["renamed-call", "call-gone-from-registry", "documentation-call", "schema"],
)
def test_the_problems_name_what_is_missing(change, expected):
    profile = dataclasses.replace(PROFILES["load"], **change)

    problems = drift.profile_problems(profile)

    assert any(expected in problem for problem in problems), problems


def test_a_missing_snapshot_is_reported(registries, tmp_path):
    report = drift.startup_check(*registries, snapshot_path=tmp_path / "absent.json")

    assert report["status"] == "no snapshot"
    assert report["verified_against"] is None


def test_the_registries_survive_a_missing_private_hook(monkeypatch):
    """Without a hook its registry is empty, but the server still starts."""
    from pypowsybl import _pypowsybl as _pp

    monkeypatch.delattr(Network, "_create_elements")
    assert schema._create_calls() == {}

    monkeypatch.delattr(_pp, "get_network_modification_metadata_with_element_type")
    assert schema._bay_calls() == {}


def test_a_bay_profile_without_its_bay_is_set_aside(registries, tmp_path, monkeypatch):
    """A bay helper gone upstream: the type falls back to schema-only creation."""
    monkeypatch.delitem(schema.BAY_CALLS, "load")
    profiles, unprofiled = registries
    profiles["load"] = dataclasses.replace(profiles["load"])  # derive anew

    report = drift.startup_check(
        profiles, unprofiled, snapshot_path=_snapshot_file(tmp_path, {})
    )

    assert "has no bay helper for load" in report["disabled_profiles"]["load"][0]
    assert unprofiled["load"].schema_only


def test_an_operation_out_of_date_is_set_aside(registries, tmp_path):
    operations = dict(OPERATIONS)
    operations["create_coupling_device"] = dataclasses.replace(
        operations["create_coupling_device"],
        required=("bus_or_busbar_section_id_1", "renamed_upstream"),
    )

    report = drift.startup_check(
        *registries,
        snapshot_path=_snapshot_file(tmp_path, drift.coverage_snapshot()),
        operations=operations,
    )

    assert report["status"] == "operations disabled"
    (problem,) = report["topology_operations"]["disabled"]["create_coupling_device"]
    assert "renamed_upstream" in problem
    assert "create_coupling_device" not in operations
    assert "create_coupling_device" in OPERATIONS  # only the copy is changed


def test_a_topology_modification_new_upstream_is_reported(registries, tmp_path):
    recorded = drift.coverage_snapshot()
    del recorded["topology_operations"]["create_coupling_device"]
    recorded["topology_operations"]["create_line_on_line"].remove("position_percent")

    report = drift.startup_check(
        *registries,
        snapshot_path=_snapshot_file(tmp_path, recorded),
        operations=dict(OPERATIONS),
    )

    assert report["status"] == "changed"
    assert report["changes"]["topology_operations"] == {
        "added": ["create_coupling_device"],
        "removed": [],
        "attributes": {
            "create_line_on_line": {"added": ["position_percent"], "removed": []}
        },
    }
