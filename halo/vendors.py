"""Vendor CLIs (Codex, Claude Code, Gemini CLI) described as data instead of code.

HALO runs a vendor in two ways: as a text-only *worker* (a paid tier after the local models,
and the council) and as an *agent* that edits a scratch copy of the project (HFF handoff).
Both are command templates. In each element of a template these are filled in:

  {bin}          the CLI found on PATH (or the `<name>_bin` config value)
  {work}         the workspace: an empty scratch dir (worker) or the project copy (agent)
  {model}        the part after "name:" in "name:model"
  {prompt_file}  a file holding the prompt; without it the prompt goes on stdin

and the element "{model_args}" becomes `model_args` when a model is named, or nothing.
`output` names how stdout is read: "codex", "claude", "gemini" (their JSON formats) or
"text" (all of stdout is the answer, tokens unknown).
"""
import json
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class Vendor:
    name: str
    label: str
    worker: tuple
    agent: tuple
    model_args: tuple
    output: str
    install: str = ""    # how to install the CLI, shown in the GUI
    brand: str = ""      # the model family people know it by, shown in the GUI


BUILTIN = {
    # read-only sandbox for the worker; workspace-write (writes only inside {work}, no
    # network) for the agent.
    "codex": Vendor(
        "codex", "Codex CLI",
        worker=("{bin}", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--ignore-user-config", "-s", "read-only", "-C", "{work}", "{model_args}", "-"),
        agent=("{bin}", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
               "--ignore-user-config", "-s", "workspace-write", "-C", "{work}",
               "{model_args}", "-"),
        model_args=("-m", "{model}"), output="codex",
        install="npm install -g @openai/codex", brand="GPT via Codex"),
    # Worker: no tools, no MCP servers, nothing saved. Agent: file tools only, no Bash
    # (Claude Code has no sandbox here) - HALO runs the check itself.
    "claude": Vendor(
        "claude", "Claude Code",
        worker=("{bin}", "-p", "--output-format", "json", "--strict-mcp-config", "--tools", "",
                "--no-session-persistence", "{model_args}"),
        agent=("{bin}", "-p", "--output-format", "json", "--strict-mcp-config",
               "--tools", "Read,Edit,Write,Glob,Grep", "--permission-mode", "acceptEdits",
               "--no-session-persistence", "{model_args}"),
        model_args=("--model", "{model}"), output="claude",
        install="npm install -g @anthropic-ai/claude-code", brand="Claude"),
    # plan = read-only, auto_edit = edits approved and shell refused in headless mode.
    # Allowing only a server that does not exist loads no MCP. The prompt comes on stdin;
    # -p "" switches to headless mode and appends nothing.
    "gemini": Vendor(
        "gemini", "Gemini CLI",
        worker=("{bin}", "-o", "json", "--approval-mode", "plan",
                "--allowed-mcp-server-names", "halo-worker-none", "-p", "", "{model_args}"),
        agent=("{bin}", "-o", "json", "--approval-mode", "auto_edit",
               "--allowed-mcp-server-names", "halo-worker-none", "-p", "", "{model_args}"),
        model_args=("-m", "{model}"), output="gemini",
        install="npm install -g @google/gemini-cli", brand="Gemini"),
}


def names() -> list[str]:
    return list(BUILTIN)


def get(name: str) -> Vendor | None:
    return BUILTIN.get(name)


def split(model: str | None) -> tuple[Vendor | None, str]:
    """"codex:gpt-5" -> (codex vendor, "gpt-5"); a local model -> (None, "")."""
    name, _, sub = (model or "").partition(":")
    v = get(name)
    return (v, sub) if v else (None, "")


def find(cfg, v: Vendor) -> str | None:
    """The CLI's path: the `<name>_bin` config value, else `name` on PATH."""
    return shutil.which(getattr(cfg, f"{v.name}_bin", "") or v.name)


def status(cfg) -> list[dict]:
    return [{"name": v.name, "label": v.label, "brand": v.brand or v.label,
             "install": v.install, "installed": bool(find(cfg, v))} for v in BUILTIN.values()]


def command(template: tuple, model_args: tuple, *, bin: str, work: str, model: str,
            prompt_file: str = "") -> list[str]:
    vals = {"bin": bin, "work": work, "model": model, "prompt_file": prompt_file}
    out = []
    for part in template:
        if part == "{model_args}":
            out += [a.format(**vals) for a in model_args] if model else []
        else:
            out.append(part.format(**vals))
    return out


def uses_prompt_file(template: tuple) -> bool:
    return any("{prompt_file}" in part for part in template)


# Each parser returns (answer, input tokens, output tokens, error) from the CLI's stdout.
def _parse_codex(out: str) -> tuple[str, int, int, str]:
    answer, tin, tout, err = "", 0, 0, ""
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = item.get("text", "")
        elif ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0)
        elif ev.get("type") in ("error", "turn.failed"):
            err = json.dumps(ev.get("error") or ev.get("message") or ev)[:300]
    return answer, tin, tout, err


def _parse_claude(out: str) -> tuple[str, int, int, str]:
    d = json.loads(out)
    u = d.get("usage") or {}
    tin = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
           + u.get("cache_creation_input_tokens", 0))
    err = str(d.get("result", ""))[:300] if d.get("is_error") else ""
    return ("" if err else d.get("result", "")), tin, u.get("output_tokens", 0), err


def _parse_gemini(out: str) -> tuple[str, int, int, str]:
    d = json.loads(out[out.index("{"):])
    tin = tout = 0
    for m in ((d.get("stats") or {}).get("models") or {}).values():
        t = m.get("tokens") or {}
        tin += t.get("prompt", 0)
        tout += t.get("candidates", 0) + t.get("thoughts", 0)
    err = json.dumps(d["error"])[:300] if d.get("error") else ""
    return d.get("response") or "", tin, tout, err


def _parse_text(out: str) -> tuple[str, int, int, str]:
    return out, 0, 0, ""


PARSERS = {"codex": _parse_codex, "claude": _parse_claude, "gemini": _parse_gemini,
           "text": _parse_text}


def parse(v: Vendor, out: str) -> tuple[str, int, int, str]:
    return PARSERS[v.output](out)
