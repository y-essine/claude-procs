"""End to end on the real OS (Linux /proc or macOS ps + sysctl): a fake `claude` session and a busy
child that carries its CLAUDE_PID; the child's CPU must be attributed to that session."""
import os
import shutil
import subprocess
import sys
import time

import pytest

BUSY = "import time\nt = time.time()\nwhile time.time() - t < 4: pass\n"


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROCS_STATE", str(tmp_path / "state"))
    # a process whose comm and argv[0] are "claude". Not a copy of `sleep`: multi-call coreutils
    # (uutils on Ubuntu 26.04) pick the applet from argv[0] and exit.
    fake = tmp_path / "claude"
    cmd = None
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc:
        src = tmp_path / "fake.c"
        src.write_text("#include <unistd.h>\nint main(void) { sleep(30); return 0; }\n")
        if subprocess.run([cc, "-o", str(fake), str(src)], capture_output=True).returncode == 0:
            cmd = [str(fake)]
    if cmd is None and sys.platform.startswith("linux"):
        fake.symlink_to(sys.executable)  # Linux names the process after the exec'd path
        cmd = [str(fake), "-c", "import time; time.sleep(30)"]
    if cmd is None:
        pytest.skip("no C compiler to build a fake claude binary")
    cfg = tmp_path / ".claude-test"
    (cfg / "sessions").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
    env["CLAUDE_CONFIG_DIR"] = str(cfg)
    claude = subprocess.Popen(cmd, env=env)
    child_env = dict(env, CLAUDE_PID=str(claude.pid))
    busy = subprocess.Popen([sys.executable, "-c", BUSY], env=child_env)
    yield claude, busy
    for p in (busy, claude):
        p.kill()
        p.wait()


def test_child_cpu_is_attributed_to_its_session(session):
    from claude_procs import core
    claude, busy = session
    time.sleep(0.3)
    s = core.Sampler()
    s.sample()
    time.sleep(1.5)
    _, dt, cur, procs = s.sample()
    rows = core.attribute(s, core.Tasks(core.db()), cur, procs, want_other=False)
    # inside a tracked session's cgroup (running the tests from Claude Code), the cgroup wins over
    # CLAUDE_PID, by design; elsewhere (CI) the CLAUDE_PID variable decides
    scope = core.scope_cgroup(os.getpid())
    acct, owner = scope if scope else ("test", claude.pid)
    mine = {k: v for k, v in rows.items() if k[1] == owner and k[2] != "agent"}
    assert mine, f"nothing attributed to session {owner}: {list(rows)[:10]}"
    assert all(k[0] == acct for k in mine), mine
    cpu = sum(v[0] for v in mine.values())
    assert cpu > 0.5 * dt, f"busy child used {cpu:.2f}s of {dt:.2f}s"
    assert claude.pid in core.live_sessions()


def test_argv_and_env_of_a_process(session):
    from claude_procs import sysinfo
    claude, busy = session
    time.sleep(0.2)
    assert sysinfo.proc_argv(busy.pid)[:2] == [sys.executable, "-c"] or \
        os.path.basename(sysinfo.proc_argv(busy.pid)[0]).startswith("python")
    assert sysinfo.proc_env(busy.pid).get("CLAUDE_PID") == str(claude.pid)
    assert busy.pid in sysinfo.children_of(os.getpid())
