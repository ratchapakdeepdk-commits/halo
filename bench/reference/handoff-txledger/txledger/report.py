import csv
import io
from collections import defaultdict
from decimal import Decimal


def monthly_totals(txs, sources=()) -> dict:
    out = defaultdict(Decimal)
    for t in txs:
        if t.src not in sources:
            out[t.date.strftime("%Y-%m")] += t.amount
    return dict(out)


def top_payees(txs, payer: str, n: int = 3) -> list:
    tot = defaultdict(Decimal)
    for t in txs:
        if t.src == payer:
            tot[t.dst] += t.amount
    return sorted(tot.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def to_csv(txs) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "from", "to", "amount", "memo"])
    for t in txs:
        w.writerow([t.date.isoformat(), t.src, t.dst, str(t.amount), t.memo])
    return buf.getvalue()
