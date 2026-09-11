#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Read-only admin HTTP API: how many sessions the server holds, and what is in
them.

Exposed as plain HTTP routes rather than MCP tools on purpose:

- a tool would appear in `tools/list`, so the LLM would see it and could call
  it, and answering "how loaded is this server?" is not the job of a session's
  own conversation;
- a monitoring client must be able to ask without opening an MCP session -
  otherwise observing the server would itself create the state being observed.

Authentication reuses `MCP_AUTH_TOKEN`, the token that already protects the
admin tools (`set_session_id`, `duplicate_session`). When it is unset, the
detailed route is disabled, exactly as those tools are.
"""

import os
import secrets
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import pypowsybl as pp
from cachetools import TTLCache
from loguru import logger
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from pypowsybl_mcp.utils.session_registry import SESSIONS, SessionRegistry

SERVER_NAME = "pypowsybl-mcp"


def _server_version() -> str:
    try:
        return version("pypowsybl-mcp")
    except PackageNotFoundError:  # running from a source checkout
        return "unknown"


def _rss_mb() -> float | None:
    """Resident memory of this process in MiB, or None where it is unreadable.

    Read from `/proc/self/statm` rather than through `psutil`, to keep the
    server's dependency list unchanged. Non-Linux platforms simply report None.
    """
    try:
        with open("/proc/self/statm", encoding="utf-8") as statm:
            resident_pages = int(statm.read().split()[1])
        return round(resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        return None


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _authorized(request: Request) -> bool:
    """Whether `request` carries the admin token.

    Accepts `Authorization: Bearer <token>` or `X-Admin-Token: <token>`; the
    token is deliberately not accepted as a query parameter, where it would end
    up in access logs and browser history.
    """
    expected = os.getenv("MCP_AUTH_TOKEN")
    if not expected:
        return False
    header = request.headers.get("authorization", "")
    presented = (
        header[len("Bearer ") :] if header.startswith("Bearer ") else ""
    ) or request.headers.get("x-admin-token", "")
    return bool(presented) and secrets.compare_digest(presented, expected)


def _network_details(proxy: Any, full: bool) -> list[dict[str, Any]]:
    """One entry per network loaded in `proxy`.

    Element counts are opt-in (`full`): they materialize a dataframe per element
    type, which is cheap on a test network and decidedly not on a real one.
    """
    details = []
    for network_id in list(proxy.networks):
        entry: dict[str, Any] = {
            "id": network_id,
            "current": network_id == proxy.current_network_id,
        }
        if full:
            network = proxy.networks.get(network_id)
            try:
                entry["buses"] = len(network.get_buses())
                entry["lines"] = len(network.get_lines())
                entry["generators"] = len(network.get_generators())
                entry["loads"] = len(network.get_loads())
            except Exception as exc:  # noqa: BLE001 - a network can be in any state
                logger.warning(f"Could not count the elements of {network_id}: {exc}")
                entry["counts_error"] = str(exc)
        details.append(entry)
    return details


def _session_payload(
    session_id: Any, proxy: Any, stats: Any, ttl: float | None, now: float, full: bool
) -> dict[str, Any]:
    """The reported state of one session."""
    payload: dict[str, Any] = {
        "session_id": str(session_id),
        "created_at": _iso(stats.created_at),
        "created_at_estimated": stats.created_at_estimated,
        "last_seen": _iso(stats.last_seen),
        "age_s": round(now - stats.created_at, 1),
        "idle_s": round(now - stats.last_seen, 1),
        # cachetools measures a TTL from insertion, not from last access: a
        # session is dropped `ttl` seconds after it was created however busy it
        # has been since. This is therefore a countdown, not an idle timeout.
        "expires_in_s": (
            round(ttl - (now - stats.created_at), 1) if ttl is not None else None
        ),
        "tool_calls": stats.tool_calls,
        "errors": stats.errors,
        "last_tool": stats.last_tool,
        "last_tool_at": _iso(stats.last_tool_at),
        # Time spent in tool calls. `avg` is over the calls that were timed,
        # which is all of them here; it is reported rather than left to the
        # client so a client cannot divide by a zero call count.
        "total_duration_ms": round(stats.total_duration_ms, 1),
        "avg_duration_ms": (
            round(stats.total_duration_ms / stats.tool_calls, 1)
            if stats.tool_calls
            else None
        ),
        "max_duration_ms": round(stats.max_duration_ms, 1),
        "last_duration_ms": (
            None if stats.last_duration_ms is None else round(stats.last_duration_ms, 1)
        ),
        "tools_used": dict(
            sorted(stats.tools_used.items(), key=lambda kv: kv[1], reverse=True)
        ),
    }
    if proxy is None:
        # Tracked, but the cache dropped it between reconcile and now.
        payload["state"] = "gone"
        return payload

    payload.update(
        {
            "current_network_id": proxy.current_network_id,
            "networks": _network_details(proxy, full),
            "loadflow_results": len(proxy.loadflow_results),
            "resources": len(proxy.resources),
            "plugin_results": len(proxy.plugin_results),
            "lf_provider": proxy.lf_provider,
        }
    )
    return payload


def build_snapshot(
    proxies: TTLCache,
    registry: SessionRegistry = SESSIONS,
    *,
    include_sessions: bool = True,
    full_networks: bool = False,
) -> dict[str, Any]:
    """The server's current session state, as a JSON-serializable dict.

    Reconciles the registry with the cache first, so expired sessions are
    counted and reaped rather than reported as live (`cachetools` only expires
    on access).
    """
    registry.reconcile(proxies)
    now = time.time()
    ttl = getattr(proxies, "ttl", None)
    maxsize = getattr(proxies, "maxsize", None)
    tracked = registry.snapshot()

    snapshot: dict[str, Any] = {
        "server": {
            "name": SERVER_NAME,
            "version": _server_version(),
            "pypowsybl_version": pp.__version__,
            "uptime_s": round(registry.uptime_s, 1),
            "started_at": _iso(registry.started_at),
            "rss_mb": _rss_mb(),
            "sessions": len(tracked),
            "max_sessions": maxsize,
            "session_ttl_s": ttl,
            "evictions": {
                "expired": registry.expired_total,
                "capacity": registry.evicted_total,
            },
        },
        "generated_at": _iso(now),
    }
    if include_sessions:
        snapshot["sessions"] = [
            _session_payload(
                session_id, proxies.get(session_id), stats, ttl, now, full_networks
            )
            for session_id, stats in sorted(
                tracked.items(), key=lambda kv: kv[1].last_seen, reverse=True
            )
        ]
    return snapshot


def register_admin_routes(
    mcp: FastMCP, proxies: TTLCache, registry: SessionRegistry = SESSIONS
) -> None:
    """Add `/admin/health` and `/admin/sessions` to the MCP server's HTTP app."""

    @mcp.custom_route("/admin/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Liveness and capacity, without authentication: no session detail is
        exposed here, so it can back a container health check or an uptime
        probe."""
        snapshot = build_snapshot(proxies, registry, include_sessions=False)
        return JSONResponse({"status": "ok", **snapshot})

    @mcp.custom_route("/admin/sessions", methods=["GET"])
    async def sessions(request: Request) -> JSONResponse:
        """Every session the server holds, and what each one contains.

        `?networks=full` adds per-network element counts - expensive on large
        networks, so it is opt-in.
        """
        if not os.getenv("MCP_AUTH_TOKEN"):
            logger.warning("Admin API refused: MCP_AUTH_TOKEN is not set")
            return JSONResponse(
                {"error": "admin API disabled: MCP_AUTH_TOKEN is not set"},
                status_code=403,
            )
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        full = request.query_params.get("networks") == "full"
        return JSONResponse(build_snapshot(proxies, registry, full_networks=full))

    logger.info("Admin API available on /admin/health and /admin/sessions")
