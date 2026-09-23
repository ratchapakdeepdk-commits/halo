"""`halo tune`: measure which local models work best *on this machine* and configure them.

Tables of "model X for Y GB" are a guess; speed and quality depend on the GPU, the driver and
the backend. So we run the installed models that fit through two small, checkable tests:

  digest - a synthetic log with a known rare error (count and last restart are known)
  code   - three single-file tasks with unit tests (generate-verify loop, no fallback)

and pick the best digest model (correct, then fastest) and the best code model (most tests
passed, then fastest). Takes a few minutes per model.
"""
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time

from . import doctor, ledger, llm, tasks
from .config import DATA_DIR, Config, save

TUNE_DIR = os.path.join(os.path.dirname(__file__), "tunedata")
RESULTS = os.path.join(DATA_DIR, "tune.json")
SKIP_RE = re.compile(r"vl\b|vl:|llava|moondream|embed|vision|clip|whisper", re.IGNORECASE)


def candidates(cfg: Config, budget_gib: float) -> list[tuple[str, float]]:
    """Installed text models that fit in the memory budget, largest first."""
    out = []
    for m in llm.raw(cfg, "/api/tags").get("models", []):
        gib = m.get("size", 0) / 2**30
        if SKIP_RE.search(m["name"]) or gib > budget_gib * 0.92:
            continue
        out.append((m["name"], gib))
    return sorted(out, key=lambda x: -x[1])


def _synthetic_log(seed: int = 7) -> tuple[str, dict]:
    rng = random.Random(seed)
    lines, t = [], 8 * 3600
    errors, restart = 0, ""
    for i in range(2500):
        t += rng.randint(1, 20)
        ts = f"Sep 21 {t // 3600 % 24:02d}:{t // 60 % 60:02d}:{t % 60:02d}"
        r = rng.random()
        if i in (400, 1300, 2210):
            lines.append(f"{ts} host systemd[1]: Started api.service - API server.")
            restart = ts
        elif 900 < i < 1000 and r < 0.08:
            lines.append(f"{ts} host api[{4000 + i}]: ERROR db: connection refused "
                         f"(pool exhausted, {rng.randint(10, 99)} waiting)")
            errors += 1
        else:
            lines.append(f"{ts} host api[{4000 + i}]: INFO GET /items/{rng.randint(1, 999)} "
                         f"200 in {rng.randint(2, 90)}ms")
    return "\n".join(lines) + "\n", {"errors": errors, "restart": restart}


def test_digest(cfg: Config, model: str) -> dict:
    text, truth = _synthetic_log()
    t0 = time.time()
    try:
        r = tasks.digest(cfg, "Which errors occurred, how many times each, and when was the "
                              "last restart?", text=text, model=model)
    except llm.LocalModelError as e:
        return {"ok": False, "seconds": 0, "error": str(e)}
    ans = r.get("answer", "")
    ok = ("connection refused" in ans.lower() and str(truth["errors"]) in ans
          and truth["restart"].split()[-1] in ans)
    out = {"ok": ok, "seconds": round(time.time() - t0, 1), "answer": ans[:300],
           "expected": truth}
    if r.get("status") == "error":
        out["error"] = r.get("error")
    return out


def test_code(cfg: Config, model: str, max_iters: int = 3) -> dict:
    passed, secs, detail = 0, 0.0, {}
    for name in sorted(os.listdir(TUNE_DIR)):
        src = os.path.join(TUNE_DIR, name)
        if not os.path.isdir(src):
            continue
        with open(os.path.join(src, "task.json"), encoding="utf-8") as fh:
            task = json.load(fh)
        work = tempfile.mkdtemp(prefix="halo-tune-")
        try:
            shutil.copy(os.path.join(src, "test_task.py"), work)
            r = tasks.code(cfg, task["spec"], task["target"],
                           f'"{sys.executable}" -m unittest -q test_task', workdir=work,
                           context_files=[os.path.join(work, "test_task.py")],
                           max_iters=max_iters, model=model, fallback=[])
        finally:
            shutil.rmtree(work, ignore_errors=True)
        detail[name] = r["status"] if r["status"] != "error" else f"error: {r.get('reason') or r.get('error')}"
        passed += r["status"] == "passed"
        secs += r.get("seconds", 0)
    return {"passed": passed, "total": len(detail), "seconds": round(secs, 1), "tasks": detail}


def run(cfg: Config, models: list[str] | None = None, apply: bool = True,
        progress=print) -> dict:
    hw = doctor.accelerator()
    progress(f"hardware: {hw['detail']} → budget {hw['gib']:.0f} GiB for models")
    if models:
        sizes = {m: g for m, g in candidates(cfg, 10**6)}
        cands = [(m, sizes.get(m, 0.0)) for m in models]
    else:
        cands = candidates(cfg, hw["gib"])
    if not cands:
        rec = doctor.recommend(hw["gib"])
        hint = f" — try: ollama pull {rec[0]}" if rec else ""
        progress(f"no installed model fits{hint}")
        return {"hardware": hw, "results": [], "chosen": None}

    saved_ledger = ledger.LEDGER  # tuning runs are not real savings
    ledger.LEDGER = os.path.join(tempfile.gettempdir(), "halo-tune-ledger.jsonl")
    results = []
    try:
        for name, gib in cands:
            progress(f"testing {name} ({gib:.0f} GiB): digest...")
            d = test_digest(cfg, name)
            progress(f"  digest {'correct' if d['ok'] else 'WRONG'} in {d['seconds']}s"
                     + (f" ({d['error']})" if d.get("error") else "") + "; code...")
            c = test_code(cfg, name)
            progress(f"  code {c['passed']}/{c['total']} passed in {c['seconds']}s")
            results.append({"model": name, "gib": round(gib, 1), "digest": d, "code": c})
    finally:
        ledger.LEDGER = saved_ledger

    good_digest = [r for r in results if r["digest"]["ok"]]
    digest_pick = min(good_digest, key=lambda r: r["digest"]["seconds"])["model"] \
        if good_digest else None
    best_code = max(results, key=lambda r: (r["code"]["passed"], -r["code"]["seconds"]))
    code_pick = best_code["model"] if best_code["code"]["passed"] else None
    chosen = {"model": digest_pick or code_pick, "code_model": code_pick or digest_pick}
    summary = {"ts": int(time.time()), "hardware": hw, "results": results, "chosen": chosen}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RESULTS, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, ensure_ascii=False)

    progress(f"best for digest: {digest_pick or 'none correct'}; "
             f"best for code: {code_pick or 'none passed'}")
    if apply and chosen["model"]:
        cfg.model = chosen["model"]
        cfg.code_model = "" if chosen["code_model"] == chosen["model"] else chosen["code_model"]
        # other models that passed some code tasks become the free fallback cascade
        cfg.fallback_models = [r["model"] for r in sorted(
            results, key=lambda r: (-r["code"]["passed"], r["code"]["seconds"]))
            if r["code"]["passed"] and r["model"] != chosen["code_model"]][:1]
        progress(f"saved to {save(cfg)}")
    return summary
