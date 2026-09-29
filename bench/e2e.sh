#!/usr/bin/env bash
# End-to-end A/B with Claude Code: same prompt, same model, same (empty) MCP config,
# the only difference is whether the HALO MCP server is attached. Costs real frontier tokens.
#
#   bench/e2e.sh digest /path/to/big.log      "What errors occurred, how often, last restart?"
#   bench/e2e.sh code   bench/tasks/semver
#
# Prints tool calls, input/output tokens, cache reads, turns and total cost for both arms.
# Arm order is random by default (prompt-cache state alone moves a run's cost by ~30%, so a
# fixed order biases the second arm); HALO_E2E_ORDER=halo-first|baseline-first pins it.
set -euo pipefail
kind=$1; target=$2; question=${3:-"What errors occurred, how many times, and when did the service last restart? Be brief."}
model=${HALO_E2E_MODEL:-sonnet}
halo_cfg='{"mcpServers":{"halo":{"command":"'"$(command -v halo-mcp)"'"}}}'
none_cfg='{"mcpServers":{}}'
out=$(mktemp -d)
case "${HALO_E2E_ORDER:-random}" in
  halo-first) arms=(with-halo baseline);;
  baseline-first) arms=(baseline with-halo);;
  random) if (( RANDOM % 2 )); then arms=(baseline with-halo); else arms=(with-halo baseline); fi;;
  *) echo "HALO_E2E_ORDER must be random, halo-first or baseline-first"; exit 2;;
esac
echo "arm order: ${arms[*]}"

report() {
  python3 - "$1" <<'PY'
import json, sys
tools = []
for line in open(sys.argv[1]):
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("type") == "assistant":
        tools += [c["name"].replace("mcp__halo__", "") for c in d["message"]["content"] if c["type"] == "tool_use"]
    if d.get("type") == "result":
        u = d.get("usage", {})
        print(f"  answer: {d.get('result', '')[:400]!r}")
        print(f"  tools={tools}\n  input={u.get('input_tokens')} output={u.get('output_tokens')} cache_read={u.get('cache_read_input_tokens')} "
              f"cache_write={u.get('cache_creation_input_tokens')} turns={d.get('num_turns')} "
              f"cost=${d.get('total_cost_usd', 0):.4f}")
PY
}

arm() {  # arm <name> <mcp-config> <allowed-tools> <workdir> <prompt>
  # The baseline also loses the Skill tool and the user-scope settings/CLAUDE.md: a user-wide
  # halo-delegate skill or HALO rule block would otherwise tell it about tools it does not have.
  local deny=(); [ "$1" = baseline ] && deny=(--disallowedTools Skill --setting-sources project,local)
  (cd "$4" && claude -p "$5" --model "$model" --strict-mcp-config --mcp-config "$2" \
     --allowedTools "$3" "${deny[@]}" --output-format stream-json --verbose \
     < /dev/null > "$out/$1.jsonl" 2>&1) || true
  echo "[$1]"; report "$out/$1.jsonl"
}

case "$kind" in
  digest)
    log=$(realpath "$target")
    base_tools="Read,Grep,Bash(grep:*),Bash(wc:*)"
    for a in "${arms[@]}"; do
      mkdir -p "$out/$a"; cp "$log" "$out/$a/"   # each arm reads its own copy, inside its cwd
      prompt="The log is at ./$(basename "$log") (large). $question"
      if [ $a = with-halo ]; then arm $a "$halo_cfg" "mcp__halo__halo_digest,$base_tools" "$out/$a" "$prompt"
      else arm $a "$none_cfg" "$base_tools" "$out/$a" "$prompt"; fi
    done
    ;;
  code)
    task=$(realpath "$target")
    target_file=$(python3 -c "import json;print(json.load(open('$task/task.json'))['target'])")
    spec=$(python3 -c "import json;print(json.load(open('$task/task.json'))['spec'])")
    prompt="In this directory, create $target_file so that \`python -m unittest test_task\` passes. Spec: $spec Be brief when done."
    tools="Read,Write,Edit,Bash(python3:*),Bash(python:*)"
    for a in "${arms[@]}"; do
      mkdir -p "$out/$a"; cp "$task/test_task.py" "$out/$a/"
      if [ $a = with-halo ]; then arm $a "$halo_cfg" "mcp__halo__halo_code,$tools" "$out/$a" "$prompt"
      else arm $a "$none_cfg" "$tools" "$out/$a" "$prompt"; fi
      echo "  tests: $(cd "$out/$a" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q test_task 2>&1 | tail -1)"
    done
    ;;
  *) echo "usage: $0 digest|code <target> [question]"; exit 2;;
esac
echo "raw transcripts: $out"
