#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.utils.visualization import (
    VisualizationTools,
    register_visualization_tools,
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
def viz_tools(pypowsybl_proxies):
    return VisualizationTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.mark.asyncio
async def test_plot_substation_single_line_diagram_success(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # Mock substation exists
    mock_net.get_substations.return_value = MagicMock()
    mock_net.get_substations.return_value.index = ["sub1"]
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    # Mock get_single_line_diagram
    mock_sld = MagicMock()
    mock_sld.svg = "<svg></svg>"
    mock_net.get_single_line_diagram.return_value = mock_sld

    with (
        patch("pypowsybl.network.SldParameters"),
        patch(
            "pypowsybl_mcp.tools.utils.visualization.generate_download_link"
        ) as mock_gen_link,
    ):
        mock_gen_link.return_value = {
            "download_url": "http://localhost/download/sub1.svg"
        }

        result = await viz_tools.plot_substation_single_line_diagram(
            network_id="net1", substation_id="sub1", ctx=mock_ctx
        )

        assert result == "http://localhost/download/sub1.svg"
        mock_net.get_single_line_diagram.assert_called_once()


@pytest.mark.asyncio
async def test_visualize_network_success(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with (
        patch.object(proxy, "create_network_visualization_bytes") as mock_viz,
        patch(
            "pypowsybl_mcp.tools.utils.visualization.generate_download_link"
        ) as mock_gen_link,
    ):
        mock_viz.return_value = b"<svg></svg>"
        mock_gen_link.return_value = {
            "download_url": "http://localhost/download/net1.svg"
        }

        result = await viz_tools.visualize_network(network_id="net1", ctx=mock_ctx)

        assert result == "http://localhost/download/net1.svg"
        mock_viz.assert_called_once_with("net1")


def test_register_visualization_tools():
    mcp = MockMCP()
    proxies = TTLCache(maxsize=10, ttl=3600)
    register_visualization_tools(mcp, proxies)
    assert "plot_substation_single_line_diagram" in mcp.tools
    assert "visualize_network" in mcp.tools


@pytest.mark.asyncio
async def test_plot_substation_uses_current_network_id(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_sld = MagicMock()
    mock_sld.svg = "<svg></svg>"
    mock_net.get_single_line_diagram.return_value = mock_sld
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with (
        patch("pypowsybl.network.SldParameters"),
        patch(
            "pypowsybl_mcp.tools.utils.visualization.generate_download_link"
        ) as mock_gen_link,
    ):
        mock_gen_link.return_value = {"download_url": "http://localhost/download/x"}

        # network_id not provided -> should fall back to current_network_id
        result = await viz_tools.plot_substation_single_line_diagram(
            substation_id="sub1", ctx=mock_ctx
        )

        assert result == "http://localhost/download/x"


@pytest.mark.asyncio
async def test_plot_substation_no_network_selected(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    proxy.current_network_id = None

    result = await viz_tools.plot_substation_single_line_diagram(
        substation_id="sub1", ctx=mock_ctx
    )

    assert result == "No network specified and no current network selected"


@pytest.mark.asyncio
async def test_plot_substation_network_not_found(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    proxy.current_network_id = "net1"

    result = await viz_tools.plot_substation_single_line_diagram(
        network_id="missing_net", substation_id="sub1", ctx=mock_ctx
    )

    assert result == "Network 'missing_net' not found"


@pytest.mark.asyncio
async def test_plot_substation_missing_substation_id(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await viz_tools.plot_substation_single_line_diagram(
        network_id="net1", substation_id=None, ctx=mock_ctx
    )

    assert result == "Substation ID is required for a substation single-line diagram."


@pytest.mark.asyncio
async def test_plot_substation_exception_handling(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_single_line_diagram.side_effect = RuntimeError("boom")
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl.network.SldParameters"):
        result = await viz_tools.plot_substation_single_line_diagram(
            network_id="net1", substation_id="sub1", ctx=mock_ctx
        )

    assert result == "Error generating single-line diagram: boom"


@pytest.mark.asyncio
async def test_visualize_network_uses_current_network_id(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with (
        patch.object(proxy, "create_network_visualization_bytes") as mock_viz,
        patch(
            "pypowsybl_mcp.tools.utils.visualization.generate_download_link"
        ) as mock_gen_link,
    ):
        mock_viz.return_value = b"<svg></svg>"
        mock_gen_link.return_value = {"download_url": "http://localhost/download/x"}

        result = await viz_tools.visualize_network(ctx=mock_ctx)

        assert result == "http://localhost/download/x"
        mock_viz.assert_called_once_with("net1")


@pytest.mark.asyncio
async def test_visualize_network_no_network_selected(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    proxy.current_network_id = None

    result = await viz_tools.visualize_network(ctx=mock_ctx)

    assert result == "No network specified and no current network selected"


@pytest.mark.asyncio
async def test_visualize_network_not_found(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    proxy.current_network_id = "net1"

    result = await viz_tools.visualize_network(network_id="missing_net", ctx=mock_ctx)

    assert result == "Network 'missing_net' not found"


@pytest.mark.asyncio
async def test_visualize_network_exception_handling(viz_tools, mock_ctx):
    proxy = viz_tools.get_proxy("test-session")
    mock_net = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch.object(proxy, "create_network_visualization_bytes") as mock_viz:
        mock_viz.side_effect = RuntimeError("boom")

        result = await viz_tools.visualize_network(network_id="net1", ctx=mock_ctx)

    assert result == "Failed to create visualization: boom"
