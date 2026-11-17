"""Run ``hl-guard approve`` in a pseudo-terminal and type the answer, as a user would.

    approve_pty.py <id> <config> [answer]

It prints the transcript and exits with the command's code.
"""

from __future__ import annotations

import os
import pty
import select
import sys
from pathlib import Path

PROMPT = b"Type yes to approve this order:"


def approve_in_a_terminal(config: Path, proposal_id: str, answer: str = "yes") -> tuple[int, str]:
    hl_guard = str(Path(sys.executable).with_name("hl-guard"))
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(hl_guard, [hl_guard, "approve", proposal_id, "--config", str(config)])
    out = b""
    answered = False
    while True:
        ready, _, _ = select.select([fd], [], [], 30)
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if not answered and PROMPT in out:
            os.write(fd, answer.encode() + b"\n")
            answered = True
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace").replace("\r\n", "\n")


if __name__ == "__main__":
    code, transcript = approve_in_a_terminal(Path(sys.argv[2]), sys.argv[1], *sys.argv[3:4])
    print(transcript, end="")
    sys.exit(code)
