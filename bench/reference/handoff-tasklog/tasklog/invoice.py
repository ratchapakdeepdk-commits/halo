from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .query import select


@dataclass
class Rates:
    hourly: dict = field(default_factory=dict)
    block_minutes: int = 15


@dataclass(frozen=True)
class InvoiceLine:
    day: date
    project: str
    text: str
    minutes: int
    hours: Decimal
    amount: Decimal


def invoice(entries, rates: Rates, tag: str | None = None) -> list[InvoiceLine]:
    lines = []
    for e in sorted(select(entries, tag=tag), key=lambda e: (e.day, e.start)):
        rate = rates.hourly[e.project]
        b = rates.block_minutes
        billed = -(-e.minutes // b) * b
        hours = (Decimal(billed) / 60).quantize(Decimal("0.01"))
        lines.append(InvoiceLine(e.day, e.project, e.text, e.minutes, hours,
                                 (hours * Decimal(rate)).quantize(Decimal("0.01"))))
    return lines
