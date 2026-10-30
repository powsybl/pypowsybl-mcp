#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The check that keeps the overlay honest against the installed pypowsybl.

Profiles name pypowsybl attributes and pypowsybl callables. When an upgrade
renames or removes one of them, these tests fail here -- at CI time, naming the
profile and the attribute -- instead of at call time in front of a user. That
is the property that makes an introspection-driven design safe to maintain: the
hand-written half can never drift silently from the generated half.
"""

import pytest

from pypowsybl_mcp.plugins.grid_extension.creation.drift import PROFILE_CHECKS
from pypowsybl_mcp.plugins.grid_extension.creation.profiles import (
    PROFILES,
    UNPROFILED,
    Connection,
)
from pypowsybl_mcp.plugins.grid_extension.creation.schema import (
    CREATION_METHODS,
    creatable_element_types,
)

PROFILE_IDS = sorted(PROFILES)


@pytest.fixture(params=PROFILE_IDS)
def profile(request):
    return PROFILES[request.param]


@pytest.mark.parametrize("check", sorted(PROFILE_CHECKS))
def test_profile_matches_the_installed_pypowsybl(profile, check):
    """The name checks the server also runs at startup (creation/drift.py).

    An upstream rename or removal shows up here, naming the profile and the
    check, instead of at call time.
    """
    problems = PROFILE_CHECKS[check](profile, profile.schema)

    assert not problems, f"{profile.element_type}: " + "; ".join(problems)


def test_managed_attributes_are_never_required_or_defaulted(profile):
    managed = set(profile.managed)
    assert not managed & set(profile.required)
    assert not managed & set(profile.defaults)


def test_enumerated_values_are_uppercase(profile):
    """The engine upper-cases what the caller sends before comparing."""
    for name, values in profile.enums.items():
        assert all(value == value.upper() for value in values), name


def test_attachments_declare_their_target_types(profile):
    if profile.connection is Connection.TARGET:
        assert profile.target_types, (
            f"{profile.element_type} attaches to an existing element but names no "
            "target type, so it would accept any"
        )


def test_every_profile_has_a_summary_and_guidance(profile):
    """Both are what a caller reads instead of a per-tool docstring."""
    assert profile.summary
    assert len(profile.guidance) > 40


def test_batch_order_is_consistent_with_dependencies():
    """Containers before equipment, equipment before what attaches to it."""
    order = {name: item.order for name, item in PROFILES.items()}
    assert order["substation"] < order["voltage_level"]
    assert order["voltage_level"] < order["load"]
    assert order["line"] <= order["hvdc_line"]
    assert order["two_windings_transformer"] < order["ratio_tap_changer"]
    assert order["line"] < order["operational_limits"]
    assert order["vsc_converter_station"] < order["hvdc_line"]


@pytest.mark.parametrize("name", sorted(UNPROFILED))
def test_every_schema_only_type_has_a_creation_method(name):
    from pypowsybl.network import Network

    profile = UNPROFILED[name]
    assert profile.schema_only
    assert profile.executor.missing_call() is None
    assert hasattr(Network, profile.executor.method)
    assert profile.summary


def test_profiles_and_fallbacks_cover_every_creatable_type():
    """Nothing pypowsybl can create is left out unless it has no create_* call."""
    assert not set(PROFILES) & set(UNPROFILED)
    left_out = set(creatable_element_types()) - set(PROFILES) - set(UNPROFILED)
    assert not left_out & set(CREATION_METHODS)


def test_every_creation_method_is_observed():
    """The spy must see every non-deprecated create_* call but create_extensions."""
    from pypowsybl.network import Network

    observed = set(CREATION_METHODS.values())
    for profile in PROFILES.values():
        calls = profile.executor.calls
        for call in calls:
            if call.startswith("Network."):
                assert call.removeprefix("Network.") in observed
    assert all(hasattr(Network, method) for method in observed)


def test_the_descriptor_renders_for_every_profile(profile):
    """describe() is the caller's only documentation: it must work everywhere."""
    from pypowsybl_mcp.plugins.grid_extension.creation.engine import CreationEngine

    described = CreationEngine().describe(profile.element_type)

    assert described["summary"]
    assert described["created_by"]
    assert described["id_attribute"]
    # every rule and derivation explains itself, in the caller's terms
    for text in described.get("rules", []) + described.get("derived", []):
        assert text and not text.startswith("<")
    names = {item["name"] for item in described["required"] + described["optional"]}
    assert set(profile.required) <= names
    assert not names & set(profile.managed)
