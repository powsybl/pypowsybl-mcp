#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pypowsybl as pp
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.network_tools import NetworkTools


class MockContext:
    def __init__(self, session_id="test-session"):
        self.session = MagicMock()
        self.session.session_id = session_id


@pytest.fixture
def pypowsybl_proxies():
    return TTLCache(maxsize=10, ttl=3600)


@pytest.fixture
def network_tools(pypowsybl_proxies):
    return NetworkTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.mark.asyncio
async def test_create_ieee_network_success(network_tools, mock_ctx):
    with patch("pypowsybl.network.create_ieee14") as mock_create:
        mock_net = MagicMock()
        mock_create.return_value = mock_net
        mock_net.get_buses.return_value = ["bus1"] * 14

        result = await network_tools.create_ieee_network(
            network_type="IEEE14", network_id="net1", ctx=mock_ctx
        )

        assert "Successfully created IEEE14 network 'net1'" in result
        proxy = network_tools.get_proxy("test-session")
        assert proxy.networks["net1"] == mock_net


@pytest.mark.asyncio
async def test_switch_network_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net

    result = await network_tools.switch_network(network_id="net1", ctx=mock_ctx)

    assert "Switched to network 'net1'" in result
    assert proxy.current_network_id == "net1"


@pytest.mark.asyncio
async def test_list_networks(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    net1 = MagicMock()
    net1.get_buses.return_value = []
    net2 = MagicMock()
    net2.get_buses.return_value = []
    proxy.networks["net1"] = net1
    proxy.networks["net2"] = net2
    proxy.current_network_id = "net1"

    result = await network_tools.list_networks(ctx=mock_ctx)

    assert "net1 (CURRENT)" in result
    assert "net2" in result


@pytest.mark.asyncio
async def test_get_network_info_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = ["b1", "b2"]
    mock_net.get_lines.return_value = ["l1"]
    mock_net.get_substations.return_value = []
    mock_net.get_generators.return_value = []
    mock_net.get_loads.return_value = []
    mock_net.get_2_term_transformers.return_value = []
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_info(network_id="net1", ctx=mock_ctx)

    assert '"network_id": "net1"' in result
    assert '"buses": 2' in result
    assert '"lines": 1' in result


@pytest.mark.asyncio
async def test_modify_network_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    # Mocking update_loads as an example that is supported
    mock_net.get_loads.return_value = MagicMock()
    # Mocking that the element exists in the dataframe
    mock_net.get_loads.return_value.index = ["l1"]

    result = await network_tools.modify_network(
        element_type="load", element_id="l1", parameter="p0", value=100.0, ctx=mock_ctx
    )

    assert "Updated load 'l1' p0 to 100.0" in result
    mock_net.update_loads.assert_called_once()


@pytest.mark.asyncio
async def test_set_line_status_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = MagicMock()
    # Mocking that the line exists in the dataframe
    mock_net.get_lines.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.set_line_status(
        line_id="l1", active=False, ctx=mock_ctx
    )

    assert "Line 'l1' deactivated in network 'net1'" in result
    mock_net.update_lines.assert_called_once()


@pytest.mark.asyncio
async def test_set_switch_status_open(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_switches.return_value = MagicMock()
    mock_net.get_switches.return_value.index = ["sw1"]
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.set_switch_status(
        switch_id="sw1", open=True, ctx=mock_ctx
    )

    assert "Switch 'sw1' opened in network 'net1'" in result
    mock_net.update_switches.assert_called_once_with(id="sw1", open=True)


@pytest.mark.asyncio
async def test_set_switch_status_close(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_switches.return_value = MagicMock()
    mock_net.get_switches.return_value.index = ["sw1"]
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.set_switch_status(
        switch_id="sw1", open=False, ctx=mock_ctx
    )

    assert "Switch 'sw1' closed in network 'net1'" in result


@pytest.mark.asyncio
async def test_set_switch_status_switch_not_found(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_switches.return_value = MagicMock()
    # The requested switch is not in the network
    mock_net.get_switches.return_value.index = ["sw1"]
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.set_switch_status(
        switch_id="sw_inconnu", open=True, ctx=mock_ctx
    )

    assert "not found" in result
    mock_net.update_switches.assert_not_called()


@pytest.mark.asyncio
async def test_set_switch_status_no_network(network_tools, mock_ctx):
    # No current network and no network_id provided
    result = await network_tools.set_switch_status(
        switch_id="sw1", open=True, ctx=mock_ctx
    )

    assert "No network" in result


@pytest.mark.asyncio
async def test_remove_network_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()

    result = await network_tools.remove_network(network_id="net1", ctx=mock_ctx)

    assert "Removed network 'net1'" in result
    assert "net1" not in proxy.networks


@pytest.mark.asyncio
async def test_variant_management(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    # Clone variant
    result = await network_tools.clone_variant(variant_id="v1", ctx=mock_ctx)
    assert "Variant 'v1' created" in result
    mock_net.clone_variant.assert_called_with("InitialState", "v1")

    # Set working variant
    result = await network_tools.set_working_variant(variant_id="v1", ctx=mock_ctx)
    assert "Switched to variant 'v1'" in result
    mock_net.set_working_variant.assert_called_with("v1")

    # Get working variant
    mock_net.get_working_variant_id.return_value = "v1"
    result = await network_tools.get_working_variant(ctx=mock_ctx)
    assert result == "v1"

    # List variants
    mock_net.get_variant_ids.return_value = ["v1", "InitialState"]
    mock_net.get_working_variant_id.return_value = "v1"
    result = await network_tools.list_variants(ctx=mock_ctx)
    assert '"id": "v1"' in result
    assert '"is_working": true' in result

    # Remove variant
    result = await network_tools.remove_variant(variant_id="v1", ctx=mock_ctx)
    assert "Variant 'v1' removed from network 'net1'" in result
    mock_net.remove_variant.assert_called_with("v1")


def _mock_network_with_buses(v_mag, nominal_v=400.0, low=None, high=None):
    """Bus voltages are in kV, as pypowsybl returns them."""
    count = len(v_mag)
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(
        {
            "v_mag": v_mag,
            "name": [f"b{i}" for i in range(1, count + 1)],
            "voltage_level_id": ["VL1"] * count,
        },
        index=[f"b{i}" for i in range(1, count + 1)],
    )
    mock_net.get_voltage_levels.return_value = pd.DataFrame(
        {
            "nominal_v": [nominal_v],
            "low_voltage_limit": [low if low is not None else float("nan")],
            "high_voltage_limit": [high if high is not None else float("nan")],
        },
        index=["VL1"],
    )
    return mock_net


@pytest.mark.asyncio
async def test_check_voltage_violations_success(network_tools, mock_ctx):
    """Default p.u. bounds are converted to kV with the bus nominal voltage."""
    proxy = network_tools.get_proxy("test-session")
    # 400 kV = 1.00 p.u. (ok), 440 kV = 1.10 p.u. (too high)
    mock_net = _mock_network_with_buses([400.0, 440.0])
    # Mocking that loadflow has already run (so it doesn't try to run it)
    proxy.loadflow_results["net1"] = {"converged": True}

    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.check_voltage_violations(
        network_id="net1", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["success"] is True
    assert data["violation_count"] == 1
    violation = data["violations"][0]
    assert violation["bus_name"] == "b2"
    assert violation["violation_type"] == "HIGH_VOLTAGE"
    assert violation["v_kv"] == 440.0
    assert violation["v_pu"] == 1.1
    assert violation["limit_kv"] == 420.0
    assert violation["limit_source"] == "parameter"


@pytest.mark.asyncio
async def test_check_voltage_violations_uses_network_limits(network_tools, mock_ctx):
    """Limits carried by the voltage level take precedence over the p.u. bounds."""
    proxy = network_tools.get_proxy("test-session")
    # 1.03 p.u. would pass the default bounds but breaches the 405 kV network limit.
    mock_net = _mock_network_with_buses([412.0], low=380.0, high=405.0)
    proxy.loadflow_results["net1"] = {"converged": True}
    proxy.networks["net1"] = mock_net

    result = await network_tools.check_voltage_violations(
        network_id="net1", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["violation_count"] == 1
    assert data["violations"][0]["limit_kv"] == 405.0
    assert data["violations"][0]["limit_source"] == "network"
    assert data["limit_sources"] == {"network": 1, "parameter": 0}


@pytest.mark.asyncio
async def test_check_voltage_violations_ignores_network_limits_on_request(
    network_tools, mock_ctx
):
    """use_network_limits=False falls back to the p.u. bounds."""
    proxy = network_tools.get_proxy("test-session")
    mock_net = _mock_network_with_buses([412.0], low=380.0, high=405.0)
    proxy.loadflow_results["net1"] = {"converged": True}
    proxy.networks["net1"] = mock_net

    result = await network_tools.check_voltage_violations(
        network_id="net1", use_network_limits=False, ctx=mock_ctx
    )

    data = json.loads(result)
    # 412 kV is 1.03 p.u., inside the default 0.95-1.05 band
    assert data["violation_count"] == 0
    assert data["limit_sources"] == {"network": 0, "parameter": 1}


@pytest.mark.asyncio
async def test_check_voltage_violations_kv_unit(network_tools, mock_ctx):
    """Bounds given in kV are applied as such."""
    proxy = network_tools.get_proxy("test-session")
    mock_net = _mock_network_with_buses([412.0])
    proxy.loadflow_results["net1"] = {"converged": True}
    proxy.networks["net1"] = mock_net

    result = await network_tools.check_voltage_violations(
        network_id="net1", min_voltage=380.0, max_voltage=405.0, unit="kv", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["violation_count"] == 1
    assert data["violations"][0]["limit_kv"] == 405.0
    assert data["parameter_limits"]["unit"] == "kv"


@pytest.mark.asyncio
async def test_check_voltage_violations_rejects_bad_unit(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = _mock_network_with_buses([400.0])
    proxy.loadflow_results["net1"] = {"converged": True}

    result = await network_tools.check_voltage_violations(
        network_id="net1", unit="volts", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "volts" in data["error"]


@pytest.mark.asyncio
async def test_check_voltage_violations_skips_islanded_buses(network_tools, mock_ctx):
    """Buses with no solved voltage are reported as not evaluated, not as violations."""
    proxy = network_tools.get_proxy("test-session")
    mock_net = _mock_network_with_buses([400.0, float("nan")])
    proxy.loadflow_results["net1"] = {"converged": True}
    proxy.networks["net1"] = mock_net

    result = await network_tools.check_voltage_violations(
        network_id="net1", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["total_buses"] == 2
    assert data["evaluated_buses"] == 1
    assert data["violation_count"] == 0


@pytest.mark.asyncio
async def test_get_network_element_data_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # Mock generators dataframe
    mock_net.get_generators.return_value = pd.DataFrame(
        {"p": [100.0], "q": [20.0]}, index=["g1"]
    )
    # Ensure InitialState variant exists in mock
    mock_net.get_variant_ids.return_value = ["InitialState"]

    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="generator", ctx=mock_ctx
    )

    assert "g1" in result
    assert "100.0" in result


@pytest.mark.asyncio
async def test_get_only_ids_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # Ensure InitialState variant exists in mock
    mock_net.get_variant_ids.return_value = ["InitialState"]
    # Mock get_elements_ids to return a list of IDs
    mock_net.get_elements_ids.return_value = ["l1", "l2"]

    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="load", get_only_ids=True, ctx=mock_ctx
    )

    assert "l1" in result
    assert "l2" in result


@pytest.mark.asyncio
async def test_get_voltage_level_data_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # Mock voltage levels dataframe
    mock_net.get_voltage_levels.return_value = pd.DataFrame(
        {"nominal_v": [400.0, 225.0]}, index=["VL1", "VL2"]
    )
    # Ensure InitialState variant exists in mock
    mock_net.get_variant_ids.return_value = ["InitialState"]

    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    # Test get_network_element_data for voltage_levels
    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="voltage_level", ctx=mock_ctx
    )
    assert "VL1" in result
    assert "400.0" in result
    assert "VL2" in result

    # Test get_only_ids for voltage_levels (getter-index fallback path)
    result_ids = await network_tools.get_network_element_data(
        network_id="net1", element_type="voltage_level", get_only_ids=True, ctx=mock_ctx
    )
    assert "VL1" in result_ids
    assert "VL2" in result_ids


@pytest.mark.asyncio
async def test_get_network_element_data_pagination(network_tools, mock_ctx):
    """Page 3 (cursor=2) returns GEN_2 and GEN_3 with correct pagination."""
    GENERATOR_COUNT = 5
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value = pd.DataFrame(
        {"p": [float(i) for i in range(GENERATOR_COUNT)]},
        index=[f"GEN_{i}" for i in range(GENERATOR_COUNT)],
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        limit=2,
        cursor="2",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert list(data["elements"].keys()) == ["GEN_2", "GEN_3"]
    assert data["elements"]["GEN_2"]["p"] == 2.0
    assert data["elements"]["GEN_3"]["p"] == 3.0
    assert data["element_type"] == "generator"
    assert data["pagination"] == {
        "limit": 2,
        "cursor": "2",
        "total": GENERATOR_COUNT,
        "returned": 2,
        "nextCursor": "4",
    }
    mock_net.set_working_variant.assert_called_with("InitialState")


@pytest.mark.asyncio
async def test_get_only_ids_pagination_last_page(network_tools, mock_ctx):
    """Last page: single remaining ID, nextCursor is None."""

    LINE_COUNT = 3
    PAGE_LIMIT = 2
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    mock_net.get_elements_ids.return_value = [
        f"LINE_{i}" for i in range(1, LINE_COUNT + 1)
    ]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        get_only_ids=True,
        limit=PAGE_LIMIT,
        cursor="2",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["element_ids"] == [f"LINE_{LINE_COUNT}"]
    assert data["element_type"] == "line"
    assert data["pagination"] == {
        "limit": PAGE_LIMIT,
        "cursor": "2",
        "total": LINE_COUNT,
        "returned": 1,
        "nextCursor": None,
    }


@pytest.mark.asyncio
async def test_check_voltage_violations_pagination(network_tools, mock_ctx):
    """violation_count is global total; violations holds current page only."""
    BUS_COUNT = 4
    PAGE_LIMIT = 2
    V_MIN = 0.95
    V_MAX = 1.05
    NOMINAL_V = 400.0
    # kV values equivalent to 0.90, 0.92, 1.08 and 1.10 p.u. on a 400 kV level
    V_MAG = [360.0, 368.0, 432.0, 440.0]

    proxy = network_tools.get_proxy("test-session")
    mock_net = _mock_network_with_buses(V_MAG, nominal_v=NOMINAL_V)
    proxy.loadflow_results["net1"] = {"converged": True}
    proxy.networks["net1"] = mock_net

    result = await network_tools.check_voltage_violations(
        network_id="net1", limit=PAGE_LIMIT, cursor="0", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["success"] is True
    assert data["parameter_limits"] == {"min": V_MIN, "max": V_MAX, "unit": "pu"}
    assert data["violation_count"] == BUS_COUNT
    low_limit_kv = V_MIN * NOMINAL_V
    assert data["violations"] == [
        {
            "bus_id": "b1",
            "bus_name": "b1",
            "voltage_level_id": "VL1",
            "nominal_v": NOMINAL_V,
            "v_kv": V_MAG[0],
            "v_pu": round(V_MAG[0] / NOMINAL_V, 4),
            "violation_type": "LOW_VOLTAGE",
            "limit_kv": low_limit_kv,
            "limit_source": "parameter",
            "deviation_kv": round(low_limit_kv - V_MAG[0], 3),
        },
        {
            "bus_id": "b2",
            "bus_name": "b2",
            "voltage_level_id": "VL1",
            "nominal_v": NOMINAL_V,
            "v_kv": V_MAG[1],
            "v_pu": round(V_MAG[1] / NOMINAL_V, 4),
            "violation_type": "LOW_VOLTAGE",
            "limit_kv": low_limit_kv,
            "limit_source": "parameter",
            "deviation_kv": round(low_limit_kv - V_MAG[1], 3),
        },
    ]
    assert data["pagination"] == {
        "limit": PAGE_LIMIT,
        "cursor": "0",
        "total": BUS_COUNT,
        "returned": 2,
        "nextCursor": "2",
    }


@pytest.mark.asyncio
async def test_get_network_element_data_filter_full_dataset(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # 8 generators loaded at 10% and 2 generators loaded above 90%.
    p = [10.0] * 8 + [95.0, 96.0]
    mock_net.get_generators.return_value = pd.DataFrame(
        {"p": p, "max_p": [100.0] * 10},
        index=[f"g{i}" for i in range(10)],
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=90,
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is True
    assert data["total_elements"] == 10
    assert data["matched_count"] == 2
    assert set(data["elements"].keys()) == {"g8", "g9"}


@pytest.mark.asyncio
async def test_get_network_element_data_filter_sorted(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {
            "i1": [150.0, 50.0, 200.0, 10.0, 180.0],
            "i2": [-150.0, -50.0, -200.0, -10.0, -180.0],
            "permanent_limit1": [100.0] * 5,
        },
        index=[f"l{i}" for i in range(5)],
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=100,
        sort="desc",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["matched_count"] == 3
    # Most loaded line first because we asked for a descending sort.
    assert list(data["elements"].keys()) == ["l2", "l4", "l0"]
    assert data["elements"]["l2"]["loading_percent"] == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_get_network_element_data_filter_respects_limit(network_tools, mock_ctx):
    """With a limit, filter mode returns only that page, not every matching line."""
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {
            "i1": [150.0, 50.0, 200.0, 10.0, 180.0],
            "i2": [-150.0, -50.0, -200.0, -10.0, -180.0],
            "permanent_limit1": [100.0] * 5,
        },
        index=[f"l{i}" for i in range(5)],
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=0,
        sort="desc",
        limit=2,
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is True
    # All 5 lines match the filter, but only the 2 most loaded are returned.
    assert data["matched_count"] == 5
    assert list(data["elements"].keys()) == ["l2", "l4"]
    assert data["pagination"]["total"] == 5
    assert data["pagination"]["returned"] == 2
    assert data["pagination"]["nextCursor"] == "2"


@pytest.mark.asyncio
async def test_get_network_element_data_filter_caps_without_limit(
    network_tools, mock_ctx
):
    """Without a limit, a wide filter returns one default page, not every line.

    This is the "show the most loaded lines" case: when no limit is given, we
    fall back to the default page size instead of returning everything.
    """
    from pypowsybl_mcp.utils.pagination import DEFAULT_PAGINATION_LIMIT

    n = DEFAULT_PAGINATION_LIMIT + 50
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {
            "i1": [float(i + 1) for i in range(n)],
            "i2": [-float(i + 1) for i in range(n)],
            "permanent_limit1": [1.0] * n,
        },
        index=[f"l{i}" for i in range(n)],
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=0,
        sort="desc",
        # No limit on purpose.
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is True
    # All rows match, but only one default-sized page is returned.
    assert data["matched_count"] == n
    assert len(data["elements"]) == DEFAULT_PAGINATION_LIMIT
    assert data["pagination"]["total"] == n
    assert data["pagination"]["returned"] == DEFAULT_PAGINATION_LIMIT


@pytest.mark.asyncio
async def test_get_network_element_data_filter_pagination_cursor(
    network_tools, mock_ctx
):
    """Second page of a filtered result continues where the first left off."""
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {
            "i1": [150.0, 50.0, 200.0, 10.0, 180.0],
            "i2": [-150.0, -50.0, -200.0, -10.0, -180.0],
            "permanent_limit1": [100.0] * 5,
        },
        index=[f"l{i}" for i in range(5)],
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=0,
        sort="desc",
        limit=2,
        cursor="2",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["matched_count"] == 5
    assert list(data["elements"].keys()) == ["l0", "l1"]
    assert data["pagination"]["cursor"] == "2"


@pytest.mark.asyncio
async def test_get_network_element_data_filter_errors(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value = pd.DataFrame({"p": [1.0]}, index=["g0"])
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        mode="filter",
        metric="loading_percent",
        filter_op="???",
        filter_value=1,
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "filter_op" in data["error"]


@pytest.mark.asyncio
async def test_filter_lines_loading_from_operational_limits(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # get_lines() has flows but no Imax column (real pypowsybl behavior).
    mock_net.get_lines.return_value = pd.DataFrame(
        {"i1": [100.0, 50.0], "i2": [-100.0, -50.0]},
        index=["l1", "l2"],
    )
    mock_net.get_operational_limits.return_value = pd.DataFrame(
        {
            "element_id": ["l1", "l2"],
            "side": ["ONE", "ONE"],
            "type": ["CURRENT", "CURRENT"],
            "acceptable_duration": [-1, -1],
            "value": [100.0, 200.0],
        }
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=90,
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is True
    assert data["matched_count"] == 1
    assert list(data["elements"].keys()) == ["l1"]
    assert data["elements"]["l1"]["loading_percent"] == pytest.approx(100.0)
    assert data["limit_kind"] == "permanent"


@pytest.mark.asyncio
async def test_get_network_element_data_filter_temporary_limit(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_operational_limits.return_value = pd.DataFrame(
        {
            "element_id": ["l1", "l1"],
            "side": ["ONE", "ONE"],
            "type": ["CURRENT", "CURRENT"],
            "acceptable_duration": [-1, 600],
            "value": [100.0, 200.0],
        }
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    # N — état normal : 110 A sur une limite permanente de 100 A -> 110 %.
    mock_net.get_lines.return_value = pd.DataFrame(
        {"i1": [110.0], "i2": [-110.0]},
        index=["l1"],
    )
    result_perm = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=100,
        limit_kind="permanent",
        ctx=mock_ctx,
    )
    data_perm = json.loads(result_perm)
    assert data_perm["matched_count"] == 1
    assert data_perm["elements"]["l1"]["loading_percent"] == pytest.approx(110.0)

    # N-1 — post-contingence : les flux augmentent (180 A) mais la limite
    # temporaire (200 A) est plus haute -> 90 %, donc pas surchargé à 100 %.
    mock_net.get_lines.return_value = pd.DataFrame(
        {"i1": [180.0], "i2": [-180.0]},
        index=["l1"],
    )
    result_temp = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=100,
        limit_kind="temporary",
        ctx=mock_ctx,
    )
    data = json.loads(result_temp)
    assert data["limit_kind"] == "temporary"
    assert data["matched_count"] == 0


@pytest.mark.asyncio
async def test_get_network_element_data_filter_temporary_limit_still_overloaded(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {"i1": [220.0], "i2": [-220.0]},
        index=["l1"],
    )
    mock_net.get_operational_limits.return_value = pd.DataFrame(
        {
            "element_id": ["l1", "l1"],
            "side": ["ONE", "ONE"],
            "type": ["CURRENT", "CURRENT"],
            "acceptable_duration": [-1, 600],
            "value": [100.0, 200.0],
        }
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    # 220 A / limite temporaire 200 A = 110 % -> toujours surchargé en N-1.
    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="line",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=100,
        limit_kind="temporary",
        ctx=mock_ctx,
    )
    data = json.loads(result)
    assert data["matched_count"] == 1
    assert data["elements"]["l1"]["loading_percent"] == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_success(network_tools, mock_ctx):
    """Top k lines sorted by descending |p1|."""
    TOP_K = 2
    P1_MW = {"L_LOW": 85.0, "L_HIGH": 210.0}
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {
            "p1": [P1_MW["L_LOW"], P1_MW["L_HIGH"]],
            "p2": [-P1_MW["L_LOW"], -P1_MW["L_HIGH"]],
            "name": ["tie_line", "interconnector"],
            "bus1_id": ["B1", "B3"],
            "bus2_id": ["B2", "B4"],
        },
        index=["L_LOW", "L_HIGH"],
    )
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", k=TOP_K, ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["k"] == TOP_K
    assert data["flow_side"] == "from (p1)"
    assert [line["id"] for line in data["elements"]] == ["L_HIGH", "L_LOW"]
    assert data["elements"][0]["active_power_mw"] == P1_MW["L_HIGH"]
    assert data["elements"][0]["name"] == "interconnector"
    assert data["elements"][1]["active_power_mw"] == P1_MW["L_LOW"]


@pytest.mark.asyncio
async def test_get_network_element_data_2wt_tap_changer(network_tools, mock_ctx):
    """Two-winding transformer data exposes ratio/phase tap-changer details."""
    import pypowsybl as pp

    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_four_substations_node_breaker_network()
    proxy.current_network_id = "net1"

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="two_windings_transformer", ctx=mock_ctx
    )
    data = json.loads(result)

    twt = data["TWT"]
    assert twt["ratio_tap_position"] == 1
    assert twt["ratio_tap_min"] == 0
    assert twt["ratio_tap_max"] == 2
    assert "ratio_regulated_side" in twt
    assert twt["phase_tap_min"] == 0
    assert twt["phase_tap_max"] == 32


@pytest.mark.asyncio
async def test_get_network_element_data_3wt_tap_changer(network_tools, mock_ctx):
    """Three-winding transformer data exposes per-leg tap-changer range."""
    import pypowsybl as pp

    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_micro_grid_be_network()
    proxy.current_network_id = "net1"

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="three_windings_transformer", ctx=mock_ctx
    )
    data = json.loads(result)

    twt = data["84ed55f4-61f5-4d9d-8755-bba7b877a246"]
    # The tap changer sits on leg TWO of this transformer.
    assert twt["ratio_tap_position2"] == 17
    assert twt["ratio_tap_min2"] == 1
    assert twt["ratio_tap_max2"] == 33


@pytest.mark.asyncio
async def test_set_tap_position_ratio_success(network_tools, mock_ctx):
    import pypowsybl as pp

    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_four_substations_node_breaker_network()
    proxy.current_network_id = "net1"
    # Pre-populate cached loadflow results to exercise the invalidation branch.
    proxy.loadflow_results["net1"] = {"converged": True}

    result = await network_tools.set_tap_position(
        transformer_id="TWT", tap_position=2, ctx=mock_ctx
    )

    assert "Updated ratio tap position of transformer 'TWT' to 2" in result
    assert proxy.networks["net1"].get_ratio_tap_changers().loc["TWT", "tap"] == 2
    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_set_tap_position_out_of_range(network_tools, mock_ctx):
    import pypowsybl as pp

    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_four_substations_node_breaker_network()
    proxy.current_network_id = "net1"

    result = await network_tools.set_tap_position(
        transformer_id="TWT", tap_position=99, ctx=mock_ctx
    )

    assert "out of range" in result


@pytest.mark.asyncio
async def test_set_tap_position_phase_success(network_tools, mock_ctx):
    import pypowsybl as pp

    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_four_substations_node_breaker_network()
    proxy.current_network_id = "net1"

    result = await network_tools.set_tap_position(
        transformer_id="TWT",
        tap_position=20,
        tap_changer_type="phase",
        ctx=mock_ctx,
    )

    assert "Updated phase tap position of transformer 'TWT' to 20" in result
    assert proxy.networks["net1"].get_phase_tap_changers().loc["TWT", "tap"] == 20


@pytest.mark.asyncio
async def test_set_tap_position_no_tap_changer(network_tools, mock_ctx):
    import pypowsybl as pp

    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_ieee14()
    proxy.current_network_id = "net1"

    result = await network_tools.set_tap_position(
        transformer_id="T4-7-1", tap_position=1, ctx=mock_ctx
    )

    assert "no ratio tap changer" in result


@pytest.mark.asyncio
async def test_set_tap_position_3wt_requires_side(network_tools, mock_ctx):
    import pypowsybl as pp

    tid = "84ed55f4-61f5-4d9d-8755-bba7b877a246"
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_micro_grid_be_network()
    proxy.current_network_id = "net1"

    # Without a side, a three-winding transformer cannot be addressed.
    result = await network_tools.set_tap_position(
        transformer_id=tid, tap_position=20, ctx=mock_ctx
    )
    assert "three-winding" in result

    # With the correct side, the update succeeds.
    result = await network_tools.set_tap_position(
        transformer_id=tid, tap_position=20, side="TWO", ctx=mock_ctx
    )
    assert "Updated ratio tap position" in result
    assert proxy.networks["net1"].get_ratio_tap_changers().loc[tid, "tap"] == 20


# ---------------------------------------------------------------------------
# Additional tests to raise coverage of network_tools.py (error/edge branches)
# ---------------------------------------------------------------------------


class MockMCP:
    """Minimal FastMCP stand-in, mirroring the pattern used in test_sensitivity_tools.py."""

    def __init__(self):
        self.tools = {}

    def tool(self, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def test_register_network_tools():
    from pypowsybl_mcp.tools.network_tools import register_network_tools

    mcp = MockMCP()
    proxies = TTLCache(maxsize=10, ttl=3600)
    register_network_tools(mcp, proxies)

    assert "create_ieee_network" in mcp.tools
    assert "get_network_info" in mcp.tools
    assert "modify_network" in mcp.tools
    assert "get_top_active_power_transit_lines" in mcp.tools


# ---------------------------------------------------------------------------
# create_ieee_network
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ieee_network_unsupported_type(network_tools, mock_ctx):
    result = await network_tools.create_ieee_network(
        network_type="IEEE9999", network_id="net1", ctx=mock_ctx
    )

    assert "Unsupported network type" in result


@pytest.mark.asyncio
async def test_create_ieee_network_exception(network_tools, mock_ctx):
    with patch("pypowsybl.network.create_ieee14") as mock_create:
        mock_create.side_effect = pp.PyPowsyblError("boom")

        result = await network_tools.create_ieee_network(
            network_type="IEEE14", network_id="net1", ctx=mock_ctx
        )

    assert "Failed to create network: boom" in result


# ---------------------------------------------------------------------------
# switch_network
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_network_not_found(network_tools, mock_ctx):
    result = await network_tools.switch_network(network_id="missing", ctx=mock_ctx)

    assert "not found" in result
    assert "Available networks" in result


@pytest.mark.asyncio
async def test_switch_network_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy._get_network_summary = MagicMock(
        side_effect=pp.PyPowsyblError("summary boom")
    )

    result = await network_tools.switch_network(network_id="net1", ctx=mock_ctx)

    assert "Failed to switch network: summary boom" in result


# ---------------------------------------------------------------------------
# list_networks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_networks_empty(network_tools, mock_ctx):
    result = await network_tools.list_networks(ctx=mock_ctx)

    assert result == "No networks loaded"


@pytest.mark.asyncio
async def test_list_networks_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy._get_network_summary = MagicMock(side_effect=pp.PyPowsyblError("boom"))

    result = await network_tools.list_networks(ctx=mock_ctx)

    assert "Failed to list networks: boom" in result


# ---------------------------------------------------------------------------
# get_network_info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_network_info_uses_current_network(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = []
    mock_net.get_lines.return_value = []
    mock_net.get_substations.return_value = []
    mock_net.get_generators.return_value = pd.DataFrame({"target_p": []})
    mock_net.get_loads.return_value = pd.DataFrame({"p0": []})
    mock_net.get_2_windings_transformers.return_value = []
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.get_network_info(ctx=mock_ctx)

    assert '"network_id": "net1"' in result


@pytest.mark.asyncio
async def test_get_network_info_no_network_selected(network_tools, mock_ctx):
    result = await network_tools.get_network_info(ctx=mock_ctx)

    assert "No network specified and no current network selected" in result


@pytest.mark.asyncio
async def test_get_network_info_not_found(network_tools, mock_ctx):
    result = await network_tools.get_network_info(network_id="missing", ctx=mock_ctx)

    assert "Network 'missing' not found" in result


@pytest.mark.asyncio
async def test_get_network_info_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy._get_network_summary = MagicMock(
        side_effect=pp.PyPowsyblError("summary boom")
    )

    result = await network_tools.get_network_info(network_id="net1", ctx=mock_ctx)

    assert "Failed to get network info: summary boom" in result


# ---------------------------------------------------------------------------
# modify_network
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modify_network_no_network_selected(network_tools, mock_ctx):
    result = await network_tools.modify_network(
        element_type="load", element_id="l1", parameter="p0", value=1.0, ctx=mock_ctx
    )

    assert "No network specified and no current network selected" in result


@pytest.mark.asyncio
async def test_modify_network_network_not_found(network_tools, mock_ctx):
    result = await network_tools.modify_network(
        element_type="load",
        element_id="l1",
        parameter="p0",
        value=1.0,
        network_id="missing",
        ctx=mock_ctx,
    )

    assert "Network 'missing' not found" in result


@pytest.mark.asyncio
async def test_modify_network_generator_not_found(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value.index = ["g1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="generator",
        element_id="missing",
        parameter="target_p",
        value=10.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Generator 'missing' not found in network 'net1'" in result


@pytest.mark.asyncio
async def test_modify_network_generator_target_p_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value.index = ["g1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="generator",
        element_id="g1",
        parameter="target_p",
        value=150.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Updated generator 'g1' target_p to 150.0 MW in network 'net1'" in result
    mock_net.update_generators.assert_called_once_with(id=["g1"], target_p=[150.0])


@pytest.mark.asyncio
async def test_modify_network_generator_target_v_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value.index = ["g1"]
    proxy.networks["net1"] = mock_net
    # Pre-populate cached loadflow results to exercise the invalidation branch.
    proxy.loadflow_results["net1"] = {"converged": True}

    result = await network_tools.modify_network(
        element_type="generator",
        element_id="g1",
        parameter="target_v",
        value=1.02,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Updated generator 'g1' target_v to 1.02 p.u." in result
    mock_net.update_generators.assert_called_once_with(id=["g1"], target_v=[1.02])
    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_modify_network_generator_unsupported_parameter(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value.index = ["g1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="generator",
        element_id="g1",
        parameter="unknown_param",
        value=1.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Unsupported parameter 'unknown_param' for generator" in result


@pytest.mark.asyncio
async def test_modify_network_load_not_found(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_loads.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="load",
        element_id="missing",
        parameter="p0",
        value=1.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Load 'missing' not found in network 'net1'" in result


@pytest.mark.asyncio
async def test_modify_network_load_q0_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_loads.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="load",
        element_id="l1",
        parameter="q0",
        value=5.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Updated load 'l1' q0 to 5.0 MVAr" in result
    mock_net.update_loads.assert_called_once_with(id=["l1"], q0=[5.0])


@pytest.mark.asyncio
async def test_modify_network_load_unsupported_parameter(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_loads.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="load",
        element_id="l1",
        parameter="unknown",
        value=1.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Unsupported parameter 'unknown' for load" in result


@pytest.mark.asyncio
async def test_modify_network_line_not_found(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="line",
        element_id="missing",
        parameter="r",
        value=1.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Line 'missing' not found in network 'net1'" in result


@pytest.mark.asyncio
async def test_modify_network_line_r_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="line",
        element_id="l1",
        parameter="r",
        value=0.5,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Updated line 'l1' resistance to 0.5" in result
    mock_net.update_lines.assert_called_once_with(id=["l1"], r=[0.5])


@pytest.mark.asyncio
async def test_modify_network_line_x_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="line",
        element_id="l1",
        parameter="x",
        value=0.15,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Updated line 'l1' reactance to 0.15" in result
    mock_net.update_lines.assert_called_once_with(id=["l1"], x=[0.15])


@pytest.mark.asyncio
async def test_modify_network_line_unsupported_parameter(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="line",
        element_id="l1",
        parameter="unknown",
        value=1.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Unsupported parameter 'unknown' for line" in result


@pytest.mark.asyncio
async def test_modify_network_unsupported_element_type(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()

    result = await network_tools.modify_network(
        element_type="transformer",
        element_id="t1",
        parameter="r",
        value=1.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Unsupported element type 'transformer'" in result


@pytest.mark.asyncio
async def test_modify_network_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value.index = ["g1"]
    mock_net.update_generators.side_effect = pp.PyPowsyblError("update boom")
    proxy.networks["net1"] = mock_net

    result = await network_tools.modify_network(
        element_type="generator",
        element_id="g1",
        parameter="target_p",
        value=1.0,
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Failed to modify network: update boom" in result


# ---------------------------------------------------------------------------
# set_line_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_line_status_no_network(network_tools, mock_ctx):
    result = await network_tools.set_line_status(
        line_id="l1", active=True, ctx=mock_ctx
    )

    assert "No network specified and no current network selected" in result


@pytest.mark.asyncio
async def test_set_line_status_network_not_found(network_tools, mock_ctx):
    result = await network_tools.set_line_status(
        line_id="l1", active=True, network_id="missing", ctx=mock_ctx
    )

    assert "Network 'missing' not found" in result


@pytest.mark.asyncio
async def test_set_line_status_line_not_found(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.set_line_status(
        line_id="missing", active=True, network_id="net1", ctx=mock_ctx
    )

    assert "Line 'missing' not found in network 'net1'" in result


@pytest.mark.asyncio
async def test_set_line_status_clears_loadflow_results(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value.index = ["l1"]
    proxy.networks["net1"] = mock_net
    proxy.loadflow_results["net1"] = {"converged": True}

    result = await network_tools.set_line_status(
        line_id="l1", active=True, network_id="net1", ctx=mock_ctx
    )

    assert "Line 'l1' activated in network 'net1'" in result
    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_set_line_status_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value.index = ["l1"]
    mock_net.update_lines.side_effect = pp.PyPowsyblError("update boom")
    proxy.networks["net1"] = mock_net

    result = await network_tools.set_line_status(
        line_id="l1", active=True, network_id="net1", ctx=mock_ctx
    )

    assert "Failed to set line status: update boom" in result


# ---------------------------------------------------------------------------
# set_switch_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_switch_status_network_not_found(network_tools, mock_ctx):
    result = await network_tools.set_switch_status(
        switch_id="sw1", open=True, network_id="missing", ctx=mock_ctx
    )

    assert "Network 'missing' not found" in result


@pytest.mark.asyncio
async def test_set_switch_status_clears_loadflow_results(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_switches.return_value.index = ["sw1"]
    proxy.networks["net1"] = mock_net
    proxy.loadflow_results["net1"] = {"converged": True}

    result = await network_tools.set_switch_status(
        switch_id="sw1", open=True, network_id="net1", ctx=mock_ctx
    )

    assert "Switch 'sw1' opened in network 'net1'" in result
    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_set_switch_status_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_switches.return_value.index = ["sw1"]
    mock_net.update_switches.side_effect = pp.PyPowsyblError("update boom")
    proxy.networks["net1"] = mock_net

    result = await network_tools.set_switch_status(
        switch_id="sw1", open=True, network_id="net1", ctx=mock_ctx
    )

    assert "Failed to set switch status: update boom" in result


# ---------------------------------------------------------------------------
# set_tap_position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_tap_position_no_network(network_tools, mock_ctx):
    result = await network_tools.set_tap_position(
        transformer_id="TWT", tap_position=1, ctx=mock_ctx
    )

    assert "No network specified and no current network selected" in result


@pytest.mark.asyncio
async def test_set_tap_position_network_not_found(network_tools, mock_ctx):
    result = await network_tools.set_tap_position(
        transformer_id="TWT", tap_position=1, network_id="missing", ctx=mock_ctx
    )

    assert "Network 'missing' not found" in result


@pytest.mark.asyncio
async def test_set_tap_position_unsupported_tap_changer_type(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()

    result = await network_tools.set_tap_position(
        transformer_id="TWT",
        tap_position=1,
        tap_changer_type="banana",
        network_id="net1",
        ctx=mock_ctx,
    )

    assert "Unsupported tap_changer_type 'banana'" in result


@pytest.mark.asyncio
async def test_set_tap_position_3wt_wrong_side(network_tools, mock_ctx):
    import pypowsybl as pp

    tid = "84ed55f4-61f5-4d9d-8755-bba7b877a246"
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_micro_grid_be_network()
    proxy.current_network_id = "net1"

    result = await network_tools.set_tap_position(
        transformer_id=tid, tap_position=20, side="THREE", ctx=mock_ctx
    )

    assert "has no ratio tap changer on side 'THREE'" in result


@pytest.mark.asyncio
async def test_set_tap_position_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_ratio_tap_changers.side_effect = pp.PyPowsyblError("tap boom")
    proxy.networks["net1"] = mock_net

    result = await network_tools.set_tap_position(
        transformer_id="TWT", tap_position=1, network_id="net1", ctx=mock_ctx
    )

    assert "Failed to set tap position: tap boom" in result


# ---------------------------------------------------------------------------
# remove_network
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_network_not_found(network_tools, mock_ctx):
    result = await network_tools.remove_network(network_id="missing", ctx=mock_ctx)

    assert result == "Network 'missing' not found"


@pytest.mark.asyncio
async def test_remove_network_switches_to_remaining(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net1 = MagicMock()
    mock_net2 = MagicMock()
    proxy.networks["net1"] = mock_net1
    proxy.networks["net2"] = mock_net2
    proxy.current_network_id = "net1"
    proxy.current_network = mock_net1
    proxy.loadflow_results["net1"] = {"converged": True}

    result = await network_tools.remove_network(network_id="net1", ctx=mock_ctx)

    assert "Removed network 'net1'" in result
    assert proxy.current_network_id == "net2"
    assert proxy.current_network == mock_net2
    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_remove_network_clears_current_when_last(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net1 = MagicMock()
    proxy.networks["net1"] = mock_net1
    proxy.current_network_id = "net1"
    proxy.current_network = mock_net1

    result = await network_tools.remove_network(network_id="net1", ctx=mock_ctx)

    assert "Removed network 'net1'" in result
    assert proxy.current_network_id is None
    assert proxy.current_network is None


@pytest.mark.asyncio
async def test_remove_network_exception(network_tools, mock_ctx):
    class RaisingDict(dict):
        def __delitem__(self, key):
            raise pp.PyPowsyblError("delete boom")

    proxy = network_tools.get_proxy("test-session")
    proxy.networks = RaisingDict({"net1": MagicMock()})

    result = await network_tools.remove_network(network_id="net1", ctx=mock_ctx)

    assert "Failed to remove network: delete boom" in result


# ---------------------------------------------------------------------------
# clone_variant / set_working_variant / get_working_variant / list_variants /
# remove_variant: no-current-network and exception branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_variant_no_network(network_tools, mock_ctx):
    result = await network_tools.clone_variant(variant_id="v1", ctx=mock_ctx)

    assert result == "No network currently loaded."


@pytest.mark.asyncio
async def test_clone_variant_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.clone_variant.side_effect = pp.PyPowsyblError("clone boom")
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.clone_variant(variant_id="v1", ctx=mock_ctx)

    assert "Failed to clone variant: clone boom" in result


@pytest.mark.asyncio
async def test_set_working_variant_no_network(network_tools, mock_ctx):
    result = await network_tools.set_working_variant(variant_id="v1", ctx=mock_ctx)

    assert result == "No network currently loaded."


@pytest.mark.asyncio
async def test_set_working_variant_clears_loadflow_results(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"
    proxy.loadflow_results["net1"] = {"converged": True}

    result = await network_tools.set_working_variant(variant_id="v1", ctx=mock_ctx)

    assert "Switched to variant 'v1'" in result
    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_set_working_variant_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.set_working_variant.side_effect = pp.PyPowsyblError("switch boom")
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.set_working_variant(variant_id="v1", ctx=mock_ctx)

    assert "Failed to set working variant: switch boom" in result


@pytest.mark.asyncio
async def test_get_working_variant_no_network(network_tools, mock_ctx):
    result = await network_tools.get_working_variant(ctx=mock_ctx)

    assert result == "No network currently loaded."


@pytest.mark.asyncio
async def test_get_working_variant_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_working_variant_id.side_effect = pp.PyPowsyblError("get boom")
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.get_working_variant(ctx=mock_ctx)

    assert "Failed to get working variant: get boom" in result


@pytest.mark.asyncio
async def test_list_variants_no_network(network_tools, mock_ctx):
    result = await network_tools.list_variants(ctx=mock_ctx)

    assert result == "No network currently loaded."


@pytest.mark.asyncio
async def test_list_variants_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.side_effect = pp.PyPowsyblError("list boom")
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.list_variants(ctx=mock_ctx)

    assert "Failed to list variants: list boom" in result


@pytest.mark.asyncio
async def test_remove_variant_no_network(network_tools, mock_ctx):
    result = await network_tools.remove_variant(variant_id="v1", ctx=mock_ctx)

    assert result == "No network currently loaded."


@pytest.mark.asyncio
async def test_remove_variant_initial_state_rejected(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await network_tools.remove_variant(variant_id="InitialState", ctx=mock_ctx)

    assert result == "The 'InitialState' variant cannot be removed."


@pytest.mark.asyncio
async def test_remove_variant_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.remove_variant.side_effect = pp.PyPowsyblError("remove boom")
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.remove_variant(variant_id="v1", ctx=mock_ctx)

    assert "Failed to remove variant: remove boom" in result


# ---------------------------------------------------------------------------
# check_voltage_violations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_voltage_violations_uses_current_network(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(
        {"v_mag": [1.0], "name": ["b1"]}, index=["b1"]
    )
    proxy.loadflow_results["net1"] = {"converged": True}
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.check_voltage_violations(ctx=mock_ctx)

    data = json.loads(result)
    assert data["success"] is True
    assert data["network_id"] == "net1"


@pytest.mark.asyncio
async def test_check_voltage_violations_no_network_selected(network_tools, mock_ctx):
    result = await network_tools.check_voltage_violations(ctx=mock_ctx)

    data = json.loads(result)
    assert data["success"] is False
    assert "No network specified" in data["error"]


@pytest.mark.asyncio
async def test_check_voltage_violations_network_not_found(network_tools, mock_ctx):
    result = await network_tools.check_voltage_violations(
        network_id="missing", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "Network 'missing' not found" in data["error"]


@pytest.mark.asyncio
async def test_check_voltage_violations_runs_loadflow_when_missing(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(
        {"v_mag": [1.0], "name": ["b1"]}, index=["b1"]
    )
    proxy.networks["net1"] = mock_net

    mock_res = MagicMock()
    mock_res.status.name = "CONVERGED"
    with patch(
        "pypowsybl_mcp.tools.network_tools.pp.loadflow.run_ac", return_value=[mock_res]
    ) as mock_run:
        result = await network_tools.check_voltage_violations(
            network_id="net1", ctx=mock_ctx
        )

    mock_run.assert_called_once_with(mock_net)
    data = json.loads(result)
    assert data["success"] is True
    assert data["loadflow_executed"] is True
    assert "net1" in proxy.loadflow_results


@pytest.mark.asyncio
async def test_check_voltage_violations_loadflow_not_converged(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(
        {"v_mag": [1.0], "name": ["b1"]}, index=["b1"]
    )
    proxy.networks["net1"] = mock_net

    mock_res = MagicMock()
    mock_res.status.name = "FAILED"
    mock_res.connected_component_num = 0
    with patch(
        "pypowsybl_mcp.tools.network_tools.pp.loadflow.run_ac", return_value=[mock_res]
    ):
        result = await network_tools.check_voltage_violations(
            network_id="net1", ctx=mock_ctx
        )

    data = json.loads(result)
    assert data["success"] is False
    assert data["loadflow_converged"] is False
    assert data["failed_components"] == [{"component_num": 0, "status": "FAILED"}]
    assert "net1" not in proxy.loadflow_results


@pytest.mark.asyncio
async def test_check_voltage_violations_invalid_cursor(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(
        {"v_mag": [1.0], "name": ["b1"]}, index=["b1"]
    )
    proxy.loadflow_results["net1"] = {"converged": True}
    proxy.networks["net1"] = mock_net

    result = await network_tools.check_voltage_violations(
        network_id="net1", limit=10, cursor="not-a-number", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "Invalid cursor" in data["error"]


@pytest.mark.asyncio
async def test_check_voltage_violations_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net

    with patch(
        "pypowsybl_mcp.tools.network_tools.pp.loadflow.run_ac",
        side_effect=pp.PyPowsyblError("lf boom"),
    ):
        result = await network_tools.check_voltage_violations(
            network_id="net1", ctx=mock_ctx
        )

    data = json.loads(result)
    assert data["success"] is False
    assert data["network_id"] == "net1"
    assert "lf boom" in data["error"]


# ---------------------------------------------------------------------------
# get_network_element_data: not-found / validation branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_network_element_data_uses_current_network(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value = pd.DataFrame({"p": [1.0]}, index=["g1"])
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.get_network_element_data(
        element_type="generator", ctx=mock_ctx
    )

    assert "g1" in result


@pytest.mark.asyncio
async def test_get_network_element_data_no_network_selected(network_tools, mock_ctx):
    result = await network_tools.get_network_element_data(
        element_type="generator", ctx=mock_ctx
    )

    assert "No network specified and no current network selected" in result


@pytest.mark.asyncio
async def test_get_network_element_data_network_not_found(network_tools, mock_ctx):
    result = await network_tools.get_network_element_data(
        network_id="missing", element_type="generator", ctx=mock_ctx
    )

    assert "Network 'missing' not found" in result


@pytest.mark.asyncio
async def test_get_network_element_data_element_type_required(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()

    result = await network_tools.get_network_element_data(
        network_id="net1", ctx=mock_ctx
    )

    assert result == "Element type is required"


@pytest.mark.asyncio
async def test_get_network_element_data_variant_not_found(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        variant_id="Bogus",
        element_type="generator",
        ctx=mock_ctx,
    )

    assert "Variant 'Bogus' not found in network 'net1'" in result


@pytest.mark.asyncio
async def test_get_network_element_data_invalid_element_type(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="not_a_type", ctx=mock_ctx
    )

    assert "Invalid element type 'not_a_type'" in result


@pytest.mark.asyncio
async def test_get_network_element_data_method_not_available(network_tools, mock_ctx):
    mock_net = MagicMock(spec=["get_variant_ids", "set_working_variant"])
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="static_var_compensator", ctx=mock_ctx
    )

    assert "not available for this network" in result


@pytest.mark.asyncio
async def test_get_network_element_data_operational_limits_failure_is_tolerated(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {"i1": [10.0], "i2": [-10.0]}, index=["l1"]
    )
    mock_net.get_operational_limits.side_effect = pp.PyPowsyblError("limits boom")
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="line", ctx=mock_ctx
    )

    assert "l1" in result


@pytest.mark.asyncio
async def test_get_network_element_data_tap_changer_failures_are_tolerated(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_2_windings_transformers.return_value = pd.DataFrame(
        {"r": [1.0]}, index=["t1"]
    )
    mock_net.get_ratio_tap_changers.side_effect = pp.PyPowsyblError("ratio boom")
    mock_net.get_phase_tap_changers.side_effect = pp.PyPowsyblError("phase boom")
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="two_windings_transformer", ctx=mock_ctx
    )

    assert "t1" in result


@pytest.mark.asyncio
async def test_get_network_element_data_compare_filter_conflict(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState", "v2"]
    mock_net.get_generators.return_value = pd.DataFrame({"p": [1.0]}, index=["g1"])
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        mode="filter",
        compare_with_variant_id="v2",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "mode='filter' cannot be combined" in data["error"]


@pytest.mark.asyncio
async def test_get_network_element_data_compare_variant_not_found(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    mock_net.get_generators.return_value = pd.DataFrame({"p": [1.0]}, index=["g1"])
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        compare_with_variant_id="missing_variant",
        ctx=mock_ctx,
    )

    assert "Variant 'missing_variant' not found in network 'net1'" in result


@pytest.mark.asyncio
async def test_get_network_element_data_compare_success(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState", "v2"]
    df_initial = pd.DataFrame({"p": [100.0]}, index=["g1"])
    df_v2 = pd.DataFrame({"p": [150.0]}, index=["g1"])
    mock_net.get_generators.side_effect = [df_initial, df_v2]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        compare_with_variant_id="v2",
        ctx=mock_ctx,
    )

    assert "g1" in result
    mock_net.set_working_variant.assert_any_call("v2")


@pytest.mark.asyncio
async def test_get_network_element_data_filter_pagination_invalid_cursor(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value = pd.DataFrame(
        {"p": [10.0, 95.0], "max_p": [100.0, 100.0]}, index=["g0", "g1"]
    )
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        mode="filter",
        metric="loading_percent",
        filter_op=">",
        filter_value=0,
        cursor="bogus-cursor",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "Invalid cursor" in data["error"]


@pytest.mark.asyncio
async def test_get_network_element_data_unsupported_mode(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value = pd.DataFrame({"p": [1.0]}, index=["g1"])
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="generator", mode="bogus", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "Unsupported mode 'bogus'" in data["error"]


@pytest.mark.asyncio
async def test_get_network_element_data_list_pagination_invalid_cursor(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_generators.return_value = pd.DataFrame({"p": [1.0]}, index=["g1"])
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        cursor="bogus-cursor",
        limit=1,
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "Invalid cursor" in data["error"]


@pytest.mark.asyncio
async def test_get_network_element_data_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    mock_net.get_generators.side_effect = pp.PyPowsyblError("data boom")
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="generator", ctx=mock_ctx
    )

    assert "Failed to get network element data: data boom" in result


# ---------------------------------------------------------------------------
# get_network_element_data(get_only_ids=True): not-found / validation branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_only_ids_uses_current_network(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    mock_net.get_elements_ids.return_value = ["g1"]
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.get_network_element_data(
        element_type="generator", get_only_ids=True, ctx=mock_ctx
    )

    assert "g1" in result


@pytest.mark.asyncio
async def test_get_only_ids_no_network_selected(network_tools, mock_ctx):
    result = await network_tools.get_network_element_data(
        element_type="generator", get_only_ids=True, ctx=mock_ctx
    )

    assert "No network specified and no current network selected" in result


@pytest.mark.asyncio
async def test_get_only_ids_network_not_found(network_tools, mock_ctx):
    result = await network_tools.get_network_element_data(
        network_id="missing", element_type="generator", get_only_ids=True, ctx=mock_ctx
    )

    assert "Network 'missing' not found" in result


@pytest.mark.asyncio
async def test_get_only_ids_element_type_required(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()

    result = await network_tools.get_network_element_data(
        network_id="net1", get_only_ids=True, ctx=mock_ctx
    )

    assert result == "Element type is required"


@pytest.mark.asyncio
async def test_get_only_ids_method_not_available(network_tools, mock_ctx):
    # Has the variant API but not the element getter, so validation passes and
    # the missing-getter branch is reached.
    mock_net = MagicMock(spec=["get_variant_ids", "set_working_variant"])
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="switch", get_only_ids=True, ctx=mock_ctx
    )

    assert "not available for this network" in result


@pytest.mark.asyncio
async def test_get_only_ids_invalid_element_type(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="not_a_type", get_only_ids=True, ctx=mock_ctx
    )

    assert "Invalid element type 'not_a_type'" in result


@pytest.mark.asyncio
async def test_get_only_ids_invalid_cursor(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    mock_net.get_elements_ids.return_value = ["g1", "g2"]
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1",
        element_type="generator",
        get_only_ids=True,
        limit=1,
        cursor="bogus-cursor",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "Invalid cursor" in data["error"]


@pytest.mark.asyncio
async def test_get_only_ids_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_variant_ids.return_value = ["InitialState"]
    mock_net.get_elements_ids.side_effect = pp.PyPowsyblError("ids boom")
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_network_element_data(
        network_id="net1", element_type="generator", get_only_ids=True, ctx=mock_ctx
    )

    assert "Failed to get network element data: ids boom" in result


# ---------------------------------------------------------------------------
# get_top_active_power_transit_lines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_uses_current_network(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {"p1": [10.0], "p2": [-10.0], "name": ["l1"]}, index=["l1"]
    )
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await network_tools.get_top_active_power_transit_lines(ctx=mock_ctx)

    data = json.loads(result)
    assert data["network_id"] == "net1"


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_no_network_selected(
    network_tools, mock_ctx
):
    result = await network_tools.get_top_active_power_transit_lines(ctx=mock_ctx)

    assert "No network specified and no current network selected" in result


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_network_not_found(
    network_tools, mock_ctx
):
    result = await network_tools.get_top_active_power_transit_lines(
        network_id="missing", ctx=mock_ctx
    )

    assert "Network 'missing' not found" in result


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_k_none_defaults_to_10(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {"p1": [10.0], "p2": [-10.0]}, index=["l1"]
    )
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", k=None, ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["k"] == 10


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_k_non_numeric_defaults_to_10(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {"p1": [10.0], "p2": [-10.0]}, index=["l1"]
    )
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", k="not-a-number", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["k"] == 10


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_negative_k_clamped_to_zero(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {"p1": [10.0], "p2": [-10.0]}, index=["l1"]
    )
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", k=-5, ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["k"] == 0
    assert data["elements"] == []


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_k_capped_at_50(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    n = 60
    mock_net.get_lines.return_value = pd.DataFrame(
        {"p1": [float(i) for i in range(n)], "p2": [-float(i) for i in range(n)]},
        index=[f"l{i}" for i in range(n)],
    )
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", k=1000, ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["k"] == 50
    assert len(data["elements"]) == 50


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_unsupported_flow_side(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {"p1": [10.0], "p2": [-10.0]}, index=["l1"]
    )
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", flow_side="sideways", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["flow_side"] == "from (p1)"


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_runs_loadflow_when_missing(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # First call: no p1/p2 columns at all -> triggers loadflow.
    lines_before = pd.DataFrame({"other": [1.0]}, index=["l1"])
    lines_after = pd.DataFrame({"p1": [42.0], "p2": [-42.0]}, index=["l1"])
    mock_net.get_lines.side_effect = [lines_before, lines_after]
    proxy.networks["net1"] = mock_net

    with patch("pypowsybl_mcp.tools.network_tools.pp.loadflow.run_ac") as mock_run:
        result = await network_tools.get_top_active_power_transit_lines(
            network_id="net1", ctx=mock_ctx
        )

    mock_run.assert_called_once_with(mock_net)
    data = json.loads(result)
    assert data["elements"][0]["active_power_mw"] == 42.0


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_loadflow_failure_tolerated(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # p1/p2 never appear, even after the (failing) loadflow attempt.
    mock_net.get_lines.return_value = pd.DataFrame({"other": [1.0]}, index=["l1"])
    proxy.networks["net1"] = mock_net

    with patch(
        "pypowsybl_mcp.tools.network_tools.pp.loadflow.run_ac",
        side_effect=pp.PyPowsyblError("lf boom"),
    ):
        result = await network_tools.get_top_active_power_transit_lines(
            network_id="net1", ctx=mock_ctx
        )

    assert "not available" in result


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_missing_name_and_bus_columns(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # No 'name', 'bus1_id', 'bus2_id' columns present.
    mock_net.get_lines.return_value = pd.DataFrame(
        {"p1": [10.0], "p2": [-10.0]}, index=["l1"]
    )
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["elements"][0]["name"] is None
    assert data["elements"][0]["from_bus_id"] is None
    assert data["elements"][0]["to_bus_id"] is None


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_k_zero_returns_no_elements(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {"p1": [10.0, 20.0], "p2": [-10.0, -20.0]}, index=["l1", "l2"]
    )
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", k=0, ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["elements"] == []


@pytest.mark.asyncio
async def test_get_top_active_power_transit_lines_exception(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.side_effect = pp.PyPowsyblError("lines boom")
    proxy.networks["net1"] = mock_net

    result = await network_tools.get_top_active_power_transit_lines(
        network_id="net1", ctx=mock_ctx
    )

    assert "Failed to compute top active power transit lines: lines boom" in result
