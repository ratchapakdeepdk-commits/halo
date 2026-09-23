"""Settings: defaults < ~/.config/halo/config.json < environment variables."""
import json
import os
from dataclasses import dataclass, asdict

CONFIG_PATH = os.path.expanduser(
    os.environ.get("HALO_CONFIG", "~/.config/halo/config.json"))
DATA_DIR = os.path.expanduser(os.environ.get("HALO_HOME", "~/.local/share/halo"))


@dataclass
class Config:
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:30b-a3b-instruct-2507-q4_K_M"
    # Optional faster model for very large inputs (0 = never switch).
    bulk_model: str = ""
    bulk_chars: int = 50000
    num_ctx: int = 8192
    timeout: int = 300
    # Rough chars-per-token used only for budgeting and savings estimates.
    chars_per_token: float = 3.0
    max_iters: int = 3
    check_timeout: int = 120


_ENV = {
    "ollama_url": "HALO_OLLAMA_URL",
    "model": "HALO_MODEL",
    "bulk_model": "HALO_BULK_MODEL",
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
    for field, env in _ENV.items():
        if env in os.environ:
            cur = getattr(cfg, field)
            setattr(cfg, field, type(cur)(os.environ[env]))
    cfg.ollama_url = cfg.ollama_url.rstrip("/")
    return cfg


def save(cfg: Config) -> str:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(asdict(cfg), fh, indent=2)
    return CONFIG_PATH
