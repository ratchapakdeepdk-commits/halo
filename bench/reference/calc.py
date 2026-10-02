import math
import re


class CalcError(ValueError):
    pass


TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|([A-Za-z_]\w*)|(\*\*|//|[-+*/%(),=]))")
CONSTS = {"pi": math.pi, "e": math.e}


def _sqrt(x):
    if x < 0:
        raise CalcError("sqrt of a negative number")
    return math.sqrt(x)


FUNCS = {"sqrt": (_sqrt, 1, 1), "abs": (abs, 1, 1), "min": (lambda *a: min(a), 1, None),
         "max": (lambda *a: max(a), 1, None), "round": (round, 1, 2)}


def tokenize(line):
    toks, pos = [], 0
    while True:
        while pos < len(line) and line[pos].isspace():
            pos += 1
        if pos >= len(line):
            toks.append(("end", None, len(line) + 1))
            return toks
        m = TOKEN.match(line, pos)
        if not m:
            raise CalcError(f"unexpected character {line[pos]!r} at col {pos + 1}")
        num, name, op = m.groups()
        col = m.start(m.lastindex) + 1
        if num is not None:
            val = float(num) if ("." in num or "e" in num.lower()) else int(num)
            toks.append(("num", val, col))
        elif name is not None:
            toks.append(("name", name, col))
        else:
            toks.append(("op", op, col))
        pos = m.end()


class Calc:
    def __init__(self):
        self.vars = {}

    def evaluate(self, line):
        self.toks, self.i = tokenize(line), 0
        t = self.toks
        if t[0][0] == "name" and t[1][1] == "=":
            name = t[0][1]
            if name in CONSTS:
                raise CalcError(f"cannot assign to constant {name}")
            self.i = 2
            val = self.expr()
            self._expect_end()
            self.vars[name] = val
            return val
        val = self.expr()
        self._expect_end()
        return val

    def peek(self):
        return self.toks[self.i]

    def take(self):
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def fail(self, tok):
        what = "end of input" if tok[0] == "end" else repr(tok[1])
        raise CalcError(f"unexpected {what} at col {tok[2]}")

    def _expect_end(self):
        if self.peek()[0] != "end":
            self.fail(self.peek())

    def _expect(self, op):
        tok = self.take()
        if tok[1] != op or tok[0] != "op":
            self.fail(tok)

    def expr(self):
        val = self.term()
        while self.peek()[1] in ("+", "-") and self.peek()[0] == "op":
            op = self.take()[1]
            rhs = self.term()
            val = val + rhs if op == "+" else val - rhs
        return val

    def term(self):
        val = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "//", "%"):
            op = self.take()[1]
            rhs = self.unary()
            if op == "*":
                val *= rhs
            elif rhs == 0:
                raise CalcError("division by zero")
            elif op == "/":
                val = val / rhs
            elif op == "//":
                val = val // rhs
            else:
                val = val % rhs
        return val

    def unary(self):
        if self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            val = self.unary()
            return -val if op == "-" else +val
        return self.power()

    def power(self):
        base = self.atom()
        if self.peek()[0] == "op" and self.peek()[1] == "**":
            self.take()
            exp = self.unary()
            try:
                return base ** exp
            except ZeroDivisionError:
                raise CalcError("zero to a negative power")
        return base

    def atom(self):
        tok = self.take()
        kind, val, _ = tok
        if kind == "num":
            return val
        if kind == "op" and val == "(":
            v = self.expr()
            self._expect(")")
            return v
        if kind == "name":
            if self.peek()[1] == "(" and self.peek()[0] == "op":
                return self.call(val)
            if val in self.vars:
                return self.vars[val]
            if val in CONSTS:
                return CONSTS[val]
            raise CalcError(f"unknown variable {val}")
        self.fail(tok)

    def call(self, name):
        self.take()
        args = []
        if not (self.peek()[0] == "op" and self.peek()[1] == ")"):
            args.append(self.expr())
            while self.peek()[0] == "op" and self.peek()[1] == ",":
                self.take()
                args.append(self.expr())
        self._expect(")")
        if name not in FUNCS:
            raise CalcError(f"unknown function {name}")
        fn, lo, hi = FUNCS[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise CalcError(f"{name}() takes {lo}..{hi or 'n'} arguments, got {len(args)}")
        return fn(*args)
