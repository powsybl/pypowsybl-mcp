#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Keep every client on a stateful, handshake-era MCP connection.

The server holds networks, variants and parameters per session, and a session
is tied to the MCP connection (see `user_session_management.py`). Protocol
versions from 2026-07-28 on are stateless: each request builds its own
connection, so nothing loaded by one tool call would survive until the next.

Clients in `auto` mode probe `server/discover` first and fall back to the
`initialize` handshake on any JSON-RPC error. Refusing every request made in a
stateless protocol version therefore sends them down the stateful path, and a
client pinned to such a version gets an explicit error instead of silently
losing its state between calls.
"""

from typing import Any

from loguru import logger
from mcp import MCPError
from mcp.server import ServerRequestContext
from mcp.types import METHOD_NOT_FOUND
from mcp.types.version import MODERN_PROTOCOL_VERSIONS


async def require_stateful_session(ctx: ServerRequestContext[Any, Any], call_next):
    """Server middleware rejecting requests made in a stateless protocol version."""
    if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        logger.debug(
            f"Refused {ctx.method} in stateless protocol {ctx.protocol_version}"
        )
        raise MCPError(
            METHOD_NOT_FOUND,
            f"This server keeps state across tool calls and requires a stateful "
            f"MCP session: protocol version {ctx.protocol_version} is not "
            f"supported, connect with the initialize handshake instead.",
        )
    return await call_next(ctx)
