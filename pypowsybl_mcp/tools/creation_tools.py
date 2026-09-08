#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Tools that add new equipment to a network (grid extension).

Where ``modify_network`` changes elements that already exist, these tools
create them: a substation, a voltage level and its connection points, then the
loads, generators, lines and transformers connected to them
(``removal_tools`` holds the inverse operation). The typical driver is a
connection study -- attaching a new consumer (a datacenter, an electrolyzer),
a new power plant or a new line to an existing grid, then running a loadflow to
see whether the surrounding network copes.

Two design choices are worth knowing:

* **Injections and lines are created through "bays"**
  (``pypowsybl.network.create_load_bay`` and friends) rather than through the
  raw ``Network.create_loads`` dataframe API. A bay takes a *bus or busbar
  section* and builds the switching equipment needed by the topology of the
  hosting voltage level: in node/breaker it inserts a breaker and a closed
  disconnector onto the busbar section (plus open disconnectors on parallel
  busbars), in bus/breaker it simply attaches to the bus. This is what makes
  the same tool work on an IEEE test case and on a real node/breaker grid file,
  and it is why the caller never passes a raw ``node`` number.
* **Connection points are validated before the call**, because pypowsybl only
  answers "Bus or busbar section X not found". The resolver below tells the
  caller which ids *are* valid for the voltage level it guessed, which matters
  a lot when a model is choosing the ids.
"""

import pandas as pd
import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.tools import NetworkNotFoundError, PyPowsyblTool

# Position orders only have to be unique inside a voltage level, so a global
# maximum is a safe (if generous) starting point when the caller does not
# provide one.
POSITION_ORDER_STEP = 10
DEFAULT_POSITION_ORDER = 10

TOPOLOGY_KINDS = ("BUS_BREAKER", "NODE_BREAKER")
NODE_BREAKER = "NODE_BREAKER"
BUS_BREAKER = "BUS_BREAKER"

# Element types the "attachment" tools apply to: limits belong to branches, and
# reactive capability to the equipment that produces or absorbs reactive power.
LIMITABLE_TYPES = frozenset(
    {
        "LINE",
        "TWO_WINDINGS_TRANSFORMER",
        "THREE_WINDINGS_TRANSFORMER",
        "BOUNDARY_LINE",
        "DANGLING_LINE",
        "TIE_LINE",
    }
)
REACTIVE_LIMIT_TYPES = frozenset(
    {"GENERATOR", "BATTERY", "VSC_CONVERTER_STATION", "HVDC_CONVERTER_STATION"}
)

LIMIT_TYPES = ("CURRENT", "ACTIVE_POWER", "APPARENT_POWER")
LIMIT_SIDES = ("ONE", "TWO", "BOTH")
# powsybl 1.15 exposes no "OFF" mode for a static var compensator: a unit that
# should not regulate is created with regulating=False.
SVC_REGULATION_MODES = ("VOLTAGE", "REACTIVE_POWER")
# ... and no FIXED_TAP mode for a phase tap changer either.
PHASE_REGULATION_MODES = ("CURRENT_LIMITER", "ACTIVE_POWER_CONTROL")
TAP_SIDES = ("ONE", "TWO")


class ElementCreationError(ValueError):
    """Raised when a creation request is inconsistent with the network.

    Carries a message meant to be handed back to the caller as-is: it names
    what was wrong and, whenever possible, the ids that would have worked.
    """


class ConnectionPoint:
    """A validated bus or busbar section an element can be attached to."""

    def __init__(
        self,
        point_id: str,
        voltage_level_id: str,
        topology_kind: str,
        nominal_v: float,
    ):
        self.id = point_id
        self.voltage_level_id = voltage_level_id
        self.topology_kind = topology_kind
        self.nominal_v = nominal_v

    @property
    def is_node_breaker(self) -> bool:
        return self.topology_kind == NODE_BREAKER


def register_creation_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    tools = CreationTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


def _without_none(attributes: dict) -> dict:
    """Drop unset attributes so pypowsybl applies its own defaults."""
    return {key: value for key, value in attributes.items() if value is not None}


class CreationTools(PyPowsyblTool):
    """Tools creating new network elements (substations, voltage levels, ...)."""

    def _ensure_id_available(self, network, element_id: str) -> None:
        """Refuse an id already taken by any identifiable of the network.

        pypowsybl would raise as well, but only after the modification has
        started, and its message ("already contains an object 'LoadImpl'")
        leaks implementation class names.
        """
        if not element_id or not element_id.strip():
            raise ElementCreationError("An element id is required and cannot be empty")

        identifiables = network.get_identifiables()
        if element_id in identifiables.index:
            existing_type = str(identifiables.loc[element_id, "type"])
            raise ElementCreationError(
                f"Id '{element_id}' is already used by a {existing_type.lower()} "
                "in this network; choose another id"
            )

    def _voltage_level_row(self, network, voltage_level_id: str):
        """Return the voltage level row, including its topology kind."""
        voltage_levels = network.get_voltage_levels(all_attributes=True)
        if voltage_level_id not in voltage_levels.index:
            raise ElementCreationError(
                f"Voltage level '{voltage_level_id}' not found in this network. "
                "Use get_network_element_data(element_type='voltage_level') to "
                "list the existing ones"
            )
        return voltage_levels.loc[voltage_level_id]

    def _connection_points_of(self, network, voltage_level_id: str) -> list[str]:
        """Ids that can host an element in a given voltage level."""
        row = self._voltage_level_row(network, voltage_level_id)
        if str(row["topology_kind"]) == NODE_BREAKER:
            sections = network.get_busbar_sections()
            return sections[
                sections["voltage_level_id"] == voltage_level_id
            ].index.tolist()
        buses = network.get_bus_breaker_view_buses()
        return buses[buses["voltage_level_id"] == voltage_level_id].index.tolist()

    def _resolve_connection_point(self, network, point_id: str) -> ConnectionPoint:
        """Validate a bus or busbar section id and describe where it lives.

        Accepts a busbar section (node/breaker voltage levels) or a *configured*
        bus of the bus/breaker view (bus/breaker voltage levels). The bus ids
        reported by loadflow results and by ``get_network_element_data('bus')``
        belong to the bus *view*: they are computed from the topology and cannot
        host an element, so they get a message pointing at the real ids.
        """
        if not point_id or not point_id.strip():
            raise ElementCreationError(
                "A bus or busbar section id is required to connect the element"
            )

        busbar_sections = network.get_busbar_sections()
        if point_id in busbar_sections.index:
            voltage_level_id = str(busbar_sections.loc[point_id, "voltage_level_id"])
            row = self._voltage_level_row(network, voltage_level_id)
            return ConnectionPoint(
                point_id, voltage_level_id, NODE_BREAKER, float(row["nominal_v"])
            )

        configured_buses = network.get_bus_breaker_view_buses()
        if point_id in configured_buses.index:
            voltage_level_id = str(configured_buses.loc[point_id, "voltage_level_id"])
            row = self._voltage_level_row(network, voltage_level_id)
            topology_kind = str(row["topology_kind"])
            if topology_kind == NODE_BREAKER:
                # A node/breaker voltage level also has bus/breaker view buses,
                # but a bay has to be attached to a busbar section there.
                sections = self._connection_points_of(network, voltage_level_id)
                raise ElementCreationError(
                    f"'{point_id}' is a bus of the bus/breaker view of voltage "
                    f"level '{voltage_level_id}', which is in {NODE_BREAKER} "
                    "topology: an element must be connected to a busbar section "
                    f"there. Busbar sections available: {', '.join(sections) or 'none'}"
                )
            return ConnectionPoint(
                point_id, voltage_level_id, topology_kind, float(row["nominal_v"])
            )

        bus_view_buses = network.get_buses()
        if point_id in bus_view_buses.index:
            voltage_level_id = str(bus_view_buses.loc[point_id, "voltage_level_id"])
            candidates = self._connection_points_of(network, voltage_level_id)
            raise ElementCreationError(
                f"'{point_id}' is a bus of the bus view (a bus computed from the "
                f"topology of voltage level '{voltage_level_id}'), so no element "
                "can be attached to it. Connect to one of these instead: "
                f"{', '.join(candidates) or 'none'}"
            )

        raise ElementCreationError(
            f"Bus or busbar section '{point_id}' not found in this network. "
            "Use get_network_element_data(element_type='busbar_section') for "
            "node/breaker voltage levels, or "
            "get_network_element_data(element_type='bus_from_bus_breaker_view') "
            "for bus/breaker ones"
        )

    def _position_order(
        self, network, connection_point: ConnectionPoint, requested: int | None
    ) -> int | None:
        """Pick the ConnectablePosition order of a new bay.

        Only node/breaker topologies use it, and pypowsybl rejects a bay without
        one ("Position order is null for attachment in node-breaker voltage
        level"), so an order is derived from the existing positions rather than
        forcing the caller to invent one.
        """
        if not connection_point.is_node_breaker:
            return None
        if requested is not None:
            return int(requested)

        positions = network.get_extensions("position")
        if positions.empty or "order" not in positions.columns:
            return DEFAULT_POSITION_ORDER
        orders = positions["order"].dropna()
        if orders.empty:
            return DEFAULT_POSITION_ORDER
        return int(orders.max()) + POSITION_ORDER_STEP

    def _require_existing(
        self, network, element_id: str, allowed_types: frozenset, what: str
    ) -> str:
        """Check an element exists and can receive what is being attached."""
        if not element_id or not element_id.strip():
            raise ElementCreationError("An element id is required and cannot be empty")

        identifiables = network.get_identifiables()
        if element_id not in identifiables.index:
            raise ElementCreationError(
                f"Element '{element_id}' not found in this network. Use "
                "get_network_element_data() to list the existing ids"
            )
        element_type = str(identifiables.loc[element_id, "type"])
        if element_type not in allowed_types:
            accepted = ", ".join(sorted(t.lower() for t in allowed_types))
            raise ElementCreationError(
                f"{what} cannot be attached to a {element_type.lower()} "
                f"('{element_id}'). Accepted element types: {accepted}"
            )
        return element_type

    def _tap_positions(self, step_count: int, tap: int | None) -> tuple[int, int]:
        """Validate a tap changer range and pick the starting position.

        Positions run from 0 to step_count - 1; the neutral (middle) step is the
        sensible default, as a transformer is normally commissioned there.
        """
        if step_count < 2:
            raise ElementCreationError(
                f"step_count must be at least 2, got {step_count}: a tap changer "
                "with a single step cannot regulate anything"
            )
        low_tap = 0
        position = step_count // 2 if tap is None else int(tap)
        if not low_tap <= position <= step_count - 1:
            raise ElementCreationError(
                f"tap {position} is out of range [0, {step_count - 1}] for a tap "
                f"changer with {step_count} steps"
            )
        return low_tap, position

    def _linear_values(self, count: int, half_range: float) -> list[float]:
        """Evenly spaced values from -half_range to +half_range, count of them."""
        step = 2 * half_range / (count - 1)
        return [-half_range + index * step for index in range(count)]

    async def create_substation(
        self,
        substation_id: str,
        country: str | None = None,
        tso: str | None = None,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a new substation (a geographical site hosting voltage levels).

        A substation is a container: it holds no electrical equipment by itself.
        It is the first step when extending a grid with a new site, for instance
        the delivery point of a new datacenter or of a new power plant. The next
        step is always create_voltage_level(), which is what equipment connects
        to.

        Args:
            substation_id (str): Unique id of the new substation. Must not already
                be used by any element of the network.
            country (str, optional): ISO 3166-1 alpha-2 country code ("FR", "BE",
                "DE", ...). Default: None (unset).
            tso (str, optional): Transmission System Operator name. Default: None.
            name (str, optional): Human-readable name. Default: None (the id is used).
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message, or an explanation of why nothing was created.

        Example:
            create_substation("SUB_DATACENTER", country="FR", tso="RTE")
            → "Created substation 'SUB_DATACENTER' (country FR) in network 'ieee_14'"

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - A substation alone has no effect on any analysis
            - Cached loadflow results are cleared, as for any network change
            - Changes are in-memory only: use export_network() to save them

        Related Tools:
            - create_voltage_level(): Add a voltage level to the new substation
            - get_network_element_data(element_type='substation'): List substations
            - export_network(): Save the extended network
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating substation '{substation_id}' in network {network_id}")

        try:
            self._ensure_id_available(network, substation_id)
            proxy.invalidate_loadflow(network_id)

            attributes = _without_none({"country": country, "tso": tso, "name": name})
            network.create_substations(id=substation_id, **attributes)

            details = f" (country {country})" if country else ""
            info = (
                f"Created substation '{substation_id}'{details} in network "
                f"'{network_id}'. Add a voltage level to it with "
                "create_voltage_level()"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create substation: {e}")
            return f"Failed to create substation: {e!s}"

    async def create_voltage_level(
        self,
        voltage_level_id: str,
        substation_id: str,
        nominal_v: float,
        topology_kind: str = BUS_BREAKER,
        low_voltage_limit: float | None = None,
        high_voltage_limit: float | None = None,
        busbar_count: int = 1,
        section_count: int = 1,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a voltage level in a substation, with the connection points to plug
        equipment into.

        A voltage level is the busbar system of a substation at one nominal
        voltage. Equipment (loads, generators, line ends) is never attached to a
        voltage level directly but to one of its connection points, so this tool
        also creates them and **returns their ids** — those are the ids to pass to
        create_load(), create_generator() and create_line().

        Args:
            voltage_level_id (str): Unique id of the new voltage level.
            substation_id (str): Id of the hosting substation. Must exist (see
                create_substation()).
            nominal_v (float): Nominal voltage in kV (e.g. 400, 225, 135, 90). Match
                the voltage of the grid it will be connected to.
            topology_kind (str): Topology model of the voltage level:
                - "BUS_BREAKER" (default): buses connected by breakers. Simple, used
                  by IEEE test cases and most study files.
                - "NODE_BREAKER": full switching detail (busbar sections,
                  disconnectors, breakers), as in real grid files.
                Prefer the kind already used by the network it is connected to; check
                it with get_network_element_data(element_type='voltage_level').
            low_voltage_limit (float, optional): Lower operational voltage limit in
                kV, used by check_voltage_violations(). Default: None (unset).
            high_voltage_limit (float, optional): Upper operational voltage limit in
                kV. Default: None (unset).
            busbar_count (int): Number of parallel busbars (node/breaker) or rows of
                buses (bus/breaker) to create. Default: 1.
            section_count (int): Number of sections per busbar. Default: 1. With more
                than one section, consecutive sections are separated by a
                disconnector.
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message listing the ids of the created connection
                points (buses or busbar sections), or an explanation of the failure.

        Example:
            create_voltage_level("VL_DC", "SUB_DATACENTER", nominal_v=135.0,
                                 low_voltage_limit=128.0, high_voltage_limit=145.0)
            → "Created voltage level 'VL_DC' (135.0 kV, BUS_BREAKER) in substation
               'SUB_DATACENTER'. Connection points created: VL_DC_1_1"

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - Voltage limits are what check_voltage_violations() compares against; a
              voltage level without limits is never reported as violated
            - A brand-new voltage level is an electrical island until a line or a
              transformer connects it: a loadflow will not converge on it before that
            - Cached loadflow results are cleared

        Related Tools:
            - create_substation(): Create the hosting substation first
            - create_load() / create_generator(): Connect equipment to the returned
              connection points
            - create_line(): Connect this voltage level to the existing grid
            - get_network_element_data(element_type='voltage_level'): List voltage levels
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(
            f"Creating voltage level '{voltage_level_id}' in network {network_id}"
        )

        try:
            self._ensure_id_available(network, voltage_level_id)

            kind = str(topology_kind).strip().upper()
            if kind not in TOPOLOGY_KINDS:
                raise ElementCreationError(
                    f"Unsupported topology_kind '{topology_kind}'. "
                    f"Supported values: {', '.join(TOPOLOGY_KINDS)}"
                )

            substations = network.get_substations()
            if substation_id not in substations.index:
                raise ElementCreationError(
                    f"Substation '{substation_id}' not found in network "
                    f"'{network_id}'. Create it with create_substation() or list "
                    "existing ones with "
                    "get_network_element_data(element_type='substation')"
                )

            if busbar_count < 1 or section_count < 1:
                raise ElementCreationError(
                    "busbar_count and section_count must both be at least 1"
                )

            proxy.invalidate_loadflow(network_id)

            attributes = _without_none(
                {
                    "low_voltage_limit": low_voltage_limit,
                    "high_voltage_limit": high_voltage_limit,
                    "name": name,
                }
            )
            network.create_voltage_levels(
                id=voltage_level_id,
                substation_id=substation_id,
                nominal_v=float(nominal_v),
                topology_kind=kind,
                **attributes,
            )

            # The topology holds the connection points: busbar sections in
            # node/breaker, a matrix of buses in bus/breaker. Sections beyond the
            # first are separated by disconnectors (section_count - 1 switches).
            topology_attributes = {}
            if section_count > 1:
                topology_attributes["switch_kinds"] = ", ".join(
                    ["DISCONNECTOR"] * (section_count - 1)
                )
            try:
                pp.network.create_voltage_level_topology(
                    network,
                    id=voltage_level_id,
                    aligned_buses_or_busbar_count=int(busbar_count),
                    section_count=int(section_count),
                    **topology_attributes,
                )
            except (pp.PyPowsyblError, ValueError, KeyError) as e:
                # The voltage level itself exists at this point: say so, or the
                # caller retries the same id and only gets "already used".
                error = (
                    f"Voltage level '{voltage_level_id}' was created but its "
                    f"connection points could not be: {e!s}. It is unusable as "
                    "is; connect equipment to another voltage level, or export "
                    "and reload the network to start over"
                )
                logger.error(error)
                return error

            connection_points = self._connection_points_of(network, voltage_level_id)
            limits = (
                f", limits [{low_voltage_limit}, {high_voltage_limit}] kV"
                if low_voltage_limit is not None or high_voltage_limit is not None
                else ""
            )
            info = (
                f"Created voltage level '{voltage_level_id}' ({nominal_v} kV, "
                f"{kind}{limits}) in substation '{substation_id}' of network "
                f"'{network_id}'. Connection points created: "
                f"{', '.join(connection_points) or 'none'}"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create voltage level: {e}")
            return f"Failed to create voltage level: {e!s}"

    async def create_load(
        self,
        load_id: str,
        bus_or_busbar_section_id: str,
        p0: float,
        q0: float = 0.0,
        load_type: str | None = None,
        position_order: int | None = None,
        direction: str | None = None,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a load (a consumption point) and connect it to the grid.

        This is the tool for connection studies of a new consumer: a datacenter,
        an electrolyzer, a factory, an electrified rail line or simply an extra
        demand at an existing bus. The load is created *with its bay*: the
        switching equipment needed to attach it is built automatically, so the
        same call works on a simple bus/breaker test case and on a detailed
        node/breaker grid file.

        Args:
            load_id (str): Unique id of the new load.
            bus_or_busbar_section_id (str): Where to connect it. A busbar section id
                in a node/breaker voltage level, or a bus id of the bus/breaker view
                in a bus/breaker one. **Not** a bus of the bus view (the "VL1_0"-style
                ids returned by loadflow results): the tool tells you which ids are
                valid if you pass one of those.
            p0 (float): Active power consumption in MW. Positive for consumption
                (a 300 MW datacenter is p0=300.0).
            q0 (float): Reactive power consumption in MVAr. Default: 0.0. A typical
                power factor of 0.98 corresponds to q0 ≈ 0.2 * p0.
            load_type (str, optional): "UNDEFINED" (default), "AUXILIARY" or
                "FICTITIOUS".
            position_order (int, optional): Feeder position in the bay ordering of a
                node/breaker voltage level. Default: None (derived automatically).
                Ignored in bus/breaker topology.
            direction (str, optional): "TOP" or "BOTTOM", the side the feeder is drawn
                on in single line diagrams (node/breaker only). Default: None.
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message with the voltage level the load was attached
                to, or an explanation of the failure.

        Example:
            # 300 MW datacenter on the new delivery busbar
            create_load("DATACENTER", "VL_DC_1_1", p0=300.0, q0=60.0)
            → "Created load 'DATACENTER' (300.0 MW, 60.0 MVAr) connected to
               'VL_DC_1_1' in voltage level 'VL_DC' of network 'ieee_14'"

        Workflow for a new connection:
            1. create_substation() and create_voltage_level() for a new site, or
               reuse an existing busbar
            2. create_load() → the new consumption
            3. create_line() → connect the site to the existing grid
            4. run_loadflow() → check convergence
            5. get_overloaded_elements() / check_voltage_violations() → is the
               connection acceptable?
            6. run_security_analysis() → is it still acceptable after an N-1?

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - The load is created connected and at full power immediately
            - Cached loadflow results are cleared: re-run run_loadflow()
            - A large load with no nearby generation shifts the slack bus balance;
              a diverging loadflow is a result in itself for a connection study

        Related Tools:
            - create_line(): Connect a new site to the grid
            - modify_network(): Change p0/q0 of an existing load afterwards
            - run_loadflow(): Compute the state of the extended network
            - get_overloaded_elements(): See what the new load overloads
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating load '{load_id}' in network {network_id}")

        try:
            self._ensure_id_available(network, load_id)
            connection_point = self._resolve_connection_point(
                network, bus_or_busbar_section_id
            )
            order = self._position_order(network, connection_point, position_order)

            proxy.invalidate_loadflow(network_id)

            attributes = _without_none(
                {
                    "type": load_type,
                    "name": name,
                    "position_order": order,
                    "direction": direction
                    if connection_point.is_node_breaker
                    else None,
                }
            )
            pp.network.create_load_bay(
                network,
                id=load_id,
                bus_or_busbar_section_id=connection_point.id,
                p0=float(p0),
                q0=float(q0),
                **attributes,
            )

            info = (
                f"Created load '{load_id}' ({p0} MW, {q0} MVAr) connected to "
                f"'{connection_point.id}' in voltage level "
                f"'{connection_point.voltage_level_id}' of network '{network_id}'. "
                "Run run_loadflow() to compute the new network state"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create load: {e}")
            return f"Failed to create load: {e!s}"

    async def create_generator(
        self,
        generator_id: str,
        bus_or_busbar_section_id: str,
        target_p: float,
        max_p: float,
        min_p: float = 0.0,
        voltage_regulator_on: bool = False,
        target_v: float | None = None,
        target_q: float | None = None,
        rated_s: float | None = None,
        energy_source: str | None = None,
        position_order: int | None = None,
        direction: str | None = None,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a generator (a production unit) and connect it to the grid.

        Used to study the connection of new generation — a wind or solar farm, a
        battery-backed plant, an on-site unit next to a new consumer — or to add
        local support that makes a new load acceptable. Like create_load(), the
        generator is created with the bay needed by the hosting topology.

        Args:
            generator_id (str): Unique id of the new generator.
            bus_or_busbar_section_id (str): Where to connect it: a busbar section
                (node/breaker) or a bus of the bus/breaker view (bus/breaker). Not a
                bus-view bus.
            target_p (float): Active power setpoint in MW (the production).
            max_p (float): Maximum active power in MW. Must be >= target_p for the
                setpoint to be reachable.
            min_p (float): Minimum active power in MW. Default: 0.0.
            voltage_regulator_on (bool): Whether the unit regulates voltage.
                Default: False.
                - True: target_v is required, the unit holds that voltage
                - False: target_q is used (0.0 when unset)
            target_v (float, optional): Voltage setpoint in **kV** (not per-unit),
                required when voltage_regulator_on is True. Use a value close to the
                nominal voltage of the hosting voltage level (e.g. 137.0 for a 135 kV
                busbar). Default: None.
            target_q (float, optional): Reactive power setpoint in MVAr, used when
                voltage_regulator_on is False. Default: None (0.0 is applied).
            rated_s (float, optional): Rated apparent power in MVA. Default: None.
            energy_source (str, optional): "HYDRO", "NUCLEAR", "WIND", "THERMAL",
                "SOLAR" or "OTHER". Default: None ("OTHER" is applied).
            position_order (int, optional): Feeder position for node/breaker voltage
                levels. Default: None (derived automatically).
            direction (str, optional): "TOP" or "BOTTOM" (node/breaker only).
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message, or an explanation of the failure.

        Example:
            # 100 MW solar farm holding the voltage on the new busbar
            create_generator("SOLAR_DC", "VL_DC_1_1", target_p=80.0, max_p=100.0,
                             voltage_regulator_on=True, target_v=137.0,
                             energy_source="SOLAR")
            → "Created generator 'SOLAR_DC' (80.0/100.0 MW, voltage regulation at
               137.0 kV) connected to 'VL_DC_1_1' ..."

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - Whether the setpoint is honored depends on the loadflow balance type;
              check the actual production with
              get_network_element_data(element_type='generator') after run_loadflow()
            - A voltage-regulating unit is what usually turns a diverging connection
              study into a converging one
            - Cached loadflow results are cleared

        Related Tools:
            - create_load(): Connect a consumption point
            - modify_network(): Change target_p / target_v afterwards
            - run_loadflow(): Compute the state of the extended network
            - run_ac_sensitivity_analysis(): Measure the influence of the new unit
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating generator '{generator_id}' in network {network_id}")

        try:
            self._ensure_id_available(network, generator_id)
            connection_point = self._resolve_connection_point(
                network, bus_or_busbar_section_id
            )

            if voltage_regulator_on and target_v is None:
                raise ElementCreationError(
                    "target_v (in kV) is required when voltage_regulator_on is True; "
                    "a value close to the nominal voltage of voltage level "
                    f"'{connection_point.voltage_level_id}' "
                    f"({connection_point.nominal_v} kV) is expected"
                )
            if max_p < target_p:
                raise ElementCreationError(
                    f"max_p ({max_p} MW) is lower than target_p ({target_p} MW): "
                    "the setpoint could never be reached"
                )
            if min_p > target_p:
                raise ElementCreationError(
                    f"min_p ({min_p} MW) is higher than target_p ({target_p} MW): "
                    "the setpoint could never be reached"
                )

            order = self._position_order(network, connection_point, position_order)

            proxy.invalidate_loadflow(network_id)

            attributes = _without_none(
                {
                    "target_v": target_v,
                    "target_q": target_q if not voltage_regulator_on else None,
                    "rated_s": rated_s,
                    "energy_source": energy_source,
                    "name": name,
                    "position_order": order,
                    "direction": direction
                    if connection_point.is_node_breaker
                    else None,
                }
            )
            # pypowsybl requires a reactive setpoint when the unit does not
            # regulate voltage; default it rather than failing on an omission.
            if not voltage_regulator_on and "target_q" not in attributes:
                attributes["target_q"] = 0.0

            pp.network.create_generator_bay(
                network,
                id=generator_id,
                bus_or_busbar_section_id=connection_point.id,
                target_p=float(target_p),
                max_p=float(max_p),
                min_p=float(min_p),
                voltage_regulator_on=bool(voltage_regulator_on),
                **attributes,
            )

            regulation = (
                f"voltage regulation at {target_v} kV"
                if voltage_regulator_on
                else f"reactive setpoint {attributes.get('target_q', 0.0)} MVAr"
            )
            info = (
                f"Created generator '{generator_id}' ({target_p}/{max_p} MW, "
                f"{regulation}) connected to '{connection_point.id}' in voltage "
                f"level '{connection_point.voltage_level_id}' of network "
                f"'{network_id}'. Run run_loadflow() to compute the new network state"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create generator: {e}")
            return f"Failed to create generator: {e!s}"

    async def create_line(
        self,
        line_id: str,
        bus_or_busbar_section_id_1: str,
        bus_or_busbar_section_id_2: str,
        r: float,
        x: float,
        g1: float = 0.0,
        b1: float = 0.0,
        g2: float = 0.0,
        b2: float = 0.0,
        position_order_1: int | None = None,
        position_order_2: int | None = None,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create an AC line between two connection points, with the bays at both ends.

        This is what actually ties a new site to the existing grid — the missing
        step after create_voltage_level() — and also what models a grid
        reinforcement (a second circuit on a corridor, a new interconnection).
        Both ends are created with the switching equipment their voltage level
        requires.

        Args:
            line_id (str): Unique id of the new line.
            bus_or_busbar_section_id_1 (str): Connection point of side 1 (busbar
                section in node/breaker, bus of the bus/breaker view in bus/breaker).
            bus_or_busbar_section_id_2 (str): Connection point of side 2.
            r (float): Series resistance in Ω (total, not per km).
            x (float): Series reactance in Ω. Typically 5 to 15 times r on a
                transmission line.
            g1 (float): Shunt conductance at side 1 in S. Default: 0.0.
            b1 (float): Shunt susceptance at side 1 in S. Default: 0.0.
            g2 (float): Shunt conductance at side 2 in S. Default: 0.0.
            b2 (float): Shunt susceptance at side 2 in S. Default: 0.0.
            position_order_1 (int, optional): Feeder position of side 1 in a
                node/breaker voltage level. Default: None (derived automatically).
            position_order_2 (int, optional): Same for side 2. Default: None.
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message with both ends, or an explanation of the
                failure. A warning is appended when the two ends have different
                nominal voltages (a transformer, not a line, is needed there).

        Example:
            # Tie the new datacenter busbar to bus B4 of the 135 kV grid
            create_line("LINE_B4_DC", "B4", "VL_DC_1_1", r=0.5, x=5.0)
            → "Created line 'LINE_B4_DC' between 'B4' (VL4) and 'VL_DC_1_1' (VL_DC)
               in network 'ieee_14'"

        Choosing impedances:
            - Order of magnitude for an overhead line: r ≈ 0.03 Ω/km,
              x ≈ 0.3 Ω/km at 400 kV, so a 20 km double link is r ≈ 0.6, x ≈ 6
            - Copy the values of a comparable existing line with
              get_network_element_data(element_type='line') rather than guessing
            - x must not be 0: a zero-impedance line makes the loadflow singular

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - Both ends are created connected; use set_line_status() to open it
            - The line has no current limits: get_overloaded_elements() cannot
              report it as overloaded until limits are defined
            - Cached loadflow results are cleared

        Related Tools:
            - create_voltage_level(): Create the connection points first
            - set_line_status(): Open or close the new line (N-1 study)
            - run_loadflow(): Compute the flows on the new line
            - run_security_analysis(): Check the connection against contingencies
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating line '{line_id}' in network {network_id}")

        try:
            self._ensure_id_available(network, line_id)
            side1 = self._resolve_connection_point(network, bus_or_busbar_section_id_1)
            side2 = self._resolve_connection_point(network, bus_or_busbar_section_id_2)

            if side1.id == side2.id:
                raise ElementCreationError(
                    f"Both ends of line '{line_id}' point at '{side1.id}': a line "
                    "must connect two distinct connection points"
                )
            if x == 0:
                raise ElementCreationError(
                    "x (series reactance) must not be 0: a zero-impedance line "
                    "makes the loadflow singular"
                )

            order1 = self._position_order(network, side1, position_order_1)
            order2 = self._position_order(network, side2, position_order_2)

            proxy.invalidate_loadflow(network_id)

            attributes = _without_none(
                {
                    "name": name,
                    "position_order_1": order1,
                    "position_order_2": order2,
                }
            )
            pp.network.create_line_bays(
                network,
                id=line_id,
                bus_or_busbar_section_id_1=side1.id,
                bus_or_busbar_section_id_2=side2.id,
                r=float(r),
                x=float(x),
                g1=float(g1),
                b1=float(b1),
                g2=float(g2),
                b2=float(b2),
                **attributes,
            )

            info = (
                f"Created line '{line_id}' (r={r} Ω, x={x} Ω) between "
                f"'{side1.id}' ({side1.voltage_level_id}) and '{side2.id}' "
                f"({side2.voltage_level_id}) in network '{network_id}'"
            )
            if side1.nominal_v != side2.nominal_v:
                info += (
                    f". Warning: the two ends have different nominal voltages "
                    f"({side1.nominal_v} kV and {side2.nominal_v} kV); a "
                    "transformer, not a line, is normally used between them"
                )
            info += ". Run run_loadflow() to compute the new network state"
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create line: {e}")
            return f"Failed to create line: {e!s}"

    async def create_transformer(
        self,
        transformer_id: str,
        bus_or_busbar_section_id_1: str,
        bus_or_busbar_section_id_2: str,
        r: float,
        x: float,
        rated_u1: float | None = None,
        rated_u2: float | None = None,
        rated_s: float | None = None,
        g: float = 0.0,
        b: float = 0.0,
        position_order_1: int | None = None,
        position_order_2: int | None = None,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a two-windings transformer between two connection points, with the
        bays at both ends.

        This is what connects a new site to a grid at a *different* voltage: a
        400/225 kV transformer at a new substation, or an extra transformer to
        relieve an existing one. It creates the element type named
        "two_windings_transformer", the only kind of transformer that can be
        created with its bays. Both connection points **must belong to voltage
        levels of the same substation** -- that is what a transformer is; use
        create_line() to link two different substations.

        Args:
            transformer_id (str): Unique id of the new transformer.
            bus_or_busbar_section_id_1 (str): Connection point of side 1, the high
                voltage side by convention (busbar section in node/breaker, bus of
                the bus/breaker view in bus/breaker).
            bus_or_busbar_section_id_2 (str): Connection point of side 2, the low
                voltage side by convention.
            r (float): Series resistance in Ω, seen from side 1.
            x (float): Series reactance in Ω, seen from side 1. Roughly
                (short-circuit voltage in %) / 100 * rated_u1² / rated_s, so a
                400/225 kV 500 MVA transformer with usc = 15% gives x ≈ 48 Ω.
            rated_u1 (float, optional): Rated voltage of side 1 in kV. Default: None
                (the nominal voltage of the voltage level on side 1).
            rated_u2 (float, optional): Rated voltage of side 2 in kV. Default: None
                (the nominal voltage of the voltage level on side 2). The ratio
                rated_u1/rated_u2 is what sets the transformation ratio.
            rated_s (float, optional): Rated apparent power in MVA. Default: None.
            g (float): Magnetizing conductance in S. Default: 0.0.
            b (float): Magnetizing susceptance in S. Default: 0.0.
            position_order_1 (int, optional): Feeder position of side 1 in a
                node/breaker voltage level. Default: None (derived automatically).
            position_order_2 (int, optional): Same for side 2. Default: None.
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message with both ends and the rated voltages applied,
                or an explanation of the failure.

        Example:
            # 225/135 kV transformer at the new datacenter substation
            create_transformer("TR_DC", "VL_DC_225_1_1", "VL_DC_135_1_1",
                               r=0.5, x=25.0, rated_s=300.0)
            → "Created transformer 'TR_DC' (225.0/135.0 kV) between
               'VL_DC_225_1_1' (VL_DC_225) and 'VL_DC_135_1_1' (VL_DC_135) ..."

        Connecting a site at another voltage:
            1. create_substation() for the site
            2. create_voltage_level() twice: one per voltage (e.g. 225 kV for the
               grid connection, 135 kV for the internal network)
            3. create_transformer() between the two connection points returned
            4. create_load() / create_generator() on the lower voltage
            5. create_line() from the higher voltage to the existing grid
            6. run_loadflow(), then get_overloaded_elements()

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - The transformer has no tap changer: create it, then use
              set_tap_position() only if the source file already provides one
            - It has no current limits either, so get_overloaded_elements() cannot
              report it as overloaded; judge its loading from the flows it carries
            - Copy r/x/rated_s from a comparable existing transformer with
              get_network_element_data(element_type='two_windings_transformer')
              rather than inventing values
            - Cached loadflow results are cleared

        Related Tools:
            - create_voltage_level(): Create the two connection points first
            - create_line(): Connect two points at the same voltage instead
            - set_line_status(): Open or close the new transformer
            - set_tap_position(): Move a tap changer, when the element has one
            - run_loadflow(): Compute the flows through the new transformer
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating transformer '{transformer_id}' in network {network_id}")

        try:
            self._ensure_id_available(network, transformer_id)
            side1 = self._resolve_connection_point(network, bus_or_busbar_section_id_1)
            side2 = self._resolve_connection_point(network, bus_or_busbar_section_id_2)

            if side1.id == side2.id:
                raise ElementCreationError(
                    f"Both ends of transformer '{transformer_id}' point at "
                    f"'{side1.id}': a transformer must connect two distinct "
                    "connection points"
                )
            if x == 0:
                raise ElementCreationError(
                    "x (series reactance) must not be 0: a zero-impedance "
                    "transformer makes the loadflow singular"
                )

            # powsybl only accepts a two-windings transformer inside a single
            # substation; say so here rather than letting the bay creation fail
            # with "both voltage ids must be on the same substation".
            voltage_levels = network.get_voltage_levels()
            substation1 = voltage_levels.loc[side1.voltage_level_id, "substation_id"]
            substation2 = voltage_levels.loc[side2.voltage_level_id, "substation_id"]
            if substation1 != substation2:
                raise ElementCreationError(
                    f"A two-windings transformer must stay inside one substation, "
                    f"but '{side1.voltage_level_id}' belongs to '{substation1}' and "
                    f"'{side2.voltage_level_id}' to '{substation2}'. Create both "
                    "voltage levels in the same substation, or connect the two "
                    "substations with create_line()"
                )

            # The rated voltages carry the transformation ratio, so defaulting
            # them to the nominal voltages of both ends gives a 1:1 ratio
            # transformer rather than a meaningless one.
            side1_rated_u = side1.nominal_v if rated_u1 is None else float(rated_u1)
            side2_rated_u = side2.nominal_v if rated_u2 is None else float(rated_u2)

            order1 = self._position_order(network, side1, position_order_1)
            order2 = self._position_order(network, side2, position_order_2)

            proxy.invalidate_loadflow(network_id)

            attributes = _without_none(
                {
                    "name": name,
                    "rated_s": rated_s,
                    "position_order_1": order1,
                    "position_order_2": order2,
                }
            )
            pp.network.create_2_windings_transformer_bays(
                network,
                id=transformer_id,
                bus_or_busbar_section_id_1=side1.id,
                bus_or_busbar_section_id_2=side2.id,
                r=float(r),
                x=float(x),
                g=float(g),
                b=float(b),
                rated_u1=side1_rated_u,
                rated_u2=side2_rated_u,
                **attributes,
            )

            info = (
                f"Created transformer '{transformer_id}' "
                f"({side1_rated_u}/{side2_rated_u} kV, r={r} Ω, x={x} Ω) between "
                f"'{side1.id}' ({side1.voltage_level_id}) and '{side2.id}' "
                f"({side2.voltage_level_id}) in network '{network_id}'"
                ". Run run_loadflow() to compute the new network state"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create transformer: {e}")
            return f"Failed to create transformer: {e!s}"

    async def create_battery(
        self,
        battery_id: str,
        bus_or_busbar_section_id: str,
        target_p: float,
        max_p: float,
        min_p: float | None = None,
        target_q: float = 0.0,
        position_order: int | None = None,
        direction: str | None = None,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a battery (storage unit) and connect it to the grid.

        A battery is the storage equivalent of a generator: it produces when
        target_p is positive and absorbs when it is negative, between min_p and
        max_p. Typical use is a storage unit next to a new consumer or a new
        renewable plant, to study how it changes the flows at the connection
        point. Like the other injections, it is created with its bay.

        Args:
            battery_id (str): Unique id of the new battery.
            bus_or_busbar_section_id (str): Where to connect it: a busbar section
                (node/breaker) or a bus of the bus/breaker view (bus/breaker).
            target_p (float): Active power setpoint in MW. Positive = discharging
                (production), negative = charging (consumption).
            max_p (float): Maximum active power in MW (discharge capability).
            min_p (float, optional): Minimum active power in MW (charge capability,
                normally negative). Default: None, which applies -max_p, i.e. a
                symmetric charge/discharge range.
            target_q (float): Reactive power setpoint in MVAr. Default: 0.0.
            position_order (int, optional): Feeder position for node/breaker voltage
                levels. Default: None (derived automatically).
            direction (str, optional): "TOP" or "BOTTOM" (node/breaker only).
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message, or an explanation of the failure.

        Example:
            # 50 MW / symmetric storage discharging at 20 MW
            create_battery("BESS_DC", "VL_DC_1_1", target_p=20.0, max_p=50.0)
            → "Created battery 'BESS_DC' (20.0 MW in [-50.0, 50.0] MW) connected
               to 'VL_DC_1_1' ..."

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - A battery does not regulate voltage; use create_reactive_limits() to
              give it a reactive capability range
            - Energy content and state of charge are not modelled: a load flow sees
              a fixed injection, not a storage schedule
            - Cached loadflow results are cleared

        Related Tools:
            - create_generator(): A production unit that can regulate voltage
            - create_reactive_limits(): Give the battery a Q range
            - modify_network(): Change the setpoint afterwards
            - run_loadflow(): Compute the state of the extended network
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating battery '{battery_id}' in network {network_id}")

        try:
            self._ensure_id_available(network, battery_id)
            connection_point = self._resolve_connection_point(
                network, bus_or_busbar_section_id
            )

            lower = -float(max_p) if min_p is None else float(min_p)
            if not lower <= target_p <= max_p:
                raise ElementCreationError(
                    f"target_p ({target_p} MW) is outside [{lower}, {max_p}] MW: "
                    "the setpoint could never be reached"
                )

            order = self._position_order(network, connection_point, position_order)

            proxy.invalidate_loadflow(network_id)

            attributes = _without_none(
                {
                    "name": name,
                    "position_order": order,
                    "direction": direction
                    if connection_point.is_node_breaker
                    else None,
                }
            )
            pp.network.create_battery_bay(
                network,
                id=battery_id,
                bus_or_busbar_section_id=connection_point.id,
                target_p=float(target_p),
                target_q=float(target_q),
                max_p=float(max_p),
                min_p=lower,
                **attributes,
            )

            info = (
                f"Created battery '{battery_id}' ({target_p} MW in "
                f"[{lower}, {max_p}] MW) connected to '{connection_point.id}' in "
                f"voltage level '{connection_point.voltage_level_id}' of network "
                f"'{network_id}'. Run run_loadflow() to compute the new network state"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create battery: {e}")
            return f"Failed to create battery: {e!s}"

    async def create_shunt_compensator(
        self,
        shunt_id: str,
        bus_or_busbar_section_id: str,
        b_per_section: float,
        max_section_count: int = 1,
        section_count: int = 1,
        g_per_section: float = 0.0,
        target_v: float | None = None,
        target_deadband: float | None = None,
        position_order: int | None = None,
        direction: str | None = None,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a shunt compensator (capacitor bank or reactor) and connect it.

        This is the usual answer to a reactive problem at a connection point: a
        capacitor bank (positive susceptance) raises the voltage and supplies
        reactive power, a reactor (negative susceptance) absorbs it. Only linear
        models are created here: identical sections, of which `section_count` are
        in service.

        Args:
            shunt_id (str): Unique id of the new shunt compensator.
            bus_or_busbar_section_id (str): Where to connect it: a busbar section
                (node/breaker) or a bus of the bus/breaker view (bus/breaker).
            b_per_section (float): Susceptance of one section in **S** (siemens).
                Positive = capacitor bank, negative = reactor. For a bank rated
                Q MVAr at U kV, b = Q / U² (e.g. 60 MVAr at 63 kV → 0.0151 S).
            max_section_count (int): Number of sections the bank has. Default: 1.
            section_count (int): Number of sections in service now, between 0 and
                max_section_count. Default: 1.
            g_per_section (float): Conductance of one section in S (losses).
                Default: 0.0.
            target_v (float, optional): Voltage target in kV, when the shunt
                regulates by switching sections. Default: None (no regulation).
            target_deadband (float, optional): Deadband around target_v in kV.
                Default: None.
            position_order (int, optional): Feeder position for node/breaker voltage
                levels. Default: None (derived automatically).
            direction (str, optional): "TOP" or "BOTTOM" (node/breaker only).
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message with the reactive power the bank represents
                at nominal voltage, or an explanation of the failure.

        Example:
            # 60 MVAr capacitor bank on a 63 kV busbar
            create_shunt_compensator("CAP_DC", "VL_DC_63_1_1", b_per_section=0.0151)
            → "Created shunt compensator 'CAP_DC' (capacitor, 1/1 section(s),
               b=0.0151 S ≈ 59.9 MVAr at 63.0 kV) ..."

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - The reactive power actually injected follows the voltage: Q = b · U²,
              so a bank produces less when the voltage sags
            - Only the linear model is exposed; non-linear (per-section) models
              require pypowsybl directly
            - Cached loadflow results are cleared

        Related Tools:
            - create_static_var_compensator(): Continuous reactive control instead
              of discrete sections
            - check_voltage_violations(): See the effect on voltages
            - run_loadflow(): Compute the state of the extended network
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating shunt compensator '{shunt_id}' in network {network_id}")

        try:
            self._ensure_id_available(network, shunt_id)
            connection_point = self._resolve_connection_point(
                network, bus_or_busbar_section_id
            )

            if max_section_count < 1:
                raise ElementCreationError("max_section_count must be at least 1")
            if not 0 <= section_count <= max_section_count:
                raise ElementCreationError(
                    f"section_count ({section_count}) must be between 0 and "
                    f"max_section_count ({max_section_count})"
                )

            order = self._position_order(network, connection_point, position_order)

            proxy.invalidate_loadflow(network_id)

            # create_shunt_compensator_bay only takes dataframes: the shunt itself
            # and its section model, which are two separate dataframes upstream.
            shunt_attributes = _without_none(
                {
                    "id": shunt_id,
                    "model_type": "LINEAR",
                    "section_count": int(section_count),
                    "target_v": target_v,
                    "target_deadband": target_deadband,
                    "bus_or_busbar_section_id": connection_point.id,
                    "position_order": order,
                    "direction": direction
                    if connection_point.is_node_breaker
                    else None,
                    "name": name,
                }
            )
            shunt_df = pd.DataFrame.from_records(index="id", data=[shunt_attributes])
            linear_model_df = pd.DataFrame.from_records(
                index="id",
                data=[
                    {
                        "id": shunt_id,
                        "g_per_section": float(g_per_section),
                        "b_per_section": float(b_per_section),
                        "max_section_count": int(max_section_count),
                    }
                ],
            )
            pp.network.create_shunt_compensator_bay(
                network, shunt_df=shunt_df, linear_model_df=linear_model_df
            )

            kind = "capacitor" if b_per_section > 0 else "reactor"
            nominal_v = connection_point.nominal_v
            reactive = b_per_section * section_count * nominal_v**2
            info = (
                f"Created shunt compensator '{shunt_id}' ({kind}, "
                f"{section_count}/{max_section_count} section(s), "
                f"b={b_per_section} S ≈ {reactive:.1f} MVAr at {nominal_v} kV) "
                f"connected to '{connection_point.id}' in voltage level "
                f"'{connection_point.voltage_level_id}' of network '{network_id}'. "
                "Run run_loadflow() to compute the new network state"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create shunt compensator: {e}")
            return f"Failed to create shunt compensator: {e!s}"

    async def create_static_var_compensator(
        self,
        svc_id: str,
        bus_or_busbar_section_id: str,
        b_min: float,
        b_max: float,
        regulation_mode: str = "VOLTAGE",
        target_v: float | None = None,
        target_q: float | None = None,
        regulating: bool = True,
        position_order: int | None = None,
        direction: str | None = None,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a static var compensator (SVC) and connect it to the grid.

        Where a shunt compensator switches fixed sections, an SVC varies its
        susceptance continuously between b_min and b_max, so it can hold a voltage
        setpoint. Used at a connection point where the reactive balance changes a
        lot, or to keep a long radial connection within its voltage limits.

        Args:
            svc_id (str): Unique id of the new SVC.
            bus_or_busbar_section_id (str): Where to connect it: a busbar section
                (node/breaker) or a bus of the bus/breaker view (bus/breaker).
            b_min (float): Minimum susceptance in **S**, the inductive end
                (negative). For ±50 MVAr at 63 kV, b_min ≈ -0.0126.
            b_max (float): Maximum susceptance in S, the capacitive end (positive).
            regulation_mode (str): What the SVC controls:
                - "VOLTAGE" (default): holds target_v, which is then required
                - "REACTIVE_POWER": holds target_q, which is then required
                There is no "OFF" mode in this pypowsybl version: pass
                regulating=False to create the device without it acting.
            target_v (float, optional): Voltage setpoint in kV. Required with
                regulation_mode="VOLTAGE". Default: None.
            target_q (float, optional): Reactive setpoint in MVAr. Required with
                regulation_mode="REACTIVE_POWER". Default: None.
            regulating (bool): Whether the SVC actually regulates. Default: True.
            position_order (int, optional): Feeder position for node/breaker voltage
                levels. Default: None (derived automatically).
            direction (str, optional): "TOP" or "BOTTOM" (node/breaker only).
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message, or an explanation of the failure.

        Example:
            # ±50 MVAr SVC holding 63.5 kV on the new busbar
            create_static_var_compensator("SVC_DC", "VL_DC_63_1_1", b_min=-0.0126,
                                          b_max=0.0126, target_v=63.5)
            → "Created static var compensator 'SVC_DC' (b in [-0.0126, 0.0126] S,
               VOLTAGE regulation at 63.5 kV) ..."

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - As with a shunt, the reactive power available follows the square of
              the voltage
            - Cached loadflow results are cleared

        Related Tools:
            - create_shunt_compensator(): Discrete capacitor bank or reactor
            - check_voltage_violations(): See the effect on voltages
            - run_loadflow(): Compute the state of the extended network
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating static var compensator '{svc_id}' in {network_id}")

        try:
            self._ensure_id_available(network, svc_id)
            connection_point = self._resolve_connection_point(
                network, bus_or_busbar_section_id
            )

            mode = str(regulation_mode).strip().upper()
            if mode not in SVC_REGULATION_MODES:
                raise ElementCreationError(
                    f"Unsupported regulation_mode '{regulation_mode}'. Supported "
                    f"values: {', '.join(SVC_REGULATION_MODES)} (this pypowsybl "
                    "version has no 'OFF' mode; use regulating=False instead)"
                )
            if b_min >= b_max:
                raise ElementCreationError(
                    f"b_min ({b_min} S) must be lower than b_max ({b_max} S)"
                )
            if mode == "VOLTAGE" and target_v is None:
                raise ElementCreationError(
                    "target_v (in kV) is required with regulation_mode='VOLTAGE'; a "
                    "value close to the nominal voltage of voltage level "
                    f"'{connection_point.voltage_level_id}' "
                    f"({connection_point.nominal_v} kV) is expected"
                )
            if mode == "REACTIVE_POWER" and target_q is None:
                raise ElementCreationError(
                    "target_q (in MVAr) is required with "
                    "regulation_mode='REACTIVE_POWER'"
                )

            order = self._position_order(network, connection_point, position_order)

            proxy.invalidate_loadflow(network_id)

            attributes = _without_none(
                {
                    "target_v": target_v,
                    "target_q": target_q,
                    "name": name,
                    "position_order": order,
                    "direction": direction
                    if connection_point.is_node_breaker
                    else None,
                }
            )
            pp.network.create_static_var_compensator_bay(
                network,
                id=svc_id,
                bus_or_busbar_section_id=connection_point.id,
                b_min=float(b_min),
                b_max=float(b_max),
                regulation_mode=mode,
                regulating=bool(regulating),
                **attributes,
            )

            setpoint = f"{target_v} kV" if mode == "VOLTAGE" else f"{target_q} MVAr"
            state = (
                f"{mode} regulation at {setpoint}" if regulating else "not regulating"
            )
            info = (
                f"Created static var compensator '{svc_id}' (b in [{b_min}, "
                f"{b_max}] S, {state}) connected to '{connection_point.id}' in "
                f"voltage level '{connection_point.voltage_level_id}' of network "
                f"'{network_id}'. Run run_loadflow() to compute the new network state"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create static var compensator: {e}")
            return f"Failed to create static var compensator: {e!s}"

    async def create_ground(
        self,
        ground_id: str,
        bus_or_busbar_section_id: str,
        name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Create a ground (an earthing connection) on a bus.

        A ground ties a bus to earth. It carries no power in a load flow and is
        mostly there for completeness of the model (earthing of a neutral, of a
        busbar or of a transformer star point).

        Args:
            ground_id (str): Unique id of the new ground.
            bus_or_busbar_section_id (str): Bus of the bus/breaker view to earth.
                **Bus/breaker voltage levels only**: pypowsybl 1.15 has no bay
                creation for grounds, so a ground cannot be attached to a busbar
                section of a node/breaker voltage level through this tool.
            name (str, optional): Human-readable name. Default: None.
            network_id (str, optional): Network to extend. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message, or an explanation of the failure.

        Example:
            create_ground("GND_DC", "VL_DC_1_1")
            → "Created ground 'GND_DC' on bus 'VL_DC_1_1' in voltage level 'VL_DC'"

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - A ground has no effect on an AC load flow result
            - Cached loadflow results are cleared

        Related Tools:
            - create_voltage_level(): Create the bus to earth first
            - get_network_element_data(element_type='ground'): List existing grounds
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Creating ground '{ground_id}' in network {network_id}")

        try:
            self._ensure_id_available(network, ground_id)
            connection_point = self._resolve_connection_point(
                network, bus_or_busbar_section_id
            )
            if connection_point.is_node_breaker:
                raise ElementCreationError(
                    f"Voltage level '{connection_point.voltage_level_id}' is in "
                    f"{NODE_BREAKER} topology, and pypowsybl provides no bay "
                    "creation for grounds: a ground can only be created on a bus "
                    "of a BUS_BREAKER voltage level here. Use "
                    "generate_python_script() to build it with Network.create_grounds"
                    " and its own switching equipment"
                )

            proxy.invalidate_loadflow(network_id)

            attributes = _without_none({"name": name})
            network.create_grounds(
                id=ground_id,
                voltage_level_id=connection_point.voltage_level_id,
                bus_id=connection_point.id,
                **attributes,
            )

            info = (
                f"Created ground '{ground_id}' on bus '{connection_point.id}' in "
                f"voltage level '{connection_point.voltage_level_id}' of network "
                f"'{network_id}'"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to create ground: {e}")
            return f"Failed to create ground: {e!s}"

    async def create_operational_limits(
        self,
        element_id: str,
        permanent_limit: float,
        side: str = "BOTH",
        limit_type: str = "CURRENT",
        temporary_limit_values: list[float] | None = None,
        temporary_limit_durations: list[int] | None = None,
        group_name: str | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Set the operational (thermal) limits of a branch, so overloads can be seen.

        **A branch created by create_line() or create_transformer() has no limits,
        and an element without limits can never be reported as overloaded**: it is
        invisible to get_overloaded_elements(), to the loading_percent metric and
        to the current-limit violations of run_security_analysis(). Giving the new
        branch a rating is therefore the step that makes a connection study
        conclusive.

        Args:
            element_id (str): Id of the branch to rate: a line, a two- or
                three-windings transformer, a boundary or tie line.
            permanent_limit (float): The permanent rating (PATL, the "N" rating),
                in A for limit_type="CURRENT". Copy it from a comparable existing
                branch with get_network_element_data(element_type='line'); an
                order of magnitude for a 400 kV overhead double circuit is
                2000-3000 A.
            side (str): Which end the limits apply to:
                - "BOTH" (default): both ends, as usual on a line
                - "ONE" or "TWO": a single end
                The overload tools read side "ONE", so keep "BOTH" unless you have
                a reason not to.
            limit_type (str): "CURRENT" (default, in A), "ACTIVE_POWER" (MW) or
                "APPARENT_POWER" (MVA).
            temporary_limit_values (list[float], optional): Temporary ratings
                (TATL), e.g. [1200.0, 1500.0], paired positionally with
                temporary_limit_durations. Default: None (permanent limit only).
            temporary_limit_durations (list[int], optional): Acceptable duration of
                each temporary rating, **in seconds**, e.g. [1200, 60] for 20 min
                and 1 min. Must have the same length as temporary_limit_values.
            group_name (str, optional): Name of the limits group. Default: None,
                which writes into the element's active group -- that is what the
                analysis tools read. Only pass a name to build an alternative,
                inactive set of ratings.
            network_id (str, optional): Network to modify. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message listing the ratings applied, or an
                explanation of the failure.

        Example Usage:
            # Rate a newly created line at 800 A, with a 20-minute 1000 A rating
            create_operational_limits("LINE_B4_DC", permanent_limit=800.0,
                                      temporary_limit_values=[1000.0],
                                      temporary_limit_durations=[1200])
            → "Set CURRENT limits on line 'LINE_B4_DC' (sides ONE, TWO): permanent
               800.0 A, temporary 1000.0 A for 1200 s"

            # Rate a new transformer
            create_operational_limits("TR_DC", permanent_limit=1800.0)

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - Calling it again for the same element and group **replaces** that
              group's limits rather than adding to them: pass the permanent and
              all temporary ratings in one call
            - powsybl requires a permanent limit whenever temporary ones exist,
              which is why permanent_limit is mandatory here
            - Limits change nothing in the load flow itself: they are thresholds
              used when interpreting its results

        Related Tools:
            - get_overloaded_elements(): What the limits make visible
            - run_security_analysis(): Reports current-limit violations in N-1
            - get_network_element_data(element_type='line'): Read existing ratings
              (and the loading_percent they produce)
            - create_line() / create_transformer(): Create the branch first
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Setting operational limits on '{element_id}' in {network_id}")

        try:
            element_type = self._require_existing(
                network, element_id, LIMITABLE_TYPES, "Operational limits"
            )

            kind = str(limit_type).strip().upper()
            if kind not in LIMIT_TYPES:
                raise ElementCreationError(
                    f"Unsupported limit_type '{limit_type}'. Supported values: "
                    f"{', '.join(LIMIT_TYPES)}"
                )
            requested_side = str(side).strip().upper()
            if requested_side not in LIMIT_SIDES:
                raise ElementCreationError(
                    f"Unsupported side '{side}'. Supported values: "
                    f"{', '.join(LIMIT_SIDES)}"
                )
            if permanent_limit <= 0:
                raise ElementCreationError(
                    f"permanent_limit must be positive, got {permanent_limit}"
                )

            values = list(temporary_limit_values or [])
            durations = list(temporary_limit_durations or [])
            if len(values) != len(durations):
                raise ElementCreationError(
                    f"temporary_limit_values ({len(values)} value(s)) and "
                    f"temporary_limit_durations ({len(durations)} duration(s)) must "
                    "have the same length: each temporary rating needs its "
                    "acceptable duration in seconds"
                )
            if any(duration <= 0 for duration in durations):
                raise ElementCreationError(
                    "Temporary durations must be positive numbers of seconds; the "
                    "permanent rating is the one given by permanent_limit"
                )

            sides = ["ONE", "TWO"] if requested_side == "BOTH" else [requested_side]

            proxy.invalidate_loadflow(network_id)

            rows: list[dict] = []
            for limit_side in sides:
                rows.append(
                    {
                        "element_id": element_id,
                        "side": limit_side,
                        "name": "permanent_limit",
                        "type": kind,
                        "value": float(permanent_limit),
                        "acceptable_duration": -1,
                    }
                )
                for value, duration in zip(values, durations, strict=True):
                    rows.append(
                        {
                            "element_id": element_id,
                            "side": limit_side,
                            "name": f"{int(duration)}s",
                            "type": kind,
                            "value": float(value),
                            "acceptable_duration": int(duration),
                        }
                    )
            if group_name is not None:
                for row in rows:
                    row["group_name"] = group_name

            network.create_operational_limits(
                **{key: [row[key] for row in rows] for key in rows[0]}
            )

            unit = {"CURRENT": "A", "ACTIVE_POWER": "MW", "APPARENT_POWER": "MVA"}[kind]
            temporary = (
                ", "
                + ", ".join(
                    f"temporary {value} {unit} for {int(duration)} s"
                    for value, duration in zip(values, durations, strict=True)
                )
                if values
                else ""
            )
            group = (
                f" in group '{group_name}' (not the active one, so the analysis "
                "tools will ignore it)"
                if group_name is not None
                else ""
            )
            info = (
                f"Set {kind} limits on {element_type.lower()} '{element_id}' "
                f"(side(s) {', '.join(sides)}){group}: permanent "
                f"{permanent_limit} {unit}{temporary}. Overloads on this element "
                "are now visible to get_overloaded_elements()"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to set operational limits: {e}")
            return f"Failed to set operational limits: {e!s}"

    async def create_reactive_limits(
        self,
        element_id: str,
        min_q: float | None = None,
        max_q: float | None = None,
        p_points: list[float] | None = None,
        min_q_points: list[float] | None = None,
        max_q_points: list[float] | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Set the reactive capability of a generator, a battery or a converter station.

        Without reactive limits, a voltage-regulating unit is treated as having
        unlimited reactive power, which makes a connection study optimistic: the
        load flow holds the voltage setpoint whatever it costs in MVAr. Two shapes
        are supported:

        - a **min/max** range, constant whatever the active power (pass min_q and
          max_q)
        - a **capability curve** Q(P), which is what a real machine has (pass
          p_points with min_q_points and max_q_points)

        Args:
            element_id (str): Id of the generator, battery or converter station.
            min_q (float, optional): Minimum reactive power in MVAr (absorption,
                normally negative). Use with max_q for a constant range.
            max_q (float, optional): Maximum reactive power in MVAr (production).
            p_points (list[float], optional): Active power values in MW at which the
                curve is defined, e.g. [0.0, 50.0, 100.0]. At least two points.
            min_q_points (list[float], optional): Minimum reactive power at each
                point of p_points, same length.
            max_q_points (list[float], optional): Maximum reactive power at each
                point of p_points, same length.
            network_id (str, optional): Network to modify. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message, or an explanation of the failure.

        Example Usage:
            # Constant +/-100 MVAr range
            create_reactive_limits("SOLAR_DC", min_q=-100.0, max_q=100.0)
            → "Set reactive limits on generator 'SOLAR_DC': min/max range
               [-100.0, 100.0] MVAr"

            # Capability curve: less reactive power at full output
            create_reactive_limits("SOLAR_DC", p_points=[0.0, 50.0, 100.0],
                                   min_q_points=[-40.0, -35.0, -20.0],
                                   max_q_points=[40.0, 35.0, 20.0])

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - Give either the min/max pair or the three curve lists, not both
            - Reactive limits are what make a load flow report a unit at its
              reactive limit instead of holding an unreachable voltage
            - Cached loadflow results are cleared

        Related Tools:
            - create_generator() / create_battery(): Create the unit first
            - get_network_element_data(element_type='generator'): Read min_q/max_q
            - run_loadflow(): Recompute with the capability enforced
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Setting reactive limits on '{element_id}' in {network_id}")

        try:
            element_type = self._require_existing(
                network, element_id, REACTIVE_LIMIT_TYPES, "Reactive limits"
            )

            curve_given = any(
                points is not None for points in (p_points, min_q_points, max_q_points)
            )
            range_given = min_q is not None or max_q is not None
            if curve_given and range_given:
                raise ElementCreationError(
                    "Give either a min/max range (min_q, max_q) or a capability "
                    "curve (p_points, min_q_points, max_q_points), not both"
                )
            if not curve_given and not range_given:
                raise ElementCreationError(
                    "Nothing to set: pass min_q and max_q for a constant range, or "
                    "p_points, min_q_points and max_q_points for a capability curve"
                )

            proxy.invalidate_loadflow(network_id)

            if range_given:
                if min_q is None or max_q is None:
                    raise ElementCreationError(
                        "Both min_q and max_q are required for a min/max range"
                    )
                if min_q > max_q:
                    raise ElementCreationError(
                        f"min_q ({min_q} MVAr) must not be greater than max_q "
                        f"({max_q} MVAr)"
                    )
                network.create_minmax_reactive_limits(
                    id=element_id, min_q=float(min_q), max_q=float(max_q)
                )
                info = (
                    f"Set reactive limits on {element_type.lower()} '{element_id}': "
                    f"min/max range [{min_q}, {max_q}] MVAr in network '{network_id}'"
                )
            else:
                points = [p_points, min_q_points, max_q_points]
                if any(series is None for series in points):
                    raise ElementCreationError(
                        "A capability curve needs the three lists p_points, "
                        "min_q_points and max_q_points"
                    )
                lengths = {len(series) for series in points}
                if len(lengths) != 1:
                    raise ElementCreationError(
                        "p_points, min_q_points and max_q_points must have the same "
                        f"length, got {[len(series) for series in points]}"
                    )
                if lengths.pop() < 2:
                    raise ElementCreationError(
                        "A capability curve needs at least two points, since it is "
                        "made of line segments between them"
                    )
                if any(
                    minimum > maximum
                    for minimum, maximum in zip(min_q_points, max_q_points, strict=True)
                ):
                    raise ElementCreationError(
                        "Each point of the curve must have min_q <= max_q"
                    )
                network.create_curve_reactive_limits(
                    id=[element_id] * len(p_points),
                    p=[float(value) for value in p_points],
                    min_q=[float(value) for value in min_q_points],
                    max_q=[float(value) for value in max_q_points],
                )
                info = (
                    f"Set reactive limits on {element_type.lower()} '{element_id}': "
                    f"capability curve over {len(p_points)} points from "
                    f"{p_points[0]} to {p_points[-1]} MW in network '{network_id}'"
                )

            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to set reactive limits: {e}")
            return f"Failed to set reactive limits: {e!s}"

    async def create_ratio_tap_changer(
        self,
        transformer_id: str,
        step_count: int = 17,
        range_percent: float = 10.0,
        tap: int | None = None,
        on_load: bool = True,
        regulating: bool = False,
        target_v: float | None = None,
        target_deadband: float = 1.0,
        regulated_side: str = "TWO",
        rho_values: list[float] | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Add a ratio tap changer (voltage regulation) to a two-windings transformer.

        A transformer created by create_transformer() has a fixed ratio, so
        set_tap_position() has nothing to move and the voltage on its low side
        cannot be controlled. This adds the on-load tap changer a real
        transformer has: a set of steps around the nominal ratio, optionally
        regulating a voltage setpoint.

        The steps are generated for you: `step_count` positions spread evenly over
        ±`range_percent` around the nominal ratio (the usual OLTC is ±10% in 17
        steps), each step having no additional impedance. Pass `rho_values` to
        describe the steps yourself instead.

        Args:
            transformer_id (str): Id of the two-windings transformer to equip.
            step_count (int): Number of tap positions. Default: 17. An odd number
                puts a neutral step (ratio 1) in the middle.
            range_percent (float): Half-range of the ratio, in percent. Default:
                10.0, i.e. steps from 0.90 to 1.10.
            tap (int, optional): Initial position, from 0 to step_count - 1.
                Default: None, which selects the middle (neutral) step.
            on_load (bool): Whether the tap changer can move under load (OLTC).
                Default: True.
            regulating (bool): Whether it actively holds target_v. Default: False,
                which creates the tap changer without letting it act.
            target_v (float, optional): Voltage setpoint in **kV**, required when
                regulating is True. Use a value close to the nominal voltage of the
                regulated side. Default: None.
            target_deadband (float): Deadband around target_v in kV. Default: 1.0.
            regulated_side (str): Which side the voltage is regulated on, "ONE" or
                "TWO". Default: "TWO", the low-voltage side, which is the usual case.
            rho_values (list[float], optional): Explicit ratios, one per step, e.g.
                [0.95, 1.0, 1.05]. Overrides step_count and range_percent.
            network_id (str, optional): Network to modify. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message with the range created and the tap position,
                or an explanation of the failure.

        Example Usage:
            # Standard OLTC, created but not regulating
            create_ratio_tap_changer("TR_DC")
            → "Added a ratio tap changer to transformer 'TR_DC': 17 steps from
               0.900 to 1.100, starting at tap 8, not regulating"

            # Regulating 63 kV on the low side
            create_ratio_tap_changer("TR_DC", regulating=True, target_v=63.0)

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - Only two-windings transformers are supported; a three-windings tap
              changer has to be created with pypowsybl directly
            - A transformer that already has a ratio tap changer is refused: use
              set_tap_position() to move the existing one
            - Whether a regulating tap changer is actually simulated depends on the
              load-flow parameters (transformer voltage control), see
              get_loadflow_params()
            - Cached loadflow results are cleared

        Related Tools:
            - create_transformer(): Create the transformer first
            - set_tap_position(): Move the tap changer once it exists
            - create_phase_tap_changer(): Control active power flow instead
            - get_network_element_data(element_type='ratio_tap_changer'): Inspect it
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Adding ratio tap changer to '{transformer_id}' in {network_id}")

        try:
            self._require_existing(
                network,
                transformer_id,
                frozenset({"TWO_WINDINGS_TRANSFORMER"}),
                "A ratio tap changer",
            )
            if transformer_id in network.get_ratio_tap_changers().index:
                raise ElementCreationError(
                    f"Transformer '{transformer_id}' already has a ratio tap "
                    "changer; use set_tap_position() to move it"
                )

            side = str(regulated_side).strip().upper()
            if side not in TAP_SIDES:
                raise ElementCreationError(
                    f"Unsupported regulated_side '{regulated_side}'. Supported "
                    f"values: {', '.join(TAP_SIDES)}"
                )
            if regulating and target_v is None:
                raise ElementCreationError(
                    "target_v (in kV) is required when regulating is True: a "
                    "regulating ratio tap changer needs a voltage setpoint"
                )

            if rho_values is not None:
                ratios = [float(value) for value in rho_values]
                if len(ratios) < 2:
                    raise ElementCreationError(
                        "rho_values must contain at least two ratios"
                    )
                if any(ratio <= 0 for ratio in ratios):
                    raise ElementCreationError("Every ratio in rho_values must be > 0")
            else:
                if range_percent <= 0:
                    raise ElementCreationError(
                        f"range_percent must be positive, got {range_percent}"
                    )
                self._tap_positions(step_count, None)
                ratios = [
                    1.0 + offset / 100.0
                    for offset in self._linear_values(
                        int(step_count), float(range_percent)
                    )
                ]

            low_tap, position = self._tap_positions(len(ratios), tap)

            proxy.invalidate_loadflow(network_id)

            steps_df = pd.DataFrame.from_records(
                index="id",
                data=[
                    {
                        "id": transformer_id,
                        "b": 0.0,
                        "g": 0.0,
                        "r": 0.0,
                        "x": 0.0,
                        "rho": ratio,
                    }
                    for ratio in ratios
                ],
            )
            rtc_attributes = _without_none(
                {
                    "id": transformer_id,
                    "target_deadband": float(target_deadband),
                    "target_v": target_v,
                    "oltc": bool(on_load),
                    "low_tap": low_tap,
                    "tap": position,
                    "regulating": bool(regulating),
                    "regulated_side": side,
                }
            )
            rtc_df = pd.DataFrame.from_records(index="id", data=[rtc_attributes])
            network.create_ratio_tap_changers(rtc_df, steps_df)

            regulation = (
                f"regulating {target_v} kV on side {side}"
                if regulating
                else "not regulating"
            )
            info = (
                f"Added a ratio tap changer to transformer '{transformer_id}': "
                f"{len(ratios)} steps from {ratios[0]:.3f} to {ratios[-1]:.3f}, "
                f"starting at tap {position}, {regulation}, in network "
                f"'{network_id}'. Move it with set_tap_position()"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to add ratio tap changer: {e}")
            return f"Failed to add ratio tap changer: {e!s}"

    async def create_phase_tap_changer(
        self,
        transformer_id: str,
        step_count: int = 17,
        max_angle_degrees: float = 10.0,
        tap: int | None = None,
        regulating: bool = False,
        regulation_mode: str = "CURRENT_LIMITER",
        regulation_value: float | None = None,
        target_deadband: float = 0.0,
        regulated_side: str = "ONE",
        alpha_values: list[float] | None = None,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Add a phase tap changer (phase shifter) to a two-windings transformer.

        A phase shifter controls the **active power** flowing through the
        transformer by inserting a phase angle, which is how a flow is pushed away
        from an overloaded corridor. This turns a transformer created by
        create_transformer() into one.

        The steps are generated for you: `step_count` positions spread evenly from
        -`max_angle_degrees` to +`max_angle_degrees`, with no ratio change. Pass
        `alpha_values` to describe them yourself instead.

        Args:
            transformer_id (str): Id of the two-windings transformer to equip.
            step_count (int): Number of tap positions. Default: 17.
            max_angle_degrees (float): Largest phase shift in degrees, reached at the
                extreme positions. Default: 10.0, i.e. steps from -10° to +10°.
            tap (int, optional): Initial position, from 0 to step_count - 1.
                Default: None, which selects the middle (0°) step.
            regulating (bool): Whether the phase shifter actively regulates.
                Default: False, which creates it in a fixed position -- the safe
                default, since an active phase shifter changes flows everywhere.
            regulation_mode (str): What is regulated when regulating is True:
                - "CURRENT_LIMITER" (default): keeps the current below
                  regulation_value (in A)
                - "ACTIVE_POWER_CONTROL": holds an active power flow of
                  regulation_value (in MW, signed, seen from regulated_side)
                This pypowsybl version has no "FIXED_TAP" mode: a fixed phase
                shifter is created with regulating=False.
            regulation_value (float, optional): Setpoint for the mode above, in A or
                MW. Required when regulating is True. Default: None.
            target_deadband (float): Deadband around the setpoint. Default: 0.0.
            regulated_side (str): Side the regulation applies to, "ONE" or "TWO".
                Default: "ONE".
            alpha_values (list[float], optional): Explicit phase shifts in degrees,
                one per step, e.g. [-5.0, 0.0, 5.0]. Overrides step_count and
                max_angle_degrees.
            network_id (str, optional): Network to modify. If None, uses the current
                network. Default: None.

        Returns:
            str: Confirmation message with the angle range created, or an
                explanation of the failure.

        Example Usage:
            # Fixed phase shifter, +/-10 degrees available
            create_phase_tap_changer("TR_DC")
            → "Added a phase tap changer to transformer 'TR_DC': 17 steps from
               -10.0° to 10.0°, starting at tap 8, not regulating"

            # Holding 200 MW through the transformer
            create_phase_tap_changer("TR_DC", regulating=True,
                                     regulation_mode="ACTIVE_POWER_CONTROL",
                                     regulation_value=200.0)

        Notes:
            - **Only create equipment when the user explicitly asked for it.**
              This is a long-term study tool (connection studies, network
              development), never a way to fix a constraint, an overload or a
              non-convergent load flow on an operational case
            - Only two-windings transformers are supported
            - A transformer that already has a phase tap changer is refused: use
              set_tap_position(tap_changer_type='phase') to move the existing one
            - Whether the regulation is simulated depends on the load-flow
              parameters (phase shifter regulation), see get_loadflow_params()
            - Cached loadflow results are cleared

        Related Tools:
            - create_transformer(): Create the transformer first
            - set_tap_position(): Move the phase tap changer once it exists
            - create_ratio_tap_changer(): Control voltage instead of power flow
            - run_ac_sensitivity_analysis(): Measure the effect on flows (PSDF)
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        logger.debug(f"Adding phase tap changer to '{transformer_id}' in {network_id}")

        try:
            self._require_existing(
                network,
                transformer_id,
                frozenset({"TWO_WINDINGS_TRANSFORMER"}),
                "A phase tap changer",
            )
            if transformer_id in network.get_phase_tap_changers().index:
                raise ElementCreationError(
                    f"Transformer '{transformer_id}' already has a phase tap "
                    "changer; use set_tap_position(tap_changer_type='phase') to move it"
                )

            side = str(regulated_side).strip().upper()
            if side not in TAP_SIDES:
                raise ElementCreationError(
                    f"Unsupported regulated_side '{regulated_side}'. Supported "
                    f"values: {', '.join(TAP_SIDES)}"
                )
            mode = str(regulation_mode).strip().upper()
            if mode not in PHASE_REGULATION_MODES:
                raise ElementCreationError(
                    f"Unsupported regulation_mode '{regulation_mode}'. Supported "
                    f"values: {', '.join(PHASE_REGULATION_MODES)} (this pypowsybl "
                    "version has no 'FIXED_TAP' mode; use regulating=False for a "
                    "phase shifter that stays put)"
                )
            if regulating and regulation_value is None:
                raise ElementCreationError(
                    "regulation_value is required when regulating is True: in A "
                    "for CURRENT_LIMITER, in MW for ACTIVE_POWER_CONTROL"
                )

            if alpha_values is not None:
                angles = [float(value) for value in alpha_values]
                if len(angles) < 2:
                    raise ElementCreationError(
                        "alpha_values must contain at least two angles"
                    )
            else:
                if max_angle_degrees <= 0:
                    raise ElementCreationError(
                        f"max_angle_degrees must be positive, got {max_angle_degrees}"
                    )
                self._tap_positions(step_count, None)
                angles = self._linear_values(int(step_count), float(max_angle_degrees))

            low_tap, position = self._tap_positions(len(angles), tap)

            proxy.invalidate_loadflow(network_id)

            steps_df = pd.DataFrame.from_records(
                index="id",
                data=[
                    {
                        "id": transformer_id,
                        "b": 0.0,
                        "g": 0.0,
                        "r": 0.0,
                        "x": 0.0,
                        "rho": 1.0,
                        "alpha": angle,
                    }
                    for angle in angles
                ],
            )
            ptc_attributes = _without_none(
                {
                    "id": transformer_id,
                    "regulation_mode": mode,
                    "target_value": regulation_value,
                    "target_deadband": float(target_deadband),
                    "low_tap": low_tap,
                    "tap": position,
                    "regulating": bool(regulating),
                    "regulated_side": side,
                }
            )
            ptc_df = pd.DataFrame.from_records(index="id", data=[ptc_attributes])
            network.create_phase_tap_changers(ptc_df, steps_df)

            regulation = (
                f"regulating in {mode} mode at {regulation_value} on side {side}"
                if regulating
                else "not regulating"
            )
            info = (
                f"Added a phase tap changer to transformer '{transformer_id}': "
                f"{len(angles)} steps from {angles[0]:.1f}° to {angles[-1]:.1f}°, "
                f"starting at tap {position}, {regulation}, in network "
                f"'{network_id}'. Move it with set_tap_position(tap_changer_type='phase')"
            )
            logger.success(info)
            return info

        except ElementCreationError as e:
            logger.warning(str(e))
            return str(e)
        except (pp.PyPowsyblError, ValueError, KeyError) as e:
            logger.error(f"Failed to add phase tap changer: {e}")
            return f"Failed to add phase tap changer: {e!s}"
