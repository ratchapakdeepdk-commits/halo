import unittest
from semver import parse, compare, bump

class T(unittest.TestCase):
    def test_order(self):
        seq = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
               "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        for a, b in zip(seq, seq[1:]):
            self.assertEqual(compare(a, b), -1, (a, b))
            self.assertEqual(compare(b, a), 1, (b, a))
        self.assertEqual(compare("1.0.0+build.1", "1.0.0+x"), 0)
        self.assertEqual(compare("2.0.0", "10.0.0"), -1)
    def test_invalid(self):
        for v in ["1.0", "01.0.0", "1.0.0-01", "1.0.0-", "a.b.c", "1.0.0+", "1.0.0-al..pha"]:
            with self.assertRaises(ValueError, msg=v):
                parse(v)
    def test_bump(self):
        self.assertEqual(bump("1.2.3-rc.1+b5", "patch"), "1.2.4")
        self.assertEqual(bump("1.2.3", "minor"), "1.3.0")
        self.assertEqual(bump("1.2.3", "major"), "2.0.0")
