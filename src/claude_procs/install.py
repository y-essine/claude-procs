"""install / uninstall / health checks (also the Status tab of the UI)."""

import glob
import json
import os
import shutil
import subprocess
import sys
import time

from . import __version__
from .sysinfo import LINUX, MAC
from .core import (CG_CLAUDE, CONFIG, CONFIG_PATH, DB, DEFAULT_CONFIG, HOME, STATE, account_of, cmd_adopt,
                   live_sessions, scope_cgroup)

UNIT = "claude-procs.service"
LAUNCHD_LABEL = "io.github.y-essine.claude-procs"
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")
UNIT_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "systemd", "user", UNIT)
EVENTS = ("SessionStart", "UserPromptSubmit")
BACKUP_SUFFIX = ".bak-claude-procs"


def exe_path():
    """absolute path of the claude-procs entry point, as hooks and the unit must call it"""
    found = shutil.which("claude-procs")
    if found:
        return os.path.abspath(found)  # not realpath: keep the stable launcher path
    argv0 = os.path.realpath(sys.argv[0])
    if os.path.basename(argv0).startswith("claude-procs") or os.path.basename(argv0) == "cprocs":
        return argv0
    return f"{sys.executable} -m claude_procs"


def ephemeral(path):
    """npx / uvx / pipx-run paths vanish: hooks must point at an installed command"""
    return any(x in path for x in ("/_npx/", "/uv/cache/", "/.cache/uv/", "/archive-v0/", "/pipx/.cache/"))


def hook_command():
    return f"{exe_path()} hook"


def is_our_hook(cmd):
    return cmd.rstrip().endswith(" hook") and ("claude-procs" in cmd or "claude_procs" in cmd or "cprocs" in cmd)


def config_dirs(explicit=None):
    """Claude Code config dirs on this machine: ~/.claude, ~/.claude-*, $CLAUDE_CONFIG_DIR"""
    if explicit:
        return [os.path.abspath(os.path.expanduser(d)) for d in explicit]
    dirs = []
    for d in [os.environ.get("CLAUDE_CONFIG_DIR"), os.path.join(HOME, ".claude"),
              *sorted(glob.glob(os.path.join(HOME, ".claude-*")))]:
        if not d or not os.path.isdir(d) or d.endswith(BACKUP_SUFFIX):
            continue
        if any(os.path.exists(os.path.join(d, x)) for x in ("settings.json", "projects", ".credentials.json", "sessions")):
            d = os.path.abspath(d)
            if d not in dirs:
                dirs.append(d)
    return dirs


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def hooks_state(d):
    """-> {event: bool} for one config dir"""
    cfg = _load(os.path.join(d, "settings.json"))
    hooks = cfg.get("hooks", {})
    return {ev: any(is_our_hook(h.get("command", "")) for g in hooks.get(ev, []) for h in g.get("hooks", []))
            for ev in EVENTS}


def add_hooks(d):
    path = os.path.join(d, "settings.json")
    if os.path.exists(path) and not os.path.exists(path + BACKUP_SUFFIX):
        shutil.copy2(path, path + BACKUP_SUFFIX)
    cfg = _load(path)
    hooks = cfg.setdefault("hooks", {})
    cmd = hook_command()
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        for g in groups:  # drop older entries (other install path), keep everything else
            g["hooks"] = [h for h in g.get("hooks", []) if not is_our_hook(h.get("command", ""))]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
    _save(path, cfg)


def remove_hooks(d):
    path = os.path.join(d, "settings.json")
    if not os.path.exists(path):
        return False
    cfg = _load(path)
    hooks = cfg.get("hooks", {})
    changed = False
    for ev in list(hooks):
        for g in hooks[ev]:
            before = len(g.get("hooks", []))
            g["hooks"] = [h for h in g.get("hooks", []) if not is_our_hook(h.get("command", ""))]
            changed |= len(g["hooks"]) != before
        hooks[ev] = [g for g in hooks[ev] if g.get("hooks")]
        if not hooks[ev]:
            del hooks[ev]
    if not hooks:
        cfg.pop("hooks", None)
    if changed:
        _save(path, cfg)
    return changed


def systemctl(*args, check=False):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=check)


def write_unit():
    os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
    with open(UNIT_PATH, "w") as f:
        f.write(f"""[Unit]
Description=claude-procs: attribute CPU/RAM to Claude Code sessions and tasks
Documentation=https://github.com/y-essine/claude-procs

[Service]
ExecStart={exe_path()} daemon
Restart=on-failure
RestartSec=10
Nice=10
CPUWeight=20

[Install]
WantedBy=default.target
""")


def write_plist():
    import plistlib
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    claude = shutil.which("claude")
    path = os.pathsep.join(dict.fromkeys(filter(None, [os.path.dirname(claude) if claude else None,
                                                      "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"])))
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump({"Label": LAUNCHD_LABEL, "ProgramArguments": [*exe_path().split(), "daemon"],
                       "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Background", "Nice": 10,
                       "EnvironmentVariables": {"PATH": path},
                       "StandardErrorPath": os.path.join(STATE, "daemon.log")}, f)


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def start_service():
    if LINUX:
        write_unit()
        systemctl("daemon-reload")
        systemctl("enable", "--now", UNIT)
        systemctl("restart", UNIT)
        return UNIT_PATH, systemctl("is-active", UNIT).stdout.strip()
    write_plist()
    os.makedirs(STATE, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LAUNCHD_LABEL}")
    r = launchctl("bootstrap", domain, PLIST_PATH)
    return PLIST_PATH, "loaded" if r.returncode == 0 else r.stderr.strip()


def stop_service():
    if LINUX:
        systemctl("disable", "--now", UNIT)
        path = UNIT_PATH
    else:
        launchctl("bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
        path = PLIST_PATH
    if os.path.exists(path):
        os.remove(path)
        if LINUX:
            systemctl("daemon-reload")
        return path
    return None


def write_config(aliases):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_load(CONFIG_PATH))
    # the systemd user manager often has a minimal PATH: pin the claude binary now. Not realpath:
    # version managers (mise, asdf) install `claude` as a symlink to a multiplexer that reads argv[0].
    found = shutil.which("claude")
    cfg["claude_bin"] = cfg.get("claude_bin") or (found and os.path.abspath(found))
    cfg["aliases"] = {**cfg.get("aliases", {}), **aliases}
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    _save(CONFIG_PATH, cfg)
    return cfg


def requirements():
    """-> [(ok, label, detail)]"""
    out = []
    if LINUX:
        cg2 = os.path.exists("/sys/fs/cgroup/cgroup.controllers")
        out.append((cg2, "cgroup v2", "unified hierarchy" if cg2 else "needs the cgroup v2 unified hierarchy"))
        r = systemctl("is-system-running")
        ok = r.stdout.strip() in ("running", "degraded")
        out.append((ok, "systemd --user", r.stdout.strip() or r.stderr.strip()))
        out.append((bool(shutil.which("busctl")), "busctl", shutil.which("busctl") or "not found (systemd)"))
    elif MAC:
        out.append((True, "macOS", "process tree + environment attribution (no cgroups: processes that start "
                    "and exit between two samples are not counted)"))
        out.append((bool(shutil.which("launchctl")), "launchd", shutil.which("launchctl") or "not found"))
    else:
        out.append((False, sys.platform, "only Linux and macOS are supported"))
    claude = CONFIG.get("claude_bin") or shutil.which("claude")
    out.append((bool(claude), "claude CLI", claude or "not found: task names will be off"))
    return out


def cmd_install(a):
    print(f"claude-procs {__version__}\n")
    reqs = requirements()
    for ok, label, detail in reqs:
        print(f"  {'✓' if ok else '✗'} {label:15} {detail}")
    if not all(ok for ok, label, _ in reqs if label != "claude CLI"):
        sys.exit("\nmissing requirements: claude-procs needs Linux (systemd, cgroup v2) or macOS")
    if ephemeral(exe_path()):
        sys.exit(f"\n{exe_path()} is a temporary npx/uvx copy: install it for good first\n"
                 "  uv tool install claude-procs   or   pipx install claude-procs   or   npm i -g claude-procs")
    aliases = dict(x.split("=", 1) for x in (a.alias or []))
    cfg = write_config(aliases)
    dirs = config_dirs(a.config_dir)
    if not dirs:
        sys.exit("\nno Claude Code config dir found (~/.claude); pass --config-dir")
    print("\nhooks (SessionStart, UserPromptSubmit):")
    for d in dirs:
        add_hooks(d)
        print(f"  ✓ {d.replace(HOME, '~')}/settings.json   account: {account_of(d)}"
              f"{'  (backup: settings.json' + BACKUP_SUFFIX + ')' if os.path.exists(os.path.join(d, 'settings.json' + BACKUP_SUFFIX)) else ''}")
    path, state = start_service()
    print(f"\nservice: {path.replace(HOME, '~')}  ({state})")
    print(f"config:  {CONFIG_PATH.replace(HOME, '~')}  (task names: {'on, ' + cfg['names_model'] if cfg['names'] else 'off'})")
    print("\nrunning sessions:")
    cmd_adopt(None)
    print("\ndone. New sessions are tracked from their first second. Run `claude-procs` to look around.")


def cmd_uninstall(a):
    for d in config_dirs(a.config_dir):
        if remove_hooks(d):
            print(f"  removed hooks from {d.replace(HOME, '~')}/settings.json")
    removed = stop_service()
    if removed:
        print(f"  removed {removed.replace(HOME, '~')}")
    if a.purge:
        shutil.rmtree(STATE, ignore_errors=True)
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
        print(f"  deleted {STATE.replace(HOME, '~')} and the config")
    print("  running sessions keep their cgroup until they exit (harmless).")


def status():
    """-> list of (section, [(ok|None, label, detail)])"""
    from .core import daemon_running
    sections = [("Requirements", requirements())]
    running = daemon_running()
    if LINUX and os.path.exists(UNIT_PATH):
        st = systemctl("show", UNIT, "-p", "ActiveState", "-p", "MainPID", "-p", "ActiveEnterTimestamp")
        props = dict(line.split("=", 1) for line in st.stdout.splitlines() if "=" in line)
        detail = f"{props.get('ActiveState', '?')}  pid {props.get('MainPID', '?')}  since {props.get('ActiveEnterTimestamp', '')}"
        sections.append(("Daemon", [(running, UNIT, detail)]))
    elif MAC and os.path.exists(PLIST_PATH):
        sections.append(("Daemon", [(running, LAUNCHD_LABEL, "running" if running else "loaded, not running")]))
    else:
        sections.append(("Daemon", [(running, "sampler", "running (started by the plugin)" if running
                                     else "not running: `claude-procs install`, or start a session with the plugin")]))
    hooks = []
    for d in config_dirs():
        hs = hooks_state(d)
        ok = all(hs.values())
        hooks.append((ok, d.replace(HOME, "~"), f"account {account_of(d)}  " +
                      "  ".join(f"{ev} {'✓' if v else '✗'}" for ev, v in hs.items())))
    sections.append(("Hooks", hooks or [(False, "no config dir", "run `claude-procs install`")]))
    live = live_sessions()
    scoped = sum(1 for p in live if scope_cgroup(p))
    sections.append(("Sessions", [
        (scoped == len(live) if live else None, f"{len(live)} running",
         f"{scoped} in their own cgroup" + ("" if scoped == len(live) else "  (run `claude-procs adopt`)")),
        (None, "cgroup root", CG_CLAUDE.replace(f"/user@{os.getuid()}.service", "/user@UID.service"))]))
    names = []
    try:
        import sqlite3
        c = sqlite3.connect(DB, timeout=2)
        hour = c.execute("SELECT count(*) FROM names WHERE ts > ?", (time.time() - 3600,)).fetchone()[0]
        total = c.execute("SELECT count(*) FROM names").fetchone()[0]
        first = c.execute("SELECT min(minute) FROM usage").fetchone()[0]
        rows = c.execute("SELECT count(*) FROM usage").fetchone()[0]
        c.close()
    except Exception:
        hour = total = rows = 0
        first = None
    names.append((None, "task names", f"{'on' if CONFIG['names'] else 'off'}  model {CONFIG['names_model']}  "
                  f"{hour} calls in the last hour (cap {CONFIG['names_per_hour']})  {total} names"))
    size = os.path.getsize(DB) / 2**20 if os.path.exists(DB) else 0
    names.append((None, "database", f"{DB.replace(HOME, '~')}  {size:.1f} MB  {rows} rows"
                  + (f"  since {time.strftime('%Y-%m-%d %H:%M', time.localtime(first))}" if first else "")))
    names.append((None, "terminal titles", ("on: sessions are titled after their task" if CONFIG.get("terminal_titles")
                                            else "off  (claude-procs titles on)")))
    names.append((None, "config", CONFIG_PATH.replace(HOME, "~") + ("" if os.path.exists(CONFIG_PATH) else "  (defaults)")))
    names.append((None, "version", __version__))
    sections.append(("Data", names))
    return sections


def cmd_status(_):
    for title, items in status():
        print(title)
        for ok, label, detail in items:
            mark = " " if ok is None else ("✓" if ok else "✗")
            print(f"  {mark} {label:22} {detail}")
        print()
