#!/bin/bash
# Wrapper script for web_research.py using uv

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Local uv install (tool-use policy R3): repo-local, never system-wide
REPO_ROOT="$( cd "$SCRIPT_DIR/../../" && pwd )"
UV_DIR="$REPO_ROOT/tmp/uv"
UV_BIN="$UV_DIR/uv"
install_uv() {
    echo "Installing uv to $UV_DIR ..." >&2
    mkdir -p "$UV_DIR"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh
}
if ! "$UV_BIN" --version >/dev/null 2>&1; then
    install_uv
    if ! "$UV_BIN" --version >/dev/null 2>&1; then
        echo "uv verification failed; retrying once ..." >&2
        install_uv
    fi
fi

# Run with inline dependencies (PEP 723 metadata in web_research.py)
# Unset PYTHONPATH to avoid conflicts with system Python packages
env -u PYTHONPATH PYTHONIOENCODING=utf-8 "$UV_BIN" run --no-project "$SCRIPT_DIR/web_research.py" "$@"
