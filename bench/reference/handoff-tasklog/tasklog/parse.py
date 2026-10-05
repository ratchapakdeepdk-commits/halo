import re
from datetime import date, time

from .model import Entry

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})-(\d{2}):(\d{2}) ([^:\s][^:]*?)\s*:\s*(.*)$")


class ParseError(ValueError):
    pass


def parse_line(line: str) -> Entry:
    m = LINE_RE.match(line.strip())
    if not m:
        raise ParseError(f"cannot parse: {line.strip()!r}")
    try:
        day = date.fromisoformat(m.group(1))
        start = time(int(m.group(2)), int(m.group(3)))
        end = time(int(m.group(4)), int(m.group(5)))
    except ValueError as e:
        raise ParseError(str(e)) from None
    if end <= start:
        raise ParseError("end must be after start")
    words = m.group(7).split()
    tags = []
    while words and words[-1].startswith("#") and len(words[-1]) > 1:
        tags.insert(0, words.pop()[1:].lower())
    text = " ".join(words)
    if not text:
        raise ParseError("missing text")
    return Entry(day, start, end, m.group(6).strip().lower(), text, tuple(tags))


def parse_text(text: str) -> list[Entry]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            out.append(parse_line(line))
        except ParseError as e:
            raise ParseError(f"line {n}: {e}") from None
    return out


def find_overlaps(entries) -> list[tuple[Entry, Entry]]:
    es = sorted(entries, key=lambda e: (e.day, e.start, e.end))
    pairs = []
    for i, a in enumerate(es):
        for b in es[i + 1:]:
            if b.day != a.day or b.start >= a.end:
                break
            pairs.append((a, b))
    return pairs
