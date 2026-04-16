---
name: Caveman Mode Default for LLM Router
description: User prefers terse caveman output as default for llm-router interactions
type: feedback
---

User expects caveman mode to be:
1. My default output style in llm-router project sessions
2. Injected as system directive when routing to external models via llm_* tools
3. Part of llm-router's default config (reduce verbosity across entire routing chain)

Rationale: Token efficiency is the core mission of llm-router. Caveman mode aligns output tokens with that mission. Every interaction should default to terse unless user asks for explanation.

Implementation: 
- Set caveman_mode: "full" in default config
- Inject caveman directive into system prompts for routed calls
- Claude's own responses in llm-router sessions use fragments, no filler
