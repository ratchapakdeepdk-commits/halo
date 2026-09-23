# HALO — Hybrid Agent for Local Offloading

**Let your frontier coding agent (Claude Code, etc.) hand its routine, token-heavy work to a
free local model, so you spend frontier tokens only where frontier reasoning is needed.**

- 📄 A 5,000-line log goes to the local model, and your agent reads back a 5-line answer.
- 🔁 A small, well-specified file (parser, converter, CLI glue, fixture) is written by the local
  model in a *generate → run your tests → fix* loop. Your agent reads back
  `PASSED — parser.py: +84 -0` instead of writing 84 lines itself.
- 🛑 When the local model can't do it, it says `#ESCALATE`, or the tests never pass. The
  file is restored and your agent gets a short failure report so it can take over.

**Early results** ([RESULTS.md](RESULTS.md)): in end-to-end A/B runs with Claude Code, cost
fell 17–29% on log questions and 13% on a small coding task. Output tokens fell 40–60%. A
local model cascade passed 14/14 benchmark coding tasks. The samples are small, and the
numbers are reproducible with `bench/`.

```
Frontier agent (plans, uses tools, decides)
        │  halo_digest(question, paths)      halo_code(spec, target, check)
        ▼
HALO ── local model via Ollama ── deterministic check (tests / compiler) ── ledger
        │
        └── compact result: answer | diffstat + test summary | escalation
```

**Why the frontier model stays in charge:** local models are good workers but poor managers.
When we tested a local-only coding agent (Codex CLI + `gpt-oss:20b`), it couldn't create a
single file: a rejected tool call made it lose track of the task, and it burned 12.6k tokens.
In HALO the frontier agent owns the tool loop. The local model only produces text, and code is
accepted only after **a command you chose exits 0**.

## Install

Requirements: Python ≥ 3.10, [Ollama](https://ollama.com), and ideally a GPU (8 GB+ VRAM).
HALO depends only on the Python standard library.

```bash
git clone https://github.com/ratchapakdeepdk-commits/halo && cd halo
./install.sh --claude      # installs `halo`, picks+pulls a model for your VRAM,
                           # registers the MCP server and a delegation skill in Claude Code
halo doctor                # checks backend and measures prefill/generation speed
```

`halo setup` recommends models from your total VRAM:

| VRAM | digest model | code model | note |
|---|---|---|---|
| ≥ 24 GB | `qwen3:30b-a3b-instruct-2507-q4_K_M` | `qwen3.6:35b-a3b-q4_K_M` | tested; below 44 GB the two swap (~15 s) |
| ≥ 13 GB | `gpt-oss:20b` | same | fastest prefill, weaker non-English |
| ≥ 9 GB | `qwen3:14b` | same | untested |
| ≥ 4 GB | `qwen3:4b-instruct` | same | digest only |

`halo doctor` measures real throughput instead of trusting that the GPU is used. An outdated
driver can silently push Ollama onto a fallback backend that runs 15–20× slower.

## Use from Claude Code (MCP)

After `./install.sh --claude` (or `halo setup --claude`), Claude Code gets four tools:

| tool | what it does |
|---|---|
| `halo_digest` | reads files locally and returns only the answer. Logs are compacted first, and `signals` gives exact, code-computed error counts with first/last timestamps |
| `halo_code` | generate-verify loop for one file against your check command, with a model cascade. Returns status + diffstat; on failure it keeps the best attempt as `<file>.halo-draft` |
| `halo_ask` | short self-contained question (unverified) |
| `halo_stats` | tasks delegated and estimated frontier tokens saved |

The installed skill (`~/.claude/skills/halo-delegate`) teaches the agent the cost model
(**every tool call re-reads the whole context, so fewer turns beat smaller turns**) and this
order: use one exact command if one answers the question (`grep -c`, `jq`) → otherwise
`halo_digest` / `halo_code` → do it yourself. Design, multi-file changes and
security-sensitive code stay with the frontier model.

Manual registration: `claude mcp add --scope user halo -- halo-mcp`

## Use from the shell

```bash
halo digest -f server.log "why did the service restart? quote the lines"
journalctl -u nginx -n 5000 | halo digest "top 3 error types with counts"
halo code -t slug.py -x test_slug.py -c "python -m pytest -q test_slug.py" \
          -s "slugify(text, max_len=50): lowercase ascii, runs of non-alnum -> '-'"
halo ask "regex for ISO-8601 dates"
halo auto "translate this to Thai: good morning"     # simple → local; tool/judgement work → `claude -p`
halo stats                                           # what was offloaded, estimated savings
```

Exit codes: `0` ok/passed, `3` escalated, `4` failed, `1` error.

## Configuration

`~/.config/halo/config.json` (written by `halo setup`), overridden by environment variables:
`HALO_OLLAMA_URL` (or `OLLAMA_HOST`), `HALO_MODEL` (digest/ask), `HALO_CODE_MODEL`,
`HALO_FALLBACK_MODELS` (comma-separated cascade for code), `HALO_BULK_MODEL` (optional faster
model for inputs over `bulk_chars`), `HALO_CTX` (default 8192), `HALO_MAX_ITERS` (default 3).
`allowed_roots` (config file only) lists extra directories the MCP server may touch.
Thinking mode is always disabled, because it makes interactive delegation far too slow.

## How savings are counted (`halo stats`)

Every task is appended to `~/.local/share/halo/ledger.jsonl`:

- **local tokens**: measured by Ollama.
- **frontier direct (est.)**: what the frontier model would have read and written doing the
  task itself (input size, generated file, check output), estimated from text length.
- **frontier via HALO (est.)**: what it still reads and writes: the spec/question plus HALO's
  compact reply.

Output tokens are weighted 5× (the Claude price ratio). Only successful tasks count. Treat
the digest figure as an **upper bound**: it assumes the frontier model would otherwise read
the whole input, but real agents often grep. Measured end-to-end numbers are in
[RESULTS.md](RESULTS.md).

## Benchmark

`bench/tasks/` contains single-file coding tasks with unit tests: slugify, duration parser,
roman numerals, LRU cache, Bragg-angle calculator for FCC crystals, SemVer precedence, and
nginx log parser. Reference solutions in `bench/reference/` prove the tests are correct.

```bash
python bench/run.py                        # configured code model
python bench/run.py -m gpt-oss:20b --repeat 3
python bench/run.py --cascade              # code_model + fallback_models
bench/e2e.sh digest some.log               # A/B with Claude Code (costs real tokens)
bench/e2e.sh code bench/tasks/semver
```

## Trust model — what not to offload

- Anything that can't be checked by a command: design decisions, reviews, subtle debugging.
- Security-sensitive code (auth, crypto, input sanitisation for untrusted data).
- Numbers the local model *reasons out*. In our tests, local models got single-formula physics
  right, but each model failed a different multi-step problem. Numbers must come from code
  that ran.
- **MCP servers run outside your agent's sandbox.** HALO therefore only reads and writes
  under the directory the agent was started in (plus `allowed_roots`). `halo_code` runs the
  check command with your permissions, like your agent's own shell tool would, but without
  the agent's sandbox.

## Development

```bash
python -m unittest discover -s tests -v   # uses a fake Ollama server, no GPU needed
```

MIT licensed.
