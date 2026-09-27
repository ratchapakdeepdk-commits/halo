import re
_INT = re.compile(r"-?(0|[1-9]\d*)")
_FLT = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?")
def _fields(text):
    rows, row, f, i, q, quoted = [], [], [], 0, False, False
    while i < len(text):
        ch = text[i]
        if q:
            if ch == '"':
                if text[i+1:i+2] == '"': f.append('"'); i += 1
                else: q = False
            else: f.append(ch)
        elif ch == '"': q = quoted = True
        elif ch == ",": row.append(("".join(f), quoted)); f, quoted = [], False
        elif ch in "\r\n":
            if ch == "\r" and text[i+1:i+2] == "\n": i += 1
            row.append(("".join(f), quoted)); rows.append(row); row, f, quoted = [], [], False
        else: f.append(ch)
        i += 1
    if f or row or quoted: row.append(("".join(f), quoted)); rows.append(row)
    return rows
def _val(s, quoted):
    if quoted: return s
    if s == "": return None
    if s.lower() in ("true", "false"): return s.lower() == "true"
    if _INT.fullmatch(s): return int(s)
    if _FLT.fullmatch(s): return float(s)
    return s
def convert(text):
    rows = _fields(text)
    head = [h for h, _ in rows[0]]
    out = []
    for r in rows[1:]:
        if len(r) != len(head): raise ValueError(r)
        out.append({h: _val(s, q) for h, (s, q) in zip(head, r)})
    return out
