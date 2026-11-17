import os

import pytest

os.environ.setdefault("CLAUDE_PROCS_STATE", "/tmp/claude-procs-test-state")

from claude_procs import core  # noqa: E402

T = 100  # pretend clock ticks per second


def deltas(prev, cur, first=False):
    return {pid: (own, short) for pid, _s, _c, _pp, own, short in core.cpu_deltas(prev, cur, first, tick=T)}


def test_running_process_counts_its_own_time():
    prev = {10: (1, 100, 0, 1, "node")}
    cur = {10: (1, 350, 0, 1, "node")}
    assert deltas(prev, cur) == {10: (2.5, 0.0)}


def test_short_lived_children_are_credited_to_the_parent():
    # cargo spawned and reaped rustc processes we never saw: they show up in cargo's cutime
    prev = {20: (1, 10, 0, 1, "cargo")}
    cur = {20: (1, 12, 500, 1, "cargo")}
    assert deltas(prev, cur)[20] == (0.02, 5.0)


def test_reaped_child_seen_before_is_not_counted_twice():
    prev = {20: (1, 10, 0, 1, "cargo"), 21: (5, 300, 0, 20, "rustc")}
    cur = {20: (1, 10, 340, 1, "cargo")}  # rustc used 40 more ticks, then exited and was reaped
    assert deltas(prev, cur)[20] == (0.0, pytest.approx(0.4))


def test_whole_tree_exiting_cascades_to_the_nearest_live_ancestor():
    # claude(1) > bash(30) > cargo(31) > rustc(32): the whole build exits within one interval
    prev = {1: (1, 0, 0, 0, "claude"), 30: (2, 1, 0, 1, "bash"), 31: (3, 5, 0, 30, "cargo"),
            32: (4, 900, 0, 31, "rustc")}
    cur = {1: (1, 0, 906 + 100, 0, "claude")}  # 100 ticks happened after the last sample
    assert deltas(prev, cur)[1] == (0.0, pytest.approx(1.0))


def test_pid_reuse_is_a_new_process():
    prev = {40: (1, 500, 0, 1, "old")}
    cur = {40: (9, 30, 0, 1, "new")}
    assert deltas(prev, cur)[40] == (0.3, 0.0)


def test_first_sample_is_a_baseline():
    assert deltas({}, {50: (1, 999, 999, 1, "x")}, first=True) == {50: (0.0, 0.0)}


@pytest.mark.parametrize("config_dir,acct", [
    (None, "default"), ("/home/u/.claude", "default"), ("/home/u/.claude-personal", "personal"),
    ("/home/u/.claude-Work/", "work"), ("/srv/cfg/team-b", "teamb"),
])
def test_account_of(config_dir, acct):
    assert core.account_of(config_dir) == acct


def test_bash_tool_command_is_extracted():
    script = ("source /home/u/.claude/shell-snapshots/snapshot-bash-1-x.sh 2>/dev/null || true && "
              "eval 'cargo build --release 2>&1 | tail -3' && pwd -P >| /tmp/claude-1-cwd")
    assert core.bash_tool_command(["/usr/bin/bash", "-c", script]) == "cargo build --release 2>&1 | tail -3"
    assert core.bash_tool_command(["node", "server.js"]) is None


@pytest.mark.parametrize("cg,label", [
    ("/", "kernel threads"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-Hyprland-code-1234.scope", "code"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-graphical.slice/"
     "app-Hyprland-xdg\\x2dterminal\\x2dexec-3c26b7f3.scope", "xdg-terminal-exec"),
    ("/system.slice/docker.service", "docker"),
])
def test_unit_label(cg, label):
    assert core.unit_label(cg) == label


def test_project_of_claude_worktree_and_scratchpad(tmp_path):
    assert core.project_of("/tmp/claude-1000/-home-u/abc/scratchpad") == "scratch"
    wt = tmp_path / "myapp" / ".claude" / "worktrees" / "agent-ac4cdb2f8de5"
    wt.mkdir(parents=True)
    assert core.project_of(str(wt)) == "myapp⎇ac4cdb"


def test_short():
    assert core.short("a  b\n c", 10) == "a b c"
    assert core.short("x" * 20, 5) == "xxxx…"
