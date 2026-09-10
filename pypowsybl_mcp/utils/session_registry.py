#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Per-session bookkeeping for the server's session cache.

`pypowsybl_proxies` (a `TTLCache`) knows *which* sessions exist, but not when
they appeared, when they were last used, or how much traffic they carry. It
also reaps expired entries lazily and does so through `Cache.__delitem__`
(see `TTLCache.expire`), which bypasses subclass hooks - so a cache subclass
cannot reliably observe its own evictions either.

This registry keeps that bookkeeping next to the cache and reconciles itself
with it on demand: whatever the cache no longer holds is counted as gone
(expired or evicted for capacity) and dropped. Nothing here is on the hot path
of a tool call beyond a dict lookup and two assignments, and none of it is
persisted - it describes the running process only.
"""

import threading
import time
from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class SessionStats:
    """What is known about one session, beyond the state its proxy holds."""

    created_at: float
    last_seen: float
    tool_calls: int = 0
    errors: int = 0
    last_tool: str | None = None
    last_tool_at: float | None = None
    # True when the session was first seen already present in the cache, so its
    # creation time is the moment the registry noticed it, not the real one
    # (happens for a session created by `duplicate_session`, or if the registry
    # is introduced while sessions are already live).
    created_at_estimated: bool = False
    tools_used: dict[str, int] = field(default_factory=dict)


class SessionRegistry:
    """Thread-safe bookkeeping for the sessions held in a `TTLCache`."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = threading.RLock()
        self._sessions: dict[Hashable, SessionStats] = {}
        # Sessions the cache dropped, split by the reason we can infer.
        self.expired_total = 0
        self.evicted_total = 0

    # --- recording -----------------------------------------------------------

    def touch(self, session_id: Hashable, *, estimated: bool = False) -> SessionStats:
        """Mark `session_id` as used now, creating its entry if needed."""
        now = time.time()
        with self._lock:
            stats = self._sessions.get(session_id)
            if stats is None:
                stats = SessionStats(
                    created_at=now, last_seen=now, created_at_estimated=estimated
                )
                self._sessions[session_id] = stats
                logger.debug(f"Session registry: tracking session {session_id}")
            else:
                stats.last_seen = now
            return stats

    def record_call(
        self, session_id: Hashable | None, tool_name: str, *, error: bool = False
    ) -> None:
        """Record one tool call against `session_id` (ignored when unknown).

        A `None` session id means the call never touched session state (the tool
        does not take a context, or failed before reading it), so there is
        nothing to attribute it to.
        """
        if session_id is None:
            return
        with self._lock:
            stats = self.touch(session_id)
            stats.tool_calls += 1
            stats.last_tool = tool_name
            stats.last_tool_at = stats.last_seen
            stats.tools_used[tool_name] = stats.tools_used.get(tool_name, 0) + 1
            if error:
                stats.errors += 1

    def forget(self, session_id: Hashable) -> None:
        """Drop what is known about `session_id` (an explicit removal, not an
        eviction: it is not counted as one)."""
        with self._lock:
            self._sessions.pop(session_id, None)

    # --- reading -------------------------------------------------------------

    def reconcile(self, cache: Any) -> None:
        """Align the registry with `cache`, counting whatever it lost.

        Expiry in `cachetools` only happens on access, so `expire()` is called
        first: without it a session whose TTL elapsed hours ago is still both in
        the cache and in `len(cache)`.
        """
        try:
            cache.expire()
        except AttributeError:  # not a TTLCache (a plain dict in tests)
            pass

        live = set(cache)
        ttl = getattr(cache, "ttl", None)
        now = time.time()
        with self._lock:
            for session_id in list(self._sessions):
                if session_id in live:
                    continue
                stats = self._sessions.pop(session_id)
                # A session idle for at least its TTL went out on expiry; one
                # dropped while still recently used was pushed out by `maxsize`.
                if ttl is not None and now - stats.last_seen >= ttl:
                    self.expired_total += 1
                else:
                    self.evicted_total += 1
                logger.debug(f"Session registry: session {session_id} is gone")
            for session_id in live:
                if session_id not in self._sessions:
                    self.touch(session_id, estimated=True)

    def snapshot(self) -> dict[Hashable, SessionStats]:
        """A copy of what is known about every tracked session."""
        with self._lock:
            return {
                # `tools_used` is mutable and shared with the live entry
                # otherwise: a caller iterating a snapshot would see it grow.
                session_id: SessionStats(
                    **{**vars(stats), "tools_used": dict(stats.tools_used)}
                )
                for session_id, stats in self._sessions.items()
            }

    @property
    def uptime_s(self) -> float:
        """Seconds since this registry (i.e. this server process) started."""
        return time.time() - self.started_at


# The registry of the running server. A module-level singleton on purpose: it
# is read by `PyPowsyblTool.get_proxy()`, which every tool group inherits, so
# adding it as a constructor argument would change a signature that external
# plugins already build against.
SESSIONS = SessionRegistry()
