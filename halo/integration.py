"""Agent integrations and the hybrid / frontier switch.

The agent in charge is the user's choice: HALO is an MCP server, and every agent gets the
same two things -
  - the `halo` MCP server registered at user scope (all projects), and
  - a short managed rule block in the agent's global instructions file
    (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.gemini/GEMINI.md, ...).
Claude Code also gets the halo-delegate skill.

Claude Code, Codex CLI and Gemini CLI are built in. Any other agent can be added
(`halo agents add NAME`, stored under `custom_agents` in the config) in one of three ways:
  - its CLI has an `mcp add` command                    (add / remove argv),
  - it reads MCP servers from a JSON file              (mcp_file + mcp_key + entry), or
  - it has no MCP at all but can run shell commands    (tools: "cli": the rule tells it to
    run `halo digest` / `halo handoff` instead of calling tools).
PRESETS has ready-made specs (Cursor, Windsurf, opencode, Qwen Code, VS Code).

Modes:
  hybrid    - HALO tools are offered and the rule tells every session to delegate
              token-heavy routine work.
  frontier  - nothing is delegated: rules and skill are removed and the MCP server offers
              no tools (sessions that are already open get "HALO is off" if they try).

Switching takes full effect in new agent sessions.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import config

SKILL_SRC = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")
MODES = config.MODES

BEGIN, END = "<!-- halo:begin (managed by `halo mode`) -->", "<!-- halo:end -->"
# The rule is built from the parts the current mode offers; `cli` = the agent has no MCP and
# is taught the same thing as `halo` shell commands.
_LOCAL = """\
- Before reading or grepping through a large log, file, or command output (over ~200 lines),
  call `halo_digest` with the path. Its `checks.verified` claims are already confirmed in
  the file; grep only a key claim listed in `checks.not_found`.
- Before writing a new self-contained file that a test or command can check, delegate it with
  `halo_code`. Keep the spec short and pass the test file as `context_files`. If the result
  is `failed`, fix the `.halo-draft` if it is close, else rewrite. If it is `escalated`, do
  the work yourself. A file you can write in one pass in under ~80 lines costs about the same
  either way; logic-dense files (expression parsers, cron, Markdown: many interacting rules)
  fail locally and cost ~20% more, so write those yourself.
- Keep design, multi-file changes, subtle debugging and security-sensitive code yourself.
"""
_LOCAL_CLI = """\
- Before reading or grepping through a large log, file, or command output (over ~200 lines),
  run `halo digest -f <path> "<question>"` and use its answer instead of reading the file.
  Add `--json` to see `checks`: claims under `verified` are confirmed in the file; grep only
  a key claim listed in `not_found`.
- Keep design, multi-file changes, subtle debugging and security-sensitive code yourself.
"""
_HFF = """\
- When the user asks to hand work to another agent or vendor (Codex/GPT, Gemini, Claude), use
  `halo_handoff` with a narrow `sector` and a check, then review the diff it returns (it finds
  real bugs). Pays off from ~150 lines of new code; smaller jobs cost about the same either way.
  Not on your own initiative: it sends the project (minus secrets) to that vendor.
"""
_HFF_CLI = """\
- When the user asks to hand work to another agent or vendor (Codex/GPT, Gemini, Claude), run
  `halo handoff -s <sector> -c "<check>" -a <agent> "<task>"` and review the diff it prints
  (add `--diff` if it is left out). Pays off from ~150 lines of new code.
  Not on your own initiative: it sends the project (minus secrets) to that vendor.
"""
_ROOTS = "- If HALO says a path is outside the allowed directories, just do that step yourself.\n"


def rule_text(mode: str, cli: bool = False) -> str:
    """HALO's managed block for an agent's global rules file in this mode ("" = none)."""
    local, hff = mode in ("both", "hybrid"), mode in ("both", "hff")
    where = "the `halo` command" if cli else "the `halo` MCP tools"
    if local:
        head = (f"# Hybrid by default (HALO)\n\nThis machine has a free local model behind "
                f"{where}. Work hybrid in every session:\n\n")
    elif hff:
        head = (f"# Handing work to other vendors (HALO)\n\n{where[0].upper()}{where[1:]} "
                f"can hand a sub-task to another vendor's agent:\n\n")
    else:
        return ""
    body = ((_LOCAL_CLI if cli else _LOCAL) if local else "") + \
           ((_HFF_CLI if cli else _HFF) if hff else "") + ("" if cli else _ROOTS)
    return f"{BEGIN}\n{head}{body}{END}\n"


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
    rules: str = ""     # absolute rules file (custom agents); "" = home()/rules_file
    mcp_file: str = ""  # JSON file the agent reads MCP servers from (instead of `mcp add`)
    mcp_key: str = "mcpServers"   # dotted path to the servers object in mcp_file
    entry: object = None          # server entry; "{command}", "{args}", "{argv}" filled in
    tools: str = "mcp"  # "cli": no MCP, the rule tells it to run `halo` commands
    custom: bool = False

    def home(self) -> str:
        return os.path.expanduser(os.environ.get(self.env_dir) or self.default_dir)

    def rules_path(self) -> str:
        if self.rules:
            return os.path.expanduser(self.rules)
        return os.path.join(self.home(), self.rules_file) if self.rules_file else ""

    def kind(self) -> str:
        return "cli" if self.tools == "cli" else "json" if self.mcp_file else "command"

    def installed(self) -> bool:
        """The agent looks present on this machine."""
        if self.cli:
            return shutil.which(self.cli) is not None
        probe = os.path.expanduser(self.mcp_file) if self.mcp_file else self.rules_path()
        return bool(probe) and os.path.isdir(os.path.dirname(probe))


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


def _vscode_user() -> str:
    if sys.platform == "darwin":
        return "~/Library/Application Support/Code/User"
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", "~"), "Code", "User")
    return "~/.config/Code/User"


# Ready-made controller specs for `halo agents add NAME`. Paths are each tool's documented
# user-level config; the user can still pass --mcp-file / --rules to override them.
PRESETS = {
    "cursor": {"title": "Cursor", "mcp_file": "~/.cursor/mcp.json"},  # rules: Cursor settings UI
    "windsurf": {"title": "Windsurf", "mcp_file": "~/.codeium/windsurf/mcp_config.json",
                 "rules": "~/.codeium/windsurf/memories/global_rules.md"},
    "opencode": {"title": "opencode", "mcp_file": "~/.config/opencode/opencode.json",
                 "mcp_key": "mcp",
                 "entry": {"type": "local", "command": "{argv}", "enabled": True},
                 "rules": "~/.config/opencode/AGENTS.md"},
    "qwen": {"title": "Qwen Code", "mcp_file": "~/.qwen/settings.json", "rules": "~/.qwen/QWEN.md"},
    "vscode": {"title": "VS Code (Copilot)", "mcp_file": _vscode_user() + "/mcp.json",
               "mcp_key": "servers", "entry": {"type": "stdio", "command": "{command}",
                                               "args": "{args}"}},
}
SPEC_KEYS = {"title", "cli", "add", "remove", "mcp_file", "mcp_key", "entry", "rules", "tools"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def from_spec(name: str, spec: dict) -> Agent:
    """A user's agent from its JSON spec; ValueError says what is wrong."""
    if not NAME_RE.match(name) or name in AGENTS:
        raise ValueError(f"agent name {name!r}: lowercase letters/digits/-/_ and not "
                         f"{', '.join(AGENTS)}")
    extra = set(spec) - SPEC_KEYS
    if extra:
        raise ValueError(f"unknown key(s) {sorted(extra)}; use {sorted(SPEC_KEYS)}")
    tools = spec.get("tools", "mcp")
    if tools not in ("mcp", "cli"):
        raise ValueError('tools must be "mcp" or "cli"')
    add, remove = tuple(spec.get("add") or ()), tuple(spec.get("remove") or ())
    if tools == "mcp" and not spec.get("mcp_file") and not (spec.get("cli") and add):
        raise ValueError("say how it gets the MCP server: mcp_file (a JSON config), or cli + "
                         "add (its `mcp add` argv with \"{cmd}\"), or tools \"cli\" for none")
    if add and "{cmd}" not in add:
        raise ValueError('add must contain "{cmd}" where the server command goes')
    if tools == "cli" and not spec.get("rules"):
        raise ValueError('an agent without MCP needs a rules file (--rules) to learn the commands')
    if not isinstance(spec.get("mcp_key", ""), str) or not isinstance(spec.get("entry", {}),
                                                                       (dict, type(None))):
        raise ValueError("mcp_key must be a string and entry a JSON object")
    return Agent(name, spec.get("title") or name, spec.get("cli", ""), "", "", "", add, remove,
                 rules=spec.get("rules", ""), mcp_file=spec.get("mcp_file", ""),
                 mcp_key=spec.get("mcp_key") or "mcpServers", entry=spec.get("entry"),
                 tools=tools, custom=True)


def custom(cfg: config.Config | None = None) -> dict:
    out = {}
    for name, spec in ((cfg or config.load()).custom_agents or {}).items():
        try:
            out[name] = from_spec(name, spec)
        except (ValueError, TypeError, AttributeError):
            pass  # a hand-broken spec is skipped, the rest still work
    return out


def all_agents(cfg: config.Config | None = None) -> dict:
    return {**AGENTS, **custom(cfg)}


def get(name: str, cfg: config.Config | None = None) -> Agent:
    a = all_agents(cfg).get(name)
    if not a:
        raise ValueError(f"unknown agent {name!r}; choose from {list(all_agents(cfg))} "
                         f"or add one: halo agents add NAME (presets: {', '.join(PRESETS)})")
    return a


def build_spec(name: str, *, preset: str = "", mcp_file: str = "", mcp_key: str = "",
               entry: dict | None = None, rules: str = "", cli: str = "",
               add: list | None = None, remove: list | None = None, tools: str = "",
               title: str = "") -> dict:
    """Spec for `halo agents add`: a preset (by default the one named like the agent) with
    any flags given laid over it. Validated before it is returned."""
    if preset and preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; presets: {', '.join(PRESETS)}")
    spec = dict(PRESETS.get(preset or name, {}))
    for k, v in (("mcp_file", mcp_file), ("mcp_key", mcp_key), ("entry", entry),
                 ("rules", rules), ("cli", cli), ("add", add), ("remove", remove),
                 ("tools", tools), ("title", title)):
        if v:
            spec[k] = v
    if not spec:
        raise ValueError(f"{name} is not a preset ({', '.join(PRESETS)}): say how it gets HALO "
                         f"with --mcp-file, --mcp-add or --cli-tools (see `halo agents -h`)")
    from_spec(name, spec)
    return spec


def claude_dir() -> str:
    return AGENTS["claude"].home()


def _skill_dir() -> str:
    return os.path.join(claude_dir(), "skills", "halo-delegate")


def detect() -> list[str]:
    """Built-in agents whose CLI is installed on this machine."""
    return [n for n, a in AGENTS.items() if shutil.which(a.cli)]


def active_agents(cfg: config.Config | None = None) -> list[str]:
    cfg = cfg or config.load()
    known = all_agents(cfg)
    names = [n for n in (cfg.agents or []) if n in known]
    return names or ["claude"]  # installs from before multi-agent support were Claude-only


def write_rule(mode: str, agent: str = "claude", cfg: config.Config | None = None) -> None:
    """Add or remove HALO's block in an agent's global rules file, keeping the user's text."""
    a = AGENTS[agent] if agent in AGENTS else get(agent, cfg)
    path = a.rules_path()
    if not path:
        return  # e.g. Cursor: global rules live in its settings, the tool descriptions remain
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    had_block = BEGIN in text
    text = BLOCK_RE.sub("", text)
    if had_block and text.strip():
        text = text.rstrip() + "\n"  # drop the blank separator line HALO added
    rule = rule_text(mode, cli=a.tools == "cli")
    if rule:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + rule
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
    mode = config.MODE_ALIASES.get(mode, mode)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    cfg = config.load()
    cfg.mode = mode
    config.save(cfg)
    for name in active_agents(cfg):
        write_rule(mode, name, cfg)
        if get(name, cfg).skill:
            write_skill(config.local_on(cfg))  # the skill is about local delegation only
    return cfg


def _rule_installed(agent: str, cfg: config.Config | None = None) -> bool:
    try:
        path = get(agent, cfg).rules_path()
        with open(path, encoding="utf-8") as fh:
            return BEGIN in fh.read()
    except (OSError, ValueError):
        return False


def status() -> dict:
    cfg = config.load()
    active = active_agents(cfg)
    agents = {n: {"title": a.title, "cli": a.installed(), "enabled": n in active,
                  "rule_installed": _rule_installed(n, cfg), "custom": a.custom,
                  "kind": a.kind(), "rules": a.rules_path(),
                  "mcp_file": os.path.expanduser(a.mcp_file) if a.mcp_file else ""}
              for n, a in all_agents(cfg).items()}
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


def _fill_entry(t, cmd: list[str]):
    """Server entry template: "{command}" -> program, "{args}" -> its arguments (list),
    "{argv}" -> both (list)."""
    if t == "{command}":
        return cmd[0]
    if t == "{args}":
        return cmd[1:]
    if t == "{argv}":
        return cmd
    if isinstance(t, dict):
        return {k: _fill_entry(v, cmd) for k, v in t.items()}
    if isinstance(t, list):
        return [_fill_entry(v, cmd) for v in t]
    return t


def _json_server(a: Agent, enabled: bool) -> str | None:
    """Add or remove `halo` under mcp_key in the agent's JSON config, keeping everything
    else. Returns an error message, or None. The first edit leaves a .halo-bak copy."""
    path = os.path.expanduser(a.mcp_file)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        raw = ""
    except OSError as e:
        return f"cannot read {path}: {e}"
    try:
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        entry = json.dumps({a.mcp_key: {"halo": _fill_entry(a.entry or DEFAULT_ENTRY,
                                                            _server_cmd())}}, indent=2)
        return (f"{path} is not plain JSON (comments or trailing commas?), so HALO will not "
                f"rewrite it. Add this to it yourself:\n{entry}")
    node = data
    for k in a.mcp_key.split("."):
        node = node.setdefault(k, {}) if isinstance(node, dict) else None
    servers = node
    if not isinstance(servers, dict):
        return f"{a.mcp_key} in {path} is not an object"
    if enabled:
        servers["halo"] = _fill_entry(a.entry or DEFAULT_ENTRY, _server_cmd())
    elif "halo" in servers:
        del servers["halo"]
    else:
        return None
    if raw and not os.path.exists(path + ".halo-bak"):
        shutil.copy2(path, path + ".halo-bak")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return None


DEFAULT_ENTRY = {"command": "{command}", "args": "{args}"}


def register_mcp(agent: str = "claude", cfg: config.Config | None = None) -> int:
    """Register the MCP server with one agent at user scope (all projects)."""
    a = get(agent, cfg)
    if a.tools == "cli":
        return 0  # no MCP: the rule block carries the shell commands
    if a.mcp_file:
        err = _json_server(a, True)
        if err:
            _say(f"  ✗ {a.title}: {err}")
            return 1
        return 0
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


def install(agents: list[str], mode: str = "both") -> int:
    """Connect HALO to the chosen agents and remember them for `halo mode`."""
    cfg = config.load()
    known = all_agents(cfg)
    unknown = [n for n in agents if n not in known]
    if unknown:
        raise ValueError(f"unknown agent(s) {unknown}; choose from {list(known)} "
                         f"or add one: halo agents add NAME (presets: {', '.join(PRESETS)})")
    # a pre-multi-agent install is Claude-only with no `agents` list: keep Claude in it
    base = cfg.agents or (["claude"] if _rule_installed("claude") else [])
    cfg.agents = list(dict.fromkeys([*base, *agents]))
    config.save(cfg)
    rc = 0
    for n in agents:
        rc |= register_mcp(n, cfg)
    set_mode(mode)
    for n in agents:
        a = known[n]
        how = ("`halo` commands" if a.tools == "cli" else
               f"HALO tools in {os.path.expanduser(a.mcp_file)}" if a.mcp_file else "HALO tools")
        where = (f"rule in {a.rules_path()}" if a.rules_path() else
                 "no global rules file (the tool descriptions still say when to use them)")
        _say(f"  ✓ {a.title}: {how} + {where}, mode: {mode}")
    return rc


def uninstall(agent: str, forget: bool = False) -> None:
    """Remove the rule and MCP registration from one agent; `forget` also drops a custom
    agent's spec from the config."""
    cfg = config.load()
    a = get(agent, cfg)
    write_rule("off", agent, cfg)
    if a.skill:
        write_skill(False)
    if a.mcp_file and a.tools != "cli":
        _json_server(a, False)
    elif a.cli and a.remove:
        cli = shutil.which(a.cli)
        if cli:
            subprocess.call([cli, *a.remove], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    cfg.agents = [n for n in (cfg.agents or []) if n != agent]
    if forget and a.custom:
        cfg.custom_agents.pop(agent, None)
    config.save(cfg)


def install_claude(mode: str = "both") -> int:
    return install(["claude"], mode)
