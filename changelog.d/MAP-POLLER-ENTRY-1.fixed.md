The map re-discovery poller no longer starts on `import llm_router.server`; it starts in `server.main`, `main_sse`, and `main_sse_secured`. The proxy already starts it in `cmd_proxy`.
