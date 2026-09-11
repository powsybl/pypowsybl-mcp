#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The three MCP tools that create network elements.

They are deliberately thin: everything they do lives in
:mod:`pypowsybl_mcp.creation`, so that adding an element type is a profile entry
rather than a new tool. What belongs here is the MCP surface -- session and
network resolution, load-flow invalidation, and turning an engine result or a
rejection into the text the caller reads.
"""

import json

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.creation.context import CreationError
from pypowsybl_mcp.creation.engine import CreationEngine
from pypowsybl_mcp.tools import NetworkNotFoundError, PyPowsyblTool

#: Minimal attributes of the most-used element types, inlined in the tool
#: description so the common cases cost no round trip to
#: describe_element_creation(). Everything else is one call away.
CHEAT_SHEET = """
    substation:      id, [country, tso, name]
    voltage_level:   id, substation_id, nominal_v, [topology_kind, low_voltage_limit,
                     high_voltage_limit, aligned_buses_or_busbar_count, section_count]
                     -> reports the ids of the connection points it creates
    load:            id, bus_or_busbar_section_id, p0, [q0, type]
    generator:       id, bus_or_busbar_section_id, target_p, max_p,
                     [min_p, voltage_regulator_on, target_v, target_q, energy_source]
    line:            id, bus_or_busbar_section_id_1, bus_or_busbar_section_id_2, r, x,
                     [g1, b1, g2, b2]
    two_windings_transformer:
                     id, bus_or_busbar_section_id_1, bus_or_busbar_section_id_2, r, x,
                     [rated_u1, rated_u2, rated_s] -- both ends in the same substation
    operational_limits:
                     element_id, permanent_limit, [side, limit_type, temporary_limits]
"""


def register_creation_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    tools = CreationTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


class CreationTools(PyPowsyblTool):
    """Create elements of any type from one generic tool."""

    def __init__(self, pypowsybl_proxies: TTLCache, engine: CreationEngine = None):
        super().__init__(pypowsybl_proxies)
        self.engine = engine or CreationEngine()

    def _failure(self, prefix: str, error: CreationError) -> str:
        """Report a rejection, with the descriptor that would have avoided it."""
        message = f"{prefix}: {error!s}"
        if error.hint:
            message += "\n" + json.dumps(error.hint, indent=2, default=str)
        logger.warning(message)
        return message

    async def describe_element_creation(
        self,
        element_type: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        List the element types that can be created, or describe one of them.

        This is the reference for create_network_element(): what a given element
        type needs, what it accepts, what the server fills in by itself, and what
        the values mean. The field list comes from the installed pypowsybl, so it
        is always the truth of the running version rather than documentation that
        may have drifted.

        Args:
            element_type (str, optional): Element type to describe, named like the
                pypowsybl ElementType lowercased ("load", "two_windings_transformer",
                "operational_limits"), the same vocabulary as
                get_network_element_data(). Default: None, which lists every
                creatable type with a one-line summary.

        Returns:
            str: JSON. For one element type:
                - summary, guidance: what it is for and how to choose values
                - required: attributes without which the element cannot be created
                - optional: everything else, with units, accepted values, defaults
                  and the description pypowsybl gives them
                - set_automatically: attributes the server derives; passing them
                  is an error
                - connection / attaches_to: how the element joins the network
                - table_attribute: the rows a table-shaped attribute takes
                  (the steps of a tap changer, the points of a capability curve)
                - rules: the constraints checked before anything is created
                - created_by: the pypowsybl call behind it

        Example:
            describe_element_creation()            → the list of types
            describe_element_creation("generator") → its attributes and rules

        Related Tools:
            - create_network_element(): Create one element
            - create_network_elements(): Create several in one call
            - remove_network_elements(): The inverse operation
        """
        try:
            return json.dumps(self.engine.describe(element_type), indent=2, default=str)
        except CreationError as error:
            return self._failure("Cannot describe that element type", error)

    async def create_network_element(
        self,
        element_type: str,
        attributes: dict,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create one element of any type in a network.

        One tool for every kind of element: the attributes a type takes come from
        the installed pypowsybl, and the server adds what pypowsybl does not say --
        which of them are required, their units and accepted values, and the
        switching equipment a new element needs to be connected.

        **Only create equipment when the user explicitly asked for it.** These are
        long-term study tools (connection studies, network development), never a
        way to fix a constraint, an overload or a non-convergent load flow on an
        operational case.

        Args:
            element_type (str): What to create, named like the pypowsybl ElementType
                lowercased. Call describe_element_creation() for the full list.
            attributes (dict): The attributes of the element, including its id.
                Minimal sets of the most-used types:
                {cheat_sheet}
                Injections and branches attach to a **bus or busbar section**:
                a busbar section id in a node/breaker voltage level, a bus of the
                bus/breaker view in a bus/breaker one — never a bus of the bus view
                (the "VL1_0"-style ids load-flow results report). The tool says
                which ids would have worked if you pass the wrong kind.
                Limits and tap changers attach to an element that already exists:
                pass its id as element_id.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: What was created, including the ids pypowsybl generated (the
                connection points of a new voltage level, for instance). On a
                rejection, the reason **and the descriptor of that element type**,
                so the call can be corrected without a further lookup.

        Example Usage:
            create_network_element("substation", {"id": "SUB_DC", "country": "FR"})
            create_network_element("voltage_level",
                {"id": "VL_DC", "substation_id": "SUB_DC", "nominal_v": 135.0,
                 "low_voltage_limit": 128.0, "high_voltage_limit": 145.0})
            create_network_element("load",
                {"id": "DATACENTER", "bus_or_busbar_section_id": "VL_DC_1_1",
                 "p0": 300.0, "q0": 60.0})
            create_network_element("operational_limits",
                {"element_id": "LINE_B4_DC", "permanent_limit": 800.0})

        Notes:
            - Cached load-flow results are cleared: re-run run_loadflow()
            - A branch created without operational_limits can never be reported as
              overloaded, and a transformer has no tap changer until one is created
              on it
            - Changes are in memory only: use export_network() to save them

        Related Tools:
            - describe_element_creation(): What a type needs, before calling
            - create_network_elements(): A whole site in one call
            - remove_network_elements(): Undo or retire what was created
            - run_loadflow(): Compute the state of the extended network
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating a {element_type} in network {network_id}")

        try:
            proxy.invalidate_loadflow(network_id)
            report = self.engine.create(network, element_type, attributes or {})
            info = f"In network '{network_id}': {report}"
            logger.success(info)
            return info
        except CreationError as error:
            return self._failure(f"Failed to create a {element_type}", error)
        except (pp.PyPowsyblError, ValueError, KeyError) as error:
            logger.error(f"Failed to create a {element_type}: {error}")
            return f"Failed to create a {element_type}: {error!s}"

    async def create_network_elements(
        self,
        items: list[dict],
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create several elements in one call, in the order their dependencies need.

        A new site is a substation, its voltage levels, the equipment inside and
        the line that connects it — six or seven elements that only make sense
        together. This creates them in one go: items may be listed in any order,
        as the server sorts containers before what they contain and elements
        before the limits and tap changers attached to them.

        **Only create equipment when the user explicitly asked for it.** These are
        long-term study tools, never a way to relieve a constraint on an
        operational case.

        Args:
            items (list[dict]): One object per element: the attributes accepted by
                create_network_element(), plus "element_type" naming the kind.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: One line per element created, in the order they were applied, and
                the reason if the batch stopped.

        Example Usage:
            create_network_elements([
              {"element_type": "substation", "id": "SUB_DC", "country": "FR"},
              {"element_type": "voltage_level", "id": "VL_DC",
               "substation_id": "SUB_DC", "nominal_v": 135.0,
               "low_voltage_limit": 128.0, "high_voltage_limit": 145.0},
              {"element_type": "load", "id": "DATACENTER",
               "bus_or_busbar_section_id": "VL_DC_1_1", "p0": 300.0, "q0": 60.0},
              {"element_type": "line", "id": "LINE_B4_DC",
               "bus_or_busbar_section_id_1": "B4",
               "bus_or_busbar_section_id_2": "VL_DC_1_1", "r": 0.5, "x": 5.0},
              {"element_type": "operational_limits", "element_id": "LINE_B4_DC",
               "permanent_limit": 800.0},
            ])

        Notes:
            - Every item is checked against its schema **before** anything is
              created, so a typo in the last one leaves the network untouched
            - Checks that need the network — an id already taken, a connection
              point that does not exist — can only run as each element is created,
              so they stop the batch there; the report says how far it got and
              what is already in the network
            - The ids of connection points created by a voltage level are reported
              as they are generated, and can be referenced by later items
            - Cached load-flow results are cleared

        Related Tools:
            - describe_element_creation(): What each type needs
            - create_network_element(): One element at a time
            - remove_network_elements(): Undo a whole site, cascade included
            - run_loadflow(): Compute the state of the extended network
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        if not items:
            return "No element to create: 'items' is empty"

        logger.debug(f"Creating {len(items)} element(s) in network {network_id}")

        try:
            proxy.invalidate_loadflow(network_id)
            done, failed = self.engine.create_many(network, list(items))
        except CreationError as error:
            return self._failure(
                f"Nothing was created in network '{network_id}'", error
            )
        except (pp.PyPowsyblError, ValueError, KeyError) as error:
            logger.error(f"Failed to create elements: {error}")
            return f"Failed to create elements: {error!s}"

        lines = [f"In network '{network_id}':"]
        lines += [f"- {line}" for line in done]
        if failed:
            lines.append(
                f"- STOPPED, {len(items) - len(done)} item(s) not created: "
                + "; ".join(failed)
            )
        else:
            lines.append("Run run_loadflow() to compute the new network state")
        info = "\n".join(lines)
        logger.success(info)
        return info


# The cheat sheet is inlined into the tool description at import time: it is the
# part a model reads without having to call describe_element_creation() first.
CreationTools.create_network_element.__doc__ = (
    CreationTools.create_network_element.__doc__ or ""
).replace("{cheat_sheet}", CHEAT_SHEET)
