import re
_RE = re.compile(r"\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*", re.I)
def parse_duration(s):
    if re.fullmatch(r"\s*\d+\s*", s or ""):
        return int(s)
    m = _RE.fullmatch(s or "")
    if not m or not any(m.groups()):
        raise ValueError(s)
    d, h, mi, se = (int(g or 0) for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se
