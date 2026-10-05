#!/usr/bin/env python3
"""Benchmark the generate-verify loop on bench/tasks/*.

Each task directory holds task.json ({"target", "spec"}) and test_task.py. The worker sees
the spec and the test file (as read-only context, the way a frontier agent would delegate),
and must make `python -m unittest test_task` pass.

    python bench/run.py                     # all tasks, configured model
    python bench/run.py -m gpt-oss:20b -k roman semver
    python bench/run.py --repeat 3          # pass-rate over several samples
    python bench/run.py --chain qwen3:30b-a3b-instruct-2507-q4_K_M codex   # local, then paid tier

Writes bench/results/<timestamp>-<model>.json and prints a markdown table.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from halo import config, ledger, tasks  # noqa: E402


def run_task(cfg, name: str, model: str | None, iters: int, fallback) -> dict:
    src = os.path.join(HERE, "tasks", name)
    with open(os.path.join(src, "task.json")) as fh:
        task = json.load(fh)
    work = tempfile.mkdtemp(prefix=f"halo-bench-{name}-")
    try:
        shutil.copy(os.path.join(src, "test_task.py"), work)
        check = f"{sys.executable} -m unittest -q test_task"
        res = tasks.code(cfg, task["spec"], task["target"], check, workdir=work,
                         context_files=[os.path.join(work, "test_task.py")],
                         max_iters=iters, model=model, fallback=fallback)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    rec = ledger.read()[-1] if ledger.read() else {}
    return {"task": name, "status": res["status"], "attempts": res.get("attempts", 0),
            "seconds": res.get("seconds", 0), "model": res.get("model"),
            "local_tokens": rec.get("local_in", 0) + rec.get("local_out", 0),
            "cloud_tokens": rec.get("cloud_in", 0) + rec.get("cloud_out", 0),
            "frontier_direct_est": rec.get("frontier_direct_est", 0),
            "frontier_returned_est": rec.get("frontier_returned_est", 0),
            "reason": res.get("reason", ""),
            "last_check_output": res.get("last_check_output", "")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-m", "--model")
    p.add_argument("-k", "--tasks", nargs="*")
    p.add_argument("-n", "--iters", type=int, default=3)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--cascade", action="store_true",
                   help="use the configured code_model + fallback_models chain")
    p.add_argument("--chain", nargs="+", help="explicit model chain, e.g. <local model> codex")
    a = p.parse_args()

    cfg = config.load()
    # Keep benchmark runs out of the user's real savings ledger.
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    ledger.LEDGER = os.path.join(HERE, "results", "ledger.jsonl")
    if a.chain:
        model, fallback = a.chain[0], a.chain[1:]
        label = " → ".join(a.chain)
    elif a.cascade:
        model, fallback = None, None
        label = " → ".join([cfg.code_model or cfg.model] + cfg.fallback_models)
    else:
        model, fallback = a.model or cfg.code_model or cfg.model, []
        label = model
    names = a.tasks or sorted(os.listdir(os.path.join(HERE, "tasks")))
    rows = []
    for rep in range(a.repeat):
        for name in names:
            r = run_task(cfg, name, model, a.iters, fallback)
            r["rep"] = rep
            rows.append(r)
            print(f"  {name:<10} {r['status']:<9} {str(r['model']):<28} attempts={r['attempts']} "
                  f"{r['seconds']:>6.1f}s  local={r['local_tokens']} cloud={r['cloud_tokens']}",
                  file=sys.stderr)

    ok = [r for r in rows if r["status"] == "passed"]
    direct = sum(r["frontier_direct_est"] for r in rows)
    # Tasks the worker failed still cost the frontier a full attempt: count their direct cost.
    hybrid = sum(r["frontier_returned_est"] + (0 if r["status"] == "passed"
                                               else r["frontier_direct_est"]) for r in rows)
    summary = {"model": label, "tasks": len(rows), "passed": len(ok),
               "pass_rate": len(ok) / len(rows) if rows else 0,
               "mean_attempts_when_passed": (sum(r["attempts"] for r in ok) / len(ok)) if ok else 0,
               "frontier_tokens_direct_est": direct, "frontier_tokens_hybrid_est": hybrid,
               "frontier_reduction_est": 1 - hybrid / direct if direct else 0,
               "local_tokens": sum(r["local_tokens"] for r in rows),
               "cloud_tokens": sum(r["cloud_tokens"] for r in rows),
               "seconds": sum(r["seconds"] for r in rows)}

    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out = os.path.join(HERE, "results",
                       f"{time.strftime('%Y%m%d-%H%M%S')}-{label.replace(':', '_').replace('/', '_').replace(' → ', '+')}.json")
    with open(out, "w") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, indent=1)

    print(f"\n| task | status | model | attempts | seconds | local tok | frontier direct (est) | frontier via HALO (est) |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        via = r["frontier_returned_est"] + (0 if r["status"] == "passed" else r["frontier_direct_est"])
        print(f"| {r['task']} | {r['status']} | {r['model']} | {r['attempts']} | {r['seconds']:.0f} | "
              f"{r['local_tokens']} | {r['frontier_direct_est']} | {via} |")
    print(f"\n**{label}**: {summary['passed']}/{summary['tasks']} passed, "
          f"est. frontier-token reduction {summary['frontier_reduction_est']:.0%} "
          f"({direct} → {hybrid}), {summary['local_tokens']} local tokens, "
          f"{summary['cloud_tokens']} paid-worker tokens, "
          f"{summary['seconds']:.0f}s total. Results: {os.path.relpath(out, os.path.dirname(HERE))}")


if __name__ == "__main__":
    main()
