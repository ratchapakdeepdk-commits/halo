import unittest
from datetime import datetime, timezone
from cron import parse, next_run

D = datetime


class T(unittest.TestCase):
    def test_parse_basic(self):
        p = parse("*/15 9-17 * * MON-fri")
        self.assertEqual(p["minute"], [0, 15, 30, 45])
        self.assertEqual(p["hour"], list(range(9, 18)))
        self.assertEqual(p["dom"], list(range(1, 32)))
        self.assertEqual(p["month"], list(range(1, 13)))
        self.assertEqual(p["dow"], [1, 2, 3, 4, 5])

    def test_parse_lists_steps(self):
        p = parse("5,10-20/5,58 0 1,15 jan,Jul-AUG 7")
        self.assertEqual(p["minute"], [5, 10, 15, 20, 58])
        self.assertEqual(p["month"], [1, 7, 8])
        self.assertEqual(p["dow"], [0])
        self.assertEqual(parse("50/4 * * * *")["minute"], [50, 54, 58])
        self.assertEqual(parse("0 0 * * 5-7")["dow"], [0, 5, 6])

    def test_macros(self):
        self.assertEqual(parse("@daily"), parse("0 0 * * *"))
        self.assertEqual(parse("@midnight"), parse("0 0 * * *"))
        self.assertEqual(parse("@annually"), parse("0 0 1 1 *"))
        self.assertEqual(parse("@weekly")["dow"], [0])
        self.assertEqual(next_run("@hourly", D(2026, 3, 1, 10, 0)), D(2026, 3, 1, 11, 0))
        self.assertEqual(next_run("@monthly", D(2026, 12, 15, 8, 0)), D(2027, 1, 1, 0, 0))

    def test_errors(self):
        for bad in ["* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *",
                    "* * * 13 *", "* * * * 8", "*/0 * * * *", "10-5 * * * *", "a * * * *",
                    "1,,2 * * * *", "* * * FOO *", "@often", "1-2-3 * * * *", "", "-1 * * * *"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse(bad)

    def test_next_simple(self):
        self.assertEqual(next_run("30 14 * * *", D(2026, 5, 1, 14, 29, 59)), D(2026, 5, 1, 14, 30))
        self.assertEqual(next_run("30 14 * * *", D(2026, 5, 1, 14, 30)), D(2026, 5, 2, 14, 30))
        self.assertEqual(next_run("* * * * *", D(2026, 5, 1, 23, 59, 30, 5)), D(2026, 5, 2, 0, 0))
        self.assertEqual(next_run("0 0 1 1 *", D(2026, 1, 1, 0, 0)), D(2027, 1, 1, 0, 0))

    def test_month_end_and_leap(self):
        self.assertEqual(next_run("0 12 31 * *", D(2026, 4, 1)), D(2026, 5, 31, 12, 0))
        self.assertEqual(next_run("0 0 29 2 *", D(2026, 3, 1)), D(2028, 2, 29, 0, 0))
        with self.assertRaises(ValueError):
            next_run("0 0 30 2 *", D(2026, 1, 1))

    def test_dom_dow_or(self):
        # 13th of the month OR any Friday (2026-03-06 is a Friday)
        self.assertEqual(next_run("0 0 13 * 5", D(2026, 3, 1)), D(2026, 3, 6, 0, 0))
        self.assertEqual(next_run("0 0 13 * 5", D(2026, 3, 6, 0, 0)), D(2026, 3, 13, 0, 0))
        # dow starts with * -> AND: 13th that is any day
        self.assertEqual(next_run("0 0 13 * *", D(2026, 3, 1)), D(2026, 3, 13, 0, 0))
        # dom starts with * -> AND: Fridays only, even with a step on dom
        self.assertEqual(next_run("0 0 */2 * FRI", D(2026, 3, 1)), D(2026, 3, 13, 0, 0))

    def test_weekday_and_tz(self):
        tz = timezone.utc
        got = next_run("15 9 * * mon-fri", D(2026, 10, 2, 9, 15, tzinfo=tz))  # Friday
        self.assertEqual(got, D(2026, 10, 5, 9, 15, tzinfo=tz))
        self.assertIs(got.tzinfo, tz)


if __name__ == "__main__":
    unittest.main()
