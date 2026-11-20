#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""IIDM extensions: the extension tools and the attach tokens reading them."""

import json
from unittest.mock import MagicMock

import pypowsybl as pp
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.extension_tools import ExtensionTools
from pypowsybl_mcp.tools.network_tools import NetworkTools


class _Ctx:
    def __init__(self):
        self.session = MagicMock()
        self.session.session_id = "extensions"
        self.request_id = "test"


@pytest.fixture
def ctx():
    return _Ctx()


@pytest.fixture
def proxies():
    return TTLCache(maxsize=10, ttl=3600)


@pytest.fixture
def tools(proxies, ctx):
    """(extension tools, network tools) on an IEEE 14 carrying both extensions."""
    network = pp.network.create_ieee14()
    network.create_extensions(
        "detail",
        id=["B2-L", "B3-L"],
        fixed_p0=[5.0, 10.0],
        variable_p0=[16.7, 84.2],
        fixed_q0=[1.0, 2.0],
        variable_q0=[11.7, 17.0],
    )
    network.create_extensions("entsoeCategory", id="B1-G", code=2)
    extension_tools = ExtensionTools(proxies)
    extension_tools.get_proxy("extensions").register_network("ieee14", network, True)
    return extension_tools, NetworkTools(proxies)


@pytest.mark.asyncio
async def test_list_network_extensions(tools, ctx):
    extension_tools, _ = tools
    result = json.loads(await extension_tools.list_network_extensions(ctx=ctx))
    assert result["success"]
    by_name = {e["name"]: e for e in result["extensions"]}
    assert by_name["detail"]["count"] == 2
    assert by_name["detail"]["columns"] == [
        "fixed_p0",
        "variable_p0",
        "fixed_q0",
        "variable_q0",
    ]
    assert by_name["entsoeCategory"]["count"] == 1
    # Only the extensions some element carries
    assert all(e["count"] > 0 for e in result["extensions"])
    assert "load_detail" in result["attach_tokens"]


@pytest.mark.asyncio
async def test_list_network_extensions_without_network(proxies, ctx):
    result = json.loads(await ExtensionTools(proxies).list_network_extensions(ctx=ctx))
    assert not result["success"]


@pytest.mark.asyncio
async def test_get_network_extension_data(tools, ctx):
    extension_tools, _ = tools
    result = json.loads(
        await extension_tools.get_network_extension_data("detail", ctx=ctx)
    )
    assert result["total_elements"] == 2
    assert result["elements"]["B3-L"] == {
        "fixed_p0": 10.0,
        "variable_p0": 84.2,
        "fixed_q0": 2.0,
        "variable_q0": 17.0,
    }

    # Case-insensitive name, filtered on ids
    result = json.loads(
        await extension_tools.get_network_extension_data(
            "Detail", ids=["B2-L"], ctx=ctx
        )
    )
    assert list(result["elements"]) == ["B2-L"]

    # Paginated
    result = json.loads(
        await extension_tools.get_network_extension_data("detail", limit=1, ctx=ctx)
    )
    assert result["pagination"]["nextCursor"] == "1"


@pytest.mark.asyncio
async def test_get_network_extension_data_errors(tools, ctx):
    extension_tools, _ = tools
    result = json.loads(
        await extension_tools.get_network_extension_data("nope", ctx=ctx)
    )
    assert not result["success"]
    assert "Unknown extension" in result["error"]

    result = json.loads(
        await extension_tools.get_network_extension_data(
            "detail", variant_id="nope", ctx=ctx
        )
    )
    assert not result["success"]


@pytest.mark.asyncio
async def test_extension_absent_from_the_network_gives_no_rows(tools, ctx):
    extension_tools, _ = tools
    result = json.loads(
        await extension_tools.get_network_extension_data(
            "busbarSectionPosition", ctx=ctx
        )
    )
    assert result["success"]
    assert result["total_elements"] == 0


@pytest.mark.asyncio
async def test_attach_load_detail_feeds_conditions(tools, ctx):
    _, network_tools = tools
    result = json.loads(
        await network_tools.get_network_element_data(
            element_type="load",
            attach=["load_detail"],
            conditions=[{"field": "variable_p0", "op": ">", "value": 50}],
            ctx=ctx,
        )
    )
    assert result["matched_count"] == 1
    assert result["elements"]["B3-L"]["fixed_p0"] == 10.0


@pytest.mark.asyncio
async def test_attach_entsoe_category_keeps_integer_codes(tools, ctx):
    _, network_tools = tools
    result = json.loads(
        await network_tools.get_network_element_data(
            element_type="generator", attach=["entsoe_category"], limit=5, ctx=ctx
        )
    )
    codes = {k: v["entsoe_category_code"] for k, v in result["elements"].items()}
    assert codes["B1-G"] == 2
    assert isinstance(codes["B1-G"], int)
    # A generator without the extension gets null
    assert codes["B2-G"] is None


@pytest.mark.asyncio
async def test_attach_any_extension_by_name(tools, ctx):
    _, network_tools = tools
    result = json.loads(
        await network_tools.get_network_element_data(
            element_type="generator",
            attach=["extension:entsoeCategory"],
            limit=5,
            ctx=ctx,
        )
    )
    assert result["elements"]["B1-G"]["entsoeCategory_code"] == 2


@pytest.mark.asyncio
async def test_attach_extension_errors(tools, ctx):
    _, network_tools = tools
    result = json.loads(
        await network_tools.get_network_element_data(
            element_type="generator", attach=["load_detail"], ctx=ctx
        )
    )
    assert "applies to element_type='load'" in result["error"]

    result = json.loads(
        await network_tools.get_network_element_data(
            element_type="load", attach=["extension:nope"], ctx=ctx
        )
    )
    assert "Unknown extension" in result["error"]

    result = json.loads(
        await network_tools.get_network_element_data(
            element_type="load", attach=["foo"], ctx=ctx
        )
    )
    assert "extension:<extension name>" in result["error"]
