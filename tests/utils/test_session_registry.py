#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import time

import pytest
from cachetools import TTLCache

from pypowsybl_mcp.utils.session_registry import SessionRegistry


@pytest.fixture
def registry():
    return SessionRegistry()


def test_touch_creates_then_updates(registry):
    first = registry.touch("s1")
    created_at = first.created_at
    time.sleep(0.01)
    second = registry.touch("s1")

    assert first is second
    assert second.created_at == created_at
    assert second.last_seen > created_at


def test_record_call_counts_tools_and_errors(registry):
    registry.record_call("s1", "run_loadflow")
    registry.record_call("s1", "run_loadflow", error=True)
    registry.record_call("s1", "get_network_info")

    stats = registry.snapshot()["s1"]
    assert stats.tool_calls == 3
    assert stats.errors == 1
    assert stats.last_tool == "get_network_info"
    assert stats.tools_used == {"run_loadflow": 2, "get_network_info": 1}


def test_record_call_without_session_is_ignored(registry):
    registry.record_call(None, "run_loadflow")
    assert registry.snapshot() == {}


def test_reconcile_counts_a_capacity_eviction(registry):
    cache = TTLCache(maxsize=1, ttl=1000)
    cache["s1"] = object()
    registry.touch("s1")
    cache["s2"] = object()  # pushes s1 out: recently used, so not an expiry

    registry.reconcile(cache)

    assert set(registry.snapshot()) == {"s2"}
    assert (registry.expired_total, registry.evicted_total) == (0, 1)


def test_reconcile_counts_an_expiry(registry):
    clock = [0.0]
    cache = TTLCache(maxsize=10, ttl=10, timer=lambda: clock[0])
    cache["s1"] = object()
    registry.touch("s1")
    clock[0] = 11
    # The registry's own clock has to move too: expiry is inferred from how long
    # the session went unused, which is measured in wall-clock seconds.
    registry.snapshot()["s1"]
    registry._sessions["s1"].last_seen = time.time() - 11
    registry._sessions["s1"].created_at = time.time() - 11

    registry.reconcile(cache)

    assert registry.snapshot() == {}
    assert (registry.expired_total, registry.evicted_total) == (1, 0)


def test_reconcile_adopts_untracked_sessions(registry):
    cache = TTLCache(maxsize=10, ttl=1000)
    cache["s1"] = object()

    registry.reconcile(cache)

    stats = registry.snapshot()["s1"]
    assert stats.created_at_estimated is True


def test_forget_is_not_an_eviction(registry):
    registry.touch("s1")
    registry.forget("s1")

    assert registry.snapshot() == {}
    assert (registry.expired_total, registry.evicted_total) == (0, 0)


def test_snapshot_is_a_copy(registry):
    registry.record_call("s1", "run_loadflow")
    snapshot = registry.snapshot()
    snapshot["s1"].tool_calls = 99

    assert registry.snapshot()["s1"].tool_calls == 1
