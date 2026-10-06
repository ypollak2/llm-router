// The command the mod runs to reach llm-router. `llm-router mod install`
// rewrites this file in the installed copy with the absolute path of the
// llm-router executable it finds on PATH, so the mod does not depend on Claude Code's PATH.
export const ROUTER_ARGV = ['llm-router']
