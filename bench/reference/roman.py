import re
_V = [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),(50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
def to_roman(n):
    if type(n) is not int or not 1 <= n <= 3999: raise ValueError(n)
    out = ""
    for v, s in _V:
        while n >= v: out += s; n -= v
    return out
_RE = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})")
def from_roman(s):
    if not s or not _RE.fullmatch(s): raise ValueError(s)
    i = n = 0
    for v, sym in _V:
        while s.startswith(sym, i): n += v; i += len(sym)
    return n
