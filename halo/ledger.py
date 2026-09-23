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


def record(kind: str, status: str, model: str, usage, *, frontier: dict, seconds: float,
           chars_per_token: float = 3.0, output_weight: float = 5.0,
           extra: dict | None = None) -> dict:
    """frontier: character counts {direct_in, direct_out, halo_in, halo_out} describing what
    the frontier model would read/write doing the task itself vs. with HALO."""
    tok = {k: estimate_tokens(int(v), chars_per_token) for k, v in frontier.items()}
    direct = tok.get("direct_in", 0) + output_weight * tok.get("direct_out", 0)
    via = tok.get("halo_in", 0) + output_weight * tok.get("halo_out", 0)
    rec = {
        "ts": int(time.time()),
        "kind": kind,
        "status": status,
        "model": model,
        "local_in": usage.prompt_tokens,
        "local_out": usage.output_tokens,
        "local_calls": usage.calls,
        "frontier_tokens": tok,
        # input-token equivalents (output weighted by price ratio)
        "frontier_direct_est": int(direct),
        "frontier_returned_est": int(via),
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
             f"Frontier tokens saved, upper bound: {s['frontier_saved_est']:>10,}  (input-equivalent)",
             ""]
    for kind, k in sorted(s["by_kind"].items()):
        ok = k.get("ok", 0) + k.get("passed", 0)
        lines.append(f"  {kind:<8} {k['tasks']:>4} tasks  {ok:>4} ok  "
                     f"local {k.get('local_in', 0) + k.get('local_out', 0):>9,} tok")
    lines.append("\nLocal figures are measured. Frontier figures are estimated from text length in "
                 "input-token equivalents (output weighted by price) and are an UPPER BOUND: they "
                 "assume the frontier would otherwise read the whole input, while real agents often "
                 "grep. Measured end-to-end savings are in the README.")
    return "\n".join(lines)
