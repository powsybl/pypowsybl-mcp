#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

import pytest
from cachetools import TTLCache
from mcp.server.fastmcp import FastMCP

from pypowsybl_mcp.plugins import (
    PLUGIN_GROUP,
    RESOURCE_PLUGIN_GROUP,
    _disabled_plugin_names,
    discover_and_load_plugins,
    discover_and_load_resource_plugins,
)


def _fake_entry_point(name, register_fn):
    ep = MagicMock()
    ep.name = name
    ep.load.return_value = register_fn
    return ep


@pytest.fixture
def mcp():
    return FastMCP("TestServer")


@pytest.fixture
def proxies():
    return TTLCache(maxsize=10, ttl=3600)


class TestDisabledPluginNames:
    def test_empty_env_returns_empty_set(self, monkeypatch):
        monkeypatch.delenv("MCP_DISABLED_PLUGINS", raising=False)
        assert _disabled_plugin_names() == set()

    def test_parses_comma_separated_names_and_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("MCP_DISABLED_PLUGINS", "rte_tools, other_plugin ,,third")
        assert _disabled_plugin_names() == {"rte_tools", "other_plugin", "third"}


class TestDiscoverAndLoadPlugins:
    def test_loads_and_calls_registrar(self, mcp, proxies):
        register_fn = MagicMock()
        ep = _fake_entry_point("rte_tools", register_fn)

        with patch(
            "pypowsybl_mcp.plugins.importlib.metadata.entry_points", return_value=[ep]
        ) as mock_entry_points:
            discover_and_load_plugins(mcp, proxies)

        mock_entry_points.assert_called_once_with(group=PLUGIN_GROUP)
        register_fn.assert_called_once_with(mcp, proxies)

    def test_skips_disabled_plugin(self, mcp, proxies, monkeypatch):
        monkeypatch.setenv("MCP_DISABLED_PLUGINS", "rte_tools")
        register_fn = MagicMock()
        ep = _fake_entry_point("rte_tools", register_fn)

        with patch(
            "pypowsybl_mcp.plugins.importlib.metadata.entry_points", return_value=[ep]
        ):
            discover_and_load_plugins(mcp, proxies)

        register_fn.assert_not_called()
        ep.load.assert_not_called()

    def test_failing_plugin_does_not_raise_or_block_others(self, mcp, proxies):
        broken_ep = MagicMock()
        broken_ep.name = "broken_plugin"
        broken_ep.load.side_effect = RuntimeError("boom")

        healthy_register_fn = MagicMock()
        healthy_ep = _fake_entry_point("healthy_plugin", healthy_register_fn)

        with patch(
            "pypowsybl_mcp.plugins.importlib.metadata.entry_points",
            return_value=[broken_ep, healthy_ep],
        ):
            discover_and_load_plugins(mcp, proxies)  # must not raise

        healthy_register_fn.assert_called_once_with(mcp, proxies)


class TestDiscoverAndLoadResourcePlugins:
    def test_loads_and_calls_registrar(self, mcp, proxies):
        register_fn = MagicMock()
        ep = _fake_entry_point("rte_docs", register_fn)

        with patch(
            "pypowsybl_mcp.plugins.importlib.metadata.entry_points", return_value=[ep]
        ) as mock_entry_points:
            discover_and_load_resource_plugins(mcp, proxies)

        mock_entry_points.assert_called_once_with(group=RESOURCE_PLUGIN_GROUP)
        register_fn.assert_called_once_with(mcp, proxies)

    def test_skips_disabled_plugin(self, mcp, proxies, monkeypatch):
        monkeypatch.setenv("MCP_DISABLED_PLUGINS", "rte_docs")
        register_fn = MagicMock()
        ep = _fake_entry_point("rte_docs", register_fn)

        with patch(
            "pypowsybl_mcp.plugins.importlib.metadata.entry_points", return_value=[ep]
        ):
            discover_and_load_resource_plugins(mcp, proxies)

        register_fn.assert_not_called()

    def test_failing_plugin_does_not_raise(self, mcp, proxies):
        broken_ep = MagicMock()
        broken_ep.name = "broken_docs"
        broken_ep.load.side_effect = RuntimeError("boom")

        with patch(
            "pypowsybl_mcp.plugins.importlib.metadata.entry_points",
            return_value=[broken_ep],
        ):
            discover_and_load_resource_plugins(mcp, proxies)  # must not raise
