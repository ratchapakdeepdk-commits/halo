"""The three delegated task types: ask, digest, code (generate-verify loop).

Every task returns a dict with a `status` field:
  ok / passed  - result is usable
  escalated    - the local model declined (said #ESCALATE) -> frontier should do it
  failed       - checks never passed within the iteration budget
  error        - infrastructure problem (Ollama down, bad path...)
"""
import difflib
import os
import re
import subprocess
import time

from . import ledger, llm
from .config import Config

ESCALATE_RE = re.compile(r"^\s*#ESCALATE\b[:\s]*(.*)", re.IGNORECASE | re.DOTALL)

WORKER_SYSTEM = (
    "You are a local worker model assisting a more capable senior model. Be concise and "
    "factual. Never invent facts, numbers, file contents or command output that are not in "
    "the material given to you. If the task needs information you do not have, or judgement "
    "beyond simple extraction/transformation, reply with exactly one line: "
    "'#ESCALATE: <short reason>' and nothing else."
)

CODE_SYSTEM = (
    "You are a careful code-writing worker controlled by a senior engineer. You write exactly "
    "ONE file. Output the complete final contents of that file inside a single fenced code "
    "block and nothing else - no explanation, no partial snippets, no '...' placeholders. "
    "If the task cannot be done correctly with the information given (unknown APIs, other "
    "files would need changes, ambiguous spec), output exactly "
    "'#ESCALATE: <one-line reason>' instead of guessing."
)

BUDGET_CHARS_PER_TOKEN = 2.5  # conservative for budgeting (non-English text is denser)
RESERVE_TOKENS = 1500
# Per-chunk notes are bounded so a chatty model cannot turn a 30-chunk digest into an hour.
NOTE_MAX_TOKENS = 400


def _escalation(text: str):
    m = ESCALATE_RE.match(text or "")
    if not m:
        return None
    reason = m.group(1).strip()
    return reason.splitlines()[0] if reason else "no reason given"


# ---------------------------------------------------------------- ask

def ask(cfg: Config, question: str, *, model: str | None = None,
        from_frontier: bool = True) -> dict:
    t0 = time.time()
    usage = llm.Usage()
    model = model or cfg.model
    try:
        answer = llm.generate(cfg, question, system=WORKER_SYSTEM, model=model, usage=usage)
    except llm.LocalModelError as e:
        return {"status": "error", "error": str(e)}
    esc = _escalation(answer)
    status = "escalated" if esc else "ok"
    q_tok = ledger.estimate_tokens(question, cfg.chars_per_token)
    a_tok = ledger.estimate_tokens(answer, cfg.chars_per_token)
    # Called from a frontier agent, a short Q&A saves almost nothing: the frontier writes
    # the question and reads the answer anyway. From the shell, the whole exchange is saved.
    returned = q_tok + a_tok if from_frontier else 0
    ledger.record("ask", status, model, usage, direct_est=q_tok + a_tok,
                  returned_est=returned, seconds=time.time() - t0)
    out = {"status": status, "model": model, "answer": answer}
    if esc:
        out["reason"] = esc
    return out


# ---------------------------------------------------------------- digest

def _chunks(text: str, size: int):
    """Split on line boundaries so logs and code are never cut mid-line."""
    buf, n = [], 0
    for line in text.splitlines(keepends=True):
        while len(line) > size:  # a single monster line (minified JSON...)
            if buf:
                yield "".join(buf)
                buf, n = [], 0
            yield line[:size]
            line = line[size:]
        if n + len(line) > size and buf:
            yield "".join(buf)
            buf, n = [], 0
        buf.append(line)
        n += len(line)
    if buf:
        yield "".join(buf)


def read_inputs(paths: list[str], text: str = "") -> tuple[str, list[str]]:
    parts, errors = [], []
    for p in paths or []:
        try:
            with open(os.path.expanduser(p), errors="replace") as fh:
                parts.append(f"===== {p} =====\n{fh.read()}")
        except OSError as e:
            errors.append(f"{p}: {e}")
    if text and text.strip():
        parts.append(text)
    return "\n\n".join(parts), errors


def digest(cfg: Config, question: str, paths: list[str] | None = None, text: str = "", *,
           model: str | None = None, max_words: int = 200, progress=None) -> dict:
    """Read a large input locally and return only what answers `question`."""
    t0 = time.time()
    usage = llm.Usage()
    body, errors = read_inputs(paths or [], text)
    if errors and not body:
        return {"status": "error", "error": "; ".join(errors)}
    if not body:
        return {"status": "error", "error": "nothing to digest (no files and no text)"}

    if model is None:
        model = cfg.bulk_model if (cfg.bulk_model and len(body) > cfg.bulk_chars) else cfg.model
    budget = int((cfg.num_ctx - RESERVE_TOKENS) * BUDGET_CHARS_PER_TOKEN)
    if budget <= 1000:
        return {"status": "error", "error": f"num_ctx {cfg.num_ctx} is too small"}
    limit = f"Answer in at most {max_words} words. Quote exact lines/values when relevant."

    try:
        pieces = list(_chunks(body, budget))
        if len(pieces) == 1:
            answer = llm.generate(cfg, f"Task: {question}\n{limit}\n\n---\n{pieces[0]}",
                                  system=WORKER_SYSTEM, model=model, usage=usage,
                                  max_tokens=max_words * 4)
        else:
            notes = []
            for i, piece in enumerate(pieces, 1):
                if progress:
                    progress(f"part {i}/{len(pieces)}")
                note = llm.generate(
                    cfg,
                    f"This is part {i} of {len(pieces)} of one input. Extract ONLY what is "
                    f"relevant to the task below, quoting exact lines/values. If nothing is "
                    f"relevant, reply '(nothing)'.\nTask: {question}\n\n---\n{piece}",
                    system=WORKER_SYSTEM, model=model, usage=usage, max_tokens=NOTE_MAX_TOKENS)
                if note and "(nothing)" not in note[:20] and not _escalation(note):
                    notes.append(f"[part {i}] {note}")
            if progress:
                progress(f"merging {len(notes)} relevant note(s)")
            merged = "\n\n".join(notes) or "(no part contained relevant information)"
            answer = llm.generate(
                cfg, f"Combine these notes extracted from parts of one input into a single "
                     f"answer. Remove duplicates.\nTask: {question}\n{limit}\n\n---\n{merged}",
                system=WORKER_SYSTEM, model=model, usage=usage, max_tokens=max_words * 4)
    except llm.LocalModelError as e:
        return {"status": "error", "error": str(e)}

    esc = _escalation(answer)
    status = "escalated" if esc else "ok"
    ledger.record("digest", status, model, usage,
                  direct_est=ledger.estimate_tokens(len(body) + len(answer), cfg.chars_per_token),
                  returned_est=ledger.estimate_tokens(question + answer, cfg.chars_per_token),
                  seconds=time.time() - t0,
                  extra={"input_chars": len(body), "chunks": len(pieces)})
    out = {"status": status, "model": model, "answer": answer,
           "input_chars": len(body), "chunks": len(pieces)}
    if esc:
        out["reason"] = esc
    if errors:
        out["warnings"] = errors
    return out


# ---------------------------------------------------------------- code

FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str | None:
    blocks = FENCE_RE.findall(text or "")
    if blocks:
        return max(blocks, key=len)
    stripped = (text or "").strip()
    # Unfenced reply: accept only if it does not look like prose.
    if stripped and not stripped.lower().startswith(("here", "sure", "i ", "the ")):
        return stripped + "\n"
    return None


def _inside(path: str, root: str) -> bool:
    root = os.path.realpath(root)
    return os.path.commonpath([os.path.realpath(path), root]) == root


def _run_check(cmd: str, cwd: str, timeout: int) -> tuple[bool, str]:
    # Retries rewrite the file within the same second, often with the same size, so Python's
    # mtime+size bytecode cache would silently test the *previous* attempt.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, f"check timed out after {timeout}s"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out


def _tail(text: str, lines: int = 40, chars: int = 3000) -> str:
    t = "\n".join(text.rstrip().splitlines()[-lines:])
    return t[-chars:]


def code(cfg: Config, spec: str, target: str, check: str, *, workdir: str = ".",
         context_files: list[str] | None = None, max_iters: int | None = None,
         model: str | None = None, keep_on_fail: bool = False,
         diff_mode: str = "stat") -> dict:
    """Generate-verify loop: the local model writes `target` until `check` exits 0.

    The frontier model receives only a status, a diffstat (or full diff) and the check
    summary. On failure the original file is restored and a compact escalation report is
    returned so the frontier can take over without re-reading everything.
    """
    t0 = time.time()
    usage = llm.Usage()
    model = model or cfg.model
    max_iters = max_iters or cfg.max_iters
    workdir = os.path.abspath(os.path.expanduser(workdir))
    path = target if os.path.isabs(target) else os.path.join(workdir, target)
    if not _inside(path, workdir):
        return {"status": "error", "error": f"target {target} is outside workdir {workdir}"}
    if not check.strip():
        return {"status": "error", "error": "a check command is required (tests, compiler...)"}

    existed = os.path.exists(path)
    original = ""
    if existed:
        with open(path, encoding="utf-8", errors="replace") as fh:
            original = fh.read()
    ctx_text, ctx_err = read_inputs(context_files or [])
    if ctx_err:
        return {"status": "error", "error": "; ".join(ctx_err)}
    rel = os.path.relpath(path, workdir)

    current, feedback, last_out, attempts = original, "", "", 0
    status, reason = "failed", ""
    for attempt in range(1, max_iters + 1):
        attempts = attempt
        prompt = (f"Task:\n{spec}\n\nTarget file: {rel}\n"
                  f"The following check command will be run from the project root and must "
                  f"exit with status 0:\n  {check}\n\n")
        if ctx_text:
            prompt += f"Read-only context files:\n{ctx_text}\n\n"
        prompt += (f"Current contents of {rel}:\n```\n{current}```\n" if current
                   else f"{rel} does not exist yet.\n")
        if feedback:
            prompt += (f"\nYour previous version (shown above as current contents) FAILED the "
                       f"check. Output of the check:\n```\n{feedback}\n```\n"
                       f"Fix the problem and output the complete file again.\n")
        try:
            reply = llm.generate(cfg, prompt, system=CODE_SYSTEM, model=model, usage=usage,
                                 temperature=0.2 if attempt == 1 else 0.5)
        except llm.LocalModelError as e:
            status, reason = "error", str(e)
            break
        esc = _escalation(reply)
        if esc:
            status, reason = "escalated", esc
            break
        candidate = extract_code(reply)
        if not candidate:
            feedback = "Your reply did not contain a fenced code block with the file."
            continue
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(candidate)
        current = candidate
        ok, last_out = _run_check(check, workdir, cfg.check_timeout)
        if ok:
            status = "passed"
            break
        feedback = _tail(last_out)

    if status != "passed" and not keep_on_fail:
        if existed:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
        elif os.path.exists(path):
            os.remove(path)

    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), current.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))

    result = {"status": status, "model": model, "target": rel, "attempts": attempts,
              "diffstat": f"{rel}: +{added} -{removed}"}
    if status == "passed":
        result["check_summary"] = _tail(last_out, lines=5, chars=600)
        if diff_mode == "full":
            result["diff"] = diff
    else:
        result["reason"] = reason or f"check still failing after {attempts} attempt(s)"
        if last_out:
            result["last_check_output"] = _tail(last_out, lines=25, chars=2000)
        result["file_restored"] = not keep_on_fail

    cpt = cfg.chars_per_token
    # Done directly, the frontier would read the current file, write the new one and read the
    # check output. With HALO it writes the spec and reads `result`. Context files (usually
    # tests the frontier wrote itself) cost the same either way, so they are left out; so are
    # the frontier's own retries, which makes this estimate conservative.
    direct = ledger.estimate_tokens(len(original) + len(current) + len(last_out), cpt)
    returned = ledger.estimate_tokens(spec + check + str(result), cpt)
    ledger.record("code", status, model, usage, direct_est=direct, returned_est=returned,
                  seconds=time.time() - t0, extra={"attempts": attempts})
    result["seconds"] = round(time.time() - t0, 1)
    return result
