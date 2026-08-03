#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

from cachetools import TTLCache

from pypowsybl_mcp.proxy import PyPowsyblMCPServerProxy
from pypowsybl_mcp.tools import PyPowsyblTool, wrap_class_methods_with_mcp_tool


class MockMCP:
    def __init__(self):
        self.tool_decorator = MagicMock()
        self.tool = MagicMock(return_value=self.tool_decorator)


class SampleTool:
    def method_one(self, x: int) -> int:
        return x + 1

    def method_two(self, y: str) -> str:
        return y.upper()

    def _private_method(self):
        pass

    def __magic_method__(self):
        pass


def test_wrap_class_methods_with_mcp_tool():
    mcp = MockMCP()
    obj = SampleTool()

    # We need to be careful because wrap_class_methods_with_mcp_tool
    # modifies the instance by setting attributes.
    # It gets methods from the CLASS dict.

    wrap_class_methods_with_mcp_tool(obj, mcp, exclude=["method_two"])

    # method_one should be wrapped
    assert mcp.tool.called
    # method_two should be excluded
    # _private_method starts with _, but the function only checks for __
    # Actually the code says: if not name.startswith("__") and callable(func) and name not in exclude:
    # So _private_method SHOULD be wrapped.

    # Let's check what was wrapped.
    # The tool() decorator is called for each method.
    wrapped_methods = [call[0][0].__name__ for call in mcp.tool().call_args_list]
    assert "method_one" in wrapped_methods
    assert "_private_method" in wrapped_methods
    assert "method_two" not in wrapped_methods
    assert "__magic_method__" not in wrapped_methods


def test_pypowsybl_tool_get_proxy():
    proxies = TTLCache(maxsize=10, ttl=3600)
    tool = PyPowsyblTool(proxies)

    # Test creating a new proxy
    proxy1 = tool.get_proxy("session1")
    assert isinstance(proxy1, PyPowsyblMCPServerProxy)
    assert "session1" in proxies
    assert proxies["session1"] == proxy1

    # Test retrieving the same proxy
    proxy2 = tool.get_proxy("session1")
    assert proxy1 is proxy2


def test_pypowsybl_tool_register_tools_with_mcp():
    mcp = MockMCP()
    proxies = TTLCache(maxsize=10, ttl=3600)

    class MySpecificTool(PyPowsyblTool):
        def my_tool_method(self):
            pass

    tool = MySpecificTool(proxies)

    with patch("pypowsybl_mcp.tools.wrap_class_methods_with_mcp_tool") as mock_wrap:
        tool.register_tools_with_mcp(mcp, exclude=["something"])
        mock_wrap.assert_called_once_with(tool, mcp, ["something"])
