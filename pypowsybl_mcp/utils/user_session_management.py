#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.

import uuid

from loguru import logger
from mcp.server.fastmcp import Context


def check_session_id(ctx: Context) -> Context:
    """Check and set session ID if not present"""
    if ctx:
        session_id = (
            ctx.session.session_id if hasattr(ctx.session, "session_id") else None
        )
        if session_id is None:
            session_id = uuid.uuid4()
            ctx.session.session_id = session_id
            logger.debug(f"Generated new session ID: {session_id}")
    return ctx


def get_session_id(ctx: Context) -> str:
    """Retrieve the session ID from the context"""
    ctx = check_session_id(ctx)
    logger.debug(ctx.session.session_id)
    return ctx.session.session_id


def get_session_info(ctx: Context):
    """Get session info"""
    ctx = check_session_id(ctx)

    if ctx:
        session_id = (
            ctx.session.session_id if hasattr(ctx.session, "session_id") else None
        )
        request_id = ctx.request_id
        client_id = ctx.client_id

        logger.debug(
            f"Session ID: {session_id}, Request ID: {request_id}, Client ID: {client_id}"
        )
