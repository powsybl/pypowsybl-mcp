#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.utils.resources import (
    MAX_INLINE_CONTENT_CHARS,
    URL_BASE,
    _extract_main_content,
    _extract_method_index,
    _pitfall_banner,
    _truncate_for_inline,
    register_resource_tools,
)

DOC_PAGE_HTML = """
<html>
  <body>
    <nav class="sidebar">Navigation links that should be dropped</nav>
    <article role="main" id="furo-main-content">
      <h1>create_empty</h1>
      <p>Create an empty network.</p>
    </article>
    <footer>Footer that should be dropped</footer>
  </body>
</html>
"""


OVERVIEW_HTML = """
<html>
  <body>
    <nav class="sidebar">Navigation links that should be dropped</nav>
    <article role="main" id="furo-main-content">
      <h1>Network<a class="headerlink" href="#network">¶</a></h1>
      <h2>Network creation<a class="headerlink" href="#creation">¶</a></h2>
      <table class="autosummary longtable docutils">
        <tbody>
          <tr>
            <td><p><a class="reference internal" href="api/pypowsybl.network.create_empty.html"
                  title="pypowsybl.network.create_empty"><code><span>create_empty</span></code></a></p></td>
            <td><p>Create an empty network.</p></td>
          </tr>
          <tr>
            <td><p><a class="reference external" title="(in pandas)">DataFrame</a></p></td>
            <td><p>A pandas ref that must be ignored.</p></td>
          </tr>
        </tbody>
      </table>
      <h2>I/O<a class="headerlink" href="#io">¶</a></h2>
      <table class="autosummary longtable docutils">
        <tbody>
          <tr>
            <td><p><a class="reference internal" href="api/pypowsybl.network.load.html"
                  title="pypowsybl.network.load"><code><span>load</span></code></a></p></td>
            <td><p>Load a network from a file.</p></td>
          </tr>
        </tbody>
      </table>
    </article>
  </body>
</html>
"""


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
def mcp():
    return MockMCP()


@pytest.fixture
def mock_proxy():
    proxy = MagicMock()
    proxy.save_markdown_resource.side_effect = lambda content, resource_id: (
        f"resources://temp/{resource_id}"
    )
    return proxy


@pytest.fixture
def pypowsybl_proxies(mock_proxy):
    proxies = TTLCache(maxsize=10, ttl=3600)
    proxies["test-session"] = mock_proxy
    return proxies


@pytest.fixture
def mock_ctx():
    return MockContext()


def make_mock_async_client(html=DOC_PAGE_HTML, raise_for_status_error=None):
    """Build a mock httpx.AsyncClient usable as an async context manager."""
    response = MagicMock()
    response.text = html
    if raise_for_status_error is not None:
        response.raise_for_status.side_effect = raise_for_status_error

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client_cm = MagicMock()
    client_cm.__aenter__.return_value = client
    return client_cm, client


# --- _extract_main_content ---


def test_extract_main_content_keeps_only_main_region():
    extracted = _extract_main_content(DOC_PAGE_HTML)

    assert "Create an empty network." in extracted
    assert "Navigation links" not in extracted
    assert "Footer" not in extracted


def test_extract_main_content_falls_back_to_full_page():
    html = "<html><body><p>No main region here</p></body></html>"

    assert _extract_main_content(html) == html


# --- _extract_method_index ---


def test_extract_method_index_lists_methods_by_section():
    index = _extract_method_index(OVERVIEW_HTML, "network")

    assert "## Network creation" in index
    assert "## I/O" in index
    assert "- create_empty - Create an empty network." in index
    assert "- load - Load a network from a file." in index
    # cross-references to other libraries (pandas, ...) are dropped
    assert "DataFrame" not in index
    # the headerlink pilcrow is stripped from section titles
    assert "¶" not in index
    # the index stays far smaller than the full page conversion
    assert len(index) < 4000


def test_extract_method_index_empty_without_autosummary():
    assert _extract_method_index(DOC_PAGE_HTML, "network") == ""


# --- _truncate_for_inline ---


def test_truncate_for_inline_leaves_small_content_untouched():
    content = "short content"

    assert _truncate_for_inline(content, "resources://temp/x") == content


def test_truncate_for_inline_caps_large_content_and_points_to_uri():
    content = "A" * (MAX_INLINE_CONTENT_CHARS + 5000)
    uri = "resources://temp/network"

    truncated = _truncate_for_inline(content, uri)

    assert len(truncated) < len(content)
    assert truncated.startswith("A" * MAX_INLINE_CONTENT_CHARS)
    assert "truncated 5000 characters" in truncated
    assert uri in truncated


# --- _pitfall_banner ---


def test_pitfall_banner_flags_known_misleading_name():
    banner = _pitfall_banner("network", "Network.disconnect")

    assert banner.startswith("> **Naming note")
    assert "does NOT tear down" in banner
    assert banner.endswith("\n\n")


def test_pitfall_banner_empty_for_unknown_name():
    assert _pitfall_banner("network", "create_empty") == ""


# --- get_online_resource ---


@pytest.mark.asyncio
async def test_get_online_resource_invalid_class_object(
    mcp, pypowsybl_proxies, mock_proxy, mock_ctx
):
    register_resource_tools(mcp, pypowsybl_proxies)
    get_online_resource = mcp.tools["get_online_resource"]

    result = json.loads(
        await get_online_resource(class_object="not_a_module", ctx=mock_ctx)
    )

    assert result["success"] is False
    assert "not found" in result["error"]
    mock_proxy.save_markdown_resource.assert_not_called()


@pytest.mark.asyncio
async def test_get_online_resource_method_documentation(
    mcp, pypowsybl_proxies, mock_proxy, mock_ctx
):
    register_resource_tools(mcp, pypowsybl_proxies)
    get_online_resource = mcp.tools["get_online_resource"]

    client_cm, client = make_mock_async_client()
    with patch(
        "pypowsybl_mcp.tools.utils.resources.httpx.AsyncClient",
        return_value=client_cm,
    ):
        result = json.loads(
            await get_online_resource(
                class_object="network", method_name="create_empty", ctx=mock_ctx
            )
        )

    assert result["success"] is True
    assert result["uri"] == "resources://temp/network-create_empty"
    # The markdown is returned directly in the response, no resource read needed
    assert "Create an empty network." in result["content"]
    assert "Navigation links" not in result["content"]

    requested_url = client.get.call_args[0][0]
    assert requested_url == f"{URL_BASE}/api/pypowsybl.network.create_empty.html"
    assert "//api" not in requested_url

    content, resource_id = mock_proxy.save_markdown_resource.call_args[0]
    assert resource_id == "network-create_empty"
    # Only the main documentation region is converted to markdown
    assert "Create an empty network." in content
    assert "Navigation links" not in content


@pytest.mark.asyncio
async def test_get_online_resource_prepends_pitfall_banner(
    mcp, pypowsybl_proxies, mock_proxy, mock_ctx
):
    register_resource_tools(mcp, pypowsybl_proxies)
    get_online_resource = mcp.tools["get_online_resource"]

    client_cm, _ = make_mock_async_client()
    with patch(
        "pypowsybl_mcp.tools.utils.resources.httpx.AsyncClient",
        return_value=client_cm,
    ):
        result = json.loads(
            await get_online_resource(
                class_object="network",
                method_name="Network.disconnect",
                ctx=mock_ctx,
            )
        )

    assert result["success"] is True
    # The banner leads the inline content and the doc body still follows it.
    assert result["content"].startswith("> **Naming note")
    assert "does NOT tear down" in result["content"]
    # It is persisted to the cache too, so read_resource returns it later.
    cached_content = mock_proxy.save_markdown_resource.call_args[0][0]
    assert cached_content.startswith("> **Naming note")


@pytest.mark.asyncio
async def test_get_online_resource_class_page(
    mcp, pypowsybl_proxies, mock_proxy, mock_ctx
):
    register_resource_tools(mcp, pypowsybl_proxies)
    get_online_resource = mcp.tools["get_online_resource"]

    client_cm, client = make_mock_async_client(html=OVERVIEW_HTML)
    with patch(
        "pypowsybl_mcp.tools.utils.resources.httpx.AsyncClient",
        return_value=client_cm,
    ):
        result = json.loads(
            await get_online_resource(class_object="network", ctx=mock_ctx)
        )

    assert result["success"] is True
    assert result["uri"] == "resources://temp/network"

    requested_url = client.get.call_args[0][0]
    assert requested_url == f"{URL_BASE}/network.html"

    _, resource_id = mock_proxy.save_markdown_resource.call_args[0]
    assert resource_id == "network"

    # Overview returns the compact method index, not the full page conversion,
    # and both the cached content and the response carry the index.
    cached_content = mock_proxy.save_markdown_resource.call_args[0][0]
    assert "- create_empty" in cached_content
    assert result["content"] == cached_content
    assert "method index" in result["message"].lower()


@pytest.mark.asyncio
async def test_get_online_resource_http_error(
    mcp, pypowsybl_proxies, mock_proxy, mock_ctx
):
    register_resource_tools(mcp, pypowsybl_proxies)
    get_online_resource = mcp.tools["get_online_resource"]

    client_cm, _ = make_mock_async_client(
        raise_for_status_error=httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=MagicMock()
        )
    )
    with patch(
        "pypowsybl_mcp.tools.utils.resources.httpx.AsyncClient",
        return_value=client_cm,
    ):
        result = json.loads(
            await get_online_resource(
                class_object="network", method_name="does_not_exist", ctx=mock_ctx
            )
        )

    assert result["success"] is False
    assert "Error fetching resource" in result["error"]
    mock_proxy.save_markdown_resource.assert_not_called()


# --- read_resource ---


@pytest.mark.asyncio
async def test_read_resource_cache_hit(mcp, pypowsybl_proxies, mock_proxy, mock_ctx):
    register_resource_tools(mcp, pypowsybl_proxies)
    read_resource = mcp.tools["read_resource"]

    mock_proxy.resources.get.return_value = "# cached markdown"

    result = json.loads(
        await read_resource(resource_id="network-create_empty", ctx=mock_ctx)
    )

    assert result["success"] is True
    assert result["uri"] == "resources://temp/network-create_empty"
    assert result["content"] == "# cached markdown"
    mock_proxy.resources.get.assert_called_once_with("network-create_empty")


@pytest.mark.asyncio
async def test_read_resource_accepts_full_uri(
    mcp, pypowsybl_proxies, mock_proxy, mock_ctx
):
    register_resource_tools(mcp, pypowsybl_proxies)
    read_resource = mcp.tools["read_resource"]

    mock_proxy.resources.get.return_value = "# cached markdown"

    result = json.loads(
        await read_resource(
            resource_id="resources://temp/network-create_empty", ctx=mock_ctx
        )
    )

    assert result["success"] is True
    # The URI prefix is stripped before the cache lookup
    mock_proxy.resources.get.assert_called_once_with("network-create_empty")


@pytest.mark.asyncio
async def test_read_resource_truncates_large_content(
    mcp, pypowsybl_proxies, mock_proxy, mock_ctx
):
    register_resource_tools(mcp, pypowsybl_proxies)
    read_resource = mcp.tools["read_resource"]

    mock_proxy.resources.get.return_value = "A" * (MAX_INLINE_CONTENT_CHARS + 100)

    result = json.loads(await read_resource(resource_id="network", ctx=mock_ctx))

    assert result["success"] is True
    # The full blob is not echoed back verbatim; only the capped head plus a note.
    assert "A" * (MAX_INLINE_CONTENT_CHARS + 100) not in result["content"]
    assert result["content"].startswith("A" * MAX_INLINE_CONTENT_CHARS)
    assert "truncated" in result["content"]
    assert result["uri"] in result["content"]


@pytest.mark.asyncio
async def test_read_resource_cache_miss(mcp, pypowsybl_proxies, mock_proxy, mock_ctx):
    register_resource_tools(mcp, pypowsybl_proxies)
    read_resource = mcp.tools["read_resource"]

    mock_proxy.resources.get.return_value = None

    result = json.loads(await read_resource(resource_id="network", ctx=mock_ctx))

    assert result["success"] is False
    assert "not found or has expired" in result["error"]
    assert "get_online_resource" in result["error"]
