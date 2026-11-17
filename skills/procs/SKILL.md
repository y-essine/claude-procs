---
name: procs
description: Show which Claude Code sessions, tasks and commands are using CPU and memory on this machine, and which processes were not started by Claude. Use when the user asks what is slowing the machine down, what a node / rustc / python process is, which session started a process, or wants to clean up leftover dev servers.
allowed-tools: Bash(claude-procs:*)
---

Run `claude-procs top --once` (and `claude-procs who <pid>` for a specific process, `claude-procs report --since 24h --by task` for history).

Then answer briefly:
- the busiest sessions and the commands inside them, by name, with their CPU (100% = one core) and memory;
- anything not started by Claude that is heavy;
- leftover work: dev servers, watchers or test apps marked `[detached]` that no task seems to need anymore. Offer to stop them; never stop another session's agent.

If the command reports that nothing is tracked yet, tell the user to start a new session (the plugin's hook starts tracking) or run `claude-procs install` for the full setup. For the interactive view, the user runs `claude-procs` in a terminal.
