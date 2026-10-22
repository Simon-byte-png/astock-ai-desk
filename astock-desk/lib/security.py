"""Bounded, process-local admission control for the single-process HTTP service."""
import threading
import time
from collections import OrderedDict, deque


class RateLimiter:
    def __init__(self, capacity=2048):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, key, limit, window):
        now = time.monotonic()
        with self.lock:
            events = self.entries.get(key)
            if events is None:
                if len(self.entries) >= self.capacity:
                    # Do not evict active limits: that would let churn bypass them.
                    for old_key, (old_events, old_window) in list(self.entries.items()):
                        if not old_events or old_events[-1] <= now - old_window:
                            del self.entries[old_key]
                if len(self.entries) >= self.capacity:
                    return False
                events = deque()
                self.entries[key] = (events, window)
            else:
                events = events[0]
            while events and events[0] <= now - window:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True
