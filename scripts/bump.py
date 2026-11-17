#!/usr/bin/env python3
"""Set (or --check) the version everywhere it lives: python package, plugin, npm, AUR."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "src/claude_procs/__init__.py": (r'__version__ = "([^"]+)"', '__version__ = "{v}"'),
    ".claude-plugin/plugin.json": None,
    "npm/package.json": None,
    "packaging/aur/PKGBUILD": (r"^pkgver=(\S+)", "pkgver={v}"),
}


def current():
    out = {}
    for f, rx in FILES.items():
        text = (ROOT / f).read_text()
        out[f] = json.loads(text)["version"] if rx is None else re.search(rx[0], text, re.M).group(1)
    return out


def main():
    check = "--check" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--check"]
    if not args:
        for f, v in current().items():
            print(f"{v:10} {f}")
        return
    v = args[0]
    if check:
        bad = {f: x for f, x in current().items() if x != v}
        if bad:
            sys.exit(f"version mismatch, expected {v}: {bad}")
        print(f"all at {v}")
        return
    for f, rx in FILES.items():
        p = ROOT / f
        text = p.read_text()
        if rx is None:
            data = json.loads(text)
            data["version"] = v
            p.write_text(json.dumps(data, indent=2) + "\n")
        else:
            p.write_text(re.sub(rx[0], rx[1].format(v=v), text, count=1, flags=re.M))
    pk = ROOT / "packaging/aur/PKGBUILD"
    pk.write_text(re.sub(r"^pkgrel=\S+", "pkgrel=1", pk.read_text(), flags=re.M))
    print(f"set {v}; then: git commit -am 'v{v}' && git tag v{v} && git push --follow-tags")


if __name__ == "__main__":
    main()
