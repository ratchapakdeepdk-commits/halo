# Results so far

All numbers were measured on one machine: 2× Tesla P100 16 GB, Ollama with the CUDA
backend, Claude Code with Sonnet as the frontier model, September 2026. The samples are
small. Treat them as early evidence, not as a benchmark claim. Everything here can be
reproduced with `bench/run.py` and `bench/e2e.sh`.

## 1. Local worker on the coding benchmark (`bench/run.py`)

Task set: 7 single-file tasks with unit tests. Each configuration ran twice (14 attempts),
with up to 3–4 generate→test→fix iterations per model. Reference solutions in
`bench/reference/` pass every test.

| local model(s) | passed | est. frontier-token reduction* |
|---|---|---|
| qwen3:30b-a3b-instruct-2507 (Q4_K_M) | 9/14 | 15% |
| qwen3.6:35b-a3b (Q4_K_M) | 13/14 | 62% |
| qwen3.6:35b-a3b → gpt-oss:20b cascade | 14/14 | 74% (fallback never needed) |

\* Price-weighted estimate (output = 5× input). A failed task is charged its full direct
cost *plus* the delegation overhead.

Findings:
- **The best digest model is not the best code model.** qwen3:30b-a3b digests faster, but
  qwen3.6:35b-a3b writes much better code. HALO therefore has separate `model` and
  `code_model` settings, plus a `fallback_models` cascade.
- Near-misses are common (e.g. 1 of 3 tests failing). On failure HALO keeps the best
  attempt as `<file>.halo-draft`, so the frontier model fixes a few lines instead of
  rewriting the whole file.

## 2. Digest quality on a real 465 KB journald log (4,003 lines)

The log's only real errors were 18× `KeyError: 'llamacpp'`, which caused 17× HTTP 500.

| version | time | found the KeyError burst | last restart correct |
|---|---|---|---|
| naive map-reduce (28 chunks) | 16 min | no (reported 2× 401 and invented a "crash") | yes |
| + log compaction (4,003 → 113 templates) | 54 s | yes, count wrong | yes |
| + instructions after material + exact signal scan | 6–16 s | **yes, 18×, stable over 3 runs** | **yes** |

What mattered, in order:
1. **Deterministic compaction.** Collapsing lines that differ only in numbers shrinks
   repetitive logs 10–40×. The rare error line stops drowning in noise.
2. **Counts computed by code, not by the model.** `signals` lists error-like line groups
   with exact counts and first/last timestamps.
3. **Put the task after the material.** With the instructions above 16 KB of log, the
   model forgot them and slipped into chat-assistant mode (advice, code, emoji).

## 3. End-to-end with Claude Code (`bench/e2e.sh`)

Both arms use the same prompt, the same model (Sonnet) and the same empty MCP config. The
only difference is whether the HALO server is attached (the baseline also has the Skill
tool disabled). Cost is `total_cost_usd` as reported by Claude Code.

| task | arm | tool calls | turns | output tok | cost | answer correct |
|---|---|---|---|---|---|---|
| log question, run 1 | HALO | 4 | 5 | 948 | $0.091 | yes (the digest made one timestamp slip, which Claude caught with a grep) |
| | baseline | 9 | 10 | 1,659 | $0.110 | yes |
| log question, run 2 | HALO | 3 | 4 | 599 | $0.076 | yes |
| | baseline | 6 | 7 | 1,499 | $0.107 | yes |
| create `semver.py` (tests given) | HALO | 5 | 6 | 1,270 | $0.107 | tests pass |
| | baseline | 5 | 6 | 2,626 | $0.122 | tests pass |

So far: **17–29% lower cost on the log questions and 13% on a ~60-line file.** Output tokens
fell by 40–60% in every case. The savings come from fewer turns (every turn re-reads the
whole context) and from not generating the code.

What we learned about the agent side:
- **Adoption is the bottleneck for code.** With a vaguely worded skill, Claude wrote the
  file itself and never called `halo_code`. The skill's trigger wording decides whether
  delegation happens at all.
- **Turns cost more than bytes.** An early skill said "grep first". Claude then made 13
  grep calls instead of one digest call, which cost 3.8× more than the digest path.
- **Spec length can eat the savings.** Claude first sent a 4.3 KB spec for a 2.7 KB file.
  The tool now asks for a short spec, because the tests already pin down the behaviour.
- **MCP servers run outside the agent's sandbox.** HALO therefore restricts MCP file access
  to the project directory itself (`allowed_roots` to extend).

## 4. Model survey with `halo tune` (same machine)

The known-answer log test (2,500 lines with 9 rare `connection refused` errors, three
restarts) and three coding tasks (slugify, roman numerals, duration parser; up to 3 fix
iterations, no fallback):

| model | log answer | code | notes |
|---|---|---|---|
| qwen3:30b-a3b-instruct-2507 | correct (4.8 s) | 2/3 | |
| qwen3-coder:30b | correct (19.6 s) | 1/3 | near misses (edge cases such as `bool`) |
| qwen3:14b | correct (16.2 s) | 1/3 | 411 s for the three tasks (dense model) |
| qwen2.5-coder:14b | wrong | 1/3 | |
| qwen3:8b | correct (9.2 s) | 0/3 | |
| qwen3:4b-instruct | wrong (copied the first restart time as the last) | 0/3 | |

Other observations:
- **Downloads stall.** Ollama pulls stopped at 92–95% several times.
  `qwen3-coder:30b` needed six restarts from the shell. HALO's `pull` now detects a stall
  (no new bytes for 2 minutes) and resumes on its own.
- **Host RAM matters too.** With 16 GB of system RAM, loading a model while a download was
  running got Ollama OOM-killed. HALO retries once when a connection drops, and doing tune
  runs after downloads finish avoids the problem.

## 5. Code-checked digest claims (`checks`), 24 Sep 2026

The HALO arm used to spend 1–3 extra turns grepping to verify the digest. `halo_digest` now
returns `checks`: every quote and timestamp in the answer is looked up in the ORIGINAL file
by code. A timestamp only counts as verified when it sits on a line that matches what its
bullet is about. The result includes the line number and the line itself, so the agent can
see the evidence without a grep. The tool description says to re-check only `not_found`.

This caught a real error that plain substring checks missed. The model gave the service
restart the timestamp of the *next* line (14:10:38, Uvicorn ready) instead of systemd's
`Started` line (14:10:30). `checks.not_found` now names 14:10:30 and the correct line.

Same 465 KB journald log, same question, Sonnet, three A/B pairs:

| run | arm | tool calls | turns | output tok | input tok (read+write) | cost |
|---|---|---|---|---|---|---|
| 1 | HALO | ToolSearch, digest | 3 | 488 | 57.7k | $0.058 |
| | baseline | ToolSearch, 2× Bash | 4 | 966 | 55.3k | $0.079 |
| 2 | HALO | ToolSearch, digest | 3 | 570 | 57.4k | $0.057 |
| | baseline | ToolSearch, 2× Bash, 4× Grep | 8 | 1,930 | 95.7k | $0.080 |
| 3 | HALO | ToolSearch, digest, Bash | 4 | 814 | 78.6k | $0.067 |
| | baseline | ToolSearch, 3× Bash | 5 | 1,265 | 67.7k | $0.048* |

\* Its cache write was 5.6k instead of the usual 11–15k (the prompt cache was still warm
from run 2), so this cost is not comparable with the others.

- Before `checks`, the HALO arm took 3–4 tool calls and cost $0.076–0.091 (section 3). With
  it, 2 of 3 runs used no verification call at all ($0.057–0.058).
- In run 3, Sonnet re-grepped the three counts anyway, even though `signals` already had
  them exactly. The tool description can reduce this, but it cannot force it.
- Output tokens were 36–70% lower in every pair. **Total cost is dominated by the fixed
  context (~55k tokens per session)**, so on a log that a few greps can answer, the cost
  gap stays modest. Prompt-cache state alone can move a run's cost by ±30%. Future A/B
  runs should randomise arm order and report input tokens next to cost.

## Not yet measured

- Larger samples and other task families: multi-file refactors, docs. (Data conversion: see
  the section below.)
- Frontier models other than Sonnet, and agents other than Claude Code.
- Smaller GPUs (8–16 GB) and the untested models in the recommendation table.

## Paid second-vendor tier: Codex after the local model (25 Sep 2026)

`bench/run.py -m codex` and `--chain qwen3:30b-a3b-instruct-2507-q4_K_M codex`, 7 tasks × 1,
Codex CLI 0.153 signed in with a ChatGPT plan (default model), text-only worker as described
in the README. Token counts are from Codex's own `turn.completed` usage events.

| chain | passed | solved by local | solved by codex | local tok | codex tok | est. frontier-token reduction* |
|---|---|---|---|---|---|---|
| codex alone | 7/7 (all first try) | – | 7 | 0 | 95,267 | 56% |
| qwen3:30b-a3b → codex | 7/7 | 3 (lru, roman, slugify) | 4 (bragg took 2 calls) | 30,060 | 71,099 | 62% |

- Same idea with Claude as the paid tier (`--chain qwen3:30b-a3b… claude:haiku`, the 4 tasks
  above that qwen3:30b-a3b had failed): 4/4 passed — local solved duration and semver this
  time (sampling varies run to run), `claude -p --model haiku` fixed bragg (2 calls) and
  nginxlog (1 call); 47,776 paid-worker tokens, 20,737 local tokens.
- Codex fixed every task qwen3:30b-a3b could not, on its first call in 4 of 5 calls.
- **Each Codex call costs ~13k input tokens even for a tiny task** (its own agent prompt; ~80%
  was reported as cached). So the paid tier only makes sense *after* free local attempts, never
  first: local-first saved 25% of Codex tokens here while giving the same pass rate.
- The frontier (Claude) side is unchanged — it sees the same short result either way — so the
  frontier estimate is about the same as the local-only runs; the gain is the pass rate
  (qwen3:30b-a3b alone: 9/14) without spending frontier tokens on the failures.
- These are ChatGPT-plan quota tokens, not free: report them next to frontier savings.
- Bug found while measuring: `ledger.read()` bound its default path at import, so `bench/run.py`
  read token counts from the user's ledger instead of the bench ledger. Fixed; the numbers
  above were recomputed from `bench/results/ledger.jsonl`. The 23 Sep runs in §1 show
  per-task counts consistent with their own tasks.

## Data-conversion tasks (27 Sep 2026)

Three new single-file tasks in `bench/tasks/`: **csvjson** (RFC 4180 CSV → typed records:
None/bool/int/float/str, leading zeros stay text, quoted fields stay text), **mdtable**
(Markdown pipe tables: parse with alignment and `\|` escapes, render back padded, round-trip),
**iniconf** (INI → nested dict with DEFAULT inheritance, `${key}`/`${section:key}`
interpolation, cycle detection, coercion, no `configparser`). Reference solutions pass
(`tests/test_bench_reference.py` now checks every task's reference in CI).

`bench/run.py -k csvjson mdtable iniconf --repeat 2`, max 3 attempts, no fallback:

| model | csvjson | mdtable | iniconf | passed | est. frontier-token reduction |
|---|---|---|---|---|---|
| qwen3:30b-a3b-instruct-2507 | 0/2 | 0/2 | 0/2 | **0/6** | −24% (55,925 → 69,130) |
| qwen3.6:35b-a3b | 1/2 | 1/2 | 1/2 | **3/6** | 44% (80,807 → 45,494) |

- These tasks are much harder for local models than the first seven: qwen3:30b-a3b passed
  9/14 there and 0/6 here; qwen3.6:35b-a3b 13/14 there and 3/6 here. Every pass was on the
  first attempt — when a draft failed, the retry loop never repaired it.
- The failures are edge cases the spec states in one clause each, e.g. an escaped `\|` inside
  a cell, a table ending at the first blank line, delimiter width counting the colons.
- **A failed delegation costs the frontier more than doing it directly** (the spec goes out
  and a failed draft comes back): qwen3:30b-a3b's run is a net −24%. On this task family the
  default code model should be the stronger one, or the paid tier should follow it.
- An earlier run of the same tasks (qwen3:30b 0/6, qwen3.6 2/3, same `--repeat` minus one)
  showed two genuinely ambiguous phrases in the mdtable spec; they were clarified and the
  table above is the rerun. One more qwen3.6 csvjson run that failed after 680 s was
  interrupted and is not counted.

### Repair rounds that actually repair (28 Sep 2026)

The retry prompt used to say "fix the problem" under a tail of the check output. Changes in
`halo/tasks.py`: the model must first name the bug on a `Cause:` line; the check output keeps
an equal share of every failing test and marks the target-file lines the traceback points at
(`>>> your line N`); a repair that returns the unchanged file is re-rolled once as a fresh
attempt (different approach, original contents, temperature 0.8) without spending an attempt.

Same tasks, `--repeat 4` (12 runs per model), max 3 attempts, no fallback:

| model | csvjson | mdtable | iniconf | passed | passed after a repair | est. frontier-token reduction |
|---|---|---|---|---|---|---|
| qwen3:30b-a3b-instruct-2507 | 0/4 | 0/4 | 1/4 | **1/12** | 1 | −12% (101,655 → 113,479) |
| qwen3.6:35b-a3b | 1/4 | 2/4 | 4/4 | **7/12** | 4 | 41% (121,265 → 71,209) |

- The repair loop now contributes: 4 of qwen3.6's 7 passes came on attempt 2 or 3 (before:
  0 of 3). The overall pass rate moved less (3/6 → 7/12), and 12 runs is a small sample.
- qwen3:30b-a3b is still not usable for this family; two of its runs hit the 300 s request
  timeout and are counted as `error` (now reported as a timeout, not "cannot reach Ollama").
- csvjson remains the hardest (1/8 over both models): the type-coercion edge cases.

