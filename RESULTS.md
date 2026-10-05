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


### End-to-end A/B on the data-conversion tasks (28 Sep 2026)

`bench/e2e.sh code bench/tasks/<task>`, Claude Sonnet in charge, 2 rounds per task, HALO
code model qwen3.6:35b-a3b (fallback gpt-oss:20b). The with-halo arm always ran first.

| task · round | with HALO: output tok / turns / cost | baseline: output tok / turns / cost | tests |
|---|---|---|---|
| csvjson · 1 | 1,039 / 5 / $0.100 | 4,154 / 5 / $0.111 | both OK |
| mdtable · 1 | 1,248 / 5 / $0.104 | 8,535 / 5 / $0.175 | both OK |
| iniconf · 1 | 989 / 5 / $0.100 | 12,364 / 5 / $0.233 | both OK |
| csvjson · 2 | 1,044 / 6 / $0.106 | 4,551 / 7 / $0.132 | both OK |
| mdtable · 2 | 8,521 / 8 / $0.231 | 11,173 / 6 / $0.222 | both OK |
| iniconf · 2 | 1,165 / 6 / $0.109 | 9,263 / 4 / $0.183 | both OK |
| **total** | **13,966 / $0.749** | **50,040 / $1.055** | **12/12** |

- Success rate is unchanged (6/6 both arms): when the local draft failed (mdtable round 2)
  Claude finished the file itself, which is also the one run where HALO cost slightly more.
- Output tokens −72%, cost −29%. Cost falls less than output because the HALO arm reads
  more cached context (tool schemas + the halo_code result): 1.01M vs 0.73M cache-read tokens.
- 6 pairs, fixed arm order; given the ±30% cache swing seen before, treat the cost figure
  as indicative.

### Same A/B with the baseline first (29 Sep 2026)

The runs above always put the with-halo arm first. This rerun pins the opposite order
(`HALO_E2E_ORDER=baseline-first bench/e2e.sh code …`, same 3 tasks × 2 rounds, same models
and HALO config, Claude Code 2.1.284). `e2e.sh` now randomises the order by default and
prints input tokens.

| task · round | with HALO: output tok / turns / cost | baseline: output tok / turns / cost | tests |
|---|---|---|---|
| csvjson · 1 | 1,807 / 6 / $0.078 | 1,566 / 4 / $0.075 | both OK |
| mdtable · 1 | 2,187 / 6 / $0.085 | 1,664 / 4 / $0.048 | both OK |
| iniconf · 1 | 813 / 4 / $0.059 | 1,575 / 4 / $0.047 | both OK |
| csvjson · 2 | 732 / 4 / $0.058 | 1,396 / 4 / $0.043 | both OK |
| mdtable · 2 | 902 / 4 / $0.061 | 1,805 / 4 / $0.050 | both OK |
| iniconf · 2 | 807 / 4 / $0.059 | 1,667 / 4 / $0.048 | both OK |
| **total** | **7,248 / $0.400** | **9,673 / $0.311** | **12/12** |

- **HALO cost 29% more in this order, and more in every one of the 6 pairs.** Output tokens
  were still 25% lower, but the baseline itself was far cheaper than on 28 Sep: Sonnet wrote
  each file in one pass (1.4–1.8k output tokens, 4 turns) instead of 4.2–12.4k. What caused
  that change (order, cache, model/CLI version, sampling) is not established by these runs.
- When the baseline is that short, HALO's fixed overhead dominates: the MCP tool schemas
  and the `halo_code` result add ~4–6k cache-write and ~12k cache-read tokens per session
  (with-halo 66.9k write / 300k read vs baseline 44.4k / 180k over 6 sessions).
- The local model solved 4 of 6. In the 2 failures (csvjson and mdtable, round 1) Claude
  wrote the file itself afterwards; those are the most expensive HALO runs.
- Taken together with the section above, the 28 Sep −29% does not hold up: across both
  orders (12 pairs) HALO is $1.149 vs $1.366 baseline, and that is driven by the 28 Sep
  baselines. For single files that the frontier model can write in one pass, delegation
  does not pay; the savings shown elsewhere come from longer outputs and large inputs.
- Leak found: the baseline arm still loaded the user-scope `~/.claude/CLAUDE.md` with the
  HALO rule block (one baseline answer said "the halo tools weren't available, so I wrote
  the file myself"). This affected both this run and 28 Sep. `e2e.sh` now runs the baseline
  with `--setting-sources project,local`; it was confirmed to hide the rule. These numbers
  were collected before that fix.

### Clean rerun: random order, baseline without the HALO rule (29 Sep 2026)

`bench/e2e.sh code …` after both fixes above (random arm order; the baseline runs with
`--setting-sources project,local`, and no baseline answer mentioned HALO this time).
Same 3 tasks × 2 rounds, same models and config. Order drawn: baseline first in 4 pairs,
with-halo first in 2.

| task · round | order | with HALO: output tok / turns / cost | baseline: output tok / turns / cost | tests |
|---|---|---|---|---|
| csvjson · 1 | base first | 769 / 4 / $0.058 | 1,459 / 4 / $0.042 | both OK |
| mdtable · 1 | base first | 2,241 / 7 / $0.092 | 2,737 / 5 / $0.065 | both OK |
| iniconf · 1 | halo first | 818 / 4 / $0.059 | 2,507 / 5 / $0.060 | both OK |
| csvjson · 2 | halo first | 737 / 4 / $0.058 | 2,311 / 5 / $0.057 | both OK |
| mdtable · 2 | base first | 901 / 4 / $0.061 | 2,736 / 5 / $0.065 | both OK |
| iniconf · 2 | base first | 807 / 4 / $0.059 | 2,633 / 5 / $0.062 | both OK |
| **total** | | **6,273 / $0.388** | **14,383 / $0.351** | **12/12** |

- Output tokens −56%, cost **+10%**. HALO was cheaper in 3 pairs, dearer in 3; the one clear
  loss is mdtable · 1, where the local draft failed and Claude wrote the file itself.
- The local model solved 5 of 6.
- **A successful delegation costs a near-constant ~$0.058–0.061** (4 turns: ToolSearch, Bash,
  `halo_code`, answer). The baseline cost follows file length: $0.042–0.065 for these
  ~70–85-line files. So these files sit at the break-even point; the saving has to come from
  longer files. The rule block and the `halo_code` description now say this (under ~80 lines
  written in one pass costs about the same either way).
- Part of HALO's per-session overhead (tool schemas, ToolSearch) is paid in any session that
  has HALO attached, whether or not it delegates, so this per-task A/B is a conservative view
  of the marginal cost of one delegation.

## Long single-file tasks (5 Oct 2026)

`cron` (5-field cron parser + next run), `calc` (tokenizer + parser + evaluator with column
errors) and `mdhtml` (Markdown subset → HTML): references 82–163 lines, the size where
delegation should start to pay (previous section). `qwen3.6:35b-a3b-q4_K_M`, 3 attempts, no
fallback, 3 tasks × 2 rounds per run.

| run | passed | what the last failure looked like |
|---|---|---|
| before the fix (2 runs, 9 tasks) | 0/9 | truncated files, a file containing only `cron.py` / `mdhtml.py` |
| after the fix | 0/6 | real logic bugs: cron 1/8 tests left (dom/dow OR rule), calc column numbers, mdhtml emphasis |
| after the fence fix (below) | 0/6 | cron 1 test left (dom/dow OR, year rollover), calc error columns, mdhtml a trailing `\n` failing all 8 |
| + whitespace hint in repair feedback (below) | 0/6 | cron dom/dow OR again, calc `-2**2` sign / `sqrt` domain error, mdhtml `*i*`/`_i_` not turned into `<em>` |

Probing every Ollama call showed two HALO bugs, not model limits:
- **Context overflow.** A repair round on `cron` used 6,249 prompt + 1,943 output tokens =
  exactly `num_ctx` 8,192 (`done_reason: "length"`). The cut-off file was then run as if it
  were complete. Fix: `halo_code` sizes the window once per run from spec + tests + draft
  (rounded to a power of two, up to the new `max_ctx`, default 32,768; short tasks keep
  8,192 so the model is not reloaded), and a cut-off reply is retried with a doubled window
  instead of being run.
- **Lone file name accepted as the file.** On a restart prompt the model sometimes answers just
  `cron.py` (3 tokens); `extract_code` took that as the whole file. Bare paths and unclosed
  fences are now rejected.

The models write 2–4× the reference length (cron: ~3,800 output tokens for an 82-line
reference), so the window has to reserve that. With both fixed, qwen3.6 still passes none of the
long tasks in 3 attempts; what reaches the frontier on escalation is a near-miss draft
(`.halo-draft`) rather than garbage. For files of this size the local tier does not save
tokens on this machine yet (est. frontier tokens −14%, i.e. more than writing it directly).

A third HALO bug turned up afterwards: `extract_code` closed a fenced block at the first
```` ``` ```` anywhere, including inside a string. Code that handles Markdown is full of
`s.startswith("```")`, so every `mdhtml` draft was cut in half before it ran (the reference
itself extracts as 42 of its 102 lines). Fences now count only on their own line, and a
closing fence must be at least as long as its opener (CommonMark). Rerun: still 0/6, but
`mdhtml` drafts now run all their tests, and one trailing newline is all that separates them
from passing. All six failures are now model mistakes, not HALO bugs.

The trailing-`\n` miss suggested the repair prompt was the problem: the extra newline is
nearly invisible in unittest's printed diff. Repair feedback now opens with a `NOTE:` line
when failing `assertEqual`s on strings differ only in whitespace or case (e.g. "8 of 8 failing
assertion(s): only whitespace differs: the left side ends with 1 newline(s), the right with 0").
Rerun (`bench/results/20261005-175002-*.json`, 1,396 s): still 0/6, and none of the six final
failures was whitespace-only, so the hint matched nothing in them. This run's `mdhtml` drafts
failed on emphasis logic instead. Prompts are not logged, so whether the hint fired in an
intermediate round is unknown. Drafts differ from run to run, and these long tasks fail on a
different bug each time: one feedback trick does not move the pass rate.

## HFF handoff: Claude hands a multi-file job to Codex (5 Oct 2026)

`bench/e2e.sh handoff bench/handoff/txledger`: a package of three modules (parser with error
line numbers, ledger with balances and errors, reports with CSV output; reference 146 lines)
against 9 tests. Both arms are Claude Code with Sonnet. The baseline does the job itself; the
HALO arm is told to hand it to `codex` with `halo_handoff` (sector `txledger`, the test command
as check). Four pairs: random order came out HALO-first three times, so the fourth was pinned
baseline-first.

| pair (order) | baseline cost | baseline output tok / turns | HALO cost | HALO output tok / turns | Codex side (in / out, time) |
|---|---|---|---|---|---|
| 1 (HALO first) | $0.1294 | 3,836 / 7 | $0.0627 | 653 / 4 | 92.7k / 2.5k, 92 s |
| 2 (HALO first) | $0.0904 | 3,571 / 7 | $0.0624 | 622 / 4 | 106.1k / 3.4k, 84 s |
| 3 (HALO first) | $0.0880 | 3,890 / 8 | $0.0621 | 598 / 4 | 96.5k / 3.8k, 88 s |
| 4 (baseline first) | $0.0887 | 3,751 / 7 | $0.0621 | 619 / 4 | 93.6k / 2.8k, 77 s |
| **total** | **$0.3965** | **15,048** | **$0.2493** | **2,492** | Codex passed 4/4 in one round |

- All 8 runs passed the tests. Claude's cost fell 37% ($0.250 vs $0.397); excluding pair 1,
  where the baseline wrote an unusually large cache, it fell 30%. Output tokens fell 83%.
- The HALO arm's cost is almost constant ($0.0621-0.0627): one ToolSearch, one handoff, one
  test re-run. That is the fixed price of a handoff on the Claude side, so the saving grows with
  the size of the job. A job Claude finishes for under ~$0.06 is not worth handing off.
- The work moved to the user's ChatGPT plan: ~97k Codex input tokens (mostly cached) and ~3k
  output per job, 77-92 s each. This is quota sharing, not a free lunch: it pays off when
  Claude's quota is the scarce one.
- Caveat: in the HALO arm Claude only re-ran the tests and did not read the code (it said so
  in its answer). Code quality beyond the tests was not compared. One task, one controller model.

