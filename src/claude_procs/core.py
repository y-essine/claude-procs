"""claude-procs core: attribute CPU / RAM on a Linux machine to the Claude Code session and task
that caused it.

  * A SessionStart hook moves each claude process (and its children) into a transient systemd scope
      claude.slice / claude-<account>.slice / cc-<account>-<pid>.scope
    so the kernel accounts every descendant's CPU, short-lived compilers included.
  * A UserPromptSubmit hook logs each prompt: that prompt is "the task" until the next one.
  * The daemon samples /proc every few seconds, attributes each process to its session (cgroup, else
    the CLAUDE_PID env every Claude-spawned process inherits), groups it under the Bash command / MCP
    server it descends from, and writes per-minute aggregates to SQLite.
  * Optionally, a tiny tool-less Haiku call (on the session's own account) names each task and each
    heavy command, so you read "Webapp Checkout Tests" instead of a pid.
"""

import functools
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

UID = os.getuid()
HOME = os.path.expanduser("~")
STATE = os.environ.get("CLAUDE_PROCS_STATE") or os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local/state")), "claude-procs")
CONFIG_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "claude-procs", "config.json")
DEFAULT_CONFIG = {
    "interval": 5,            # daemon sampling period, seconds
    "names": True,            # generate task / command names with a small model call
    "names_model": "haiku",
    "names_per_hour": 60,
    "claude_bin": None,       # path to the claude CLI (default: found on PATH)
    "aliases": {},            # account -> how you launch it, e.g. {"personal": "claudep"}
    "retain_days": 60,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


CONFIG = load_config()
DB = os.path.join(STATE, "usage.db")
CG_USER = f"/sys/fs/cgroup/user.slice/user-{UID}.slice/user@{UID}.service"
CG_CLAUDE = os.path.join(CG_USER, "claude.slice")
TICK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
SCOPE_RE = re.compile(r"/cc-([a-z]+)-(\d+)\.scope")
RETAIN_DAYS = CONFIG["retain_days"]


# ---------------------------------------------------------------- helpers

def account_of(config_dir):
    base = os.path.basename((config_dir or "").rstrip("/"))
    if base in ("", ".claude"):
        return "default"
    if base.startswith(".claude-"):
        return re.sub(r"[^a-z0-9]", "", base[len(".claude-"):].lower()) or "default"
    return re.sub(r"[^a-z0-9]", "", base.lower()) or "default"


def config_dir_of(acct):
    return os.path.join(HOME, ".claude" if acct == "default" else f".claude-{acct}")


def read(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return None


def proc_stat(pid):
    """-> (comm, ppid, own_ticks, children_ticks, starttime, rss_bytes) or None"""
    s = read(f"/proc/{pid}/stat")
    if not s:
        return None
    r = s.rfind(")")
    comm = s[s.find("(") + 1:r]
    f = s[r + 2:].split()
    # fields after comm: state(0) ppid(1) ... utime(11) stime(12) cutime(13) cstime(14) ... starttime(19)
    return comm, int(f[1]), int(f[11]) + int(f[12]), int(f[13]) + int(f[14]), int(f[19]), int(f[21]) * PAGE


def proc_rss(pid):
    s = read(f"/proc/{pid}/statm")
    return int(s.split()[1]) * PAGE if s else 0


def proc_cgroup(pid):
    s = read(f"/proc/{pid}/cgroup")
    return s.strip().split("::", 1)[-1] if s else ""


def proc_env(pid):
    s = read(f"/proc/{pid}/environ", "rb")
    if not s:
        return {}
    env = {}
    for kv in s.split(b"\0"):
        if kv.startswith(b"CLAUDE_") and b"=" in kv:
            k, v = kv.decode(errors="replace").split("=", 1)
            env[k] = v
    return env


def proc_argv(pid):
    s = read(f"/proc/{pid}/cmdline", "rb")
    return [a.decode(errors="replace") for a in s.split(b"\0")[:-1]] if s else []


def children_of(pid):
    out = []
    for t in os.listdir(f"/proc/{pid}/task") if os.path.isdir(f"/proc/{pid}/task") else []:
        s = read(f"/proc/{pid}/task/{t}/children")
        if s:
            out += [int(x) for x in s.split()]
    return out


def descendants(pid):
    out, stack = [], [pid]
    while stack:
        p = stack.pop()
        out.append(p)
        stack += children_of(p)
    return out


def is_claude(pid):
    """a claude CLI process (not its embedded ugrep/rg helpers, which keep comm=claude)"""
    st = proc_stat(pid)
    if not st or st[0] != "claude":
        return False
    argv = proc_argv(pid)
    return bool(argv) and os.path.basename(argv[0]) == "claude"


@functools.lru_cache(maxsize=256)
def project_of(cwd):
    if not cwd:
        return "?"
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        top = ""
    p = top or cwd
    if re.match(r"/tmp/claude-\d+/", p):
        return "scratch"
    m = re.search(r"^(.*)/\.claude/worktrees/([^/]+)", p)
    if m:  # Claude agent worktree -> repo + short worktree id
        return f"{os.path.basename(m.group(1))}⎇{m.group(2).removeprefix('agent-')[:6]}"
    return "~" if p == HOME else os.path.basename(p.rstrip("/")) or p


def session_file(acct, pid):
    s = read(os.path.join(config_dir_of(acct), "sessions", f"{pid}.json"))
    try:
        return json.loads(s) if s else {}
    except json.JSONDecodeError:
        return {}


def short(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def bash_tool_command(argv):
    """Claude's Bash tool runs `bash -c 'source snapshot ... && eval '<cmd>' && pwd -P >| ...'`."""
    if len(argv) < 3 or argv[1] != "-c" or "shell-snapshots/snapshot-" not in argv[2]:
        return None
    try:
        toks = shlex.split(argv[2])
        return toks[toks.index("eval") + 1]
    except (ValueError, IndexError):
        return "?"


def unit_label(cg):
    """friendly name for a non-claude cgroup: app-Hyprland-code-1234.scope -> code"""
    if cg in ("", "/"):
        return "kernel threads"
    leaf = cg.rstrip("/").rsplit("/", 1)[-1]
    leaf = re.sub(r"\.(scope|service|slice)$", "", leaf)
    leaf = leaf.replace("\\x2d", "-")
    leaf = re.sub(r"^app-(Hyprland|uwsm|flatpak|gnome)-", "", leaf)
    leaf = re.sub(r"^app-", "", leaf)
    leaf = re.sub(r"(@[0-9a-f]+|-[0-9a-f]{6,}|-\d+)$", "", leaf)
    return leaf or cg


def db():
    os.makedirs(STATE, exist_ok=True)
    c = sqlite3.connect(DB, timeout=5)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, ts REAL, kind TEXT, acct TEXT, claude_pid INTEGER,
        session_id TEXT, cwd TEXT, project TEXT, text TEXT);
    CREATE INDEX IF NOT EXISTS events_pid ON events(claude_pid, ts);
    CREATE TABLE IF NOT EXISTS usage(
        minute INTEGER, acct TEXT, claude_pid INTEGER, session_id TEXT, task_id INTEGER,
        project TEXT, grp TEXT, label TEXT, comm TEXT, cpu_s REAL, rss_mb REAL);
    CREATE INDEX IF NOT EXISTS usage_minute ON usage(minute);
    CREATE TABLE IF NOT EXISTS scopes(
        minute INTEGER, acct TEXT, claude_pid INTEGER, cpu_s REAL, mem_mb REAL);
    CREATE TABLE IF NOT EXISTS names(
        kind TEXT, key TEXT, title TEXT, ts REAL, PRIMARY KEY(kind, key));
    """)
    return c


# ---------------------------------------------------------------- names
# Human names for tasks and heavy commands ("Webapp Checkout Bug Fix", "Billing API Tests").
# Precedence for a session: your /rename title > generated name of the current task > Claude's
# own ai-title > project. Generated by a tiny tool-less haiku call on the session's own account.

NAMER_ENV = "CLAUDE_PROCS_NAMER"   # marks our naming calls: hooks skip them, monitor hides them
NAMER_MAX_PER_HOUR = CONFIG["names_per_hour"]
CMD_NAME_AFTER_CPU_S = 30
TASK_NAME_DELAY_S = 40
TASK_RENAME_AFTER_S = 900   # re-check a busy task's name every 15 min (it keeps the name if nothing changed)

TASK_SYS = ("You name what a coding agent is working on, for a process monitor. Reply with ONLY the name: "
            "2-5 words, Title Case, product or project first, no quotes, no trailing punctuation. Examples: "
            "Webapp Checkout Bug Fix, Billing API Tests, Docs Site Deploy, Shell Config Cleanup. "
            "If the latest request just continues the previous task, reply with the previous name. The heaviest "
            "commands show what is really happening right now: trust them over the session title, which only "
            "reflects how the session started and is often outdated. Vague requests (ok, continue, ?) mean the "
            "work goes on: name it from the commands.")
CMD_SYS = ("You name a group of processes started by a coding agent, for a process monitor. Reply with ONLY "
           "the name: 2-6 words, Title Case, product or project first, saying what it is, no quotes. Examples: "
           "Webapp Release Build, Desktop App Test Instance, Webapp Frontend Checks, Billing API Tests, "
           "API Dev Server, Claude Agent.")


def llm_name(acct, system, prompt, timeout=60):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir_of(acct), **{NAMER_ENV: "1"})
    env.pop("CLAUDE_PID", None)
    try:
        r = subprocess.run(
            [CONFIG.get("claude_bin") or shutil.which("claude") or "claude", "-p",
             "--model", CONFIG["names_model"], "--tools", "", "--no-session-persistence",
             "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "project",
             "--output-format", "json", "--system-prompt", system, prompt],
            capture_output=True, text=True, timeout=timeout, env=env, cwd=STATE)
        out = json.loads(r.stdout).get("result", "") if r.stdout.strip().startswith("{") else ""
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    out = out.strip().splitlines()[0].strip(' "\'.`*') if out.strip() else ""
    return short(out, 48) if 0 < len(out) <= 80 else None


class TranscriptTitles:
    """latest custom-title (/rename) and ai-title of a session, read incrementally from its transcript"""

    def __init__(self):
        self.paths, self.state = {}, {}   # sid -> path ; path -> [offset, custom, ai]

    def get(self, acct, sid):
        if not sid:
            return None, None
        if sid not in self.paths:
            import glob
            hits = glob.glob(os.path.join(config_dir_of(acct), "projects", "*", f"{sid}.jsonl"))
            self.paths[sid] = hits[0] if hits else None
        path = self.paths[sid]
        if not path:
            return None, None
        st = self.state.setdefault(path, [0, None, None])
        try:
            size = os.path.getsize(path)
            if size > st[0]:
                with open(path, "rb") as f:
                    f.seek(st[0])
                    chunk = f.read(size - st[0])
                cut = chunk.rfind(b"\n") + 1
                st[0] += cut
                for line in chunk[:cut].splitlines():
                    if b'"custom-title"' in line or b'"ai-title"' in line:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("type") == "custom-title":
                            st[1] = d.get("customTitle") or st[1]
                        elif d.get("type") == "ai-title":
                            st[2] = d.get("aiTitle") or st[2]
        except OSError:
            pass
        return st[1], st[2]


class Names:
    """names table cache (top/report/who read it; the daemon also writes it)"""

    def __init__(self, conn):
        self.c, self.t, self.map, self.ts = conn, 0, {}, {}

    def refresh(self):
        for kind, key, title, ts in self.c.execute("SELECT kind,key,title,ts FROM names WHERE ts>?", (self.t,)):
            self.map[(kind, key)] = title
            self.ts[(kind, key)] = ts
            self.t = max(self.t, ts)

    def task(self, tid):
        return self.map.get(("task", str(tid))) if tid else None

    def cmd(self, acct, cpid, label):
        return self.map.get(("cmd", cmd_key(acct, cpid, label)))


def cmd_key(acct, cpid, label):
    return f"{acct}:{cpid}:{label}"


def session_title(acct, cpid, tasks, names, titles):
    """-> (title, source) for a claude session"""
    sf = session_file(acct, cpid)
    t = tasks.by_pid.get(cpid)
    custom, ai = titles.get(acct, sf.get("sessionId") or (t and t[4]))
    if custom:
        return custom, "renamed"
    if t:
        n = names.task(t[0])
        if n:
            return n, "task"
        prev = names.c.execute(
            "SELECT n.title FROM names n JOIN events e ON n.kind='task' AND n.key=CAST(e.id AS TEXT) "
            "WHERE e.claude_pid=? ORDER BY e.id DESC LIMIT 1", (cpid,)).fetchone()
        if prev:
            return prev[0], "task"
    if ai:
        return ai, "ai-title"
    proj = (t[3] if t else None) or (project_of(sf["cwd"]) if sf.get("cwd") else None)
    return (proj or "Claude session"), "project"


class Namer:
    """background naming worker for the daemon: one call at a time, capped per hour"""

    def __init__(self):
        import queue
        import threading
        self.q, self.pending, self.calls, self.failed = queue.Queue(), set(), [], {}
        self.enabled = CONFIG["names"] and os.environ.get("CLAUDE_PROCS_NAMES", "on") != "off"
        threading.Thread(target=self.run, daemon=True).start()

    def want(self, kind, key, acct, system, prompt_fn, force=False):
        if self.enabled and (kind, key) not in self.pending and time.time() - self.failed.get((kind, key), 0) > 600:
            self.pending.add((kind, key))
            self.q.put((kind, key, acct, system, prompt_fn, force))

    def run(self):
        conn = db()
        while True:
            kind, key, acct, system, prompt_fn, force = self.q.get()
            try:
                if not force and conn.execute("SELECT 1 FROM names WHERE kind=? AND key=?", (kind, key)).fetchone():
                    self.pending.discard((kind, key))
                    continue
                now = time.time()
                self.calls = [t for t in self.calls if now - t < 3600]
                if len(self.calls) >= NAMER_MAX_PER_HOUR:
                    self.pending.discard((kind, key))   # retry later
                    time.sleep(30)
                    continue
                prompt = prompt_fn(conn)
                if not prompt:
                    continue
                self.calls.append(now)
                title = llm_name(acct, system, prompt)
                if title:
                    conn.execute("INSERT OR REPLACE INTO names VALUES(?,?,?,?)", (kind, key, title, time.time()))
                    conn.commit()
                    self.pending.discard((kind, key))
                else:
                    self.failed[(kind, key)] = time.time()
                    self.pending.discard((kind, key))
            except Exception as e:
                with open(os.path.join(STATE, "namer-errors.log"), "a") as f:
                    f.write(f"{time.ctime()} {type(e).__name__}: {e}\n")


def task_prompt(acct, cpid, tid, titles):
    def build(conn):
        ev = conn.execute("SELECT ts, session_id, project FROM events WHERE id=?", (tid,)).fetchone()
        if not ev:
            return None
        ts, sid, proj = ev
        reqs = [r[0] for r in conn.execute(
            "SELECT text FROM events WHERE claude_pid=? AND kind='prompt' AND id<=? ORDER BY id DESC LIMIT 4",
            (cpid, tid))][::-1]
        prev = conn.execute(
            "SELECT n.title FROM names n JOIN events e ON n.kind='task' AND n.key=CAST(e.id AS TEXT) "
            "WHERE e.claude_pid=? AND e.id<=? ORDER BY e.id DESC LIMIT 1", (cpid, tid)).fetchone()
        cmds = conn.execute(
            "SELECT coalesce(project,'?'), label, sum(cpu_s) FROM usage WHERE claude_pid=? AND minute>=? "
            "AND grp!='agent' GROUP BY project, label ORDER BY sum(cpu_s) DESC LIMIT 6",
            (cpid, int(max(ts, time.time() - 1200) // 60 * 60))).fetchall()
        custom, ai = titles.get(acct, sid)
        lines = [f"Session started in project: {proj or '?'}"]
        if ai:
            lines.append(f"Session title (from its first request, may be outdated): {ai}")
        if prev:
            lines.append(f"Previous task name: {prev[0]}")
        lines.append("Recent requests, oldest first:")
        lines += [f"- {short(r, 300)}" for r in reqs]
        if cmds:
            lines.append("Heaviest commands since the latest request (project: command):")
            lines += [f"- {p}: {short(lab, 140)}" for p, lab, _ in cmds]
        return "\n".join(lines)
    return build


def cmd_prompt(proj, label, comms, task_name):
    def build(_conn):
        return "\n".join([f"Project: {proj or '?'}", f"Command: {label}",
                          f"Busiest processes: {', '.join(comms) or '?'}",
                          f"The agent's current task: {task_name or '?'}"])
    return build


# ---------------------------------------------------------------- cgroup scopes

def scope_cgroup(pid):
    m = SCOPE_RE.search(proc_cgroup(pid))
    return (m.group(1), int(m.group(2))) if m else None


def move_to_scope(pid, acct):
    """Put a claude process tree into claude.slice/claude-<acct>.slice/cc-<acct>-<pid>.scope."""
    if scope_cgroup(pid):
        return "already"
    pids = descendants(pid)
    # keep "closing the terminal kills everything it started": bind to the unit we leave
    origin = proc_cgroup(pid).rstrip("/").rsplit("/", 1)[-1]
    bind = []
    if re.search(r"\.(scope|service)$", origin):
        bind = ["BindsTo", "as", "1", origin, "After", "as", "1", origin]
    args = ["busctl", "--user", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
            f"cc-{acct}-{pid}.scope", "fail", str(4 + len(bind) // 4),
            "PIDs", "au", str(len(pids)), *map(str, pids),
            "Slice", "s", f"claude-{acct}.slice",
            "Description", "s", f"Claude Code ({acct}) pid {pid}",
            "CollectMode", "s", "inactive-or-failed", *bind, "0"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=5)
    return "moved" if r.returncode == 0 else f"failed: {r.stderr.strip()}"


def find_claude_pid():
    p = os.environ.get("CLAUDE_PID")
    if p and p.isdigit() and is_claude(int(p)):
        return int(p)
    pid = os.getppid()
    while pid > 1:
        if is_claude(pid):
            return pid
        st = proc_stat(pid)
        if not st:
            break
        pid = st[1]
    return None


# ---------------------------------------------------------------- hook

def cmd_hook(_):
    """Never prints (SessionStart/UserPromptSubmit stdout becomes model context), never fails."""
    if os.environ.get(NAMER_ENV):
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    try:
        ev = data.get("hook_event_name", "")
        pid = find_claude_pid()
        acct = account_of(os.environ.get("CLAUDE_CONFIG_DIR"))
        cwd = data.get("cwd") or os.getcwd()
        if ev == "SessionStart":
            res = move_to_scope(pid, acct) if pid else "no-claude-pid"
            kind, text = "session", f"{data.get('source', '')} scope:{res}"
        elif ev == "UserPromptSubmit":
            prompt = data.get("prompt", "").lstrip()
            if re.match(r"<[a-z][a-z-]*[\s>]", prompt):
                return  # machine-generated turn, not a new human task
            kind, text = "prompt", short(prompt, 300)
        else:
            return
        c = db()
        c.execute("INSERT INTO events(ts,kind,acct,claude_pid,session_id,cwd,project,text) VALUES(?,?,?,?,?,?,?,?)",
                  (time.time(), kind, acct, pid, data.get("session_id"), cwd, project_of(cwd), text))
        c.commit()
    except Exception as e:
        try:
            os.makedirs(STATE, exist_ok=True)
            with open(os.path.join(STATE, "hook-errors.log"), "a") as f:
                f.write(f"{time.ctime()} {type(e).__name__}: {e}\n")
        except OSError:
            pass


# ---------------------------------------------------------------- sampler

def cpu_deltas(prev, cur, first=False, tick=None):
    """CPU seconds each process used between two /proc snapshots.

    prev, cur: {pid: (starttime, own_ticks, children_ticks, ppid, comm)}
    -> [(pid, start, comm, ppid, own_s, short_lived_children_s)]

    Every process that died since `prev` had its last-known time (own + reaped children) already
    counted. When it is reaped, that time cascades into the cutime of its nearest *live* ancestor
    (a whole `cargo build` tree can exit within one interval), so it is subtracted there; what is
    left in the cutime increase is processes that lived and died entirely between samples, which
    we report as "<comm> children".
    """
    tick = tick or TICK

    def alive(p):
        return p in cur and (p not in prev or cur[p][0] == prev[p][0])

    reaped = {}
    for pid, (start, own, ch, ppid, _comm) in prev.items():
        if pid in cur and cur[pid][0] == start:
            continue
        anc, hops = ppid, 0
        while anc in prev and not alive(anc) and hops < 64:
            anc, hops = prev[anc][3], hops + 1
        reaped[anc] = reaped.get(anc, 0) + own + ch
    out = []
    for pid, (start, own, ch, ppid, comm) in cur.items():
        p = prev.get(pid)
        if p and p[0] == start:
            d_own = own - p[1]
            d_short = max(0, (ch - p[2]) - reaped.get(pid, 0))
        elif first:
            d_own = d_short = 0
        else:
            d_own, d_short = own + ch, 0
        out.append((pid, start, comm, ppid, d_own / tick, d_short / tick))
    return out


class Sampler:
    """Per-process CPU deltas, including processes that started and exited between samples:
    their time shows up in the reaping parent's cutime/cstime, which we credit to the parent's
    owner as '<parent> children'."""

    def __init__(self):
        self.prev = {}        # pid -> (start, own, ch, ppid, comm)
        self.envcache = {}    # (pid,start) -> claude_pid or 0
        self.labelcache = {}  # (pid,start) -> (grp,label)
        self.scope_prev = {}  # cg path -> usage_usec
        self.t = None
        self.first = True
        self.rss = {}
        self.root_pids = {}   # (acct, claude_pid, label) -> root pids, from the last attribute()
        self.cgcache = {}     # (pid,start) -> cgroup path (refreshed every 60s: adopt moves processes)
        self.cg_t = 0

    def cgroup(self, pid, start):
        key = (pid, start)
        if key not in self.cgcache:
            self.cgcache[key] = proc_cgroup(pid)
        return self.cgcache[key]

    def owner(self, pid, start, cg, comm=""):
        """-> (acct, claude_pid, escaped) or None"""
        m = SCOPE_RE.search(cg)
        if m:
            return m.group(1), int(m.group(2)), False
        key = (pid, start)
        if key not in self.envcache:
            env = proc_env(pid)
            cp = env.get("CLAUDE_PID", "")
            self.envcache[key] = (account_of(env.get("CLAUDE_CONFIG_DIR")), int(cp)) if cp.isdigit() else None
        o = self.envcache[key]
        if o is None and comm == "claude" and is_claude(pid):
            # a claude process has no CLAUDE_PID itself (only its children do): it owns itself
            env = proc_env(pid)
            if not env.get(NAMER_ENV):
                o = self.envcache[key] = (account_of(env.get("CLAUDE_CONFIG_DIR")), pid)
        return (o[0], o[1], True) if o else None

    def sample(self):
        now = time.time()
        dt = now - self.t if self.t else 0
        cur, procs = {}, []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            pid = int(d)
            st = proc_stat(pid)
            if not st:
                continue
            comm, ppid, own, ch, start, rss = st
            cur[pid] = (start, own, ch, ppid, comm)
            self.rss[pid] = rss
        procs = cpu_deltas(self.prev, cur, self.first)
        self.prev, self.t, self.first = cur, now, False
        self.rss = {p: r for p, r in self.rss.items() if p in cur}
        if now - self.cg_t > 60:
            self.cgcache, self.cg_t = {}, now
        for cache in (self.envcache, self.cgcache):
            for k in [k for k in cache if k[0] not in cur or cur[k[0]][0] != k[1]]:
                del cache[k]
        for k in [k for k in self.labelcache if k[0] not in cur or cur[k[0]][0] != k[1]]:
            del self.labelcache[k]
        return now, dt, cur, procs

    def root_of(self, pid, cur, cpid):
        """ancestor whose parent is the claude process (= one Bash command / MCP server / ...)"""
        if pid == cpid:
            return pid
        seen = 0
        while pid in cur and seen < 64:
            ppid = cur[pid][3]
            if ppid == cpid:
                return pid
            if ppid <= 1 or ppid not in cur:
                return pid
            po = self.owner(ppid, cur[ppid][0], self.cgroup(ppid, cur[ppid][0]), cur[ppid][4])
            if not po or po[1] != cpid:
                return pid  # detached (e.g. reparented to systemd): topmost ancestor still ours
            pid, seen = ppid, seen + 1
        return pid

    def label(self, rpid, cur, cpid):
        key = (rpid, cur[rpid][0])
        if key in self.labelcache:
            return self.labelcache[key]
        comm = cur[rpid][4]
        if rpid == cpid:
            v = ("agent", "claude (agent itself)", None)
        else:
            argv = proc_argv(rpid)
            cmd = bash_tool_command(argv)
            if cmd is not None:
                v = ("cmd", "$ " + short(cmd.splitlines()[0] if cmd else cmd, 120), self.cwd_project(rpid))
            else:
                args = " ".join(os.path.basename(a) if a.startswith("/") else a for a in argv[:4])
                grp = "mcp" if re.search(r"mcp|server", " ".join(argv), re.I) else "proc"
                v = (grp, short(args or comm, 120), self.cwd_project(rpid))
        self.labelcache[key] = v
        return v

    @staticmethod
    def cwd_project(pid):
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
        return None if cwd == HOME else project_of(cwd)

    def scope_totals(self):
        """cgroup truth per claude scope: {(acct,pid): (cpu_s_delta, mem_bytes)}"""
        out = {}
        if not os.path.isdir(CG_CLAUDE):
            return out
        for root, dirs, _ in os.walk(CG_CLAUDE):
            for d in dirs:
                m = SCOPE_RE.search("/" + d)
                if not m:
                    continue
                p = os.path.join(root, d)
                cs = read(p + "/cpu.stat") or ""
                u = int(cs.split()[1]) if cs.startswith("usage_usec") else 0
                mem = int((read(p + "/memory.current") or "0").strip() or 0)
                prev = self.scope_prev.get(p)
                self.scope_prev[p] = u
                out[(m.group(1), int(m.group(2)))] = ((u - prev) / 1e6 if prev is not None else 0.0, mem)
        return out


class Tasks:
    """latest prompt per claude pid, from the events table"""

    def __init__(self, conn):
        self.c, self.last, self.by_pid, self.sess = conn, 0, {}, {}

    def refresh(self):
        for id_, ts, kind, _acct, pid, sid, _cwd, proj, text in self.c.execute(
                "SELECT id,ts,kind,acct,claude_pid,session_id,cwd,project,text FROM events WHERE id>? ORDER BY id",
                (self.last,)):
            self.last = id_
            if kind == "prompt":
                self.by_pid[pid] = (id_, ts, text, proj, sid)
            elif kind == "session":
                self.sess[pid] = (sid, proj)

    def bootstrap(self, live_pids):
        """load the latest prompt of each running session (for top/who without the full scan)"""
        for pid in live_pids:
            r = self.c.execute("SELECT id,ts,text,project,session_id FROM events WHERE claude_pid=? AND kind='prompt' "
                               "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
            if r:
                self.by_pid[pid] = r
        self.last = (self.c.execute("SELECT max(id) FROM events").fetchone()[0] or 0)


def attribute(sampler, tasks, cur, procs, want_other=True):
    """-> rows: dict(key)->[cpu_s, rss_bytes]   key = (acct,cpid,grp,label,comm) or ('-',0,'other',unit,comm)"""
    rows = {}

    def add(key, cpu, rss):
        r = rows.setdefault(key, [0.0, 0])
        r[0] += cpu
        r[1] += rss

    sampler.root_pids = {}
    for pid, start, comm, _ppid, d_own, d_short in procs:
        cg = sampler.cgroup(pid, start)
        o = sampler.owner(pid, start, cg, comm)
        rss = sampler.rss.get(pid, 0)
        if o:
            acct, cpid, escaped = o
            rpid = sampler.root_of(pid, cur, cpid)
            grp, lab, proj = sampler.label(rpid, cur, cpid)
            if rpid != cpid and cur.get(rpid, (0, 0, 0, 0))[3] != cpid:
                grp, lab = "detached", f"{lab}  [detached]"
            add((acct, cpid, grp, lab, comm, proj), d_own, rss)
            sampler.root_pids.setdefault((acct, cpid, lab), set()).add(rpid)
            if d_short:
                add((acct, cpid, grp, lab, f"{comm} children", proj), d_short, 0)
        elif want_other:
            if not (d_own or d_short or rss > 50 << 20):
                continue
            unit = unit_label(cg)
            add(("-", 0, "other", unit, comm, unit), d_own + d_short, rss)
    return rows


def cmd_daemon(a):
    conn = db()
    tasks = Tasks(conn)
    tasks.refresh()
    s = Sampler()
    agg, sagg, minute = {}, {}, None
    last_prune = 0
    namer, titles, names = Namer(), TranscriptTitles(), Names(conn)
    cmd_cpu, live, live_t, busy = {}, {}, 0, {}

    def stop(*_):
        if minute is not None:
            flush(conn, minute, agg, sagg, tasks)
        sys.exit(0)

    import signal
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        now, dt, cur, procs = s.sample()
        tasks.refresh()
        rows = attribute(s, tasks, cur, procs)
        scopes = s.scope_totals()
        m = int(now // 60 * 60)
        if minute is not None and m != minute:
            flush(conn, minute, agg, sagg, tasks)
            agg, sagg = {}, {}
        minute = m
        for k, (cpu, rss) in rows.items():
            if k[2] == "other" and cpu < 0.2 and rss < 300 << 20:
                continue
            tid = tasks.by_pid.get(k[1], (None,))[0] if k[1] else None
            r = agg.setdefault(k + (tid,), [0.0, 0])
            r[0] += cpu
            r[1] = max(r[1], rss)
        for k, (cpu, mem) in scopes.items():
            r = sagg.setdefault(k, [0.0, 0])
            r[0] += cpu
            r[1] = max(r[1], mem)
        # names: the current task of each live session, and every command that got heavy
        if now - live_t > 30:
            live, live_t = live_sessions(), now
            names.refresh()
        for cpid, acct in live.items():
            t = tasks.by_pid.get(cpid)
            if not t or now - t[1] < TASK_NAME_DELAY_S:
                continue
            if not names.task(t[0]):
                namer.want("task", str(t[0]), acct, TASK_SYS, task_prompt(acct, cpid, t[0], titles))
            elif now - names.ts.get(("task", str(t[0])), now) > TASK_RENAME_AFTER_S \
                    and busy.get(cpid, 0) >= 60:
                busy[cpid] = 0
                namer.want("task", str(t[0]), acct, TASK_SYS, task_prompt(acct, cpid, t[0], titles), force=True)
        per_root = {}
        for (acct, cpid, grp, lab, comm, rproj), (cpu, _rss) in rows.items():
            if acct == "-" or grp == "agent":
                continue
            pr = per_root.setdefault((acct, cpid, lab), [0.0, rproj, {}])
            pr[0] += cpu
            pr[2][comm] = pr[2].get(comm, 0) + cpu
        for (acct, cpid, lab), (cpu, rproj, comms) in per_root.items():
            busy[cpid] = busy.get(cpid, 0) + cpu
            k = (acct, cpid, lab)
            cmd_cpu[k] = cmd_cpu.get(k, 0) + cpu
            if cmd_cpu[k] >= CMD_NAME_AFTER_CPU_S and not names.cmd(acct, cpid, lab):
                t = tasks.by_pid.get(cpid)
                top_comms = [c for c, _ in sorted(comms.items(), key=lambda kv: -kv[1])[:4]]
                namer.want("cmd", cmd_key(acct, cpid, lab), acct, CMD_SYS,
                           cmd_prompt(rproj, lab, top_comms, names.task(t[0]) if t else None))
        if len(cmd_cpu) > 5000:
            cmd_cpu.clear()
        if now - last_prune > 86400:
            conn.execute("DELETE FROM usage WHERE minute < ?", (now - RETAIN_DAYS * 86400,))
            conn.execute("DELETE FROM scopes WHERE minute < ?", (now - RETAIN_DAYS * 86400,))
            conn.commit()
            last_prune = now
        time.sleep(max(0.5, a.interval - (time.time() - now)))


def flush(conn, minute, agg, sagg, tasks):
    out = []
    for (acct, cpid, grp, lab, comm, rproj, tid), (cpu, rss) in agg.items():
        if cpu < 0.05 and rss < 20 << 20:
            continue
        sid, proj = None, None
        if cpid:
            t = tasks.by_pid.get(cpid)
            ss = tasks.sess.get(cpid)
            sf = session_file(acct, cpid)
            sid = (t and t[4]) or (ss and ss[0]) or sf.get("sessionId")
            proj = rproj or (t and t[3]) or (ss and ss[1]) or (project_of(sf.get("cwd")) if sf.get("cwd") else None)
        out.append((minute, acct, cpid, sid, tid, proj, grp, lab, comm, round(cpu, 2), round(rss / 2**20, 1)))
    conn.executemany("INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?)", out)
    conn.executemany("INSERT INTO scopes VALUES(?,?,?,?,?)",
                     [(minute, a, p, round(c, 2), round(m / 2**20, 1)) for (a, p), (c, m) in sagg.items()])
    conn.commit()


# ---------------------------------------------------------------- top

def fmt_cpu(pct):
    return f"{pct:5.0f}%"


def fmt_mem(b):
    mb = b / 2**20
    return f"{mb / 1024:5.1f}G" if mb >= 1024 else f"{mb:5.0f}M"


def ago(ts):
    s = time.time() - ts
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.0f}m" if s < 5400 else f"{s / 3600:.1f}h"


def live_sessions():
    out = {}
    for d in os.listdir("/proc"):
        if d.isdigit() and is_claude(int(d)):
            env = proc_env(int(d))
            if not env.get(NAMER_ENV):
                out[int(d)] = account_of(env.get("CLAUDE_CONFIG_DIR"))
    return out


def snapshot(rows, dt, tasks, scopes, names, titles, live, root_pids=None, show_all=False):
    """Structured live view: accounts > sessions > roots (commands), plus non-Claude apps.
    Percentages are of one core."""
    dt = dt or 1
    pct = lambda cpu: cpu / dt * 100  # noqa: E731
    root_pids = root_pids or {}
    sess = {}
    for (acct, cpid, grp, lab, comm, rproj), (cpu, rss) in rows.items():
        if acct == "-":
            continue
        s = sess.setdefault((acct, cpid), {"cpu": 0, "rss": 0, "roots": {}})
        s["cpu"] += cpu
        s["rss"] += rss
        r = s["roots"].setdefault((grp, lab), {"cpu": 0, "rss": 0, "comms": {}, "proj": rproj})
        r["cpu"] += cpu
        r["rss"] += rss
        c = r["comms"].setdefault(comm, [0, 0])
        c[0] += cpu
        c[1] += rss
    for pid, acct in live.items():
        sess.setdefault((acct, pid), {"cpu": 0, "rss": 0, "roots": {}})
    total = pct(sum(v[0] for v in rows.values()))
    claude = pct(sum(v[0] for k, v in rows.items() if k[0] != "-"))
    accounts = []
    for acct in sorted({a for a, _ in sess}, key=lambda x: (x != "personal", x != "work", x)):
        items = sorted(((k, v) for k, v in sess.items() if k[0] == acct), key=lambda kv: -kv[1]["cpu"])
        out_sessions = []
        for (a, cpid), v in items:
            alive = cpid in live
            if not show_all and not alive and pct(v["cpu"]) < 0.5 and v["rss"] < 200 << 20:
                continue
            sf = session_file(a, cpid)
            t = tasks.by_pid.get(cpid)
            title, src = session_title(a, cpid, tasks, names, titles)
            sc = scopes.get((a, cpid))
            roots = []
            for (grp, lab), r in sorted(v["roots"].items(), key=lambda kv: -kv[1]["cpu"]):
                if not show_all and pct(r["cpu"]) < 0.5 and r["rss"] < 250 << 20:
                    continue
                comms = sorted(((c, pct(x[0])) for c, x in r["comms"].items()), key=lambda kv: -kv[1])
                roots.append({
                    "grp": grp, "label": lab, "proj": r["proj"], "pct": pct(r["cpu"]), "rss": r["rss"],
                    "title": "Claude agent" if grp == "agent" else names.cmd(a, cpid, lab),
                    "comms": [(c, x) for c, x in comms if x >= 1][:4],
                    "pids": sorted(root_pids.get((a, cpid, lab), ())),
                })
            out_sessions.append({
                "acct": a, "cpid": cpid, "title": title, "title_src": src, "alive": alive,
                "proj": (t[3] if t else None) or (project_of(sf["cwd"]) if sf.get("cwd") else "?"),
                "session_id": sf.get("sessionId") or (t[4] if t else None), "cwd": sf.get("cwd"),
                "pct": pct(v["cpu"]), "rss": v["rss"],
                "cgroup_pct": pct(sc[0]) if sc else None, "in_cgroup": bool(sc) or bool(scope_cgroup(cpid)),
                "last_prompt": (t[1], t[2]) if t else None, "roots": roots,
            })
        accounts.append({"acct": acct, "alias": (CONFIG.get("aliases") or {}).get(acct),
                         "pct": sum(x["pct"] for x in out_sessions), "sessions": out_sessions})
    other = {}
    for (acct, _, _grp, unit, comm, _p), (cpu, rss) in rows.items():
        if acct != "-":
            continue
        o = other.setdefault(unit, {"unit": unit, "cpu": 0, "rss": 0, "comms": {}})
        o["cpu"] += cpu
        o["rss"] += rss
        o["comms"][comm] = o["comms"].get(comm, 0) + cpu
    others = []
    for o in sorted(other.values(), key=lambda o: -o["cpu"]):
        others.append({"unit": o["unit"], "pct": pct(o["cpu"]), "rss": o["rss"],
                       "comms": [(c, pct(x)) for c, x in sorted(o["comms"].items(), key=lambda kv: -kv[1])
                                 if pct(x) >= 1][:3]})
    return {"t": time.time(), "dt": dt, "total_pct": total, "claude_pct": claude,
            "accounts": accounts, "other": others}


def render_plain(snap, show_all=False, color=None):
    color = sys.stdout.isatty() if color is None else color
    B, D, R = ("\033[1m", "\033[2m", "\033[0m") if color else ("", "", "")
    lines = [f"{B}claude-procs{R}  {time.strftime('%H:%M:%S', time.localtime(snap['t']))}   "
             f"all procs {snap['total_pct']:.0f}%  claude-induced {snap['claude_pct']:.0f}%  "
             f"other {snap['total_pct'] - snap['claude_pct']:.0f}%   {D}(100% = one core){R}"]
    for acc in snap["accounts"]:
        alias = f" ({acc['alias']})" if acc["alias"] else ""
        lines += ["", f"{B}{acc['acct'].upper()}{alias}{R}  {acc['pct']:.0f}%"]
        for se in acc["sessions"]:
            cg = (f" · cgroup {se['cgroup_pct']:.0f}%" if se["cgroup_pct"] is not None else
                  ("" if se["in_cgroup"] or not se["alive"] else " · not in own cgroup: run `claude-procs adopt`"))
            lines.append(f"  {B}{fmt_cpu(se['pct'])} {fmt_mem(se['rss'])}  {short(se['title'], 48)}{R}"
                         f"  {D}{se['proj']} · pid {se['cpid']}{' · renamed' if se['title_src'] == 'renamed' else ''}"
                         f"{'' if se['alive'] else ' · session ended'}{cg}{R}")
            if se["cgroup_pct"] is not None and se["cgroup_pct"] - se["pct"] >= 5:
                lines.append(f"  {fmt_cpu(se['cgroup_pct'] - se['pct'])} {'':6}    "
                             f"{D}(exited between samples / timing skew; cgroup total is exact){R}")
            if se["last_prompt"]:
                lines.append(f"  {D}{'':13}last prompt {ago(se['last_prompt'][0])} ago: "
                             f"{short(se['last_prompt'][1], 90)}{R}")
            for r in se["roots"]:
                cs = ", ".join(f"{c} {x:.0f}%" for c, x in r["comms"])
                pj = f"{r['proj']}  " if r["proj"] and r["proj"] != se["proj"] else ""
                if r["title"]:
                    lines.append(f"  {fmt_cpu(r['pct'])} {fmt_mem(r['rss'])}    {r['title']}  "
                                 f"{D}{pj}{short(r['label'], 55)}  {cs}{R}")
                else:
                    lines.append(f"  {fmt_cpu(r['pct'])} {fmt_mem(r['rss'])}    {B}{pj}{R}{short(r['label'], 70)}  {D}{cs}{R}")
    lines += ["", f"{B}NOT CLAUDE{R}  {snap['total_pct'] - snap['claude_pct']:.0f}%"]
    for o in snap["other"][: 20 if show_all else 8]:
        if o["pct"] < 1 and not show_all:
            continue
        cs = ", ".join(f"{c} {x:.0f}%" for c, x in o["comms"])
        lines.append(f"  {fmt_cpu(o['pct'])} {fmt_mem(o['rss'])}  {short(o['unit'], 50)}  {D}{cs}{R}")
    return "\n".join(lines)


class Live:
    """one sampler + caches, producing snapshots (used by `top` and the interactive UI)"""

    def __init__(self):
        self.s = Sampler()
        self.conn = db()
        self.tasks = Tasks(self.conn)
        self.live = live_sessions()
        self.tasks.bootstrap(self.live)
        self.names, self.titles = Names(self.conn), TranscriptTitles()
        self.s.sample()
        self.s.scope_totals()
        self.live_t = time.time()

    def snap(self, show_all=False):
        now, dt, cur, procs = self.s.sample()
        self.tasks.refresh()
        self.names.refresh()
        if now - self.live_t > 10:
            self.live, self.live_t = live_sessions(), now
        rows = attribute(self.s, self.tasks, cur, procs)
        return snapshot(rows, dt, self.tasks, self.s.scope_totals(), self.names, self.titles,
                        self.live, self.s.root_pids, show_all)


def cmd_top(a):
    lv = Live()
    try:
        while True:
            time.sleep(a.interval)
            out = render_plain(lv.snap(a.all), a.all)
            if a.once:
                print(out)
                return
            sys.stdout.write("\033[H\033[2J" + out + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- who

def cmd_who(a):
    conn = db()
    s = Sampler()
    s.sample()
    cur = s.prev
    for pid in a.pids:
        if pid not in cur:
            print(f"{pid}: no such process")
            continue
        start, _, _, _, comm = cur[pid]
        cg = proc_cgroup(pid)
        o = s.owner(pid, start, cg, comm)
        if not o:
            print(f"{pid} {comm}: not Claude-induced  (cgroup unit: {unit_label(cg)})")
            print(f"    {short(' '.join(proc_argv(pid)), 160)}")
            continue
        acct, cpid, escaped = o
        rpid = s.root_of(pid, cur, cpid)
        grp, lab, rproj = s.label(rpid, cur, cpid)
        sf = session_file(acct, cpid)
        t = conn.execute("SELECT ts,text,project,id FROM events WHERE claude_pid=? AND kind='prompt' AND ts<=? "
                         "ORDER BY id DESC LIMIT 1",
                         (cpid, time.time())).fetchone()
        names, tasks = Names(conn), Tasks(conn)
        names.refresh()
        tasks.bootstrap([cpid])
        title, _ = session_title(acct, cpid, tasks, names, TranscriptTitles())
        cn = names.cmd(acct, cpid, lab)
        started = time.time() - (float(read("/proc/uptime").split()[0]) - start / TICK)
        print(f"{pid} {comm}: {title}  ({acct}, session pid {cpid} {'alive' if is_claude(cpid) else 'ended'})"
              f"{'  (not in its own cgroup yet)' if escaped else ''}")
        print(f"    session : {sf.get('name', '?')}  {sf.get('sessionId', '')}  cwd {sf.get('cwd', '?')}")
        print(f"    via     : {(cn + ' = ') if cn else ''}{lab}  (root pid {rpid}{', project ' + rproj if rproj else ''})")
        print(f"    started : {ago(started)} ago")
        if t:
            print(f"    task    : ({ago(t[0])} ago, {t[2]}) {short(t[1], 140)}")


# ---------------------------------------------------------------- report

def parse_since(s):
    if s == "today":
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", s)
    if not m:
        raise ValueError("--since: use e.g. 30m, 6h, 7d or today")
    return time.time() - float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


REPORT_KEYS = {
    "task": "u.acct, coalesce(nt.title, coalesce(u.project,'?') || ': ' || coalesce(e.text,'(before first prompt)'))",
    "session": "u.acct, u.claude_pid, coalesce((SELECT n2.title FROM names n2 JOIN events e2 ON n2.kind='task' "
               "AND n2.key=CAST(e2.id AS TEXT) WHERE e2.claude_pid=u.claude_pid ORDER BY e2.id DESC LIMIT 1), "
               "coalesce(u.project,'?'))",
    "project": "u.acct, coalesce(u.project,'?')",
    "cmd": "u.acct, coalesce(u.project,'?'), coalesce(nc.title, u.label)",
    "comm": "u.acct, u.comm",
    "account": "u.acct",
}


def report_rows(by="task", since="24h", acct=None, other=False, n=25, conn=None):
    """-> (span_seconds, [{"key": [..], "cpu_s", "avg_pct", "rss_mb", "first", "last"}])"""
    conn = conn or db()
    t0 = parse_since(since) if isinstance(since, str) else since
    keys = REPORT_KEYS[by]
    where = "u.minute >= ?" + ("" if other else " AND u.acct != '-'")
    params = [t0]
    if acct:
        where += " AND u.acct = ?"
        params.append(acct)
    q = (f"SELECT {keys}, sum(u.cpu_s), max(u.rss_mb), min(u.minute), max(u.minute) "
         f"FROM usage u LEFT JOIN events e ON e.id = u.task_id "
         f"LEFT JOIN names nt ON nt.kind='task' AND nt.key=CAST(u.task_id AS TEXT) "
         f"LEFT JOIN names nc ON nc.kind='cmd' AND nc.key=u.acct || ':' || u.claude_pid || ':' || u.label "
         f"WHERE {where} GROUP BY {keys} ORDER BY sum(u.cpu_s) DESC LIMIT ?")
    rows = conn.execute(q, params + [n]).fetchall()
    first = conn.execute("SELECT min(minute) FROM usage").fetchone()[0] or t0
    span = max(60, time.time() - max(t0, first))
    out = []
    for r in rows:
        *k, cpu, rss, f, last = r
        out.append({"key": [str(x) for x in k if x not in (None, "")], "cpu_s": cpu, "avg_pct": cpu / span * 100,
                    "rss_mb": rss, "first": f, "last": last + 60})
    return span, out


def fmt_dur(sec):
    return f"{sec / 3600:.1f}h" if sec >= 3600 else f"{sec / 60:.1f}m" if sec >= 60 else f"{sec:.0f}s"


def cmd_report(a):
    try:
        _, rows = report_rows(a.by, a.since, a.acct, a.other, a.n)
    except ValueError as e:
        sys.exit(str(e))
    if not rows:
        print("no data yet (is the daemon running?  systemctl --user status claude-procs)")
        return
    print(f"{'CPU time':>9} {'avg':>5} {'peak RSS':>8}  {'last seen':>9}  {a.by}")
    for r in rows:
        print(f"{fmt_dur(r['cpu_s']):>9} {r['avg_pct']:4.0f}% {fmt_mem(r['rss_mb'] * 2**20):>8}  "
              f"{ago(r['last']):>9}  " + "  ·  ".join(short(x, 90) for x in r["key"]))


# ---------------------------------------------------------------- adopt

def cmd_adopt(_):
    live = live_sessions()
    tagged = {}  # claude pid -> processes carrying its CLAUDE_PID (detached ones included)
    for d in os.listdir("/proc"):
        if d.isdigit():
            cp = proc_env(int(d)).get("CLAUDE_PID", "")
            if cp.isdigit() and int(cp) in live and not scope_cgroup(int(d)):
                tagged.setdefault(int(cp), []).append(int(d))
    for pid, acct in sorted(live.items()):
        res = move_to_scope(pid, acct)
        rest = [p for p in tagged.get(pid, []) if not scope_cgroup(p)]
        if rest and res in ("moved", "already"):
            procs = os.path.join(CG_USER, proc_cgroup(pid).split("user@%d.service/" % UID, 1)[-1], "cgroup.procs")
            ok = 0
            for p in rest:
                try:
                    with open(procs, "w") as f:
                        f.write(str(p))
                    ok += 1
                except OSError:
                    pass
            res += f", +{ok}/{len(rest)} detached"
        print(f"{acct:9} pid {pid:>7}: {res}")
