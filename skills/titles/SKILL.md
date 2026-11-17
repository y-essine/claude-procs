---
name: titles
description: Turn claude-procs task titles on or off. When on, every Claude Code session's title (terminal tab or window title, prompt box, /resume list) follows the task it is working on, e.g. "Webapp Checkout Tests", instead of the title from its first message.
argument-hint: "[on|off]"
allowed-tools: Bash(claude-procs titles:*)
---

Run `claude-procs titles $ARGUMENTS` (no argument: show the current state) and tell the user the result in one line.
When turning it on, add that each session's title updates on its next message once the task has a name, and that a
title set with /rename is never overridden.
