"""Vendor CLIs (Codex, Claude Code, Gemini CLI, and any the user adds) described as data.

HALO runs a vendor in two ways: as a text-only *worker* (a paid tier after the local models,
and the council) and as an *agent* that edits a scratch copy of the project (HFF handoff).
Both are command templates. In each element of a template these are filled in:

  {bin}          the CLI: `bin` of a user worker or the `<name>_bin` config value, else
                 `name` on PATH
  {work}         the workspace: an empty scratch dir (worker) or the project copy (agent)
  {model}        the part after "name:" in "name:model"
  {prompt_file}  a file holding the prompt; without it the prompt goes on stdin

and the element "{model_args}" becomes `model_args` when a model is named, or nothing.
`worker_env` / `agent_env` add environment variables for that role (same placeholders).
`run_check: true` (opt-in) lets the agent run the handoff's check command itself: then
`check_env` is added over `agent_env`, with {check} (the command) and {check_json} (the
command escaped for a JSON string) filled in. That means running code the model wrote,
with no sandbox around it beyond the CLI's own permission rules.
`output` names how stdout is read: "codex", "claude", "gemini" (their own formats), "text"
(all of stdout is the answer, tokens unknown), "json" (one document) or "jsonl" (one event
per line), the last two read through dotted `paths`:

  answer      the final message; with jsonl the last line that has it wins
  tokens_in   one path or a list of paths, summed over every line
  tokens_out  likewise
  error       any line that has it makes the run fail with its value

A worker can instead be an OpenAI-compatible HTTP API (`api`: the base URL ending in /v1,
`key_env` or `key_file` for the key, `model` as default): text only, so it can be a fallback
tier or a council member but never a handoff agent. The key itself is never stored in the
config, and is only sent over https (or plain http to this machine).

Users add workers in the config under `custom_workers` ({name: spec}, see from_spec) or
with `halo workers add`. HALO cannot sandbox those: whatever the CLI's own permissions allow
runs as the user, inside a scratch copy of the project.
"""
import ipaddress
import json
import os
import re
import shutil
import urllib.parse
from dataclasses import dataclass, field

PLACEHOLDERS = ("{bin}", "{work}", "{model}", "{prompt_file}", "{check}", "{check_json}")
OUTPUTS = ("codex", "claude", "gemini", "text", "json", "jsonl")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    bin: str = ""        # path of a user worker's CLI ("" = `name` on PATH)
    worker_env: dict = field(default_factory=dict)
    agent_env: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)  # json/jsonl: answer, tokens_in/out, error
    custom: bool = False
    api: str = ""        # OpenAI-compatible base URL: an HTTP worker instead of a CLI
    key_env: str = ""
    key_file: str = ""
    default_model: str = ""
    run_check: bool = False   # the agent may run the handoff's check itself (opt-in)
    check_env: dict = field(default_factory=dict)


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


# Ready-made specs: `halo workers add opencode` copies one into the config, where the user
# can still change it.
PRESETS = {
    # OpenAI-compatible APIs. Model names change often, so pick one with "name:model" (or
    # set `model`); deepseek-chat is DeepSeek's long-lived alias.
    "deepseek": {"label": "DeepSeek API", "api": "https://api.deepseek.com/v1",
                 "key_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
    "openrouter": {"label": "OpenRouter API", "api": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY"},
    # opencode (npm install -g opencode-ai): model is "provider/model", e.g. a provider for
    # Ollama or the HALO local API set up in opencode's own config. Its permissions come
    # from OPENCODE_CONFIG_CONTENT: the worker may not edit or run anything; the agent edits
    # without asking (--auto) but shell, web and directories outside {work} stay denied -
    # HALO runs the check itself. --dir because opencode otherwise takes its project root
    # from $PWD (seen: it edited the directory HALO was started from).
    # (opencode's hosted free tier refuses any config override, so it cannot be used here.)
    "opencode": {
        "label": "opencode",
        "install": "npm install -g opencode-ai",
        "worker": ["{bin}", "run", "--format", "json", "--dir", "{work}", "{model_args}"],
        "agent": ["{bin}", "run", "--format", "json", "--auto", "--dir", "{work}",
                  "{model_args}"],
        "model_args": ["-m", "{model}"],
        "worker_env": {"OPENCODE_CONFIG_CONTENT":
                       '{"permission":{"edit":"deny","bash":"deny","webfetch":"deny",'
                       '"external_directory":"deny"}}'},
        "agent_env": {"OPENCODE_CONFIG_CONTENT":
                      '{"permission":{"bash":"deny","webfetch":"deny",'
                      '"external_directory":"deny"}}'},
        # With run_check: shell stays denied except the exact check command (opencode
        # matches the whole command, so `check; rm -rf x` or `check 2>&1` is refused).
        "run_check": False,
        "check_env": {"OPENCODE_CONFIG_CONTENT":
                      '{"permission":{"bash":{"*":"deny","{check_json}":"allow"},'
                      '"webfetch":"deny","external_directory":"deny"}}'},
        "output": "jsonl",
        "paths": {"answer": "part.text",
                  "tokens_in": ["part.tokens.input", "part.tokens.cache.read"],
                  "tokens_out": ["part.tokens.output", "part.tokens.reasoning"],
                  "error": "error"},
    },
}


def from_spec(name: str, spec: dict) -> Vendor:
    """A user worker from its config spec; raises ValueError naming what is wrong."""
    if not NAME_RE.match(name or ""):
        raise ValueError(f"name {name!r}: lowercase letters, digits, - and _ only")
    if name in BUILTIN:
        raise ValueError(f"{name} is built in and cannot be redefined")
    if not isinstance(spec, dict):
        raise ValueError(f"{name}: the spec must be an object")
    if spec.get("api"):
        return _api_spec(name, spec)
    known = {"label", "install", "bin", "worker", "agent", "model_args", "worker_env",
             "agent_env", "output", "paths", "run_check", "check_env"}
    extra = set(spec) - known
    if extra:
        raise ValueError(f"{name}: unknown keys {sorted(extra)} (allowed: {sorted(known)})")

    def argv(key):
        v = spec.get(key) or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{name}.{key} must be a list of strings")
        return tuple(v)

    def env(key):
        v = spec.get(key) or {}
        if not isinstance(v, dict) or not all(isinstance(x, str) for x in v.values()):
            raise ValueError(f"{name}.{key} must map names to strings")
        return dict(v)

    worker, agent = argv("worker"), argv("agent")
    if not worker and not agent:
        raise ValueError(f"{name}: give a `worker` command, an `agent` command or both")
    run_check = spec.get("run_check", False)
    if not isinstance(run_check, bool):
        raise ValueError(f"{name}.run_check must be true or false")
    if run_check and not (agent and spec.get("check_env")):
        raise ValueError(f"{name}: run_check needs an agent command and a check_env that "
                         f"allows exactly {{check}} in the CLI's own permission rules")
    output = spec.get("output", "text")
    if output not in OUTPUTS:
        raise ValueError(f"{name}.output must be one of {', '.join(OUTPUTS)}")
    paths = spec.get("paths") or {}
    if output in ("json", "jsonl") and not paths.get("answer"):
        raise ValueError(f"{name}: output {output} needs paths.answer (e.g. \"result\")")
    return Vendor(name, str(spec.get("label") or name), worker=worker, agent=agent,
                  model_args=argv("model_args"), output=output,
                  install=str(spec.get("install") or ""), bin=str(spec.get("bin") or ""),
                  worker_env=env("worker_env"), agent_env=env("agent_env"),
                  paths=dict(paths), custom=True, run_check=run_check,
                  check_env=env("check_env"))


def _local_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _api_spec(name: str, spec: dict) -> Vendor:
    known = {"label", "api", "key_env", "key_file", "model"}
    extra = set(spec) - known
    if extra:
        raise ValueError(f"{name}: unknown keys {sorted(extra)} for an API worker "
                         f"(allowed: {sorted(known)})")
    url = urllib.parse.urlparse(str(spec["api"]))
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError(f"{name}.api must be an http(s) URL such as https://host/v1")
    if url.scheme == "http" and not _local_host(url.hostname):
        raise ValueError(f"{name}.api: plain http only to this machine - the key would "
                         f"travel unencrypted")
    key_env, key_file = str(spec.get("key_env") or ""), str(spec.get("key_file") or "")
    if key_env and not ENV_RE.match(key_env):
        raise ValueError(f"{name}.key_env must be an environment variable name")
    if key_env and key_file:
        raise ValueError(f"{name}: give key_env or key_file, not both")
    return Vendor(name, str(spec.get("label") or name), worker=(), agent=(), model_args=(),
                  output="api", api=str(spec["api"]).rstrip("/"), key_env=key_env,
                  key_file=key_file, default_model=str(spec.get("model") or ""), custom=True)


def api_key(v: Vendor) -> str | None:
    """The key for an API worker: "" when it needs none, None when it is missing."""
    if v.key_env:
        return os.environ.get(v.key_env) or None
    if v.key_file:
        try:
            with open(os.path.expanduser(v.key_file), encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None
    return ""


def build_spec(name: str, *, spec: dict | None = None, api: str = "", key_env: str = "",
               key_file: str = "", model: str = "", bin: str = "",
               run_check: bool | None = None) -> dict:
    """The config spec for `halo workers add` and the GUI: `spec` as given, else an API from
    `api`, else the preset called `name`; the other arguments override. The CLI is looked up
    now and stored as an absolute path, so agents with another PATH still find it. Raises
    ValueError when the result is not a valid worker."""
    if name in BUILTIN:
        raise ValueError(f"{name} is built in already")
    if spec is not None:
        spec = json.loads(json.dumps(spec))
    elif api:
        spec = {"api": api}
    elif name in PRESETS:
        spec = json.loads(json.dumps(PRESETS[name]))
    else:
        raise ValueError(f"no preset for {name}: give its commands as a spec, or an API URL "
                         f"for an OpenAI-compatible API (presets: {', '.join(PRESETS)})")
    if not isinstance(spec, dict):
        raise ValueError(f"{name}: the spec must be an object")
    for key, val in (("key_env", key_env), ("key_file", key_file), ("model", model)):
        if val:
            spec[key] = val
    if key_env or key_file:  # one replaces the other, e.g. a preset's key_env
        spec.pop("key_file" if key_env else "key_env", None)
    if run_check is not None:
        spec["run_check"] = run_check
    if not spec.get("api"):
        exe = bin or spec.get("bin") or shutil.which(name)
        if exe:
            path = os.path.expanduser(exe)
            spec["bin"] = os.path.abspath(path) if os.sep in path or "/" in path else (
                shutil.which(path) or path)
    from_spec(name, spec)
    return spec


def remove(cfg, name: str) -> bool:
    """Drop a user worker and every place the config uses it; False if it is not one."""
    if name not in (cfg.custom_workers or {}):
        return False
    del cfg.custom_workers[name]
    mine = lambda m: m.partition(":")[0] == name  # noqa: E731
    cfg.fallback_models = [m for m in cfg.fallback_models if not mine(m)]
    cfg.council_models = [m for m in cfg.council_models if not mine(m)]
    if mine(cfg.handoff_agent):
        cfg.handoff_agent = type(cfg)().handoff_agent
    return True


def check_allowed(check: str) -> bool:
    """A check an agent may run itself: one plain command line. Glob characters would
    widen a CLI's permission pattern beyond this exact command."""
    return bool(check.strip()) and not any(c in check for c in "*?[]\n\r")


def custom(cfg) -> dict:
    """The user's workers that are valid; a broken spec is skipped (see `problems`)."""
    out = {}
    for name, spec in (getattr(cfg, "custom_workers", None) or {}).items():
        try:
            out[name] = from_spec(name, spec)
        except ValueError:
            pass
    return out


def problems(cfg) -> list[str]:
    errs = []
    for name, spec in (getattr(cfg, "custom_workers", None) or {}).items():
        try:
            from_spec(name, spec)
        except ValueError as e:
            errs.append(str(e))
    return errs


def all_vendors(cfg=None) -> dict:
    return {**BUILTIN, **(custom(cfg) if cfg is not None else {})}


def names(cfg=None) -> list[str]:
    return list(all_vendors(cfg))


def get(name: str, cfg=None) -> Vendor | None:
    return all_vendors(cfg).get(name)


def split(model: str | None, cfg=None) -> tuple[Vendor | None, str]:
    """"codex:gpt-5" -> (codex vendor, "gpt-5"); a local model -> (None, "")."""
    name, _, sub = (model or "").partition(":")
    v = get(name, cfg)
    return (v, sub) if v else (None, "")


def find(cfg, v: Vendor) -> str | None:
    """The CLI's path: a user worker's `bin`, the `<name>_bin` config value, else `name`
    on PATH."""
    return shutil.which(v.bin or getattr(cfg, f"{v.name}_bin", "") or v.name)


def ready(cfg, v: Vendor) -> bool:
    """CLI found, or the API worker's key available."""
    return api_key(v) is not None if v.api else bool(find(cfg, v))


def status(cfg) -> list[dict]:
    return [{"name": v.name, "label": v.label, "brand": v.brand or v.label,
             "install": v.install or (f"set {v.key_env}" if v.key_env else
                                      f"put the key in {v.key_file}" if v.key_file else ""),
             "installed": ready(cfg, v), "custom": v.custom, "api": v.api,
             "model": v.default_model, "worker": bool(v.worker or v.api),
             "agent": bool(v.agent), "run_check": v.run_check}
            for v in all_vendors(cfg).values()]


def command(template: tuple, model_args: tuple, *, bin: str, work: str, model: str,
            prompt_file: str = "", check: str = "") -> list[str]:
    vals = values(bin=bin, work=work, model=model, prompt_file=prompt_file, check=check)
    out = []
    for part in template:
        if part == "{model_args}":
            out += [fill(a, vals) for a in model_args] if model else []
        else:
            out.append(fill(part, vals))
    return out


def values(*, bin: str, work: str, model: str, prompt_file: str = "", check: str = "") -> dict:
    return {"bin": bin, "work": work, "model": model, "prompt_file": prompt_file,
            "check": check, "check_json": json.dumps(check)[1:-1]}


def fill(text: str, vals: dict) -> str:
    # Plain replacement, not str.format: user commands and env values carry JSON braces.
    for k, v in vals.items():
        text = text.replace("{" + k + "}", v)
    return text


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

_MISSING = object()


def _dig(obj, path: str):
    for key in path.split("."):
        if isinstance(obj, dict) and key in obj:
            obj = obj[key]
        elif isinstance(obj, list) and key.isdigit() and int(key) < len(obj):
            obj = obj[int(key)]
        else:
            return _MISSING
    return obj


def _parse_paths(paths: dict, docs: list) -> tuple[str, int, int, str]:
    def total(key):
        ps = paths.get(key) or []
        ps = [ps] if isinstance(ps, str) else ps
        n = 0
        for d in docs:
            for p in ps:
                v = _dig(d, p)
                n += v if isinstance(v, int) and not isinstance(v, bool) else 0
        return n

    answer, err = "", ""
    for d in docs:
        v = _dig(d, paths["answer"])
        if isinstance(v, str) and v.strip():
            answer = v
        if paths.get("error"):
            e = _dig(d, paths["error"])
            if e is not _MISSING and e not in (None, "", False):
                err = (e if isinstance(e, str) else json.dumps(e))[:300]
    return answer, total("tokens_in"), total("tokens_out"), err


def parse(v: Vendor, out: str) -> tuple[str, int, int, str]:
    if v.output == "json":
        return _parse_paths(v.paths, [json.loads(out[out.index("{"):])])
    if v.output == "jsonl":
        docs = []
        for line in out.splitlines():
            try:
                docs.append(json.loads(line))
            except ValueError:
                continue  # progress text between events
        if not docs:
            raise ValueError("no JSON lines")
        return _parse_paths(v.paths, docs)
    return PARSERS[v.output](out)
