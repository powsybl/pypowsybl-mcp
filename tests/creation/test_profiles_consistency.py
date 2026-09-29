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

from pypowsybl_mcp.creation.profiles import PROFILES, Connection
from pypowsybl_mcp.creation.schema import creatable_element_types

PROFILE_IDS = sorted(PROFILES)


@pytest.fixture(params=PROFILE_IDS)
def profile(request):
    return PROFILES[request.param]


def known_names(profile) -> set[str]:
    """Attributes a profile may legitimately refer to."""
    names = set(profile.schema.names) | set(profile.virtual)
    if profile.rows_attribute:
        names.add(profile.rows_attribute)
    return names


def test_every_profile_targets_a_creatable_element_type(profile):
    assert profile.element_type in creatable_element_types()


def test_every_executor_call_exists_in_pypowsybl(profile):
    """An upstream rename shows up here, not at call time."""
    assert profile.executor.missing_call() is None, (
        f"{profile.element_type}: {profile.executor.missing_call()} no longer exists "
        "in the installed pypowsybl"
    )


def test_required_attributes_exist_in_the_schema(profile):
    unknown = set(profile.required) - known_names(profile)
    assert not unknown, f"{profile.element_type} requires unknown attributes: {unknown}"


def test_managed_attributes_exist_in_the_schema(profile):
    unknown = set(profile.managed) - set(profile.schema.names)
    assert not unknown, f"{profile.element_type} manages unknown attributes: {unknown}"


def test_defaults_units_and_enums_refer_to_real_attributes(profile):
    names = known_names(profile)
    for label, keys in (
        ("defaults", profile.defaults),
        ("units", profile.units),
        ("enums", profile.enums),
    ):
        unknown = set(keys) - names
        assert not unknown, f"{profile.element_type} {label} name(s) unknown: {unknown}"


def test_rules_refer_to_real_attributes(profile):
    names = known_names(profile)
    for item in profile.rules:
        unknown = set(item.fields) - names
        assert not unknown, (
            f"{profile.element_type} rule {type(item).__name__} refers to "
            f"unknown attribute(s): {unknown}"
        )


def test_derivations_produce_real_attributes(profile):
    names = known_names(profile)
    for derivation in profile.derivations:
        assert derivation.produces in names, (
            f"{profile.element_type} derivation {type(derivation).__name__} produces "
            f"'{derivation.produces}', which is not an attribute of that element"
        )


def test_virtual_attributes_are_not_pypowsybl_fields(profile):
    """A virtual attribute that upstream starts providing must stop being virtual."""
    clashing = set(profile.virtual) & set(profile.schema.names)
    assert not clashing, (
        f"{profile.element_type} declares {clashing} as virtual, but pypowsybl now "
        "provides them: drop them from 'virtual'"
    )


def test_row_attribute_is_not_a_pypowsybl_field(profile):
    if profile.rows_attribute:
        assert profile.rows_attribute not in profile.schema.names


def test_managed_attributes_are_never_required_or_defaulted(profile):
    managed = set(profile.managed)
    assert not managed & set(profile.required)
    assert not managed & set(profile.defaults)


def test_the_id_attribute_is_the_schema_index(profile):
    assert profile.id_field == profile.schema.index_name
    assert profile.schema.field(profile.id_field) is not None


def test_enumerated_values_are_uppercase(profile):
    """The engine upper-cases what the caller sends before comparing."""
    for name, values in profile.enums.items():
        assert all(value == value.upper() for value in values), name


def test_connection_style_matches_the_schema(profile):
    """A bay-connected element must really take a bus_or_busbar_section_id."""
    names = set(profile.schema.names)
    if profile.connection is Connection.SINGLE:
        assert "bus_or_busbar_section_id" in names
    elif profile.connection is Connection.DOUBLE:
        assert {"bus_or_busbar_section_id_1", "bus_or_busbar_section_id_2"} <= names


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


def test_the_descriptor_renders_for_every_profile(profile):
    """describe() is the caller's only documentation: it must work everywhere."""
    from pypowsybl_mcp.creation.engine import CreationEngine

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
