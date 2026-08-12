#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.utils.code_export import CodeTools, register_code_tools


class MockContext:
    def __init__(self, session_id="test-session"):
        self.session = MagicMock()
        self.session.session_id = session_id


@pytest.fixture
def pypowsybl_proxies():
    return TTLCache(maxsize=10, ttl=3600)


@pytest.fixture
def code_tools(pypowsybl_proxies):
    return CodeTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.mark.asyncio
async def test_generate_python_script_success(code_tools, mock_ctx):
    with (
        patch(
            "pypowsybl_mcp.tools.utils.code_export.generate_code_from_macro"
        ) as mock_gen_code,
        patch(
            "pypowsybl_mcp.tools.utils.code_export.generate_download_link"
        ) as mock_gen_link,
    ):
        mock_gen_code.return_value = "import pypowsybl as pp\n# some code"
        mock_gen_link.return_value = {
            "download_url": "http://localhost/download/script.py"
        }

        result = await code_tools.generate_python_script(
            actions="Do something", script_name="test.py", ctx=mock_ctx
        )

        assert result == "http://localhost/download/script.py"
        mock_gen_code.assert_called_once()
        mock_gen_link.assert_called_once()


@pytest.mark.asyncio
async def test_generate_python_script_error(code_tools, mock_ctx):
    with patch(
        "pypowsybl_mcp.tools.utils.code_export.generate_code_from_macro"
    ) as mock_gen_code:
        mock_gen_code.return_value = "# Error: Something went wrong"

        with pytest.raises(Exception) as excinfo:
            await code_tools.generate_python_script(
                actions="Do something", ctx=mock_ctx
            )

        assert "Something went wrong" in str(excinfo.value)


def test_register_code_tools():
    mcp = MagicMock()
    proxies = TTLCache(maxsize=10, ttl=3600)

    with patch(
        "pypowsybl_mcp.tools.utils.code_export.CodeTools.register_tools_with_mcp"
    ) as mock_register:
        register_code_tools(mcp, proxies)
        mock_register.assert_called_once_with(mcp)


@pytest.mark.asyncio
async def test_generate_python_script_handles_unreadable_source_file(
    code_tools, mock_ctx
):
    # _get_relevant_content should tolerate a source file it cannot read
    # (e.g. a permission error) and simply skip it instead of raising.
    real_open = open

    def flaky_open(path, *args, **kwargs):
        if str(path).endswith("broken_module.py"):
            raise OSError("permission denied")
        return real_open(path, *args, **kwargs)

    with (
        patch("os.listdir", return_value=["broken_module.py"]),
        patch(
            "pypowsybl_mcp.tools.utils.code_export.open",
            side_effect=flaky_open,
            create=True,
        ),
        patch(
            "pypowsybl_mcp.tools.utils.code_export.generate_code_from_macro"
        ) as mock_gen_code,
        patch(
            "pypowsybl_mcp.tools.utils.code_export.generate_download_link"
        ) as mock_gen_link,
    ):
        mock_gen_code.return_value = "import pypowsybl as pp\n# some code"
        mock_gen_link.return_value = {
            "download_url": "http://localhost/download/script.py"
        }

        result = await code_tools.generate_python_script(
            actions="Do something", ctx=mock_ctx
        )

        assert result == "http://localhost/download/script.py"
