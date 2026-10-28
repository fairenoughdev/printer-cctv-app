#!/usr/bin/env bash
# Printer CCTV native launcher. Safe to invoke from any working directory.
set -uo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || exit 1
cd -- "$ROOT" || exit 1
umask 077
PYTHON="$(command -v python3.12 || command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
    printf '%s\n' 'Python 3.12+ is required. Install Python and uv, then retry.' >&2
    exit 1
fi
if ! command -v flock >/dev/null; then
    printf '%s\n' 'This Linux launcher requires flock (util-linux).' >&2
    exit 1
fi
mkdir -p -- "$ROOT/.runtime"
exec flock -- "$ROOT/.runtime/lifecycle.lock" "$PYTHON" "$ROOT/scripts/lifecycle.py" launch "$@"
