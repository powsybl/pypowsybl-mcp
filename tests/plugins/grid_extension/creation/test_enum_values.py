#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The accepted values of the profiles, checked against the installed pypowsybl.

Offline: every value a profile lists, and every default, must be accepted by
pypowsybl, so a value removed or renamed upstream fails here. The values
pypowsybl *added* are found by ``scripts/creation_enums.py``, which reads the
powsybl-core sources; its offline parts are tested here too.
"""

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pypowsybl_mcp.plugins.grid_extension.creation import enum_check
from pypowsybl_mcp.plugins.grid_extension.creation.profiles import PROFILES

CASES = [
    (element_type, field, value)
    for element_type, field in enum_check.enum_fields()
    for value in (None, *PROFILES[element_type].enums[field])
]


@pytest.mark.parametrize(
    ("element_type", "field", "value"),
    CASES,
    ids=[f"{t}.{f}={'default' if v is None else v}" for t, f, v in CASES],
)
def test_pypowsybl_accepts_every_listed_value(element_type, field, value):
    assert enum_check.rejected_value(element_type, field, value) is None


def test_every_type_with_listed_values_has_an_example():
    types = {element_type for element_type, _ in enum_check.enum_fields()}

    assert types <= set(enum_check.EXAMPLES)


def test_a_value_pypowsybl_does_not_know_is_caught(monkeypatch):
    """What the test would report if a value were removed upstream."""
    generator = PROFILES["generator"]
    monkeypatch.setitem(
        PROFILES,
        "generator",
        replace(
            generator,
            enums={
                **generator.enums,
                "energy_source": (*generator.enums["energy_source"], "COAL"),
            },
        ),
    )

    error = enum_check.rejected_value("generator", "energy_source", "COAL")

    assert "No enum constant com.powsybl.iidm.network.EnergySource.COAL" in error


@pytest.mark.parametrize(
    ("element_type", "field"), enum_check.enum_fields(), ids="{0[0]}.{0[1]}".format
)
def test_pypowsybl_names_the_enum_behind_every_field(element_type, field):
    java_class = enum_check.java_enum(element_type, field)

    assert java_class is not None
    assert java_class.startswith("com.powsybl.")


def test_the_review_records_name_real_fields_and_values():
    fields = set(enum_check.enum_fields())
    for record in (enum_check.SERVER_ONLY, enum_check.LEFT_OUT, enum_check.WORKAROUNDS):
        assert set(record) <= fields
    for (element_type, field), values in enum_check.SERVER_ONLY.items():
        assert values <= set(PROFILES[element_type].enums[field])
    for (element_type, field), values in enum_check.LEFT_OUT.items():
        assert not set(values) & set(PROFILES[element_type].enums[field])


# ---------------------------------------------------------------------------
# Reading a Java enum
# ---------------------------------------------------------------------------

TOP_LEVEL = """
package com.powsybl.iidm.network;

/**
 * Energy sources. A {@code WIND} source is intermittent.
 */
public enum EnergySource {
    HYDRO(false),
    NUCLEAR(false),
    // an intermittent one
    WIND(true),
    @Deprecated
    COAL(false),
    OTHER(false);

    private final boolean intermittent;

    EnergySource(boolean intermittent) {
        this.intermittent = intermittent;
    }
}
"""

NESTED = """
public interface StaticVarCompensator extends Injection<StaticVarCompensator> {

    enum RegulationMode {
        VOLTAGE,
        REACTIVE_POWER
    }

    enum Other { A, B { @Override public String toString() { return "b"; } }, C }

    double getBmin();
}
"""


def test_enum_constants_of_a_top_level_enum():
    constants = enum_check.enum_constants(TOP_LEVEL, "EnergySource")

    assert constants == {
        "HYDRO": False,
        "NUCLEAR": False,
        "WIND": False,
        "COAL": True,
        "OTHER": False,
    }


def test_enum_constants_of_nested_enums():
    assert enum_check.enum_constants(NESTED, "RegulationMode") == {
        "VOLTAGE": False,
        "REACTIVE_POWER": False,
    }
    assert list(enum_check.enum_constants(NESTED, "Other")) == ["A", "B", "C"]


def test_enum_constants_of_a_missing_enum():
    assert enum_check.enum_constants(NESTED, "ConvertersMode") is None


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def test_compare_reports_what_to_review():
    differences = enum_check.compare(
        "generator",
        "energy_source",
        ("HYDRO", "NUCLEAR", "COAL"),
        {"HYDRO": False, "NUCLEAR": True, "WIND": False},
    )

    assert differences == {
        "added": ["WIND"],
        "removed": ["COAL"],
        "deprecated": ["NUCLEAR"],
        "workaround_obsolete": [],
    }


def test_compare_leaves_out_what_was_reviewed():
    # BOTH is the server's own; OFF is worked around until upstream has it
    assert not any(
        enum_check.compare(
            "operational_limits",
            "side",
            ("ONE", "TWO", "BOTH"),
            {"ONE": False, "TWO": False},
        ).values()
    )
    assert enum_check.compare(
        "static_var_compensator",
        "regulation_mode",
        ("VOLTAGE", "REACTIVE_POWER"),
        {"VOLTAGE": False, "REACTIVE_POWER": False, "OFF": False},
    ) == {"added": [], "removed": [], "deprecated": [], "workaround_obsolete": ["OFF"]}


# ---------------------------------------------------------------------------
# The upstream comparison, offline parts
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "creation_enums.py"


def test_the_script_loads():
    spec = importlib.util.spec_from_file_location("creation_enums", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["creation_enums"] = module
    spec.loader.exec_module(module)

    assert callable(module.main)


def test_the_bundled_powsybl_core_version_is_read():
    version = enum_check.core_version()

    assert version.count(".") == 2
    assert all(part.isdigit() for part in version.split("."))


def test_where_an_enum_is_declared():
    paths, name = enum_check.source_paths(
        "com.powsybl.iidm.network.StaticVarCompensator$RegulationMode"
    )
    assert paths == [
        "iidm/iidm-api/src/main/java/com/powsybl/iidm/network/StaticVarCompensator.java"
    ]
    assert name == "RegulationMode"

    paths, name = enum_check.source_paths(
        "com.powsybl.iidm.network.extensions.ConnectablePosition$Direction"
    )
    assert paths[0].startswith("iidm/iidm-extensions/")
    assert name == "Direction"

    assert (
        enum_check.source_paths("com.powsybl.python.network.TemporaryLimitData$Side")
        is None
    )


def test_the_upstream_review_reads_a_local_checkout(tmp_path):
    """Offline: a powsybl-core checkout where EnergySource changed."""
    path = (
        tmp_path
        / "iidm/iidm-api/src/main/java/com/powsybl/iidm/network/EnergySource.java"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        "public enum EnergySource { HYDRO, NUCLEAR, THERMAL, SOLAR, OTHER, TIDAL }"
    )

    review = enum_check.upstream_review(tmp_path)

    (energy,) = [e for e in review["fields"] if e["field"] == "generator.energy_source"]
    assert energy["state"] == "review"
    assert energy["differences"]["added"] == ["TIDAL"]
    assert energy["differences"]["removed"] == ["WIND"]
    # every other enum is missing from this checkout, and said so
    assert any("not found in" in line for line in review["to_review"])
