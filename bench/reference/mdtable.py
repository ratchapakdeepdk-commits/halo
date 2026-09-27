import re
_DELIM = re.compile(r":?-+:?")
def _cells(line):
    s = line.strip()
    if s.startswith("|"): s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"): s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]
def _align(d):
    if not _DELIM.fullmatch(d): raise ValueError(d)
    return {(True, True): "center", (True, False): "left", (False, True): "right"}.get((d[0] == ":", d[-1] == ":"), "none")
def parse(md):
    lines = md.splitlines()
    i = next(k for k, l in enumerate(lines) if "|" in l)
    head = _cells(lines[i])
    if i + 1 >= len(lines): raise ValueError("no delimiter")
    aligns = [_align(d) for d in _cells(lines[i + 1])]
    if len(aligns) != len(head): raise ValueError("column count")
    rows = []
    for l in lines[i + 2:]:
        if not l.strip(): break
        c = _cells(l)[:len(head)]
        rows.append(c + [""] * (len(head) - len(c)))
    return head, aligns, rows
def render(headers, aligns, rows):
    esc = [[c.replace("|", "\\|") for c in r] for r in [headers] + rows]
    w = [max(3, *(len(r[j]) for r in esc)) for j in range(len(headers))]
    def just(c, j):
        a = aligns[j]
        return c.rjust(w[j]) if a == "right" else c.center(w[j]) if a == "center" else c.ljust(w[j])
    line = lambda r: "| " + " | ".join(just(c, j) for j, c in enumerate(r)) + " |"
    d = {"left": lambda n: ":" + "-" * (n - 1), "right": lambda n: "-" * (n - 1) + ":",
         "center": lambda n: ":" + "-" * (n - 2) + ":", "none": lambda n: "-" * n}
    delim = "| " + " | ".join(d[aligns[j]](w[j]) for j in range(len(headers))) + " |"
    return "\n".join([line(esc[0]), delim] + [line(r) for r in esc[1:]])
