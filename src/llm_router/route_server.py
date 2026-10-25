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
       "max_tokens": 4096, "temperature": 0.2,  # optional
       "messages": [{"role", "content"}, ...]}  # optional: turns before prompt
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


#: Model names that mean "let LLM Router pick", compared case-insensitively.
AUTO_MODEL_NAMES = frozenset({"auto", "llm_router-auto", "llm-router-auto"})


def is_auto_model(name: object) -> bool:
    """True when ``name`` is an "auto" sentinel, in any case, padded or not.

    P0.6 (R-AGT-1): the gateway's ``_AUTO_SENTINELS`` matched case-insensitively,
    but this module compared ``model`` to ``("auto", "llm_router-auto")`` exactly.
    "Auto", "AUTO" and "llm-router-auto" (the hyphenated product name) passed the
    gateway untouched and then reached ``route_and_call`` as a literal
    ``model_override``, which rejects a name with no ``/`` -> HTTP 400. One helper,
    used by both, so the two lists cannot drift again.
    """
    return isinstance(name, str) and name.strip().lower() in AUTO_MODEL_NAMES


# P2.4 ext-a / D-165 (4) / F33: a request whose NEWEST user turn carries an image is refused
# (the router has no image channel, so the part would be dropped silently); images only in
# history are flattened. Shared with gateway.py, which imports these.
IMAGES_UNSUPPORTED = (
    "This gateway routes to a text-completion backend and cannot read images. "
    "The request included image content, which would have been silently ignored, "
    "so it is refused instead. Remove the image parts to route this prompt as text."
)

_IMAGE_PART_TYPES = ("image", "image_url", "input_image", "image_file")
# Role-less content parts at the top of a Responses `input` list are one user turn.
_BARE_PART_TYPES = _IMAGE_PART_TYPES + ("text", "input_text", "input_file")


def _has_image(value) -> bool:
    """True when *value* (a content, a message, or a list of either) carries an image part."""
    if isinstance(value, list):
        return any(_has_image(v) for v in value)
    if isinstance(value, dict):
        if value.get("type") in _IMAGE_PART_TYPES or value.get("images"):
            return True
        return _has_image(value.get("content"))
    return False


def _newest_user_turn(value):
    """The newest user turn of a request field: the trailing run of role-less content
    parts (a bare Responses part list), else the last item whose role is ``user`` (a
    role-less typed item, such as a tool output, counts as the caller's). A dict (the
    Ollama generate ``images`` field) is the request itself; anything else has none."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        return None
    i = len(value)
    while i > 0 and isinstance(value[i - 1], dict) and "role" not in value[i - 1] \
            and value[i - 1].get("type") in _BARE_PART_TYPES:
        i -= 1
    if i < len(value):
        return value[i:]
    for item in reversed(value):
        if isinstance(item, dict) and item.get("role", "user") == "user":
            return item
    return None


def refuses_images(payload: dict) -> bool:
    """True when ``/route`` must refuse *payload*: its ``prompt`` or Ollama-style ``images``
    field carries image content in the newest turn."""
    return any(_has_image(_newest_user_turn(v))
               for v in ({"images": payload.get("images")}, payload.get("prompt")))


def _client_failure(what: str, exc: BaseException) -> None:
    """Log a failure to the local ``llm_router.gateway`` logger: the exception CLASS and the
    length of its text, never the text (F91). Exception text can hold the prompt or a
    provider's echo of it, and no scrubber can tell prompt text from other text. The client
    response carries only the class and a fixed message too."""
    import logging

    logging.getLogger("llm_router.gateway").warning(
        "%s: %s (detail withheld, %d chars)", what, type(exc).__name__, len(str(exc)))


def invalid_request_message(exc: ValueError) -> str:
    """The 400 body text for a ValueError (F91). A router-authored ``RouterInputError``
    echoes its own message; any other ValueError (pydantic, JSON, a library) is answered
    ``<Class>: invalid request`` and logged by class only."""
    from llm_router.types import RouterInputError

    if isinstance(exc, RouterInputError):
        return str(exc)
    _client_failure("invalid request", exc)
    return f"{type(exc).__name__}: invalid request"


async def route_payload_async(payload: dict) -> dict:
    """Run one routing call through LLM Router's FULL router and return a JSON-able
    result. This is the single routing core shared by both HTTP surfaces — this
    zero-dep server AND ``gateway.py`` — so every external caller goes through
    ``route_and_call`` and uniformly gets budget caps, caching, the paid-spend
    cap, and cost logging. Importing inside keeps module import cheap and lets
    tests monkeypatch ``llm_router.router.route_and_call``."""
    from llm_router.router import route_and_call
    from llm_router.types import RouterInputError, TaskType

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise RouterInputError("missing 'prompt'")
    try:
        task_type = TaskType(payload.get("task_type", "code"))
    except ValueError:
        task_type = TaskType.CODE

    # Optional tier override (OpenAI ``model`` field / explicit model_override).
    # Gateway hosts should be able to request plain "auto"; all auto aliases mean
    # "let LLM Router pick".
    _override = payload.get("model_override") or payload.get("model")
    if not _override or is_auto_model(_override):
        _override = None

    system = payload.get("system") or None
    # A.2-t: the caller's function-calling request, OpenAI form (the gateway normalises
    # the three wire formats before it gets here). An empty list is no request.
    tools = payload.get("tools") or None
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools)):
        raise RouterInputError("'tools' must be a list of tool objects")
    tool_choice = payload.get("tool_choice") if tools else None
    # PLAN v16 P1.1 door 7: the caller's conversation before ``prompt``, as
    # chat message dicts. Absent keeps the old path (no caller history).
    messages = payload.get("messages")
    if messages is not None and (
        not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages)
    ):
        raise RouterInputError("'messages' must be a list of message objects")
    # P0.6: the semantic cache keys on ``prompt`` and task type only. Once the
    # gateway forwards a caller's system prompt separately (instead of folding it
    # into ``prompt``), two callers with different system prompts and the same
    # question would share one cached answer. Bypass the cache for this call
    # until the key carries the system prompt (P0.5 owns the key).
    from llm_router import semantic_cache
    _sc_token = semantic_cache.CALLER_SYSTEM_PROMPT.set(True) if system else None
    # A.2-t: same bypass for a request with tools (see semantic_cache.REQUEST_HAS_TOOLS).
    _tools_token = semantic_cache.REQUEST_HAS_TOOLS.set(True) if tools else None
    from llm_router import call_identity
    try:
        with call_identity.door_scope("gateway"):  # P3.1-2: judge sampler door tag
            resp = await route_and_call(
                task_type, prompt,
                complexity_hint=payload.get("complexity") or None,
                system_prompt=system,
                model_override=_override,
                max_tokens=payload.get("max_tokens"),
                temperature=payload.get("temperature"),
                # Stage A: the caller's project, for OKF retrieval scope. Both HTTP
                # surfaces hand the router a plain dict, so this is the only place the
                # key can survive the trip. Absent → scope falls back to env, then cwd,
                # which is this process's launch directory and not the asker's.
                project_root=payload.get("project_root") or None,
                messages=messages,
                context_door="gateway",
                **({"tools": tools, "tool_choice": tool_choice} if tools else {}),
            )
    finally:
        if _tools_token is not None:
            semantic_cache.REQUEST_HAS_TOOLS.reset(_tools_token)
        if _sc_token is not None:
            semantic_cache.CALLER_SYSTEM_PROMPT.reset(_sc_token)

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
        # A.2-t: the backend's own stop reason and the tool calls it made (OpenAI shape).
        "finish_reason": getattr(resp, "finish_reason", "") or "",
        "tool_calls": getattr(resp, "tool_calls", None) or [],
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

        def _unauthorised(self) -> dict | None:
            """The 401 body when auth is configured and not satisfied, else None.

            S-03 (audit 2026-09-22). The gateway grew an opt-in bearer token;
            this server, which exposes the SAME router and the same paid calls,
            had no auth check at all — and the file's own comment at
            `main()` already acknowledged the gap. Same exposure, less
            mitigation.

            Shares the gateway's token rather than introducing a second one: an
            operator who sets `LLM_ROUTER_GATEWAY_TOKEN` is protecting "the
            router over HTTP", and making them configure that twice is how one
            of the two ends up unset.

            OPT-IN, like the gateway's: no token configured means no check, so
            an upgrade cannot silently start refusing an operator's own traffic.
            """
            import secrets as _secrets

            try:
                from llm_router.gateway import gateway_token
                expected = gateway_token()
            except Exception:  # noqa: BLE001 — never fail closed on an import error
                return None
            if not expected:
                return None
            supplied = (self.headers.get("Authorization") or "").strip()
            prefix = "bearer "
            if supplied[:len(prefix)].lower() != prefix:
                return {"error": "this server requires Authorization: Bearer <token>"}
            # Constant-time: a timing oracle on a loopback socket is cheap to
            # exploit from the same machine, which is the attacker this defends
            # against.
            if not _secrets.compare_digest(supplied[len(prefix):].strip(), expected):
                return {"error": "invalid token"}
            return None

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self._forbidden_cross_origin():
                return self._send(403, {"error": "forbidden: cross-origin request rejected"})
            unauth = self._unauthorised()
            if unauth is not None:
                return self._send(401, unauth)
            if self.path not in ("/route", "/feedback"):
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as e:
                _client_failure("bad json", e)
                return self._send(400, {"error": "{}: bad json".format(type(e).__name__)})
            if self.path == "/feedback":
                # Ground-truth routing quality from LoopHole verdicts. Best-effort:
                # an unusable record is acknowledged (recorded=false), never 5xx'd,
                # so the caller's fallback-to-JSONL is only for transport failures.
                try:
                    from llm_router.quality_feedback import record_loophole_verdict
                    recorded = record_loophole_verdict(payload)
                    return self._send(200, {"ok": True, "recorded": recorded})
                except Exception as e:
                    _client_failure("feedback failed", e)
                    return self._send(500, {"error": "{}: feedback failed".format(type(e).__name__)})
            # F33 (#558 review): refuse an image in ``prompt`` / ``images`` with 400, like the
            # gateway's /route. ``messages`` is history and is flattened, never refused.
            if isinstance(payload, dict) and refuses_images(payload):
                return self._send(400, {"error": IMAGES_UNSUPPORTED})
            try:
                self._send(200, route_payload(payload))
            except ValueError as e:
                self._send(400, {"error": invalid_request_message(e)})
            except Exception as e:                      # routing / provider failure
                _client_failure("route failed", e)
                self._send(502, {"error": "{}: routing failed".format(type(e).__name__)})

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
    # RED6-04: a public bind exposes the router -- and the paid calls it makes --
    # to the network. S-03 (2026-09-22) added the gateway's opt-in bearer token
    # to `do_POST`, so "no auth checks at all" is no longer true; the bind
    # refusal stays because an opt-in token that nobody opted into is not a
    # substitute for not listening on 0.0.0.0.
    from llm_router.net_bind import refuse_public_bind_or_exit
    refuse_public_bind_or_exit(args.host, component="route")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
