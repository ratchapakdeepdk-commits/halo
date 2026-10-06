"""Model backends (stdlib only). Tracks token usage for the ledger.

The default backend is Ollama (free, local). A model named `<cli>` or `<cli>:<model>` for a
vendor in vendors.py runs that vendor's own command-line agent, signed in with the user's
plan, as a *text-only* worker instead:

  codex[:model]   OpenAI Codex CLI   (`codex exec`)
  claude[:model]  Claude Code        (`claude -p`)
  gemini[:model]  Gemini CLI         (`gemini -p`)

They are paid tiers meant to go *after* the free local models in `fallback_models`. Each
runs in an empty scratch directory with tools/MCP off, and its tokens are counted
separately (cloud_*), never as local.
"""
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from . import vendors
from .config import Config


class LocalModelError(RuntimeError):
    pass


class Usage:
    def __init__(self):
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.cloud_in = 0
        self.cloud_out = 0
        self.cloud_calls = 0
        # Set by every local call: Ollama stopped because the context window was full
        # (done_reason "length"), so the reply is cut off mid-way.
        self.last_truncated = False

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
    except TimeoutError:
        # Ollama answered the connect but the model is still generating (slow model, long
        # file, or another model being swapped in) - a config problem, not a dead server.
        raise LocalModelError(f"Ollama at {cfg.ollama_url} did not finish within {timeout}s "
                              f"(raise `timeout` in the HALO config for slow models)") from None
    except (urllib.error.URLError, OSError) as e:
        raise LocalModelError(f"cannot reach Ollama at {cfg.ollama_url}: {e}") from None


CLI_WORKERS = tuple(vendors.names())
RETRY_WAITS = (5, 20)  # seconds between attempts on a transient Ollama failure


def is_cloud(model: str | None) -> bool:
    return vendors.split(model)[0] is not None


WORKER_RULES = (
    "You are used as a plain text generator inside another tool. Do NOT run shell commands, "
    "read files or edit files: everything you need is in this message, and the tool that "
    "called you applies your answer itself. Reply with the answer only.\n\n"
)


def _run_vendor(cfg: Config, model: str, role: str, prompt: str, work: str,
                usage: Usage | None, timeout: int) -> str:
    """Run vendor `model` ("name[:model]") in `role` "worker" or "agent" with `work` as its
    workspace; returns its final message. Raises LocalModelError when the CLI is missing,
    fails or times out."""
    v, sub = vendors.split(model)
    exe = vendors.find(cfg, v)
    if not exe:
        raise LocalModelError(f"{v.name} CLI not found (install it and sign in first)")
    template = v.worker if role == "worker" else v.agent
    # HALO_WORKER=1: the MCP server offers no tools inside a vendor, so no delegation loops.
    env = dict(os.environ, HALO_WORKER="1")
    # The prompt file lives outside `work`, so it never shows up in a handoff's diff.
    with tempfile.TemporaryDirectory(prefix="halo-prompt-") as tmp:
        pfile = ""
        if vendors.uses_prompt_file(template):
            pfile = os.path.join(tmp, "prompt.md")
            with open(pfile, "w", encoding="utf-8") as fh:
                fh.write(prompt)
        cmd = vendors.command(template, v.model_args, bin=exe, work=work, model=sub,
                              prompt_file=pfile)
        try:
            p = subprocess.run(cmd, input="" if pfile else prompt, capture_output=True,
                               text=True, timeout=timeout, cwd=work, env=env)
        except subprocess.TimeoutExpired:
            raise LocalModelError(f"{v.name} timed out after {timeout}s") from None
    try:
        answer, tin, tout, err = vendors.parse(v, p.stdout)
    except (ValueError, KeyError, AttributeError, TypeError):
        # e.g. gemini without a login prints its error JSON on stderr only
        answer, tin, tout, err = "", 0, 0, ("unreadable output: "
                                            + (p.stderr.strip()[-300:] or "nothing on stderr"))
    if usage is not None and (tin or tout):
        usage.cloud_in += tin
        usage.cloud_out += tout
        usage.cloud_calls += 1
    # A worker must answer; an agent may finish with its edits and no closing message.
    if p.returncode != 0 or err or (role == "worker" and not answer):
        raise LocalModelError(f"{v.name} failed (exit {p.returncode}): "
                              f"{err or p.stderr.strip()[-300:] or 'empty answer'}")
    return answer.strip()


def _cli_worker(cfg: Config, prompt: str, system: str, model: str, usage: Usage | None) -> str:
    text = WORKER_RULES + (f"{system}\n\n" if system else "") + prompt
    # An empty scratch dir as the workspace: the worker only returns text, HALO writes and
    # checks files itself exactly as it does for local models.
    with tempfile.TemporaryDirectory(prefix=f"halo-{model.partition(':')[0]}-") as work:
        return _run_vendor(cfg, model, "worker", text, work, usage, cfg.timeout)


def cli_agent(cfg: Config, prompt: str, model: str, work: str, usage: Usage | None = None,
              timeout: int | None = None) -> str:
    """Run a vendor CLI as an agent (HFF handoff) inside `work`, a scratch copy of the
    project; returns its final message. Each vendor may edit files only in that copy; shell
    access follows its own sandbox."""
    if not is_cloud(model):
        raise LocalModelError(f"{model} is not an agent CLI "
                              f"(use one of {', '.join(vendors.names())})")
    return _run_vendor(cfg, model, "agent", prompt, work, usage, timeout or cfg.timeout)


def generate(cfg: Config, prompt: str, *, system: str = "", model: str | None = None,
             usage: Usage | None = None, num_ctx: int | None = None,
             temperature: float | None = None, max_tokens: int | None = None) -> str:
    if is_cloud(model or cfg.model):
        return _cli_worker(cfg, prompt, system, model or cfg.model, usage)
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
    # Ollama sometimes fails a request while swapping models in/out of VRAM (another program
    # on the same server asked for a different model), or restarts after running out of host
    # RAM. Then it either errors (5xx, dropped connection) or - worse - answers 200 with an
    # empty, unfinished object {"model": "", "done": false}, which used to count as the model
    # writing nothing. Both are retried after a pause. An unreachable server or 4xx errors are
    # not. The same unfinished object also comes back every time when the model's own numerics
    # break (laguna-xs-2.1 on Pascal P100s: NaN logits on some prompts, the runner samples an
    # empty special token over and over and Ollama drops the reply) - then retrying cannot help.
    for wait in RETRY_WAITS + (None,):
        try:
            d = _request(cfg, "/api/generate", payload, cfg.timeout)
        except LocalModelError as e:
            msg = str(e)
            if wait is None or ("HTTP 5" not in msg and "closed connection" not in msg
                                and "reset" not in msg):
                raise
        else:
            if d.get("done") is True:
                break
            if wait is None:
                raise LocalModelError(
                    f"Ollama returned an unfinished answer {len(RETRY_WAITS) + 1} times for "
                    f"{payload['model']} (another client swapping models in VRAM, or the model itself "
                    f"producing NaN/empty tokens on this prompt - try another model)")
        time.sleep(wait)
    if usage is not None:
        usage.last_truncated = d.get("done_reason") == "length"
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
