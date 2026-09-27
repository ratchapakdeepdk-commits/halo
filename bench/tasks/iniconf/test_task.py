import unittest
from iniconf import load

INI = """
root = /srv
debug = off

[paths]
; comment
data = ${root}/data
logs: ${data}/logs
cost = $$5

[server]
Port = 8080
host = ${paths:data}
motd = hello
  world
debug = YES

[paths]
extra = 1
"""

class T(unittest.TestCase):
    def test_load(self):
        c = load(INI)
        self.assertEqual(c["DEFAULT"], {"root": "/srv", "debug": False})
        self.assertEqual(c["paths"], {"root": "/srv", "debug": False, "data": "/srv/data",
                                      "logs": "/srv/data/logs", "cost": "$5", "extra": 1})
        self.assertEqual(c["server"]["port"], 8080)
        self.assertEqual(c["server"]["host"], "/srv/data")
        self.assertEqual(c["server"]["motd"], "hello\nworld")
        self.assertIs(c["server"]["debug"], True)
    def test_no_default(self):
        self.assertEqual(load("[a]\nx=1\n# hi\n"), {"a": {"x": 1}})
    def test_errors(self):
        for bad in ["[a]\nx=${y}\n", "[a]\nx=${y}\ny=${x}\n", "[a]\nx=${b:z}\n", "[a]\njunk line\n"]:
            with self.assertRaises(ValueError, msg=bad):
                load(bad)

if __name__ == "__main__":
    unittest.main()
