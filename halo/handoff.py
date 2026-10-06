"""HFF - hybrid frontier-frontier: hand a whole sub-task to another vendor's agent.

The controlling agent (say Claude Code) gives a task, a *sector* (the files or directories
the other agent may change) and a check command. HALO copies the project to a scratch
directory, runs the other agent (Codex, Gemini or Claude CLI on the user's own plan) there as
a real agent, runs the check itself, gives it repair rounds, and then copies back only the
changes inside the sector. The controller reads a short report instead of doing the work,
so the work is paid from the other vendor's quota.

Safety comes from the copy, not from trusting the agent: it never sees the real directory,
changes outside the sector are dropped (and reported), and a file the controller edited in
the meantime is never overwritten.
"""
import difflib
import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
import time

from . import llm, tasks
from .config import Config, DATA_DIR

# Not copied: VCS data, caches and dependency trees. Dependency trees are linked instead so
# a check like `venv/bin/python -m pytest` still works in the copy; they are never compared.
SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".tox", ".cache", "dist", "build", ".halo"}
LINK_DIRS = {"node_modules", "venv", ".venv", "env"}
# Never shown to another vendor: secrets stay on this machine (a check that needs them fails).
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12",
                "credentials*.json", ".netrc", ".npmrc", ".pypirc")
PATCH_DIR = os.path.join(DATA_DIR, "handoffs")  # patches that were not applied
MAX_FILES = 5000
MAX_BYTES = 200 * 1024 * 1024
# diff_mode "auto": the diff comes back inline when it is at most this long, so the agent in
# charge can review the work in the same turn instead of opening every file (more turns).
AUTO_DIFF_CHARS = 16000
MAX_TEXT = 2 * 1024 * 1024  # larger files are compared by hash but never diffed

AGENT_RULES = """You are working on a task handed over by another AI agent through HALO.
You are inside a scratch copy of the project; HALO reviews your changes afterwards.

Rules:
- You may create, edit or delete files ONLY inside this sector: {sector}
  Changes anywhere else are thrown away.
- Do not ask questions; nobody will answer. If the task is impossible as specified, change
  nothing and say why in your final message, starting with "#ESCALATE:".
- HALO runs this check from the project root afterwards and it must exit 0:
    {check}
- Finish with a short summary (max 8 lines) of what you changed and anything the other
  agent must know. Do not paste whole files into it.

Task:
{task}
"""

REPAIR = """
Your previous round did not pass the check. Check output:
```
{feedback}
```
The project copy still holds your previous changes. Fix the cause, then summarise again.
"""


def _skip(name: str) -> bool:
    return name in SKIP_DIRS or name.endswith(".egg-info")


def _walk(root: str):
    """Relative paths of regular files, minus skipped and linked directories."""
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not _skip(x) and x not in LINK_DIRS
                         and not os.path.islink(os.path.join(d, x)))
        for f in sorted(files):
            full = os.path.join(d, f)
            if (os.path.isfile(full) and not os.path.islink(full)
                    and not any(fnmatch.fnmatch(f, p) for p in SECRET_FILES)):
                yield os.path.relpath(full, root).replace(os.sep, "/")  # sector globs use /


def _hash(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def snapshot(root: str) -> dict:
    return {rel: _hash(os.path.join(root, rel)) for rel in _walk(root)}


def copy_project(src: str, dst: str) -> str | None:
    """Copy `src` into `dst` (which exists). Returns an error message if it is too big."""
    n = size = 0
    for rel in _walk(src):
        n += 1
        size += os.path.getsize(os.path.join(src, rel))
        if n > MAX_FILES or size > MAX_BYTES:
            return (f"{src} is too big to hand off ({MAX_FILES} files / "
                    f"{MAX_BYTES >> 20} MB max): pass a narrower workdir")
    for d, dirs, _ in os.walk(src):
        for x in list(dirs):
            if x in LINK_DIRS:
                try:
                    os.symlink(os.path.join(d, x),
                               os.path.join(dst, os.path.relpath(os.path.join(d, x), src)),
                               target_is_directory=True)
                except OSError:  # Windows without symlink rights: checks needing it fail
                    pass
        dirs[:] = [x for x in dirs if not _skip(x) and x not in LINK_DIRS
                   and not os.path.islink(os.path.join(d, x))]
        for x in dirs:
            os.makedirs(os.path.join(dst, os.path.relpath(os.path.join(d, x), src)),
                        exist_ok=True)
    for rel in _walk(src):
        shutil.copy2(os.path.join(src, rel), os.path.join(dst, rel))
    return None


def normalize_sector(sector: list[str], workdir: str) -> tuple[list[str], list[str]]:
    """Sector entries relative to workdir; absolute paths are converted. Returns (ok, bad)."""
    ok, bad = [], []
    for s in sector:
        s = s.strip()
        if not s:
            continue
        try:
            rel = os.path.relpath(s, workdir) if os.path.isabs(s) else os.path.normpath(s)
        except ValueError:  # another drive on Windows
            bad.append(s)
            continue
        rel = rel.replace(os.sep, "/")
        if rel == ".":
            ok.append(".")
        elif rel.startswith(".."):
            bad.append(s)
        else:
            ok.append(rel.rstrip("/"))
    return ok, bad


def in_sector(rel: str, sector: list[str]) -> bool:
    for s in sector:
        if s == "." or rel == s or rel.startswith(s + "/") or fnmatch.fnmatch(rel, s):
            return True
    return False


def _read_text(path: str) -> list[str] | None:
    try:
        if os.path.getsize(path) > MAX_TEXT:
            return None
        with open(path, encoding="utf-8") as fh:
            return fh.read().splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None


def _diff(old_root: str, new_root: str, rels: list[str]) -> tuple[str, list[str]]:
    """Unified diff of `rels` plus one diffstat line per file."""
    out, stat = [], []
    for rel in rels:
        a, b = os.path.join(old_root, rel), os.path.join(new_root, rel)
        old = _read_text(a) if os.path.exists(a) else []
        new = _read_text(b) if os.path.exists(b) else []
        if old is None or new is None:
            stat.append(f"{rel}: binary or large file changed")
            continue
        d = list(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        out.extend(d)
        add = sum(1 for l in d if l.startswith("+") and not l.startswith("+++"))
        rem = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
        what = "new" if not os.path.exists(a) else "deleted" if not os.path.exists(b) else ""
        stat.append(f"{rel}: +{add} -{rem}" + (f" ({what})" if what else ""))
    return "".join(out), stat


def handoff(cfg: Config, task: str, sector: list[str], *, workdir: str = ".",
            check: str = "", agent: str | None = None, rounds: int | None = None,
            apply: bool = True, diff_mode: str = "auto") -> dict:
    """Run `agent` on `task` in a copy of `workdir`; bring back the changes inside `sector`."""
    t0 = time.time()
    usage = llm.Usage()
    agent = agent or cfg.handoff_agent
    rounds = max(1, rounds or cfg.handoff_rounds)
    workdir = os.path.realpath(os.path.expanduser(workdir))
    if not os.path.isdir(workdir):
        return {"status": "error", "error": f"workdir {workdir} is not a directory"}
    if not llm.is_cloud(agent):
        return {"status": "error",
                "error": f"{agent} is not a vendor agent: use one of "
                         f"{', '.join(llm.CLI_WORKERS)} (optionally :model) "
                         f"(local models take single files through halo_code)"}
    sector, bad = normalize_sector(sector or [], workdir)
    if bad or not sector:
        return {"status": "error", "error": f"sector must name paths inside {workdir}"
                                            + (f"; outside: {bad}" if bad else "")}

    scratch = tempfile.mkdtemp(prefix="halo-handoff-")
    base, proj = os.path.join(scratch, "base"), os.path.join(scratch, "proj")
    try:
        os.makedirs(base)
        os.makedirs(proj)
        err = copy_project(workdir, base) or copy_project(workdir, proj)
        if err:
            return {"status": "error", "error": err}
        before = snapshot(base)

        status, reason, summary, out, feedback, done = "failed", "", "", "", "", 0
        for done in range(1, rounds + 1):
            prompt = AGENT_RULES.format(sector=", ".join(sector), task=task,
                                        check=check or "(none - HALO only reviews the diff)")
            if feedback:
                prompt += REPAIR.format(feedback=feedback)
            try:
                summary = llm.cli_agent(cfg, prompt, agent, proj, usage, cfg.handoff_timeout)
            except llm.LocalModelError as e:
                status, reason = "error", str(e)
                break
            esc = tasks._escalation(summary)
            if esc:
                status, reason = "escalated", esc
                break
            if not check:
                status = "done"
                break
            ok, out = tasks._run_check(check, proj, cfg.check_timeout)
            if ok:
                status = "passed"
                break
            feedback = tasks.check_feedback(out, "", "")

        after = snapshot(proj)
        changed = sorted(r for r in set(before) | set(after) if before.get(r) != after.get(r))
        inside = [r for r in changed if in_sector(r, sector)]
        outside = [r for r in changed if r not in inside]
        diff, stat = _diff(base, proj, inside)

        # Files the controller changed in the real directory while the agent was working.
        conflicts = [r for r in inside
                     if (_hash(os.path.join(workdir, r))
                         if os.path.isfile(os.path.join(workdir, r)) else None) != before.get(r)]
        applied = False
        if apply and status in ("passed", "done") and inside and not conflicts:
            for r in inside:
                src, dst = os.path.join(proj, r), os.path.join(workdir, r)
                if os.path.exists(src):
                    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                    shutil.copy2(src, dst)
                elif os.path.exists(dst):
                    os.remove(dst)
            applied = True
        patch = None
        if inside and not applied and diff:
            os.makedirs(PATCH_DIR, exist_ok=True)
            fd, patch = tempfile.mkstemp(dir=PATCH_DIR, suffix=".patch",
                                         prefix=time.strftime("%Y%m%d-%H%M%S-"))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(diff)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if status == "failed" and not reason:
        reason = f"check still failing after {done} round(s)"
    if status in ("passed", "done") and not inside:
        status, reason = "failed", "the agent changed nothing inside the sector"
    result = {"status": status, "agent": agent, "rounds": done, "sector": sector,
              "applied": applied, "diffstat": stat, "agent_summary": summary[-1500:]}
    if reason:
        result["reason"] = reason
    if outside:
        result["dropped_outside_sector"] = outside[:20]
    if conflicts:
        result["conflicts"] = conflicts
        result["note"] = ("you changed these files while the agent worked, so nothing was "
                          "applied; apply the patch by hand")
    if patch:
        result["patch"] = patch
    if check and out and status != "passed":
        result["last_check_output"] = tasks._tail(out, lines=15, chars=1200)
    elif check and status == "passed":
        result["check_summary"] = tasks._tail(out, lines=3, chars=300)
    if diff and (diff_mode == "full" or diff_mode == "auto" and len(diff) <= AUTO_DIFF_CHARS):
        result["diff"] = diff[:20000]
    elif diff and diff_mode == "auto":
        result["diff_omitted"] = (f"{len(diff)} chars; review the changed files or call again "
                                  f"with full_diff")

    # Done directly, the controller would read the files it changes and write the new ones;
    # via HALO it writes the task and reads this report. Conservative: exploration is free.
    new_chars = sum(len(l) for l in diff.splitlines() if l.startswith("+"))
    tasks._record(cfg, "handoff", status, agent, usage,
                      {"direct_in": len(diff) + len(out), "direct_out": new_chars,
                       "halo_in": len(json.dumps(result)), "halo_out": len(task) + len(check)},
                      t0, {"rounds": done})
    result["seconds"] = round(time.time() - t0, 1)
    return result
