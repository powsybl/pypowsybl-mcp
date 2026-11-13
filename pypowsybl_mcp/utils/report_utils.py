#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Turn a pypowsybl ReportNode into plain, readable messages."""

from __future__ import annotations

import json
import re

import pypowsybl as pp

_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


def report_messages(report_node: pp.report.ReportNode) -> list[str]:
    """Return the leaf messages of a report, templates filled in.

    The JSON form of a report stores message templates in a dictionary
    ("Load flow parameter '${parameterName}' is not supported ...") and the
    values for each node separately. Only leaves are returned: inner nodes are
    section titles ("Check load flow parameters compatibility with ...").
    A leaf carrying a severity is prefixed with it, e.g. "WARN: ...".
    """
    report = json.loads(report_node.to_json())
    templates = report.get("dictionaries", {}).get("default", {})
    messages: list[str] = []

    def visit(node: dict) -> None:
        children = node.get("children", [])
        if children:
            for child in children:
                visit(child)
            return
        key = node.get("messageKey", "")
        if not key:
            return
        values = {
            name: str(value.get("value", ""))
            for name, value in node.get("values", {}).items()
        }
        text = _PLACEHOLDER.sub(
            lambda match: values.get(match.group(1), match.group(0)),
            templates.get(key, key),
        )
        severity = values.get("reportSeverity")
        messages.append(f"{severity}: {text}" if severity else text)

    visit(report.get("reportRoot", {}))
    return messages
