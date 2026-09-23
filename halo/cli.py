"""halo — command-line entry point."""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time

from . import __version__, config, doctor, ledger, llm, router, tasks

SKILL_SRC = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")


def _stdin_idle(wait: float = 0.3) -> bool:
    """True if stdin has nothing to read shortly (e.g. an agent's idle socket)."""
    import select
    try:
        return not select.select([sys.stdin], [], [], wait)[0]
    except (OSError, ValueError):
        return True


def _stdin_text(files: list[str]) -> tuple[list[str], str]:
    """Read stdin only when it is the input: '-f -' explicitly, or no -f and data is waiting.

    Agents and CI often run commands with a non-tty stdin that never closes; reading it
    unconditionally would hang `halo digest -f file` forever.
    """
    explicit = "-" in files
    files = [f for f in files if f != "-"]
    if sys.stdin is None or sys.stdin.isatty():
        return files, ""
    if explicit:
        return files, sys.stdin.read()
    if files:
        return files, ""
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError):
        return files, ""
    # A pipe or redirected file is deliberate input (wait for slow producers); anything else
    # (sockets, /dev/null-like devices) only counts if data is already there.
    if stat.S_ISFIFO(mode) or stat.S_ISREG(mode) or not _stdin_idle():
        return files, sys.stdin.read()
    return files, ""


def _progress(msg: str):
    if sys.stderr.isatty():
        print(f"\r[halo: {msg}]\033[K", end="", file=sys.stderr, flush=True)
    else:
        print(f"[halo: {msg}]", file=sys.stderr, flush=True)


def _emit(res: dict, as_json: bool, main_key: str = "answer") -> int:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        st = res.get("status")
        if st == "error":
            print(f"halo: {res.get('error')}", file=sys.stderr)
        elif main_key in res:
            print(res[main_key])
        else:
            for k, v in res.items():
                print(f"{k}: {v}" if "\n" not in str(v) else f"{k}:\n{v}")
        tag = f"[local:{res.get('model')}]" if res.get("model") else ""
        if st in ("escalated", "failed"):
            tag += f" {st}: {res.get('reason', '')}"
        if tag:
            print(tag, file=sys.stderr)
    return {"ok": 0, "passed": 0, "escalated": 3, "failed": 4}.get(res.get("status"), 1)


def cmd_ask(a, cfg):
    return _emit(tasks.ask(cfg, " ".join(a.question), model=a.model, from_frontier=False), a.json)


def cmd_digest(a, cfg):
    files, stdin = _stdin_text(a.file)
    if not files and not stdin.strip():
        print("halo digest: give -f FILE, or pipe input (use '-f -' for slow pipes)",
              file=sys.stderr)
        return 2
    res = tasks.digest(cfg, " ".join(a.question), files, stdin, model=a.model,
                       max_words=a.max_words, progress=_progress, compact=a.compact,
                       debug=a.debug)
    if a.debug and not a.json:
        for i, n in enumerate(res.get("notes", []), 1):
            print(f"--- note {i}\n{n}", file=sys.stderr)
    if res.get("compacted") and not a.json:
        print(f"[halo: {res['compacted']}]", file=sys.stderr)
    if sys.stderr.isatty() and res.get("chunks", 1) > 1:
        print(file=sys.stderr)
    return _emit(res, a.json)


def cmd_code(a, cfg):
    spec = a.spec or ""
    if a.spec_file:
        spec += open(a.spec_file, encoding="utf-8").read()
    if not spec.strip():
        print("halo code: give --spec or --spec-file", file=sys.stderr)
        return 2
    res = tasks.code(cfg, spec, a.target, a.check, workdir=a.workdir, context_files=a.context,
                     max_iters=a.iters, model=a.model, keep_on_fail=a.keep,
                     diff_mode="full" if a.full_diff else "stat")
    if a.json:
        return _emit(res, True)
    print(f"{res['status'].upper()} after {res.get('attempts', 0)} attempt(s) "
          f"in {res.get('seconds', 0)}s — {res.get('diffstat', '')}")
    for k in ("reason", "check_summary", "last_check_output", "diff", "error"):
        if res.get(k):
            print(f"\n{k}:\n{res[k]}")
    return 0 if res["status"] == "passed" else 1


def cmd_auto(a, cfg):
    prompt = " ".join(a.question)
    files, stdin = _stdin_text(a.file)
    where, why = router.route(prompt, has_input=bool(files or stdin.strip()))
    if a.verbose or where == "frontier":
        print(f"[halo route: {where} — {why}]", file=sys.stderr)
    if where == "local":
        if files or stdin.strip():
            return _emit(tasks.digest(cfg, prompt, files, stdin), False)
        return _emit(tasks.ask(cfg, prompt, from_frontier=False), False)
    if not shutil.which("claude"):
        print("halo: this looks like frontier work but the `claude` CLI is not installed; "
              "use `halo ask` to force local.", file=sys.stderr)
        return 1
    return subprocess.call(["claude", "-p", prompt])


def cmd_stats(a, cfg):
    since = int(time.time() - a.days * 86400) if a.days else 0
    s = ledger.summary(ledger.read(), since)
    print(json.dumps(s, indent=1) if a.json else ledger.format_summary(s))
    return 0


def cmd_doctor(a, cfg):
    print(f"halo {__version__} — config: {config.CONFIG_PATH}")
    lines, facts = doctor.run(cfg, bench=not a.no_bench)
    print("\n".join(lines))
    return 0 if facts["ok"] else 1


def _detect_url(cfg) -> str | None:
    candidates = [cfg.ollama_url, "http://127.0.0.1:11434"]
    for url in dict.fromkeys(candidates):
        try:
            llm.raw(config.Config(ollama_url=url), "/api/tags", timeout=3)
            return url
        except llm.LocalModelError:
            continue
    return None


def _pull(cfg, model: str) -> bool:
    print(f"pulling {model} (this can take a while)...")
    if shutil.which("ollama"):
        env = dict(os.environ, OLLAMA_HOST=cfg.ollama_url)
        return subprocess.call(["ollama", "pull", model], env=env) == 0
    try:
        llm.raw(cfg, "/api/pull", {"model": model, "stream": False}, timeout=7200)
        return True
    except llm.LocalModelError as e:
        print(f"  pull failed: {e}")
        return False


def install_claude() -> int:
    if not shutil.which("claude"):
        print("  `claude` CLI not found — skipping Claude Code integration")
        return 1
    exe = shutil.which("halo-mcp")
    cmd = [exe] if exe else [sys.executable, "-m", "halo.mcp_server"]
    subprocess.call(["claude", "mcp", "remove", "--scope", "user", "halo"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = subprocess.call(["claude", "mcp", "add", "--scope", "user", "halo", "--", *cmd])
    dst = os.path.expanduser("~/.claude/skills/halo-delegate")
    os.makedirs(dst, exist_ok=True)
    shutil.copy(SKILL_SRC, os.path.join(dst, "SKILL.md"))
    print(f"  ✓ MCP server registered (user scope) and skill installed to {dst}")
    return rc


def cmd_setup(a, cfg):
    if a.url:
        cfg.ollama_url = a.url.rstrip("/")
    url = _detect_url(cfg)
    if not url:
        print(f"Cannot reach Ollama at {cfg.ollama_url}. Install it from https://ollama.com, "
              f"start it, then re-run (or pass --url).")
        return 1
    cfg.ollama_url = url
    print(f"✓ Ollama at {url}")

    models = [a.model] if a.model else []
    if not a.model:
        vram = sum(m for _, m in doctor.gpus()) / 1024
        rec = doctor.recommend(vram)
        if not rec:
            print(f"Only {vram:.0f} GiB VRAM detected; pass --model explicitly "
                  f"(e.g. qwen3:4b-instruct).")
            return 1
        print(f"✓ {vram:.0f} GiB VRAM → digest model {rec[0]}, code model {rec[1]} ({rec[2]})")
        cfg.model = rec[0]
        cfg.code_model = "" if rec[1] == rec[0] else rec[1]
        models = list(dict.fromkeys(rec[:2]))
    else:
        cfg.model, cfg.code_model = a.model, a.code_model or ""
        if a.code_model:
            models.append(a.code_model)

    installed = [m["name"] for m in llm.raw(cfg, "/api/tags").get("models", [])]
    for model in models:
        if model in installed:
            continue
        if not (a.yes or input(f"Pull {model} now? [Y/n] ").strip().lower() in ("", "y")):
            print(f"Not pulled. Pull it later with `ollama pull {model}`.")
        elif not _pull(cfg, model):
            return 1
    print(f"✓ config written to {config.save(cfg)}")
    if a.claude:
        install_claude()
    print("\nNext: `halo doctor` to benchmark, `halo stats` to see savings.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="halo", description="Offload routine work to a local model.")
    p.add_argument("--version", action="version", version=f"halo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ask", help="short question to the local model")
    s.add_argument("question", nargs="+")
    s.add_argument("-m", "--model")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("digest", help="read big files/stdin locally, return only the answer")
    s.add_argument("question", nargs="+")
    s.add_argument("-f", "--file", action="append", default=[])
    s.add_argument("-m", "--model")
    s.add_argument("-w", "--max-words", type=int, default=200)
    s.add_argument("--compact", choices=["auto", "on", "off"], default="auto",
                   help="collapse repetitive log lines (auto: only for log-like input)")
    s.add_argument("--debug", action="store_true", help="show per-chunk notes")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("code", help="generate-verify loop for one file")
    s.add_argument("-s", "--spec")
    s.add_argument("--spec-file")
    s.add_argument("-t", "--target", required=True)
    s.add_argument("-c", "--check", required=True, help="shell command; exit 0 = accepted")
    s.add_argument("-C", "--workdir", default=".")
    s.add_argument("-x", "--context", action="append", default=[], help="read-only context file")
    s.add_argument("-n", "--iters", type=int)
    s.add_argument("-m", "--model")
    s.add_argument("--keep", action="store_true", help="keep the last attempt even if it fails")
    s.add_argument("--full-diff", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_code)

    s = sub.add_parser("auto", help="route: local for simple/large-input, else `claude -p`")
    s.add_argument("question", nargs="+")
    s.add_argument("-f", "--file", action="append", default=[])
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_auto)

    s = sub.add_parser("stats", help="delegated tasks and estimated savings")
    s.add_argument("--days", type=float, default=0)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("doctor", help="check Ollama, GPU backend and speed")
    s.add_argument("--no-bench", action="store_true")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("setup", help="detect hardware, pull a model, write config")
    s.add_argument("--model", help="digest/ask model (default: pick from VRAM)")
    s.add_argument("--code-model", help="model for `halo code` (default: same as --model)")
    s.add_argument("--url", help="Ollama URL (default: auto-detect)")
    s.add_argument("--claude", action="store_true", help="also register with Claude Code")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("mcp", help="run the MCP server on stdio")
    s.set_defaults(fn=lambda a, cfg: __import__("halo.mcp_server").mcp_server.main() or 0)

    a = p.parse_args(argv)
    try:
        sys.exit(a.fn(a, config.load()))
    except KeyboardInterrupt:
        sys.exit(130)
    except llm.LocalModelError as e:
        print(f"halo: {e}", file=sys.stderr)
        sys.exit(1)
