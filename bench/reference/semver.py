import re
_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?")
def parse(v):
    m = _RE.fullmatch(v)
    if not m: raise ValueError(v)
    return (int(m[1]), int(m[2]), int(m[3]), tuple(m[4].split(".")) if m[4] else ())
def _key(v):
    a, b, c, pre = parse(v)
    ids = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in pre)
    return (a, b, c, 1 if not pre else 0, ids)
def compare(a, b):
    ka, kb = _key(a), _key(b)
    return (ka > kb) - (ka < kb)
def bump(v, part):
    a, b, c, _ = parse(v)
    return {"major": f"{a+1}.0.0", "minor": f"{a}.{b+1}.0", "patch": f"{a}.{b}.{c+1}"}[part]
