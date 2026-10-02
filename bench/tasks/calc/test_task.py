import math
import unittest
from calc import Calc, CalcError


class T(unittest.TestCase):
    def setUp(self):
        self.c = Calc()
        self.ev = self.c.evaluate

    def test_arithmetic(self):
        self.assertEqual(self.ev("1 + 2 * 3"), 7)
        self.assertEqual(self.ev("(1 + 2) * 3"), 9)
        self.assertEqual(self.ev("10 - 4 - 3"), 3)
        self.assertEqual(self.ev("7 // 2 + 7 % 3"), 4)
        r = self.ev("7 / 2")
        self.assertEqual(r, 3.5)
        r = self.ev("8 / 4")
        self.assertIsInstance(r, float)
        self.assertIsInstance(self.ev("2 * 3"), int)
        self.assertEqual(self.ev("-7 // 2"), -4)

    def test_numbers(self):
        self.assertEqual(self.ev(".5 + 1e3"), 1000.5)
        self.assertAlmostEqual(self.ev("2.5E-2"), 0.025)
        self.assertIsInstance(self.ev("1e3"), float)

    def test_power_and_unary(self):
        self.assertEqual(self.ev("-2**2"), -4)
        self.assertEqual(self.ev("2**3**2"), 512)
        self.assertEqual(self.ev("2**-1"), 0.5)
        self.assertEqual(self.ev("--3"), 3)
        self.assertEqual(self.ev("-(2+3)*2"), -10)
        self.assertEqual(self.ev("+4 - -1"), 5)

    def test_variables(self):
        self.assertEqual(self.ev("x = 4"), 4)
        self.assertEqual(self.ev("y = x * 2 + 1"), 9)
        self.assertEqual(self.ev("x_2 = y - x"), 5)
        self.assertEqual(self.ev("x_2 ** 2"), 25)
        with self.assertRaises(CalcError):
            Calc().evaluate("x")

    def test_functions_constants(self):
        self.assertAlmostEqual(self.ev("sqrt(16) + abs(-2)"), 6.0)
        self.assertEqual(self.ev("max(1, 7, 3) - min(4, 2)"), 5)
        self.assertEqual(self.ev("max(5)"), 5)
        self.assertEqual(self.ev("round(2.675, 2)"), round(2.675, 2))
        self.assertEqual(self.ev("round(2.5)"), 2)
        self.assertAlmostEqual(self.ev("2 * pi"), 2 * math.pi)
        self.assertAlmostEqual(self.ev("e ** 1"), math.e)
        self.assertEqual(self.ev("max(1, 2 * (3 + 1))"), 8)

    def test_runtime_errors(self):
        for bad in ["1 / 0", "1 // 0", "5 % 0", "sqrt(-1)", "foo(1)", "nope + 1",
                    "max()", "abs(1, 2)", "round(1, 2, 3)", "sqrt()", "pi = 3", "e = 1"]:
            with self.subTest(bad=bad):
                with self.assertRaises(CalcError):
                    self.ev(bad)
        self.assertTrue(issubclass(CalcError, ValueError))

    def assertSyntax(self, line, col):
        with self.assertRaises(CalcError) as cm:
            self.ev(line)
        self.assertIn(f"col {col}", str(cm.exception), line)

    def test_syntax_errors(self):
        self.assertSyntax("1 + $", 5)
        self.assertSyntax("1 +", 4)
        self.assertSyntax("(1 + 2", 7)
        self.assertSyntax("1 2", 3)
        self.assertSyntax("", 1)
        self.assertSyntax("   ", 4)
        self.assertSyntax("2 * )", 5)
        self.assertSyntax("max(1,)", 7)
        self.assertSyntax("3 = 4", 3)
        self.assertSyntax("x = ", 5)
        self.assertSyntax("1 +* 2", 4)

    def test_no_python_eval(self):
        import calc, inspect, re
        src = inspect.getsource(calc)
        hit = re.search(r"(?<![\w.])(eval|exec|compile)\(|^\s*(import|from) ast\b", src, re.M)
        self.assertIsNone(hit, "must not use eval/exec/compile/ast")

if __name__ == "__main__":
    unittest.main()
