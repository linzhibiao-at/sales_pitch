#!/usr/bin/env bash
set -euo pipefail

# Install uv
if ! command -v uv &> /dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

source "$HOME/.local/bin/env"

# Create venv
uv venv
source .venv/bin/activate

# Install dependencies
uv pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
uv pip install -r fila_agent_html/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

echo "Setup complete. Activate with: source .venv/bin/activate"
