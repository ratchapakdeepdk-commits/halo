---
name: halo-delegate
description: Delegate token-heavy routine work to a free local model through the HALO MCP tools (halo_digest, halo_code) instead of pulling large inputs into context or writing boilerplate yourself. Use when about to read a long log/file/command output, or when a task needs a well-specified single file (script, parser, converter, small function, fixture) that can be checked by a command.
---

# Delegate routine work to the local model (HALO)

Goal: **keep large raw material and routine generation out of your context.**
Reading a 5,000-line log costs 5,000 lines of tokens; `halo_digest` returns 5 lines.
Writing a 150-line parser costs 150 lines of output tokens; `halo_code` returns a diffstat.

You stay in charge: plan, decide, review. The local model is a worker, not a manager.

## Decision order (do not skip)

1. **Deterministic tools first** — `grep`, `awk`, `jq`, `wc`, `sort | uniq -c`, `python -c`.
   Free and exact. Never delegate what grep can answer ("lines with ERROR", "how many").
2. **`halo_digest`** when understanding language over a large input is needed:
   what happened in this log, which function handles X in these files, summarize this doc,
   classify these records. Pass **paths**, never paste contents. Best pattern:
   grep/tail to cut first, write the slice to a temp file, digest that.
3. **`halo_code`** for a single file that is well specified and mechanically checkable:
   boilerplate, CLI glue, parsers/format converters, small pure functions, test fixtures.
   - Write the check yourself first (a small pytest file, or `python -c "...assert..."`).
     A check that only imports the module is not a check.
   - Put exact signatures, edge cases and allowed libraries in `spec`.
   - Pass the test file and any interface it must match as `context_files`.
   - `passed` → trust the tests, optionally skim the file. `failed`/`escalated` → the file
     was restored; use `last_check_output` to do it yourself — do not retry blindly.
4. **Do it yourself** for design, multi-file changes, debugging subtle issues,
   security-sensitive code, and anything whose correctness cannot be checked by a command.

## Trust rules

- Digest output is a lead, not a fact: confirm critical values with a targeted read/grep.
- Numbers from the local model's own reasoning are unverified. Numbers must come from
  code that ran or from the source material.
- If the local model escalates, that is the system working — take over.

`halo_stats` shows delegated tasks and estimated tokens saved.
