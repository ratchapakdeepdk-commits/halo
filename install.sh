#!/usr/bin/env bash
# One-command install: halo CLI + MCP server, model pull, optional Claude Code wiring.
#   curl -fsSL <raw url>/install.sh | bash            (or ./install.sh from a clone)
#   ./install.sh --claude                             also register with Claude Code
set -euo pipefail
REPO_URL="${HALO_REPO:-https://github.com/OWNER/halo}"

here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -f "$here/pyproject.toml" ]]; then src="$here"; else src="git+$REPO_URL"; fi

command -v python3 >/dev/null || { echo "python3 (>=3.10) is required"; exit 1; }
if ! command -v ollama >/dev/null && ! curl -fs http://127.0.0.1:11434/api/tags >/dev/null; then
  echo "Ollama not found. Install it first: https://ollama.com/download"
  echo "(Linux: curl -fsSL https://ollama.com/install.sh | sh)"
  exit 1
fi

if command -v pipx >/dev/null; then
  pipx install --force "$src"
else
  python3 -m pip install --user --upgrade "$src" 2>/dev/null \
    || python3 -m pip install --user --upgrade --break-system-packages "$src"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
    echo "NOTE: add ~/.local/bin to your PATH";; esac
fi

export PATH="$HOME/.local/bin:$PATH"
halo setup --yes "$@"
halo doctor || true
