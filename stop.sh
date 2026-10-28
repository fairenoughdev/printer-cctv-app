#!/usr/bin/env bash
# Stops only process groups whose saved Linux identity still matches.
set -uo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || exit 1
cd -- "$ROOT" || exit 1
umask 077
PYTHON="$(command -v python3.12 || command -v python3 || true)"
if [[ -z "$PYTHON" ]] || ! command -v flock >/dev/null; then
    printf '%s\n' 'Stopping safely requires Python 3 and flock (util-linux).' >&2
    exit 1
fi
mkdir -p -- "$ROOT/.runtime"
exec flock -- "$ROOT/.runtime/lifecycle.lock" "$PYTHON" "$ROOT/scripts/lifecycle.py" stop "$@"
