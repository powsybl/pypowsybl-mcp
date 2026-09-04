#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import Mock

import pandas as pd
import pypowsybl as pp
import pytest

from pypowsybl_mcp.proxy import PyPowsyblMCPServerProxy


class TestPyPowsyblMCPServerProxyInit:
    """Test initialization of PyPowsyblMCPServerProxy."""

    def test_init_creates_empty_state(self):
        """Test that initialization creates empty state dictionaries."""
        proxy = PyPowsyblMCPServerProxy()

        assert proxy.networks == {}
        assert proxy.current_network_id is None
        assert proxy.current_network is None
        assert proxy.loadflow_results == {}
        assert proxy.plugin_results == {}
        assert proxy.lf_params is not None

    def test_init_honors_configured_provider(self, tmp_path, monkeypatch):
        """A non-default `provider` from the TOML config must survive __init__.

        Regression test: __init__ used to call init_lf_params_from_config()
        (which reads `provider` from the config) and then immediately
        overwrite it with the hardcoded default "OpenLoadFlow", silently
        discarding any custom provider.
        """
        config_path = tmp_path / "LF_default_parameters.toml"
        config_path.write_text(
            'voltage_init_mode = "UNIFORM_VALUES"\n'
            'balance_type = "PROPORTIONAL_TO_GENERATION_P_MAX"\n'
            "distributed_slack = true\n"
            'provider = "DynaFlow"\n'
        )
        monkeypatch.setattr(
            "pypowsybl_mcp.proxy.LF_DEFAULT_PARAMETERS_PATH", config_path
        )

        proxy = PyPowsyblMCPServerProxy()

        assert proxy.lf_provider == "DynaFlow"


class TestGetNetworkSummary:
    """Test _get_network_summary method."""

    @pytest.fixture
    def proxy(self):
        """Create a proxy instance for testing."""
        return PyPowsyblMCPServerProxy()

    @pytest.fixture
    def mock_network(self):
        """Create a mock network with typical data."""
        network = Mock()

        # Mock substations
        substations_df = pd.DataFrame({"id": ["SUB1", "SUB2"]})
        network.get_substations.return_value = substations_df

        # Mock buses
        buses_df = pd.DataFrame(
            {"voltage_level_id": ["VL1", "VL2", "VL3"], "v_mag": [1.0, 0.98, 1.02]}
        )
        network.get_buses.return_value = buses_df

        # Mock generators
        generators_df = pd.DataFrame(
            {"id": ["GEN1", "GEN2"], "target_p": [100.0, 150.0]}
        )
        network.get_generators.return_value = generators_df

        # Mock loads
        loads_df = pd.DataFrame(
            {"id": ["LOAD1", "LOAD2", "LOAD3"], "p0": [50.0, 75.0, 100.0]}
        )
        network.get_loads.return_value = loads_df

        # Mock lines
        lines_df = pd.DataFrame({"id": ["LINE1", "LINE2", "LINE3", "LINE4"]})
        network.get_lines.return_value = lines_df

        # Mock transformers
        transformers_df = pd.DataFrame({"id": ["TRAFO1", "TRAFO2"]})
        network.get_2_windings_transformers.return_value = transformers_df

        return network

    def test_get_network_summary_not_found(self, proxy):
        """Test getting summary for non-existent network."""
        result = proxy._get_network_summary("non_existent")

        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_get_network_summary_success(self, proxy, mock_network):
        """Test successful network summary generation."""
        network_id = "test_network"
        proxy.networks[network_id] = mock_network
        proxy.current_network_id = network_id
        proxy.loadflow_results[network_id] = {"status": "converged"}

        result = proxy._get_network_summary(network_id)

        assert result["network_id"] == network_id
        assert result["buses"] == 3
        assert result["generators"] == 2
        assert result["total_generation_mw"] == 250.0
        assert result["loads"] == 3
        assert result["total_load_mw"] == 225.0
        assert result["lines"] == 4
        assert result["2_windings_transformers"] == 2
        assert result["is_current"] is True
        assert result["has_loadflow_results"] is True

    def test_get_network_summary_not_current(self, proxy, mock_network):
        """Test summary when network is not current."""
        network_id = "test_network"
        proxy.networks[network_id] = mock_network
        proxy.current_network_id = "other_network"

        result = proxy._get_network_summary(network_id)

        assert result["is_current"] is False
        assert result["has_loadflow_results"] is False

    def test_get_network_summary_empty_generators(self, proxy):
        """Test summary with no generators."""
        network = Mock()
        network.get_substations.return_value = pd.DataFrame({"id": ["S1"]})
        network.get_buses.return_value = pd.DataFrame({"id": ["B1"]})
        network.get_generators.return_value = pd.DataFrame()
        network.get_loads.return_value = pd.DataFrame({"id": ["L1"], "p0": [100.0]})
        network.get_lines.return_value = pd.DataFrame()
        network.get_2_windings_transformers.return_value = pd.DataFrame()

        network_id = "empty_gen_network"
        proxy.networks[network_id] = network

        result = proxy._get_network_summary(network_id)

        assert result["generators"] == 0
        assert result["total_generation_mw"] == 0

    def test_get_network_summary_empty_loads(self, proxy):
        """Test summary with no loads."""
        network = Mock()
        network.get_substations.return_value = pd.DataFrame({"id": ["S1"]})
        network.get_buses.return_value = pd.DataFrame({"id": ["B1"]})
        network.get_generators.return_value = pd.DataFrame(
            {"id": ["G1"], "target_p": [100.0]}
        )
        network.get_loads.return_value = pd.DataFrame()
        network.get_lines.return_value = pd.DataFrame()
        network.get_2_windings_transformers.return_value = pd.DataFrame()

        network_id = "empty_load_network"
        proxy.networks[network_id] = network

        result = proxy._get_network_summary(network_id)

        assert result["loads"] == 0
        assert result["total_load_mw"] == 0

    def test_get_network_summary_exception_handling(self, proxy):
        """Test error handling when network methods raise exceptions."""
        network = Mock()
        network.get_buses.side_effect = pp.PyPowsyblError("Network access error")

        network_id = "error_network"
        proxy.networks[network_id] = network

        result = proxy._get_network_summary(network_id)

        assert "error" in result
        assert "Error getting network summary" in result["error"]


class TestCreateNetworkVisualization:
    """Test _create_network_visualization method."""

    @pytest.fixture
    def proxy(self):
        """Create a proxy instance for testing."""
        return PyPowsyblMCPServerProxy()

    @pytest.fixture
    def mock_network(self):
        """Create a mock network with write_network_area_diagram_svg method."""
        network = Mock()

        # Mock the write_network_area_diagram_svg method
        def mock_write_svg(path, **kwargs):
            # Write fake SVG data to the file
            with open(path, "wb") as f:
                f.write(b"<svg>fake svg content</svg>")

        network.write_network_area_diagram_svg = Mock(side_effect=mock_write_svg)
        return network

    def test_create_visualization_network_not_found(self, proxy):
        """Test visualization creation for non-existent network."""
        with pytest.raises(ValueError) as exc_info:
            proxy.create_network_visualization_bytes("non_existent")

        assert "not found" in str(exc_info.value).lower()

    def test_create_visualization_success(self, proxy, mock_network):
        """Test successful visualization creation using SVG generation."""
        network_id = "test_network"
        proxy.networks[network_id] = mock_network

        result = proxy.create_network_visualization_bytes(network_id)

        # Verify result is bytes
        assert isinstance(result, bytes)
        assert len(result) > 0

        # Verify write_network_area_diagram_svg was called
        mock_network.write_network_area_diagram_svg.assert_called_once()

    def test_create_visualization_exception_handling(self, proxy):
        """Test that errors raised while generating the SVG are wrapped in a ValueError."""
        network = Mock()
        network.write_network_area_diagram_svg = Mock(
            side_effect=pp.PyPowsyblError("pypowsybl failure")
        )
        network_id = "broken_network"
        proxy.networks[network_id] = network

        with pytest.raises(ValueError) as exc_info:
            proxy.create_network_visualization_bytes(network_id)

        assert "Error creating visualization" in str(exc_info.value)
        assert "pypowsybl failure" in str(exc_info.value)


class TestUpdateLfParams:
    """Test update_lf_params string-to-enum conversions."""

    @pytest.fixture
    def proxy(self):
        return PyPowsyblMCPServerProxy()

    def test_update_lf_params_converts_balance_type_string(self, proxy):
        proxy.update_lf_params(balance_type="PROPORTIONAL_TO_LOAD")

        from pypowsybl_mcp import BALANCE_TYPES

        assert proxy.lf_params.balance_type == BALANCE_TYPES["PROPORTIONAL_TO_LOAD"]

    def test_update_lf_params_converts_connected_component_mode_string(self, proxy):
        # connected_component_mode is deprecated in pypowsybl in favor of
        # component_mode, so update_lf_params maps it to component_mode
        # itself instead of setting it on lf_params directly.
        proxy.update_lf_params(connected_component_mode="ALL")

        from pypowsybl_mcp import COMPONENT_MODES

        assert proxy.lf_params.component_mode == COMPONENT_MODES["ALL_CONNECTED"]


class TestNetworkAccessors:
    """Test the thread-safe network registry accessor methods."""

    @pytest.fixture
    def proxy(self):
        return PyPowsyblMCPServerProxy()

    def test_set_get_has_delete_network(self, proxy):
        network = Mock()

        assert proxy.has_network("net1") is False
        assert proxy.get_network("net1") is None

        proxy.set_network("net1", network)

        assert proxy.has_network("net1") is True
        assert proxy.get_network("net1") is network
        assert "net1" in proxy.network_ids()

        proxy.delete_network("net1")

        assert proxy.has_network("net1") is False
        assert proxy.get_network("net1") is None
        assert "net1" not in proxy.network_ids()

    def test_delete_network_missing_id_is_noop(self, proxy):
        # Should not raise even though the network was never added.
        proxy.delete_network("does_not_exist")
        assert proxy.network_ids() == []

    def test_register_network_sets_as_current_by_default(self, proxy):
        network = Mock()
        proxy.register_network("net1", network)

        assert proxy.get_network("net1") is network
        assert proxy.current_network_id == "net1"
        assert proxy.current_network is network

    def test_register_network_without_setting_current(self, proxy):
        network = Mock()
        proxy.register_network("net1", network, set_as_current=False)

        assert proxy.get_network("net1") is network
        assert proxy.current_network_id is None
        assert proxy.current_network is None

    def test_invalidate_loadflow_drops_cached_result(self, proxy):
        proxy.loadflow_results["net1"] = {"converged": True}
        proxy.invalidate_loadflow("net1")
        assert "net1" not in proxy.loadflow_results

    def test_invalidate_loadflow_missing_id_is_noop(self, proxy):
        # Should not raise even though nothing is cached for the network.
        proxy.invalidate_loadflow("does_not_exist")


class TestPluginResultAccessors:
    """Test the plugin-owned result cache"""

    @pytest.fixture
    def proxy(self):
        return PyPowsyblMCPServerProxy()

    def test_get_missing_key_returns_none(self, proxy):
        assert proxy.get_plugin_result("rte_security:net1") is None

    def test_set_then_get_round_trips(self, proxy):
        proxy.set_plugin_result("rte_security:net1", {"violations": []})

        assert proxy.get_plugin_result("rte_security:net1") == {"violations": []}

    def test_distinct_keys_do_not_collide(self, proxy):
        proxy.set_plugin_result("rte_security:net1", "first")
        proxy.set_plugin_result("other_plugin:net1", "second")

        assert proxy.get_plugin_result("rte_security:net1") == "first"
        assert proxy.get_plugin_result("other_plugin:net1") == "second"


class TestCopy:
    """Test the deep-copy semantics of PyPowsyblMCPServerProxy.copy()."""

    @pytest.fixture
    def proxy(self):
        return PyPowsyblMCPServerProxy()

    def test_copy_duplicates_networks_and_state(self, proxy):
        network = Mock()
        network_id = "net1"
        proxy.set_network(network_id, network)
        proxy.current_network_id = network_id
        proxy.loadflow_results[network_id] = {"status": "converged"}
        proxy.set_plugin_result("rte_security:net1", {"violations": []})

        new_proxy = proxy.copy()

        # Independent proxy instance
        assert new_proxy is not proxy
        assert new_proxy.has_network(network_id)
        # Network was deep-copied, not the same object
        assert new_proxy.get_network(network_id) is not network
        assert new_proxy.current_network_id == network_id
        assert new_proxy.current_network is new_proxy.get_network(network_id)
        assert new_proxy.loadflow_results[network_id] == {"status": "converged"}
        assert (
            new_proxy.loadflow_results[network_id]
            is not proxy.loadflow_results[network_id]
        )
        assert new_proxy.visualization_config == proxy.visualization_config
        assert new_proxy.visualization_config is not proxy.visualization_config
        assert new_proxy.lf_params is not proxy.lf_params
        assert new_proxy.get_plugin_result("rte_security:net1") == {"violations": []}
        assert new_proxy.get_plugin_result(
            "rte_security:net1"
        ) is not proxy.get_plugin_result("rte_security:net1")

    def test_copy_without_current_network_selected(self, proxy):
        # current_network_id is None by default -> copy should not attempt to
        # resolve a current network from the (empty) registry.
        new_proxy = proxy.copy()

        assert new_proxy.current_network_id is None
        assert new_proxy.current_network is None
