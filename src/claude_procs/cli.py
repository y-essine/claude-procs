"""claude-procs command line."""

import argparse
import os
import sys

from . import __version__

DESCRIPTION = """See which Claude Code session and task is using your CPU and RAM.

With no command, opens the interactive view (Live / History / Machine / Status tabs).
"""

EPILOG = """examples:
  claude-procs install --alias personal=claudep --alias work=claudew
  claude-procs                       interactive view
  claude-procs --demo                the interactive view on made-up data
  claude-procs who 12345             which session / task / command started pid 12345
  claude-procs report --since 7d --by project
"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claude-procs", description=DESCRIPTION, epilog=EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"claude-procs {__version__}")
    ap.add_argument("--demo", action="store_true", help="interactive view with made-up data")
    ap.add_argument("--tab", choices=["live", "history", "machine", "status"], help="open on this tab")
    ap.add_argument("-i", "--interval", type=float, default=2, help="refresh period of the view, seconds")
    sp = ap.add_subparsers(dest="cmd", metavar="command")

    sp.add_parser("ui", help="interactive view (the default)")
    t = sp.add_parser("top", help="plain-text live view (pipe-friendly with --once)")
    t.add_argument("-i", "--interval", type=float, default=2)
    t.add_argument("--once", action="store_true")
    t.add_argument("--all", action="store_true", help="include idle rows")
    w = sp.add_parser("who", help="which session / task / command a process belongs to")
    w.add_argument("pids", type=int, nargs="+")
    r = sp.add_parser("report", help="history from the daemon's database")
    r.add_argument("--since", default="24h", help="30m, 6h, 7d, today (default 24h)")
    r.add_argument("--by", default="task", choices=["task", "session", "project", "cmd", "comm", "account"])
    r.add_argument("--acct", help="only this account (personal, work, default...)")
    r.add_argument("--other", action="store_true", help="include processes not started by Claude")
    r.add_argument("-n", type=int, default=25)
    sp.add_parser("status", help="health check: requirements, daemon, hooks, data")
    i = sp.add_parser("install", help="add the hooks, start the daemon, adopt running sessions")
    i.add_argument("--config-dir", action="append", help="only these Claude config dirs (default: all found)")
    i.add_argument("--alias", action="append", metavar="ACCOUNT=CMD",
                   help="how you launch an account, shown in the UI (e.g. work=claudew)")
    u = sp.add_parser("uninstall", help="remove the hooks and the daemon")
    u.add_argument("--config-dir", action="append")
    u.add_argument("--purge", action="store_true", help="also delete the database and config")
    sp.add_parser("adopt", help="move already-running claude sessions into their own cgroup")
    d = sp.add_parser("daemon", help="the sampler (run by the systemd user unit)")
    d.add_argument("-i", "--interval", type=float, default=None)
    hk = sp.add_parser("hook", help="Claude Code hook entry point (reads the hook JSON on stdin)")
    hk.add_argument("--ensure-daemon", action="store_true", help="start the sampler if it is not running (plugin mode)")
    ti = sp.add_parser("titles", help="name each session (terminal title, prompt box) after its current task")
    ti.add_argument("state", nargs="?", choices=["on", "off"])
    sl = sp.add_parser("statusline", help="a segment for Claude Code's status line: this session's CPU and memory")
    sl.add_argument("--label", default="⚙ ", help="text before the numbers (default '⚙ ')")
    sl.add_argument("--no-color", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "hook":  # hot path: keep imports minimal
        from .core import cmd_hook
        return cmd_hook(a)
    if a.cmd == "statusline":
        from .core import cmd_statusline
        return cmd_statusline(a)
    if a.cmd in (None, "ui"):
        if not sys.stdout.isatty():
            from .core import cmd_top
            return cmd_top(argparse.Namespace(interval=2, once=True, all=False))
        from .tui import main as tui
        return tui(demo=a.demo, interval=a.interval, start_tab=a.tab, tour=a.demo and bool(os.environ.get("CLAUDE_PROCS_TOUR")))
    from . import core, install
    if a.cmd == "daemon" and a.interval is None:
        a.interval = core.CONFIG["interval"]
    return {
        "top": core.cmd_top, "who": core.cmd_who, "report": core.cmd_report, "adopt": core.cmd_adopt,
        "daemon": core.cmd_daemon, "status": install.cmd_status, "titles": core.cmd_titles, "install": install.cmd_install,
        "uninstall": install.cmd_uninstall,
    }[a.cmd](a)


if __name__ == "__main__":
    main()
