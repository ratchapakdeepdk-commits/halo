"""Agent integrations (Claude Code, Codex CLI, Gemini CLI) and the hybrid / frontier switch.

The agent in charge is the user's choice: HALO is an MCP server, and every supported agent
gets the same two things -
  - the `halo` MCP server registered at user scope (all projects), and
  - a short managed rule block in the agent's global instructions file
    (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.gemini/GEMINI.md).
Claude Code also gets the halo-delegate skill.

Modes:
  hybrid    - HALO tools are offered and the rule tells every session to delegate
              token-heavy routine work.
  frontier  - nothing is delegated: rules and skill are removed and the MCP server offers
              no tools (sessions that are already open get "HALO is off" if they try).

Switching takes full effect in new agent sessions.
"""
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

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
  A file you can write in one pass in under ~80 lines costs about the same either way;
  the saving is on longer files.
- Keep design, multi-file changes, subtle debugging and security-sensitive code yourself.
- When the user asks to hand work to another agent or vendor (Codex/GPT, Gemini, Claude), use
  `halo_handoff` with a narrow `sector` and a check, then review the diff it returns (it finds
  real bugs). Pays off from ~150 lines of new code; smaller jobs cost about the same either way.
  Not on your own initiative: it sends the project (minus secrets) to that vendor.
- If HALO says a path is outside the allowed directories, just do that step yourself.
{END}
"""
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.DOTALL)


@dataclass(frozen=True)
class Agent:
    name: str
    title: str
    cli: str
    env_dir: str        # env var that overrides the config dir (tests, custom installs)
    default_dir: str
    rules_file: str     # global instructions file inside the config dir
    add: tuple          # argv after the CLI name; "{cmd}" expands to the server command
    remove: tuple
    skill: bool = False

    def home(self) -> str:
        return os.path.expanduser(os.environ.get(self.env_dir) or self.default_dir)

    def rules_path(self) -> str:
        return os.path.join(self.home(), self.rules_file)


AGENTS = {
    "claude": Agent("claude", "Claude Code", "claude", "HALO_CLAUDE_DIR", "~/.claude",
                    "CLAUDE.md", ("mcp", "add", "--scope", "user", "halo", "--", "{cmd}"),
                    ("mcp", "remove", "--scope", "user", "halo"), skill=True),
    "codex": Agent("codex", "Codex CLI", "codex", "HALO_CODEX_DIR",
                   os.environ.get("CODEX_HOME") or "~/.codex", "AGENTS.md",
                   ("mcp", "add", "halo", "--", "{cmd}"), ("mcp", "remove", "halo")),
    "gemini": Agent("gemini", "Gemini CLI", "gemini", "HALO_GEMINI_DIR", "~/.gemini",
                    "GEMINI.md", ("mcp", "add", "--scope", "user", "halo", "{cmd}"),
                    ("mcp", "remove", "--scope", "user", "halo")),
}


def claude_dir() -> str:
    return AGENTS["claude"].home()


def _skill_dir() -> str:
    return os.path.join(claude_dir(), "skills", "halo-delegate")


def detect() -> list[str]:
    """Agents whose CLI is installed on this machine."""
    return [n for n, a in AGENTS.items() if shutil.which(a.cli)]


def active_agents(cfg: config.Config | None = None) -> list[str]:
    cfg = cfg or config.load()
    names = [n for n in (cfg.agents or []) if n in AGENTS]
    return names or ["claude"]  # installs from before multi-agent support were Claude-only


def write_rule(enabled: bool, agent: str = "claude") -> None:
    """Add or remove HALO's block in an agent's global rules file, keeping the user's text."""
    path = AGENTS[agent].rules_path()
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    had_block = BEGIN in text
    text = BLOCK_RE.sub("", text)
    if had_block and text.strip():
        text = text.rstrip() + "\n"  # drop the blank separator line HALO added
    if enabled:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + RULE
    if text.strip():
        os.makedirs(os.path.dirname(path), exist_ok=True)
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
    for name in active_agents(cfg):
        write_rule(mode == "hybrid", name)
        if AGENTS[name].skill:
            write_skill(mode == "hybrid")
    return cfg


def _rule_installed(agent: str) -> bool:
    try:
        with open(AGENTS[agent].rules_path(), encoding="utf-8") as fh:
            return BEGIN in fh.read()
    except OSError:
        return False


def status() -> dict:
    cfg = config.load()
    agents = {n: {"title": a.title, "cli": shutil.which(a.cli) is not None,
                  "enabled": n in active_agents(cfg), "rule_installed": _rule_installed(n)}
              for n, a in AGENTS.items()}
    return {"mode": cfg.mode, "rule_installed": _rule_installed("claude"),
            "skill_installed": os.path.exists(os.path.join(_skill_dir(), "SKILL.md")),
            "claude_cli": agents["claude"]["cli"], "agents": agents}


def _say(msg: str) -> None:
    """print() that cannot crash on a legacy console code page (Windows cp1252 has no ✓)."""
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


def _server_cmd() -> list[str]:
    exe = shutil.which("halo-mcp")
    return [exe] if exe else [sys.executable, "-m", "halo.mcp_server"]


def register_mcp(agent: str = "claude") -> int:
    """Register the MCP server with one agent at user scope (all projects)."""
    a = AGENTS[agent]
    cli = shutil.which(a.cli)  # resolves .cmd shims on Windows
    if not cli:
        _say(f"  `{a.cli}` not found — install {a.title}, then run `halo setup --agent {agent}`")
        return 1
    subprocess.call([cli, *a.remove], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    argv = []
    for part in a.add:
        argv += _server_cmd() if part == "{cmd}" else [part]
    rc = subprocess.call([cli, *argv], stdin=subprocess.DEVNULL)
    if rc == 0 and agent == "codex":
        _codex_auto_approve()
    return rc


def _codex_auto_approve() -> None:
    """Codex asks before every MCP call, and `codex exec` (non-interactive) then refuses them
    all. HALO confines paths itself (allowed_roots), so its tools are pre-approved."""
    path = os.path.join(AGENTS["codex"].home(), "config.toml")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    try:
        start = lines.index("[mcp_servers.halo]")
    except ValueError:
        return
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    if any(l.startswith("default_tools_approval_mode") for l in lines[start:end]):
        return
    lines.insert(start + 1, 'default_tools_approval_mode = "approve"')
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def install(agents: list[str], mode: str = "hybrid") -> int:
    """Connect HALO to the chosen agents and remember them for `halo mode`."""
    unknown = [n for n in agents if n not in AGENTS]
    if unknown:
        raise ValueError(f"unknown agent(s) {unknown}; choose from {list(AGENTS)}")
    cfg = config.load()
    # a pre-multi-agent install is Claude-only with no `agents` list: keep Claude in it
    base = cfg.agents or (["claude"] if _rule_installed("claude") else [])
    cfg.agents = list(dict.fromkeys([*base, *agents]))
    config.save(cfg)
    rc = 0
    for n in agents:
        rc |= register_mcp(n)
    set_mode(mode)
    for n in agents:
        _say(f"  ✓ {AGENTS[n].title}: HALO tools + rule in {AGENTS[n].rules_path()}, mode: {mode}")
    return rc


def uninstall(agent: str) -> None:
    """Remove the rule (and MCP registration if the CLI is there) from one agent."""
    a = AGENTS[agent]
    write_rule(False, agent)
    if a.skill:
        write_skill(False)
    cli = shutil.which(a.cli)
    if cli:
        subprocess.call([cli, *a.remove], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = config.load()
    cfg.agents = [n for n in (cfg.agents or []) if n != agent]
    config.save(cfg)


def install_claude(mode: str = "hybrid") -> int:
    return install(["claude"], mode)
