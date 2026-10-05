import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\w+)->(\w+)\s+(\d+(?:\.\d{1,2})?)(?:\s+(.*))?$")


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Tx:
    date: date
    src: str
    dst: str
    amount: Decimal
    memo: str = ""


def parse_line(line: str) -> Tx:
    m = LINE_RE.match(line.strip())
    if not m:
        raise ParseError(f"cannot parse: {line.strip()!r}")
    try:
        d = date.fromisoformat(m.group(1))
    except ValueError:
        raise ParseError(f"bad date: {m.group(1)}") from None
    if m.group(2) == m.group(3):
        raise ParseError("transfer to self")
    amount = Decimal(m.group(4))
    if amount <= 0:
        raise ParseError("amount must be positive")
    return Tx(d, m.group(2), m.group(3), amount, (m.group(5) or "").strip())


def parse_file(fh) -> list[Tx]:
    out = []
    for n, line in enumerate(fh, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            out.append(parse_line(line))
        except ParseError as e:
            raise ParseError(f"line {n}: {e}") from None
    return out
