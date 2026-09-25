"""Settings: defaults < ~/.config/halo/config.json < environment variables."""
import json
import os
from dataclasses import dataclass, asdict, field

CONFIG_PATH = os.path.expanduser(
    os.environ.get("HALO_CONFIG", "~/.config/halo/config.json"))
DATA_DIR = os.path.expanduser(os.environ.get("HALO_HOME", "~/.local/share/halo"))


@dataclass
class Config:
    # "hybrid" = delegate routine work to the local model; "frontier" = HALO switched off.
    mode: str = "hybrid"
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:30b-a3b-instruct-2507-q4_K_M"
    # Model for halo_code ("" = same as `model`) and further local models to try, in order,
    # when it fails - still free, and different models fail on different tasks.
    code_model: str = ""
    fallback_models: list = field(default_factory=list)
    # Optional faster model for very large inputs ("" = never switch).
    bulk_model: str = ""
    bulk_chars: int = 50000
    num_ctx: int = 8192
    timeout: int = 300
    # Rough chars-per-token used only for budgeting and savings estimates.
    chars_per_token: float = 3.0
    max_iters: int = 3
    # Frontier output tokens cost ~5x input tokens (Claude Sonnet/Opus list prices), so
    # savings are reported in input-token equivalents: in + output_weight * out.
    output_weight: float = 5.0
    check_timeout: int = 120
    # Extra directories the MCP server may read/write besides the one the agent was started
    # in. MCP servers run outside the agent's own sandbox, so HALO enforces this itself.
    allowed_roots: list = field(default_factory=list)
    # Vendor CLIs used by the paid worker tiers "codex[:m]", "claude[:m]", "gemini[:m]"
    # ("" = find on PATH). See llm.CLI_WORKERS.
    codex_bin: str = ""
    claude_bin: str = ""
    gemini_bin: str = ""
    # Agents that use HALO (MCP + rule file), set by `halo setup --agent`: claude/codex/gemini.
    agents: list = field(default_factory=list)


_ENV = {
    "ollama_url": "HALO_OLLAMA_URL",
    "model": "HALO_MODEL",
    "bulk_model": "HALO_BULK_MODEL",
    "code_model": "HALO_CODE_MODEL",
    "num_ctx": "HALO_CTX",
    "timeout": "HALO_TIMEOUT",
    "max_iters": "HALO_MAX_ITERS",
}


def load() -> Config:
    cfg = Config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
    except (OSError, ValueError):
        pass
    # OLLAMA_HOST is Ollama's own convention; honour it when HALO_OLLAMA_URL is unset.
    host = os.environ.get("OLLAMA_HOST")
    if host and "HALO_OLLAMA_URL" not in os.environ:
        cfg.ollama_url = host if host.startswith("http") else f"http://{host}"
    for name, env in _ENV.items():
        if env in os.environ:
            cur = getattr(cfg, name)
            setattr(cfg, name, type(cur)(os.environ[env]))
    if "HALO_FALLBACK_MODELS" in os.environ:
        cfg.fallback_models = [m for m in os.environ["HALO_FALLBACK_MODELS"].split(",") if m]
    cfg.ollama_url = cfg.ollama_url.rstrip("/")
    return cfg


def save(cfg: Config) -> str:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(asdict(cfg), fh, indent=2)
    return CONFIG_PATH
