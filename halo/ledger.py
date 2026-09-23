"""Append-only JSONL ledger of every delegated task.

Each record separates what we *know* (local tokens, measured by Ollama) from what we
*estimate* (frontier tokens the task would have cost if done directly, and the tokens the
frontier model still has to read in HALO's compact reply).
"""
import json
import os
import time
from collections import defaultdict

from .config import DATA_DIR

LEDGER = os.path.join(DATA_DIR, "ledger.jsonl")


def estimate_tokens(text_or_chars, chars_per_token: float = 3.0) -> int:
    n = text_or_chars if isinstance(text_or_chars, int) else len(text_or_chars or "")
    return int(n / chars_per_token + 0.999)


def record(kind: str, status: str, model: str, usage, *, direct_est: int,
           returned_est: int, seconds: float, extra: dict | None = None) -> dict:
    rec = {
        "ts": int(time.time()),
        "kind": kind,
        "status": status,
        "model": model,
        "local_in": usage.prompt_tokens,
        "local_out": usage.output_tokens,
        "local_calls": usage.calls,
        "frontier_direct_est": direct_est,
        "frontier_returned_est": returned_est,
        "seconds": round(seconds, 2),
    }
    if extra:
        rec.update(extra)
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # accounting must never break the actual task
    return rec


def read(path: str = LEDGER) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def summary(records: list[dict], since: int = 0) -> dict:
    recs = [r for r in records if r.get("ts", 0) >= since]
    by_kind = defaultdict(lambda: defaultdict(int))
    tot = defaultdict(int)
    for r in recs:
        k = by_kind[r["kind"]]
        k["tasks"] += 1
        k[r["status"]] += 1
        for f in ("local_in", "local_out", "frontier_direct_est", "frontier_returned_est"):
            k[f] += r.get(f, 0)
            tot[f] += r.get(f, 0)
        tot["tasks"] += 1
        tot[r["status"]] += 1
    # Escalated/failed tasks still cost the frontier a full attempt later, so only
    # successful ones count as saved.
    saved = sum(max(0, r.get("frontier_direct_est", 0) - r.get("frontier_returned_est", 0))
                for r in recs if r.get("status") in ("ok", "passed"))
    return {"total": dict(tot), "by_kind": {k: dict(v) for k, v in by_kind.items()},
            "frontier_saved_est": saved}


def format_summary(s: dict) -> str:
    t = s["total"]
    if not t.get("tasks"):
        return "No delegated tasks recorded yet."
    lines = [f"Delegated tasks: {t['tasks']}  (ok/passed: {t.get('ok', 0) + t.get('passed', 0)}, "
             f"escalated: {t.get('escalated', 0)}, failed: {t.get('failed', 0)})",
             f"Local tokens used:            {t.get('local_in', 0) + t.get('local_out', 0):>10,}",
             f"Frontier tokens saved (est.): {s['frontier_saved_est']:>10,}",
             ""]
    for kind, k in sorted(s["by_kind"].items()):
        ok = k.get("ok", 0) + k.get("passed", 0)
        lines.append(f"  {kind:<8} {k['tasks']:>4} tasks  {ok:>4} ok  "
                     f"local {k.get('local_in', 0) + k.get('local_out', 0):>9,} tok")
    lines.append("\nFrontier figures are estimates from text length; local figures are measured.")
    return "\n".join(lines)
