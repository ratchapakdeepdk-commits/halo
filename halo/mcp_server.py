"""MCP server (stdio, newline-delimited JSON-RPC 2.0) exposing HALO to frontier agents.

Stdlib only, so `pip install` pulls nothing. Register with Claude Code:
    claude mcp add --scope user halo -- halo-mcp
"""
import json
import os
import sys
import traceback

from . import __version__, config, ledger, llm, tasks

SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]

TOOLS = [
    {
        "name": "halo_digest",
        "description": (
            "Read large files or text with a FREE local model and get back only the part that "
            "answers your question. Use this INSTEAD of reading big logs, data dumps, long "
            "docs or long command output into your own context (anything over ~200 lines). "
            "Pass file paths, not contents. The answer is extracted by a small model, but "
            "`signals` (exact counts) and `checks.verified` (its quotes/timestamps found "
            "verbatim in the file, with line number and the line itself) are computed by code: "
            "trust those without re-grepping, and verify only claims in `checks.not_found`. "
            "status='escalated' means the local model could not do it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "What to extract or summarize."},
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "Files to read (absolute or relative to cwd)."},
                "text": {"type": "string", "description": "Inline text (optional)."},
                "max_words": {"type": "integer", "default": 200},
                "compact": {"type": "string", "enum": ["auto", "on", "off"], "default": "auto",
                            "description": "Collapse repetitive log lines before reading "
                            "(auto = only log-like input). Use 'off' for CSV/data where "
                            "every exact value matters."},
            },
            "required": ["question"],
        },
    },
    {
        "name": "halo_code",
        "description": (
            "Delegate writing ONE file to a free local model in a generate-verify loop: it "
            "writes the file, runs your check command, and retries with the error output until "
            "the check exits 0 (max_iters). Use for routine, well-specified code: boilerplate, "
            "small functions/scripts, parsers, format converters, test fixtures, CLI glue. "
            "The saving grows with file length: under ~80 lines that you can write in one pass, "
            "delegating costs about the same as writing it yourself. "
            "You MUST give a check that really verifies behaviour (e.g. a pytest file you "
            "wrote, `python -c 'import ...; assert ...'`, a compiler). Returns status plus a "
            "diffstat only; read the file if you want to review it. On failure/escalation the "
            "original file is restored and you get the last check output so you can take over. "
            "Do not use for multi-file changes, security-sensitive code, or design decisions."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string", "description": "SHORT requirements (aim for under "
                         "~600 chars): what the tests do not already show - signatures, allowed "
                         "libraries, constraints. Do not restate behaviour the test file in "
                         "context_files already pins down; a long spec costs you the output "
                         "tokens this tool is meant to save."},
                "target": {"type": "string", "description": "File to create/overwrite, "
                           "relative to workdir."},
                "check": {"type": "string", "description": "Shell command run in workdir; "
                          "exit 0 = accepted."},
                "workdir": {"type": "string", "description": "Project root (default: cwd)."},
                "context_files": {"type": "array", "items": {"type": "string"},
                                  "description": "Read-only files the worker needs to see "
                                  "(e.g. the test file, an interface)."},
                "max_iters": {"type": "integer", "default": 3},
                "full_diff": {"type": "boolean", "default": False,
                              "description": "Return the full diff instead of a diffstat."},
            },
            "required": ["spec", "target", "check"],
        },
    },
    {
        "name": "halo_ask",
        "description": (
            "Ask the free local model a short self-contained question (translation, regex, "
            "unit conversion, reformatting a small snippet). Saves little when the answer is "
            "short; prefer halo_digest/halo_code for real savings. Unverified output."),
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "halo_council",
        "description": (
            "Get independent second opinions from OTHER frontier models (GPT via the Codex CLI, "
            "Gemini CLI, Claude) and/or local models, asked in parallel on the user's own plans. "
            "Use it for things worth a cross-check: a derivation or calculation, a design or "
            "review decision, a bug you are unsure about, a claim you cannot verify. Not for "
            "routine work. You get every answer side by side; compare them yourself, and treat "
            "agreement as evidence, not proof. Pass file paths (max ~60k chars) rather than "
            "pasting. The material is sent to those vendors."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "Files the models should see (optional)."},
                "models": {"type": "array", "items": {"type": "string"},
                           "description": "Override who is asked, e.g. [\"codex\", "
                                          "\"gemini\", \"claude:opus\"]. Default: the "
                                          "configured council."},
            },
            "required": ["question"],
        },
    },
    {
        "name": "halo_stats",
        "description": "Show how many tasks were delegated and estimated frontier tokens saved.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _roots(cfg) -> list[str]:
    return [os.path.realpath(r) for r in [os.getcwd(), *map(os.path.expanduser, cfg.allowed_roots)]]


def _outside(paths: list[str], roots: list[str]) -> list[str]:
    bad = []
    for p in paths:
        rp = os.path.realpath(os.path.expanduser(p))
        if not any(os.path.commonpath([rp, r]) == r for r in roots):
            bad.append(p)
    return bad


def _denied(bad: list[str], roots: list[str]) -> dict:
    return {"status": "error",
            "error": f"outside the allowed directories {roots}: {bad}. HALO runs outside your "
                     f"sandbox, so it only touches the project directory; the user can add "
                     f"more with allowed_roots in ~/.config/halo/config.json."}


def call_tool(name: str, args: dict) -> dict:
    cfg = config.load()
    if os.environ.get("HALO_WORKER") and name != "halo_stats":
        return {"status": "off", "error": "HALO tools are disabled inside a HALO worker."}
    if cfg.mode != "hybrid" and name not in ("halo_stats", "halo_council"):
        return {"status": "off", "error": "HALO is switched to frontier-only mode by the user. "
                                          "Do this step yourself."}
    roots = _roots(cfg)
    if name == "halo_digest":
        bad = _outside(args.get("paths") or [], roots)
        if bad:
            return _denied(bad, roots)
        return tasks.digest(cfg, args["question"], args.get("paths") or [],
                            args.get("text", ""), max_words=int(args.get("max_words", 200)),
                            compact=args.get("compact", "auto"))
    if name == "halo_code":
        workdir = args.get("workdir") or os.getcwd()
        bad = _outside([workdir, *(args.get("context_files") or [])], roots)
        if bad:
            return _denied(bad, roots)
        return tasks.code(cfg, args["spec"], args["target"], args["check"],
                          workdir=workdir,
                          context_files=args.get("context_files") or [],
                          max_iters=args.get("max_iters"),
                          diff_mode="full" if args.get("full_diff") else "stat")
    if name == "halo_ask":
        return tasks.ask(cfg, args["question"])
    if name == "halo_council":
        bad = _outside(args.get("paths") or [], roots)
        if bad:
            return {"status": "error", "error": f"outside the allowed directories: {bad}"}
        models = args.get("models") or None
        if cfg.mode != "hybrid":  # frontier-only: vendors yes, local models no
            models = [m for m in (models or tasks.council_models(cfg)) if llm.is_cloud(m)]
            if not models:
                return {"status": "off", "error": "frontier-only mode: no vendor models "
                                                  "in the council"}
        return tasks.council(cfg, args["question"], args.get("paths") or [], models=models)
    if name == "halo_stats":
        return {"status": "ok", "report": ledger.format_summary(ledger.summary(ledger.read()))}
    raise KeyError(name)


def handle(msg: dict) -> dict | None:
    mid, method = msg.get("id"), msg.get("method", "")
    if mid is None:  # notification (e.g. notifications/initialized)
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return ok({"protocolVersion": want if want in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "halo", "version": __version__},
                   "instructions": (
                       "HALO gives you a free local model as a worker. Keep planning, tool use "
                       "and final decisions yourself; delegate token-heavy routine work: "
                       "halo_digest for large inputs, halo_code for well-specified single-file "
                       "code with a real check.")})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        # Frontier-only mode: offer nothing, so new sessions do not even see the tools.
        # Inside a HALO worker (a vendor CLI HALO itself started) offer nothing: no loops.
        # Frontier-only mode still offers halo_council: frontier models helping each other.
        if os.environ.get("HALO_WORKER"):
            return ok({"tools": []})
        if config.load().mode != "hybrid":
            return ok({"tools": [t for t in TOOLS if t["name"] in ("halo_council", "halo_stats")]})
        return ok({"tools": TOOLS})
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            res = call_tool(p.get("name", ""), p.get("arguments") or {})
        except KeyError as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool or missing argument: {e}"}}
        except Exception as e:  # report, never crash the server
            traceback.print_exc(file=sys.stderr)
            res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        is_err = res.get("status") in ("error", "off")
        return ok({"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False,
                                                                  indent=1)}],
                   "isError": is_err})
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
