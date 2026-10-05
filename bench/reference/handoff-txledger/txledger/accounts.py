from decimal import Decimal


class UnknownAccount(KeyError):
    pass


class InsufficientFunds(ValueError):
    pass


class Ledger:
    def __init__(self, sources=()):
        self.sources = set(sources)
        self._bal: dict[str, Decimal] = {s: Decimal(0) for s in self.sources}
        self._hist: dict[str, list] = {s: [] for s in self.sources}

    def balance(self, name: str) -> Decimal:
        if name not in self._bal:
            raise UnknownAccount(name)
        return self._bal[name]

    def history(self, name: str) -> list:
        self.balance(name)
        return list(self._hist[name])

    def apply(self, tx) -> None:
        if tx.src not in self._bal:
            raise UnknownAccount(tx.src)
        if tx.src not in self.sources and self._bal[tx.src] < tx.amount:
            raise InsufficientFunds(f"{tx.src} has {self._bal[tx.src]}, needs {tx.amount}")
        self._bal[tx.src] -= tx.amount
        self._bal[tx.dst] = self._bal.get(tx.dst, Decimal(0)) + tx.amount
        for who in (tx.src, tx.dst):
            self._hist.setdefault(who, []).append(tx)
