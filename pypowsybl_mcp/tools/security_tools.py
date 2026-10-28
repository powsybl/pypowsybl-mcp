#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from pypowsybl_mcp.tools import NetworkNotFoundError, PyPowsyblTool
from pypowsybl_mcp.utils.artifact_utils import (
    artifact_response,
    normalize_return_options,
)
from pypowsybl_mcp.utils.element_data_filter import (
    converter_station_nominal_voltages,
    element_nominal_voltage,
)
from pypowsybl_mcp.utils.element_types import (
    ELEMENT_TYPE_TO_GETTER,
    element_type_hint,
)
from pypowsybl_mcp.utils.pagination import (
    paginate,
)


def register_security_tools(mcp: MCPServer, pypowsybl_proxies: TTLCache):
    tools = SecurityTools(pypowsybl_proxies)
    # Private helpers (underscore-prefixed) are skipped automatically.
    tools.register_tools_with_mcp(mcp)


class SecurityTools(PyPowsyblTool):
    @staticmethod
    def _parse_json_if_needed(value: Any, field_name: str):
        """Accept native Python values or JSON strings from MCP clients.

        LLM-driven clients may send tool arguments either as already parsed
        objects or as JSON strings. Keeping this conversion in one place makes
        the public tools tolerant without duplicating JSON parsing logic.
        """
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON for {field_name}: {exc}") from exc

    @staticmethod
    def _status_name(result) -> str:
        """Return a plain text status from a pypowsybl result object."""
        status = getattr(result, "status", None)
        if status is None:
            return "UNKNOWN"
        return status.name if hasattr(status, "name") else str(status)

    @staticmethod
    def _limit_type_name(violation) -> str:
        """Return a plain text limit type from a pypowsybl violation object."""
        limit_type = getattr(violation, "limit_type", None)
        if hasattr(limit_type, "name"):
            return limit_type.name
        return str(limit_type) if limit_type is not None else "UNKNOWN"

    @staticmethod
    def _limit_violation_data(violation) -> dict:
        """Format one limit violation in the JSON shape returned by this tool."""
        return {
            "subject_id": getattr(violation, "subject_id", None),
            "limit_type": SecurityTools._limit_type_name(violation),
            "limit": round(float(getattr(violation, "limit", 0.0)), 2),
            "value": round(float(getattr(violation, "value", 0.0)), 2),
        }

    @staticmethod
    def _loading_and_excess(
        value: float, limit: float
    ) -> tuple[float | None, float | None]:
        """Turn a current and its limit into a loading and an overload, in percent.

        A line carrying 130 % of its limit is 30 % over. When the limit is 0 or a
        dummy "no limit" value (pypowsybl stores it as a huge number), the ratio
        would be meaningless, so we return None instead of a wrong number.
        """
        try:
            limit_f = float(limit)
            value_f = float(value)
        except (TypeError, ValueError):
            return None, None
        if limit_f == 0.0 or abs(limit_f) > 1e12:
            return None, None
        loading = value_f / limit_f * 100.0
        return round(loading, 2), round(loading - 100.0, 2)

    @staticmethod
    def _converter_station_voltages(network, voltage_by_level: dict) -> dict:
        """Converter station id -> its voltage level's nominal voltage.

        Thin wrapper over the shared resolver; contingency building keeps the
        legacy 0-for-unresolvable semantics via _element_nominal_voltage below.
        """
        return converter_station_nominal_voltages(network, voltage_by_level)

    @staticmethod
    def _element_nominal_voltage(
        element_type: str,
        element_row,
        voltage_by_level: dict,
        voltage_by_converter_station: dict | None = None,
    ) -> float:
        """Classifying voltage for an element (higher side), 0 when unresolvable.

        Delegates to the shared resolver (which returns NaN when unresolvable)
        and coerces NaN -> 0 so the contingency band filter keeps treating an
        unresolvable element as 0 kV, as before.
        """
        value = element_nominal_voltage(
            element_type, element_row, voltage_by_level, voltage_by_converter_station
        )
        return 0.0 if pd.isna(value) else value

    @staticmethod
    def _build_contingencies_from_filter(
        network,
        element_type: str,
        min_nominal_voltage: float | None = None,
        max_nominal_voltage: float | None = None,
    ) -> dict:
        """Build N-1 single-element contingencies from topology filters.

        This is shared by create_contingencies_list and run_security_analysis
        so both tools apply the same element-type and voltage-filter rules.
        An element is kept when its nominal voltage lies within the requested
        band: min_nominal_voltage <= nominal_v <= max_nominal_voltage, each
        bound applied only when provided (both inclusive).
        """
        # Only these element types make sense as N-1 contingencies. The getter
        # names come from the canonical map so they cannot drift from the rest
        # of the code base.
        supported_types = {
            key: ELEMENT_TYPE_TO_GETTER[key]
            for key in (
                "line",
                "generator",
                "two_windings_transformer",
                "hvdc_line",
            )
        }

        if element_type not in supported_types:
            raise ValueError(
                f"Unsupported element type '{element_type}'. "
                f"{element_type_hint(element_type, supported_types)}"
            )

        method_name = supported_types[element_type]
        elements_df = getattr(network, method_name)()
        total_elements = len(elements_df)

        voltage_levels_df = network.get_voltage_levels()
        vl_nominal_v = {}
        if "nominal_v" in voltage_levels_df.columns:
            for vl_id, row in voltage_levels_df.iterrows():
                vl_nominal_v[vl_id] = row["nominal_v"]

        voltage_by_converter_station = (
            SecurityTools._converter_station_voltages(network, vl_nominal_v)
            if element_type == "hvdc_line"
            else {}
        )

        filtered_element_ids = []
        for element_id, row in elements_df.iterrows():
            nominal_v = SecurityTools._element_nominal_voltage(
                element_type=element_type,
                element_row=row,
                voltage_by_level=vl_nominal_v,
                voltage_by_converter_station=voltage_by_converter_station,
            )

            if min_nominal_voltage is not None and nominal_v < min_nominal_voltage:
                continue
            if max_nominal_voltage is not None and nominal_v > max_nominal_voltage:
                continue
            filtered_element_ids.append(element_id)

        contingencies = [
            {"element_id": eid, "contingency_id": f"{eid}_contingency"}
            for eid in filtered_element_ids
        ]
        return {
            "element_type": element_type,
            "filters_applied": {
                "min_nominal_voltage": min_nominal_voltage,
                "max_nominal_voltage": max_nominal_voltage,
            },
            "total_elements": total_elements,
            "filtered_count": len(contingencies),
            "contingencies": contingencies,
        }

    def _resolve_contingencies(
        self,
        network,
        contingencies: list[dict] | str | None = None,
        auto_contingencies: dict | str | None = None,
    ) -> tuple[list[dict], str]:
        """Choose the contingency source and return the list to run.

        The caller can either provide an explicit list or ask the tool to build
        one from filters. Mixing both would be ambiguous, so it is rejected.
        """
        contingencies = self._parse_json_if_needed(contingencies, "contingencies")
        auto_contingencies = self._parse_json_if_needed(
            auto_contingencies, "auto_contingencies"
        )

        if contingencies is not None and auto_contingencies is not None:
            raise ValueError(
                "Parameters 'contingencies' and 'auto_contingencies' are mutually exclusive"
            )
        if contingencies is None and auto_contingencies is None:
            raise ValueError(
                "Provide either 'contingencies' or 'auto_contingencies' before running the security analysis"
            )

        if contingencies is not None:
            if not isinstance(contingencies, list):
                raise ValueError("Parameter 'contingencies' must be a list")
            return contingencies, "manual"

        if not isinstance(auto_contingencies, dict):
            # ValueError kept for consistency with the other input-validation
            # errors raised in this method (tested contract).
            raise ValueError(  # noqa: TRY004
                "Parameter 'auto_contingencies' must be an object"
            )

        filter_result = self._build_contingencies_from_filter(
            network=network,
            element_type=auto_contingencies.get("element_type", "line"),
            min_nominal_voltage=auto_contingencies.get("min_nominal_voltage"),
            max_nominal_voltage=auto_contingencies.get("max_nominal_voltage"),
        )
        return filter_result["contingencies"], "auto"

    @staticmethod
    def _violation_passes(
        limit_type: str,
        loading_percent: float | None,
        limit_type_filter: set[str] | None,
        threshold_percent: float | None,
    ) -> bool:
        """Whether a violation survives the active filters.

        Shared by the pre- and post-contingency loops so the whole response obeys
        one lens. ``limit_type_filter`` keeps only the requested kinds (e.g.
        {"CURRENT"}). ``threshold_percent`` is a thermal-overload filter: it
        narrows CURRENT rows only — voltage violations have no "% over" in this
        sense and always pass it — and a CURRENT row whose loading is not
        computable (infinite limit) is dropped once a threshold is in force.
        """
        if limit_type_filter is not None and limit_type not in limit_type_filter:
            return False
        if threshold_percent is not None and limit_type == "CURRENT":
            return loading_percent is not None and loading_percent > threshold_percent
        return True

    @staticmethod
    def _compute_ranked_contingencies(
        post_contingency_results,
        limit_type_filter: set[str] | None = None,
        threshold_percent: float | None = None,
    ) -> tuple[list[dict], int]:
        """Sort the contingencies, worst first, and add a loading to each violation.

        The contingency with the most problems comes first. Every violation gets
        its loading and its overload in percent, so nobody has to compute them
        again. Violations that do not pass the active filters (limit_type_filter,
        threshold_percent — see _violation_passes) are dropped here, so every
        downstream view (ranked_violations, the detail list, the top-contingency
        counts and total_violations) stays consistent with the filter.
        """
        ranked_contingencies = []
        total_violations = 0

        for contingency_id, contingency_result in post_contingency_results.items():
            contingency_violations = []
            limit_violations = getattr(contingency_result, "limit_violations", [])
            status_name = SecurityTools._status_name(contingency_result)

            for violation in limit_violations:
                limit_type = SecurityTools._limit_type_name(violation)
                violation_data = SecurityTools._limit_violation_data(violation)
                loading_percent, excess_percent = SecurityTools._loading_and_excess(
                    violation_data["value"], violation_data["limit"]
                )
                violation_data["loading_percent"] = loading_percent
                violation_data["excess_percent"] = excess_percent
                if not SecurityTools._violation_passes(
                    limit_type, loading_percent, limit_type_filter, threshold_percent
                ):
                    continue
                contingency_violations.append(violation_data)

            total_violations += len(contingency_violations)

            ranked_contingencies.append(
                {
                    "contingency_id": contingency_id,
                    "status": status_name,
                    "violations": contingency_violations,
                    "violation_count": len(contingency_violations),
                }
            )

        ranked_contingencies.sort(
            key=lambda item: item["violation_count"], reverse=True
        )
        return ranked_contingencies, total_violations

    @staticmethod
    def _flatten_ranked_violations(
        ranked_contingencies: list[dict],
        top_violations: int | None,
    ) -> list[dict]:
        """Gather every violation into one flat list, most loaded first.

        Each row remembers the contingency it comes from, so the list reads like
        a simple table. Rows without a real loading (infinite limit) go to the
        end. We keep at most top_violations rows, or all of them when it is None
        - which is what an artifact wants, since nothing is truncated there. The
        rows are already narrowed by the active filters (limit_type_filter,
        threshold_percent) in _compute_ranked_contingencies, so this is a pure
        flatten-and-sort.
        """
        flat = []
        for item in ranked_contingencies:
            for violation in item["violations"]:
                flat.append(
                    {
                        "contingency_id": item["contingency_id"],
                        "status": item["status"],
                        **violation,
                    }
                )

        flat.sort(
            key=lambda v: (
                v["loading_percent"]
                if v["loading_percent"] is not None
                else float("-inf")
            ),
            reverse=True,
        )
        return flat if top_violations is None else flat[:top_violations]

    async def run_security_analysis(
        self,
        network_id: str | None = None,
        contingencies: list[dict] | str | None = None,
        auto_contingencies: dict | str | None = None,
        mode: str = "summary",
        top_k: int = 10,
        detail_limit: int = 25,
        limit_type: str | None = None,
        top_violations: int = 20,
        threshold_percent: float | None = None,
        return_as: str = "inline",
        artifact_format: str = "json",
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context = None,  # MCPServer injects this
    ) -> str:
        """
        Execute N-1 security analysis to evaluate network robustness under contingencies.

        Security analysis assesses whether the power system can withstand the loss of
        any single element (N-1 criterion) without violating operational limits. This is
        critical for ensuring reliable grid operation.

        IMPORTANT: Before running a security analysis, always call get_loadflow_provider_info to
        verify that the current session provider is the one you intend to use. If it is
        not, use set_loadflow_provider to change it, then call get_loadflow_provider_info
        again to confirm the change.

        **Important**: Load flow must be successfully completed before running security analysis.
        Use run_loadflow() first.

        To list the lines overloaded after an N-1 event in a single call, pass the
        contingencies on the lines and ask for current violations only:

            run_security_analysis(
                network_id,
                auto_contingencies={"element_type": "line", "min_nominal_voltage": 63.0},
                limit_type="CURRENT",
                top_violations=20,
            )

        The answer then holds a ready table in "ranked_violations", already sorted
        with the most loaded line first. Each line shows its loading and its
        overload in percent (a line at 1300 A with a 1000 A limit is at 130 %, so
        30 % over). If that table is empty but there are still many violations, the
        problems are voltage ones, not overloads; look at "violation_types_breakdown"
        to see what they are.

        For pypowsybl security-analysis API details not exposed here (other
        contingency types, monitored elements, result fields), call
        get_online_resource(class_object='security') rather than relying on
        prior knowledge, which may be outdated.

        Args:
            network_id (str, optional): Network to analyze. If None, uses current network. Default: None.
            contingencies (list[dict] | str, optional): List of contingencies to analyze, or a JSON string
                containing the same information. Each contingency is a dictionary with:
                - element_id (str): Identifier of the network element to be tested (e.g., line, generator)
                - contingency_id (str): Identifier for the contingency
                Mutually exclusive with auto_contingencies. Default: None.
            auto_contingencies (dict | str, optional): Compact filter to auto-build contingencies.
                Format:
                - element_type (str): line, generator, two_windings_transformer, hvdc_line
                - min_nominal_voltage (float, optional): keep elements with nominal_v >= this (kV)
                - max_nominal_voltage (float, optional): keep elements with nominal_v <= this (kV)
                When both bounds are given, only elements with
                min <= nominal_v <= max are kept (bounds inclusive). See
                create_contingencies_list for the per-element-type voltage rules.
                Mutually exclusive with contingencies. Default: None.
            mode (str, optional): "summary" (default) or "detail". Summary is lightweight.
            top_k (int, optional): Number of top violating contingencies returned in summary. Default: 10.
            detail_limit (int, optional): Max number of contingencies returned in detail mode. Default: 25.
            limit_type (str, optional): Keep only this kind of violation. One value,
                or several separated by commas; the case does not matter. Usual ones:
                "CURRENT" for line and transformer overloads, "HIGH_VOLTAGE",
                "LOW_VOLTAGE". Default None keeps them all; use "CURRENT" for overloads.
            top_violations (int, optional): How many lines to keep in the
                "ranked_violations" table. Default: 20.
            threshold_percent (float, optional): Keep only CURRENT (thermal)
                violations loaded strictly above this percentage; voltage
                violations are never affected by it. Applied consistently across
                ranked_violations, the detail contingencies_with_violations,
                top_violating_contingencies, post_contingency.total_violations and
                the pre_contingency violations; only violation_types_breakdown /
                all_violations_count stay the full, unfiltered census of the study.
                NOTE: the security analysis reports an element only once it is
                ALREADY over its limit, so every CURRENT violation is already above
                100%. A threshold below 100 therefore keeps them all and CANNOT
                reveal near-limit (sub-100%) branches — those never appear as
                violations at all. Use >= 100 (e.g. 120) to isolate the serious
                overloads; to find near-limit branches in the base state, use
                check_current_violations. Default None keeps every violation.
            return_as (str, optional): "inline" (default) puts the violations in the
                answer, capped by top_violations and detail_limit. "artifact" writes
                every violation of every contingency to a temporary file instead -
                one row per violation, carrying its contingency_id - and returns a
                link to it next to the same counts. Use it for a full N-1 study: the
                table runs into the thousands of rows, and the file is fetched over
                plain HTTP by whoever needs the data instead of being read out loud.
            artifact_format (str, optional): "json" (default) or "csv", when
                return_as="artifact". Default: "json".
            limit (int, optional): Max entries in contingencies_with_violations per page.
            cursor (str | int, optional): Page offset for contingencies_with_violations.

        Returns:
            str: JSON formatted string containing:
                - success (bool): Whether analysis completed successfully
                - network_id (str): Network identifier
                - pre_contingency (dict): Pre-contingency state and violations
                - post_contingency (dict): Post-contingency summary, with a count of
                  each violation type (violation_types_breakdown, all_violations_count)
                - ranked_violations (list): The violations of every contingency in one
                  list, most loaded first, each with its loading and overload in percent
                - top_violating_contingencies (list): Small ranked subset in summary mode
                - contingencies_with_violations (list): Detailed violation information in detail mode
                - error (str): Error message (if failed)
                With return_as="artifact", ranked_violations and
                contingencies_with_violations are replaced by:
                - artifact (dict): url, format, row_count, columns, size_bytes, expires_at
                - preview (list): the first few violation rows, to check the columns

        Example Output:
            {
              "success": true,
              "network_id": "ieee_14",
              "pre_contingency": {
                "status": "CONVERGED",
                "violations": []
              },
              "post_contingency": {
                "total_contingencies": 23,
                "contingencies_with_violations": 3,
                "total_violations": 7
              },
              "contingencies_with_violations": [
                {
                  "contingency_id": "LINE-5-7",
                  "status": "CONVERGED",
                  "violations": [
                    {
                      "subject_id": "LINE-5-7",
                      "limit_type": "CURRENT",
                      "limit": 100.0,
                      "value": 105.3
                    }
                  ]
                }
              ],
              "sample_contingencies": [...]
            }
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            return json.dumps({"success": False, "error": str(e)}, indent=2)

        logger.debug(f"Running security analysis for network '{network_id}'")

        if mode not in {"summary", "detail"}:
            return json.dumps(
                {
                    "success": False,
                    "error": "Parameter 'mode' must be 'summary' or 'detail'",
                },
                indent=2,
            )

        if top_k is None or top_k < 1:
            return json.dumps(
                {"success": False, "error": "Parameter 'top_k' must be >= 1"}, indent=2
            )
        if detail_limit is None or detail_limit < 1:
            return json.dumps(
                {
                    "success": False,
                    "error": "Parameter 'detail_limit' must be >= 1",
                },
                indent=2,
            )
        if top_violations is None or top_violations < 1:
            return json.dumps(
                {
                    "success": False,
                    "error": "Parameter 'top_violations' must be >= 1",
                },
                indent=2,
            )
        if threshold_percent is not None:
            try:
                threshold_percent = float(threshold_percent)
            except (TypeError, ValueError):
                threshold_percent = -1.0
            if threshold_percent < 0:
                return json.dumps(
                    {
                        "success": False,
                        "error": "Parameter 'threshold_percent' must be a number >= 0",
                    },
                    indent=2,
                )

        try:
            return_mode, artifact_format = normalize_return_options(
                return_as, artifact_format
            )
        except ValueError as e:
            return json.dumps({"success": False, "error": str(e)}, indent=2)

        limit_type_filter = None
        if limit_type is not None:
            limit_type_filter = {
                part.strip().upper()
                for part in str(limit_type).split(",")
                if part.strip()
            } or None

        try:
            contingencies, contingency_source = self._resolve_contingencies(
                network=network,
                contingencies=contingencies,
                auto_contingencies=auto_contingencies,
            )

            # Run security analysis with default parameters
            analysis = pp.security.create_analysis()

            # Add contingencies if provided
            if contingencies:
                for contingency in contingencies:
                    element_id = contingency.get("element_id")
                    contingency_id = contingency.get("contingency_id")
                    if element_id and contingency_id:
                        analysis.add_single_element_contingency(
                            element_id, contingency_id
                        )

            results = analysis.run_ac(network)
            # Store results
            proxy.security_results = getattr(proxy, "security_results", {})
            proxy.security_results[network_id] = {
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Process pre-contingency results
            pre_contingency_result = results.pre_contingency_result
            pre_contingency_violations = []

            if hasattr(pre_contingency_result, "limit_violations"):
                violations = pre_contingency_result.limit_violations
                for violation in violations:
                    limit_type = self._limit_type_name(violation)
                    violation_data = self._limit_violation_data(violation)
                    loading_percent, excess_percent = self._loading_and_excess(
                        violation_data["value"], violation_data["limit"]
                    )
                    violation_data["loading_percent"] = loading_percent
                    violation_data["excess_percent"] = excess_percent
                    # Same filters as the post-contingency views, so the whole
                    # response obeys one lens (limit_type + threshold).
                    if not self._violation_passes(
                        limit_type,
                        loading_percent,
                        limit_type_filter,
                        threshold_percent,
                    ):
                        continue
                    pre_contingency_violations.append(violation_data)

            pre_contingency_data = {
                "status": pre_contingency_result.status.name,
                "violations": pre_contingency_violations,
            }

            # Process post-contingency results
            post_contingency_results = results.post_contingency_results

            # Count each type of violation before filtering, so we can tell
            # overloads apart from voltage problems.
            violation_types_breakdown: dict[str, int] = {}
            for contingency_result in post_contingency_results.values():
                for violation in getattr(contingency_result, "limit_violations", []):
                    lt = self._limit_type_name(violation)
                    violation_types_breakdown[lt] = (
                        violation_types_breakdown.get(lt, 0) + 1
                    )
            all_violations_count = sum(violation_types_breakdown.values())

            ranked_contingencies, total_violations = self._compute_ranked_contingencies(
                post_contingency_results, limit_type_filter, threshold_percent
            )
            contingencies_with_violations_list = [
                item for item in ranked_contingencies if item["violation_count"] > 0
            ]

            post_contingency_data = {
                "total_contingencies": len(post_contingency_results),
                "contingencies_with_violations": len(
                    contingencies_with_violations_list
                ),
                "total_violations": total_violations,
                "all_violations_count": all_violations_count,
                "violation_types_breakdown": violation_types_breakdown,
            }

            result = {
                "success": True,
                "network_id": network_id,
                "mode": mode,
                "contingency_source": contingency_source,
                "limit_type_filter": sorted(limit_type_filter)
                if limit_type_filter
                else None,
                "pre_contingency": pre_contingency_data,
                "post_contingency": post_contingency_data,
                "top_k": top_k,
                "threshold_percent": threshold_percent,
            }

            if return_mode == "artifact":
                # The whole table goes to the file: every violation of every
                # contingency, one row each, with no cap and no pagination. The
                # ranked list kept for the inline answer would be a subset.
                all_violations = self._flatten_ranked_violations(
                    contingencies_with_violations_list, None
                )
                result["top_violating_contingencies"] = [
                    {
                        "contingency_id": item["contingency_id"],
                        "status": item["status"],
                        "violation_count": item["violation_count"],
                    }
                    for item in contingencies_with_violations_list[:top_k]
                ]
                logger.info(
                    f"Security analysis completed for network '{network_id}' "
                    f"({len(all_violations)} violations, returned as an artifact)"
                )
                return json.dumps(
                    artifact_response(
                        result,
                        all_violations,
                        f"{network_id}_security_analysis",
                        artifact_format,
                    ),
                    indent=2,
                    default=str,
                )

            result["top_violations"] = top_violations
            result["ranked_violations"] = self._flatten_ranked_violations(
                contingencies_with_violations_list, top_violations
            )

            if mode == "summary":
                # Keep only the most relevant contingencies to avoid huge payloads.
                result["top_violating_contingencies"] = [
                    {
                        "contingency_id": item["contingency_id"],
                        "status": item["status"],
                        "violation_count": item["violation_count"],
                    }
                    for item in contingencies_with_violations_list[:top_k]
                ]
            else:
                limited_details = contingencies_with_violations_list[:detail_limit]
                result["detail_limit"] = detail_limit
                result["returned_details"] = len(limited_details)
                result["contingencies_with_violations"] = [
                    {
                        "contingency_id": item["contingency_id"],
                        "status": item["status"],
                        "violations": item["violations"],
                    }
                    for item in limited_details
                ]

            try:
                result = paginate(
                    result,
                    limit=limit,
                    cursor=cursor,
                    field="contingencies_with_violations",
                )
            except (ValueError, TypeError) as e:
                return json.dumps({"success": False, "error": str(e)}, indent=2)

            logger.info(f"Security analysis completed for network '{network_id}'")
            return json.dumps(result, indent=2)

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Failed to run security analysis: {e}")
            return json.dumps(
                {"success": False, "network_id": network_id, "error": str(e)}, indent=2
            )

    async def create_contingencies_list(
        self,
        network_id: str | None = None,
        element_type: str = "line",
        min_nominal_voltage: float | None = None,
        max_nominal_voltage: float | None = None,
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context = None,  # MCPServer injects this
    ) -> str:
        """
        Create a list of contingencies for security analysis based on network elements and filter criteria.

        This tool generates a list of contingencies compatible with the run_security_analysis tool.
        It allows filtering elements by type and voltage level to focus security analysis on
        specific parts of the network (e.g., all high-voltage lines above 225 kV).

        Args:
            network_id (str, optional): Network to query. If None, uses current network. Default: None.
            element_type (str, optional): Type of elements to create contingencies for. Supported types:
                - "line": Transmission lines (default)
                - "generator": Generators
                - "two_windings_transformer": Two-winding transformers ("transformer"
                  on its own always means this one)
                - "hvdc_line": HVDC lines
                Default: "line".
            min_nominal_voltage (float, optional): Minimum nominal voltage in kV to filter elements.
                Only elements connected to voltage levels >= this value are included.
                For lines/transformers, uses the higher voltage level of the two terminals.
                For HVDC lines, uses the higher of the two converter stations'
                voltage levels (the AC side, not the DC pole voltage).
                Default: None (no minimum filter).
            max_nominal_voltage (float, optional): Maximum nominal voltage in kV to filter elements.
                Only elements connected to voltage levels <= this value are included.
                For lines/transformers, uses the higher voltage level of the two terminals.
                For HVDC lines, uses the higher of the two converter stations'
                voltage levels (the AC side, not the DC pole voltage).
                Default: None (no maximum filter).
                When both bounds are given, only elements with
                min_nominal_voltage <= nominal_v <= max_nominal_voltage are kept
                (both bounds inclusive); omit either for a one-sided filter.
            limit (int, optional): Max contingencies per page in the contingencies field.
            cursor (str | int, optional): Page offset.

        Returns:
            str: JSON formatted string containing:
                - success (bool): Whether operation completed successfully
                - network_id (str): Network identifier
                - element_type (str): Type of elements used
                - filters_applied (dict): Filter criteria that were applied
                - total_elements (int): Total number of elements of this type in network
                - filtered_count (int): Number of elements after filtering
                - contingencies (list): List of contingency dictionaries, each with:
                    - element_id (str): Identifier of the network element
                    - contingency_id (str): Identifier for the contingency (format: "{element_id}_contingency")
                - error (str): Error message (if failed)

        Example Output:
            {
              "success": true,
              "network_id": "ieee_14",
              "element_type": "line",
              "filters_applied": {
                "min_nominal_voltage": 220.0,
                "max_nominal_voltage": null
              },
              "total_elements": 20,
              "filtered_count": 8,
              "contingencies": [
                {"element_id": "LINE_1_2", "contingency_id": "LINE_1_2_contingency"},
                {"element_id": "LINE_1_5", "contingency_id": "LINE_1_5_contingency"},
                ...
              ]
            }

        Example Usage:
            # Get all lines as contingencies
            contingencies = create_contingencies_list("ieee_14", "line")

            # Get only high-voltage lines (>= 220 kV)
            contingencies = create_contingencies_list("ieee_14", "line", min_nominal_voltage=220)

            # Get only lines within a voltage band (63 kV <= V <= 225 kV)
            contingencies = create_contingencies_list(
                "ieee_14", "line", min_nominal_voltage=63, max_nominal_voltage=225
            )

            # Get all generators as contingencies
            contingencies = create_contingencies_list("ieee_14", "generator")

            # Use with run_security_analysis
            result = create_contingencies_list("ieee_14", "line", min_nominal_voltage=220)
            contingencies = json.loads(result)["contingencies"]
            security_result = run_security_analysis("ieee_14", contingencies=contingencies)

        Notes:
            - The output is directly compatible with run_security_analysis contingencies parameter
            - For lines and transformers, voltage filtering uses the maximum nominal voltage
              of the two connected voltage levels
            - Generators are filtered by their connected voltage level
        """
        try:
            _, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            return json.dumps({"success": False, "error": str(e)}, indent=2)

        logger.debug(f"Creating contingencies list for network '{network_id}'")

        try:
            filter_result = self._build_contingencies_from_filter(
                network=network,
                element_type=element_type,
                min_nominal_voltage=min_nominal_voltage,
                max_nominal_voltage=max_nominal_voltage,
            )

            result = {
                "success": True,
                "network_id": network_id,
                "element_type": filter_result["element_type"],
                "filters_applied": filter_result["filters_applied"],
                "total_elements": filter_result["total_elements"],
                "filtered_count": filter_result["filtered_count"],
                "contingencies": filter_result["contingencies"],
            }

            try:
                result = paginate(
                    result, limit=limit, cursor=cursor, field="contingencies"
                )
            except (ValueError, TypeError) as e:
                return json.dumps({"success": False, "error": str(e)}, indent=2)

            logger.info(
                f"Created {filter_result['filtered_count']} contingencies for {element_type} in network '{network_id}'"
            )
            return json.dumps(result, indent=2)

        except ValueError as e:
            return json.dumps(
                {"success": False, "network_id": network_id, "error": str(e)}, indent=2
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Failed to create contingencies list: {e}")
            return json.dumps(
                {"success": False, "network_id": network_id, "error": str(e)}, indent=2
            )
