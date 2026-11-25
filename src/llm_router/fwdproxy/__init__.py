"""fwdproxy: local CONNECT proxy so Claude Code can keep Remote Control while its
api.anthropic.com traffic still reaches the llm-router proxy.

  CONNECT api.anthropic.com:443 -> TLS-terminate with the leaf (ALPN http/1.1 only),
      byte-pipe the decrypted stream to plain 127.0.0.1:8787. If that does not connect
      within 2 s, FAIL OPEN: TLS to the real api.anthropic.com:443 and pipe there.
  every other CONNECT target -> blind byte tunnel to its own host:port (no TLS terminate).
  a connection that sends nothing (claude-rc's `nc -z` liveness probe) -> logged mode PROBE, no reply.
  anything else that is not CONNECT -> 405.

Logs one line per connection: UTC time, mode, host:port, byte counts, error class name.
It never logs headers, bodies or request lines (only the method token and host:port).
Stdlib only; run as:  python -I -m llm_router.fwdproxy
OPT-IN, beta: nothing starts it unless `llm-router fwdproxy install` was run (docs/fwdproxy.md).

Config (environment, all optional; tests use these):
  FWDPROXY_BIND            default 127.0.0.1 (loopback only is enforced)
  FWDPROXY_PORT            default 8789 (0 = ephemeral)
  FWDPROXY_RELAY           default 127.0.0.1:8787  (plain TCP)
  FWDPROXY_FALLBACK        default api.anthropic.com:443
  FWDPROXY_FALLBACK_TLS    default 1 (0 only for tests)
  FWDPROXY_INTERCEPT_HOST  default api.anthropic.com
  FWDPROXY_INTERCEPT_PORT  default 443
  FWDPROXY_CA_DIR          default ~/.llm-router/fwd-ca  (reads leaf.crt, leaf.key only)
  FWDPROXY_LOG             default ~/.llm-router/logs/fwdproxy.log
  FWDPROXY_IDLE_S          default 600: intercepted connection with no bytes either way this long
                           is closed and logged IDLE-TIMEOUT (a hung relay cannot hang a session)
  FWDPROXY_ALLOW_LOOPBACK  default 0: blind tunnels to loopback/private/link-local literals are
                           refused with 403 (REFUSE); 1 only for tests
"""
import asyncio
import ipaddress
import os
import re
import ssl
import sys
import time

CONNECT_TIMEOUT = 2.0
HEADER_TIMEOUT = 10.0
MAX_HEADER_LINES = 100
MAX_LINE = 8192


class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        home = os.path.expanduser("~")
        self.bind = e.get("FWDPROXY_BIND", "127.0.0.1")
        if self.bind not in ("127.0.0.1", "::1", "localhost"):
            raise SystemExit("fwdproxy: refusing non-loopback bind")
        self.port = int(e.get("FWDPROXY_PORT", "8789"))
        self.relay = self._hp(e.get("FWDPROXY_RELAY", "127.0.0.1:8787"))
        self.fallback = self._hp(e.get("FWDPROXY_FALLBACK", "api.anthropic.com:443"))
        self.fallback_tls = e.get("FWDPROXY_FALLBACK_TLS", "1") != "0"
        self.intercept_host = e.get("FWDPROXY_INTERCEPT_HOST", "api.anthropic.com").lower()
        self.intercept_port = int(e.get("FWDPROXY_INTERCEPT_PORT", "443"))
        self.ca_dir = e.get("FWDPROXY_CA_DIR", os.path.join(home, ".llm-router", "fwd-ca"))
        self.log = e.get("FWDPROXY_LOG", os.path.join(home, ".llm-router", "logs", "fwdproxy.log"))
        self.idle_s = float(e.get("FWDPROXY_IDLE_S", "600"))
        self.allow_loopback = e.get("FWDPROXY_ALLOW_LOOPBACK", "0") == "1"

    @staticmethod
    def _hp(s):
        h, _, p = s.rpartition(":")
        return (h, int(p))


class IdleTimeout(Exception):
    pass


def is_local_target(host):
    """True for localhost names and loopback/private/link-local/unspecified IP literals.
    The check is on the literal host only: a DNS name that resolves to a private IP (rebinding)
    is not blocked."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
            or ip.is_reserved or ip.is_multicast)


def tls_err(e, stage):
    """err= value naming the failed TLS stage and the ssl reason (no spaces, <= 60 chars)."""
    reason = getattr(e, "reason", None) or type(e).__name__
    return f"{stage}:{re.sub(r'[^A-Za-z0-9_.-]', '_', str(reason))}"[:60]


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Logger:
    def __init__(self, path):
        self.path = path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass

    def line(self, mode, target, up=0, down=0, err="-"):
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, f"{utc()} {mode} {target} up={up} down={down} err={err}\n".encode("ascii", "replace"))
            finally:
                os.close(fd)
        except Exception:
            pass


def close(w):
    try:
        w.close()
    except Exception:
        pass


async def drain_bounded(w, counts, idle):
    """w.drain() that raises IdleTimeout when no bytes moved in either direction for idle seconds
    (a peer that stopped reading must not leave the writer blocked forever).
    The clock (counts["last"]) is reset by reads in either direction, not by write progress:
    a peer that keeps sending but never reads is not torn down by this check."""
    t = getattr(w, "transport", None)
    if t is not None and t.get_write_buffer_size() == 0:
        await w.drain()  # empty buffer: protocol not paused, returns at once (no task per chunk)
        return
    d = asyncio.ensure_future(w.drain())
    try:
        while True:
            await asyncio.wait({d}, timeout=0.5)
            if d.done():
                d.result()
                return
            if time.monotonic() - counts["last"] > idle:
                raise IdleTimeout()
    finally:
        if not d.done():
            d.cancel()


async def pipe(r, w, counts, key, idle=None):
    """Copy r -> w. On EOF propagate half-close where the transport supports it.
    With idle set, raise IdleTimeout when no bytes moved in EITHER direction for idle seconds
    (counts["last"] is shared by both directions)."""
    try:
        while True:
            if idle is None:
                b = await r.read(65536)
            else:
                try:
                    b = await asyncio.wait_for(r.read(65536), 0.5)
                except asyncio.TimeoutError:
                    if time.monotonic() - counts["last"] > idle:
                        raise IdleTimeout()
                    continue
            if not b:
                break
            counts["last"] = time.monotonic()
            counts[key] += len(b)
            w.write(b)
            if idle is None:
                await w.drain()
            else:
                await drain_bounded(w, counts, idle)
        try:
            if w.can_write_eof():
                w.write_eof()
            else:
                w.close()
        except Exception:
            close(w)
    except asyncio.CancelledError:
        raise
    except Exception:
        close(w)
        raise


async def pipe_pair(cr, cw, ur, uw, counts, idle=None):
    """Run both directions; when either dies with an error, tear both down."""
    counts["last"] = time.monotonic()
    t1 = asyncio.ensure_future(pipe(cr, uw, counts, "up", idle))
    t2 = asyncio.ensure_future(pipe(ur, cw, counts, "down", idle))
    try:
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_EXCEPTION)
        # a clean EOF on one side is a half-close: the other side keeps draining; cancel only on error
        if any(t.exception() for t in done if not t.cancelled()):
            for t in (t1, t2):
                t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
        excs = [t.exception() for t in (t1, t2) if not t.cancelled() and t.exception()]
        for e in excs:
            if isinstance(e, IdleTimeout):
                raise e
        if excs:
            raise excs[0]
    finally:
        close(uw)
        close(cw)


def make_server_ctx(ca_dir):
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    c.minimum_version = ssl.TLSVersion.TLSv1_2
    c.load_cert_chain(os.path.join(ca_dir, "leaf.crt"), os.path.join(ca_dir, "leaf.key"))
    c.set_alpn_protocols(["http/1.1"])
    return c


def make_fallback_ctx():
    c = ssl.create_default_context()
    c.set_alpn_protocols(["http/1.1"])
    return c


async def read_head(r):
    """Read request line + headers, discarding the headers. Returns the request line bytes."""
    async def _go():
        first = await r.readline()
        if len(first) > MAX_LINE:
            raise ValueError("line too long")
        n = 0
        while True:
            ln = await r.readline()
            if ln in (b"\r\n", b"\n", b""):
                break
            n += 1
            if n > MAX_HEADER_LINES or len(ln) > MAX_LINE:
                raise ValueError("headers too large")
        return first
    return await asyncio.wait_for(_go(), HEADER_TIMEOUT)


class FwdProxy:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = Logger(cfg.log)
        self.server_ctx = make_server_ctx(cfg.ca_dir)
        self.fallback_ctx = make_fallback_ctx() if cfg.fallback_tls else None

    async def handle(self, cr, cw):
        target = "-"
        st = {"mode": "ERR", "up": 0, "down": 0}
        err = "-"
        try:
            try:
                first = await read_head(cr)
            except Exception as e:
                err = type(e).__name__
                return
            if first == b"":
                # client connected and closed without a byte: a liveness probe, not an error
                st["mode"] = "PROBE"
                return
            parts = first.decode("latin1").split()
            method = parts[0] if parts else ""
            if method != "CONNECT" or len(parts) < 2:
                target = method if re.fullmatch(r"[A-Z]{1,10}", method) else "?"
                err = "NotConnect405"
                cw.write(b"HTTP/1.1 405 Method Not Allowed\r\nAllow: CONNECT\r\n"
                         b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                await cw.drain()
                return
            host, _, port_s = parts[1].rpartition(":")
            host = host.strip("[]").lower().rstrip(".")
            if not host or not port_s.isdigit() or not (0 < int(port_s) < 65536):
                target = "CONNECT-bad-target"
                err = "BadTarget"
                cw.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await cw.drain()
                return
            port = int(port_s)
            target = f"{host}:{port}"
            if host != self.cfg.intercept_host and not self.cfg.allow_loopback and is_local_target(host):
                st["mode"] = "REFUSE"
                err = "LocalTarget403"
                cw.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await cw.drain()
                return
            if host == self.cfg.intercept_host and port == self.cfg.intercept_port:
                await self.intercept(cr, cw, st)
            else:
                try:
                    ur, uw = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
                except Exception as e:
                    err = type(e).__name__
                    cw.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await cw.drain()
                    return
                st["mode"] = "TUNNEL"
                cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await cw.drain()
                await pipe_pair(cr, cw, ur, uw, st)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = st.get("err") or type(e).__name__
        finally:
            self.log.line(st["mode"], target, st["up"], st["down"], err)
            close(cw)

    async def intercept(self, cr, cw, st):
        cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await cw.drain()
        try:
            await cw.start_tls(self.server_ctx, ssl_handshake_timeout=10)
        except Exception as e:
            st["err"] = tls_err(e, "ClientTLS")
            raise
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(*self.cfg.relay), CONNECT_TIMEOUT)
            st["mode"] = "INTERCEPT"
        except Exception:
            host, port = self.cfg.fallback
            try:
                ur, uw = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=self.fallback_ctx,
                                            server_hostname=host if self.fallback_ctx else None),
                    10)
            except ssl.SSLError as e:
                st["err"] = tls_err(e, "UpstreamTLS")
                raise
            st["mode"] = "FALLBACK"
        try:
            await pipe_pair(cr, cw, ur, uw, st, self.cfg.idle_s)
        except IdleTimeout:
            st["mode"] = "IDLE-TIMEOUT"
            raise


async def serve(cfg, started=None):
    fp = FwdProxy(cfg)
    srv = await asyncio.start_server(fp.handle, cfg.bind, cfg.port)
    port = srv.sockets[0].getsockname()[1]
    if started is not None:
        started(port, srv)
    async with srv:
        await srv.serve_forever()


def main():
    cfg = Config()
    try:
        fp_probe = make_server_ctx(cfg.ca_dir)  # fail loudly at start if the leaf is missing
        del fp_probe
    except Exception as e:
        print(f"fwdproxy: cannot load leaf from {cfg.ca_dir}: {type(e).__name__}", file=sys.stderr)
        sys.exit(2)
    Logger(cfg.log).line("START", f"listen={cfg.bind}:{cfg.port} allow_loopback={int(cfg.allow_loopback)} "
                                  f"idle_s={cfg.idle_s:g}")
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
