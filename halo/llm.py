"""Minimal Ollama client (stdlib only). Tracks token usage for the ledger."""
import json
import time
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
    try:
        d = _request(cfg, "/api/generate", payload, cfg.timeout)
    except LocalModelError as e:
        # Ollama sometimes fails a request while swapping models in/out of VRAM, or restarts
        # after running out of host RAM; one retry after a pause fixes it. An unreachable
        # server or 4xx errors are not retried.
        msg = str(e)
        if "HTTP 5" not in msg and "closed connection" not in msg and "reset" not in msg:
            raise
        time.sleep(5)
        d = _request(cfg, "/api/generate", payload, cfg.timeout)
    if usage is not None:
        usage.prompt_tokens += d.get("prompt_eval_count", 0)
        usage.output_tokens += d.get("eval_count", 0)
        usage.calls += 1
    return (d.get("response") or "").strip()


def raw(cfg: Config, path: str, payload: dict | None = None, timeout: int = 30):
    """Direct API access for doctor/setup (tags, ps, pull...)."""
    return _request(cfg, path, payload, timeout)


STALL_SECONDS = 120


def pull(cfg: Config, name: str, progress=None, attempts: int = 6) -> bool:
    """Download a model through Ollama's API, reporting progress roughly every 10%.

    Ollama downloads sometimes stall near the end; a stalled stream (no new bytes for
    STALL_SECONDS) is dropped and the pull restarted, which resumes from the partial files.
    """
    for attempt in range(1, attempts + 1):
        result = _pull_once(cfg, name, progress)
        if result is not None:
            return result
        if progress:
            progress(f"{name}: download stalled, resuming (attempt {attempt + 1}/{attempts})")
    return False


def _pull_once(cfg: Config, name: str, progress) -> bool | None:
    """True = done, False = hard error, None = stalled (retry)."""
    req = urllib.request.Request(cfg.ollama_url + "/api/pull",
                                 data=json.dumps({"model": name, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    last_pct, best, since = -10, -1, time.time()
    try:
        with urllib.request.urlopen(req, timeout=STALL_SECONDS) as r:
            for line in r:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("error"):
                    if progress:
                        progress(f"{name}: {d['error']}")
                    return False
                if d.get("status") == "success":
                    if progress:
                        progress(f"{name}: ready")
                    return True
                total, done = d.get("total"), d.get("completed")
                if total and done is not None:
                    if done > best:
                        best, since = done, time.time()
                    elif time.time() - since > STALL_SECONDS:
                        return None
                    pct = int(100 * done / total)
                    if progress and pct >= last_pct + 10:
                        last_pct = pct
                        progress(f"{name}: {pct}% of {total / 2**30:.1f} GiB")
    except TimeoutError:
        return None
    except (urllib.error.URLError, OSError) as e:
        if "timed out" in str(e):
            return None
        if progress:
            progress(f"{name}: download failed: {e}")
        return False
    return None
