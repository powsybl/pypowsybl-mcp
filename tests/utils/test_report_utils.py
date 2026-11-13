#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import json
from unittest.mock import MagicMock

import pypowsybl as pp

from pypowsybl_mcp.utils.report_utils import report_messages


def _report(report: dict) -> MagicMock:
    report_node = MagicMock()
    report_node.to_json.return_value = json.dumps(report)
    return report_node


def test_empty_report_has_no_messages():
    assert report_messages(pp.report.ReportNode()) == []


def test_leaves_are_rendered_with_their_values_and_severity():
    report = {
        "dictionaries": {
            "default": {
                "check": "Check parameters",
                "ignored": "Parameter '${name}' will be ignored",
                "info": "Nothing to say",
            }
        },
        "reportRoot": {
            "messageKey": "",
            "children": [
                {
                    "messageKey": "check",
                    "children": [
                        {
                            "messageKey": "ignored",
                            "values": {
                                "name": {"value": "WriteSlackBus"},
                                "reportSeverity": {
                                    "value": "WARN",
                                    "type": "SEVERITY",
                                },
                            },
                        },
                        {"messageKey": "info"},
                    ],
                }
            ],
        },
    }

    assert report_messages(_report(report)) == [
        "WARN: Parameter 'WriteSlackBus' will be ignored",
        "Nothing to say",
    ]


def test_unknown_template_and_placeholder_are_kept_verbatim():
    report = {
        "dictionaries": {"default": {"known": "Value ${missing}"}},
        "reportRoot": {
            "messageKey": "",
            "children": [{"messageKey": "known"}, {"messageKey": "unknown.key"}],
        },
    }

    assert report_messages(_report(report)) == ["Value ${missing}", "unknown.key"]


def test_real_provider_check_report():
    report_node = pp.report.ReportNode()
    pp.loadflow.check_loadflow_parameters(
        pp.loadflow.Parameters(write_slack_bus=True),
        provider="DynaFlow",
        report_node=report_node,
    )

    assert (
        "WARN: Load flow parameter 'WriteSlackBus' is not supported by DynaFlow, "
        "the parameter will be ignored"
    ) in report_messages(report_node)
