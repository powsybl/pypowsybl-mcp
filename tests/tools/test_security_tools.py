#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import json
from unittest.mock import MagicMock, patch

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

    # Private helpers must never be registered as MCP tools (a leading
    # underscore alone does not exclude a method — see docs/plugins.md).
    for helper in [
        "_parse_json_if_needed",
        "_status_name",
        "_limit_type_name",
        "_limit_violation_data",
        "_loading_and_excess",
        "_converter_station_voltages",
        "_element_nominal_voltage",
        "_build_contingencies_from_filter",
        "_resolve_contingencies",
        "_compute_ranked_contingencies",
        "_flatten_ranked_violations",
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
        network_id="net1", element_type="line", ctx=mock_ctx
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
        element_type="line",
        limit=2,
        cursor="1",
        ctx=mock_ctx,
    )
    data = json.loads(result)
    assert data["filtered_count"] == 5
    assert len(data["contingencies"]) == 2
    assert data["pagination"]["cursor"] == "1"


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
            auto_contingencies={"element_type": "line", "min_nominal_voltage": 200.0},
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
        auto_contingencies={"element_type": "line"},
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


async def _run_sa(sa_tools, mock_ctx, post_results, pre_violations=None, **kwargs):
    """Run run_security_analysis against mocked pre/post results, return the JSON."""
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
            limit_violations=pre_violations or []
        )
        mock_results.pre_contingency_result.status.name = "CONVERGED"
        mock_results.post_contingency_results = post_results
        mock_sa.run_ac.return_value = mock_results

        result = await sa_tools.run_security_analysis(
            network_id="net1",
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            ctx=mock_ctx,
            **kwargs,
        )
    return json.loads(result)


@pytest.mark.asyncio
async def test_run_security_analysis_threshold_filters_every_view(sa_tools, mock_ctx):
    # C1 has a 150% overload, C2 a 130% one. threshold=135 keeps only C1's
    # violation everywhere, not just in ranked_violations.
    post = {
        "C1": _PostResult([_make_violation("lineA", 100.0, 150.0, "CURRENT")]),
        "C2": _PostResult([_make_violation("lineC", 100.0, 130.0, "CURRENT")]),
    }
    data = await _run_sa(
        sa_tools, mock_ctx, post, limit_type="CURRENT", threshold_percent=135
    )

    assert [v["subject_id"] for v in data["ranked_violations"]] == ["lineA"]
    # top_violating_contingencies and the counts drop C2 too (0 violations left).
    top = data["top_violating_contingencies"]
    assert [c["contingency_id"] for c in top] == ["C1"]
    assert top[0]["violation_count"] == 1
    assert data["post_contingency"]["total_violations"] == 1
    assert data["post_contingency"]["contingencies_with_violations"] == 1
    # ...but the breakdown stays the full census of the study.
    assert data["post_contingency"]["violation_types_breakdown"] == {"CURRENT": 2}
    assert data["post_contingency"]["all_violations_count"] == 2


@pytest.mark.asyncio
async def test_run_security_analysis_threshold_filters_detail_mode(sa_tools, mock_ctx):
    # Detail mode's contingencies_with_violations is filtered too: C2's only
    # violation is sub-threshold, so the whole contingency leaves the list.
    post = {
        "C1": _PostResult([_make_violation("lineA", 100.0, 150.0, "CURRENT")]),
        "C2": _PostResult([_make_violation("lineC", 100.0, 130.0, "CURRENT")]),
    }
    data = await _run_sa(
        sa_tools,
        mock_ctx,
        post,
        limit_type="CURRENT",
        threshold_percent=135,
        mode="detail",
    )

    ids = [c["contingency_id"] for c in data["contingencies_with_violations"]]
    assert ids == ["C1"]
    assert data["returned_details"] == 1


@pytest.mark.asyncio
async def test_run_security_analysis_threshold_spares_voltage_violations(
    sa_tools, mock_ctx
):
    # With limit_type=None all types flow through, and the threshold is CURRENT
    # only: a LOW_VOLTAGE row (loading < 100) survives while a sub-threshold
    # CURRENT row is dropped.
    post = {
        "C1": _PostResult(
            [
                _make_violation("busLow", 0.95, 0.90, "LOW_VOLTAGE"),  # ~94.7%
                _make_violation("lineA", 100.0, 150.0, "CURRENT"),  # 150%
                _make_violation("lineB", 100.0, 110.0, "CURRENT"),  # 110%
            ]
        ),
    }
    data = await _run_sa(sa_tools, mock_ctx, post, threshold_percent=120)

    subjects = {v["subject_id"] for v in data["ranked_violations"]}
    assert "busLow" in subjects  # voltage kept despite loading < 100
    assert "lineA" in subjects  # CURRENT 150% kept
    assert "lineB" not in subjects  # CURRENT 110% dropped
    assert data["post_contingency"]["total_violations"] == 2


@pytest.mark.asyncio
async def test_run_security_analysis_threshold_below_100_keeps_all_overloads(
    sa_tools, mock_ctx
):
    # The SA only reports elements already over their limit, so everything it
    # reports is above 100%: a threshold below 100 is a no-op, it cannot surface
    # near-limit branches.
    post = {
        "C1": _PostResult(
            [
                _make_violation("lineA", 100.0, 105.0, "CURRENT"),
                _make_violation("lineB", 100.0, 130.0, "CURRENT"),
            ]
        ),
    }
    data = await _run_sa(
        sa_tools, mock_ctx, post, limit_type="CURRENT", threshold_percent=90
    )

    assert {v["subject_id"] for v in data["ranked_violations"]} == {"lineA", "lineB"}
    assert data["post_contingency"]["total_violations"] == 2


@pytest.mark.asyncio
async def test_run_security_analysis_rejects_negative_threshold(sa_tools, mock_ctx):
    data = await _run_sa(sa_tools, mock_ctx, {}, threshold_percent=-5)

    assert data["success"] is False
    assert ">= 0" in data["error"]


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
        network_id="net1", element_type="line", ctx=mock_ctx
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
        element_type="line",
        limit=1,
        cursor="not-a-number",
        ctx=mock_ctx,
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "cursor" in data["error"].lower()
