"""Operation-local readback cache protected by an active repository read pin.

Only physical bytes read during this pin may be reused. Publication verification
continues to use ordinary get_durable, which always bypasses earlier caches.
"""

from collections import OrderedDict


class PinnedObjectReader:
    def __init__(self, backend, snapshot, *, max_bytes=64 * 1024**2):
        self.backend, self.snapshot = backend, snapshot
        self.max_bytes, self.size = max_bytes, 0
        self.cache = OrderedDict()

    def get_durable(self, oid):
        self.snapshot.check_live()
        if oid in self.cache:
            self.cache.move_to_end(oid)
            return self.cache[oid]
        value = self.backend.get(oid)
        if len(value) <= self.max_bytes:
            while self.cache and self.size + len(value) > self.max_bytes:
                _, old = self.cache.popitem(last=False)
                self.size -= len(old)
            self.cache[oid] = value
            self.size += len(value)
        return value
