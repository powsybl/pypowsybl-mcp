#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from starlette.responses import JSONResponse, Response

from pypowsybl_mcp.utils.download_utils import (
    cleanup_expired_links,
    download_file_endpoint,
    download_links,
    download_links_lock,
    generate_download_link,
)


@pytest.fixture(autouse=True)
def clear_download_links():
    with download_links_lock:
        download_links.clear()
    yield
    with download_links_lock:
        # Cleanup any remaining temp files created during tests
        for token in list(download_links.keys()):
            info = download_links[token]
            temp_path = info.get("temp_path")
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    temp_dir = os.path.dirname(temp_path)
                    if os.path.exists(temp_dir):
                        os.rmdir(temp_dir)
                except Exception:
                    pass
        download_links.clear()


def test_generate_download_link():
    filename = "test.txt"
    file_data = b"hello world"
    base_url = "http://example.com/download"

    result = generate_download_link(filename, file_data, base_url)

    assert "token" in result
    assert result["download_url"] == f"{base_url}/{result['token']}/{filename}"
    assert "expires_at" in result
    assert result["expires_in_seconds"] == 3600

    # Check if file exists and has correct content
    token = result["token"]
    with download_links_lock:
        assert token in download_links
        info = download_links[token]
        assert info["filename"] == filename
        assert os.path.exists(info["temp_path"])
        with open(info["temp_path"], "rb") as f:
            assert f.read() == file_data


def test_cleanup_expired_links():
    # Create an expired link
    token = "expired_token"
    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, "expired.txt")
    with open(temp_path, "wb") as f:
        f.write(b"expired")

    with download_links_lock:
        download_links[token] = {
            "filename": "expired.txt",
            "temp_path": temp_path,
            "expires_at": datetime.now() - timedelta(seconds=1),
        }

    assert os.path.exists(temp_path)

    cleanup_expired_links()

    with download_links_lock:
        assert token not in download_links
    assert not os.path.exists(temp_path)
    assert not os.path.exists(temp_dir)


@pytest.mark.asyncio
async def test_download_file_endpoint_success():
    filename = "test.png"
    file_data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR..."
    base_url = "http://test"

    gen_result = generate_download_link(filename, file_data, base_url)
    token = gen_result["token"]

    mock_request = MagicMock()
    mock_request.path_params = {"token": token}

    response = await download_file_endpoint(mock_request)

    assert isinstance(response, Response)
    assert response.status_code == 200
    assert response.body == file_data
    assert response.headers["Content-Type"] == "image/png"
    assert "inline" in response.headers["Content-Disposition"]
    assert filename in response.headers["Content-Disposition"]


@pytest.mark.asyncio
async def test_download_file_endpoint_not_found():
    mock_request = MagicMock()
    mock_request.path_params = {"token": "nonexistent"}

    response = await download_file_endpoint(mock_request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_download_file_endpoint_expired():
    token = "expired_token"
    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, "expired.txt")
    with open(temp_path, "wb") as f:
        f.write(b"expired")

    with download_links_lock:
        download_links[token] = {
            "filename": "expired.txt",
            "temp_path": temp_path,
            "expires_at": datetime.now() - timedelta(seconds=1),
        }

    mock_request = MagicMock()
    mock_request.path_params = {"token": token}

    # Mock cleanup_expired_links to do nothing, so we can test the 410 logic
    with patch("pypowsybl_mcp.utils.download_utils.cleanup_expired_links"):
        response = await download_file_endpoint(mock_request)

    assert response.status_code == 410
    with download_links_lock:
        assert token not in download_links


@pytest.mark.asyncio
async def test_download_file_endpoint_missing_token():
    mock_request = MagicMock()
    mock_request.path_params = {}

    response = await download_file_endpoint(mock_request)

    assert response.status_code == 400


def test_cleanup_expired_links_deletion_error_is_logged():
    # Expired link whose temp file deletion raises - should be caught and logged,
    # and the registry entry should still be removed.
    token = "expired_token_err"
    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, "expired.txt")
    with open(temp_path, "wb") as f:
        f.write(b"expired")

    with download_links_lock:
        download_links[token] = {
            "filename": "expired.txt",
            "temp_path": temp_path,
            "expires_at": datetime.now() - timedelta(seconds=1),
        }

    with patch("os.unlink", side_effect=OSError("permission denied")):
        cleanup_expired_links()

    with download_links_lock:
        assert token not in download_links

    # cleanup the real file left behind since os.unlink was mocked out
    if os.path.exists(temp_path):
        os.unlink(temp_path)
    if os.path.exists(temp_dir):
        os.rmdir(temp_dir)


def test_generate_download_link_write_failure_cleans_up_and_raises():
    filename = "test.txt"
    file_data = b"hello world"
    base_url = "http://example.com/download"

    with patch(
        "pypowsybl_mcp.utils.download_utils.open",
        side_effect=OSError("disk full"),
        create=True,
    ):
        with pytest.raises(OSError, match="disk full"):
            generate_download_link(filename, file_data, base_url)

    # No entry should have been registered
    with download_links_lock:
        assert len(download_links) == 0


@pytest.mark.asyncio
async def test_download_file_endpoint_expired_deletion_error_is_logged():
    token = "expired_token_err2"
    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, "expired.txt")
    with open(temp_path, "wb") as f:
        f.write(b"expired")

    with download_links_lock:
        download_links[token] = {
            "filename": "expired.txt",
            "temp_path": temp_path,
            "expires_at": datetime.now() - timedelta(seconds=1),
        }

    mock_request = MagicMock()
    mock_request.path_params = {"token": token}

    with (
        patch("pypowsybl_mcp.utils.download_utils.cleanup_expired_links"),
        patch("os.unlink", side_effect=OSError("permission denied")),
    ):
        response = await download_file_endpoint(mock_request)

    assert response.status_code == 410
    with download_links_lock:
        assert token not in download_links

    # cleanup the real file left behind since os.unlink was mocked out
    if os.path.exists(temp_path):
        os.unlink(temp_path)
    if os.path.exists(temp_dir):
        os.rmdir(temp_dir)


@pytest.mark.asyncio
async def test_download_file_endpoint_unknown_content_type_defaults_octet_stream():
    filename = "README"  # no extension -> mimetypes.guess_type returns (None, None)
    file_data = b"some content"
    base_url = "http://test"

    gen_result = generate_download_link(filename, file_data, base_url)
    token = gen_result["token"]

    mock_request = MagicMock()
    mock_request.path_params = {"token": token}

    response = await download_file_endpoint(mock_request)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert "attachment" in response.headers["Content-Disposition"]


@pytest.mark.asyncio
async def test_download_file_endpoint_read_failure_returns_500():
    filename = "test.txt"
    file_data = b"hello world"
    base_url = "http://test"

    gen_result = generate_download_link(filename, file_data, base_url)
    token = gen_result["token"]

    mock_request = MagicMock()
    mock_request.path_params = {"token": token}

    with patch(
        "pypowsybl_mcp.utils.download_utils.open",
        side_effect=OSError("read fail"),
        create=True,
    ):
        response = await download_file_endpoint(mock_request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500


def test_generate_download_link_failure_after_write_cleanup_error_is_swallowed():
    # Exercise the case where an error happens *after* the temp file was
    # successfully written (so the except block's own cleanup actually has
    # something to delete), and that cleanup itself fails - which must be
    # swallowed rather than masking the original error.
    filename = "test.txt"
    file_data = b"hello"
    base_url = "http://example.com"

    fixed_dir = tempfile.mkdtemp()

    call_count = {"n": 0}

    def flaky_info(msg, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("logging backend down")

    try:
        with (
            patch("tempfile.mkdtemp", return_value=fixed_dir),
            patch(
                "pypowsybl_mcp.utils.download_utils.logger.info",
                side_effect=flaky_info,
            ),
            patch("os.unlink", side_effect=OSError("cannot delete")),
        ):
            with pytest.raises(RuntimeError, match="logging backend down"):
                generate_download_link(filename, file_data, base_url)
        # Note: the failure happens after the registry entry is inserted
        # (download_links[token] = ...) but before the final "generated link"
        # log line, so the token is left dangling in the registry even though
        # the underlying temp file/dir cleanup was attempted. Not asserted on
        # here since it's incidental to what this test targets (the swallowed
        # cleanup-of-cleanup error), but noted as a minor real edge case.
    finally:
        # os.unlink was mocked out during the call, so clean up for real now.
        leftover = os.path.join(fixed_dir, filename)
        if os.path.exists(leftover):
            os.unlink(leftover)
        if os.path.exists(fixed_dir):
            os.rmdir(fixed_dir)
        with download_links_lock:
            download_links.clear()


@pytest.mark.asyncio
async def test_download_file_endpoint_temp_file_missing_on_disk_returns_404():
    # The registry entry is valid and not expired, but the underlying temp
    # file has disappeared from disk (e.g. deleted out-of-band).
    token = "valid_token_missing_file"
    with download_links_lock:
        download_links[token] = {
            "filename": "gone.txt",
            "temp_path": "/nonexistent/path/gone.txt",
            "expires_at": datetime.now() + timedelta(seconds=60),
        }

    mock_request = MagicMock()
    mock_request.path_params = {"token": token}

    response = await download_file_endpoint(mock_request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 404
