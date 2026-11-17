"""Interactive view, styled after Claude Code's /status and /usage dialogs.

Tabs: Live (sessions > commands, select / expand / stop), History, Machine, Status.
"""

import curses
import locale
import os
import signal
import time

from .core import ago, descendants, fmt_dur, fmt_mem, short

TABS = ["Live", "History", "Machine", "Status"]
WINDOWS = ["1h", "6h", "24h", "7d", "30d"]
GROUPS = ["task", "session", "project", "cmd", "comm", "account"]
EIGHTHS = " ▏▎▍▌▋▊▉"


# ---------------------------------------------------------------- data sources

class RealSource:
    demo = False

    def __init__(self):
        from .core import Live
        from .sysinfo import machine
        self.lv = Live()
        self.ncpu = os.cpu_count() or 1
        self._machine = machine
        _, self._mstate = machine()

    def snap(self):
        return self.lv.snap()

    def history(self, by, window):
        from .core import report_rows
        return report_rows(by, window, n=200, conn=self.lv.conn)

    def machine(self, snap):
        m, self._mstate = self._machine(self._mstate)
        if m["busy"] is None:  # macOS: estimate from the per-process samples
            m["busy"] = max(0.0, min(1.0, snap["total_pct"] / (self.ncpu * 100)))
        m["ncpu"] = self.ncpu
        m["claude_rss"] = sum(se["rss"] for a in snap["accounts"] for se in a["sessions"])
        return m

    def status(self):
        from .install import status
        return status()

    def stop(self, pids):
        """SIGTERM a command's whole process tree (never a claude agent itself)"""
        from .core import is_claude
        targets = set()
        for p in pids:
            targets.update(descendants(p))
        targets = [p for p in targets if not is_claude(p)]
        n = 0
        for p in sorted(targets, reverse=True):
            try:
                os.kill(p, signal.SIGTERM)
                n += 1
            except OSError:
                pass
        return f"sent SIGTERM to {n} process{'es' if n != 1 else ''}"


# ---------------------------------------------------------------- drawing helpers

class Painter:
    def __init__(self, scr):
        self.scr = scr
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        rich = curses.COLORS >= 256
        pal = {  # name: (256-color, 8-color fallback)
            "accent": (173, curses.COLOR_YELLOW), "dim": (245, curses.COLOR_WHITE), "faint": (239, curses.COLOR_BLACK),
            "good": (114, curses.COLOR_GREEN), "warn": (179, curses.COLOR_YELLOW), "bad": (167, curses.COLOR_RED),
            "text": (-1, -1), "blue": (110, curses.COLOR_BLUE),
        }
        self.c = {}
        self.selpair = {}  # normal pair number -> same color on the selection background
        sel_bg = 237 if rich else curses.COLOR_BLUE
        for i, (name, (c256, c8)) in enumerate(pal.items(), start=1):
            fg = c256 if rich else c8
            curses.init_pair(i, fg, bg)
            curses.init_pair(i + 20, fg, sel_bg)
            self.c[name] = curses.color_pair(i)
            self.selpair[i] = i + 20
        curses.init_pair(40, -1 if bg == -1 else curses.COLOR_WHITE, sel_bg)
        self.selpair[0] = 40
        self.sel = curses.color_pair(40)
        self.c["dim"] |= 0 if rich else curses.A_DIM
        self.c["faint"] |= 0 if rich else curses.A_DIM

    def put(self, y, x, text, attr=0, maxw=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return 0
        room = w - x - (1 if y == h - 1 else 0)
        if maxw is not None:
            room = min(room, maxw)
        if room <= 0:
            return 0
        text = text[:room]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass
        return len(text)

    def on_sel(self, attr):
        """the same attribute drawn on the selection background"""
        pair = curses.pair_number(attr & curses.A_COLOR)
        return (attr & ~curses.A_COLOR) | curses.color_pair(self.selpair.get(pair, 40))

    def segs(self, y, x, segments, maxw, fill_attr=None):
        """draw [(text, attr)] left to right, clipped to maxw; fill_attr = selected row"""
        if fill_attr is not None:
            self.put(y, x, " " * maxw, self.sel)
        used = 0
        for text, attr in segments:
            if used >= maxw:
                break
            used += self.put(y, x + used, text, self.on_sel(attr) if fill_attr is not None else attr, maxw - used)
        return used

    def bar(self, frac, width, color="accent", style="line"):
        """style "block": solid /usage-like bar (eighth-cell steps); "line": slim bar for lists"""
        frac = max(0.0, min(1.0, frac))
        if style == "block":
            n = int(round(frac * width * 8))
            full, part = divmod(n, 8)
            s = "█" * full + (EIGHTHS[part] if part and full < width else "")
        else:
            n = int(round(frac * width * 2))
            full, half = divmod(n, 2)
            s = "━" * full + ("╸" if half and full < width else "")
            if frac > 0 and not s:
                s = "╸"
        return [(s, self.c[color]), ("─" * (width - len(s)), self.c["faint"])]


def pct_str(p):
    return f"{p:5.0f}%"


# ---------------------------------------------------------------- app

class App:
    def __init__(self, scr, source, interval):
        self.scr, self.src, self.interval = scr, source, interval
        self.p = Painter(scr)
        self.tab = 0
        self.sel = {t: 0 for t in TABS}
        self.scroll = {t: 0 for t in TABS}
        self.sel_key = None
        self.collapsed = set()
        self.paused = False
        self.show_all = False
        self.msg, self.msg_t = "", 0
        self.window, self.group = 2, 0
        self.snap = None
        self.mach = None
        self.hist, self.hist_key, self.hist_t = None, None, 0
        self.stat, self.stat_t = None, 0
        self.confirm = None
        self.last = 0

    # -- data
    def refresh(self, force=False):
        now = time.time()
        if force or (not self.paused and now - self.last >= self.interval) or self.snap is None:
            self.snap = self.src.snap()
            self.mach = self.src.machine(self.snap)
            self.last = now
        if TABS[self.tab] == "History":
            key = (GROUPS[self.group], WINDOWS[self.window])
            if force or key != self.hist_key or now - self.hist_t > 30:
                self.hist, self.hist_key, self.hist_t = self.src.history(*key), key, now
        if TABS[self.tab] == "Status" and (force or now - self.stat_t > 10 or self.stat is None):
            self.stat, self.stat_t = self.src.status(), now

    def flash(self, msg):
        self.msg, self.msg_t = msg, time.time()

    # -- frame
    def draw(self):
        scr, p = self.scr, self.p
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 14 or w < 60:
            p.put(0, 0, "claude-procs: make the terminal at least 60×14", p.c["warn"])
            scr.refresh()
            return
        f = p.c["faint"]
        # rounded box
        title = " claude-procs "
        top = "╭─" + title + "─" * (w - 4 - len(title)) + "╮"
        p.put(0, 0, top, f)
        p.put(0, 2, title, p.c["accent"] | curses.A_BOLD)
        clock = f" {time.strftime('%H:%M:%S')}{' · paused' if self.paused else ''}{' · demo' if self.src.demo else ''} "
        p.put(0, w - 2 - len(clock), clock, p.c["dim"])
        for y in range(1, h - 2):
            p.put(y, 0, "│", f)
            p.put(y, w - 1, "│", f)
        p.put(h - 2, 0, "╰" + "─" * (w - 2) + "╯", f)
        # tabs
        x = 3
        for i, t in enumerate(TABS):
            lab = f" {t} "
            if i == self.tab:
                p.put(1, x, lab, p.c["accent"] | curses.A_BOLD)
                p.put(2, x, "─" * len(lab), p.c["accent"])
            else:
                p.put(1, x, lab, p.c["dim"])
            x += len(lab) + 2
        p.put(2, x, "", 0)
        # body
        body = (3, 3, h - 5, w - 6)  # y, x, height, width
        getattr(self, "draw_" + TABS[self.tab].lower())(*body)
        # footer
        hints = {
            "Live": "↑↓ move · ⏎ expand · x stop · a all · space pause",
            "History": "↑↓ move · t window · g group by",
            "Machine": "",
            "Status": "t terminal titles on/off",
        }[TABS[self.tab]]
        foot = f"←→ tabs · {hints + ' · ' if hints else ''}q quit"
        if self.confirm:
            p.put(h - 1, 2, f"{self.confirm[0]}  y/N", p.c["warn"] | curses.A_BOLD)
        elif self.msg and time.time() - self.msg_t < 4:
            p.put(h - 1, 2, self.msg, p.c["good"])
        else:
            p.put(h - 1, 2, foot, p.c["dim"])
        scr.refresh()

    # -- Live
    def live_items(self):
        """flatten the snapshot into rows: (key, kind, data, depth)"""
        items = []
        snap = self.snap
        for acc in snap["accounts"]:
            items.append((("a", acc["acct"]), "account", acc, 0))
            for se in acc["sessions"]:
                k = ("s", se["acct"], se["cpid"])
                items.append((k, "session", se, 1))
                if k not in self.collapsed:
                    for r in se["roots"]:
                        items.append((("r", se["acct"], se["cpid"], r["label"]), "root", (se, r), 2))
        items.append((("o",), "other-head", None, 0))
        for o in snap["other"][:12 if not self.show_all else 40]:
            if o["pct"] >= 1 or self.show_all:
                items.append((("u", o["unit"]), "other", o, 1))
        return items

    def draw_live(self, y0, x0, hh, ww):
        p, snap, m = self.p, self.snap, self.mach
        scale = max(100.0, m["ncpu"] * 100)
        # summary line
        segs = [("Claude-induced ", p.c["text"] | curses.A_BOLD)] + p.bar(snap["claude_pct"] / scale, 18, "accent", "block") + [
            (f" {snap['claude_pct']:.0f}%", p.c["accent"] | curses.A_BOLD),
            (f"   other {snap['total_pct'] - snap['claude_pct']:.0f}%   of {m['ncpu'] * 100}%"
             f" ({m['ncpu']} threads)   load {' '.join(m['load'])}", p.c["dim"])]
        p.segs(y0, x0, segs, ww)
        items = self.live_items()
        selectable = [i for i, it in enumerate(items) if it[1] in ("session", "root", "other")]
        # keep the selection on the same thing across refreshes
        if self.sel_key is not None:
            for i, it in enumerate(items):
                if it[0] == self.sel_key:
                    self.sel["Live"] = selectable.index(i) if i in selectable else 0
                    break
        if not selectable:
            p.put(y0 + 2, x0, "No processes yet.", p.c["dim"])
            return
        si = max(0, min(self.sel["Live"], len(selectable) - 1))
        self.sel["Live"] = si
        cur = selectable[si]
        self.sel_key = items[cur][0]
        detail_h = 4
        list_y, list_h = y0 + 2, hh - 2 - detail_h - 1
        # scroll
        top = self.scroll["Live"]
        if cur < top:
            top = cur
        if cur >= top + list_h:
            top = cur - list_h + 1
        top = max(0, min(top, max(0, len(items) - list_h)))
        self.scroll["Live"] = top
        peak = max([se["pct"] for a in snap["accounts"] for se in a["sessions"]] + [100.0])
        barw = 10
        right_w = barw + 1 + 6 + 1 + 6       # bar, cpu, rss
        meta_w = min(34, max(0, (ww - right_w) // 3))
        title_w = ww - right_w - meta_w - 2
        for row, idx in enumerate(range(top, min(len(items), top + list_h))):
            key, kind, data, depth = items[idx]
            yy = list_y + row
            selected = idx == cur
            fill = p.sel if selected else None
            if kind == "account":
                alias = f" · {data['alias']}" if data.get("alias") else ""
                p.segs(yy, x0, [(data["acct"].upper(), p.c["accent"] | curses.A_BOLD), (alias, p.c["dim"])], ww)
                s = f"{data['pct']:.0f}%"
                p.put(yy, x0 + ww - len(s), s, p.c["accent"] | curses.A_BOLD)
                continue
            if kind == "other-head":
                p.segs(yy, x0, [("NOT CLAUDE", p.c["dim"] | curses.A_BOLD)], ww)
                s = f"{snap['total_pct'] - snap['claude_pct']:.0f}%"
                p.put(yy, x0 + ww - len(s), s, p.c["dim"] | curses.A_BOLD)
                continue
            if kind == "session":
                se = data
                mark = "▸ " if key in self.collapsed else "▾ "
                tattr = (p.c["text"] | curses.A_BOLD) if se["alive"] else p.c["dim"]
                left = [("  " + mark, p.c["faint"]), (short(se["title"], title_w - 4), tattr)]
                meta = f"{se['proj']} · pid {se['cpid']}" + ("" if se["alive"] else " · ended")
                pct, rss, color = se["pct"], se["rss"], "accent"
            elif kind == "root":
                se, r = data
                name = r["title"] or r["label"]
                tattr = p.c["text"] if r["title"] else p.c["dim"]
                if r["grp"] == "agent":
                    tattr = p.c["dim"]
                left = [("      ", 0), (short(name, title_w - 6), tattr)]
                cs = ", ".join(f"{c} {x:.0f}%" for c, x in r["comms"][:2])
                meta = cs or (r["proj"] or "")
                pct, rss, color = r["pct"], r["rss"], "blue"
            else:  # other app
                o = data
                left = [("  ", 0), (short(o["unit"], title_w - 2), p.c["dim"])]
                meta = ", ".join(f"{c} {x:.0f}%" for c, x in o["comms"][:2])
                pct, rss, color = o["pct"], o["rss"], "dim"
            p.segs(yy, x0, left, title_w, fill)
            if meta_w:
                p.segs(yy, x0 + title_w, [(" " + short(meta, meta_w - 1), p.c["dim"])], meta_w + 2, fill)
            rx = x0 + ww - right_w
            p.segs(yy, rx, p.bar(pct / peak, barw, color) + [(pct_str(pct), p.c["text"]),
                                                            (" " + fmt_mem(rss), p.c["dim"])], right_w, fill)
        if len(items) > list_h:
            p.put(list_y + list_h - 1, x0 + ww - 1, "↓" if top + list_h < len(items) else " ", p.c["faint"])
        # detail panel
        dy = y0 + hh - detail_h
        p.put(dy - 1, x0, "─" * ww, p.c["faint"])
        self.draw_detail(dy, x0, detail_h, ww, items[cur])

    def draw_detail(self, y, x, hh, ww, item):
        p = self.p
        key, kind, data, _ = item
        lines = []
        if kind == "session":
            se = data
            src = {"renamed": "your /rename", "task": "named from the current task",
                   "ai-title": "Claude's session title", "project": "project"}.get(se["title_src"], "")
            lines.append([(se["title"], p.c["accent"] | curses.A_BOLD), (f"   {src}", p.c["dim"])])
            lines.append([(f"{se['acct']} · pid {se['cpid']} · session {(se['session_id'] or '?')[:8]} · "
                           f"{(se['cwd'] or '?').replace(os.path.expanduser('~'), '~')}", p.c["dim"])])
            if se["last_prompt"]:
                lines.append([("last prompt ", p.c["dim"]), (f"{ago(se['last_prompt'][0])} ago: ", p.c["dim"]),
                              (short(se["last_prompt"][1], ww - 30), p.c["text"])])
            cg = (f"cgroup total {se['cgroup_pct']:.0f}% (exact)" if se["cgroup_pct"] is not None
                  else "not in its own cgroup yet: claude-procs adopt" if se["alive"] and not se["in_cgroup"] else "")
            lines.append([(f"{len(se['roots'])} command groups", p.c["dim"]), (("   " + cg) if cg else "", p.c["dim"])])
        elif kind == "root":
            se, r = data
            lines.append([(r["title"] or "unnamed command", p.c["accent"] | curses.A_BOLD),
                          (f"   in {se['title']}", p.c["dim"])])
            lines.append([(short(r["label"], ww), p.c["text"])])
            lines.append([(f"project {r['proj'] or '?'} · pids {' '.join(map(str, r['pids'][:6])) or '?'}", p.c["dim"])])
            cs = ", ".join(f"{c} {x:.0f}%" for c, x in r["comms"]) or "idle"
            lines.append([(cs, p.c["dim"]), ("" if r["grp"] == "agent" else "   x to stop it", p.c["faint"])])
        elif kind == "other":
            o = data
            lines.append([(o["unit"], p.c["text"] | curses.A_BOLD), ("   not started by Claude", p.c["dim"])])
            lines.append([(", ".join(f"{c} {x:.0f}%" for c, x in o["comms"]) or "idle", p.c["dim"])])
        for i, segs in enumerate(lines[:hh]):
            p.segs(y + i, x, segs, ww)

    # -- History
    def draw_history(self, y0, x0, hh, ww):
        p = self.p
        segs = [("Window ", p.c["dim"])]
        for i, wdw in enumerate(WINDOWS):
            segs.append((f" {wdw} ", (p.c["accent"] | curses.A_BOLD | curses.A_REVERSE) if i == self.window else p.c["dim"]))
        segs.append(("    Group by ", p.c["dim"]))
        for i, g in enumerate(GROUPS):
            segs.append((f" {g} ", (p.c["accent"] | curses.A_BOLD | curses.A_REVERSE) if i == self.group else p.c["dim"]))
        p.segs(y0, x0, segs, ww)
        span, rows = self.hist or (0, [])
        if not rows:
            p.put(y0 + 2, x0, "No history yet: the daemon writes a row per minute (claude-procs status).", p.c["dim"])
            return
        hdr = f"{'':16} {'CPU time':>8} {'avg':>6} {'peak':>6}  {'last':>5}  "
        p.put(y0 + 2, x0, hdr, p.c["faint"])
        list_y, list_h = y0 + 3, hh - 3
        si = max(0, min(self.sel["History"], len(rows) - 1))
        self.sel["History"] = si
        top = self.scroll["History"]
        if si < top:
            top = si
        if si >= top + list_h:
            top = si - list_h + 1
        self.scroll["History"] = top
        peak = max(r["cpu_s"] for r in rows) or 1
        acct_w = max((len(r["key"][0]) for r in rows if len(r["key"]) > 1), default=0)
        for row, i in enumerate(range(top, min(len(rows), top + list_h))):
            r = rows[i]
            fill = p.sel if i == si else None
            key = r["key"]
            acct, rest = (key[0], key[1:]) if len(key) > 1 else ("", key)
            label = " · ".join(rest) if rest else acct
            segs = p.bar(r["cpu_s"] / peak, 16, "accent") + [
                (f" {fmt_dur(r['cpu_s']):>8} {r['avg_pct']:5.0f}% {fmt_mem(r['rss_mb'] * 2**20):>6}  "
                 f"{ago(r['last']):>5}  ", p.c["dim"]),
                ((acct.ljust(acct_w) + "  ") if rest else "", p.c["faint"]), (label, p.c["text"])]
            p.segs(list_y + row, x0, segs, ww, fill)
        win = WINDOWS[self.window]
        p.put(y0 + 2, x0 + len(hdr), f"{GROUPS[self.group]}  ({len(rows)} rows, over {win}; avg = mean CPU over the window)",
              p.c["faint"], ww - len(hdr))

    # -- Machine
    def draw_machine(self, y0, x0, hh, ww):
        p, m, snap = self.p, self.mach, self.snap
        gb = lambda b: f"{b / 2**30:.1f}"  # noqa: E731
        barw = max(12, min(44, ww - 18 - 52))
        y = y0
        scale = m["ncpu"] * 100

        def line(label, frac, right, color="accent", indent=2):
            nonlocal y
            p.segs(y, x0, [(" " * indent + label.ljust(16), p.c["text"])] + p.bar(frac, barw, color, "block") +
                   [("  " + right, p.c["dim"])], ww)
            y += 1

        p.put(y, x0, "CPU", p.c["text"] | curses.A_BOLD)
        y += 1
        line("whole machine", m["busy"], f"{m['busy'] * 100:.0f}% of {m['ncpu']} threads · load {' '.join(m['load'])}")
        line("Claude-induced", snap["claude_pct"] / scale, f"{snap['claude_pct']:.0f}% (one core = 100%)")
        line("everything else", (snap["total_pct"] - snap["claude_pct"]) / scale,
             f"{snap['total_pct'] - snap['claude_pct']:.0f}%", "dim")
        y += 1
        p.put(y, x0, "Memory", p.c["text"] | curses.A_BOLD)
        y += 1
        line("RAM", m["mem_used"] / max(1, m["mem_total"]), f"{gb(m['mem_used'])} / {gb(m['mem_total'])} GB")
        line("Claude-induced", m["claude_rss"] / max(1, m["mem_total"]),
             f"{gb(m['claude_rss'])} GB resident (shared pages count once per process)", "blue")
        if m["swap_total"]:
            frac = m["swap_used"] / m["swap_total"]
            line("swap", frac, f"{gb(m['swap_used'])} / {gb(m['swap_total'])} GB", "warn" if frac > 0.2 else "dim")
        y += 1
        p.put(y, x0, "Heaviest sessions", p.c["text"] | curses.A_BOLD)
        y += 1
        sess = sorted((se for a in snap["accounts"] for se in a["sessions"]), key=lambda s: -s["pct"])
        for se in sess[:max(0, min(5, (hh - (y - y0)) // 2))]:
            line(short(se["title"], 15), se["pct"] / scale, f"{se['pct']:.0f}%  {fmt_mem(se['rss']).strip()}  {se['acct']}")
        y += 1
        if y < y0 + hh - 1:
            p.put(y, x0, "Heaviest apps not started by Claude", p.c["text"] | curses.A_BOLD)
            y += 1
            for o in snap["other"]:
                if y >= y0 + hh:
                    break
                line(short(o["unit"], 15), o["pct"] / scale, f"{o['pct']:.0f}%  {fmt_mem(o['rss']).strip()}", "dim")

    # -- Status
    def draw_status(self, y0, x0, hh, ww):
        p = self.p
        y = y0
        for title, items in self.stat or []:
            if y >= y0 + hh:
                break
            p.put(y, x0, title, p.c["text"] | curses.A_BOLD)
            y += 1
            for ok, label, detail in items:
                if y >= y0 + hh:
                    break
                mark, col = ("✓", p.c["good"]) if ok else ("✗", p.c["bad"]) if ok is False else ("·", p.c["faint"])
                p.segs(y, x0, [("  " + mark + " ", col), (label.ljust(24), p.c["text"]), (detail, p.c["dim"])], ww)
                y += 1
            y += 1

    # -- input
    def key(self, ch):
        if self.confirm:
            if ch in (ord("y"), ord("Y")):
                self.flash(self.confirm[1]())
            else:
                self.flash("cancelled")
            self.confirm = None
            return True
        t = TABS[self.tab]
        if ch in (ord("q"), 27):
            return False
        if ch in (curses.KEY_RIGHT, ord("\t"), ord("l")):
            self.tab = (self.tab + 1) % len(TABS)
        elif ch in (curses.KEY_LEFT, curses.KEY_BTAB, ord("h")):
            self.tab = (self.tab - 1) % len(TABS)
        elif ord("1") <= ch <= ord(str(len(TABS))):
            self.tab = ch - ord("1")
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel[t] += 1
            self.sel_key = None if t == "Live" else self.sel_key
        elif ch in (curses.KEY_UP, ord("k")):
            self.sel[t] = max(0, self.sel[t] - 1)
            self.sel_key = None if t == "Live" else self.sel_key
        elif ch == curses.KEY_NPAGE:
            self.sel[t] += 10
            self.sel_key = None
        elif ch == curses.KEY_PPAGE:
            self.sel[t] = max(0, self.sel[t] - 10)
            self.sel_key = None
        elif ch == ord(" "):
            self.paused = not self.paused
            self.flash("paused" if self.paused else "live")
        elif ch == ord("r"):
            self.refresh(force=True)
        elif t == "Live":
            self.key_live(ch)
        elif t == "Status" and ch == ord("t") and not self.src.demo:
            from .core import CONFIG, set_config
            on = set_config("terminal_titles", not CONFIG.get("terminal_titles"))
            self.flash(f"terminal titles {'on: sessions take their task name on their next message' if on else 'off'}")
            self.refresh(force=True)
        elif t == "History":
            if ch == ord("t"):
                self.window = (self.window + 1) % len(WINDOWS)
                self.sel["History"] = 0
            elif ch == ord("T"):
                self.window = (self.window - 1) % len(WINDOWS)
                self.sel["History"] = 0
            elif ch in (ord("g"), ord("b")):
                self.group = (self.group + 1) % len(GROUPS)
                self.sel["History"] = 0
            elif ch in (ord("G"), ord("B")):
                self.group = (self.group - 1) % len(GROUPS)
                self.sel["History"] = 0
        return True

    def key_live(self, ch):
        items = self.live_items()
        k = self.sel_key
        it = next((x for x in items if x[0] == k), None)
        if ch in (10, 13, curses.KEY_ENTER):
            if it and it[1] == "session":
                self.collapsed.symmetric_difference_update({k})
            elif it and it[1] == "root":
                self.collapsed.add(("s", k[1], k[2]))
                self.sel_key = ("s", k[1], k[2])
        elif ch == ord("e"):
            sess = [x[0] for x in items if x[1] == "session"]
            self.collapsed = set() if self.collapsed else set(sess)
        elif ch == ord("a"):
            self.show_all = not self.show_all
            self.flash("showing everything" if self.show_all else "hiding idle rows")
        elif ch == ord("x"):
            if not it or it[1] != "root":
                self.flash("select a command row to stop it (sessions are stopped from their own terminal)")
            elif it[2][1]["grp"] == "agent":
                self.flash("that is the Claude agent itself: stop the session from its terminal")
            else:
                se, r = it[2]
                name = r["title"] or short(r["label"], 50)
                pids = r["pids"]
                self.confirm = (f"Stop “{name}” ({len(pids)} root process{'es' if len(pids) != 1 else ''} and children)?",
                                lambda: self.src.stop(pids))

    def run(self, tour=None):
        curses.curs_set(0)
        self.scr.keypad(True)
        self.scr.timeout(100 if tour else 200)
        self.refresh(force=True)
        t0 = time.time()
        while True:
            self.draw()
            ch = self.scr.getch()
            if tour and ch == -1 and tour[0][0] <= time.time() - t0:
                ch = tour.pop(0)[1]
                if ch is None:
                    return
            if ch != -1:
                if ch == curses.KEY_RESIZE:
                    continue
                if not self.key(ch):
                    return
                self.refresh(force=TABS[self.tab] in ("History", "Status") and ch in (
                    curses.KEY_LEFT, curses.KEY_RIGHT, ord("\t"), ord("t"), ord("T"), ord("g"), ord("G"),
                    ord("b"), ord("B"), ord("r")))
            else:
                self.refresh()


# scripted walk-through for recordings: (seconds, key)
TOUR = [(2.5, curses.KEY_DOWN), (3.3, curses.KEY_DOWN), (4.1, curses.KEY_UP), (5.0, ord("x")), (6.5, ord("n")),
        (7.3, curses.KEY_RIGHT), (8.8, ord("t")), (9.8, ord("g")), (11.3, curses.KEY_RIGHT),
        (13.3, curses.KEY_RIGHT), (15.5, curses.KEY_RIGHT), (17.0, None)]


def main(demo=False, interval=2.0, start_tab=None, tour=False):
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    if demo:
        from .demo import DemoSource
        source = DemoSource()
    else:
        source = RealSource()

    def _run(scr):
        app = App(scr, source, interval)
        if start_tab:
            app.tab = [t.lower() for t in TABS].index(start_tab.lower())
        app.run(list(TOUR) if tour else None)

    curses.wrapper(_run)
