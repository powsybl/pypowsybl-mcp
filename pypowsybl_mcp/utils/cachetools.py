#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import threading

from cachetools import TTLCache


class ThreadSafeTTLCache(TTLCache):
    """A TTLCache subclass that protects all read and write operations with a threading.Lock."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #  reentrant threading.RLock to prevent deadlocks during concurrent access
        self._lock = threading.RLock()

    def __getitem__(self, key):
        with self._lock:
            return TTLCache.__getitem__(self, key)

    def __setitem__(self, key, value):
        with self._lock:
            TTLCache.__setitem__(self, key, value)

    def __delitem__(self, key):
        with self._lock:
            TTLCache.__delitem__(self, key)

    def __contains__(self, key):
        with self._lock:
            return TTLCache.__contains__(self, key)

    def __len__(self):
        with self._lock:
            return TTLCache.__len__(self)

    def __iter__(self):
        # Materialise the iterator while the lock is held so callers get a snapshot.
        with self._lock:
            return iter(list(TTLCache.__iter__(self)))

    def __bool__(self):
        with self._lock:
            return TTLCache.__len__(self) > 0

    def __repr__(self):
        with self._lock:
            return TTLCache.__repr__(self)

    def get(self, key, default=None):
        with self._lock:
            return TTLCache.get(self, key, default)

    def pop(self, key, *args):
        with self._lock:
            return TTLCache.pop(self, key, *args)

    def popitem(self):
        with self._lock:
            return TTLCache.popitem(self)

    def setdefault(self, key, default=None):
        with self._lock:
            return TTLCache.setdefault(self, key, default)

    def update(self, *args, **kwargs):
        with self._lock:
            TTLCache.update(self, *args, **kwargs)

    def clear(self):
        with self._lock:
            TTLCache.clear(self)

    def expire(self, time=None):
        with self._lock:
            TTLCache.expire(self, time)

    def keys(self):
        with self._lock:
            return list(TTLCache.keys(self))

    def values(self):
        with self._lock:
            return list(TTLCache.values(self))

    def items(self):
        with self._lock:
            return list(TTLCache.items(self))
