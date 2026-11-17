"""Synthetic data for `claude-procs --demo`: screenshots, previews and tests, no real sessions shown."""

import math
import random
import time

G = 2**30
M = 2**20

SESSIONS = [
    # acct, pid, title, src, project, last prompt, [(grp, title, label, project, base%, rss, comms)]
    ("personal", 48211, "Desktop App Playback Fix", "task", "player", "the crossfade stutters when I hit next, fix it", [
        ("cmd", "Desktop App Release Build", "$ cargo build --release 2>&1 | tail -20", "player", 310, 1.6 * G,
         [("rustc", .78), ("cargo children", .2), ("cargo", .02)]),
        ("detached", "Desktop App Test Instance", "bash .dev/headless.sh start PLAYLIST=demo  [detached]", "player",
         42, 420 * M, [("player", .8), ("yt-dlp", .2)]),
        ("cmd", None, "$ until grep -q ready /tmp/player.log; do sleep 1; done", "player", 3, 6 * M, [("bash", 1)]),
        ("agent", "Claude agent", "claude (agent itself)", None, 9, 410 * M, [("claude", 1)]),
    ]),
    ("personal", 51734, "Dotfiles Status Bar Tweak", "renamed", "dotfiles", "make the clock bold and move it left", [
        ("agent", "Claude agent", "claude (agent itself)", None, 4, 380 * M, [("claude", 1)]),
    ]),
    ("work", 39002, "Webapp Checkout Tests", "task", "webapp", "run the whole checkout suite and fix what fails", [
        ("detached", "Webapp Frontend Checks", "bash fe-checks.sh  [detached]", "webapp", 260, 2.1 * G,
         [("node", .7), ("node children", .3)]),
        ("cmd", "Billing API Tests", "$ cd api && uv run pytest -q -x", "api", 95, 640 * M, [("python", 1)]),
        ("agent", "Claude agent", "claude (agent itself)", None, 14, 450 * M, [("claude", .8), ("claude children", .2)]),
    ]),
    ("work", 40115, "Docs Site Deploy", "task", "docs", "ship the new pricing page to staging", [
        ("cmd", "Docs Site Build", "$ pnpm --filter docs build", "docs", 70, 900 * M, [("node", 1)]),
        ("cmd", "API Dev Server", "$ pnpm dev --port 4000", "api", 6, 300 * M, [("node", 1)]),
        ("agent", "Claude agent", "claude (agent itself)", None, 5, 400 * M, [("claude", 1)]),
    ]),
    ("work", 44871, "PR Review Summary", "task", "webapp", "summarize the open PRs by author", [
        ("agent", "Claude agent", "claude (agent itself)", None, 3, 390 * M, [("claude", 1)]),
    ]),
]

OTHER = [("browser", 48, 3.2 * G, [("chrome", 1)]), ("compositor", 14, 700 * M, [("Hyprland", .6), ("bar", .4)]),
         ("editor", 9, 1.1 * G, [("code", 1)]), ("docker", 3, 300 * M, [("dockerd", 1)]),
         ("kernel threads", 4, 0, [("kswapd0", 1)])]

HISTORY = {
    "task": [["work", "Webapp Checkout Tests"], ["personal", "Desktop App Playback Fix"], ["work", "Docs Site Deploy"],
             ["personal", "Desktop App DJ Mode"], ["work", "Billing API Refactor"], ["personal", "Dotfiles Status Bar Tweak"],
             ["work", "PR Review Summary"], ["personal", "Photo Import Speedup"]],
    "session": [["work", "39002", "Webapp Checkout Tests"], ["personal", "48211", "Desktop App Playback Fix"],
                ["work", "40115", "Docs Site Deploy"], ["personal", "51734", "Dotfiles Status Bar Tweak"]],
    "project": [["work", "webapp"], ["personal", "player"], ["work", "api"], ["work", "docs"], ["personal", "dotfiles"]],
    "cmd": [["work", "webapp", "Webapp Frontend Checks"], ["personal", "player", "Desktop App Release Build"],
            ["work", "api", "Billing API Tests"], ["work", "docs", "Docs Site Build"],
            ["personal", "player", "Desktop App Test Instance"], ["work", "webapp", "Claude agent"]],
    "comm": [["personal", "rustc"], ["work", "node"], ["work", "python"], ["personal", "claude"], ["work", "claude"]],
    "account": [["work"], ["personal"]],
}


class DemoSource:
    demo = True

    def __init__(self, seed=7):
        self.rng = random.Random(seed)
        self.t0 = time.time()

    def _j(self, base, k):
        t = time.time() - self.t0
        return max(0.0, base * (0.75 + 0.25 * math.sin(t / 3 + k) + self.rng.uniform(-0.08, 0.08)))

    def snap(self):
        accounts = {}
        k = 0
        for acct, pid, title, src, proj, prompt, roots in SESSIONS:
            rs = []
            for grp, rtitle, label, rproj, base, rss, comms in roots:
                k += 1
                pct = self._j(base, k)
                rs.append({"grp": grp, "label": label, "proj": rproj, "pct": pct, "rss": rss, "title": rtitle,
                           "comms": [(c, pct * f) for c, f in comms if pct * f >= 1], "pids": [pid + 37 * k]})
            se = {"acct": acct, "cpid": pid, "title": title, "title_src": src, "alive": True, "proj": proj,
                  "session_id": f"{pid:08x}-demo", "cwd": f"~/code/{proj}", "pct": sum(r["pct"] for r in rs),
                  "rss": sum(r["rss"] for r in rs), "in_cgroup": True, "last_prompt": (time.time() - 60 * (k % 9 + 1), prompt),
                  "roots": sorted(rs, key=lambda r: -r["pct"])}
            se["cgroup_pct"] = se["pct"] * 1.03
            accounts.setdefault(acct, []).append(se)
        accs = [{"acct": a, "alias": {"personal": "claudep", "work": "claudew"}.get(a),
                 "pct": sum(s["pct"] for s in ss), "sessions": sorted(ss, key=lambda s: -s["pct"])}
                for a, ss in sorted(accounts.items(), key=lambda kv: kv[0] != "personal")]
        other = []
        for unit, base, rss, comms in OTHER:
            k += 1
            pct = self._j(base, k)
            other.append({"unit": unit, "pct": pct, "rss": rss, "comms": [(c, pct * f) for c, f in comms]})
        claude = sum(a["pct"] for a in accs)
        return {"t": time.time(), "dt": 2, "total_pct": claude + sum(o["pct"] for o in other), "claude_pct": claude,
                "accounts": accs, "other": other}

    def machine(self, snap):
        return {"ncpu": 12, "busy": min(1, snap["total_pct"] / 1200), "load": ["7.21", "6.13", "5.40"],
                "mem_total": 18.8 * G, "mem_used": 11.4 * G, "swap_total": 16 * G, "swap_used": 2.1 * G,
                "claude_rss": sum(se["rss"] for a in snap["accounts"] for se in a["sessions"])}

    def history(self, by, window):
        mult = {"1h": 1, "6h": 5, "24h": 14, "7d": 60, "30d": 200}[window]
        span = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}[window]
        rows = []
        for i, key in enumerate(HISTORY[by]):
            cpu = 1800 * mult / (i + 1) ** 0.9
            rows.append({"key": key, "cpu_s": cpu, "avg_pct": cpu / span * 100, "rss_mb": 2600 / (i + 1) ** 0.5,
                         "first": time.time() - span, "last": time.time() - 40 * i * mult})
        return span, rows

    def status(self):
        return [
            ("Requirements", [(True, "cgroup v2", "unified hierarchy"), (True, "systemd --user", "running"),
                              (True, "busctl", "/usr/bin/busctl"), (True, "claude CLI", "~/.local/bin/claude")]),
            ("Daemon", [(True, "claude-procs.service", "active  pid 1234  since Mon 09:12")]),
            ("Hooks", [(True, "~/.claude-personal", "account personal  SessionStart ✓  UserPromptSubmit ✓"),
                       (True, "~/.claude-work", "account work  SessionStart ✓  UserPromptSubmit ✓")]),
            ("Sessions", [(True, "5 running", "5 in their own cgroup"),
                          (None, "cgroup root", "/sys/fs/cgroup/user.slice/user-1000.slice/user@UID.service/claude.slice")]),
            ("Data", [(None, "task names", "on  model haiku  9 calls in the last hour (cap 60)  41 names"),
                      (None, "database", "~/.local/state/claude-procs/usage.db  3.2 MB  18211 rows"),
                      (None, "config", "~/.config/claude-procs/config.json"), (None, "version", "demo")]),
        ]

    def stop(self, pids):
        return f"demo: would send SIGTERM to {len(pids)} process tree(s)"
