#!/usr/bin/env node
// claude-procs is pure-stdlib Python: run the bundled sources with the system python3.
"use strict";
const { spawnSync } = require("child_process");
const path = require("path");

const pkg = path.join(__dirname, "..", "python");
const env = { ...process.env, PYTHONPATH: pkg + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : "") };

for (const py of ["python3", "python"]) {
  const ok = spawnSync(py, ["-c", "import sys; sys.exit(sys.version_info < (3, 9))"], { stdio: "ignore" });
  if (ok.error || ok.status !== 0) continue;
  const r = spawnSync(py, ["-m", "claude_procs", ...process.argv.slice(2)], { stdio: "inherit", env });
  if (r.signal) process.kill(process.pid, r.signal);
  process.exit(r.status === null ? 1 : r.status);
}
console.error("claude-procs needs Python 3.9 or newer (python3). On macOS: xcode-select --install");
process.exit(1);
