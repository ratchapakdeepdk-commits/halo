import unittest
from datetime import date, time
from decimal import Decimal

from tasklog.model import Entry
from tasklog.parse import ParseError, find_overlaps, parse_line, parse_text
from tasklog.query import select, totals, weekly
from tasklog.invoice import InvoiceLine, Rates, invoice
from tasklog.render import table, to_csv, to_markdown

LOG = """\
# week 10
2026-03-02 09:00-10:30 acme: kickoff call #client #billable
2026-03-02 10:30-12:00 acme: write spec #billable
2026-03-02 13:00-13:20 internal: email

2026-03-04 22:00-23:59 globex: deploy #billable #urgent
2026-03-09 08:05-08:50 acme: fix bug #billable
"""


class TestModel(unittest.TestCase):
    def test_entry(self):
        e = Entry(date(2026, 3, 2), time(9, 0), time(10, 30), "acme", "kickoff call",
                  ("client", "billable"))
        self.assertEqual(e.minutes, 90)
        self.assertTrue(e.has_tag("billable"))
        self.assertFalse(e.has_tag("urgent"))


class TestParse(unittest.TestCase):
    def test_line(self):
        e = parse_line("2026-03-02 09:00-10:30 acme: kickoff call #client #billable")
        self.assertEqual((e.project, e.text, e.tags), ("acme", "kickoff call", ("client", "billable")))
        self.assertEqual((e.start, e.end), (time(9, 0), time(10, 30)))
        e = parse_line("2026-03-02 13:00-13:20 internal: email")
        self.assertEqual((e.text, e.tags), ("email", ()))
        e = parse_line("2026-03-02 13:00-13:20 Internal:   Email  #Ops  ")
        self.assertEqual((e.project, e.text, e.tags), ("internal", "Email", ("ops",)))

    def test_bad_lines(self):
        for bad in ("2026-03-02 09:00-08:00 acme: backwards",
                    "2026-03-02 09:00-09:00 acme: empty",
                    "2026-03-02 9:00-10:00 acme: one-digit hour",
                    "2026-03-02 09:00-24:00 acme: no 24:00",
                    "2026-02-30 09:00-10:00 acme: no such day",
                    "2026-03-02 09:00-10:00 : no project",
                    "2026-03-02 09:00-10:00 acme no colon",
                    "2026-03-02 09:00-10:00 acme: #onlytags"):
            with self.assertRaises(ParseError, msg=bad):
                parse_line(bad)

    def test_text_reports_line_numbers(self):
        self.assertEqual(len(parse_text(LOG)), 5)
        with self.assertRaises(ParseError) as cm:
            parse_text("# c\n2026-03-02 09:00-10:00 a: x\n\nbroken\n")
        self.assertIn("line 4", str(cm.exception))

    def test_overlaps(self):
        es = parse_text(LOG + "2026-03-02 11:30-12:30 globex: call\n2026-03-02 12:00-13:00 x: y\n")
        pairs = find_overlaps(es)
        # pairs in start order; touching (10:30-12:00 then 12:00-13:00) is not an overlap
        self.assertEqual([(a.text, b.text) for a, b in pairs],
                         [("write spec", "call"), ("call", "y")])
        self.assertEqual(find_overlaps(parse_text(LOG)), [])


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.es = parse_text(LOG)

    def test_select(self):
        self.assertEqual(len(select(self.es, project="acme")), 3)
        self.assertEqual(len(select(self.es, tag="urgent")), 1)
        self.assertEqual(len(select(self.es, start=date(2026, 3, 3), end=date(2026, 3, 9))), 2)
        self.assertEqual(len(select(self.es, project="acme", tag="client")), 1)

    def test_totals_and_weekly(self):
        self.assertEqual(totals(self.es), {"acme": 225, "internal": 20, "globex": 119})
        self.assertEqual(list(totals(self.es)), ["acme", "globex", "internal"])  # by minutes desc
        self.assertEqual(totals(self.es, by="tag")["billable"], 344)
        self.assertEqual(weekly(self.es), {"2026-W10": {"acme": 180, "internal": 20, "globex": 119},
                                           "2026-W11": {"acme": 45}})


class TestInvoice(unittest.TestCase):
    def test_invoice_rounds_up_to_blocks(self):
        es = parse_text(LOG)
        rates = Rates({"acme": Decimal("80"), "globex": Decimal("120.50")}, block_minutes=15)
        lines = invoice(es, rates, tag="billable")
        self.assertEqual(lines[0], InvoiceLine(date(2026, 3, 2), "acme", "kickoff call", 90,
                                               Decimal("1.50"), Decimal("120.00")))
        # 119 min -> 120 min billed = 2.00 h; 45 min stays 0.75 h
        self.assertEqual([(l.project, l.hours, l.amount) for l in lines[2:]],
                         [("globex", Decimal("2.00"), Decimal("241.00")),
                          ("acme", Decimal("0.75"), Decimal("60.00"))])
        self.assertEqual(sum(l.amount for l in lines), Decimal("541.00"))

    def test_missing_rate(self):
        with self.assertRaises(KeyError):
            invoice(parse_text(LOG), Rates({"acme": Decimal("80")}), tag="billable")


class TestRender(unittest.TestCase):
    def setUp(self):
        self.es = parse_text(LOG)[:3]

    def test_table_aligns_columns(self):
        self.assertEqual(table(self.es).splitlines(), [
            "date        time         min  project   text",
            "2026-03-02  09:00-10:30   90  acme      kickoff call",
            "2026-03-02  10:30-12:00   90  acme      write spec",
            "2026-03-02  13:00-13:20   20  internal  email",
            "total                    200",
        ])

    def test_csv_and_markdown(self):
        self.assertEqual(to_csv(self.es[:1]), "date,start,end,minutes,project,text,tags\r\n"
                                              "2026-03-02,09:00,10:30,90,acme,kickoff call,client billable\r\n")
        md = to_markdown(self.es[2:])
        self.assertEqual(md, "| date | time | min | project | text |\n"
                             "|---|---|---:|---|---|\n"
                             "| 2026-03-02 | 13:00-13:20 | 20 | internal | email |\n")
        e = parse_line("2026-03-02 13:00-13:20 a: pipe | here")
        self.assertIn("pipe \\| here", to_markdown([e]))


if __name__ == "__main__":
    unittest.main()
