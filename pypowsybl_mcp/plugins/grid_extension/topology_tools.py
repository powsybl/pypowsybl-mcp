#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Tool changing how existing elements are connected (see creation/topology.py).

One tool for every topology operation, named after pypowsybl's network
modifications: coupling two busbars, inserting a substation on a line, tapping
a line into another, replacing a tee point, and the reverts. The operations and
their attributes are listed in the tool's description, generated from the
operations themselves so it cannot drift from them.
"""

import json
import re

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from pypowsybl_mcp.plugins.grid_extension.creation.context import CreationError
from pypowsybl_mcp.plugins.grid_extension.creation.topology import (
    OPERATIONS,
    TopologyCallError,
    TopologyEngine,
)
from pypowsybl_mcp.tools import NetworkNotFoundError, PyPowsyblTool


def register_topology_tools(mcp: MCPServer, pypowsybl_proxies: TTLCache):
    # after the startup check, which may have set operations aside
    _describe_operations()
    tools = TopologyTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


def _operations_sheet(indent: str) -> str:
    """The operations and their attributes, for the tool's description."""
    lines = []
    for name, operation in sorted(OPERATIONS.items()):
        optional = sorted(
            set(operation.schema.names) - set(operation.required)
            if operation.call
            else ()
        )
        lines.append(f"{name}: {operation.summary}")
        lines.append(f"    required: {', '.join(operation.required)}")
        if optional:
            lines.append(f"    optional: {', '.join(optional)}")
    # indented as the {operations} placeholder, which supplies the first line's
    return "\n".join(f"{indent}{line}" for line in lines).lstrip()


class TopologyTools(PyPowsyblTool):
    """Change the topology around existing elements."""

    def __init__(self, pypowsybl_proxies: TTLCache, engine: TopologyEngine = None):
        super().__init__(pypowsybl_proxies)
        self.engine = engine or TopologyEngine()

    async def modify_network_topology(
        self,
        operation: str,
        attributes: dict,
        network_id: str | None = None,
        ctx: Context = None,  # MCPServer injects this
    ) -> str:
        """
        Change how existing elements are connected: couple busbars, insert a
        substation on a line, tap a line into another, or undo those.

        **Only change the network when the user explicitly asked for it.** These
        are long-term study tools (connection studies, network development),
        never a way to fix a constraint on an operational case.

        Args:
            operation (str): The operation, named after pypowsybl's network
                modification:
                {operations}
            attributes (dict): Its attributes. A busbar is a busbar section id in
                a node/breaker voltage level (element_type 'busbar_section' in
                get_network_element_data), or a configured bus of the bus/breaker
                view in a bus/breaker one (element_type
                'bus_from_bus_breaker_view') -- never a bus of the bus view
                ("VL1_0").
            network_id (str, optional): Network to change. If None, uses the
                current network. Default: None.

        Returns:
            str: JSON, with the same keys whatever the outcome:
                - success, network_id, operation, methods (the pypowsybl call)
                - created_ids, removed_ids: the elements that appeared or went,
                  switches aside -- also on a failure, when pypowsybl had already
                  changed part of the network
                - switches: {{created, removed, orphans_removed}}; a revert removes
                  the switches it would otherwise leave with nothing behind them
                  (in bus/breaker, the empty bus they led to stays: pypowsybl
                  cannot remove a bus)
                - notes: e.g. a new line without limits
                - errors: [{{problem, hint}}] on a rejection, hint being the
                  operation's descriptor
                - message: one line saying what happened
                - loadflow_invalidated

        Example Usage:
            # insert substation S4 on LINE_S2S3, 40 % of the way from side 1
            modify_network_topology("connect_voltage_level_on_line",
                {{"bbs_or_bus_id": "S4VL1_BBS", "line_id": "LINE_S2S3",
                 "position_percent": 40}})
            # and undo it
            modify_network_topology("revert_connect_voltage_level_on_line",
                {{"line1_id": "LINE_S2S3_1", "line2_id": "LINE_S2S3_2",
                 "line_id": "LINE_S2S3"}})

        Notes:
            - Checked before anything changes: the lines and busbars exist, a
              position strictly between 0 and 100 %, a busbar at the line's
              nominal voltage (a line cannot change voltage), and the ids
              pypowsybl would create (line sections, tee point, bus/breaker bus
              and switch) are free
            - A line cut in two keeps its limits on both halves; a new line
              tapped into another has none until operational_limits are added
            - Cached load-flow results are cleared: re-run run_loadflow()

        Related Tools:
            - create_network_element(): Create the substation or busbar first
            - remove_network_elements(): Remove elements
            - run_loadflow(): Compute the new network state
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            return _reply(
                _rejected(network_id, operation, [], str(e), [_error(str(e))])
            )

        try:
            methods = [self.engine.operation_for(operation).call.name]
        except (CreationError, AttributeError):
            methods = []
        try:
            result = self.engine.apply(network, operation, attributes or {})
        except CreationError as error:
            message = f"Failed to apply {operation}: {error!s}"
            logger.warning(message)
            return _reply(
                _rejected(
                    network_id,
                    operation,
                    methods,
                    message,
                    [_error(str(error), error.hint)],
                )
            )
        except TopologyCallError as error:
            # pypowsybl refused it, possibly after applying part of it
            proxy.invalidate_loadflow(network_id)
            changes = error.changes
            message = f"Failed to apply {operation}: {error!s}"
            reply = _rejected(
                network_id,
                operation,
                methods,
                message,
                [_error(str(error))],
                loadflow_invalidated=True,
            )
            if changes:
                reply["message"] += (
                    "; pypowsybl had already changed the network: "
                    + "; ".join(changes.parts())
                )
                reply["created_ids"] = list(changes.created_ids)
                reply["removed_ids"] = list(changes.removed_ids)
                reply["switches"]["created"] = changes.switches_created
                reply["switches"]["removed"] = changes.switches_removed
                reply["notes"] = [
                    (
                        "the network is not as it was before this call: the changes "
                        "listed in created_ids and removed_ids stay. Check them with "
                        "get_network_element_data() before going on"
                    )
                ]
            logger.error(reply["message"])
            return _reply(reply)
        except (pp.PyPowsyblError, ValueError, KeyError) as error:
            # pypowsybl may have applied part of the operation before refusing it.
            proxy.invalidate_loadflow(network_id)
            message = f"Failed to apply {operation}: {error!s}"
            logger.error(message)
            return _reply(
                _rejected(
                    network_id,
                    operation,
                    methods,
                    message,
                    [_error(str(error))],
                    loadflow_invalidated=True,
                )
            )

        proxy.invalidate_loadflow(network_id)
        logger.success(f"In network '{network_id}': {result.message}")
        return _reply(
            {
                "success": True,
                "network_id": network_id,
                "operation": result.operation,
                "methods": list(result.methods),
                "created_ids": list(result.created_ids),
                "removed_ids": list(result.removed_ids),
                "switches": {
                    "created": result.switches_created,
                    "removed": result.switches_removed,
                    "orphans_removed": list(result.orphan_switches_removed),
                },
                "notes": list(result.notes),
                "errors": [],
                "message": result.message,
                "loadflow_invalidated": True,
            }
        )


def _reply(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str)


def _error(problem: str, hint: dict | None = None) -> dict:
    return {"problem": problem, "hint": hint}


def _rejected(
    network_id,
    operation,
    methods,
    message,
    errors,
    loadflow_invalidated=False,
) -> dict:
    """The reply to an operation refused, before or by pypowsybl."""
    return {
        "success": False,
        "network_id": network_id,
        "operation": operation,
        "methods": methods,
        "created_ids": [],
        "removed_ids": [],
        "switches": {"created": 0, "removed": 0, "orphans_removed": []},
        "notes": [],
        "errors": errors,
        "message": message,
        "loadflow_invalidated": loadflow_invalidated,
    }


# The operations are inlined into the tool description, so a model reads them
# without a further call.
_TEMPLATE = TopologyTools.modify_network_topology.__doc__ or ""


def _describe_operations() -> None:
    """Fill the tool description with the operations offered."""
    # the placeholder's indentation, read since Python 3.13 dedents docstrings
    indent = re.search(r"^( *)\{operations\}", _TEMPLATE, re.MULTILINE)[1]
    TopologyTools.modify_network_topology.__doc__ = _TEMPLATE.format(
        operations=_operations_sheet(indent)
    )


_describe_operations()
