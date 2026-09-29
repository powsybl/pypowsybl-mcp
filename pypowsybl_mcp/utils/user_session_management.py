#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import uuid

from loguru import logger
from mcp.server.mcpserver import Context

# Key under which the session ID is kept in the MCP connection state.
SESSION_ID_KEY = "pypowsybl_mcp.session_id"


def _connection_state(ctx: Context) -> dict | None:
    """The per-connection state dict behind `ctx`, or None if unreachable.

    Since mcp 2, `ctx.session` is rebuilt for every request, so an attribute
    set on it is lost at the end of the tool call. Only the connection outlives
    a request (one per `Mcp-Session-Id` on streamable HTTP); the SDK exposes it
    to the session object only, hence the `_connection` lookup.
    """
    connection = getattr(ctx.session, "_connection", None)
    state = getattr(connection, "state", None)
    return state if isinstance(state, dict) else None


def peek_session_id(ctx: Context | None) -> str | None:
    """The session ID bound to `ctx`, or None if none was assigned yet."""
    if not ctx:
        return None
    state = _connection_state(ctx)
    if state is not None:
        return state.get(SESSION_ID_KEY)
    # No connection (a context built outside a live request): the ID lives on
    # the session object itself.
    return getattr(ctx.session, "session_id", None)


def bind_session_id(ctx: Context, session_id: str) -> None:
    """Bind `session_id` to the connection behind `ctx`."""
    state = _connection_state(ctx)
    if state is not None:
        state[SESSION_ID_KEY] = session_id
    else:
        ctx.session.session_id = session_id


def check_session_id(ctx: Context) -> Context:
    """Check and set session ID if not present"""
    if ctx and peek_session_id(ctx) is None:
        session_id = str(uuid.uuid4())
        bind_session_id(ctx, session_id)
        logger.debug(f"Generated new session ID: {session_id}")
    return ctx


def get_session_id(ctx: Context) -> str:
    """Retrieve the session ID from the context"""
    ctx = check_session_id(ctx)
    session_id = peek_session_id(ctx)
    logger.debug(session_id)
    return session_id


def get_session_info(ctx: Context):
    """Get session info"""
    ctx = check_session_id(ctx)

    if ctx:
        session_id = peek_session_id(ctx)
        request_id = ctx.request_id
        client_params = ctx.session.client_params
        client_name = client_params.client_info.name if client_params else None

        logger.debug(
            f"Session ID: {session_id}, Request ID: {request_id}, Client: {client_name}"
        )
