import io
import unittest
from datetime import date
from decimal import Decimal

from txledger.parse import Tx, ParseError, parse_line, parse_file
from txledger.accounts import Ledger, InsufficientFunds, UnknownAccount
from txledger.report import monthly_totals, top_payees, to_csv

LINES = """\
# opening balances
2026-01-01 bank->alice 100.00 salary
2026-01-01 bank->bob 50 salary
2026-01-05 alice->bob 12.50 lunch, split
2026-01-20 bob->carol 5.25
2026-02-02 alice->carol 30.00 rent

2026-02-15 alice->bob 7.75 coffee
"""


class TestParse(unittest.TestCase):
    def test_line(self):
        tx = parse_line("2026-01-05 alice->bob 12.50 lunch, split")
        self.assertEqual(tx, Tx(date(2026, 1, 5), "alice", "bob", Decimal("12.50"), "lunch, split"))
        self.assertEqual(parse_line("2026-01-20 bob->carol 5.25").memo, "")
        self.assertEqual(parse_line("  2026-01-20   bob->carol   5  ").amount, Decimal("5"))

    def test_bad_lines_name_the_line_number(self):
        for bad in ("2026-13-01 a->b 1", "2026-01-01 a-b 1", "2026-01-01 a->b -1",
                    "2026-01-01 a->a 1", "2026-01-01 a->b 1.234", "2026-01-01 a->b x"):
            with self.assertRaises(ParseError, msg=bad):
                parse_line(bad)
        with self.assertRaises(ParseError) as cm:
            parse_file(io.StringIO("2026-01-01 a->b 1\noops\n"))
        self.assertIn("line 2", str(cm.exception))

    def test_file_skips_comments_and_blanks(self):
        txs = parse_file(io.StringIO(LINES))
        self.assertEqual(len(txs), 6)
        self.assertEqual(txs[-1].memo, "coffee")


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.l = Ledger(sources={"bank"})
        for tx in parse_file(io.StringIO(LINES)):
            self.l.apply(tx)

    def test_balances(self):
        self.assertEqual(self.l.balance("alice"), Decimal("49.75"))
        self.assertEqual(self.l.balance("bob"), Decimal("65.00"))
        self.assertEqual(self.l.balance("carol"), Decimal("35.25"))
        self.assertEqual(self.l.balance("bank"), Decimal("-150.00"))  # sources may go negative

    def test_unknown_and_insufficient(self):
        with self.assertRaises(UnknownAccount):
            self.l.balance("dave")
        with self.assertRaises(InsufficientFunds):
            self.l.apply(parse_line("2026-03-01 carol->alice 100 too much"))
        self.assertEqual(self.l.balance("carol"), Decimal("35.25"))  # failed tx changes nothing
        with self.assertRaises(UnknownAccount):  # only sources may create money
            self.l.apply(parse_line("2026-03-01 dave->alice 1"))

    def test_history_in_order(self):
        h = self.l.history("bob")
        self.assertEqual([t.amount for t in h], [Decimal("50"), Decimal("12.50"), Decimal("5.25"),
                                                 Decimal("7.75")])


class TestReport(unittest.TestCase):
    def setUp(self):
        self.txs = parse_file(io.StringIO(LINES))

    def test_monthly_totals_ignore_sources(self):
        self.assertEqual(monthly_totals(self.txs, sources={"bank"}),
                         {"2026-01": Decimal("17.75"), "2026-02": Decimal("37.75")})

    def test_top_payees(self):
        self.assertEqual(top_payees(self.txs, "alice", n=2),
                         [("carol", Decimal("30.00")), ("bob", Decimal("20.25"))])
        self.assertEqual(top_payees(self.txs, "carol"), [])

    def test_csv(self):
        out = to_csv(self.txs[2:4])
        self.assertEqual(out, 'date,from,to,amount,memo\r\n'
                              '2026-01-05,alice,bob,12.50,"lunch, split"\r\n'
                              '2026-01-20,bob,carol,5.25,\r\n')


if __name__ == "__main__":
    unittest.main()
