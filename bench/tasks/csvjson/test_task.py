import unittest
from csvjson import convert

class T(unittest.TestCase):
    def test_types(self):
        rows = convert("a,b,c,d,e\n1,2.5,true,,hi\n-7,1e3,FALSE,x, y \n")
        self.assertEqual(rows, [{"a": 1, "b": 2.5, "c": True, "d": None, "e": "hi"},
                                {"a": -7, "b": 1000.0, "c": False, "d": "x", "e": " y "}])
        self.assertIs(type(rows[0]["a"]), int)
        self.assertIs(type(rows[1]["b"]), float)
    def test_leading_zero_is_str(self):
        self.assertEqual(convert("zip,n\n007,0\n"), [{"zip": "007", "n": 0}])
    def test_quoted(self):
        rows = convert('name,note\n"Smith, J","said ""hi""\nthen left"\n"",""\n"42",x\n')
        self.assertEqual(rows[0], {"name": "Smith, J", "note": 'said "hi"\nthen left'})
        self.assertEqual(rows[1], {"name": "", "note": ""})
        self.assertEqual(rows[2], {"name": "42", "note": "x"})
    def test_ragged(self):
        with self.assertRaises(ValueError):
            convert("a,b\n1,2,3\n")
    def test_header_only(self):
        self.assertEqual(convert("a,b\n"), [])

if __name__ == "__main__":
    unittest.main()
