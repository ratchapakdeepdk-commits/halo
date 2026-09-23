import re
from collections import Counter
from datetime import datetime
_RE = re.compile(r'(\S+) - (\S+) \[([^\]]+)\] "((?:[^"\\]|\\.)*)" (\d{3}) (\d+|-) "((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"')
def _u(s): return re.sub(r'\\(.)', r'\1', s)
def parse_line(line):
    m = _RE.fullmatch(line.strip())
    if not m: return None
    ip, user, t, req, st, by, ref, ua = m.groups()
    parts = _u(req).split(" ")
    method, path, proto = parts if len(parts) == 3 else (None, None, None)
    return {"ip": ip, "user": None if user == "-" else user,
            "time": datetime.strptime(t, "%d/%b/%Y:%H:%M:%S %z"), "method": method,
            "path": path, "protocol": proto, "status": int(st),
            "bytes": 0 if by == "-" else int(by), "referer": None if ref == "-" else _u(ref),
            "user_agent": _u(ua)}
def summarize(lines):
    rs = [r for r in map(parse_line, lines) if r]
    paths = Counter(r["path"] for r in rs if r["path"])
    return {"total": len(rs), "by_status": dict(Counter(r["status"] for r in rs)),
            "top_paths": sorted(paths.items(), key=lambda x: (-x[1], x[0]))[:3]}
