import unittest
from duration import parse_duration as p

class T(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(p("1h30m"), 5400)
        self.assertEqual(p("45s"), 45)
        self.assertEqual(p("2d3h"), 2*86400 + 3*3600)
        self.assertEqual(p("1h 5m 10s"), 3910)
        self.assertEqual(p("90M"), 5400)
        self.assertEqual(p("120"), 120)
    def test_bad(self):
        for s in ["", "  ", "5x", "1m1h", "1h1h", "-5s", "h", "1.5h", "abc"]:
            with self.assertRaises(ValueError, msg=s):
                p(s)
