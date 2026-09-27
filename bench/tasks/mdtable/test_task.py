import unittest
from mdtable import parse, render

MD = """Some intro text.

| Name | Qty | Note |
|:-----|----:|:----:|
| apple | 3 | red \\| green |
pear | 10
| fig | 1 | ok | extra |

after
"""

class T(unittest.TestCase):
    def test_parse(self):
        h, a, r = parse(MD)
        self.assertEqual(h, ["Name", "Qty", "Note"])
        self.assertEqual(a, ["left", "right", "center"])
        self.assertEqual(r, [["apple", "3", "red | green"], ["pear", "10", ""], ["fig", "1", "ok"]])
    def test_none_align(self):
        self.assertEqual(parse("a|b\n---|---\n1|2")[1], ["none", "none"])
    def test_bad_delimiter(self):
        with self.assertRaises(ValueError):
            parse("| a | b |\n| x | y |\n")
        with self.assertRaises(ValueError):
            parse("| a | b |\n|---|\n")
    def test_render(self):
        out = render(["id", "name"], ["right", "none"], [["1", "a|b"], ["22", ""]])
        self.assertEqual(out, "|  id | name |\n| --: | ---- |\n|   1 | a\\|b |\n|  22 |      |")
        out = render(["x"], ["center"], [["hello"]])
        self.assertEqual(out, "|   x   |\n| :---: |\n| hello |")
    def test_roundtrip(self):
        h, a, r = parse(MD)
        self.assertEqual(parse(render(h, a, r)), (h, a, r))

if __name__ == "__main__":
    unittest.main()
