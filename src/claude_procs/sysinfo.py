"""Per-OS process access. Linux reads /proc (exact, with cgroups); macOS uses `ps` + sysctl(KERN_PROCARGS2).

Every backend exposes the same functions:
  all_procs()       {pid: (comm, ppid, own_ticks, children_ticks, start, rss_bytes)}
  proc_stat(pid)    one row of the above, or None
  proc_argv(pid)    [argv...]
  proc_env(pid)     {CLAUDE_*: value}   (only CLAUDE_ variables are kept)
  proc_cgroup(pid)  cgroup v2 path, "" where there are no cgroups
  proc_cwd(pid)     working directory or None
  children_of(pid)  [pid...]
  started_epoch(start)  wall-clock start time of a process from its `start` value
  machine(state)    -> (dict, state): busy (0..1 or None), load, memory, swap
"""

import os
import re
import subprocess
import sys
import time

LINUX = sys.platform.startswith("linux")
MAC = sys.platform == "darwin"
TICK = os.sysconf("SC_CLK_TCK") if LINUX else 100   # macOS values are converted to 1/100 s
PAGE = os.sysconf("SC_PAGE_SIZE")


def read(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return None


def _claude_vars(pairs):
    env = {}
    for kv in pairs:
        if kv.startswith(b"CLAUDE_") and b"=" in kv:
            k, v = kv.decode(errors="replace").split("=", 1)
            env[k] = v
    return env


# ---------------------------------------------------------------- `ps` parsing (macOS backend, testable anywhere)

def _secs(t):
    """ps TIME: [[dd-]hh:]mm:ss.cc"""
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = t.split(":")
    secs = float(parts[-1])
    if len(parts) > 1:
        secs += 60 * int(parts[-2])
    if len(parts) > 2:
        secs += 3600 * int(parts[-3])
    return secs + days * 86400

def _parse_ps(text):
    out = {}
    for line in text.splitlines():
        f = line.split(None, 9)
        if len(f) < 10 or not f[0].isdigit():
            continue
        pid, ppid, rss, cpu, comm = int(f[0]), int(f[1]), int(f[2]) * 1024, _secs(f[3]), f[9]
        try:
            start = int(time.mktime(time.strptime(" ".join(f[4:9]), "%a %b %d %H:%M:%S %Y")))
        except ValueError:
            start = 0
        out[pid] = (os.path.basename(comm), ppid, int(cpu * TICK), 0, start, rss)
    return out


# ---------------------------------------------------------------- Linux

if LINUX:
    def proc_stat(pid):
        s = read(f"/proc/{pid}/stat")
        if not s:
            return None
        r = s.rfind(")")
        comm = s[s.find("(") + 1:r]
        f = s[r + 2:].split()
        # after comm: state(0) ppid(1) ... utime(11) stime(12) cutime(13) cstime(14) ... starttime(19) .. rss(21)
        return comm, int(f[1]), int(f[11]) + int(f[12]), int(f[13]) + int(f[14]), int(f[19]), int(f[21]) * PAGE

    def all_procs():
        out = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                st = proc_stat(int(d))
                if st:
                    out[int(d)] = st
        return out

    def proc_cgroup(pid):
        s = read(f"/proc/{pid}/cgroup")
        return s.strip().split("::", 1)[-1] if s else ""

    def proc_env(pid):
        s = read(f"/proc/{pid}/environ", "rb")
        return _claude_vars(s.split(b"\0")) if s else {}

    def proc_argv(pid):
        s = read(f"/proc/{pid}/cmdline", "rb")
        return [a.decode(errors="replace") for a in s.split(b"\0")[:-1]] if s else []

    def proc_cwd(pid):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None

    def children_of(pid):
        out = []
        for t in os.listdir(f"/proc/{pid}/task") if os.path.isdir(f"/proc/{pid}/task") else []:
            s = read(f"/proc/{pid}/task/{t}/children")
            if s:
                out += [int(x) for x in s.split()]
        return out

    def started_epoch(start):
        return time.time() - (float(read("/proc/uptime").split()[0]) - start / TICK)

    def _cpu_times():
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        return sum(v[:8]), v[3] + (v[4] if len(v) > 4 else 0)

    def machine(state=None):
        tot, idle = _cpu_times()
        busy = None
        if state:
            pt, pi = state
            busy = max(0.0, min(1.0, 1 - (idle - pi) / max(1, tot - pt)))
        mem = {}
        for line in (read("/proc/meminfo") or "").splitlines():
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
        return {"busy": busy, "load": [f"{x:.2f}" for x in os.getloadavg()],
                "mem_total": mem.get("MemTotal", 0), "mem_used": mem.get("MemTotal", 0) - mem.get("MemAvailable", 0),
                "swap_total": mem.get("SwapTotal", 0), "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
                }, (tot, idle)


# ---------------------------------------------------------------- macOS

else:
    import ctypes
    import ctypes.util

    _ENV_C = dict(os.environ, LC_ALL="C")
    _PS_FIELDS = "pid=,ppid=,rss=,time=,lstart=,comm="
    _table = {"t": 0.0, "procs": {}}

    def all_procs():
        r = subprocess.run(["ps", "-axo", _PS_FIELDS], capture_output=True, text=True, env=_ENV_C)
        procs = _parse_ps(r.stdout)
        _table.update(t=time.time(), procs=procs)
        return procs

    def proc_stat(pid):
        r = subprocess.run(["ps", "-o", _PS_FIELDS, "-p", str(pid)], capture_output=True, text=True, env=_ENV_C)
        return _parse_ps(r.stdout).get(pid)

    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _CTL_KERN, _KERN_ARGMAX, _KERN_PROCARGS2 = 1, 8, 49

    def _argmax():
        mib = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
        val, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        if _libc.sysctl(mib, 2, ctypes.byref(val), ctypes.byref(size), None, 0) != 0:
            return 1 << 18
        return val.value

    _ARGMAX = _argmax()

    def procargs(pid):
        """exact argv and environment of one of our own processes, via sysctl(KERN_PROCARGS2)"""
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        buf = ctypes.create_string_buffer(_ARGMAX)
        size = ctypes.c_size_t(_ARGMAX)
        if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return [], []
        data = buf.raw[:size.value]
        argc = int.from_bytes(data[:4], sys.byteorder)
        rest = data[4:]
        rest = rest[rest.find(b"\0"):].lstrip(b"\0")   # skip the exec path and its padding
        parts = rest.split(b"\0")
        argv = [a.decode(errors="replace") for a in parts[:argc]]
        env = []
        for kv in parts[argc:]:
            if not kv:
                break
            env.append(kv)
        return argv, env

    def proc_argv(pid):
        return procargs(pid)[0]

    def proc_env(pid):
        return _claude_vars(procargs(pid)[1])

    def proc_cgroup(pid):
        return ""

    def proc_cwd(pid):
        try:
            r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                               capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in r.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
        return None

    def children_of(pid):
        procs = _table["procs"] if time.time() - _table["t"] < 2 else all_procs()
        return [p for p, row in procs.items() if row[1] == pid]

    def started_epoch(start):
        return float(start)

    def _sysctl(name):
        try:
            return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True).stdout.strip()
        except OSError:
            return ""

    def machine(state=None):
        total = int(_sysctl("hw.memsize") or 0)
        used = 0
        try:
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            pages = {k.strip(): int(v.strip(" .")) for k, v in re.findall(r"^([^:]+):\s+(\d+)\.?$", vm, re.M)}
            used = page * (pages.get("Pages active", 0) + pages.get("Pages wired down", 0)
                           + pages.get("Pages occupied by compressor", 0))
        except (OSError, AttributeError, ValueError):
            pass
        sw = re.findall(r"(total|used) = ([\d.]+)M", _sysctl("vm.swapusage"))
        swap = {k: float(v) * 2**20 for k, v in sw}
        return {"busy": None, "load": [f"{x:.2f}" for x in os.getloadavg()], "mem_total": total, "mem_used": used,
                "swap_total": swap.get("total", 0), "swap_used": swap.get("used", 0)}, state


def descendants(pid):
    out, stack = [], [pid]
    while stack:
        p = stack.pop()
        out.append(p)
        stack += children_of(p)
    return out
