#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.security_tools import SecurityTools, register_security_tools


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


def test_register_security_tools_excludes_private_helpers():
    mcp = MockMCP()
    proxies = TTLCache(maxsize=10, ttl=3600)
    register_security_tools(mcp, proxies)

    assert "run_security_analysis" in mcp.tools
    assert "create_contingencies_list" in mcp.tools
    assert "get_overloaded_elements" in mcp.tools

    # Private helpers must never be registered as MCP tools (a leading
    # underscore alone does not exclude a method — see docs/plugins.md).
    for helper in [
        "_parse_json_if_needed",
        "_status_name",
        "_limit_type_name",
        "_limit_violation_data",
        "_loading_and_excess",
        "_element_nominal_voltage",
        "_build_contingencies_from_filter",
        "_resolve_contingencies",
        "_compute_ranked_contingencies",
        "_flatten_ranked_violations",
        "_overloaded_in_normal_operation",
        "_overloaded_after_contingencies",
    ]:
        assert helper not in mcp.tools


@pytest.fixture
def sa_tools(pypowsybl_proxies):
    return SecurityTools(pypowsybl_proxies)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.mark.asyncio
async def test_run_security_analysis_success(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    # Ensure it's not the default MagicMock class to avoid spec issues if any
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    # We patch the object in the module where it's used
    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        # We need SecurityAnalysis() to return something that has run_ac()
        mock_sa = MagicMock()
        mock_security.SecurityAnalysis.return_value = mock_sa

        # We must also mock create_analysis() if it's used
        mock_security.create_analysis.return_value = mock_sa

        mock_results = MagicMock()
        mock_sa.run_ac.return_value = mock_results

        # Mock pre-contingency result
        # We use a regular dict or a class to avoid MagicMock serializability issues
        class Status:
            def __init__(self, name):
                self.name = name

        class PreResult:
            def __init__(self, status_name):
                self.status = Status(status_name)
                self.limit_violations = []

        mock_results.pre_contingency_result = PreResult("CONVERGED")

        # Mock post-contingency results
        mock_results.post_contingency_results = {}

        result = await sa_tools.run_security_analysis(
            network_id="net1",
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            ctx=mock_ctx,
        )

        try:
            data = json.loads(result)
            if not data["success"]:
                pytest.fail(f"Analysis failed: {data.get('error')}")
        except json.JSONDecodeError:
            pass

        assert '"success": true' in result
        assert data["mode"] == "summary"
        assert "top_violating_contingencies" in data
        mock_sa.add_single_element_contingency.assert_called_with("l1", "C1")
        mock_sa.run_ac.assert_called_once_with(mock_net)


@pytest.mark.asyncio
async def test_create_contingencies_list_success(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()

    # Mock lines for automatic contingency creation
    mock_net.get_lines.return_value = pd.DataFrame(index=["l1", "l2"])
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await sa_tools.create_contingencies_list(
        network_id="net1", element_type="lines", ctx=mock_ctx
    )

    assert "l1" in result
    assert "l2" in result


@pytest.mark.asyncio
async def test_create_contingencies_list_pagination(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        index=[f"l{i}" for i in range(5)],
    )
    mock_net.get_voltage_levels.return_value = pd.DataFrame()
    proxy.networks["net1"] = mock_net
    result = await sa_tools.create_contingencies_list(
        network_id="net1",
        element_type="lines",
        limit=2,
        cursor="1",
        ctx=mock_ctx,
    )
    data = json.loads(result)
    assert data["filtered_count"] == 5
    assert len(data["contingencies"]) == 2
    assert data["pagination"]["cursor"] == "1"


@pytest.mark.asyncio
async def test_get_overloaded_elements_study_n(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    filter_response = json.dumps(
        {
            "success": True,
            "network_id": "net1",
            "variant_id": "InitialState",
            "limit_kind": "permanent",
            "elements": {
                "l1": {"loading_percent": 110.0, "i1": 110.0},
            },
        }
    )

    with patch(
        "pypowsybl_mcp.tools.security_tools.NetworkTools.get_network_element_data",
        new=AsyncMock(return_value=filter_response),
    ):
        result = await sa_tools.get_overloaded_elements(
            network_id="net1",
            study="n",
            threshold_percent=100,
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["study"] == "n"
    assert data["matched_count"] == 1
    assert data["overloaded"][0]["element_id"] == "l1"
    assert data["overloaded"][0]["loading_percent"] == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_get_overloaded_elements_study_n1(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"
    proxy.loadflow_results["net1"] = {"converged": True}

    sa_response = json.dumps(
        {
            "success": True,
            "network_id": "net1",
            "post_contingency": {
                "total_contingencies": 2,
                "contingencies_with_violations": 1,
            },
            "contingencies_with_violations": [
                {
                    "contingency_id": "l1_contingency",
                    "status": "CONVERGED",
                    "violations": [
                        {
                            "subject_id": "l3",
                            "limit_type": "CURRENT",
                            "limit": 100.0,
                            "value": 105.0,
                            "loading_percent": 105.0,
                            "excess_percent": 5.0,
                        },
                        {
                            "subject_id": "l4",
                            "limit_type": "CURRENT",
                            "limit": 100.0,
                            "value": 90.0,
                            "loading_percent": 90.0,
                            "excess_percent": -10.0,
                        },
                    ],
                }
            ],
        }
    )

    with patch.object(
        sa_tools,
        "run_security_analysis",
        new=AsyncMock(return_value=sa_response),
    ):
        result = await sa_tools.get_overloaded_elements(
            network_id="net1",
            study="n1",
            threshold_percent=100,
            contingencies=[{"element_id": "l1", "contingency_id": "l1_contingency"}],
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["study"] == "n1"
    assert data["matched_count"] == 1
    assert data["overloaded"][0]["element_id"] == "l3"
    assert data["overloaded"][0]["loading_percent"] == pytest.approx(105.0)
    assert data["overloaded"][0]["contingency_id"] == "l1_contingency"


@pytest.mark.asyncio
async def test_get_overloaded_elements_n1_requires_loadflow(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await sa_tools.get_overloaded_elements(
        network_id="net1",
        study="n1",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "loadflow" in data["error"].lower()


@pytest.mark.asyncio
async def test_get_overloaded_elements_rejects_unknown_study(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await sa_tools.get_overloaded_elements(
        network_id="net1",
        study="n2",
        ctx=mock_ctx,
    )

    data = json.loads(result)
    assert data["success"] is False
    assert "n2" in data["error"]

@pytest.mark.asyncio
async def test_run_security_analysis_auto_contingencies(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(
        {
            "voltage_level1_id": ["VL1", "VL2"],
            "voltage_level2_id": ["VL2", "VL2"],
        },
        index=["l1", "l2"],
    )
    mock_net.get_voltage_levels.return_value = pd.DataFrame(
        {"nominal_v": [225.0, 63.0]}, index=["VL1", "VL2"]
    )
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        mock_sa = MagicMock()
        mock_security.create_analysis.return_value = mock_sa
        mock_results = MagicMock()
        mock_results.pre_contingency_result = MagicMock(
            status=MagicMock(name="CONVERGED"), limit_violations=[]
        )
        mock_results.pre_contingency_result.status.name = "CONVERGED"
        mock_results.post_contingency_results = {}
        mock_sa.run_ac.return_value = mock_results

        result = await sa_tools.run_security_analysis(
            network_id="net1",
            auto_contingencies={"element_type": "lines", "min_nominal_voltage": 200.0},
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["contingency_source"] == "auto"
    mock_sa.add_single_element_contingency.assert_called_once_with(
        "l1", "l1_contingency"
    )


@pytest.mark.asyncio
async def test_run_security_analysis_mutual_exclusion(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await sa_tools.run_security_analysis(
        network_id="net1",
        contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
        auto_contingencies={"element_type": "lines"},
        ctx=mock_ctx,
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "mutually exclusive" in data["error"]


@pytest.mark.asyncio
async def test_run_security_analysis_detail_mode_is_limited(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        mock_sa = MagicMock()
        mock_security.create_analysis.return_value = mock_sa
        mock_results = MagicMock()
        mock_results.pre_contingency_result = MagicMock(
            status=MagicMock(name="CONVERGED"), limit_violations=[]
        )
        mock_results.pre_contingency_result.status.name = "CONVERGED"

        class Violation:
            subject_id = "line"
            limit = 100.0
            value = 120.0
            limit_type = MagicMock(name="CURRENT")
            limit_type.name = "CURRENT"

        class PostResult:
            def __init__(self):
                self.status = MagicMock(name="CONVERGED")
                self.status.name = "CONVERGED"
                self.limit_violations = [Violation()]

        mock_results.post_contingency_results = {
            "C1": PostResult(),
            "C2": PostResult(),
            "C3": PostResult(),
        }
        mock_sa.run_ac.return_value = mock_results

        result = await sa_tools.run_security_analysis(
            network_id="net1",
            contingencies=[
                {"element_id": "l1", "contingency_id": "C1"},
                {"element_id": "l2", "contingency_id": "C2"},
                {"element_id": "l3", "contingency_id": "C3"},
            ],
            mode="detail",
            detail_limit=2,
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["mode"] == "detail"
    assert data["returned_details"] == 2
    assert len(data["contingencies_with_violations"]) == 2


def _make_violation(subject_id, limit, value, limit_type_name):
    violation = MagicMock()
    violation.subject_id = subject_id
    violation.limit = limit
    violation.value = value
    violation.limit_type = MagicMock()
    violation.limit_type.name = limit_type_name
    return violation


class _PostResult:
    def __init__(self, violations):
        self.status = MagicMock()
        self.status.name = "CONVERGED"
        self.limit_violations = violations


@pytest.mark.asyncio
async def test_run_security_analysis_ranked_violations(sa_tools, mock_ctx):
    """ranked_violations is one flat table, most loaded first, percentages already filled in."""
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        mock_sa = MagicMock()
        mock_security.create_analysis.return_value = mock_sa
        mock_results = MagicMock()
        mock_results.pre_contingency_result = MagicMock(limit_violations=[])
        mock_results.pre_contingency_result.status.name = "CONVERGED"
        mock_results.post_contingency_results = {
            "C1": _PostResult([_make_violation("lineA", 100.0, 150.0, "CURRENT")]),
            "C2": _PostResult(
                [
                    _make_violation("busB", 400.0, 420.0, "HIGH_VOLTAGE"),
                    _make_violation("lineC", 100.0, 130.0, "CURRENT"),
                ]
            ),
        }
        mock_sa.run_ac.return_value = mock_results

        result = await sa_tools.run_security_analysis(
            network_id="net1",
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            limit_type="CURRENT",
            top_violations=10,
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["limit_type_filter"] == ["CURRENT"]
    # Only current violations are kept, most loaded first.
    ranked = data["ranked_violations"]
    assert [v["subject_id"] for v in ranked] == ["lineA", "lineC"]
    assert ranked[0]["loading_percent"] == pytest.approx(150.0)
    assert ranked[0]["excess_percent"] == pytest.approx(50.0)
    # The breakdown counts every type of violation, before we filter on current.
    assert data["post_contingency"]["violation_types_breakdown"] == {
        "CURRENT": 2,
        "HIGH_VOLTAGE": 1,
    }


@pytest.mark.asyncio
async def test_get_overloaded_elements_n1_returns_violations(sa_tools, mock_ctx):
    """After an N-1 event the overloaded lines must show up, not come back empty."""
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"
    proxy.loadflow_results["net1"] = {"converged": True}

    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        mock_sa = MagicMock()
        mock_security.create_analysis.return_value = mock_sa
        mock_results = MagicMock()
        mock_results.pre_contingency_result = MagicMock(limit_violations=[])
        mock_results.pre_contingency_result.status.name = "CONVERGED"
        mock_results.post_contingency_results = {
            "C1": _PostResult([_make_violation("line_A", 100.0, 130.0, "CURRENT")]),
        }
        mock_sa.run_ac.return_value = mock_results

        result = await sa_tools.get_overloaded_elements(
            network_id="net1",
            element_type="lines",
            study="n1",
            threshold_percent=100.0,
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["study"] == "n1"
    assert data["matched_count"] == 1
    assert data["overloaded"][0]["element_id"] == "line_A"
    assert data["overloaded"][0]["loading_percent"] == pytest.approx(130.0)


# --- run_security_analysis: input validation / edge cases ---


@pytest.mark.asyncio
async def test_run_security_analysis_no_network_selected(sa_tools, mock_ctx):
    result = await sa_tools.run_security_analysis(network_id=None, ctx=mock_ctx)
    data = json.loads(result)
    assert data["success"] is False
    assert "no current network" in data["error"].lower()


@pytest.mark.asyncio
async def test_run_security_analysis_uses_current_network_id(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        mock_sa = MagicMock()
        mock_security.create_analysis.return_value = mock_sa
        mock_results = MagicMock()
        mock_results.pre_contingency_result = MagicMock(limit_violations=[])
        mock_results.pre_contingency_result.status.name = "CONVERGED"
        mock_results.post_contingency_results = {}
        mock_sa.run_ac.return_value = mock_results

        result = await sa_tools.run_security_analysis(
            network_id=None,
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["network_id"] == "net1"


@pytest.mark.asyncio
async def test_run_security_analysis_network_not_found(sa_tools, mock_ctx):
    result = await sa_tools.run_security_analysis(network_id="missing", ctx=mock_ctx)
    data = json.loads(result)
    assert data["success"] is False
    assert "missing" in data["error"]


@pytest.mark.asyncio
async def test_run_security_analysis_invalid_mode(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await sa_tools.run_security_analysis(
        network_id="net1", mode="bogus", ctx=mock_ctx
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "mode" in data["error"].lower()


@pytest.mark.asyncio
async def test_run_security_analysis_invalid_top_k(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await sa_tools.run_security_analysis(
        network_id="net1", top_k=0, ctx=mock_ctx
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "top_k" in data["error"]


@pytest.mark.asyncio
async def test_run_security_analysis_invalid_detail_limit(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await sa_tools.run_security_analysis(
        network_id="net1", detail_limit=0, ctx=mock_ctx
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "detail_limit" in data["error"]


@pytest.mark.asyncio
async def test_run_security_analysis_invalid_top_violations(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await sa_tools.run_security_analysis(
        network_id="net1", top_violations=0, ctx=mock_ctx
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "top_violations" in data["error"]


@pytest.mark.asyncio
async def test_run_security_analysis_pre_contingency_violations(sa_tools, mock_ctx):
    """Pre-contingency violations must be included with loading/excess percent."""
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        mock_sa = MagicMock()
        mock_security.create_analysis.return_value = mock_sa
        mock_results = MagicMock()
        mock_results.pre_contingency_result = MagicMock(
            limit_violations=[_make_violation("lineX", 100.0, 140.0, "CURRENT")]
        )
        mock_results.pre_contingency_result.status.name = "CONVERGED"
        mock_results.post_contingency_results = {}
        mock_sa.run_ac.return_value = mock_results

        result = await sa_tools.run_security_analysis(
            network_id="net1",
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is True
    pre_violations = data["pre_contingency"]["violations"]
    assert len(pre_violations) == 1
    assert pre_violations[0]["subject_id"] == "lineX"
    assert pre_violations[0]["loading_percent"] == pytest.approx(140.0)
    assert pre_violations[0]["excess_percent"] == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_run_security_analysis_pagination_invalid_cursor(sa_tools, mock_ctx):
    """An invalid cursor bubbles up as a controlled JSON error, not an exception."""
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        mock_sa = MagicMock()
        mock_security.create_analysis.return_value = mock_sa
        mock_results = MagicMock()
        mock_results.pre_contingency_result = MagicMock(limit_violations=[])
        mock_results.pre_contingency_result.status.name = "CONVERGED"
        mock_results.post_contingency_results = {
            "C1": _PostResult([_make_violation("lineA", 100.0, 150.0, "CURRENT")]),
        }
        mock_sa.run_ac.return_value = mock_results

        result = await sa_tools.run_security_analysis(
            network_id="net1",
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            mode="detail",
            limit=1,
            cursor="not-a-number",
            ctx=mock_ctx,
        )

    data = json.loads(result)
    assert data["success"] is False
    assert "cursor" in data["error"].lower()


# --- create_contingencies_list: input validation / edge cases ---


@pytest.mark.asyncio
async def test_create_contingencies_list_no_network_selected(sa_tools, mock_ctx):
    result = await sa_tools.create_contingencies_list(network_id=None, ctx=mock_ctx)
    data = json.loads(result)
    assert data["success"] is False
    assert "no current network" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_contingencies_list_network_not_found(sa_tools, mock_ctx):
    result = await sa_tools.create_contingencies_list(
        network_id="missing", ctx=mock_ctx
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "missing" in data["error"]


@pytest.mark.asyncio
async def test_create_contingencies_list_unsupported_type(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await sa_tools.create_contingencies_list(
        network_id="net1", element_type="not_a_real_type", ctx=mock_ctx
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["network_id"] == "net1"
    assert "Unsupported element type" in data["error"]


@pytest.mark.asyncio
async def test_create_contingencies_list_generic_exception(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.side_effect = RuntimeError("network access failed")
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await sa_tools.create_contingencies_list(
        network_id="net1", element_type="lines", ctx=mock_ctx
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["network_id"] == "net1"
    assert "network access failed" in data["error"]


@pytest.mark.asyncio
async def test_create_contingencies_list_pagination_invalid_cursor(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net.get_lines.return_value = pd.DataFrame(index=["l1", "l2"])
    mock_net.get_voltage_levels.return_value = pd.DataFrame()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"

    result = await sa_tools.create_contingencies_list(
        network_id="net1",
        element_type="lines",
        limit=1,
        cursor="not-a-number",
        ctx=mock_ctx,
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "cursor" in data["error"].lower()


# --- get_overloaded_elements: input validation / edge cases ---


@pytest.mark.asyncio
async def test_get_overloaded_elements_no_network_selected(sa_tools, mock_ctx):
    result = await sa_tools.get_overloaded_elements(network_id=None, ctx=mock_ctx)
    data = json.loads(result)
    assert data["success"] is False
    assert "no current network" in data["error"].lower()


@pytest.mark.asyncio
async def test_get_overloaded_elements_uses_current_network_id(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    filter_response = json.dumps(
        {
            "success": True,
            "network_id": "net1",
            "variant_id": "InitialState",
            "limit_kind": "permanent",
            "elements": {},
        }
    )

    with patch(
        "pypowsybl_mcp.tools.security_tools.NetworkTools.get_network_element_data",
        new=AsyncMock(return_value=filter_response),
    ):
        result = await sa_tools.get_overloaded_elements(
            network_id=None, study="n", ctx=mock_ctx
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["network_id"] == "net1"


@pytest.mark.asyncio
async def test_get_overloaded_elements_network_not_found(sa_tools, mock_ctx):
    result = await sa_tools.get_overloaded_elements(network_id="missing", ctx=mock_ctx)
    data = json.loads(result)
    assert data["success"] is False
    assert "missing" in data["error"]


@pytest.mark.asyncio
async def test_get_overloaded_elements_invalid_threshold(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    result = await sa_tools.get_overloaded_elements(
        network_id="net1", threshold_percent="not-a-number", ctx=mock_ctx
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "threshold_percent" in data["error"]


@pytest.mark.asyncio
async def test_overloaded_in_normal_operation_invalid_json(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    with patch(
        "pypowsybl_mcp.tools.security_tools.NetworkTools.get_network_element_data",
        new=AsyncMock(return_value="not valid json"),
    ):
        result = await sa_tools.get_overloaded_elements(
            network_id="net1", study="n", ctx=mock_ctx
        )

    data = json.loads(result)
    assert data["success"] is False
    assert data["network_id"] == "net1"


@pytest.mark.asyncio
async def test_overloaded_in_normal_operation_upstream_failure(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.current_network_id = "net1"

    failure_response = json.dumps(
        {"success": False, "error": "element_type 'bogus' is not supported"}
    )

    with patch(
        "pypowsybl_mcp.tools.security_tools.NetworkTools.get_network_element_data",
        new=AsyncMock(return_value=failure_response),
    ):
        result = await sa_tools.get_overloaded_elements(
            network_id="net1", study="n", element_type="bogus", ctx=mock_ctx
        )

    assert result == failure_response


@pytest.mark.asyncio
async def test_overloaded_after_contingencies_auto_generates_list(sa_tools, mock_ctx):
    """When contingencies is omitted for study=n1, the tool auto-builds it."""
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"
    proxy.loadflow_results["net1"] = {"converged": True}

    contingencies_response = json.dumps(
        {
            "success": True,
            "contingencies": [{"element_id": "l1", "contingency_id": "l1_contingency"}],
        }
    )
    sa_response = json.dumps(
        {
            "success": True,
            "post_contingency": {
                "total_contingencies": 1,
                "contingencies_with_violations": 1,
            },
            "contingencies_with_violations": [
                {
                    "contingency_id": "l1_contingency",
                    "status": "CONVERGED",
                    "violations": [
                        {
                            "subject_id": "l1",
                            "limit_type": "CURRENT",
                            "limit": 100.0,
                            "value": 120.0,
                            "loading_percent": 120.0,
                            "excess_percent": 20.0,
                        }
                    ],
                }
            ],
        }
    )

    with (
        patch.object(
            sa_tools,
            "create_contingencies_list",
            new=AsyncMock(return_value=contingencies_response),
        ) as mock_create,
        patch.object(
            sa_tools, "run_security_analysis", new=AsyncMock(return_value=sa_response)
        ),
    ):
        result = await sa_tools.get_overloaded_elements(
            network_id="net1", study="n1", threshold_percent=100.0, ctx=mock_ctx
        )

    mock_create.assert_awaited_once()
    data = json.loads(result)
    assert data["success"] is True
    assert data["matched_count"] == 1
    assert data["overloaded"][0]["element_id"] == "l1"


@pytest.mark.asyncio
async def test_overloaded_after_contingencies_auto_generation_failure(sa_tools, mock_ctx):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"
    proxy.loadflow_results["net1"] = {"converged": True}

    failure_response = json.dumps(
        {"success": False, "error": "Unsupported element type 'weird'"}
    )

    with patch.object(
        sa_tools,
        "create_contingencies_list",
        new=AsyncMock(return_value=failure_response),
    ):
        result = await sa_tools.get_overloaded_elements(
            network_id="net1", study="n1", element_type="weird", ctx=mock_ctx
        )

    assert result == failure_response


@pytest.mark.asyncio
async def test_overloaded_after_contingencies_security_analysis_failure(
    sa_tools, mock_ctx
):
    proxy = sa_tools.get_proxy("test-session")
    mock_net = MagicMock()
    mock_net._handle = MagicMock()
    proxy.networks["net1"] = mock_net
    proxy.current_network_id = "net1"
    proxy.loadflow_results["net1"] = {"converged": True}

    failure_response = json.dumps({"success": False, "error": "solver crashed"})

    with patch.object(
        sa_tools, "run_security_analysis", new=AsyncMock(return_value=failure_response)
    ):
        result = await sa_tools.get_overloaded_elements(
            network_id="net1",
            study="n1",
            threshold_percent=100.0,
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            ctx=mock_ctx,
        )

    assert result == failure_response
