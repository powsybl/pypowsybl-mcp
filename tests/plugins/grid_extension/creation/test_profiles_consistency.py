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

from pathlib import Path

import pypowsybl as pp
import pytest

from pypowsybl_mcp.plugins import grid_extension
from pypowsybl_mcp.plugins.grid_extension.creation.drift import PROFILE_CHECKS
from pypowsybl_mcp.plugins.grid_extension.creation.engine import (
    CreationEngine,
    target_type_of,
)
from pypowsybl_mcp.plugins.grid_extension.creation.profiles import (
    LEFT_OUT_TYPES,
    PROFILES,
    UNPROFILED,
    Connection,
)
from pypowsybl_mcp.plugins.grid_extension.creation.schema import (
    BAY_CALLS,
    CREATE_CALLS,
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


def test_every_virtual_attribute_declares_its_type(profile):
    # an untyped virtual attribute reaches the rules and derivations unchecked
    assert set(profile.virtual_kinds) == set(profile.virtual)


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
    assert hasattr(Network, profile.executor.call.function)
    assert profile.summary


def test_profiles_and_fallbacks_cover_every_creatable_type():
    """Nothing pypowsybl can create is left out unless it has no create_* call,
    or is knowingly not offered (LEFT_OUT_TYPES, which says why)."""
    assert not set(PROFILES) & set(UNPROFILED)
    assert not set(LEFT_OUT_TYPES) & (set(PROFILES) | set(UNPROFILED))
    left_out = (
        set(creatable_element_types())
        - set(PROFILES)
        - set(UNPROFILED)
        - set(LEFT_OUT_TYPES)
    )
    assert not left_out & set(CREATE_CALLS)


def test_no_profile_names_its_call():
    """Every call comes from the observed registries, the voltage level aside."""
    observed = {call.name for call in [*CREATE_CALLS.values(), *BAY_CALLS.values()]}
    for name, profile in PROFILES.items():
        if profile.special_executor is not None:
            continue
        assert set(profile.executor.calls) <= observed, name


def test_bay_connected_types_go_through_their_bay():
    for name, profile in PROFILES.items():
        if profile.connection in (Connection.SINGLE, Connection.DOUBLE):
            assert profile.executor.calls == (BAY_CALLS[name].name,), name


def test_the_registries_cover_every_creation_call():
    """Every non-deprecated create_* call, and every bay helper, is observed."""
    import pypowsybl.network as pn
    from pypowsybl.network import Network

    assert len(CREATE_CALLS) == len(set(CREATE_CALLS.values())) >= 32
    assert {"load", "line", "two_windings_transformer", "boundary_line"} <= set(
        BAY_CALLS
    )
    for call in [*CREATE_CALLS.values(), *BAY_CALLS.values()]:
        owner = Network if call.owner == "Network" else pn
        assert hasattr(owner, call.function), call.name
        assert call.frames, call.name


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


def test_the_transformer_reactance_guidance_uses_the_side_2_base():
    """pypowsybl refers a transformer's r and x to side 2, and so must the guidance.

    IEEE14's T4-7-1 (135/14 kV) has x = 0.20912 pu on 100 MVA, which in ohms only
    holds on the side-2 base rated_u2^2 / 100. A formula on rated_u1 makes a new
    135/20 kV transformer's x about 45 times too large, and the load flow diverges.
    """
    transformer = pp.network.create_ieee14().get_2_windings_transformers()
    t4_7 = transformer.loc["T4-7-1"]
    assert t4_7.x / (t4_7.rated_u2**2 / 100) == pytest.approx(0.20912, rel=1e-3)

    skill = Path(grid_extension.__file__).parent / "skills" / "grid-extension.md"
    for text in (PROFILES["two_windings_transformer"].guidance, skill.read_text()):
        assert "rated_u2^2" in text
        assert "rated_u1^2" not in text


def _reported_target_types() -> set[str]:
    """Every element type the engine reads off existing elements, over sample
    networks that hold every kind an attachment may target."""
    with_boundary_line = pp.network.create_ieee14()
    engine = CreationEngine()
    engine.create(
        with_boundary_line,
        "boundary_line",
        {"id": "BL", "bus": "B4", "r": 1.0, "x": 10.0},
    )
    engine.create(
        with_boundary_line,
        "battery",
        {"id": "BAT", "bus": "B4", "target_p": 0.0, "max_p": 10.0},
    )
    networks = [
        with_boundary_line,
        pp.network.create_four_substations_node_breaker_network(),  # VSC, LCC
        pp.network.create_micro_grid_be_network(),  # three-windings transformer
        pp.network.create_eurostag_tutorial_example1_with_tie_lines_and_areas(),
    ]
    return {
        target_type_of(network, element_id)
        for network in networks
        for element_id in network.get_identifiables().index
    }


def test_every_target_type_is_one_the_network_reports():
    """A target type the engine never reads off an element can never match:
    dangling_line (BOUNDARY_LINE since 1.15) and vsc_converter_station (reported
    as HVDC_CONVERTER_STATION) were silently dead entries."""
    reported = _reported_target_types()
    for element_type, item in sorted(PROFILES.items()):
        unknown = item.target_types - reported
        assert not unknown, f"{element_type}: never reported: {sorted(unknown)}"
