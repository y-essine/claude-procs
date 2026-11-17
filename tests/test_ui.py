"""Render every tab of the interactive view on demo data into a virtual screen."""
import os
import pty
import select
import time

import pytest


@pytest.mark.parametrize("tab", ["live", "history", "machine", "status"])
def test_tab_renders(tab):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm-256color", LINES="40", COLUMNS="130", CLAUDE_PROCS_STATE="/tmp/claude-procs-test-state")
        from claude_procs import tui
        try:
            tui.main(demo=True, start_tab=tab)
        except Exception:
            import traceback
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    out, t, quit_sent, status = b"", time.time(), False, None
    # keep draining the terminal until the UI exits: macOS ptys have small buffers, and a UI blocked
    # on writing a frame never reads the "q"
    while time.time() - t < 20:
        if not quit_sent and time.time() - t > 2.0:
            os.write(fd, b"q")
            quit_sent = True
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                out += os.read(fd, 65536)
            except OSError:
                pass
        done, st = os.waitpid(pid, os.WNOHANG)
        if done:
            status = st
            break
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        pytest.fail("the UI did not quit on q")
    text = out.decode(errors="replace")
    assert "Traceback" not in text, text[text.find("Traceback"):]
    assert "claude-procs" in text
    assert os.WEXITSTATUS(status) == 0
