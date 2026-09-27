import re
_REF = re.compile(r"\$\$|\$\{([^}]*)\}")
def load(text):
    raw, sec, last = {}, "DEFAULT", None
    for line in text.splitlines():
        s = line.strip()
        if not s: last = None; continue
        if s[0] in ";#": continue
        if line[0] in " \t" and last:
            raw[sec][last] += "\n" + s; continue
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1].strip(); raw.setdefault(sec, {}); last = None; continue
        m = re.match(r"([^=:]+)[=:](.*)", s)
        if not m: raise ValueError(line)
        last = m[1].strip().lower(); raw.setdefault(sec, {})[last] = m[2].strip()
    d = raw.get("DEFAULT", {})
    for k, v in raw.items():
        if k != "DEFAULT": raw[k] = {**d, **v}
    def get(sec, key, stack):
        if (sec, key) in stack: raise ValueError("cycle")
        src = raw.get(sec, {})
        if key not in src:
            if key in d: sec, src = "DEFAULT", d
            else: raise ValueError(f"missing {sec}:{key}")
        return expand(sec, src[key], stack | {(sec, key)})
    def expand(sec, v, stack):
        def sub(m):
            if m[0] == "$$": return "$"
            ref = m[1]
            s, k = ref.split(":", 1) if ":" in ref else (sec, ref)
            return get(s, k.lower(), stack)
        return _REF.sub(sub, v)
    def coerce(v):
        l = v.lower()
        if l in ("yes", "true", "on"): return True
        if l in ("no", "false", "off"): return False
        if re.fullmatch(r"-?\d+", v): return int(v)
        return v
    return {s: {k: coerce(get(s, k, frozenset())) for k in kv} for s, kv in raw.items() if kv}
