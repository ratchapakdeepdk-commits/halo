import unittest
from lru import LRUCache

class T(unittest.TestCase):
    def test_evict(self):
        c = LRUCache(2); c.put("a", 1); c.put("b", 2); c.get("a"); c.put("c", 3)
        self.assertNotIn("b", c); self.assertEqual(c.get("a"), 1); self.assertEqual(len(c), 2)
    def test_put_refreshes(self):
        c = LRUCache(2); c.put("a", 1); c.put("b", 2); c.put("a", 9); c.put("c", 3)
        self.assertEqual(c.get("a"), 9); self.assertIsNone(c.get("b"))
    def test_contains_no_refresh(self):
        c = LRUCache(2); c.put("a", 1); c.put("b", 2); "a" in c; c.put("c", 3)
        self.assertNotIn("a", c)
    def test_default(self):
        self.assertEqual(LRUCache(1).get("x", 7), 7)
    def test_bad(self):
        with self.assertRaises(ValueError):
            LRUCache(0)
