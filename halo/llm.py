"""Minimal Ollama client (stdlib only). Tracks token usage for the ledger."""
import json
import urllib.error
import urllib.request

from .config import Config


class LocalModelError(RuntimeError):
    pass


class Usage:
    def __init__(self):
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.output_tokens


def _request(cfg: Config, path: str, payload: dict | None, timeout: int):
    url = cfg.ollama_url + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode(errors="replace")
        raise LocalModelError(f"Ollama HTTP {e.code} on {path}: {body}") from None
    except (urllib.error.URLError, OSError) as e:
        raise LocalModelError(f"cannot reach Ollama at {cfg.ollama_url}: {e}") from None


def generate(cfg: Config, prompt: str, *, system: str = "", model: str | None = None,
             usage: Usage | None = None, num_ctx: int | None = None,
             temperature: float | None = None, max_tokens: int | None = None) -> str:
    payload = {
        "model": model or cfg.model,
        "prompt": prompt,
        "stream": False,
        # Thinking modes make interactive delegation far too slow and some models
        # (qwen3.x) put the whole answer in a separate "thinking" field.
        "think": False,
        "options": {"num_ctx": num_ctx or cfg.num_ctx},
    }
    if system:
        payload["system"] = system
    if max_tokens:
        payload["options"]["num_predict"] = max_tokens
    if temperature is not None:
        payload["options"]["temperature"] = temperature
    d = _request(cfg, "/api/generate", payload, cfg.timeout)
    if usage is not None:
        usage.prompt_tokens += d.get("prompt_eval_count", 0)
        usage.output_tokens += d.get("eval_count", 0)
        usage.calls += 1
    return (d.get("response") or "").strip()


def raw(cfg: Config, path: str, payload: dict | None = None, timeout: int = 30):
    """Direct API access for doctor/setup (tags, ps, pull...)."""
    return _request(cfg, path, payload, timeout)
