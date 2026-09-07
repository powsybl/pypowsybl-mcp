#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, mock_open, patch

import pypowsybl as pp
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.utils.io import IOTools, register_io_tools


class MockContext:
    def __init__(self, session_id="test-session"):
        self.session = MagicMock()
        self.session.session_id = session_id
        self.request_id = "test-request"
        self.client_id = "test-client"


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
def io_tools(pypowsybl_proxies):
    return IOTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.mark.asyncio
async def test_load_network_from_file_success(io_tools, mock_ctx):
    with (
        patch("os.path.exists", return_value=True),
        patch("pypowsybl.network.load") as mock_load,
    ):
        mock_network = MagicMock()
        mock_network.get_buses.return_value = ["bus1", "bus2"]
        mock_load.return_value = mock_network

        result = await io_tools.load_network_from_file(
            path="test.xiidm", network_id="net1", set_as_current=True, ctx=mock_ctx
        )

        assert result["status"] == "success"
        assert "Successfully loaded network 'net1'" in result["message"]
        assert "2 buses" in result["message"]

        proxy = io_tools.get_proxy("test-session")
        assert proxy.networks["net1"] == mock_network
        assert proxy.current_network_id == "net1"
        assert proxy.current_network == mock_network


@pytest.mark.asyncio
async def test_load_network_from_file_not_found(io_tools, mock_ctx):
    with patch("os.path.exists", return_value=False):
        result = await io_tools.load_network_from_file(
            path="nonexistent.xiidm", network_id="net1", ctx=mock_ctx
        )
        assert result["status"] == "error"
        assert "Error: File not found" in result["message"]


@pytest.mark.asyncio
async def test_load_network_from_url_success(io_tools, mock_ctx):
    mock_response = MagicMock()
    mock_response.__enter__.return_value.read.return_value = b"fake-data"

    with (
        patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen,
        patch("builtins.open", mock_open()),
        patch("pypowsybl.network.load") as mock_load,
        patch("os.unlink") as mock_unlink,
        patch("os.path.exists", return_value=True),
    ):
        mock_network = MagicMock()
        mock_network.get_buses.return_value = ["bus1"]
        mock_load.return_value = mock_network

        result = await io_tools.load_network_from_url(
            url="http://example.com/test.xiidm", network_id="net_url", ctx=mock_ctx
        )

        assert result["status"] == "success"
        assert "Successfully loaded network 'net_url'" in result["message"]
        mock_load.assert_called_once()
        mock_urlopen.assert_called_once_with("http://example.com/test.xiidm")
        mock_unlink.assert_called_once()

        proxy = io_tools.get_proxy("test-session")
        assert proxy.networks["net_url"] == mock_network


@pytest.mark.asyncio
async def test_load_network_from_url_failure(io_tools, mock_ctx):
    with patch("urllib.request.urlopen", side_effect=OSError("Download failed")):
        result = await io_tools.load_network_from_url(
            url="http://example.com/test.xiidm", network_id="net_url", ctx=mock_ctx
        )
        assert result["status"] == "error"
        assert "Failed to load network from URL: Download failed" in result["message"]


@pytest.mark.asyncio
async def test_load_network_from_url_https(io_tools, mock_ctx):
    mock_response = MagicMock()
    mock_response.__enter__.return_value.read.return_value = b"fake-data"

    with (
        patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen,
        patch("builtins.open", mock_open()),
        patch("pypowsybl.network.load") as mock_load,
        patch("os.unlink"),
        patch("os.path.exists", return_value=True),
    ):
        mock_network = MagicMock()
        mock_network.get_buses.return_value = ["bus1"]
        mock_load.return_value = mock_network

        result = await io_tools.load_network_from_url(
            url="https://example.com/test.xiidm",
            network_id="net_url",
            ctx=mock_ctx,
        )

        assert result["status"] == "success"
        mock_urlopen.assert_called_once_with("https://example.com/test.xiidm")


@pytest.mark.asyncio
async def test_export_network_success(io_tools, mock_ctx):
    proxy = io_tools.get_proxy("test-session")
    mock_network = MagicMock()
    proxy.networks["net1"] = mock_network

    with (
        patch("tempfile.NamedTemporaryFile") as mock_tmp,
        patch("builtins.open", mock_open(read_data=b"exported-data")),
        patch("pypowsybl_mcp.tools.utils.io.generate_download_link") as mock_gen_link,
        patch("os.unlink"),
        patch("os.path.exists", return_value=True),
    ):
        mock_tmp.return_value.__enter__.return_value.name = "/tmp/fake-tmp"
        mock_gen_link.return_value = {
            "download_url": "http://localhost/download/token/net1.xiidm"
        }

        result = await io_tools.export_network(
            network_id="net1", format_type="XIIDM", ctx=mock_ctx
        )

        assert result["status"] == "success"
        assert result["message"] == "http://localhost/download/token/net1.xiidm"
        mock_network.save.assert_called_once_with("/tmp/fake-tmp", format="XIIDM")


@pytest.mark.asyncio
async def test_export_network_not_found(io_tools, mock_ctx):
    result = await io_tools.export_network(network_id="nonexistent", ctx=mock_ctx)
    assert result["status"] == "error"
    assert "Network 'nonexistent' not found" in result["message"]


@pytest.mark.asyncio
async def test_export_network_no_current(io_tools, mock_ctx):
    result = await io_tools.export_network(network_id=None, ctx=mock_ctx)
    assert result["status"] == "error"
    assert "No network specified and no current network selected" in result["message"]


def test_register_io_tools():
    mcp = MockMCP()
    proxies = TTLCache(maxsize=10, ttl=3600)
    register_io_tools(mcp, proxies)
    assert "load_network_from_url" in mcp.tools
    assert "load_network_from_file" in mcp.tools
    assert "export_network" in mcp.tools


@pytest.mark.asyncio
async def test_load_network_from_url_cleanup_error_is_logged(io_tools, mock_ctx):
    # The temp-file removal in the finally block can itself fail; that error
    # should be logged but must not abort the (otherwise successful) load.
    mock_response = MagicMock()
    mock_response.__enter__.return_value.read.return_value = b"fake-data"

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        patch("builtins.open", mock_open()),
        patch("pypowsybl.network.load") as mock_load,
        patch("os.unlink", side_effect=OSError("permission denied")),
        patch("os.path.exists", return_value=True),
    ):
        mock_network = MagicMock()
        mock_network.get_buses.return_value = ["bus1"]
        mock_load.return_value = mock_network

        result = await io_tools.load_network_from_url(
            url="http://example.com/test.xiidm", network_id="net_url", ctx=mock_ctx
        )

    # Cleanup failure is swallowed; the overall load still succeeds.
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_load_network_from_file_exception_handling(io_tools, mock_ctx):
    with (
        patch("os.path.exists", return_value=True),
        patch("pypowsybl.network.load", side_effect=pp.PyPowsyblError("bad format")),
    ):
        result = await io_tools.load_network_from_file(
            path="broken.xiidm", network_id="net1", ctx=mock_ctx
        )

    assert result["status"] == "error"
    assert "Failed to load network from file: bad format" in result["message"]


@pytest.mark.asyncio
async def test_export_network_uses_provided_file_name(io_tools, mock_ctx):
    proxy = io_tools.get_proxy("test-session")
    mock_network = MagicMock()
    proxy.networks["net1"] = mock_network

    with (
        patch("tempfile.NamedTemporaryFile") as mock_tmp,
        patch("builtins.open", mock_open(read_data=b"exported-data")),
        patch("pypowsybl_mcp.tools.utils.io.generate_download_link") as mock_gen_link,
        patch("os.unlink"),
        patch("os.path.exists", return_value=True),
    ):
        mock_tmp.return_value.__enter__.return_value.name = "/tmp/fake-tmp"
        mock_gen_link.return_value = {
            "download_url": "http://localhost/download/token/custom.xiidm"
        }

        result = await io_tools.export_network(
            network_id="net1",
            file_name="/some/dir/custom.xiidm",
            format_type="XIIDM",
            ctx=mock_ctx,
        )

        assert result["status"] == "success"
        # basename should have been used, stripping the directory portion
        mock_gen_link.assert_called_once()
        _, kwargs = mock_gen_link.call_args
        assert kwargs["filename"] == "custom.xiidm"


@pytest.mark.asyncio
async def test_export_network_exception_handling(io_tools, mock_ctx):
    proxy = io_tools.get_proxy("test-session")
    mock_network = MagicMock()
    mock_network.save.side_effect = pp.PyPowsyblError("disk full")
    proxy.networks["net1"] = mock_network

    with (
        patch("tempfile.NamedTemporaryFile") as mock_tmp,
        patch("os.unlink"),
        patch("os.path.exists", return_value=True),
    ):
        mock_tmp.return_value.__enter__.return_value.name = "/tmp/fake-tmp"

        result = await io_tools.export_network(
            network_id="net1", format_type="XIIDM", ctx=mock_ctx
        )

    assert result["status"] == "error"
    assert "Failed to export network: disk full" in result["message"]


@pytest.mark.asyncio
async def test_export_network_invalid_filename(io_tools, mock_ctx):
    """A network_id that sanitizes to an unsafe default filename (e.g. containing
    a space) must be reported as a normal error dict, not raise ValueError out of
    the tool (regression: only (PyPowsyblError, OSError) were caught here before).
    """
    proxy = io_tools.get_proxy("test-session")
    mock_network = MagicMock()
    proxy.networks["ieee 14"] = mock_network

    with (
        patch("tempfile.NamedTemporaryFile") as mock_tmp,
        patch("builtins.open", mock_open(read_data=b"exported-data")),
        patch("os.unlink"),
        patch("os.path.exists", return_value=True),
    ):
        mock_tmp.return_value.__enter__.return_value.name = "/tmp/fake-tmp"

        result = await io_tools.export_network(
            network_id="ieee 14", format_type="XIIDM", ctx=mock_ctx
        )

    assert result["status"] == "error"
    assert "Failed to export network: Invalid or unsafe filename" in result["message"]
