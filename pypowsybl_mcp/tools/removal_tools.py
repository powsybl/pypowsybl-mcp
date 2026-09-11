#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Tool removing elements from a network, the inverse of the creation tools.

Unlike creation, removal is *uniform*: whatever the element, the only input is
its id. The element type is already known to the network, so a single generic
tool is enough -- and better, since the caller does not have to name a type it
could get wrong. What the tool does have to do is pick the right pypowsybl
removal for that type:

* **Feeders** (loads, generators, lines, transformers, shunts, ...) go through
  ``remove_feeder_bays``, which also deletes the breakers and disconnectors
  that attached them. ``Network.remove_elements`` would leave that switching
  equipment behind as orphans in node/breaker topologies.
* **Voltage levels** go through ``remove_voltage_levels``, which cascades to
  every element they contain *and* to the far end of their lines and
  transformers.
* **Substations** have no dedicated removal: their voltage levels are removed
  first, then the (now empty) substation itself.
* **HVDC lines** go through ``remove_hvdc_lines``, which also removes their
  converter stations.
* Anything else (switches, buses, busbar sections) falls back to
  ``remove_elements``.

Because voltage levels and substations destroy far more than the id names, they
are refused unless the caller explicitly asks for a cascade, and the refusal
says what would have been lost.
"""

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.tools import NetworkNotFoundError, PyPowsyblTool

# Element types attached to a bus or busbar section through a bay. Removing one
# of these also removes the switching equipment that connected it.
FEEDER_TYPES = frozenset(
    {
        "LOAD",
        "GENERATOR",
        "BATTERY",
        "LINE",
        "TWO_WINDINGS_TRANSFORMER",
        "THREE_WINDINGS_TRANSFORMER",
        "SHUNT_COMPENSATOR",
        "STATIC_VAR_COMPENSATOR",
        "DANGLING_LINE",
        "BOUNDARY_LINE",
        "LCC_CONVERTER_STATION",
        "VSC_CONVERTER_STATION",
        "HVDC_CONVERTER_STATION",
    }
)

# Removal order: feeders first, then the containers holding them, so that a
# whole site can be listed in any order without an id disappearing under a
# cascade before its turn.
REMOVAL_RANKS = {"VOLTAGE_LEVEL": 1, "SUBSTATION": 2}


def register_removal_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    tools = RemovalTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


class RemovalTools(PyPowsyblTool):
    """Tool removing existing network elements."""

    def _voltage_levels_of(self, network, substation_id: str) -> list[str]:
        voltage_levels = network.get_voltage_levels()
        return voltage_levels[
            voltage_levels["substation_id"] == substation_id
        ].index.tolist()

    def _cascade_summary(self, network, voltage_level_ids: list[str]) -> str:
        """Describe what removing some voltage levels would take down with them."""
        if not voltage_level_ids:
            return "nothing else"

        connectables = 0
        for getter in ("get_loads", "get_generators", "get_busbar_sections"):
            table = getattr(network, getter)()
            if "voltage_level_id" in table.columns:
                connectables += int(
                    table["voltage_level_id"].isin(voltage_level_ids).sum()
                )

        branches = []
        for getter in ("get_lines", "get_2_windings_transformers"):
            table = getattr(network, getter)()
            if {"voltage_level1_id", "voltage_level2_id"} <= set(table.columns):
                touching = table[
                    table["voltage_level1_id"].isin(voltage_level_ids)
                    | table["voltage_level2_id"].isin(voltage_level_ids)
                ]
                branches.extend(touching.index.tolist())

        parts = [f"{connectables} connectable(s)"]
        if branches:
            listed = ", ".join(branches[:5])
            more = f" and {len(branches) - 5} more" if len(branches) > 5 else ""
            parts.append(f"{len(branches)} branch(es) ({listed}{more})")
        return ", ".join(parts)

    def _remove_one(self, network, element_id: str, element_type: str, cascade: bool):
        """Remove a single element, returning the line to report for it."""
        if element_type in FEEDER_TYPES:
            pp.network.remove_feeder_bays(network, connectable_ids=[element_id])
            return f"removed {element_type.lower()} '{element_id}' with its bay(s)"

        if element_type == "HVDC_LINE":
            pp.network.remove_hvdc_lines(network, hvdc_line_ids=[element_id])
            return f"removed hvdc line '{element_id}' with its converter stations"

        if element_type == "VOLTAGE_LEVEL":
            if not cascade:
                summary = self._cascade_summary(network, [element_id])
                return (
                    f"REFUSED voltage level '{element_id}': removing it also "
                    f"removes {summary}. Call again with cascade=True to confirm"
                )
            pp.network.remove_voltage_levels(network, voltage_level_ids=[element_id])
            return f"removed voltage level '{element_id}' and everything it contained"

        if element_type == "SUBSTATION":
            voltage_levels = self._voltage_levels_of(network, element_id)
            if not cascade and voltage_levels:
                summary = self._cascade_summary(network, voltage_levels)
                return (
                    f"REFUSED substation '{element_id}': it still holds voltage "
                    f"level(s) {', '.join(voltage_levels)}, whose removal also "
                    f"removes {summary}. Call again with cascade=True to confirm"
                )
            if voltage_levels:
                # A substation cannot be removed while it holds voltage levels.
                pp.network.remove_voltage_levels(
                    network, voltage_level_ids=voltage_levels
                )
            network.remove_elements(element_id)
            contained = (
                f" and its voltage level(s) {', '.join(voltage_levels)}"
                if voltage_levels
                else ""
            )
            return f"removed substation '{element_id}'{contained}"

        network.remove_elements(element_id)
        return f"removed {element_type.lower()} '{element_id}'"

    async def remove_network_elements(
        self,
        element_ids: list[str],
        cascade: bool = False,
        network_id: str | None = None,
        ctx: Context[ServerSession, None] = None,  # FastMCP injects this
    ) -> str:
        """
        Remove elements from a network, whatever their type.

        The inverse of the creation tools: it undoes an extension that turned out
        not to work, retires equipment (a decommissioned plant, a dismantled
        line), or builds a "without this element" variant of a case. One tool
        covers every element type, because an id is all that is needed — the type
        is read from the network and the right removal is applied:

            - load, generator, line, transformer, shunt, SVC, dangling line,
              converter station → removed **with their bay**, i.e. with the
              breakers and disconnectors that attached them
            - HVDC line → removed with its converter stations
            - voltage level → removed with everything it contains, and with the
              far end of its lines and transformers (needs cascade=True)
            - substation → its voltage levels are removed first, then itself
              (needs cascade=True)
            - switch, bus, busbar section → removed as is

        This is different from set_line_status(), which only *opens* a branch and
        keeps it in the network: an open branch still exists for a security
        analysis, a removed one does not.

        Args:
            element_ids (list[str]): Ids of the elements to remove. They may mix
                types, and be given in any order: feeders are removed before the
                voltage levels and substations that contain them, so listing a
                whole site works. Unknown ids are reported, they do not abort the
                other removals.
            cascade (bool): Confirmation required to remove a voltage level or a
                substation, because those take down every element they contain.
                Default: False, which reports what *would* be removed instead of
                doing it.
            network_id (str, optional): Network to modify. If None, uses the
                current network. Default: None.

        Returns:
            str: One line per requested id, saying what was removed, refused or
                not found.

        Example Usage:
            # Retire one generator
            remove_network_elements(["B3-G"])
            → "removed generator 'B3-G' with its bay(s)"

            # Undo a whole datacenter connection, in any order
            remove_network_elements(["LINE_B4_DC", "DATACENTER", "VL_DC",
                                     "SUB_DC"], cascade=True)

            # Ask first what a voltage level removal would cost
            remove_network_elements(["VL_DC"])
            → "REFUSED voltage level 'VL_DC': removing it also removes 2
               connectable(s), 1 branch(es) (LINE_B4_DC). Call again with
               cascade=True to confirm"

        Notes:
            - **Only remove equipment when the user explicitly asked for it.**
              Never delete an element to make a violation, an overload or a
              divergence disappear: use set_line_status() for an outage study
              and report constraints as results
            - **Removal is not undoable**: the element is gone from the in-memory
              network. Work on a clone (clone_variant() only isolates the state,
              not the structure — duplicate_session() or reloading the file gives
              back the original)
            - Cached loadflow results are cleared: re-run run_loadflow()
            - Removing an element can split the network into several connected
              components, which usually shows up as a non-convergent loadflow
            - Removing a bus or a switch of a live topology is accepted but can
              disconnect equipment silently; prefer removing the equipment itself
            - Changes are in-memory only: use export_network() to save them

        Related Tools:
            - set_line_status(): Open a branch instead of deleting it (N-1 study)
            - create_load() / create_line() / ...: The inverse operations
            - get_network_element_data(): Find the exact ids to remove
            - run_loadflow(): Recompute the state after removal
            - export_network(): Save the reduced network
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            logger.warning(str(e))
            return str(e)

        if isinstance(element_ids, str):
            element_ids = [element_ids]
        requested = [str(item).strip() for item in element_ids if str(item).strip()]
        if not requested:
            error = "No element id provided: nothing to remove"
            logger.warning(error)
            return error

        logger.debug(
            f"Removing {len(requested)} element(s) from network {network_id} "
            f"(cascade={cascade})"
        )

        identifiables = network.get_identifiables()
        types = {
            element_id: str(identifiables.loc[element_id, "type"])
            if element_id in identifiables.index
            else None
            for element_id in requested
        }
        # Containers last, so a cascade never deletes an id still to be processed.
        ordered = sorted(requested, key=lambda i: REMOVAL_RANKS.get(types[i], 0))

        proxy.invalidate_loadflow(network_id)

        report: list[str] = []
        for element_id in ordered:
            element_type = types[element_id]
            if element_type is None:
                report.append(
                    f"NOT FOUND '{element_id}': no element with this id in network "
                    f"'{network_id}'"
                )
                continue
            if element_type == "NETWORK":
                report.append(
                    f"REFUSED '{element_id}': this is the network itself, use "
                    "remove_network() to drop it from the session"
                )
                continue
            try:
                report.append(
                    self._remove_one(network, element_id, element_type, cascade)
                )
            except (pp.PyPowsyblError, ValueError, KeyError) as e:
                logger.error(f"Failed to remove '{element_id}': {e}")
                report.append(f"FAILED '{element_id}': {e!s}")

        info = f"Removal report for network '{network_id}':\n" + "\n".join(
            f"- {line}" for line in report
        )
        if any(
            line.startswith("removed") for line in report
        ):  # something actually changed
            info += "\nRun run_loadflow() to compute the new network state"
        logger.success(info)
        return info
