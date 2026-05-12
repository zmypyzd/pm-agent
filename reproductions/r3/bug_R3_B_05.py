#!/usr/bin/env python3
"""R3-B-05: _gh hangs because subprocess inherits parent stdin.

Exit 0 = REPRODUCED (subprocess blocks reading stdin → hits the 5s outer
            timeout, or elapsed >= 2s). Exit 1 = NOT REPRODUCED (stdin
            pinned to DEVNULL so the shim sees EOF and exits fast).

Strategy: install a fake `gh` shim that reads stdin (blocks forever if the
caller hasn't redirected stdin). Measure how long _gh takes.

We feed our own /dev/null into the *parent* python process too — otherwise
asyncio.create_subprocess_exec by default inherits the parent's stdin, and
when run from a TTY the buggy code would just hang. Inside CI / a test
runner stdin is usually already not a TTY (pytest captures stdin), so the
buggy code may still appear to hang because the shim sits in read() waiting
for EOF on the inherited pipe. The DEVNULL fix makes the shim see EOF
immediately.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
import time


def main() -> int:
    shim_dir = pathlib.Path(tempfile.mkdtemp(prefix="r3-b-05-"))
    shim = shim_dir / "gh"
    # Shim reads all of stdin then prints ok. If stdin is DEVNULL, read()
    # returns immediately. If stdin is inherited from a process that doesn't
    # close it, read() blocks forever.
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdin.read()\n"
        "print('ok')\n"
    )
    shim.chmod(0o755)
    os.environ["PATH"] = f"{shim_dir}:{os.environ.get('PATH', '')}"

    # Make sure we import pm_agent from the repo, not any installed version.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from pm_agent.github import _gh

    # Open a pipe and use its read end as our process stdin so the shim,
    # if it inherits our stdin, will sit forever waiting for input. This
    # simulates the daemon-launched-from-TTY case.
    r_fd, w_fd = os.pipe()
    saved_stdin = os.dup(0)
    try:
        os.dup2(r_fd, 0)
        os.close(r_fd)

        async def _call() -> tuple[int, str, str]:
            return await _gh("whatever")

        start = time.time()
        try:
            asyncio.run(asyncio.wait_for(_call(), timeout=5.0))
            elapsed = time.time() - start
        except asyncio.TimeoutError:
            elapsed = time.time() - start
            print(f"[R3-B-05] REPRODUCED — _gh hung (TimeoutError at "
                  f"{elapsed:.2f}s); subprocess inherited stdin")
            return 0
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        try:
            os.close(w_fd)
        except OSError:
            pass

    if elapsed >= 2.0:
        print(f"[R3-B-05] REPRODUCED — _gh elapsed={elapsed:.2f}s "
              f"(stdin not DEVNULL'd; shim blocked on read)")
        return 0
    print(f"[R3-B-05] NOT-REPRODUCED — _gh elapsed={elapsed:.2f}s "
          f"(stdin handled correctly)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
