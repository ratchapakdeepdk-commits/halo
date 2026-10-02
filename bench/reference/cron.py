from datetime import timedelta

FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12), ("dow", 0, 7)]
NAMES = {"month": "jan feb mar apr may jun jul aug sep oct nov dec".split(),
         "dow": "sun mon tue wed thu fri sat".split()}
MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
          "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
          "@hourly": "0 * * * *"}


def _num(tok, name, lo, hi):
    t = tok.lower()
    if name in NAMES and t in NAMES[name]:
        return NAMES[name].index(t) + (1 if name == "month" else 0)
    if not t.isdigit():
        raise ValueError(f"bad value {tok!r} in {name}")
    v = int(t)
    if not lo <= v <= hi:
        raise ValueError(f"{name} value {v} out of range")
    return v


def _field(text, name, lo, hi):
    out = set()
    for part in text.split(","):
        base, _, step = part.partition("/")
        if _ and not step.isdigit() or _ and int(step) == 0:
            raise ValueError(f"bad step in {part!r}")
        step = int(step) if step else 1
        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            x, _, y = base.partition("-")
            a, b = _num(x, name, lo, hi), _num(y, name, lo, hi)
            if a > b:
                raise ValueError(f"reversed range {base!r}")
        else:
            a = _num(base, name, lo, hi)
            b = hi if _ else a
        out.update(range(a, b + 1, step))
    if name == "dow" and 7 in out:
        out.discard(7)
        out.add(0)
    return sorted(out)


def _split(expr):
    expr = expr.strip()
    if expr.startswith("@"):
        if expr.lower() not in MACROS:
            raise ValueError(f"unknown macro {expr!r}")
        expr = MACROS[expr.lower()]
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron expression needs 5 fields")
    return parts


def parse(expr):
    return {n: _field(t, n, lo, hi) for t, (n, lo, hi) in zip(_split(expr), FIELDS)}


def next_run(expr, after):
    parts = _split(expr)
    p = parse(expr)
    sets = {k: set(v) for k, v in p.items()}
    either = not parts[2].startswith("*") and not parts[4].startswith("*")
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366 * 5)
    while t <= limit:
        dom_ok = t.day in sets["dom"]
        dow_ok = (t.weekday() + 1) % 7 in sets["dow"]
        day_ok = (dom_ok or dow_ok) if either else (dom_ok and dow_ok)
        if t.month not in sets["month"] or not day_ok:
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
        elif t.hour not in sets["hour"]:
            t = (t + timedelta(hours=1)).replace(minute=0)
        elif t.minute not in sets["minute"]:
            t += timedelta(minutes=1)
        else:
            return t
    raise ValueError("no matching time within 5 years")
