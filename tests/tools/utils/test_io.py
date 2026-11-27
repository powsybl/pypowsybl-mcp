#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import os
from unittest.mock import ANY, MagicMock, mock_open, patch

import pypowsybl as pp
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.utils.io import IOTools, register_io_tools


class MockContext:
    def __init__(self, session_id="test-session"):
        self.session = MagicMock()
        self.session.session_id = session_id
        self.request_id = "test-request"


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
        patch("tempfile.mkdtemp", return_value="/tmp/fake-dir"),
        patch("os.path.isdir", return_value=True),
        patch("shutil.rmtree") as mock_rmtree,
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
        # Loaded from the downloaded file, named as in the URL, with no option
        assert mock_load.call_args.args == (
            os.path.join("/tmp/fake-dir", "test.xiidm"),
        )
        assert mock_load.call_args.kwargs == {
            "parameters": None,
            "post_processors": None,
            "report_node": ANY,
        }
        # The temp directory goes, with the file in it
        mock_rmtree.assert_called_once_with("/tmp/fake-dir")

        proxy = io_tools.get_proxy("test-session")
        assert proxy.networks["net_url"] == mock_network


@pytest.mark.asyncio
async def test_load_network_from_url_failure(io_tools, mock_ctx):
    with (
        patch("urllib.request.urlopen", side_effect=OSError("Download failed")),
        patch("tempfile.mkdtemp", return_value="/tmp/fake-dir"),
        patch("os.path.isdir", return_value=True),
        patch("shutil.rmtree") as mock_rmtree,
    ):
        result = await io_tools.load_network_from_url(
            url="http://example.com/test.xiidm", network_id="net_url", ctx=mock_ctx
        )
        assert result["status"] == "error"
        assert "Failed to load network from URL: Download failed" in result["message"]
        # The temp directory must still be cleaned up even though the download
        # itself failed (before, cleanup only ran on the success/load path).
        mock_rmtree.assert_called_once_with("/tmp/fake-dir")


@pytest.mark.asyncio
async def test_load_network_from_url_https(io_tools, mock_ctx):
    mock_response = MagicMock()
    mock_response.__enter__.return_value.read.return_value = b"fake-data"

    with (
        patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen,
        patch("builtins.open", mock_open()),
        patch("pypowsybl.network.load") as mock_load,
        patch("tempfile.mkdtemp", return_value="/tmp/fake-dir"),
        patch("os.path.isdir", return_value=True),
        patch("shutil.rmtree"),
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
        patch("tempfile.mkdtemp", return_value="/tmp/fake-dir"),
        patch("os.unlink", side_effect=OSError("permission denied")),
        patch("os.path.exists", return_value=True),
        patch("os.path.isdir", return_value=True),
        patch("os.rmdir", side_effect=OSError("directory not empty")),
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
async def test_load_network_from_url_uses_unique_temp_dir(io_tools, mock_ctx):
    """Regression for bug #8: the download must land in a fresh directory from
    tempfile.mkdtemp(), not the shared system temp dir (tempfile.gettempdir()),
    so two concurrent downloads with the same URL basename can never collide.
    """
    mock_response = MagicMock()
    mock_response.__enter__.return_value.read.return_value = b"fake-data"

    fake_dir = os.path.join("tmp", "unique-abc123")

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        patch("builtins.open", mock_open()),
        patch("pypowsybl.network.load") as mock_load,
        patch("tempfile.mkdtemp", return_value=fake_dir) as mock_mkdtemp,
        patch("tempfile.gettempdir") as mock_gettempdir,
        patch("os.path.isdir", return_value=True),
        patch("shutil.rmtree") as mock_rmtree,
    ):
        mock_network = MagicMock()
        mock_network.get_buses.return_value = ["bus1"]
        mock_load.return_value = mock_network

        await io_tools.load_network_from_url(
            url="http://example.com/network.xiidm", network_id="net_url", ctx=mock_ctx
        )

        mock_mkdtemp.assert_called_once()
        mock_gettempdir.assert_not_called()
        # Build the expected path with os.path.join too, rather than a hardcoded
        # "/"-joined string: os.path.join uses "\" on Windows, so a literal
        # "fake_dir/network.xiidm" would mismatch the real call there.
        mock_load.assert_called_once_with(
            os.path.join(fake_dir, "network.xiidm"),
            parameters=None,
            post_processors=None,
            report_node=ANY,
        )
        mock_rmtree.assert_called_once_with(fake_dir)


@pytest.mark.asyncio
async def test_load_network_from_url_empty_basename_fallback(io_tools, mock_ctx):
    """Regression for bug #8: a URL whose path ends in "/" has an empty
    os.path.basename(), which used to be joined onto the temp dir as-is and
    crash with IsADirectoryError. It must now fall back to a generic filename.
    """
    mock_response = MagicMock()
    mock_response.__enter__.return_value.read.return_value = b"fake-data"

    fake_dir = os.path.join("tmp", "fake-dir")

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        patch("builtins.open", mock_open()),
        patch("pypowsybl.network.load") as mock_load,
        patch("tempfile.mkdtemp", return_value=fake_dir),
        patch("os.path.isdir", return_value=True),
        patch("shutil.rmtree"),
    ):
        mock_network = MagicMock()
        mock_network.get_buses.return_value = ["bus1"]
        mock_load.return_value = mock_network

        result = await io_tools.load_network_from_url(
            url="http://example.com/networks/", network_id="net_url", ctx=mock_ctx
        )

        assert result["status"] == "success"
        mock_load.assert_called_once_with(
            os.path.join(fake_dir, "downloaded_network"),
            parameters=None,
            post_processors=None,
            report_node=ANY,
        )


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


_MAT = b"MATLAB 5.0 MAT-file, Platform: GLNXA64"


@pytest.mark.parametrize(
    ("name", "data", "boundary", "expected"),
    [
        # A zip saved under an extensionless URL name gets .zip ...
        ("download", b"PK\x03\x04rest", False, "download.zip"),
        ("download", b"PK\x03\x04rest", True, "download.zip"),
        # ... once only, whatever the case, and even after another extension
        ("BD.ZIP", b"PK\x03\x04rest", True, "BD.ZIP"),
        ("grid.xiidm", b"PK\x03\x04rest", False, "grid.xiidm.zip"),
        # XML gets .xml, for a network (XIIDM or CGMES) and a boundary alike
        ("download", b"\xef\xbb\xbf  <?xml version", True, "download.xml"),
        ("download", b"<?xml version", False, "download.xml"),
        ("download.php", b"<iidm:network", False, "download.php.xml"),
        # JSON gets .jiidm, a MATLAB file .mat - for a network only
        ("download", b'{\n  "version" : "1.17"', False, "download.jiidm"),
        ("download", _MAT, False, "download.mat"),
        ("download", b'{"version"', True, "download"),
        ("download", _MAT, True, "download"),
        # A name the importers already read is kept
        ("EQBD.xml", b"<?xml version", True, "EQBD.xml"),
        ("grid.xiidm", b"<?xml version", False, "grid.xiidm"),
        ("grid.IIDM", b"<?xml version", False, "grid.IIDM"),
        ("grid.json", b'{"version"', False, "grid.json"),
        ("case14.mat", _MAT, False, "case14.mat"),
        # Content that cannot be told is left alone
        ("grid.uct", b"##C 2007.05.01", False, "grid.uct"),
        ("download", b"##C 2007.05.01", False, "download"),
        ("download", b"", False, "download"),
    ],
)
def test_downloaded_file_is_named_after_its_content(name, data, boundary, expected):
    from pypowsybl_mcp.tools.utils.io import _name_for_content

    assert _name_for_content(name, data, boundary) == expected
