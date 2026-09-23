import unittest
from roman import to_roman, from_roman

class T(unittest.TestCase):
    def test_roundtrip(self):
        for n in range(1, 4000):
            self.assertEqual(from_roman(to_roman(n)), n)
    def test_values(self):
        self.assertEqual(to_roman(1994), "MCMXCIV")
        self.assertEqual(to_roman(3999), "MMMCMXCIX")
    def test_invalid(self):
        for s in ["IIII", "IC", "VX", "MMMM", "mcm", "", "IIV", "XM"]:
            with self.assertRaises(ValueError, msg=s):
                from_roman(s)
        for n in [0, 4000, -1, 2.5, True]:
            with self.assertRaises(ValueError, msg=repr(n)):
                to_roman(n)
