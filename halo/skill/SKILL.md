---
name: halo-delegate
description: Use BEFORE writing any new self-contained file that tests or a command can check (e.g. "create X.py so the tests pass", a parser, converter, script, small module, fixture) - delegate it to the free local model with halo_code instead of generating it yourself. Also use BEFORE reading or grepping through any large log/file/command output - one halo_digest call returns the answer with exact counts. Saves frontier output tokens and turns.
---

# Delegate routine work to the local model (HALO)

Goal: **keep large raw material and routine generation out of your context.**
Reading a 5,000-line log costs 5,000 lines of tokens; `halo_digest` returns 5 lines.
Writing a 150-line parser costs 150 lines of output tokens; `halo_code` returns a diffstat.

You stay in charge: plan, decide, review. The local model is a worker, not a manager.

## The cost model you are optimising

Every tool call re-sends your whole context. Ten small grep calls on a 40k-token
context cost ~400k tokens. **Fewer turns beat smaller turns.** One `halo_digest`
call that answers the question, plus one targeted check, is the cheap path.

## Decision order

1. **A single deterministic command answers it exactly** (`grep -c`, `wc -l`, `jq`,
   one-line `python -c`)? Run that one command. Do not start a grep-exploration series.
2. **Open-ended question about a large input** (what happened in this log, which
   errors and how often, what does this dir of files do, summarise this doc):
   call **`halo_digest` first** with file paths. For logs it already does the
   deterministic part for you: repeated lines are collapsed and `signals` lists
   error/warning lines with **exact, code-computed counts and first/last timestamps**.
   `signals` and `checks.verified` are code-checked against the file (with line numbers):
   do not re-grep those. Grep only a critical claim listed in `checks.not_found`.
3. **`halo_code`** for a single, well-specified, mechanically checkable file:
   boilerplate, simple format converters, CLI glue, small pure functions, fixtures.
   Not logic-dense files (expression parsers, cron, Markdown: many interacting rules):
   the local model fails those and the round trip costs ~20% more than writing it.
   - Write the check first (small test file or `python -c "...assert..."`); an
     import-only check is not a check. Pass the test file as `context_files` and keep
     `spec` SHORT (only what the tests don't show: signatures, allowed libraries).
     Your spec is output tokens - if it is as long as the code, just write the code.
   - `passed` → trust the tests, optionally skim. `failed` → if a `.halo-draft`
     exists and is close (its line count is in the result), fix it; if it is much
     longer than what you would write, rewrite; `escalated`
     → do it yourself.
   - For a 10-line function just write it yourself.
4. **Do it yourself**: design, multi-file changes, subtle debugging,
   security-sensitive code, anything a command cannot check.
5. **Handing a chunk to another vendor** (only when the user asks, e.g. "let Codex do the
   parser package"): `halo_handoff` with `task`, a narrow `sector` (dirs/globs it may change)
   and a `check`. It runs that vendor's agent in a copy of the project on the user's plan for
   that vendor and applies only in-sector changes. `passed` → review the inline `diff` for
   what the tests miss (empty inputs, loose parsing, rounding) - measured reviews found real
   bugs every time; `failed` → read `last_check_output`, fix or re-hand-off; `conflicts` →
   apply the patch by hand. Measured: ~110 lines of new code = break-even, ~175 lines = -29%
   Claude cost including the review.

## Trust rules

- Digest prose is a lead, not a fact; `checks.not_found` tells you which parts are unverified.
- Numbers from the local model's own reasoning are unverified. Numbers must come from
  code that ran or from the source material.
- If the local model escalates, that is the system working — take over.

`halo_stats` shows delegated tasks and estimated tokens saved. `halo_council` asks other vendors
for a second opinion (no file changes).
