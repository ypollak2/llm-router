---
description: Wire every detected host to this plugin's version (asks first)
---

The plugin's SessionStart check found that the hosts wired by llm-router are not on
this plugin's version. List what that changes (the other detected hosts: Codex, Gemini
CLI and so on; Claude Code's hooks and MCP entry already come from this plugin) and ask
me to confirm. Only after I say yes, run `npx -y llm-routing install --auto --via-plugin`
and report its `receipt:` line and any host that failed. Running it records my consent,
so later plugin updates are applied in the background.
