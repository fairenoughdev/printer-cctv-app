#!/usr/bin/env python3
"""Private process-group supervisor: redact application output before persistence."""
import os
from pathlib import Path
import re
import signal
import subprocess
import sys


def redact(text: str) -> str:
    text = re.sub(
        r"(?i)((?:password|access[_ -]?code|api[_ -]?key|authorization|secret|token)[\"']?\s*[:=]\s*).*$",
        r"\1[redacted]", text)
    # Camera addresses may contain access codes in arbitrary paths: remove URLs entirely.
    text = re.sub(r"(?i)\b(?:https?|rtsps?|rtmps?|ws[s]?)://[^\s<>\"']+", "[url redacted]", text)
    text = re.sub(r"(?i)([?&](?:token|key|password|access_code|secret)=)[^&\s\"']+", r"\1[redacted]", text)
    text = re.sub(r"(/display/)[A-Za-z0-9_-]+", r"\1[redacted]", text)
    return text


def main() -> int:
    # argv contains a role/root marker used by lifecycle.py to verify ownership.
    role, root = sys.argv[1:3]
    command = sys.argv[3:]
    log_path = Path(root) / ".runtime" / f"{role}.log"
    try:
        child = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace",
                                 bufsize=1, close_fds=True)
    except OSError as error:
        with log_path.open("a", encoding="utf-8") as output:
            output.write(redact(f"Could not start service: {error}\n"))
        return 1

    def terminate(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)
        # Stay alive until the child exits so lifecycle identity remains verifiable.

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    assert child.stdout is not None
    # Bounded reads prevent unlimited memory use if a dependency emits no newline.
    with log_path.open("a", encoding="utf-8", buffering=1) as output:
        os.chmod(log_path, 0o600)
        while line := child.stdout.readline(65536):
            output.write(redact(line))
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
