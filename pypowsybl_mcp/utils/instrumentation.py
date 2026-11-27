#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Attribution of tool calls to sessions, for the admin API.

`PyPowsyblTool.get_proxy()` already sees every call that touches session state,
but not which tool made it nor whether it failed. That is known one level up,
in MCPServer's tool manager, which is wrapped here.

The wrapper goes on `mcp._tool_manager.call_tool`, which receives the request
`Context` for every tool call and is looked up on each call, so it can be
replaced after the server is built.
"""

import time
from contextvars import ContextVar
from typing import Any

from loguru import logger
from mcp.server.mcpserver import MCPServer

from pypowsybl_mcp.utils.session_registry import SessionRegistry
from pypowsybl_mcp.utils.user_session_management import peek_session_id

_INSTRUMENTED_FLAG = "_pypowsybl_mcp_instrumented"

# The request context of the tool call in progress, for _tag_session.
_tool_call_context: ContextVar[Any] = ContextVar("tool_call_context", default=None)


def _session_id_of(context: Any) -> Any | None:
    """The session id carried by `context`, or None if there is none (yet).

    Read *after* the tool ran: the id is created lazily by `get_session_id()`
    inside the tool, so before the call a first-time session still looks
    anonymous.
    """
    try:
        return peek_session_id(context)
    except (AttributeError, ValueError):  # no request behind this context
        return None


def _tag_session(record) -> None:
    """Loguru patcher: tag what a tool call logs with its session.

    Read for each message: on a session's first call, the id only exists once
    the tool created it, and what the tool logs from then on is tagged too.
    """
    context = _tool_call_context.get()
    if context is not None and "session_id" not in record["extra"]:
        session_id = _session_id_of(context)
        if session_id is not None:
            record["extra"]["session_id"] = session_id


def instrument_tool_calls(mcp: MCPServer, registry: SessionRegistry) -> None:
    """Count tool calls, failures and durations per session on `mcp`.

    Idempotent: instrumenting the same server twice is a no-op, so importing
    the server module more than once (tests, reload) cannot stack wrappers.
    """
    tool_manager = mcp._tool_manager
    if getattr(tool_manager, _INSTRUMENTED_FLAG, False):
        return

    original_call_tool = tool_manager.call_tool

    async def call_tool(name: str, arguments: dict[str, Any], *args, **kwargs) -> Any:
        context = kwargs.get("context") or (args[0] if args else None)
        # perf_counter(), not monotonic(): on Windows before Python 3.13,
        # monotonic() is GetTickCount64(), whose resolution is the ~15.6 ms
        # scheduler tick, so every call shorter than that would be recorded
        # as 0 ms. Python 3.13 moved monotonic() to QueryPerformanceCounter();
        # perf_counter() has always been that clock, on every version and OS.
        started = time.perf_counter()
        error = False
        token = _tool_call_context.set(context)
        try:
            return await original_call_tool(name, arguments, *args, **kwargs)
        except Exception:
            error = True
            raise
        finally:
            _tool_call_context.reset(token)
            try:
                elapsed_ms = (time.perf_counter() - started) * 1000
                session_id = _session_id_of(context)
                registry.record_call(
                    session_id,
                    name,
                    error=error,
                    duration_ms=elapsed_ms,
                )
                logger.bind(session_id=session_id).debug(
                    f"Tool {name} finished in {elapsed_ms:.0f} ms (error={error})"
                )
            except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks a call
                logger.warning(f"Could not record the call to {name}: {exc}")

    tool_manager.call_tool = call_tool
    logger.configure(patcher=_tag_session)  # The server's only patcher.
    setattr(tool_manager, _INSTRUMENTED_FLAG, True)
    logger.debug("Tool calls are now attributed to sessions")
