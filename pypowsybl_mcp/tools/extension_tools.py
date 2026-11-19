#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Tools reading the IIDM extensions a network carries."""

import json

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from pypowsybl_mcp.tools import NetworkNotFoundError, PyPowsyblTool
from pypowsybl_mcp.utils.artifact_utils import (
    artifact_response,
    dataframe_to_rows,
    normalize_return_options,
)
from pypowsybl_mcp.utils.extension_data import (
    EXTENSION_ATTACH_ALIASES,
    check_extension_name,
    extension_description,
    present_extensions,
    read_extension,
)
from pypowsybl_mcp.utils.pagination import attach_pagination, paginate


def register_extension_tools(mcp: MCPServer, pypowsybl_proxies: TTLCache):
    extension_tools = ExtensionTools(pypowsybl_proxies)
    extension_tools.register_tools_with_mcp(mcp)


class ExtensionTools(PyPowsyblTool):
    async def list_network_extensions(
        self,
        network_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """List the IIDM extensions the network carries.

        Extensions hold data beside the core model, read from the grid file or
        added by an import post-processor, for example:
          - "detail": fixed and variable (thermosensitive) part of each load
            (fixed_p0, variable_p0, fixed_q0, variable_q0);
          - "entsoeCategory": ENTSO-E category code of each generator, set when
            a CGMES file is loaded with the EntsoeCategory post-processor;
          - positions, short-circuit data, observability, ...

        Args:
            network_id (str, optional): Network to inspect. None = current network.

        Returns:
            str: JSON {"success", "network_id", "extensions": [{name, count,
            columns, description}]} - only the extensions at least one element
            carries - and "attach_tokens", the get_network_element_data attach
            tokens that join an extension onto an element table.
        """
        try:
            _, network_id, network = self.resolve_network(ctx, network_id)
        except NetworkNotFoundError as e:
            return json.dumps({"success": False, "error": str(e)}, indent=2)

        try:
            extensions = present_extensions(network)
        except (pp.PyPowsyblError, ValueError) as e:
            logger.error(f"Failed to list extensions: {e}")
            return json.dumps({"success": False, "error": str(e)}, indent=2)

        return json.dumps(
            {
                "success": True,
                "network_id": network_id,
                "extensions": extensions,
                "attach_tokens": {
                    **{
                        token: (
                            f"extension '{alias.extension}' on element_type="
                            f"'{alias.element_type}'"
                        )
                        for token, alias in EXTENSION_ATTACH_ALIASES.items()
                    },
                    "extension:<name>": "any extension, columns named <name>_<column>",
                },
            },
            indent=2,
        )

    async def get_network_extension_data(
        self,
        extension_name: str,
        network_id: str | None = None,
        variant_id: str = "InitialState",
        ids: list[str] | None = None,
        return_as: str = "inline",
        limit: int | None = None,
        cursor: str | int | None = None,
        ctx: Context = None,
    ) -> str:
        """Read one IIDM extension of the network as a table, indexed by element id.

        To filter or sort elements on extension values (e.g. loads by their
        variable part), prefer get_network_element_data with an attach token
        ("load_detail", "entsoe_category", or "extension:<name>").

        Args:
            extension_name (str): Extension to read, e.g. "detail" (loads' fixed /
                variable part) or "entsoeCategory" (generators' ENTSO-E
                category). list_network_extensions lists those present.
            network_id (str, optional): Network to read. None = current network.
            variant_id (str, optional): Variant to read. Default 'InitialState'.
            ids (list[str], optional): Keep only these element ids.
            return_as (str, optional): "inline" (default) or "artifact_json" /
                "artifact_csv" to get the whole table as a file.
            limit (int, optional): Page size (inline). None = all rows.
            cursor (str | int, optional): Page offset; use pagination.nextCursor.

        Returns:
            str: JSON {"success", "network_id", "extension_name", "description",
            "total_elements", "elements": {id: {column: value}}} (+ pagination),
            or the artifact. An extension no element carries gives 0 elements.
        """
        try:
            _, network_id, network = self.resolve_network(ctx, network_id)
            extension_name = check_extension_name(extension_name)
            return_mode, artifact_format = normalize_return_options(return_as, None)
        except (NetworkNotFoundError, ValueError) as e:
            return json.dumps({"success": False, "error": str(e)}, indent=2)

        original_variant_id = network.get_working_variant_id()
        try:
            if variant_id not in network.get_variant_ids():
                msg = f"Variant '{variant_id}' not found in network '{network_id}'"
                return json.dumps({"success": False, "error": msg}, indent=2)
            network.set_working_variant(variant_id)

            frame = read_extension(network, extension_name)
            if ids:
                frame = frame[frame.index.isin(ids)]

            summary = {
                "success": True,
                "network_id": network_id,
                "variant_id": variant_id,
                "extension_name": extension_name,
                "description": extension_description(extension_name),
                "total_elements": len(frame),
            }
            if return_mode == "artifact":
                return json.dumps(
                    artifact_response(
                        summary,
                        dataframe_to_rows(frame),
                        f"{network_id}_{extension_name}",
                        artifact_format,
                    ),
                    indent=2,
                    default=str,
                )

            page, pagination = paginate(frame, limit=limit, cursor=cursor)
            payload = {**summary, "elements": json.loads(page.to_json(orient="index"))}
            return json.dumps(attach_pagination(payload, pagination), indent=2)

        except (pp.PyPowsyblError, ValueError, KeyError, TypeError) as e:
            logger.error(f"Failed to read extension '{extension_name}': {e}")
            return json.dumps(
                {
                    "success": False,
                    "error": f"Failed to read extension '{extension_name}': {e!s}",
                },
                indent=2,
            )
        finally:
            try:
                network.set_working_variant(original_variant_id)
            except (pp.PyPowsyblError, ValueError, KeyError) as e:
                logger.warning(f"Could not restore variant {original_variant_id}: {e}")
