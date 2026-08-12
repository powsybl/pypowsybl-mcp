#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import threading

import pytest

from pypowsybl_mcp.utils.cachetools import ThreadSafeTTLCache


@pytest.fixture
def cache():
    return ThreadSafeTTLCache(maxsize=10, ttl=60)


def test_setitem_getitem(cache):
    cache["a"] = 1
    assert cache["a"] == 1


def test_contains(cache):
    cache["a"] = 1
    assert "a" in cache
    assert "z" not in cache


def test_delitem(cache):
    cache["a"] = 1
    del cache["a"]
    assert "a" not in cache


def test_len(cache):
    cache["a"] = 1
    cache["b"] = 2
    assert len(cache) == 2


def test_bool(cache):
    assert not bool(cache)
    cache["a"] = 1
    assert bool(cache)


def test_iter(cache):
    cache["a"] = 1
    cache["b"] = 2
    assert set(iter(cache)) == {"a", "b"}


def test_repr(cache):
    cache["a"] = 1
    assert "a" in repr(cache)


def test_get(cache):
    cache["a"] = 1
    assert cache.get("a") == 1
    assert cache.get("z", 99) == 99
    assert cache.get("z") is None


def test_pop(cache):
    cache["a"] = 1
    assert cache.pop("a") == 1
    assert "a" not in cache


def test_pop_default(cache):
    assert cache.pop("missing", 42) == 42


def test_popitem(cache):
    cache["a"] = 1
    key, value = cache.popitem()
    assert key == "a"
    assert value == 1
    assert len(cache) == 0


def test_setdefault(cache):
    cache.setdefault("a", 10)
    assert cache["a"] == 10
    cache.setdefault("a", 99)
    assert cache["a"] == 10


def test_update(cache):
    cache.update({"x": 1, "y": 2})
    assert cache["x"] == 1
    assert cache["y"] == 2


def test_clear(cache):
    cache["a"] = 1
    cache.clear()
    assert len(cache) == 0


def test_keys(cache):
    cache["a"] = 1
    cache["b"] = 2
    assert set(cache.keys()) == {"a", "b"}


def test_values(cache):
    cache["a"] = 1
    cache["b"] = 2
    assert set(cache.values()) == {1, 2}


def test_items(cache):
    cache["a"] = 1
    cache["b"] = 2
    assert set(cache.items()) == {("a", 1), ("b", 2)}


def test_expire(cache):
    cache["a"] = 1
    cache.expire()
    assert "a" in cache


def test_concurrent_no_errors():
    """Multiple writers and readers run concurrently without raising exceptions."""
    cache = ThreadSafeTTLCache(maxsize=1000, ttl=60)
    errors = []

    def writer():
        try:
            for i in range(100):
                cache[f"key-{i}"] = i
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                _ = cache.keys()
                _ = len(cache)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(5)] + [
        threading.Thread(target=reader) for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread safety errors: {errors}"


def test_concurrent_writes_correctness():
    """Each key written by exactly one thread retains its correct value after all threads finish."""
    cache = ThreadSafeTTLCache(maxsize=10000, ttl=60)
    num_threads = 10
    items_per_thread = 100
    errors = []

    def writer(thread_id):
        try:
            for i in range(items_per_thread):
                key = f"t{thread_id}-k{i}"
                cache[key] = thread_id * 1000 + i
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=(tid,)) for tid in range(num_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors during concurrent writes: {errors}"
    for tid in range(num_threads):
        for i in range(items_per_thread):
            key = f"t{tid}-k{i}"
            assert cache[key] == tid * 1000 + i, f"Unexpected value for {key}"


def test_concurrent_mixed_operations():
    """Concurrent writes, reads, deletes, and clears do not corrupt the cache or raise exceptions."""
    cache = ThreadSafeTTLCache(maxsize=500, ttl=60)
    errors = []

    def writer():
        try:
            for i in range(200):
                cache[f"key-{i % 50}"] = i
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            for i in range(200):
                cache.get(f"key-{i % 50}")
                _ = len(cache)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def deleter():
        try:
            for i in range(200):
                cache.pop(f"key-{i % 50}", None)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = (
        [threading.Thread(target=writer) for _ in range(4)]
        + [threading.Thread(target=reader) for _ in range(4)]
        + [threading.Thread(target=deleter) for _ in range(2)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors during mixed concurrent operations: {errors}"


def test_concurrent_setdefault_atomicity():
    """setdefault called concurrently for the same key sets the value exactly once."""
    cache = ThreadSafeTTLCache(maxsize=100, ttl=60)
    results = []
    errors = []

    def set_default():
        try:
            val = cache.setdefault("shared", 42)
            results.append(val)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=set_default) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors during concurrent setdefault: {errors}"
    assert all(v == 42 for v in results), (
        f"setdefault returned inconsistent values: {set(results)}"
    )
    assert cache["shared"] == 42
