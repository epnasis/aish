#!/bin/sh
# aish installer for macOS & Linux:
#   curl -fsSL https://raw.githubusercontent.com/epnasis/aish/main/install.sh | sh
set -eu

REPO="git+https://github.com/epnasis/aish.git"
MODEL="${AISH_MODEL:-qwen3.6:35b-a3b}"

if ! command -v uv >/dev/null 2>&1; then
    echo "==> installing uv (https://astral.sh/uv)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> installing aish from $REPO"
uv tool install --force "$REPO"

if ! command -v ollama >/dev/null 2>&1; then
    echo "==> ollama not found. Install it first:"
    echo "      macOS:  https://ollama.com/download"
    echo "      Linux:  curl -fsSL https://ollama.com/install.sh | sh"
elif ! ollama list 2>/dev/null | grep -q "^${MODEL}"; then
    echo "==> default model not present. Pull it with:"
    echo "      ollama pull ${MODEL}    # ~23 GB; needs ~24 GB RAM/VRAM"
    echo "    On smaller machines use a lighter tool-calling model, e.g.:"
    echo "      ollama pull qwen3:8b && export AISH_MODEL=qwen3:8b"
fi

echo "==> done. Try:  aish \"what OS am I on?\""
