#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
uv=$(command -v uv || true)
if [ -z "$uv" ]; then
    uv="$PWD/.noveria-tools/uv"
    if [ ! -x "$uv" ]; then
        installer=$(mktemp)
        trap 'rm -f "$installer"' EXIT
        curl -fsSL https://astral.sh/uv/install.sh -o "$installer"
        UV_UNMANAGED_INSTALL="$PWD/.noveria-tools" sh "$installer"
    fi
fi
"$uv" sync --locked
"$uv" pip install --python .venv/bin/python -r requirements-ui.txt -r requirements-research.txt
printf '%s\n' 'Start the map server: .venv/bin/python scripts/godot_server.py'
