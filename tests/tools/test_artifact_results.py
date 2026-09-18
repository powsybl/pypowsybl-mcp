#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Tests for `return_as="artifact"` on the read tools that return big tables."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.network_tools import NetworkTools
from pypowsybl_mcp.tools.security_tools import SecurityTools
from pypowsybl_mcp.utils.download_utils import download_links


class MockContext:
    def __init__(self, session_id="test-session"):
        self.session = MagicMock()
        self.session.session_id = session_id


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.fixture
def network_tools():
    return NetworkTools(TTLCache(maxsize=10, ttl=3600))


@pytest.fixture
def security_tools():
    return SecurityTools(TTLCache(maxsize=10, ttl=3600))


def artifact_bytes(data: dict) -> bytes:
    """Read back the file the artifact of an answer points at."""
    token = data["artifact"]["url"].rsplit("/", 2)[-2]
    with open(download_links[token]["temp_path"], "rb") as handle:
        return handle.read()


def artifact_rows(data: dict) -> list[dict]:
    """Fetch the artifact of an answer and return the rows it holds."""
    return json.loads(artifact_bytes(data))["rows"]


def network_with_buses(v_mag, nominal_v=400.0):
    """A network whose buses carry the given voltages, in kV."""
    count = len(v_mag)
    network = MagicMock()
    network.get_buses.return_value = pd.DataFrame(
        {
            "v_mag": v_mag,
            "name": [f"b{i}" for i in range(1, count + 1)],
            "voltage_level_id": ["VL1"] * count,
        },
        index=[f"b{i}" for i in range(1, count + 1)],
    )
    network.get_voltage_levels.return_value = pd.DataFrame(
        {
            "nominal_v": [nominal_v],
            "low_voltage_limit": [float("nan")],
            "high_voltage_limit": [float("nan")],
        },
        index=["VL1"],
    )
    return network


# --- check_voltage_violations ----------------------------------------------


@pytest.mark.asyncio
async def test_voltage_violations_go_to_a_file_with_the_counts_kept(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    # Three buses over 1.05 p.u., one inside the band.
    proxy.networks["net1"] = network_with_buses([400.0, 440.0, 445.0, 450.0])
    proxy.loadflow_results["net1"] = {"converged": True}

    result = await network_tools.check_voltage_violations(
        network_id="net1", return_as="artifact", ctx=mock_ctx
    )
    data = json.loads(result)

    assert data["success"] is True
    assert data["return_as"] == "artifact"
    # The summary a reader acts on is still inline.
    assert data["violation_count"] == 3
    assert data["evaluated_buses"] == 4
    # The rows are not.
    assert "violations" not in data
    assert data["artifact"]["row_count"] == 3
    assert "bus_id" in data["artifact"]["columns"]
    assert data["artifact"]["url"].endswith(".json")


@pytest.mark.asyncio
async def test_the_voltage_artifact_holds_every_violation_untruncated(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = network_with_buses([440.0] * 30)
    proxy.loadflow_results["net1"] = {"converged": True}

    # A limit that would cut the inline answer down to two rows.
    result = await network_tools.check_voltage_violations(
        network_id="net1", return_as="artifact", limit=2, ctx=mock_ctx
    )
    data = json.loads(result)

    assert data["artifact"]["row_count"] == 30
    assert len(artifact_rows(data)) == 30
    assert "pagination" not in data


@pytest.mark.asyncio
async def test_the_preview_shows_the_shape_of_a_violation(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = network_with_buses([440.0] * 10)
    proxy.loadflow_results["net1"] = {"converged": True}

    data = json.loads(
        await network_tools.check_voltage_violations(
            network_id="net1", return_as="artifact", ctx=mock_ctx
        )
    )

    assert len(data["preview"]) == 5
    assert data["preview"][0]["violation_type"] == "HIGH_VOLTAGE"


@pytest.mark.asyncio
async def test_voltage_violations_stay_inline_by_default(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = network_with_buses([440.0])
    proxy.loadflow_results["net1"] = {"converged": True}

    data = json.loads(
        await network_tools.check_voltage_violations(network_id="net1", ctx=mock_ctx)
    )

    assert "artifact" not in data
    assert len(data["violations"]) == 1


@pytest.mark.asyncio
async def test_an_unknown_return_as_is_refused_before_any_computation(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    network = network_with_buses([440.0])
    proxy.networks["net1"] = network

    data = json.loads(
        await network_tools.check_voltage_violations(
            network_id="net1", return_as="file", ctx=mock_ctx
        )
    )

    assert data["success"] is False
    assert "inline, artifact" in data["error"]
    network.get_buses.assert_not_called()


@pytest.mark.asyncio
async def test_an_unknown_artifact_format_is_refused(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    proxy.networks["net1"] = network_with_buses([440.0])

    data = json.loads(
        await network_tools.check_voltage_violations(
            network_id="net1",
            return_as="artifact",
            artifact_format="xlsx",
            ctx=mock_ctx,
        )
    )

    assert data["success"] is False
    assert "json, csv" in data["error"]


# --- get_network_element_data ----------------------------------------------


@pytest.mark.asyncio
async def test_element_data_goes_to_a_file_with_the_ids_kept(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    network = MagicMock()
    network.get_variant_ids.return_value = ["InitialState"]
    network.get_working_variant_id.return_value = "InitialState"
    network.get_buses.return_value = pd.DataFrame(
        {"v_mag": [400.0, 225.0]}, index=["B1", "B2"]
    )
    proxy.networks["net1"] = network

    data = json.loads(
        await network_tools.get_network_element_data(
            network_id="net1",
            element_type="bus",
            return_as="artifact",
            ctx=mock_ctx,
        )
    )

    assert data["return_as"] == "artifact"
    assert data["total_elements"] == 2
    assert "elements" not in data
    assert artifact_rows(data) == [
        {"id": "B1", "v_mag": 400.0},
        {"id": "B2", "v_mag": 225.0},
    ]


@pytest.mark.asyncio
async def test_a_filtered_artifact_holds_every_match_not_just_the_page(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    network = MagicMock()
    network.get_variant_ids.return_value = ["InitialState"]
    network.get_working_variant_id.return_value = "InitialState"
    network.get_generators.return_value = pd.DataFrame(
        {"p": [-10.0 * i for i in range(1, 21)], "max_p": [100.0] * 20},
        index=[f"G{i}" for i in range(1, 21)],
    )
    proxy.networks["net1"] = network

    data = json.loads(
        await network_tools.get_network_element_data(
            network_id="net1",
            element_type="generator",
            mode="filter",
            metric="p_abs",
            filter_op="gt",
            filter_value=0,
            return_as="artifact",
            limit=3,
            ctx=mock_ctx,
        )
    )

    assert data["matched_count"] == 20
    assert data["artifact"]["row_count"] == 20
    assert "pagination" not in data


@pytest.mark.asyncio
async def test_element_data_can_be_written_as_csv(network_tools, mock_ctx):
    proxy = network_tools.get_proxy("test-session")
    network = MagicMock()
    network.get_variant_ids.return_value = ["InitialState"]
    network.get_working_variant_id.return_value = "InitialState"
    network.get_buses.return_value = pd.DataFrame({"v_mag": [400.0]}, index=["B1"])
    proxy.networks["net1"] = network

    data = json.loads(
        await network_tools.get_network_element_data(
            network_id="net1",
            element_type="bus",
            return_as="artifact",
            artifact_format="csv",
            ctx=mock_ctx,
        )
    )

    content = artifact_bytes(data).decode("utf-8-sig")

    assert data["artifact"]["url"].endswith(".csv")
    assert content.splitlines() == ["id,v_mag", "B1,400.0"]


@pytest.mark.asyncio
async def test_reading_elements_restores_the_working_variant_even_as_an_artifact(
    network_tools, mock_ctx
):
    proxy = network_tools.get_proxy("test-session")
    network = MagicMock()
    network.get_variant_ids.return_value = ["InitialState", "v2"]
    network.get_working_variant_id.return_value = "v2"
    network.get_buses.return_value = pd.DataFrame({"v_mag": [400.0]}, index=["B1"])
    proxy.networks["net1"] = network

    await network_tools.get_network_element_data(
        network_id="net1",
        element_type="bus",
        variant_id="InitialState",
        return_as="artifact",
        ctx=mock_ctx,
    )

    assert network.set_working_variant.call_args_list[-1].args == ("v2",)


# --- run_security_analysis --------------------------------------------------


class FakeStatus:
    def __init__(self, name):
        self.name = name


class FakeViolation:
    def __init__(self, subject_id, limit_type="CURRENT", limit=1000.0, value=1300.0):
        self.subject_id = subject_id
        self.limit_type = FakeStatus(limit_type)
        self.limit = limit
        self.value = value


class FakeResult:
    def __init__(self, status_name, violations=()):
        self.status = FakeStatus(status_name)
        self.limit_violations = list(violations)


async def run_analysis(security_tools, mock_ctx, post_results, **kwargs):
    """Run the tool against a canned pypowsybl result.

    Awaited inside the patch block on purpose: returning the coroutine would let
    the patch be undone before the analysis actually runs.
    """
    proxy = security_tools.get_proxy("test-session")
    network = MagicMock()
    proxy.networks["net1"] = network

    with patch("pypowsybl_mcp.tools.security_tools.pp.security") as mock_security:
        analysis = MagicMock()
        mock_security.create_analysis.return_value = analysis
        results = MagicMock()
        analysis.run_ac.return_value = results
        results.pre_contingency_result = FakeResult("CONVERGED")
        results.post_contingency_results = post_results

        return await security_tools.run_security_analysis(
            network_id="net1",
            contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
            ctx=mock_ctx,
            **kwargs,
        )


@pytest.mark.asyncio
async def test_every_violation_of_every_contingency_lands_in_one_table(
    security_tools, mock_ctx
):
    post_results = {
        f"C{i}": FakeResult(
            "CONVERGED",
            [FakeViolation(f"LINE_{i}_a"), FakeViolation(f"LINE_{i}_b")],
        )
        for i in range(1, 6)
    }

    data = json.loads(
        await run_analysis(security_tools, mock_ctx, post_results, return_as="artifact")
    )

    assert data["return_as"] == "artifact"
    assert data["post_contingency"]["total_violations"] == 10
    assert data["artifact"]["row_count"] == 10
    assert "ranked_violations" not in data
    assert "contingencies_with_violations" not in data

    rows = artifact_rows(data)
    # Each row carries the contingency it belongs to - that is what makes the
    # flat table usable on its own.
    assert {row["contingency_id"] for row in rows} == {f"C{i}" for i in range(1, 6)}
    assert rows[0]["loading_percent"] == 130.0
    assert rows[0]["excess_percent"] == 30.0


@pytest.mark.asyncio
async def test_the_artifact_is_not_capped_by_top_violations(security_tools, mock_ctx):
    post_results = {
        f"C{i}": FakeResult("CONVERGED", [FakeViolation(f"LINE_{i}")])
        for i in range(1, 31)
    }

    data = json.loads(
        await run_analysis(
            security_tools,
            mock_ctx,
            post_results,
            return_as="artifact",
            top_violations=5,
        )
    )

    assert data["artifact"]["row_count"] == 30
    # The ranked contingency summary is small enough to stay inline.
    assert len(data["top_violating_contingencies"]) == 10


@pytest.mark.asyncio
async def test_the_limit_type_filter_still_applies_to_the_artifact(
    security_tools, mock_ctx
):
    post_results = {
        "C1": FakeResult(
            "CONVERGED",
            [
                FakeViolation("LINE_1", limit_type="CURRENT"),
                FakeViolation("BUS_1", limit_type="HIGH_VOLTAGE"),
            ],
        )
    }

    data = json.loads(
        await run_analysis(
            security_tools,
            mock_ctx,
            post_results,
            return_as="artifact",
            limit_type="HIGH_VOLTAGE",
        )
    )

    rows = artifact_rows(data)
    assert [row["subject_id"] for row in rows] == ["BUS_1"]
    # The breakdown still counts what was filtered out, as it does inline.
    assert data["post_contingency"]["violation_types_breakdown"] == {
        "CURRENT": 1,
        "HIGH_VOLTAGE": 1,
    }


@pytest.mark.asyncio
async def test_security_analysis_stays_inline_by_default(security_tools, mock_ctx):
    post_results = {"C1": FakeResult("CONVERGED", [FakeViolation("LINE_1")])}

    data = json.loads(await run_analysis(security_tools, mock_ctx, post_results))

    assert "artifact" not in data
    assert len(data["ranked_violations"]) == 1


@pytest.mark.asyncio
async def test_an_unknown_return_as_is_refused_by_the_security_analysis(
    security_tools, mock_ctx
):
    data = json.loads(
        await run_analysis(security_tools, mock_ctx, {}, return_as="file")
    )

    assert data["success"] is False
    assert "inline, artifact" in data["error"]


# --- get_overloaded_elements ------------------------------------------------


@pytest.mark.asyncio
async def test_overloaded_elements_in_normal_operation_go_to_a_file(
    security_tools, mock_ctx
):
    proxy = security_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()

    filter_response = json.dumps(
        {
            "success": True,
            "network_id": "net1",
            "variant_id": "InitialState",
            "limit_kind": "permanent",
            "elements": {f"l{i}": {"loading_percent": 100.0 + i} for i in range(1, 13)},
        }
    )

    with patch(
        "pypowsybl_mcp.tools.security_tools.NetworkTools.get_network_element_data",
        new=AsyncMock(return_value=filter_response),
    ):
        data = json.loads(
            await security_tools.get_overloaded_elements(
                network_id="net1",
                study="n",
                threshold_percent=100,
                return_as="artifact",
                ctx=mock_ctx,
            )
        )

    assert data["matched_count"] == 12
    assert "overloaded" not in data
    assert data["artifact"]["row_count"] == 12
    assert data["artifact"]["columns"] == [
        "element_id",
        "loading_percent",
        "limit_kind",
    ]
    assert len(artifact_rows(data)) == 12


@pytest.mark.asyncio
async def test_overloaded_elements_after_contingencies_go_to_a_file(
    security_tools, mock_ctx
):
    proxy = security_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()
    proxy.loadflow_results["net1"] = {"converged": True}

    sa_response = json.dumps(
        {
            "success": True,
            "network_id": "net1",
            "post_contingency": {
                "total_contingencies": 3,
                "contingencies_with_violations": 2,
            },
            "contingencies_with_violations": [
                {
                    "contingency_id": "C1",
                    "violations": [
                        {
                            "subject_id": "l1",
                            "loading_percent": 130.0,
                            "value": 1300.0,
                            "limit": 1000.0,
                            "limit_type": "CURRENT",
                        }
                    ],
                },
                {
                    "contingency_id": "C2",
                    "violations": [
                        {
                            "subject_id": "l2",
                            "loading_percent": 90.0,
                            "value": 900.0,
                            "limit": 1000.0,
                            "limit_type": "CURRENT",
                        }
                    ],
                },
            ],
        }
    )

    with patch(
        "pypowsybl_mcp.tools.security_tools.SecurityTools.run_security_analysis",
        new=AsyncMock(return_value=sa_response),
    ):
        data = json.loads(
            await security_tools.get_overloaded_elements(
                network_id="net1",
                study="n1",
                threshold_percent=100,
                contingencies=[{"element_id": "l1", "contingency_id": "C1"}],
                return_as="artifact",
                ctx=mock_ctx,
            )
        )

    # Only the element above the threshold is kept, artifact or not.
    assert data["matched_count"] == 1
    assert data["total_contingencies"] == 3
    assert artifact_rows(data) == [
        {
            "contingency_id": "C1",
            "element_id": "l1",
            "loading_percent": 130.0,
            "value": 1300.0,
            "limit": 1000.0,
            "limit_type": "CURRENT",
        }
    ]


@pytest.mark.asyncio
async def test_overloaded_elements_refuse_an_unknown_return_as(
    security_tools, mock_ctx
):
    proxy = security_tools.get_proxy("test-session")
    proxy.networks["net1"] = MagicMock()

    data = json.loads(
        await security_tools.get_overloaded_elements(
            network_id="net1", return_as="file", ctx=mock_ctx
        )
    )

    assert data["success"] is False
    assert "inline, artifact" in data["error"]
