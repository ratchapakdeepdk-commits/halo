"""Claude Code integration and the hybrid / frontier-only switch.

Modes:
  hybrid    - HALO tools are offered, the delegation skill is installed and a short rule in
              ~/.claude/CLAUDE.md tells every session to delegate token-heavy routine work.
  frontier  - nothing is delegated: the rule and skill are removed and the MCP server offers
              no tools (sessions that are already open get "HALO is off" if they try).

Switching takes full effect in new Claude Code sessions.
"""
import os
import re
import shutil
import subprocess
import sys

from . import config

SKILL_SRC = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")
MODES = ("hybrid", "frontier")

BEGIN, END = "<!-- halo:begin (managed by `halo mode`) -->", "<!-- halo:end -->"
RULE = f"""{BEGIN}
# Hybrid by default (HALO)

This machine has a free local model behind the `halo` MCP tools. Work hybrid in every session:

- Before reading or grepping through a large log, file, or command output (over ~200 lines),
  call `halo_digest` with the path. Its `checks.verified` claims are already confirmed in
  the file; grep only a key claim listed in `checks.not_found`.
- Before writing a new self-contained file that a test or command can check, delegate it with
  `halo_code`. Keep the spec short and pass the test file as `context_files`. If the result
  is `failed`, fix the `.halo-draft`. If it is `escalated`, do the work yourself.
- Keep design, multi-file changes, subtle debugging and security-sensitive code yourself.
- If HALO says a path is outside the allowed directories, just do that step yourself.
{END}
"""
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.DOTALL)


def claude_dir() -> str:
    return os.path.expanduser(os.environ.get("HALO_CLAUDE_DIR", "~/.claude"))


def _claude_md() -> str:
    return os.path.join(claude_dir(), "CLAUDE.md")


def _skill_dir() -> str:
    return os.path.join(claude_dir(), "skills", "halo-delegate")


def write_rule(enabled: bool) -> None:
    """Add or remove HALO's block in the global CLAUDE.md, leaving the user's text alone."""
    path = _claude_md()
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    text = BLOCK_RE.sub("", text)
    if enabled:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + RULE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if text.strip():
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    elif os.path.exists(path):
        os.remove(path)


def write_skill(enabled: bool) -> None:
    dst = _skill_dir()
    if enabled:
        os.makedirs(dst, exist_ok=True)
        shutil.copy(SKILL_SRC, os.path.join(dst, "SKILL.md"))
    elif os.path.isdir(dst):
        shutil.rmtree(dst)


def set_mode(mode: str) -> config.Config:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    cfg = config.load()
    cfg.mode = mode
    config.save(cfg)
    write_rule(mode == "hybrid")
    write_skill(mode == "hybrid")
    return cfg


def status() -> dict:
    try:
        with open(_claude_md(), encoding="utf-8") as fh:
            rule = BEGIN in fh.read()
    except OSError:
        rule = False
    return {"mode": config.load().mode, "rule_installed": rule,
            "skill_installed": os.path.exists(os.path.join(_skill_dir(), "SKILL.md")),
            "claude_cli": shutil.which("claude") is not None}


def register_mcp() -> int:
    """Register the MCP server with Claude Code at user scope (all projects)."""
    claude = shutil.which("claude")  # resolves claude.cmd on Windows
    if not claude:
        print("  `claude` CLI not found — install Claude Code, then run `halo setup --claude`")
        return 1
    exe = shutil.which("halo-mcp")
    cmd = [exe] if exe else [sys.executable, "-m", "halo.mcp_server"]
    subprocess.call([claude, "mcp", "remove", "--scope", "user", "halo"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.call([claude, "mcp", "add", "--scope", "user", "halo", "--", *cmd])


def install_claude(mode: str = "hybrid") -> int:
    rc = register_mcp()
    set_mode(mode)
    print(f"  ✓ Claude Code integration installed, mode: {mode}")
    return rc
