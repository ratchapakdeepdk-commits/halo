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

## Not yet measured

- Larger samples and other task families: multi-file refactors, data conversion, docs.
- Frontier models other than Sonnet, and agents other than Claude Code.
- Smaller GPUs (8–16 GB) and the untested models in the recommendation table.
