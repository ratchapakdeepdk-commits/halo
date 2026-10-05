import csv
import io

HEAD = ("date", "time", "min", "project", "text")


def _rows(entries):
    return [(e.day.isoformat(), f"{e.start:%H:%M}-{e.end:%H:%M}", str(e.minutes), e.project, e.text)
            for e in entries]


def table(entries) -> str:
    rows = _rows(entries)
    total = str(sum(e.minutes for e in entries))
    w = [max(len(r[i]) for r in [HEAD, *rows]) for i in range(4)]
    w[2] = max(w[2], len(total))
    fmt = lambda r: (f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:>{w[2]}}  "
                     f"{r[3]:<{w[3]}}  {r[4]}").rstrip()
    lines = [fmt(HEAD), *map(fmt, rows)]
    lines.append(f"{'total':<{w[0] + w[1] + 4}}{total:>{w[2]}}")
    return "\n".join(lines) + "\n"


def to_csv(entries) -> str:
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["date", "start", "end", "minutes", "project", "text", "tags"])
    for e in entries:
        wr.writerow([e.day.isoformat(), f"{e.start:%H:%M}", f"{e.end:%H:%M}", e.minutes,
                     e.project, e.text, " ".join(e.tags)])
    return buf.getvalue()


def to_markdown(entries) -> str:
    cell = lambda s: s.replace("|", "\\|")
    out = ["| " + " | ".join(HEAD) + " |", "|---|---|---:|---|---|"]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in _rows(entries)]
    return "\n".join(out) + "\n"
