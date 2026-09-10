#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Attribution of tool calls to sessions, for the admin API.

`PyPowsyblTool.get_proxy()` already sees every call that touches session state,
but not which tool made it nor whether it failed. That is known one level up,
in FastMCP's tool manager, which is wrapped here.

The wrapper goes on `mcp._tool_manager.call_tool` rather than on
`mcp.call_tool`: `FastMCP._setup_handlers()` binds `self.call_tool` into the
low-level server at construction time, so replacing that attribute afterwards
would have no effect, while `self._tool_manager` is looked up on every call.
"""

import time
from typing import Any

from loguru import logger
from mcp.server.fastmcp import FastMCP

from pypowsybl_mcp.utils.session_registry import SessionRegistry

_INSTRUMENTED_FLAG = "_pypowsybl_mcp_instrumented"


def _session_id_of(context: Any) -> Any | None:
    """The session id carried by `context`, or None if there is none (yet).

    Read *after* the tool ran: the id is created lazily by `get_session_id()`
    inside the tool, so before the call a first-time session still looks
    anonymous.
    """
    try:
        return getattr(context.session, "session_id", None)
    except AttributeError:
        return None


def instrument_tool_calls(mcp: FastMCP, registry: SessionRegistry) -> None:
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
        started = time.monotonic()
        error = False
        try:
            return await original_call_tool(name, arguments, *args, **kwargs)
        except Exception:
            error = True
            raise
        finally:
            try:
                elapsed_ms = (time.monotonic() - started) * 1000
                registry.record_call(
                    _session_id_of(context),
                    name,
                    error=error,
                    duration_ms=elapsed_ms,
                )
                logger.debug(
                    f"Tool {name} finished in {elapsed_ms:.0f} ms (error={error})"
                )
            except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks a call
                logger.warning(f"Could not record the call to {name}: {exc}")

    tool_manager.call_tool = call_tool
    setattr(tool_manager, _INSTRUMENTED_FLAG, True)
    logger.debug("Tool calls are now attributed to sessions")
