from collections import OrderedDict
class LRUCache:
    def __init__(self, capacity):
        if capacity < 1: raise ValueError(capacity)
        self.cap, self.d = capacity, OrderedDict()
    def get(self, key, default=None):
        if key not in self.d: return default
        self.d.move_to_end(key); return self.d[key]
    def put(self, key, value):
        self.d[key] = value; self.d.move_to_end(key)
        if len(self.d) > self.cap: self.d.popitem(last=False)
    def __len__(self): return len(self.d)
    def __contains__(self, key): return key in self.d
