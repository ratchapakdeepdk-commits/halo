#!/bin/bash
# HALO installer for macOS: double-click this file in Finder.
#   macOS says it is from an unidentified developer? Right-click it → Open (once), or
#   System Settings → Privacy & Security → "Open Anyway". Or from Terminal:
#   bash install.command
# Installs uv (which brings its own Python 3.12, no admin rights) and Ollama if missing,
# installs HALO, picks and pulls models for this Mac, connects the agent CLIs it finds,
# adds a HALO app to ~/Applications (and the Desktop) and opens the control panel.
#   --ci-smoke   CI only: skip Ollama + model download, do not open the panel.
set -euo pipefail
cd "$(dirname "$0")"
here="$(pwd)"
smoke=0
if [[ "${1:-}" == "--ci-smoke" ]]; then smoke=1; shift; fi

say() { printf '\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*"; [[ $smoke == 1 ]] || read -rp "Press Enter to close." _; exit 1; }
ollama_up() { curl -fs -m 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; }
# Finder starts .command files with a bare PATH: add the usual install locations.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# 1. uv (installs and runs HALO with its own Python; the system python3 is 3.9)
if ! command -v uv >/dev/null; then
  say "Installing uv (Python package manager, no admin needed)..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh \
    || die "Could not install uv. Check the internet connection and re-run."
fi

# 2. Ollama (the local model server)
if [[ $smoke == 0 ]] && ! ollama_up; then
  app=""
  for d in /Applications/Ollama.app "$HOME/Applications/Ollama.app"; do [[ -d $d ]] && app=$d; done
  if [[ -z $app ]]; then
    say "Installing Ollama..."
    if command -v brew >/dev/null && brew install --cask ollama; then
      app=/Applications/Ollama.app
    else
      tmp="$(mktemp -d)"
      if curl -fL --progress-bar https://ollama.com/download/Ollama-darwin.zip -o "$tmp/o.zip" \
          && mkdir -p "$HOME/Applications" && ditto -x -k "$tmp/o.zip" "$HOME/Applications"; then
        app="$HOME/Applications/Ollama.app"
      else
        open https://ollama.com/download
        die "Could not download Ollama. Install it from the page that just opened, then re-run."
      fi
    fi
  fi
  say "Starting Ollama..."
  open "$app"
  for _ in $(seq 60); do ollama_up && break; sleep 1; done
  ollama_up || die "Ollama did not start. Open the Ollama app once, then re-run install.command."
  say "Ollama is running."
fi

# 3. HALO itself (into ~/.local/bin: halo, halo-mcp)
say "Installing HALO..."
uv tool install --force --python 3.12 "$here" || die "Installing HALO failed."
halo --version

# 4. Pick + pull models for this Mac, then choose which agents (Claude Code / Codex / Gemini)
#    use HALO from the ones installed (Enter = recommended models / all agents)
if [[ $smoke == 0 ]]; then
  if ! command -v claude >/dev/null && ! command -v codex >/dev/null && ! command -v gemini >/dev/null; then
    echo "   (No Claude Code / Codex / Gemini CLI found: HALO works from the terminal;"
    echo "    run 'halo agents add <name>' after installing one.)"
  fi
  halo setup || die "halo setup failed"
fi

# 5. HALO app (Launchpad / Spotlight / Desktop) that opens the control panel
mkdir -p "$HOME/Applications"
osacompile -o "$HOME/Applications/HALO.app" \
  -e "do shell script \"'$HOME/.local/bin/halo' gui > /dev/null 2>&1 &\"" \
  && ln -sfn "$HOME/Applications/HALO.app" "$HOME/Desktop/HALO" 2>/dev/null \
  && say "HALO app created: ~/Applications/HALO.app (and on the Desktop)"

if [[ $smoke == 1 ]]; then say "CI smoke install OK"; exit 0; fi
halo doctor || true
say "Done. Optional: run 'halo tune' (or 'Auto-pick' in the panel) to find the best local models for this Mac."
open "$HOME/Applications/HALO.app"
