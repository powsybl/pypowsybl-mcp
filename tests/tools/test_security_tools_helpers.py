#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock

import pandas as pd
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.security_tools import SecurityTools, register_security_tools


def test_parse_json_if_needed():
    # Native dict
    val = {"a": 1}
    assert SecurityTools._parse_json_if_needed(val, "test") == val

    # Valid JSON string
    val_str = '{"a": 1}'
    assert SecurityTools._parse_json_if_needed(val_str, "test") == val

    # None
    assert SecurityTools._parse_json_if_needed(None, "test") is None

    # Invalid JSON string
    with pytest.raises(ValueError) as excinfo:
        SecurityTools._parse_json_if_needed("{invalid}", "test_field")
    assert "Invalid JSON for test_field" in str(excinfo.value)


def test_element_nominal_voltage():
    vl_voltages = {"VL1": 400.0, "VL2": 225.0, "VL3": 63.0}

    # Lines/Transformers - highest side
    row = MagicMock()
    row.get.side_effect = lambda k: {
        "voltage_level1_id": "VL1",
        "voltage_level2_id": "VL2",
    }.get(k)
    assert SecurityTools._element_nominal_voltage("line", row, vl_voltages) == 400.0

    row.get.side_effect = lambda k: {
        "voltage_level1_id": "VL3",
        "voltage_level2_id": "VL2",
    }.get(k)
    assert (
        SecurityTools._element_nominal_voltage(
            "two_windings_transformer", row, vl_voltages
        )
        == 225.0
    )

    # Generators - single side
    row = MagicMock()
    row.get.side_effect = lambda k: {"voltage_level_id": "VL2"}.get(k)
    assert (
        SecurityTools._element_nominal_voltage("generator", row, vl_voltages) == 225.0
    )

    # HVDC lines - highest converter station side, same rule as lines
    row = MagicMock()
    row.get.side_effect = lambda k: {
        "converter_station1_id": "CS1",
        "converter_station2_id": "CS2",
    }.get(k)
    station_voltages = {"CS1": 225.0, "CS2": 400.0}
    assert (
        SecurityTools._element_nominal_voltage(
            "hvdc_line", row, vl_voltages, station_voltages
        )
        == 400.0
    )

    # HVDC line whose converter stations are unknown falls back to 0, like the
    # other types, rather than to a sentinel that depends on the filter value.
    assert (
        SecurityTools._element_nominal_voltage("hvdc_line", row, vl_voltages, {}) == 0
    )

    # Unknown type
    assert SecurityTools._element_nominal_voltage("unknown", None, {}) == 0


def test_build_contingencies_from_filter_unsupported_type():
    with pytest.raises(ValueError) as excinfo:
        SecurityTools._build_contingencies_from_filter(None, "unsupported")
    assert "Unsupported element type 'unsupported'" in str(excinfo.value)


def test_build_contingencies_from_filter_logic():
    mock_net = MagicMock()
    # Mock voltage levels
    mock_net.get_voltage_levels.return_value = pd.DataFrame(
        {"nominal_v": [400.0, 225.0, 63.0]}, index=["VL400", "VL225", "VL63"]
    )

    # Mock generators
    mock_net.get_generators.return_value = pd.DataFrame(
        {"voltage_level_id": ["VL400", "VL225", "VL63"]}, index=["G1", "G2", "G3"]
    )

    # Test min/max voltage filtering on generators
    # G1: 400, G2: 225, G3: 63
    res = SecurityTools._build_contingencies_from_filter(
        mock_net, "generator", min_nominal_voltage=100.0, max_nominal_voltage=300.0
    )
    assert res["filtered_count"] == 1
    assert res["contingencies"][0]["element_id"] == "G2"
    assert res["total_elements"] == 3


def test_build_contingencies_from_filter_transformers():
    mock_net = MagicMock()
    mock_net.get_voltage_levels.return_value = pd.DataFrame(
        {"nominal_v": [400.0, 225.0]}, index=["VL400", "VL225"]
    )
    # Transformers use get_2_windings_transformers
    mock_net.get_2_windings_transformers.return_value = pd.DataFrame(
        {"voltage_level1_id": ["VL400"], "voltage_level2_id": ["VL225"]}, index=["T1"]
    )

    res = SecurityTools._build_contingencies_from_filter(
        mock_net, "two_windings_transformer"
    )
    assert res["filtered_count"] == 1
    assert res["contingencies"][0]["element_id"] == "T1"


def _hvdc_network():
    """A network with two HVDC links whose AC sides differ: 400 kV and 63 kV.

    HVDC1 also carries a DC pole voltage (320 kV) distinct from its AC sides,
    which is what makes the "which voltage does the filter mean" question
    observable.
    """
    mock_net = MagicMock()
    mock_net.get_voltage_levels.return_value = pd.DataFrame(
        {"nominal_v": [400.0, 63.0]}, index=["VL400", "VL63"]
    )
    mock_net.get_hvdc_lines.return_value = pd.DataFrame(
        {
            "nominal_v": [320.0, 320.0],
            "converter_station1_id": ["VSC1", "LCC1"],
            "converter_station2_id": ["VSC2", "LCC2"],
        },
        index=["HVDC_400", "HVDC_63"],
    )
    mock_net.get_vsc_converter_stations.return_value = pd.DataFrame(
        {"voltage_level_id": ["VL400", "VL400"]}, index=["VSC1", "VSC2"]
    )
    mock_net.get_lcc_converter_stations.return_value = pd.DataFrame(
        {"voltage_level_id": ["VL63", "VL63"]}, index=["LCC1", "LCC2"]
    )
    return mock_net


def _kept(mock_net, **filters):
    res = SecurityTools._build_contingencies_from_filter(
        mock_net, "hvdc_line", **filters
    )
    return [c["element_id"] for c in res["contingencies"]]


def test_hvdc_min_nominal_voltage_filter_is_applied():
    """Regression (issue #7, bug #11): a min-only filter must actually filter.

    The sentinel returned inf whenever min_nominal_voltage was truthy, so every
    HVDC line passed a min-only filter regardless of its real voltage.
    """
    mock_net = _hvdc_network()

    assert _kept(mock_net, min_nominal_voltage=100.0) == ["HVDC_400"]
    assert _kept(mock_net, min_nominal_voltage=500.0) == []


def test_hvdc_min_and_max_filter_keeps_matching_lines():
    """Regression (issue #7, bug #11): min+max must not drop every HVDC line.

    The inf sentinel always failed the "> max" test, so filtered_count came
    back 0 even when matching HVDC lines existed.
    """
    mock_net = _hvdc_network()

    assert _kept(mock_net, min_nominal_voltage=63.0, max_nominal_voltage=400.0) == [
        "HVDC_400",
        "HVDC_63",
    ]
    assert _kept(mock_net, min_nominal_voltage=100.0, max_nominal_voltage=400.0) == [
        "HVDC_400"
    ]


def test_hvdc_zero_min_voltage_behaves_like_no_minimum():
    """Regression (issue #7, bug #11): a falsy 0.0 must mean "no lower bound".

    The sentinel branched on truthiness, so min=0.0 took the same path as
    min=None while min=0.1 took the opposite one -- an imperceptible change in
    the bound flipped the whole result.

    An upper bound is set on all three so the sentinel's inf is observable:
    without one, nothing ever exceeds the maximum and the bug stays hidden.
    """
    mock_net = _hvdc_network()

    no_minimum = _kept(mock_net, max_nominal_voltage=500.0)
    assert no_minimum == ["HVDC_400", "HVDC_63"]
    assert (
        _kept(mock_net, min_nominal_voltage=0.0, max_nominal_voltage=500.0)
        == no_minimum
    )
    assert (
        _kept(mock_net, min_nominal_voltage=0.1, max_nominal_voltage=500.0)
        == no_minimum
    )


def test_hvdc_filter_uses_ac_side_not_dc_pole_voltage():
    """The documented contract is "elements connected to voltage levels", i.e.
    the converter stations' AC side, not the DC voltage on the HVDC line row.

    Both lines here carry nominal_v=320.0 on the DC side, so a filter that read
    that column instead could not tell the 400 kV link from the 63 kV one.
    """
    mock_net = _hvdc_network()

    assert _kept(mock_net, min_nominal_voltage=330.0, max_nominal_voltage=500.0) == [
        "HVDC_400"
    ]
    assert _kept(mock_net, min_nominal_voltage=0.0, max_nominal_voltage=100.0) == [
        "HVDC_63"
    ]


def test_resolve_contingencies_errors():
    sa = SecurityTools(MagicMock())

    # Both provided
    with pytest.raises(ValueError, match="mutually exclusive"):
        sa._resolve_contingencies(None, contingencies=[], auto_contingencies={})

    # Neither provided
    with pytest.raises(ValueError, match="Provide either"):
        sa._resolve_contingencies(None, contingencies=None, auto_contingencies=None)

    # contingencies not a list (passed as a non-JSON string)
    with pytest.raises(ValueError, match="Invalid JSON for contingencies"):
        sa._resolve_contingencies(None, contingencies="not a list")

    # contingencies not a list (passed as a valid JSON string but not a list)
    with pytest.raises(ValueError, match="must be a list"):
        sa._resolve_contingencies(None, contingencies='{"not": "a list"}')

    # auto_contingencies not a dict (passed as a non-JSON string)
    with pytest.raises(ValueError, match="Invalid JSON for auto_contingencies"):
        sa._resolve_contingencies(None, auto_contingencies="not a dict")

    # auto_contingencies not a dict (passed as a valid JSON string but not a dict)
    with pytest.raises(ValueError, match="must be an object"):
        sa._resolve_contingencies(None, auto_contingencies="[1, 2]")


def test_resolve_contingencies_defaults():
    sa = SecurityTools(MagicMock())
    mock_net = MagicMock()
    mock_net.get_voltage_levels.return_value = pd.DataFrame()
    mock_net.get_lines.return_value = pd.DataFrame(index=["l1"])

    # Test default element_type="line"
    contingencies, source = sa._resolve_contingencies(mock_net, auto_contingencies={})
    assert source == "auto"
    assert len(contingencies) == 1
    assert contingencies[0]["element_id"] == "l1"
    mock_net.get_lines.assert_called_once()


def test_status_name():
    # No status attribute at all
    assert SecurityTools._status_name(object()) == "UNKNOWN"

    # status with a .name attribute (typical pypowsybl enum-like object)
    result_with_name = MagicMock()
    result_with_name.status.name = "CONVERGED"
    assert SecurityTools._status_name(result_with_name) == "CONVERGED"

    # status without a .name attribute, falls back to str()
    class PlainStatus:
        def __str__(self):
            return "SOME_STATUS"

    class ResultWithPlainStatus:
        status = PlainStatus()

    assert SecurityTools._status_name(ResultWithPlainStatus()) == "SOME_STATUS"


def test_limit_type_name():
    # limit_type with a .name attribute
    violation = MagicMock()
    violation.limit_type.name = "CURRENT"
    assert SecurityTools._limit_type_name(violation) == "CURRENT"

    # limit_type present but without a .name attribute
    class ViolationPlainType:
        limit_type = "HIGH_VOLTAGE"

    assert SecurityTools._limit_type_name(ViolationPlainType()) == "HIGH_VOLTAGE"

    # limit_type is None
    class ViolationNoType:
        limit_type = None

    assert SecurityTools._limit_type_name(ViolationNoType()) == "UNKNOWN"

    # no limit_type attribute at all
    assert SecurityTools._limit_type_name(object()) == "UNKNOWN"


def test_loading_and_excess():
    # Normal case
    loading, excess = SecurityTools._loading_and_excess(130.0, 100.0)
    assert loading == pytest.approx(130.0)
    assert excess == pytest.approx(30.0)

    # Non-numeric inputs -> (None, None)
    assert SecurityTools._loading_and_excess("abc", 100.0) == (None, None)
    assert SecurityTools._loading_and_excess(130.0, None) == (None, None)

    # Zero limit -> meaningless ratio -> (None, None)
    assert SecurityTools._loading_and_excess(130.0, 0.0) == (None, None)

    # Dummy "no limit" huge value -> (None, None)
    assert SecurityTools._loading_and_excess(130.0, 1e13) == (None, None)
    assert SecurityTools._loading_and_excess(130.0, -1e13) == (None, None)


def test_register_security_tools():
    proxies = TTLCache(maxsize=10, ttl=3600)
    mock_mcp = MagicMock()
    mock_mcp.tool.return_value = lambda f: f

    register_security_tools(mock_mcp, proxies)

    assert mock_mcp.tool.called
