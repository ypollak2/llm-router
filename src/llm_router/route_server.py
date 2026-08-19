"""HTTP ``/route`` endpoint — route a single prompt through LLM Router's real router
over HTTP, so external processes (e.g. LoopHole's ``llm_router:`` provider) can use
LLM Router's model selection without importing llm_router or speaking MCP.

Pure stdlib server (no FastAPI/uvicorn dependency) — the **zero-dependency
fallback**. The primary surface is ``llm_router.gateway`` (FastAPI on :17900), which
also exposes ``/route`` plus OpenAI/Anthropic/Ollama wire formats and shares this
module's :func:`route_payload` as its routing core, so both go through
``route_and_call`` identically. Use this server where FastAPI/uvicorn aren't
available. Launch it with the ``llm_router-route`` console script, or
``python -m llm_router.route_server``.

    POST /route
      {"prompt": "...",                         # required
       "complexity": "simple|moderate|complex", # optional -> routing profile
       "system": "...",                         # optional system prompt
       "task_type": "code|query|...",           # optional (default: code)
       "max_tokens": 4096, "temperature": 0.2}  # optional
      -> 200 {"text","model","provider","cost_usd",
              "input_tokens","output_tokens","complexity"}
      -> 400 on bad input · 502 if routing/provider fails

    GET /health -> {"ok": true}
"""

from __future__ import annotations

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from llm_router.savings import net_saved


async def route_payload_async(payload: dict) -> dict:
    """Run one routing call through LLM Router's FULL router and return a JSON-able
    result. This is the single routing core shared by both HTTP surfaces — this
    zero-dep server AND ``gateway.py`` — so every external caller goes through
    ``route_and_call`` and uniformly gets budget caps, caching, the paid-spend
    cap, and cost logging. Importing inside keeps module import cheap and lets
    tests monkeypatch ``llm_router.router.route_and_call``."""
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("missing 'prompt'")
    try:
        task_type = TaskType(payload.get("task_type", "code"))
    except ValueError:
        task_type = TaskType.CODE

    # Optional tier override (OpenAI ``model`` field / explicit model_override).
    # Gateway hosts should be able to request plain "auto"; all auto aliases mean
    # "let LLM Router pick".
    _override = payload.get("model_override") or payload.get("model")
    if _override in ("auto", "llm_router-auto", "", None):
        _override = None

    resp = await route_and_call(
        task_type, prompt,
        complexity_hint=payload.get("complexity") or None,
        system_prompt=payload.get("system") or None,
        model_override=_override,
        max_tokens=payload.get("max_tokens"),
        temperature=payload.get("temperature"),
    )

    # Surface this external route in the host-tagged savings pipeline so gateway /
    # LoopHole traffic shows up in the cross-surface indicators + savings_stats.
    # route_and_call already logged COST to usage.db, so we only add the
    # host-tagged savings record (never usage.db → no double count).
    _log_route_savings(resp, task_type.value,
                       payload.get("complexity") or resp.complexity or "moderate",
                       str(payload.get("host") or "gateway"))

    return {
        "text": resp.content,
        "model": resp.model,
        "provider": resp.provider,
        "cost_usd": resp.cost_usd,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "complexity": resp.complexity,
    }


def route_payload(payload: dict) -> dict:
    """Synchronous compatibility wrapper for stdlib server/tests/CLI callers."""
    return asyncio.run(route_payload_async(payload))


def _log_route_savings(resp, task_type: str, complexity: str, host: str) -> None:
    """Append a host-tagged record to ~/.llm-router/savings_log.jsonl for an external
    (gateway/route) call. Fire-and-forget.

    Deliberately does NOT touch session_spend.json (the CURRENT Claude Code
    session's ledger) the way the hook's log_direct_savings does — external
    traffic is not this session's spend. Cost is already in usage.db via
    route_and_call; this only adds the host-tagged savings record so the traffic
    is visible per-surface.
    """
    try:
        import json as _json
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from llm_router.hooks.savings_logger import (
            _baseline_cost,
            _cost_for,
            _savings_log_path,
        )

        provider = resp.provider or ""
        model = resp.model or ""
        bare = model.split("/", 1)[1] if "/" in model and model.split("/", 1)[0] == provider else model
        in_tok = max(0, int(resp.input_tokens or 0))
        out_tok = max(0, int(resp.output_tokens or 0))
        external = _cost_for(provider, bare, in_tok, out_tok)
        baseline = _baseline_cost(complexity, in_tok, out_tok)
        record = {
            "timestamp": _dt.now(_tz.utc).isoformat(),
            "session_id": host,
            "task_type": task_type,
            "complexity": complexity,
            "estimated_saved": net_saved(baseline, external),
            "external_cost": external,
            "model": model,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "host": host,
        }
        path = _savings_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(_json.dumps(record) + "\n")
    except Exception:
        pass


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_forbidden_cross_origin(headers) -> bool:
    """CHZ-SEC-04: block browser CSRF / DNS-rebinding on this loopback API.

    DNS-rebinding makes a browser resolve attacker.com -> 127.0.0.1 but still
    send ``Host: attacker.com``; requiring a loopback Host defeats it.
    Cross-site browser requests also carry ``Origin``/``Referer`` to another
    host. Legitimate CLI/SDK clients (curl, openai SDK) send a loopback Host and
    no browser Origin, so they are unaffected. ``headers`` is any mapping with a
    case-insensitive ``.get`` (http.client.HTTPMessage, dict, Starlette Headers).
    """
    import os
    from urllib.parse import urlparse

    # Operators who front the loopback server behind a reverse proxy / custom
    # hostname can opt that host in via LLM_ROUTER_ALLOWED_HOSTS (comma-separated).
    allowed = set(_LOCAL_HOSTS)
    extra = os.environ.get("LLM_ROUTER_ALLOWED_HOSTS", "")
    if extra:
        allowed |= {h.strip().lower() for h in extra.split(",") if h.strip()}

    host = (headers.get("Host") or headers.get("host") or "")
    host = host.rsplit(":", 1)[0].strip("[]").lower()
    if host and host not in allowed:
        return True
    for h in ("Origin", "Referer", "origin", "referer"):
        v = headers.get(h)
        if v:
            oh = (urlparse(v).hostname or "").lower()
            if oh and oh not in allowed:
                return True
    return False


def make_handler():
    class _Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _forbidden_cross_origin(self) -> bool:
            return is_forbidden_cross_origin(self.headers)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self._forbidden_cross_origin():
                return self._send(403, {"error": "forbidden: cross-origin request rejected"})
            if self.path not in ("/route", "/feedback"):
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": "bad json: {}".format(e)})
            if self.path == "/feedback":
                # Ground-truth routing quality from LoopHole verdicts. Best-effort:
                # an unusable record is acknowledged (recorded=false), never 5xx'd,
                # so the caller's fallback-to-JSONL is only for transport failures.
                try:
                    from llm_router.quality_feedback import record_loophole_verdict
                    recorded = record_loophole_verdict(payload)
                    return self._send(200, {"ok": True, "recorded": recorded})
                except Exception as e:
                    return self._send(500, {"error": "feedback failed: {}".format(e)})
            try:
                self._send(200, route_payload(payload))
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:                      # routing / provider failure
                self._send(502, {"error": "route failed: {}".format(e)})

        def log_message(self, *_a):                     # quiet by default
            pass

    return _Handler


def serve(host: str = "127.0.0.1", port: int = 7338) -> None:
    srv = ThreadingHTTPServer((host, port), make_handler())
    print("llm_router route endpoint -> http://{}:{}/route   (Ctrl-C to stop)".format(
        host, srv.server_address[1]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="llm_router-route",
        description="Serve LLM Router's router over HTTP for external callers (e.g. LoopHole).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7338)
    args = ap.parse_args(argv)
    # RED6-04: llm_router-route has no auth checks at all; a public bind exposes the
    # router -- and the paid calls it makes -- to the network.
    from llm_router.net_bind import refuse_public_bind_or_exit
    refuse_public_bind_or_exit(args.host, component="route")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
