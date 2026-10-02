"""The three delegated task types: ask, digest, code (generate-verify loop).

Every task returns a dict with a `status` field:
  ok / passed  - result is usable
  escalated    - the local model declined (said #ESCALATE) -> frontier should do it
  failed       - checks never passed within the iteration budget
  error        - infrastructure problem (Ollama down, bad path...)
"""
import difflib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from . import ledger, llm
from .config import Config

ESCALATE_RE = re.compile(r"^\s*#ESCALATE\b[:\s]*(.*)", re.IGNORECASE | re.DOTALL)

DIGEST_SYSTEM = (
    "You extract information from raw material for a senior engineer who cannot see it. "
    "Output terse plain-text bullet points containing ONLY facts present in the material: "
    "quote exact lines, values, timestamps and counts. No advice, no recommendations, no "
    "explanations of general concepts, no code, no headings, no emoji. Never guess causes "
    "that the material does not state. A line starting with '[Nx, last: ...]' stands for N "
    "similar lines (only numbers differ); always report N as the count for that line. If the material cannot answer the task, say so "
    "in one line."
)

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

# Repair rounds: a bare "fix it" made local models re-emit the same file (seen verbatim in
# the data-conversion bench); naming the cause first makes them actually look at the bug.
REPAIR_NOTE = (
    "Your previous version (shown above as current contents) FAILED the check. Check output "
    "(lines marked '>>> your line N' are the lines of your file that the traceback points "
    "at):\n```\n{feedback}\n```\n"
    "First write ONE line starting with 'Cause:' naming the exact mistake in your code that "
    "produces this output (compare expected vs actual values). Then output the complete "
    "fixed file in a single fenced code block.\n")
# Same file twice in a row = the model is stuck on its approach; restart without the draft.
RESTART_NOTE = (
    "An earlier attempt used an approach that failed these tests and could not be fixed:\n"
    "```\n{feedback}\n```\n"
    "Write the file from scratch with a DIFFERENT approach (for parsing formats prefer the "
    "standard library module made for it, e.g. csv, configparser, json, re, if the spec "
    "allows). Re-read the spec and the tests carefully; every detail in them is checked.\n")
CAUSE_RE = re.compile(r"^\s*Cause:.*(?:\n|$)", re.IGNORECASE)

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


def _record(cfg: Config, kind, status, model, usage, frontier: dict, t0: float, extra=None):
    ledger.record(kind, status, model, usage, frontier=frontier, seconds=time.time() - t0,
                  chars_per_token=cfg.chars_per_token, output_weight=cfg.output_weight,
                  extra=extra)


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
    # Directly, the frontier reads the question and writes the answer. Through HALO it writes
    # the question and reads the answer - so from an agent the saving is the output/input
    # price difference only. From the shell nothing reaches the frontier at all.
    fr = {"direct_in": len(question), "direct_out": len(answer),
          "halo_in": len(answer) if from_frontier else 0,
          "halo_out": len(question) if from_frontier else 0}
    _record(cfg, "ask", status, model, usage, fr, t0)
    out = {"status": status, "model": model, "answer": answer}
    if esc:
        out["reason"] = esc
    return out



# ---------------------------------------------------------------- council

COUNCIL_SYSTEM = (
    "You are giving an independent second opinion to another AI agent. State your conclusion "
    "first, then the key reasoning or evidence for it. Point out anything in the material that "
    "looks wrong, even if you were not asked about it. Say plainly where you are unsure. "
    "Be concise.")
COUNCIL_MAX_CHARS = 60000  # these run on the user's paid plans: keep each call bounded
COUNCIL_DEFAULT = ["codex", "gemini"]


def council_models(cfg: Config) -> list[str]:
    return list(cfg.council_models or COUNCIL_DEFAULT)


def council(cfg: Config, question: str, paths: list[str] | None = None, text: str = "", *,
            models: list[str] | None = None, from_frontier: bool = True) -> dict:
    """Ask several models (other vendors' CLIs and/or local models) the same question in
    parallel and return every answer side by side. HALO does not merge or vote: the agent
    that asked compares them, since that is a judgement a frontier model is good at."""
    t0 = time.time()
    models = models or council_models(cfg)
    material, errors = read_inputs(paths or [], text)
    if errors:
        return {"status": "error", "error": "; ".join(errors)}
    if len(material) > COUNCIL_MAX_CHARS:
        return {"status": "error",
                "error": f"material is {len(material)} chars, over {COUNCIL_MAX_CHARS}: "
                         "pass only the relevant part (or halo_digest it first)"}
    prompt = (f"<material>\n{material}\n</material>\n\nQuestion: {question}" if material
              else question)

    def one(m):
        u, t = llm.Usage(), time.time()
        try:
            ans = llm.generate(cfg, prompt, system=COUNCIL_SYSTEM, model=m, usage=u)
            r = {"model": m, "status": "ok", "answer": ans}
        except llm.LocalModelError as e:
            r = {"model": m, "status": "error", "error": str(e)}
        r["seconds"] = round(time.time() - t, 1)
        return r, u

    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        done = list(pool.map(one, models))
    total = llm.Usage()
    for _, u in done:
        for k in vars(total):
            setattr(total, k, getattr(total, k) + getattr(u, k))
    answers = [r for r, _ in done]
    n_ok = sum(r["status"] == "ok" for r in answers)
    status = "ok" if n_ok else "error"
    # Not a saving: the asking agent still reads every answer. Recorded so the cost shows up.
    read_back = sum(len(r.get("answer", "")) for r in answers)
    fr = {"direct_in": 0, "direct_out": 0,
          "halo_in": read_back if from_frontier else 0,
          "halo_out": len(question) if from_frontier else 0}
    _record(cfg, "council", status, ",".join(models), total, fr, t0)
    return {"status": status, "answered": f"{n_ok}/{len(models)}", "answers": answers}

# ---------------------------------------------------------------- digest

def _chunks(text: str, size: int):
    """Split on line boundaries so logs and code are never cut mid-line.

    Chunks never exceed `size` and are balanced (16.6 KB + 0.4 KB becomes ~8.5 KB + 8.5 KB),
    so no part is a near-empty scrap that the model answers with noise.
    """
    goal = -(-len(text) // -(-len(text) // size)) if len(text) > size else size
    buf, n = [], 0
    for line in text.splitlines(keepends=True):
        while len(line) > size:  # a single monster line (minified JSON...)
            if buf:
                yield "".join(buf)
                buf, n = [], 0
            yield line[:size]
            line = line[size:]
        if buf and (n + len(line) > size or n >= goal):
            yield "".join(buf)
            buf, n = [], 0
        buf.append(line)
        n += len(line)
    if buf:
        yield "".join(buf)


TIME_RE = re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")
TEMPLATE_RE = re.compile(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{8,}|\d+")
# Leading timestamp: syslog/journald ("Sep 23 10:20:01") or ISO-8601 ("2026-09-23T10:20:01Z").
LEAD_TS_RE = re.compile(r"^\s*(\[?(?:[A-Z][a-z]{2}\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2}"
                        r"|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?)\s*")
SIGNAL_RE = re.compile(r"error|exception|traceback|fatal|panic|fail|critical|warn|killed|"
                       r"denied|refused|timed? ?out|segfault|oom|\b5\d\d\b", re.IGNORECASE)


# "host prog[pid]: " prefix of syslog/journald lines, and traceback frame / source lines that
# merely *mention* errors (e.g. File ".../_exception_handler.py") rather than report one.
PROC_PREFIX_RE = re.compile(r"^(?:\S+\s+)?[\w.@/\-]+\[\d+\]:\s")
FRAME_RE = re.compile(r"^(?:\s|File \")")


def _is_signal(msg: str) -> bool:
    body = PROC_PREFIX_RE.sub("", msg, count=1)
    return bool(SIGNAL_RE.search(body)) and not FRAME_RE.match(body)


def _split_ts(line: str) -> tuple[str, str]:
    m = LEAD_TS_RE.match(line)
    return (m.group(1).strip("[]"), line[m.end():]) if m else ("", line)


def compact_log(text: str, max_signals: int = 15) -> tuple[str, int, int, list[dict]]:
    """Collapse lines that differ only in numbers/hex ids into one line with a count.

    Groups keep their first-occurrence order. A repeated line is shown as
    "[18x, first: <ts>, last: <ts>] <message>" so a small model sees count and recency up
    front. Also returns exact, code-computed "signals" (error/warning-like line groups with
    counts and first/last timestamps) - numbers the LLM does not have to get right.
    Returns (text, original_lines, kept_lines, signals).
    """
    lines = text.splitlines()
    groups: dict[str, list] = {}
    order = []
    for line in lines:
        ts, msg = _split_ts(line)
        key = TEMPLATE_RE.sub("#", msg)
        g = groups.get(key)
        if g:
            g[1] += 1
            g[3] = ts or g[3]
        else:
            groups[key] = [msg, 1, ts, ts, line]
            order.append(key)
    out, signals = [], []
    for key in order:
        msg, n, first, last, raw = groups[key]
        if n == 1:
            out.append(raw)
        elif first:
            out.append(f"[{n}x, first: {first}, last: {last}] {msg}")
        else:
            out.append(f"[{n}x] {msg}")
        if _is_signal(msg):
            signals.append({"count": n, "first": first, "last": last, "line": msg.strip()[:200]})
    signals.sort(key=lambda d: -d["count"])
    return "\n".join(out) + "\n", len(lines), len(order), signals[:max_signals]


CLAIM_QUOTE_RE = re.compile(r"\"([^\"\n]{6,200})\"|`([^`\n]{6,200})`|'([^'\n]{8,200})'")
FILE_HEADER_RE = re.compile(r"^===== (.+) =====$")
WORD_RE = re.compile(r"[a-z]{3,}")
# Words of a digest bullet that say nothing about WHICH log line it describes.
CLAIM_STOPWORDS = {"occurred", "times", "time", "first", "last", "the", "and", "was", "were",
                   "with", "from", "for", "that", "this", "between", "each", "line", "lines",
                   "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                   "dec", "info", "error", "warning"}


def _where(f: str, num: int) -> str:
    return f"{f}:{num}" if f else f"line {num}"


def check_claims(answer: str, raw: str, max_items: int = 8) -> dict:
    """Check the answer's quotes and timestamps against the ORIGINAL input, by code.

    Every verification grep is one more frontier turn, and turns cost more than bytes.
    A quote is verified if it occurs verbatim. A timestamp is verified only if it occurs on
    a line that matches what its bullet is about: finding "14:10:38" somewhere in the log is
    not enough (seen end-to-end: the model gave the restart the time of the next line).
    Returns {"verified": [{claim, hits, where, line}], "not_found": [str, ...]}.
    """
    claims: list[tuple[str, str, set]] = []  # (claim, kind, words of its bullet)
    seen = set()
    for bullet in answer.splitlines():
        words = set(WORD_RE.findall(bullet.lower())) - CLAIM_STOPWORDS
        found = [(q, "quote") for m in CLAIM_QUOTE_RE.finditer(bullet)
                 if len(q := next(g for g in m.groups() if g).strip().rstrip(".…").strip()) >= 6]
        found += [(t, "time") for t in TIME_RE.findall(bullet)]
        for c, kind in found:
            # A quote is checked once; a timestamp once per bullet (it may be right in one
            # bullet and borrowed in another).
            key = c if kind == "quote" else (c, bullet)
            if key not in seen:
                seen.add(key)
                claims.append((c, kind, words))
    if not claims:
        return {}
    where, fname, n = [], "", 0
    for line in raw.splitlines():
        h = FILE_HEADER_RE.match(line)
        if h:
            fname, n = h.group(1), 0
            continue
        n += 1
        where.append((fname, n, line))
    verified, missing = [], []
    line_words: dict[int, set] = {}

    def overlap(i: int, words: set) -> int:
        if i not in line_words:
            line_words[i] = set(WORD_RE.findall(where[i][2].lower()))
        return len(words & line_words[i])

    for c, kind, words in claims[:max_items]:
        hits = [i for i, w in enumerate(where) if c in w[2]]
        if not hits:
            missing.append(c)
            continue
        if kind == "time" and len(words) >= 2:
            # The lines this bullet is about = those sharing (nearly) the most words with it.
            scores = [overlap(i, words) for i in range(len(where))]
            best = max(scores)
            if best >= 2:
                about = [i for i, sc in enumerate(scores) if sc >= max(2, best - 1)]
                ok = [i for i in about if c in where[i][2]]
                if not ok:
                    f, num, line = where[about[-1]]
                    missing.append(f"{c} is not on any line matching its bullet; closest "
                                   f"match {_where(f, num)}: {line.strip()[:160]}")
                    continue
                hits = ok
        # Among equals show the LAST hit: "when did X last happen" is the usual question.
        i = max(reversed(hits), key=lambda i: overlap(i, words))
        f, num, line = where[i]
        verified.append({"claim": c, "hits": len(hits), "where": _where(f, num),
                         "line": line.strip()[:160]})
    return {"verified": verified, "not_found": missing}


def looks_like_log(text: str, sample: int = 400) -> bool:
    lines = [l for l in text.splitlines()[:sample] if l.strip()]
    return bool(lines) and sum(1 for l in lines if TIME_RE.search(l)) >= 0.5 * len(lines)


def read_inputs(paths: list[str], text: str = "") -> tuple[str, list[str]]:
    parts, errors = [], []
    for p in paths or []:
        try:
            with open(os.path.expanduser(p), encoding="utf-8", errors="replace") as fh:
                parts.append(f"===== {p} =====\n{fh.read()}")
        except OSError as e:
            errors.append(f"{p}: {e}")
    if text and text.strip():
        parts.append(text)
    return "\n\n".join(parts), errors


def _material_prompt(material: str, question: str, extra: str, part: str = "input") -> str:
    # Instructions go AFTER the material: with the task on top of 16 KB of log, a small model
    # forgets it by the time it answers and slips back into chat-assistant mode.
    return (f"<material {part}>\n{material}\n</material>\n\n"
            f"Task: {question}\n{extra}\n"
            f"Reply with terse plain-text bullet points of facts from the material only "
            f"(exact quotes, counts, timestamps). No advice, no explanations, no headings.")


def digest(cfg: Config, question: str, paths: list[str] | None = None, text: str = "", *,
           model: str | None = None, max_words: int = 200, progress=None,
           compact: str = "auto", debug: bool = False) -> dict:
    """Read a large input locally and return only what answers `question`.

    compact: "auto" collapses repetitive log lines when the input looks like a log and
    shrinks at least 2x; "on" always; "off" never (use for CSV/data where every row matters).
    """
    t0 = time.time()
    usage = llm.Usage()
    body, errors = read_inputs(paths or [], text)
    if errors and not body:
        return {"status": "error", "error": "; ".join(errors)}
    if not body:
        return {"status": "error", "error": "nothing to digest (no files and no text)"}

    raw_chars = len(body)
    raw = body
    compacted, signals = None, []
    if compact == "on" or (compact == "auto" and looks_like_log(body)):
        small, n_lines, n_kept, signals = compact_log(body)
        if compact == "on" or len(small) * 2 <= len(body):
            body = small
            compacted = f"{n_lines} lines collapsed to {n_kept} line templates"
            if signals:
                scan = "\n".join(f"- {g['count']}x  first {g['first'] or '?'}  last "
                                 f"{g['last'] or '?'}  | {g['line']}" for g in signals)
                body = (f"Exact scan of error/warning-like lines (computed by a program, "
                        f"counts are reliable):\n{scan}\n\nCompacted log:\n{body}")
            if progress:
                progress(f"compacted log: {compacted}")

    if model is None:
        model = cfg.bulk_model if (cfg.bulk_model and len(body) > cfg.bulk_chars) else cfg.model
    budget = int((cfg.num_ctx - RESERVE_TOKENS) * BUDGET_CHARS_PER_TOKEN)
    if budget <= 1000:
        return {"status": "error", "error": f"num_ctx {cfg.num_ctx} is too small"}
    limit = f"Answer in at most {max_words} words."
    notes: list[str] = []

    try:
        pieces = list(_chunks(body, budget))
        if len(pieces) == 1:
            answer = llm.generate(cfg, _material_prompt(pieces[0], question, limit),
                                  system=DIGEST_SYSTEM, model=model, usage=usage,
                                  max_tokens=max_words * 4, temperature=0.1)
        else:
            for i, piece in enumerate(pieces, 1):
                if progress:
                    progress(f"part {i}/{len(pieces)}")
                note = llm.generate(
                    cfg, _material_prompt(
                        piece, question,
                        f"This is part {i} of {len(pieces)} of a larger input; extract only "
                        f"what is relevant to the task. If nothing is, reply exactly "
                        f"'(nothing)'.", part=f"part {i} of {len(pieces)}"),
                    system=DIGEST_SYSTEM, model=model, usage=usage,
                    max_tokens=NOTE_MAX_TOKENS, temperature=0.1)
                if note and "(nothing)" not in note[:20] and not _escalation(note):
                    notes.append(f"[part {i}] {note}")
            if progress:
                progress(f"merging {len(notes)} relevant note(s)")
            merged = "\n\n".join(notes) or "(no part contained relevant information)"
            answer = llm.generate(
                cfg, _material_prompt(
                    merged, question,
                    f"The material is notes extracted, in order, from consecutive parts of "
                    f"one input. Combine them into one answer: merge duplicates, add up "
                    f"counts across parts, keep exact quotes. {limit}", part="notes"),
                system=DIGEST_SYSTEM, model=model, usage=usage, max_tokens=max_words * 4,
                temperature=0.1)
    except llm.LocalModelError as e:
        return {"status": "error", "error": str(e)}

    esc = _escalation(answer)
    status = "escalated" if esc else "ok"
    out = {"status": status, "model": model, "answer": answer,
           "input_chars": raw_chars, "chunks": len(pieces)}
    if compacted:
        out["compacted"] = compacted + " (numbers normalised; pass compact='off' for exact data)"
    if signals:
        # Code-computed, exact: the frontier can trust these counts even if the prose is off.
        out["signals"] = signals
    if not esc:
        checks = check_claims(answer, raw)
        if checks:
            out["checks"] = checks
    if debug:
        out["notes"] = notes
    if esc:
        out["reason"] = esc
    if errors:
        out["warnings"] = errors
    # Directly, the frontier reads the whole input. Through HALO it writes the question and
    # reads this result (answer + signals).
    _record(cfg, "digest", status, model, usage,
            {"direct_in": raw_chars, "direct_out": 0,
             "halo_in": len(json.dumps(out, ensure_ascii=False)), "halo_out": len(question)},
            t0, {"input_chars": raw_chars, "chunks": len(pieces), "compacted": bool(compacted)})
    return out


# ---------------------------------------------------------------- code

FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str | None:
    blocks = FENCE_RE.findall(text or "")
    if blocks:
        return max(blocks, key=len)
    stripped = CAUSE_RE.sub("", (text or "").strip(), count=1).strip()
    # Unfenced reply: accept only if it does not look like prose.
    if stripped and not stripped.lower().startswith(("here", "sure", "i ", "the ")):
        return stripped + "\n"
    return None


FAIL_COUNT_RES = [re.compile(p) for p in (
    r"failures=(\d+)", r"errors=(\d+)",            # unittest
    r"(\d+) failed", r"(\d+) errors?\b")]           # pytest


def failure_score(output: str) -> int:
    """Lower is better. Counts failing tests when the runner reports them; anything else
    (syntax error, import error, crash) ranks below any attempt that ran the tests."""
    counts = [int(m) for rx in FAIL_COUNT_RES for m in rx.findall(output)]
    return sum(counts) if counts else 10**6


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


FAIL_SPLIT_RE = re.compile(r"^(?:={20,}|_{5,} .* _{5,})$", re.MULTILINE)  # unittest / pytest
TRACE_LINE_RE = re.compile(r'^\s*File "([^"]+)", line (\d+)')


def check_feedback(output: str, path: str, source: str, chars: int = 3500) -> str:
    """Check output for the repair prompt. A plain tail cut the first failure off whenever
    tracebacks were long, and the model then kept 'fixing' only the last one. Here every
    failure block gets an equal share, and frames in the target file are annotated with the
    offending source line so the model does not have to count lines itself."""
    src_lines = source.splitlines()
    real = os.path.realpath(path)

    def annotate(block: str) -> str:
        lines, out, skip = block.splitlines(), [], False
        for i, line in enumerate(lines):
            if skip:  # the source line Python already printed, replaced by the marker
                skip = False
                continue
            out.append(line)
            m = TRACE_LINE_RE.match(line)
            if m and os.path.realpath(m.group(1)) == real and 0 < int(m.group(2)) <= len(src_lines):
                code_line = src_lines[int(m.group(2)) - 1].strip()
                if code_line:
                    out.append(f"    >>> your line {m.group(2)}: {code_line}")
                    skip = i + 1 < len(lines) and lines[i + 1].strip() == code_line
        return "\n".join(out)

    parts = [p.strip("\n") for p in FAIL_SPLIT_RE.split(output) if p.strip()]
    blocks = [p for p in parts if re.search(r"^(FAIL|ERROR)\b|Error|assert", p, re.MULTILINE)]
    if len(blocks) < 2:
        return _tail(annotate(output), chars=chars)
    summary = _tail(output, lines=3, chars=200)
    share = max(400, (chars - len(summary)) // len(blocks))
    kept = []
    for b in blocks:
        b = annotate(b.split("\n" + "-" * 70 + "\nRan ")[0])
        kept.append(b if len(b) <= share else b[:share // 2] + "\n[...]\n" + b[-share // 2:])
    return "\n\n".join(kept + [summary])


def code(cfg: Config, spec: str, target: str, check: str, *, workdir: str = ".",
         context_files: list[str] | None = None, max_iters: int | None = None,
         model: str | None = None, keep_on_fail: bool = False,
         diff_mode: str = "stat", fallback: list[str] | None = None) -> dict:
    """Generate-verify loop: the local model writes `target` until `check` exits 0.

    The frontier model receives only a status, a diffstat (or full diff) and the check
    summary. On failure the original file is restored and a compact escalation report is
    returned so the frontier can take over without re-reading everything.
    """
    t0 = time.time()
    usage = llm.Usage()
    model = model or cfg.code_model or cfg.model
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

    # Cascade: each model gets a fresh start (a broken draft tends to anchor the next model
    # to the same mistake); the best attempt across all of them is kept as a draft.
    chain = [model] + [m for m in (fallback if fallback is not None else cfg.fallback_models)
                       if m and m != model]
    current, last_out, attempts = original, "", 0
    status, reason, used = "failed", "", model
    best = None  # (failure score, candidate, check output)
    for used in chain:
        current, feedback, restart, prev, rerolled = original, "", False, None, False
        for attempt in range(1, max_iters + 1):
            attempts += 1
            reply = None
            while reply is None:
                prompt = (f"Task:\n{spec}\n\nTarget file: {rel}\n"
                          f"The following check command will be run from the project root "
                          f"and must exit with status 0:\n  {check}\n\n")
                if ctx_text:
                    prompt += f"Read-only context files:\n{ctx_text}\n\n"
                shown = original if restart else current
                prompt += (f"Current contents of {rel}:\n```\n{shown}```\n" if shown
                           else f"{rel} does not exist yet.\n")
                if feedback:
                    prompt += "\n" + (RESTART_NOTE if restart else REPAIR_NOTE).format(
                        feedback=feedback)
                try:
                    reply = llm.generate(cfg, prompt, system=CODE_SYSTEM, model=used,
                                         usage=usage, temperature=0.2 if attempt == 1 else
                                         0.8 if restart else 0.5)
                except llm.LocalModelError as e:
                    status, reason = "error", str(e)
                    break
                # Local models often answer a repair request with the unchanged file. Running
                # the check again would only burn the attempt, so start over right away
                # (once per model, so a model that always echoes cannot loop forever).
                if (feedback and not restart and not rerolled and
                        (extract_code(reply) or "").strip() == current.strip()):
                    restart, rerolled, reply = True, True, None
            if reply is None:
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
            score = failure_score(last_out)
            if best is None or score <= best[0]:
                best = (score, candidate, last_out)
            feedback = check_feedback(last_out, path, candidate)
            # A repair that did not reduce the failures (or returned the same file) means the
            # model is stuck on its approach: the next attempt starts over without the draft.
            restart = (prev is not None and not restart and
                       (candidate.strip() == prev[0] or score >= prev[1]))
            prev = (candidate.strip(), score)
        if status == "passed":
            break
        # errors (Ollama down) and escalations also move on to the next model

    if status != "passed" and best is not None:
        current, last_out = best[1], best[2]
    draft_rel = None
    if status != "passed" and not keep_on_fail:
        if best is not None:
            draft_rel = rel + ".halo-draft"
            with open(path + ".halo-draft", "w", encoding="utf-8") as fh:
                fh.write(best[1])
        if existed:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
        elif os.path.exists(path):
            os.remove(path)
    elif status != "passed" and best is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(best[1])

    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), current.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))

    result = {"status": status, "target": rel, "attempts": attempts, "model": used,
              "diffstat": f"{rel}: +{added} -{removed}"}
    if status == "passed":
        result["check_summary"] = _tail(last_out, lines=3, chars=300)
        if diff_mode == "full":
            result["diff"] = diff
    else:
        result["reason"] = reason or f"check still failing after {attempts} attempt(s)"
        if last_out:
            result["last_check_output"] = _tail(last_out, lines=15, chars=1200)
        result["file_restored"] = not keep_on_fail
        if draft_rel:
            result["draft"] = (f"{draft_rel} holds the best attempt (check output above); "
                               f"fixing it is usually cheaper than rewriting")

    # Done directly, the frontier reads the current file and the check output and WRITES the
    # new file (output tokens). With HALO it writes spec + check and reads `result`. Context
    # files (usually tests the frontier wrote itself) cost the same either way, and the
    # frontier's own retries are not counted, so the estimate is conservative.
    _record(cfg, "code", status, used, usage,
            {"direct_in": len(original) + len(last_out), "direct_out": len(current),
             "halo_in": len(json.dumps(result)), "halo_out": len(spec) + len(check) + len(rel)},
            t0, {"attempts": attempts})
    result["seconds"] = round(time.time() - t0, 1)
    return result
