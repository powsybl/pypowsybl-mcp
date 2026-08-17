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

from pypowsybl_mcp.tools.sensitivity_tools import (
    SensitivityTools,
    register_sensitivity_tools,
)


class MockContext:
    def __init__(self, session_id="test-session"):
        self.session = MagicMock()
        self.session.session_id = session_id


class MockMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


@pytest.fixture
def pypowsybl_proxies():
    return TTLCache(maxsize=10, ttl=3600)


@pytest.fixture
def sens_tools(pypowsybl_proxies):
    return SensitivityTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


def _make_result(matrix_size=1):
    mock_results = MagicMock()
    cols = [f"v{i}" for i in range(matrix_size)]
    idx = [f"l{i}" for i in range(matrix_size)]
    mock_results.get_sensitivity_matrix.return_value = pd.DataFrame(
        [[0.5] * matrix_size] * matrix_size, columns=cols, index=idx
    )
    mock_results.get_reference_matrix.return_value = pd.DataFrame(
        [[100.0] * matrix_size] * matrix_size, columns=cols, index=idx
    )
    return mock_results


def test_register_sensitivity_tools():
    mcp = MockMCP()
    proxies = TTLCache(maxsize=10, ttl=3600)
    register_sensitivity_tools(mcp, proxies)
    assert "run_dc_sensitivity_analysis" in mcp.tools
    assert "run_ac_sensitivity_analysis" in mcp.tools
    assert "run_psdf_analysis" in mcp.tools
    assert "run_dcdf_analysis" in mcp.tools
    assert "run_ptdf_analysis" in mcp.tools
    assert "run_custom_sensitivity_analysis" in mcp.tools
    # excluded helper should not be registered as a tool
    assert "_handle_sensitivity_result" not in mcp.tools


@pytest.mark.asyncio
async def test_run_dc_sensitivity_analysis_success(sens_tools, mock_ctx):
    proxy = sens_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with (
        patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create,
    ):
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis

        # Mock sensitivity result
        mock_results = MagicMock()
        mock_results.get_sensitivity_matrix.return_value = pd.DataFrame(
            [[0.5]], columns=["v1"], index=["l1"]
        )
        mock_results.get_reference_matrix.return_value = pd.DataFrame(
            [[100.0]], columns=["v1"], index=["l1"]
        )
        mock_analysis.run.return_value = mock_results

        result = await sens_tools.run_dc_sensitivity_analysis(
            network_id="net1", branches_ids=["l1"], variables_ids=["v1"], ctx=mock_ctx
        )

        assert "Sensitivity Analysis Results" in result
        mock_create.assert_called_once()
        mock_analysis.run.assert_called_once()
        # Verify first argument is our mock network
        args, _kwargs = mock_analysis.run.call_args
        assert args[0] == mock_net


@pytest.mark.asyncio
async def test_run_dc_sensitivity_pagination(sens_tools, mock_ctx):
    proxy = sens_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_results = MagicMock()
        mock_results.get_sensitivity_matrix.return_value = pd.DataFrame(
            [[float(i) for i in range(4)] for i in range(4)],
            columns=[f"v{i}" for i in range(4)],
            index=[f"l{i}" for i in range(4)],
        )
        mock_results.get_reference_matrix.return_value = pd.DataFrame(
            [[100.0] * 4] * 4,
            columns=[f"v{i}" for i in range(4)],
            index=[f"l{i}" for i in range(4)],
        )
        mock_analysis.run.return_value = mock_results

        result = await sens_tools.run_dc_sensitivity_analysis(
            network_id="net1",
            branches_ids=["l0"],
            variables_ids=["v0"],
            limit=2,
            cursor="1",
            ctx=mock_ctx,
        )
        data = json.loads(result)
        assert data["pagination"]["returned"] == 2
        assert len(data["sensitivity_matrix"]["data"]) == 2


def _setup_network(sens_tools, network_id="net1", session_id="test-session"):
    proxy = sens_tools.get_proxy(session_id)
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks[network_id] = mock_net
    proxy.current_network_id = network_id
    return proxy, mock_net


@pytest.mark.asyncio
async def test_run_dc_sensitivity_network_not_found(sens_tools, mock_ctx):
    result = await sens_tools.run_dc_sensitivity_analysis(
        network_id="missing", branches_ids=["l1"], variables_ids=["v1"], ctx=mock_ctx
    )
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_dc_sensitivity_no_current_network(sens_tools, mock_ctx):
    # No network_id given and no current network selected: the message must
    # describe the missing selection, not report a network named 'None'.
    result = await sens_tools.run_dc_sensitivity_analysis(
        branches_ids=["l1"], variables_ids=["v1"], ctx=mock_ctx
    )
    assert "No network specified" in result
    assert "None" not in result


@pytest.mark.asyncio
async def test_run_dc_sensitivity_default_network_id(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_dc_sensitivity_analysis(
            branches_ids=["l0"], variables_ids=["v0"], ctx=mock_ctx
        )
        assert "Sensitivity Analysis Results" in result


@pytest.mark.asyncio
async def test_run_dc_sensitivity_with_zones_and_contingencies(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with (
        patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create,
        patch("pypowsybl.sensitivity.create_country_zone") as mock_country_zone,
        patch("pypowsybl.sensitivity.create_empty_zone") as mock_empty_zone,
    ):
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        country_zone = MagicMock()
        mock_country_zone.return_value = country_zone
        empty_zone = MagicMock()
        mock_empty_zone.return_value = empty_zone

        zones = [
            {
                "id": "FR",
                "type": "country",
                "country": "FR",
                "key_type": "GENERATOR_TARGET_P",
                "injections": ["GEN1"],
            },
            {"id": "Z2", "type": "empty", "injections": ["LOAD1"]},
            {"id": "ignored", "type": "unknown"},
        ]

        result = await sens_tools.run_dc_sensitivity_analysis(
            network_id="net1",
            branches_ids=["l0"],
            variables_ids=["v0"],
            zones=zones,
            contingencies=["l0"],
            ctx=mock_ctx,
        )

        assert "Sensitivity Analysis Results" in result
        mock_country_zone.assert_called_once()
        country_zone.add_injection.assert_called_once_with("GEN1")
        empty_zone.add_injection.assert_called_once_with("LOAD1")
        mock_analysis.set_zones.assert_called_once_with([country_zone, empty_zone])
        mock_analysis.add_single_element_contingency.assert_called_once_with("l0")


@pytest.mark.asyncio
async def test_run_dc_sensitivity_analysis_failure(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.side_effect = RuntimeError("boom")

        result = await sens_tools.run_dc_sensitivity_analysis(
            network_id="net1", branches_ids=["l0"], variables_ids=["v0"], ctx=mock_ctx
        )
        assert "DC Sensitivity Analysis failed" in result
        assert "boom" in result


@pytest.mark.asyncio
async def test_run_ac_sensitivity_default_network_id(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_ac_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_ac_sensitivity_analysis(ctx=mock_ctx)
        assert "Sensitivity Analysis Results" in result


@pytest.mark.asyncio
async def test_run_ac_sensitivity_network_not_found(sens_tools, mock_ctx):
    result = await sens_tools.run_ac_sensitivity_analysis(
        network_id="missing", ctx=mock_ctx
    )
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_ac_sensitivity_analysis_success(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_ac_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_ac_sensitivity_analysis(
            network_id="net1",
            branch_flow_factors={"branches_ids": ["l0"], "variables_ids": ["v0"]},
            bus_voltage_factors={
                "bus_ids": ["b0"],
                "target_voltage_ids": ["g0"],
            },
            ctx=mock_ctx,
        )
        assert "Sensitivity Analysis Results" in result
        mock_analysis.add_branch_flow_factor_matrix.assert_called_once_with(
            branches_ids=["l0"], variables_ids=["v0"], matrix_id="m"
        )
        mock_analysis.add_bus_voltage_factor_matrix.assert_called_once_with(
            bus_ids=["b0"], target_voltage_ids=["g0"], matrix_id="m"
        )


@pytest.mark.asyncio
async def test_run_ac_sensitivity_analysis_failure(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_ac_analysis") as mock_create:
        mock_create.side_effect = RuntimeError("ac boom")

        result = await sens_tools.run_ac_sensitivity_analysis(
            network_id="net1", ctx=mock_ctx
        )
        assert "AC Sensitivity Analysis failed" in result
        assert "ac boom" in result


@pytest.mark.asyncio
async def test_run_psdf_analysis_success(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_psdf_analysis(
            network_id="net1",
            branches_ids=["l0"],
            phase_shifter_ids=["ps0"],
            ctx=mock_ctx,
        )
        assert "Sensitivity Analysis Results" in result


@pytest.mark.asyncio
async def test_run_psdf_analysis_default_network_id(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_psdf_analysis(
            branches_ids=["l0"], phase_shifter_ids=["ps0"], ctx=mock_ctx
        )
        assert "Sensitivity Analysis Results" in result


@pytest.mark.asyncio
async def test_run_psdf_analysis_network_not_found(sens_tools, mock_ctx):
    result = await sens_tools.run_psdf_analysis(
        network_id="missing",
        branches_ids=["l0"],
        phase_shifter_ids=["ps0"],
        ctx=mock_ctx,
    )
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_psdf_analysis_failure(sens_tools, mock_ctx):
    _setup_network(sens_tools)
    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_create.side_effect = RuntimeError("psdf boom")
        result = await sens_tools.run_psdf_analysis(
            network_id="net1",
            branches_ids=["l0"],
            phase_shifter_ids=["ps0"],
            ctx=mock_ctx,
        )
        assert "PSDF Analysis failed" in result
        assert "psdf boom" in result


@pytest.mark.asyncio
async def test_run_dcdf_analysis_success(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_dcdf_analysis(
            network_id="net1",
            branches_ids=["l0"],
            hvdc_line_ids=["hvdc0"],
            ctx=mock_ctx,
        )
        assert "Sensitivity Analysis Results" in result


@pytest.mark.asyncio
async def test_run_dcdf_analysis_default_network_id(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_dcdf_analysis(
            branches_ids=["l0"], hvdc_line_ids=["hvdc0"], ctx=mock_ctx
        )
        assert "Sensitivity Analysis Results" in result


@pytest.mark.asyncio
async def test_run_dcdf_analysis_network_not_found(sens_tools, mock_ctx):
    result = await sens_tools.run_dcdf_analysis(
        network_id="missing", branches_ids=["l0"], hvdc_line_ids=["hvdc0"], ctx=mock_ctx
    )
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_dcdf_analysis_failure(sens_tools, mock_ctx):
    _setup_network(sens_tools)
    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_create.side_effect = RuntimeError("dcdf boom")
        result = await sens_tools.run_dcdf_analysis(
            network_id="net1",
            branches_ids=["l0"],
            hvdc_line_ids=["hvdc0"],
            ctx=mock_ctx,
        )
        assert "DCDF Analysis failed" in result
        assert "dcdf boom" in result


@pytest.mark.asyncio
async def test_run_ptdf_analysis_with_string_zones(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with (
        patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create,
        patch("pypowsybl.sensitivity.create_empty_zone") as mock_empty_zone,
    ):
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()
        mock_empty_zone.side_effect = lambda zid: MagicMock(name=zid)

        result = await sens_tools.run_ptdf_analysis(
            network_id="net1",
            branches_ids=["l0"],
            source_zone="FR",
            target_zone="DE",
            ctx=mock_ctx,
        )
        assert "Sensitivity Analysis Results" in result
        assert mock_empty_zone.call_count == 2
        _args, kwargs = mock_analysis.add_branch_flow_factor_matrix.call_args
        assert kwargs["variables_ids"] == [("FR", "DE")]


@pytest.mark.asyncio
async def test_run_ptdf_analysis_with_dict_zones(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with (
        patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create,
        patch("pypowsybl.sensitivity.create_country_zone") as mock_country_zone,
        patch("pypowsybl.sensitivity.create_empty_zone") as mock_empty_zone,
    ):
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()
        mock_country_zone.return_value = MagicMock()
        mock_empty_zone.return_value = MagicMock()

        result = await sens_tools.run_ptdf_analysis(
            network_id="net1",
            branches_ids=["l0"],
            source_zone={"id": "FR", "type": "country", "country": "FR"},
            target_zone={"id": "DE", "type": "empty"},
            ctx=mock_ctx,
        )
        assert "Sensitivity Analysis Results" in result
        mock_country_zone.assert_called_once()
        mock_empty_zone.assert_called_once_with("DE")


@pytest.mark.asyncio
async def test_run_ptdf_analysis_default_network_id(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with (
        patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create,
        patch("pypowsybl.sensitivity.create_empty_zone") as mock_empty_zone,
    ):
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()
        mock_empty_zone.return_value = MagicMock()

        result = await sens_tools.run_ptdf_analysis(
            branches_ids=["l0"], source_zone="FR", target_zone="DE", ctx=mock_ctx
        )
        assert "Sensitivity Analysis Results" in result


@pytest.mark.asyncio
async def test_run_ptdf_analysis_network_not_found(sens_tools, mock_ctx):
    result = await sens_tools.run_ptdf_analysis(
        network_id="missing",
        branches_ids=["l0"],
        source_zone="FR",
        target_zone="DE",
        ctx=mock_ctx,
    )
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_ptdf_analysis_failure(sens_tools, mock_ctx):
    _setup_network(sens_tools)
    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_create.side_effect = RuntimeError("ptdf boom")
        result = await sens_tools.run_ptdf_analysis(
            network_id="net1",
            branches_ids=["l0"],
            source_zone="FR",
            target_zone="DE",
            ctx=mock_ctx,
        )
        assert "PTDF Analysis failed" in result
        assert "ptdf boom" in result


@pytest.mark.asyncio
async def test_run_custom_sensitivity_analysis_dc(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_custom_sensitivity_analysis(
            network_id="net1",
            functions_ids=["l0"],
            variables_ids=["v0"],
            sensitivity_function_type="BRANCH_ACTIVE_POWER_1",
            sensitivity_variable_type="AUTO_DETECT",
            ac=False,
            ctx=mock_ctx,
        )
        assert "Sensitivity Analysis Results" in result
        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_run_custom_sensitivity_analysis_ac(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_ac_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_custom_sensitivity_analysis(
            network_id="net1",
            functions_ids=["l0"],
            variables_ids=["v0"],
            sensitivity_function_type="BRANCH_ACTIVE_POWER_1",
            sensitivity_variable_type="AUTO_DETECT",
            ac=True,
            ctx=mock_ctx,
        )
        assert "Sensitivity Analysis Results" in result
        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_run_custom_sensitivity_analysis_real_dc(sens_tools, mock_ctx):
    """Exercises the real pypowsybl.sensitivity API (no mocking of
    create_dc_analysis/add_factor_matrix) so signature mismatches between
    the wrapper and the installed pypowsybl version are actually caught."""
    proxy = sens_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_ieee14()
    proxy.current_network_id = "net1"

    result = await sens_tools.run_custom_sensitivity_analysis(
        network_id="net1",
        functions_ids=["L1-2-1"],
        variables_ids=["B2-L"],
        sensitivity_function_type="BRANCH_ACTIVE_POWER_1",
        sensitivity_variable_type="AUTO_DETECT",
        ac=False,
        ctx=mock_ctx,
    )
    assert "Sensitivity Analysis Results" in result
    assert "failed" not in result.lower()


@pytest.mark.asyncio
async def test_run_custom_sensitivity_analysis_real_dc_with_contingencies(
    sens_tools, mock_ctx
):
    """Same as above, but with contingencies set - covers the
    contingencies_ids/contingency_context_type arguments required by
    SensitivityAnalysis.add_factor_matrix()."""
    proxy = sens_tools.get_proxy("test-session")
    proxy.networks["net1"] = pp.network.create_ieee14()
    proxy.current_network_id = "net1"

    result = await sens_tools.run_custom_sensitivity_analysis(
        network_id="net1",
        functions_ids=["L1-2-1"],
        variables_ids=["B2-L"],
        sensitivity_function_type="BRANCH_ACTIVE_POWER_1",
        sensitivity_variable_type="AUTO_DETECT",
        ac=False,
        contingencies=["L2-3-1"],
        ctx=mock_ctx,
    )
    assert "Sensitivity Analysis Results" in result
    assert "failed" not in result.lower()


@pytest.mark.asyncio
async def test_run_custom_sensitivity_analysis_default_network_id(sens_tools, mock_ctx):
    _setup_network(sens_tools)

    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_analysis = MagicMock()
        mock_create.return_value = mock_analysis
        mock_analysis.run.return_value = _make_result()

        result = await sens_tools.run_custom_sensitivity_analysis(
            functions_ids=["l0"],
            variables_ids=["v0"],
            sensitivity_function_type="BRANCH_ACTIVE_POWER_1",
            ctx=mock_ctx,
        )
        assert "Sensitivity Analysis Results" in result


@pytest.mark.asyncio
async def test_run_custom_sensitivity_analysis_network_not_found(sens_tools, mock_ctx):
    result = await sens_tools.run_custom_sensitivity_analysis(
        network_id="missing",
        functions_ids=["l0"],
        variables_ids=["v0"],
        sensitivity_function_type="BRANCH_ACTIVE_POWER_1",
        ctx=mock_ctx,
    )
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_custom_sensitivity_analysis_failure(sens_tools, mock_ctx):
    _setup_network(sens_tools)
    with patch("pypowsybl.sensitivity.create_dc_analysis") as mock_create:
        mock_create.side_effect = RuntimeError("custom boom")
        result = await sens_tools.run_custom_sensitivity_analysis(
            network_id="net1",
            functions_ids=["l0"],
            variables_ids=["v0"],
            sensitivity_function_type="BRANCH_ACTIVE_POWER_1",
            ctx=mock_ctx,
        )
        assert "Custom Sensitivity Analysis failed" in result
        assert "custom boom" in result


def test_handle_sensitivity_result_value_error(sens_tools):
    mock_result = MagicMock()
    mock_result.get_sensitivity_matrix.side_effect = ValueError("bad matrix id")

    output = sens_tools._handle_sensitivity_result(mock_result, matrix_id="m")
    data = json.loads(output)
    assert data["success"] is False
    assert "bad matrix id" in data["error"]


def test_handle_sensitivity_result_generic_error(sens_tools):
    mock_result = MagicMock()
    mock_result.get_sensitivity_matrix.side_effect = RuntimeError("unexpected")

    output = sens_tools._handle_sensitivity_result(mock_result, matrix_id="m")
    assert "Error formatting sensitivity results" in output
    assert "unexpected" in output
