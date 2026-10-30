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

import json

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from pypowsybl_mcp.tools import NetworkNotFoundError, PyPowsyblTool
from pypowsybl_mcp.utils.element_types import ELEMENT_TYPE_TO_GETTER

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

# Columns through which an element table names the voltage level of each end.
VOLTAGE_LEVEL_COLUMNS = (
    "voltage_level_id",
    "voltage_level1_id",
    "voltage_level2_id",
    "voltage_level3_id",
)


def register_removal_tools(mcp: MCPServer, pypowsybl_proxies: TTLCache):
    tools = RemovalTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


class RemovalTools(PyPowsyblTool):
    """Tool removing existing network elements."""

    def _voltage_levels_of(self, network, substation_id: str) -> list[str]:
        voltage_levels = network.get_voltage_levels()
        return voltage_levels[
            voltage_levels["substation_id"] == substation_id
        ].index.tolist()

    def _cascade(
        self, network, voltage_level_ids: list[str]
    ) -> tuple[dict[str, str], set[str]]:
        """What removing some voltage levels takes down with them.

        Mirrors ``remove_voltage_levels``: every element with an end in one of
        the voltage levels, the HVDC lines whose converter station goes (with
        the station at their far end), and the tie lines whose boundary line
        goes. Every element table is scanned, so a type added upstream is
        counted without a change here.

        Returns the removed ids with their type, and the subset reaching beyond
        the voltage levels -- the part a caller is least likely to expect.
        """
        identifiables = network.get_identifiables()
        removed: set[str] = set()
        beyond: set[str] = set()
        for getter in sorted(set(ELEMENT_TYPE_TO_GETTER.values())):
            try:
                table = getattr(network, getter)()
            except (pp.PyPowsyblError, ValueError):  # pragma: no cover - defensive
                continue
            columns = [name for name in VOLTAGE_LEVEL_COLUMNS if name in table.columns]
            if not columns:
                continue
            ends = table[columns]
            inside = ends.isin(voltage_level_ids)
            touching = inside.any(axis=1)
            removed.update(table.index[touching])
            beyond.update(table.index[touching & ~(inside | ends.isna()).all(axis=1)])

        hvdc_lines = network.get_hvdc_lines()
        for line_id, row in hvdc_lines.iterrows():
            stations = {row["converter_station1_id"], row["converter_station2_id"]}
            if stations & removed:
                pulled = {line_id} | (stations - removed)
                removed.update(pulled | stations)
                beyond.update(pulled)

        tie_lines = network.get_tie_lines()
        for line_id, row in tie_lines.iterrows():
            if {row["boundary_line1_id"], row["boundary_line2_id"]} & removed:
                removed.add(line_id)
                beyond.add(line_id)

        types = {
            element_id: str(identifiables.loc[element_id, "type"])
            for element_id in removed
            if element_id in identifiables.index
        }
        return types, beyond & set(types)

    def _cascade_details(self, network, voltage_level_ids: list[str]) -> dict:
        """What removing some voltage levels takes down, by element type.

        ``inside`` counts the equipment of the voltage levels, ``beyond`` lists
        by id what lies outside them (branches, HVDC links and their far-end
        stations, tie lines), and ``switches`` counts the switching equipment,
        which is listed as a whole: it is the bays of everything else.
        """
        types, beyond = self._cascade(network, voltage_level_ids)
        inside: dict[str, int] = {}
        reaching: dict[str, list[str]] = {}
        switches = 0
        for element_id, kind in sorted(types.items()):
            if kind == "SWITCH":
                switches += 1
            elif kind == "VOLTAGE_LEVEL":
                continue
            elif element_id in beyond:
                reaching.setdefault(kind.lower(), []).append(element_id)
            else:
                inside[kind.lower()] = inside.get(kind.lower(), 0) + 1
        return {"inside": inside, "switches": switches, "beyond": reaching}

    def _cascade_summary(self, network, voltage_level_ids: list[str]) -> str:
        """Describe what removing some voltage levels would take down with them."""
        return _describe_cascade(self._cascade_details(network, voltage_level_ids))

    def _remove_one(
        self, network, element_id: str, element_type: str, cascade: bool
    ) -> dict:
        """Remove a single element, returning its item of the reply."""
        kind = element_type.lower()

        def removed(methods: list[str], message: str, details: dict | None = None):
            return _item(element_id, kind, "removed", methods, message, details)

        if element_type in FEEDER_TYPES:
            pp.network.remove_feeder_bays(network, connectable_ids=[element_id])
            return removed(
                ["pp.network.remove_feeder_bays"],
                f"removed {kind} '{element_id}' with its bay(s)",
            )

        if element_type == "HVDC_LINE":
            pp.network.remove_hvdc_lines(network, hvdc_line_ids=[element_id])
            return removed(
                ["pp.network.remove_hvdc_lines"],
                f"removed hvdc line '{element_id}' with its converter stations",
            )

        if element_type == "VOLTAGE_LEVEL":
            details = self._cascade_details(network, [element_id])
            if not cascade:
                problem = (
                    f"voltage level '{element_id}' not removed: removing it also "
                    f"removes {_describe_cascade(details)}. Call again with "
                    "cascade=True to confirm"
                )
                return _item(element_id, kind, "refused", [], problem, details, problem)
            pp.network.remove_voltage_levels(network, voltage_level_ids=[element_id])
            return removed(
                ["pp.network.remove_voltage_levels"],
                f"removed voltage level '{element_id}' and everything it contained",
                details,
            )

        if element_type == "SUBSTATION":
            voltage_levels = self._voltage_levels_of(network, element_id)
            details = (
                self._cascade_details(network, voltage_levels)
                if voltage_levels
                else None
            )
            if not cascade and voltage_levels:
                problem = (
                    f"substation '{element_id}' not removed: it still holds voltage "
                    f"level(s) {', '.join(voltage_levels)}, whose removal also "
                    f"removes {_describe_cascade(details)}. Call again with "
                    "cascade=True to confirm"
                )
                return _item(element_id, kind, "refused", [], problem, details, problem)
            methods = ["Network.remove_elements"]
            if voltage_levels:
                # A substation cannot be removed while it holds voltage levels.
                pp.network.remove_voltage_levels(
                    network, voltage_level_ids=voltage_levels
                )
                methods.insert(0, "pp.network.remove_voltage_levels")
            network.remove_elements(element_id)
            contained = (
                f" and its voltage level(s) {', '.join(voltage_levels)}"
                if voltage_levels
                else ""
            )
            return removed(
                methods, f"removed substation '{element_id}'{contained}", details
            )

        network.remove_elements(element_id)
        return removed(["Network.remove_elements"], f"removed {kind} '{element_id}'")

    async def remove_network_elements(
        self,
        element_ids: list[str],
        cascade: bool = False,
        network_id: str | None = None,
        ctx: Context = None,  # MCPServer injects this
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
            str: JSON, with the same keys whatever the outcome:
                - success: whether every requested id was removed
                - network_id, removed_ids, removed_count
                - items: one per id, in the order they were processed, each with
                  id, element_type, status (removed, refused, not_found,
                  failed), methods (the pypowsybl calls), message, problem, and
                  cascade: for a voltage level or a substation, what goes with
                  it -- {inside: {type: count}, switches: n, beyond: {type: [ids]}}
                - errors: [{id, problem}] for every id not removed
                - message: one line summing it up
                - loadflow_invalidated: whether cached load-flow results were
                  dropped

        Example Usage:
            # Retire one generator
            remove_network_elements(["B3-G"])
            → items[0].message: "removed generator 'B3-G' with its bay(s)"

            # Undo a whole datacenter connection, in any order
            remove_network_elements(["LINE_B4_DC", "DATACENTER", "VL_DC",
                                     "SUB_DC"], cascade=True)

            # Ask first what a voltage level removal would cost
            remove_network_elements(["VL_DC"])
            → items[0].status: "refused", items[0].problem: "voltage level 'VL_DC'
               not removed: removing it also removes 1 bus(es), 1 load(s); and
               beyond it: 1 line(s) (LINE_B4_DC). Call again with cascade=True
               to confirm", items[0].cascade: {"inside": {"bus": 1, "load": 1},
               "switches": 0, "beyond": {"line": ["LINE_B4_DC"]}}

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
            - create_network_element() / create_network_elements(): The inverse
              operations
            - get_network_element_data(): Find the exact ids to remove
            - run_loadflow(): Recompute the state after removal
            - export_network(): Save the reduced network
        """
        try:
            proxy, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            return _reply(_envelope(network_id, [], [_error(None, str(e))], str(e)))

        if isinstance(element_ids, str):
            element_ids = [element_ids]
        requested = [str(item).strip() for item in element_ids if str(item).strip()]
        if not requested:
            message = "No element id provided: nothing to remove"
            logger.warning(message)
            return _reply(_envelope(network_id, [], [_error(None, message)], message))

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

        items: list[dict] = []
        for element_id in ordered:
            element_type = types[element_id]
            if element_type is None:
                problem = f"no element '{element_id}' in network '{network_id}'"
                items.append(
                    _item(element_id, None, "not_found", [], problem, None, problem)
                )
                continue
            if element_type == "NETWORK":
                problem = (
                    f"'{element_id}' is the network itself: use remove_network() to "
                    "drop it from the session"
                )
                items.append(
                    _item(element_id, "network", "refused", [], problem, None, problem)
                )
                continue
            try:
                items.append(
                    self._remove_one(network, element_id, element_type, cascade)
                )
            except (pp.PyPowsyblError, ValueError, KeyError) as e:
                logger.error(f"Failed to remove '{element_id}': {e}")
                problem = f"pypowsybl refused to remove '{element_id}': {e!s}"
                items.append(
                    _item(
                        element_id,
                        element_type.lower(),
                        "failed",
                        [],
                        problem,
                        None,
                        problem,
                    )
                )

        removed = [item["id"] for item in items if item["status"] == "removed"]
        # A failed removal may have gone half way, so it counts as a change too;
        # refusals and unknown ids leave the network, and its results, intact.
        changed = any(item["status"] in ("removed", "failed") for item in items)
        if changed:
            proxy.invalidate_loadflow(network_id)
        not_removed = [item for item in items if item["status"] != "removed"]
        if not not_removed:
            message = (
                f"Removed {len(removed)} element(s) from network '{network_id}'. "
                "Run run_loadflow() to compute the new network state"
            )
            logger.success(message)
        else:
            message = (
                f"Removed {len(removed)} of {len(items)} element(s) from network "
                f"'{network_id}'; not removed: "
                + ", ".join(
                    f"'{item['id']}' ({item['status']})" for item in not_removed
                )
            )
            logger.warning(message)
        errors = [_error(item["id"], item["problem"]) for item in not_removed]
        return _reply(_envelope(network_id, items, errors, message, changed))


def _item(
    element_id, element_type, status, methods, message, cascade=None, problem=None
):
    """One element of the reply, the same keys whatever its status."""
    return {
        "id": element_id,
        "element_type": element_type,
        "status": status,
        "methods": methods,
        "message": message,
        "cascade": cascade,
        "problem": problem,
    }


def _error(element_id, problem: str) -> dict:
    return {"id": element_id, "problem": problem}


def _envelope(network_id, items, errors, message, loadflow_invalidated=False) -> dict:
    removed = [item["id"] for item in items if item["status"] == "removed"]
    return {
        "success": bool(items) and not errors,
        "network_id": network_id,
        "removed_ids": removed,
        "removed_count": len(removed),
        "items": items,
        "errors": errors,
        "message": message,
        "loadflow_invalidated": loadflow_invalidated,
    }


def _reply(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str)


def _label(kind: str, count: int) -> str:
    name = kind.replace("_", " ")
    return (
        f"{count} {name}(es)"
        if name.endswith(("s", "ch", "x"))
        else f"{count} {name}(s)"
    )


def _describe_cascade(details: dict) -> str:
    """The cascade details as one phrase, as shown in a refusal."""
    parts = [_label(kind, count) for kind, count in sorted(details["inside"].items())]
    if details["switches"]:
        parts.append(f"{details['switches']} switch(es)")
    if not parts and not details["beyond"]:
        return "nothing else"
    summary = ", ".join(parts) if parts else "no equipment of its own"
    if details["beyond"]:
        listed = []
        for kind, ids in sorted(details["beyond"].items()):
            shown = ", ".join(ids[:5])
            more = f" and {len(ids) - 5} more" if len(ids) > 5 else ""
            listed.append(f"{_label(kind, len(ids))} ({shown}{more})")
        summary += "; and beyond it: " + ", ".join(listed)
    return summary
