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

from pypowsybl_mcp.tools.loadflow_tools import LoadflowTools, register_loadflow_tools


class MockContext:
    def __init__(self, session_id="test-session"):
        self.session = MagicMock()
        self.session.session_id = session_id


@pytest.fixture
def pypowsybl_proxies():
    return TTLCache(maxsize=10, ttl=3600)


@pytest.fixture
def lf_tools(pypowsybl_proxies):
    return LoadflowTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.mark.asyncio
async def test_get_loadflow_params(lf_tools, mock_ctx):
    result = await lf_tools.get_loadflow_params(ctx=mock_ctx)
    assert '"voltageInitMode" : "UNIFORM_VALUES"' in result


@pytest.mark.asyncio
async def test_update_loadflow_params(lf_tools, mock_ctx):
    # Update some params
    result = await lf_tools.update_loadflow_params(
        voltage_init_mode="DC_VALUES", distributed_slack=True, ctx=mock_ctx
    )

    assert '"success": true' in result
    assert "DC_VALUES" in result
    assert (
        '"distributedSlack" : true' in result
        or '"distributed_slack": true' in result
        or '"distributedSlack": true' in result
    )

    # Verify state in proxy
    proxy = lf_tools.get_proxy("test-session")
    assert proxy.lf_params.voltage_init_mode.name == "DC_VALUES"
    assert proxy.lf_params.distributed_slack is True


@pytest.mark.asyncio
async def test_update_loadflow_params_additional(lf_tools, mock_ctx):
    # Update new params
    result = await lf_tools.update_loadflow_params(
        countries_to_balance=["FR", "BE"],
        component_mode="ALL_CONNECTED",
        dc_power_factor=0.95,
        ctx=mock_ctx,
    )

    assert '"success": true' in result
    assert "FR" in result
    assert "BE" in result
    assert "ALL_CONNECTED" in result

    # Verify state in proxy
    proxy = lf_tools.get_proxy("test-session")
    assert proxy.lf_params.countries_to_balance == ["FR", "BE"]
    assert proxy.lf_params.component_mode.name == "ALL_CONNECTED"
    assert proxy.lf_params.dc_power_factor == 0.95


@pytest.mark.asyncio
async def test_restore_default_loadflow_param(lf_tools, mock_ctx):
    # 1. First, update some params to non-default values
    await lf_tools.update_loadflow_params(
        voltage_init_mode="DC_VALUES", distributed_slack=False, ctx=mock_ctx
    )

    proxy = lf_tools.get_proxy("test-session")
    assert proxy.lf_params.voltage_init_mode.name == "DC_VALUES"
    assert proxy.lf_params.distributed_slack is False

    # 2. Restore defaults
    result = await lf_tools.restore_default_loadflow_param(ctx=mock_ctx)

    assert '"success": true' in result
    assert "Load flow parameters reinitialized" in result

    # 3. Verify they are back to defaults (UNIFORM_VALUES and True for distributed_slack)
    assert proxy.lf_params.voltage_init_mode.name == "UNIFORM_VALUES"
    assert proxy.lf_params.distributed_slack is True

    # Check JSON output (pypowsybl to_json uses camelCase)
    assert (
        '"voltageInitMode": "UNIFORM_VALUES"' in result
        or '"voltageInitMode" : "UNIFORM_VALUES"' in result
    )
    assert '"distributedSlack": true' in result or '"distributedSlack" : true' in result


@pytest.mark.asyncio
async def test_run_loadflow_success(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(columns=["name", "v_mag", "v_angle"])
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with (
        patch("pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.run_ac") as mock_run,
        patch("pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.Parameters"),
    ):
        mock_res = MagicMock()
        mock_res.status.name = "CONVERGED"
        mock_run.return_value = [mock_res]

        result = await lf_tools.run_loadflow(network_id="net1", ctx=mock_ctx)

        assert '"success": true' in result
        assert '"converged": true' in result
        assert "net1" in proxy.loadflow_results
        assert proxy.loadflow_results["net1"]["converged"] is True


@pytest.mark.asyncio
async def test_run_loadflow_failure(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with (
        patch("pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.run_ac") as mock_run,
        patch("pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.Parameters"),
    ):
        mock_res = MagicMock()
        mock_res.status.name = "FAILED"
        mock_res.connected_component_num = 1
        mock_run.return_value = [mock_res]

        result = await lf_tools.run_loadflow(network_id="net1", ctx=mock_ctx)

        assert '"success": false' in result
        assert '"converged": false' in result
        # The code STORES the result even if it failed
        assert "net1" in proxy.loadflow_results
        assert proxy.loadflow_results["net1"]["converged"] is False


@pytest.mark.asyncio
async def test_get_loadflow_provider_info(lf_tools, mock_ctx):
    with (
        patch(
            "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_provider_names",
            return_value=["OpenLoadFlow", "DynaFlow"],
        ),
        patch(
            "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_default_provider",
            return_value="OpenLoadFlow",
        ),
        patch(
            "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_provider_parameters",
            return_value=pd.DataFrame(
                {
                    "slackBusSelectionMode": {
                        "description": "Slack bus selection mode",
                        "type": "STRING",
                        "default": "MOST_MESHED",
                        "possible_values": "[FIRST, MOST_MESHED, NAME, LARGEST_GENERATOR]",
                    },
                },
            ),
        ),
    ):
        out = await lf_tools.get_loadflow_provider_info(ctx=mock_ctx)
        assert '"success": true' in out
        assert "OpenLoadFlow" in out
        assert "DynaFlow" in out
        assert '"default_provider": "OpenLoadFlow"' in out
        assert '"session_provider": "OpenLoadFlow"' in out
        assert "providers_details" in out


@pytest.mark.asyncio
async def test_set_loadflow_provider_valid(lf_tools, mock_ctx):
    with patch(
        "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_provider_names",
        return_value=["OpenLoadFlow", "DynaFlow", "Hades2"],
    ):
        out = await lf_tools.set_loadflow_provider(provider="Hades2", ctx=mock_ctx)
        assert '"success": true' in out
        assert '"previous_provider": "OpenLoadFlow"' in out
        assert '"new_provider": "Hades2"' in out
        # Verify it is stored in the proxy
        proxy = lf_tools.get_proxy("test-session")
        assert proxy.lf_provider == "Hades2"


@pytest.mark.asyncio
async def test_set_loadflow_provider_invalid(lf_tools, mock_ctx):
    with patch(
        "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_provider_names",
        return_value=["OpenLoadFlow", "DynaFlow"],
    ):
        out = await lf_tools.set_loadflow_provider(
            provider="NonExistentProvider", ctx=mock_ctx
        )
        assert '"success": false' in out
        assert "not found" in out
        assert "available_providers" in out


@pytest.mark.asyncio
async def test_set_loadflow_provider_reset_to_default(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    proxy.lf_provider = "OpenLoadFlow"
    # Reset to default with empty string
    out = await lf_tools.set_loadflow_provider(provider="", ctx=mock_ctx)
    assert '"success": true' in out
    assert '"previous_provider": "OpenLoadFlow"' in out
    assert '"new_provider": ""' in out
    assert proxy.lf_provider == ""


@pytest.mark.asyncio
async def test_run_loadflow_uses_session_provider(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(columns=["name", "v_mag", "v_angle"])
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"
    proxy.lf_provider = "DynaFlow"

    with (
        patch("pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.run_ac") as mock_run,
    ):
        mock_res = MagicMock()
        mock_res.status.name = "CONVERGED"
        mock_run.return_value = [mock_res]

        result_str = await lf_tools.run_loadflow(network_id="net1", ctx=mock_ctx)
        assert '"success": true' in result_str
        # Verify session provider was used
        call_kwargs = mock_run.call_args
        assert call_kwargs[1]["provider"] == "DynaFlow"
        assert proxy.loadflow_results["net1"]["provider"] == "DynaFlow"


def test_register_loadflow_tools():
    proxies = TTLCache(maxsize=10, ttl=3600)
    mock_mcp = MagicMock()
    mock_mcp.tool.return_value = lambda f: f

    register_loadflow_tools(mock_mcp, proxies)

    # register_tools_with_mcp calls mcp.tool() once per public method
    assert mock_mcp.tool.called


@pytest.mark.asyncio
async def test_get_loadflow_provider_info_single_provider_error(lf_tools, mock_ctx):
    """One provider raising while listing its parameters must not break the whole call."""

    def fake_params(provider_name):
        if provider_name == "BadProvider":
            raise pp.PyPowsyblError("cannot introspect provider")
        return pd.DataFrame({"p": {"description": "d", "type": "t"}})

    with (
        patch(
            "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_provider_names",
            return_value=["OpenLoadFlow", "BadProvider"],
        ),
        patch(
            "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_default_provider",
            return_value="OpenLoadFlow",
        ),
        patch(
            "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_provider_parameters",
            side_effect=fake_params,
        ),
    ):
        out = await lf_tools.get_loadflow_provider_info(ctx=mock_ctx)

    data = json.loads(out)
    assert data["success"] is True
    assert "error" in data["providers_details"]["BadProvider"]
    assert "OpenLoadFlow" in data["providers_details"]


@pytest.mark.asyncio
async def test_get_loadflow_provider_info_outer_exception(lf_tools, mock_ctx):
    with patch(
        "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_provider_names",
        side_effect=pp.PyPowsyblError("boom"),
    ):
        out = await lf_tools.get_loadflow_provider_info(ctx=mock_ctx)

    data = json.loads(out)
    assert data["success"] is False
    assert "boom" in data["error"]


@pytest.mark.asyncio
async def test_set_loadflow_provider_exception(lf_tools, mock_ctx):
    with patch(
        "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.get_provider_names",
        side_effect=pp.PyPowsyblError("boom"),
    ):
        out = await lf_tools.set_loadflow_provider(
            provider="OpenLoadFlow", ctx=mock_ctx
        )

    data = json.loads(out)
    assert data["success"] is False
    assert "boom" in data["error"]


@pytest.mark.asyncio
async def test_get_loadflow_params_exception(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    proxy.lf_params = MagicMock()
    proxy.lf_params.to_json.side_effect = pp.PyPowsyblError("cannot serialize")

    with pytest.raises(pp.PyPowsyblError, match="cannot serialize"):
        await lf_tools.get_loadflow_params(ctx=mock_ctx)


@pytest.mark.asyncio
async def test_restore_default_loadflow_param_exception(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    proxy.init_lf_params_from_config = MagicMock(
        side_effect=pp.PyPowsyblError("cannot reset")
    )

    result = await lf_tools.restore_default_loadflow_param(ctx=mock_ctx)

    data = json.loads(result)
    assert data["success"] is False
    assert "cannot reset" in data["error"]


@pytest.mark.asyncio
async def test_update_loadflow_params_exception(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    proxy.update_lf_params = MagicMock(side_effect=pp.PyPowsyblError("cannot update"))

    result = await lf_tools.update_loadflow_params(
        voltage_init_mode="DC_VALUES", ctx=mock_ctx
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "cannot update" in data["error"]


@pytest.mark.asyncio
async def test_run_loadflow_uses_current_network_id_when_none(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(columns=["name", "v_mag", "v_angle"])
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.run_ac") as mock_run:
        mock_res = MagicMock()
        mock_res.status.name = "CONVERGED"
        mock_run.return_value = [mock_res]

        result = await lf_tools.run_loadflow(network_id=None, ctx=mock_ctx)

    data = json.loads(result)
    assert data["success"] is True
    assert data["network_id"] == "net1"


@pytest.mark.asyncio
async def test_run_loadflow_no_network_selected(lf_tools, mock_ctx):
    result = await lf_tools.run_loadflow(network_id=None, ctx=mock_ctx)

    data = json.loads(result)
    assert data["success"] is False
    assert "no current network" in data["error"].lower()


@pytest.mark.asyncio
async def test_run_loadflow_network_not_found(lf_tools, mock_ctx):
    result = await lf_tools.run_loadflow(network_id="missing", ctx=mock_ctx)

    data = json.loads(result)
    assert data["success"] is False
    assert "missing" in data["error"]


@pytest.mark.asyncio
async def test_run_loadflow_dc_mode_with_bus_samples(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_buses.return_value = pd.DataFrame(
        {"name": ["B1", "B2"], "v_mag": [1.05, 0.98], "v_angle": [-2.5, 3.1]}
    )
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.run_dc") as mock_run:
        mock_res = MagicMock()
        mock_res.status.name = "CONVERGED"
        mock_run.return_value = [mock_res]

        result = await lf_tools.run_loadflow(network_id="net1", dc=True, ctx=mock_ctx)

    data = json.loads(result)
    assert data["success"] is True
    assert data["dc"] is True
    mock_run.assert_called_once()
    assert len(data["sample_bus_voltages"]) == 2
    assert data["sample_bus_voltages"][0]["name"] == "B1"
    assert data["sample_bus_voltages"][0]["v_mag"] == pytest.approx(1.05)
    assert data["sample_bus_voltages"][0]["v_angle"] == pytest.approx(-2.5)


@pytest.mark.asyncio
async def test_run_loadflow_exception(lf_tools, mock_ctx):
    proxy = lf_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch(
        "pypowsybl_mcp.tools.loadflow_tools.pp.loadflow.run_ac",
        side_effect=pp.PyPowsyblError("solver crashed"),
    ):
        result = await lf_tools.run_loadflow(network_id="net1", ctx=mock_ctx)

    data = json.loads(result)
    assert data["success"] is False
    assert data["network_id"] == "net1"
    assert "solver crashed" in data["error"]
