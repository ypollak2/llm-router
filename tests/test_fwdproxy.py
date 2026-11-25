"""Tests for llm_router.fwdproxy, its shell scripts and `llm-router fwdproxy`.
Ported from the live ~/.llm-router/fwd/test_fwdproxy.py (every assertion kept) plus the PROBE
test. Only ephemeral 127.0.0.1 ports and fake upstreams are used; never a real host or the
production ports (8787/8789/11434/11435)."""
import asyncio
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import http.server
import unittest
from unittest import mock

from llm_router import fwdproxy

HERE = os.path.dirname(os.path.abspath(fwdproxy.__file__))
MAKE_CA = os.path.join(HERE, "make-ca.sh")

SENTINEL = "SENTINEL-HEADER-BODY-9f3a7c"


def run_make_ca(home, *args, extra_env=None):
    env = dict(os.environ, HOME=home)
    env.pop("FWDPROXY_CA_DIR", None)
    env.update(extra_env or {})
    return subprocess.run(["/bin/sh", MAKE_CA, *args], env=env, capture_output=True, text=True)


class FakeUpstream:
    """Accepts connections, reads until the client half-closes or sends, replies PREFIX+data."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.received = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        try:
            c.settimeout(5)
            data = c.recv(65536)
            self.received.append(data)
            c.sendall(self.prefix + data)
            c.shutdown(socket.SHUT_WR)
            c.recv(1)
        except Exception:
            pass
        finally:
            c.close()

    def stop(self):
        self.sock.close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ProxyHarness:
    def __init__(self, ca_dir, log, relay_port, fallback_port, extra=None):
        env = {
            "FWDPROXY_PORT": "0",
            "FWDPROXY_RELAY": f"127.0.0.1:{relay_port}",
            "FWDPROXY_FALLBACK": f"127.0.0.1:{fallback_port}",
            "FWDPROXY_FALLBACK_TLS": "0",
            "FWDPROXY_CA_DIR": ca_dir,
            "FWDPROXY_LOG": log,
        }
        env.update(extra or {})
        self.cfg = fwdproxy.Config(env)
        self.ready = threading.Event()
        self.port = None
        self.loop = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        assert self.ready.wait(10), "proxy did not start"

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        def started(port, srv):
            self.port = port
            self.ready.set()

        try:
            self.loop.run_until_complete(fwdproxy.serve(self.cfg, started))
        except Exception:
            pass

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


def connect_head(port, target, extra_header=""):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{extra_header}\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        ch = s.recv(1)
        if not ch:
            break
        buf += ch
    return s, buf


def read_all(s):
    out = b""
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            out += d
    except (socket.timeout, ssl.SSLError, OSError):
        pass
    return out


class FwdProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = cls.tmp.name
        r = run_make_ca(cls.home)
        assert r.returncode == 0, r.stderr
        cls.ca_dir = os.path.join(cls.home, ".llm-router", "fwd-ca")
        cls.cafile = os.path.join(cls.ca_dir, "ca.crt")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.log = os.path.join(self.tmp.name, f"{self.id().split('.')[-1]}.log")
        self.relay = FakeUpstream(b"RELAY:")
        self.fallback = FakeUpstream(b"FALLBACK:")
        self.tunnel = FakeUpstream(b"TUNNEL:")
        self.addCleanup(self.relay.stop)
        self.addCleanup(self.fallback.stop)
        self.addCleanup(self.tunnel.stop)

    def start(self, relay_port=None, extra=None):
        env = {"FWDPROXY_ALLOW_LOOPBACK": "1"}  # fakes live on loopback; default is refuse
        env.update(extra or {})
        p = ProxyHarness(self.ca_dir, self.log, relay_port or self.relay.port, self.fallback.port, env)
        self.addCleanup(p.stop)
        return p

    def tls_client(self, sock):
        ctx = ssl.create_default_context(cafile=self.cafile)
        ctx.set_alpn_protocols(["h2", "http/1.1"])
        return ctx.wrap_socket(sock, server_hostname="api.anthropic.com")

    def logtext(self):
        for _ in range(50):
            if os.path.exists(self.log) and os.path.getsize(self.log):
                break
            threading.Event().wait(0.05)
        threading.Event().wait(0.2)
        with open(self.log) as f:
            return f.read()

    def test_a_intercept_pipes_to_relay(self):
        p = self.start()
        s, head = connect_head(p.port, "api.anthropic.com:443")
        self.assertIn(b"200", head)
        t = self.tls_client(s)
        self.assertEqual(t.selected_alpn_protocol(), "http/1.1")  # h2 offered, refused
        t.sendall(b"hello-relay")
        self.assertEqual(read_all(t), b"RELAY:hello-relay")
        t.close()
        self.assertEqual(self.relay.received, [b"hello-relay"])
        self.assertEqual(self.fallback.received, [])
        self.assertIn("INTERCEPT api.anthropic.com:443", self.logtext())

    def test_b_relay_down_falls_back(self):
        p = self.start(relay_port=closed_port())
        s, head = connect_head(p.port, "api.anthropic.com:443")
        self.assertIn(b"200", head)
        t = self.tls_client(s)
        t.sendall(b"hello-fb")
        self.assertEqual(read_all(t), b"FALLBACK:hello-fb")
        t.close()
        self.assertIn("FALLBACK api.anthropic.com:443", self.logtext())

    def test_c_other_host_blind_tunnel(self):
        p = self.start()
        target = f"127.0.0.1:{self.tunnel.port}"
        s, head = connect_head(p.port, target)
        self.assertIn(b"200", head)
        s.sendall(b"plain-bytes")  # no TLS: proves no termination
        self.assertEqual(read_all(s), b"TUNNEL:plain-bytes")
        s.close()
        self.assertEqual(self.tunnel.received, [b"plain-bytes"])
        self.assertEqual(self.relay.received, [])
        self.assertIn(f"TUNNEL {target}", self.logtext())

    def test_d_non_connect_405(self):
        p = self.start()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        s.sendall(b"GET http://example.invalid/ HTTP/1.1\r\nHost: example.invalid\r\n\r\n")
        resp = read_all(s)
        s.close()
        self.assertTrue(resp.startswith(b"HTTP/1.1 405"), resp[:40])
        self.assertEqual(self.relay.received + self.tunnel.received + self.fallback.received, [])

    def test_probe_empty_connection_logs_probe_not_error(self):
        # claude-rc's `nc -z` liveness probe: connect, send nothing, close.
        p = self.start()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        s.close()
        text = self.logtext()
        self.assertIn(" PROBE ", text)
        self.assertNotIn("NotConnect405", text)
        self.assertNotIn("ERR", text)
        # a real non-CONNECT request is still a 405 and still logged as an error
        s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(read_all(s).startswith(b"HTTP/1.1 405"))
        s.close()
        self.assertIn("NotConnect405", self.logtext())

    def test_e_log_has_no_headers_or_bodies(self):
        p = self.start()
        # intercepted: sentinel in a header and in the encrypted body
        s, _ = connect_head(p.port, "api.anthropic.com:443", f"X-Secret: {SENTINEL}\r\n")
        t = self.tls_client(s)
        t.sendall(f"POST /v1/messages HTTP/1.1\r\nAuthorization: {SENTINEL}\r\n\r\n{SENTINEL}".encode())
        read_all(t)
        t.close()
        # tunnelled
        s, _ = connect_head(p.port, f"127.0.0.1:{self.tunnel.port}", f"X-Secret: {SENTINEL}\r\n")
        s.sendall(SENTINEL.encode())
        read_all(s)
        s.close()
        # 405 with a sentinel in the request line path and header
        s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        s.sendall(f"POST /{SENTINEL} HTTP/1.1\r\nAuthorization: {SENTINEL}\r\n\r\n{SENTINEL}".encode())
        read_all(s)
        s.close()
        text = self.logtext()
        self.assertGreaterEqual(len(text.strip().splitlines()), 3)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("/v1/messages", text)

    def test_f_server_exception_does_not_kill_server(self):
        p = self.start()
        real = fwdproxy.FwdProxy.intercept
        calls = {"n": 0}

        async def boom(self_, cr, cw, st):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("injected")
            return await real(self_, cr, cw, st)

        with mock.patch.object(fwdproxy.FwdProxy, "intercept", boom):
            s, _ = connect_head(p.port, "api.anthropic.com:443")
            self.assertEqual(read_all(s), b"")  # connection dropped, no reply
            s.close()
            # garbage and a bad target must not hurt either
            g = socket.create_connection(("127.0.0.1", p.port), timeout=5)
            g.sendall(b"\x00\xff\xfe garbage\r\n\r\n")
            read_all(g)
            g.close()
            s, head = connect_head(p.port, "x:notaport")
            self.assertIn(b"400", head)
            s.close()
            # next real connection still served
            s, head = connect_head(p.port, "api.anthropic.com:443")
            t = self.tls_client(s)
            t.sendall(b"after")
            self.assertEqual(read_all(t), b"RELAY:after")
            t.close()
        self.assertEqual(calls["n"], 2)
        self.assertIn("RuntimeError", self.logtext())

    def test_g_unreachable_tunnel_target_502(self):
        p = self.start()
        s, head = connect_head(p.port, f"127.0.0.1:{closed_port()}")
        self.assertIn(b"502", head)
        s.close()

    def test_i_hung_relay_idle_timeout(self):
        hung = socket.socket()
        hung.bind(("127.0.0.1", 0))
        hung.listen(8)  # kernel accepts into backlog, never replies
        self.addCleanup(hung.close)
        p = self.start(relay_port=hung.getsockname()[1], extra={"FWDPROXY_IDLE_S": "1"})
        s, _ = connect_head(p.port, "api.anthropic.com:443")
        t = self.tls_client(s)
        t.settimeout(6)
        t.sendall(b"request-that-never-answers")
        t0 = time.monotonic()
        self.assertEqual(read_all(t), b"")
        el = time.monotonic() - t0
        self.assertLess(el, 4.5, "client connection must be closed by the idle timeout")
        self.assertGreaterEqual(el, 0.8)
        t.close()
        self.assertIn("IDLE-TIMEOUT api.anthropic.com:443", self.logtext())

    def test_j_loopback_tunnel_refused(self):
        p = self.start(extra={"FWDPROXY_ALLOW_LOOPBACK": "0"})
        for tgt in (f"127.0.0.1:{self.tunnel.port}", "localhost:11434", "[::1]:8787", "0.0.0.0:80",
                    "169.254.169.254:80", "10.0.0.1:443"):
            s, head = connect_head(p.port, tgt)
            self.assertIn(b"403", head, tgt)
            s.close()
        self.assertEqual(self.tunnel.received, [])
        text = self.logtext()
        self.assertIn("REFUSE 127.0.0.1:", text)
        # trailing-dot form of the intercept host is still intercepted, not tunnelled
        s, head = connect_head(p.port, "API.anthropic.com.:443")
        t = self.tls_client(s)
        t.sendall(b"dot")
        self.assertEqual(read_all(t), b"RELAY:dot")
        t.close()

    def test_k_untrusted_client_logs_client_tls_reason(self):
        p = self.start()
        s, head = connect_head(p.port, "api.anthropic.com:443")
        self.assertIn(b"200", head)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # empty trust store: does not trust our CA
        with self.assertRaises(ssl.SSLError):
            ctx.wrap_socket(s, server_hostname="api.anthropic.com")
        s.close()
        text = self.logtext()
        self.assertRegex(text, r"ERR api\.anthropic\.com:443 up=0 down=0 err=ClientTLS:\S+")
        self.assertNotIn("err=SSLError", text)

    def test_l_fallback_tls_failure_logs_upstream_tls_reason(self):
        # fallback is a plain-TCP fake but the proxy is told to speak TLS to it -> handshake fails
        p = self.start(relay_port=closed_port(), extra={"FWDPROXY_FALLBACK_TLS": "1"})
        s, _ = connect_head(p.port, "api.anthropic.com:443")
        t = self.tls_client(s)
        read_all(t)
        t.close()
        self.assertRegex(self.logtext(), r"ERR api\.anthropic\.com:443 up=0 down=0 err=UpstreamTLS:\S{1,50}")

    def test_m_stalled_peer_drain_is_bounded(self):
        async def go():
            stuck = socket.socket()
            stuck.bind(("127.0.0.1", 0))
            stuck.listen(1)  # accepts into backlog, never reads
            r0 = asyncio.StreamReader()
            r0.feed_data(b"x" * (64 * 1024 * 1024))
            r0.feed_eof()
            _, w = await asyncio.open_connection("127.0.0.1", stuck.getsockname()[1])
            counts = {"up": 0, "down": 0, "last": time.monotonic()}
            try:
                await asyncio.wait_for(fwdproxy.pipe(r0, w, counts, "up", 1.0), 10)
            finally:
                w.close()
                stuck.close()
        t0 = time.monotonic()
        with self.assertRaises(fwdproxy.IdleTimeout):
            asyncio.run(go())
        self.assertLess(time.monotonic() - t0, 6)

    def test_n_start_line_logged(self):
        log = os.path.join(self.tmp.name, "start.log")
        env = {"FWDPROXY_PORT": "0", "FWDPROXY_CA_DIR": self.ca_dir, "FWDPROXY_LOG": log,
               "FWDPROXY_IDLE_S": "42", "FWDPROXY_ALLOW_LOOPBACK": "1"}

        def fake_run(coro):
            coro.close()
        with mock.patch.dict(os.environ, env), mock.patch.object(fwdproxy.asyncio, "run", fake_run):
            fwdproxy.main()
        with open(log) as f:
            self.assertRegex(f.read(), r"START listen=127\.0\.0\.1:0 allow_loopback=1 idle_s=42 up=0 down=0 err=-")

    def test_h_refuses_non_loopback_bind(self):
        with self.assertRaises(SystemExit):
            fwdproxy.Config({"FWDPROXY_BIND": "0.0.0.0"})


class MakeCaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.dir = os.path.join(self.home, ".llm-router", "fwd-ca")
        self.kdir = os.path.join(self.home, ".llm-router", "fwd-ca-key")

    def mode(self, p):
        return stat.S_IMODE(os.stat(p).st_mode)

    def test_modes_constraints_and_verify(self):
        r = run_make_ca(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mode(self.dir), 0o700)
        self.assertEqual(self.mode(self.kdir), 0o700)
        names = sorted(os.listdir(self.dir))
        for must in ("ca.crt", "leaf.crt", "leaf.key"):
            self.assertIn(must, names)
        self.assertNotIn("ca.key", names)  # CA key is offline, not beside the leaf
        self.assertTrue(os.path.exists(f"{self.kdir}/ca.key"))
        for d in (self.dir, self.kdir):
            for n in os.listdir(d):
                self.assertEqual(self.mode(os.path.join(d, n)), 0o600, n)
        ca_txt = subprocess.run(["openssl", "x509", "-in", f"{self.dir}/ca.crt", "-noout", "-text"],
                                capture_output=True, text=True).stdout
        self.assertIn("Name Constraints: critical", ca_txt)
        self.assertIn("DNS:api.anthropic.com", ca_txt)
        self.assertIn("CA:TRUE, pathlen:0", ca_txt)
        leaf_txt = subprocess.run(["openssl", "x509", "-in", f"{self.dir}/leaf.crt", "-noout", "-text"],
                                  capture_output=True, text=True).stdout
        self.assertIn("DNS:api.anthropic.com", leaf_txt)
        self.assertIn("TLS Web Server Authentication", leaf_txt)
        v = subprocess.run(["openssl", "verify", "-CAfile", f"{self.dir}/ca.crt", f"{self.dir}/leaf.crt"],
                           capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        for f, mx in (("ca.crt", 825), ("leaf.crt", 397)):
            ok = subprocess.run(["openssl", "x509", "-in", f"{self.dir}/{f}", "-noout",
                                 "-checkend", str((mx + 1) * 86400)], capture_output=True)
            self.assertEqual(ok.returncode, 1, f"{f} valid beyond {mx} days")
        d = self.dir
        subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{self.home}/o.key",
                        "-subj", "/CN=example.com", "-addext", "subjectAltName=DNS:example.com",
                        "-out", f"{self.home}/o.csr"], capture_output=True, check=True)
        ext = os.path.join(self.home, "o.ext")
        with open(ext, "w") as f:
            f.write("basicConstraints=CA:FALSE\nsubjectAltName=DNS:example.com\nextendedKeyUsage=serverAuth\n")
        subprocess.run(["openssl", "x509", "-req", "-in", f"{self.home}/o.csr", "-CA", f"{d}/ca.crt",
                        "-CAkey", f"{self.kdir}/ca.key", "-CAcreateserial", "-CAserial", f"{self.home}/o.srl",
                        "-days", "2", "-extfile", ext, "-out", f"{self.home}/o.crt"],
                       capture_output=True, check=True)
        bad = subprocess.run(["openssl", "verify", "-CAfile", f"{d}/ca.crt", f"{self.home}/o.crt"],
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("permitted subtree violation", bad.stdout + bad.stderr)

    def test_refuses_overwrite_unless_force(self):
        self.assertEqual(run_make_ca(self.home).returncode, 0)
        with open(f"{self.dir}/ca.crt", "rb") as f:
            before = f.read()
        r = run_make_ca(self.home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing", r.stderr)
        with open(f"{self.dir}/ca.crt", "rb") as f:
            self.assertEqual(f.read(), before)
        r = run_make_ca(self.home, "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(f"{self.dir}/ca.crt", "rb") as f:
            self.assertNotEqual(f.read(), before)

    def test_renew_leaf_only(self):
        self.assertEqual(run_make_ca(self.home).returncode, 0)
        rd = lambda p: open(p, "rb").read()
        ca0, key0, leaf0 = rd(f"{self.dir}/ca.crt"), rd(f"{self.kdir}/ca.key"), rd(f"{self.dir}/leaf.crt")
        r = run_make_ca(self.home, "--renew-leaf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rd(f"{self.dir}/ca.crt"), ca0)
        self.assertEqual(rd(f"{self.kdir}/ca.key"), key0)
        self.assertNotEqual(rd(f"{self.dir}/leaf.crt"), leaf0)
        v = subprocess.run(["openssl", "verify", "-CAfile", f"{self.dir}/ca.crt", f"{self.dir}/leaf.crt"],
                           capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        for n in os.listdir(self.dir):
            self.assertEqual(self.mode(os.path.join(self.dir, n)), 0o600, n)
        # no CA -> renew refuses
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.assertEqual(run_make_ca(other.name, "--renew-leaf").returncode, 1)


class ShellScriptTests(unittest.TestCase):
    """claude-rc and rollback.sh under a temp HOME with fakes (no real ports, no real launchctl)."""
    RC = os.path.join(HERE, "claude-rc")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = self.tmp.name
        os.makedirs(f"{self.h}/bin")
        os.makedirs(f"{self.h}/.llm-router/fwd-ca")
        with open(f"{self.h}/bin/claude", "w") as f:
            f.write('#!/bin/sh\necho "ARGS=$* HP=${HTTPS_PROXY:-unset} BASE=${ANTHROPIC_BASE_URL:-unset} '
                    'NP=${NO_PROXY:-unset} CA=${NODE_EXTRA_CA_CERTS:-unset}"\nexit 7\n')
        os.chmod(f"{self.h}/bin/claude", 0o755)

    def rc(self, fwd_port, proxy_port, extra=None):
        env = {"PATH": os.environ["PATH"], "CLAUDE_REAL_BIN": f"{self.h}/bin/claude",
               "LLM_ROUTER_WRAPPER_HOME": self.h, "FWD_PORT": str(fwd_port),
               "LLM_ROUTER_PROXY_URL": f"http://127.0.0.1:{proxy_port}", "ANTHROPIC_BASE_URL": "http://x"}
        env.update(extra or {})
        return subprocess.run(["/bin/sh", self.RC, "a", "b"], env=env, capture_output=True, text=True)

    def test_rc_routed_and_unrouted(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
            def log_message(self, *a): pass
        web = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        up = FakeUpstream(b"")
        self.addCleanup(up.stop)
        wp = web.server_address[1]
        r = self.rc(up.port, wp, {"NO_PROXY": "corp.example"})
        self.assertEqual(r.returncode, 7)
        self.assertIn(f"HP=http://127.0.0.1:{up.port}", r.stdout)
        self.assertIn("BASE=unset", r.stdout)
        self.assertIn("NP=corp.example,127.0.0.1,localhost,::1", r.stdout)
        self.assertIn(f"CA={self.h}/.llm-router/fwd-ca/ca.crt", r.stdout)
        # user's own CA file -> bundle = theirs + ours, 0600
        with open(f"{self.h}/corp.pem", "w") as f:
            f.write("CORP\n")
        with open(f"{self.h}/.llm-router/fwd-ca/ca.crt", "w") as f:
            f.write("OURS\n")
        r = self.rc(up.port, wp, {"NODE_EXTRA_CA_CERTS": f"{self.h}/corp.pem"})
        b = f"{self.h}/.llm-router/fwd-ca/bundle.pem"
        self.assertIn(f"CA={b}", r.stdout)
        self.assertEqual(open(b).read(), "CORP\nOURS\n")
        self.assertEqual(stat.S_IMODE(os.stat(b).st_mode), 0o600)
        self.assertEqual(sorted(n for n in os.listdir(f"{self.h}/.llm-router/fwd-ca") if n.startswith("bundle")),
                         ["bundle.pem"])  # temp file renamed into place, none left behind
        # either side down -> unrouted, no proxy vars, base url unset
        dead = closed_port()
        for fp, pp in ((dead, wp), (up.port, dead)):
            r = self.rc(fp, pp, {"HTTPS_PROXY": "http://inherited:1"})
            self.assertEqual(r.returncode, 7)
            self.assertIn("HP=unset", r.stdout)
            self.assertIn("BASE=unset", r.stdout)
            self.assertIn("UNROUTED", r.stderr)
        self.assertIn("fallback-direct-unrouted", open(f"{self.h}/.llm-router/wrapper.log").read())

    # --- python bundle (SSL_CERT_FILE / REQUESTS_CA_BUNDLE) ---
    def _servers(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
            def log_message(self, *a): pass
        web = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        up = FakeUpstream(b"")
        self.addCleanup(up.stop)
        return up.port, web.server_address[1]

    def _ssl_claude(self):
        p = f"{self.h}/bin/claude-ssl"
        with open(p, "w") as f:
            f.write('#!/bin/sh\necho "SSL=${SSL_CERT_FILE-unset} REQ=${REQUESTS_CA_BUNDLE-unset}"\nexit 0\n')
        os.chmod(p, 0o755)
        return p

    def _rc_ssl(self, extra=None, routed=True):
        fp, pp = self._servers()
        if not routed:
            fp = closed_port()
        env = {"CLAUDE_REAL_BIN": self._ssl_claude(), "FWD_SYS_ROOTS": f"{self.h}/roots.pem"}
        env.update(extra or {})
        r = self.rc(fp, pp, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def _write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def test_rc_py_bundle_routed(self):
        d = f"{self.h}/.llm-router/fwd-ca"
        pb = f"{d}/py-bundle.pem"
        self._write(f"{self.h}/roots.pem", "ROOTS\n")
        self._write(f"{d}/ca.crt", "OURS\n")
        r = self._rc_ssl()
        self.assertEqual(r.stdout.strip(), f"SSL={pb} REQ={pb}")
        with open(pb) as f:
            self.assertEqual(f.read(), "ROOTS\nOURS\n")
        self.assertEqual(stat.S_IMODE(os.stat(pb).st_mode), 0o600)
        self.assertEqual(sorted(n for n in os.listdir(d) if n.startswith("py-bundle")), ["py-bundle.pem"])
        # caller's own SSL_CERT_FILE (other file) -> bundle = theirs + ours
        self._write(f"{self.h}/other.pem", "OTHER\n")
        r = self._rc_ssl({"SSL_CERT_FILE": f"{self.h}/other.pem"})
        self.assertEqual(r.stdout.strip(), f"SSL={pb} REQ={pb}")
        with open(pb) as f:
            self.assertEqual(f.read(), "OTHER\nOURS\n")
        # inherited SSL_CERT_FILE that is our own bundle is not re-wrapped
        r = self._rc_ssl({"SSL_CERT_FILE": pb})
        with open(pb) as f:
            self.assertEqual(f.read(), "ROOTS\nOURS\n")
        # failure (no readable base roots, no CA): vars unchanged, one log line
        os.remove(f"{d}/ca.crt")
        r = self._rc_ssl({"SSL_CERT_FILE": f"{self.h}/other.pem", "REQUESTS_CA_BUNDLE": "/keep/me"})
        self.assertEqual(r.stdout.strip(), f"SSL={self.h}/other.pem REQ=/keep/me")
        with open(f"{self.h}/.llm-router/wrapper.log") as f:
            self.assertEqual(f.read().count("py-bundle-failed"), 1)
        self.assertEqual(sorted(n for n in os.listdir(d) if n.startswith("py-bundle")), ["py-bundle.pem"])

    def test_rc_py_bundle_review_folds(self):
        d = f"{self.h}/.llm-router/fwd-ca"
        pb = f"{d}/py-bundle.pem"
        self._write(f"{self.h}/roots.pem", "ROOTS\n")
        self._write(f"{d}/ca.crt", "OURS\n")
        # caller's own REQUESTS_CA_BUNDLE (another file) is kept; SSL_CERT_FILE still ours
        r = self._rc_ssl({"REQUESTS_CA_BUNDLE": "/keep/me"})
        self.assertEqual(r.stdout.strip(), f"SSL={pb} REQ=/keep/me")
        # an EMPTY caller SSL_CERT_FILE is not used as the base (falls back to system roots)
        self._write(f"{self.h}/empty.pem", "")
        r = self._rc_ssl({"SSL_CERT_FILE": f"{self.h}/empty.pem"})
        self.assertEqual(r.stdout.strip(), f"SSL={pb} REQ={pb}")
        with open(pb) as f:
            self.assertEqual(f.read(), "ROOTS\nOURS\n")
        # an EMPTY system roots file never yields a CA-only bundle
        self._write(f"{self.h}/roots.pem", "")
        self._rc_ssl()
        with open(pb) as f:
            self.assertNotEqual(f.read(), "OURS\n")
        # SSL_CERT_DIR set -> one note line in the wrapper log
        self._write(f"{self.h}/roots.pem", "ROOTS\n")
        self._rc_ssl({"SSL_CERT_DIR": f"{self.h}/certs"})
        with open(f"{self.h}/.llm-router/wrapper.log") as f:
            self.assertEqual(f.read().count("SSL_CERT_DIR set"), 1)

    def test_rc_py_bundle_unrouted(self):
        pb = f"{self.h}/.llm-router/fwd-ca/py-bundle.pem"
        r = self._rc_ssl({"SSL_CERT_FILE": pb, "REQUESTS_CA_BUNDLE": pb}, routed=False)
        self.assertEqual(r.stdout.strip(), "SSL=unset REQ=unset")
        r = self._rc_ssl({"SSL_CERT_FILE": f"{self.h}/other.pem", "REQUESTS_CA_BUNDLE": "/keep/me"}, routed=False)
        self.assertEqual(r.stdout.strip(), f"SSL={self.h}/other.pem REQ=/keep/me")

    def test_rc_py_bundle_end_to_end_tls(self):
        t = os.path.join(self.h, "pki")
        os.makedirs(t)

        def ossl(*a):
            subprocess.run(["openssl", *a], cwd=t, capture_output=True, check=True)

        def mkca(name):
            self._write(f"{t}/{name}.cnf", "[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3\n[dn]\nCN=%s\n"
                        "[v3]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign\n"
                        "subjectKeyIdentifier=hash\n" % name)
            ossl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-days", "2",
                 "-config", f"{name}.cnf", "-out", f"{name}.crt")
        mkca("testca")
        mkca("fakeroot")
        self._write(f"{t}/leaf.cnf", "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=localhost\n"
                    "[v3]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                    "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
                    "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n")
        ossl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-config", "leaf.cnf", "-out", "leaf.csr")
        ossl("x509", "-req", "-in", "leaf.csr", "-CA", "testca.crt", "-CAkey", "testca.key", "-CAcreateserial",
             "-days", "2", "-extfile", "leaf.cnf", "-extensions", "v3", "-out", "leaf.crt")
        # claude-rc builds the bundle: fake roots (an unrelated CA) + the test CA as "ours"
        shutil.copy(f"{t}/fakeroot.crt", f"{self.h}/roots.pem")
        shutil.copy(f"{t}/testca.crt", f"{self.h}/.llm-router/fwd-ca/ca.crt")
        r = self._rc_ssl()
        pb = f"{self.h}/.llm-router/fwd-ca/py-bundle.pem"
        self.assertEqual(r.stdout.strip(), f"SSL={pb} REQ={pb}")
        # local TLS server on an ephemeral loopback port, leaf signed by the test CA
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(f"{t}/leaf.crt", f"{t}/leaf.key")
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]

        def serve():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                try:
                    c.settimeout(5)
                    tc = sctx.wrap_socket(c, server_side=True)
                    tc.recv(65536)
                    tc.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                    tc.close()
                except Exception:
                    c.close()
        threading.Thread(target=serve, daemon=True).start()
        code = ("import sys,urllib.request\n"
                f"print(urllib.request.urlopen('https://localhost:{port}/', timeout=5).read().decode())\n")

        def client(extra):
            env = {"PATH": os.environ["PATH"], "HOME": self.h}
            env.update(extra)
            return subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True)
        bad = client({})
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", bad.stderr)
        good = client({"SSL_CERT_FILE": pb})
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(good.stdout.strip(), "ok")
        # control: the fake roots alone (without our CA) must still fail
        ctl = client({"SSL_CERT_FILE": f"{self.h}/roots.pem"})
        self.assertNotEqual(ctl.returncode, 0)

    def test_rollback_label_and_failure_reporting(self):
        os.makedirs(f"{self.h}/Library/LaunchAgents")
        os.makedirs(f"{self.h}/.llm-router/bin")
        os.makedirs(f"{self.h}/.llm-router/fwd-ca-key")
        for p in ("Library/LaunchAgents/com.llm_router.fwdproxy.plist", ".llm-router/bin/claude-rc"):
            open(f"{self.h}/{p}", "w").close()
        os.makedirs(f"{self.h}/stub")
        calls = f"{self.h}/calls.txt"
        with open(f"{self.h}/stub/launchctl", "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> {calls}\n[ "$1" = bootout ] && exit ${{BOOTOUT_RC:-0}}\nexit 0\n')
        os.chmod(f"{self.h}/stub/launchctl", 0o755)
        env = {"PATH": f"{self.h}/stub:" + os.environ["PATH"], "LLM_ROUTER_WRAPPER_HOME": self.h, "BOOTOUT_RC": "5"}
        script = os.path.join(HERE, "rollback.sh")
        r = subprocess.run(["/bin/sh", script], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bootout failed", r.stdout)
        self.assertNotIn("unloaded", r.stdout)
        self.assertIn(f"bootout gui/{os.getuid()}/com.llm_router.fwdproxy", open(calls).read())
        names = os.listdir(f"{self.h}/.llm-router")
        self.assertFalse(any(n.startswith("fwd-ca-key.discard-") for n in names), names)
        self.assertIn("fwd-ca-key", names)  # key dir is left in place, never renamed
        self.assertTrue(any(n.startswith("fwd-ca.discard-") for n in names), names)
        self.assertFalse(os.path.exists(f"{self.h}/.llm-router/bin/claude-rc"))
        # the note line about CA key dir
        self.assertIn("note: CA key dir", r.stdout)
        self.assertIn(f"{self.h}/.llm-router/fwd-ca-key left in place (contains ca.key)", r.stdout)
        # success path prints "unloaded"
        env["BOOTOUT_RC"] = "0"
        open(f"{self.h}/Library/LaunchAgents/com.llm_router.fwdproxy.plist", "w").close()
        r = subprocess.run(["/bin/sh", script], env=env, capture_output=True, text=True)
        self.assertIn("unloaded", r.stdout)


class FwdProxyCommandTests(unittest.TestCase):
    """`llm-router fwdproxy install|uninstall|status` under a temp HOME with a stubbed launchctl."""

    def setUp(self):
        from llm_router.commands import fwdproxy as cmd
        self.cmd = cmd
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.calls = []

    def runner(self, args, env=None, capture_output=True, text=True):
        self.calls.append(list(args))
        if args[0] == "/bin/sh":  # rollback.sh runs for real, with launchctl stubbed on PATH
            stub = os.path.join(self.home, "stub")
            os.makedirs(stub, exist_ok=True)
            with open(os.path.join(stub, "launchctl"), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(os.path.join(stub, "launchctl"), 0o755)
            env = dict(env, PATH=stub + ":" + os.environ["PATH"])
            return subprocess.run(args, env=env, capture_output=True, text=True)
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_install_needs_ca_first(self):
        code, out = self.cmd.install(home=self.home, system="darwin", runner=self.runner)
        self.assertEqual(code, 1)
        self.assertIn("fwdproxy ca", out[0])
        self.assertEqual(self.calls, [])

    def test_install_status_uninstall_roundtrip(self):
        r = run_make_ca(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        code, out = self.cmd.install(home=self.home, system="darwin", runner=self.runner, python="/x/python")
        self.assertEqual(code, 0, out)
        plist = os.path.join(self.home, "Library/LaunchAgents/com.llm_router.fwdproxy.plist")
        text = open(plist).read()
        self.assertIn("<string>llm_router.fwdproxy</string>", text)
        self.assertIn("<string>/x/python</string>", text)
        self.assertTrue(os.access(os.path.join(self.home, ".llm-router/bin/claude-rc"), os.X_OK))
        self.assertTrue(any(c[:2] == ["launchctl", "bootstrap"] for c in self.calls), self.calls)
        cport = closed_port()
        with mock.patch.dict(os.environ, {"FWDPROXY_PORT": str(cport)}):
            st = self.cmd.status(home=self.home, system="darwin")
        self.assertTrue(all(ln.startswith("yes") for ln in st[:3]), st)
        self.assertTrue(st[3].startswith("no") and "listener" in st[3], st)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"FWDPROXY_PORT": str(srv.getsockname()[1])}):
            self.assertTrue(self.cmd.status(home=self.home, system="darwin")[3].startswith("yes"))
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
        self.assertEqual(mode(plist), 0o600)
        self.assertEqual(mode(os.path.join(self.home, ".llm-router/bin/claude-rc")), 0o700)
        code, out = self.cmd.uninstall(home=self.home, system="darwin", runner=self.runner)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(plist))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".llm-router/bin/claude-rc")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".llm-router/fwd-ca-key")))  # key kept
        self.assertTrue(any(n.startswith("fwd-ca.discard-") for n in os.listdir(f"{self.home}/.llm-router")))

    def test_install_failure_is_reported(self):
        run_make_ca(self.home)

        def failing(args, **kw):
            return subprocess.CompletedProcess(args, 5, "", "")
        code, out = self.cmd.install(home=self.home, system="darwin", runner=failing)
        self.assertEqual(code, 1)
        self.assertIn("service start failed", out[-1])

    def test_modes_are_independent_of_umask(self):
        run_make_ca(self.home)
        old = os.umask(0o000)
        try:
            self.cmd.install(home=self.home, system="linux", runner=self.runner, python="/x/python")
        finally:
            os.umask(old)
        unit = os.path.join(self.home, ".config/systemd/user/llm-router-fwdproxy.service")
        self.assertEqual(stat.S_IMODE(os.stat(unit).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.home, ".llm-router/bin/claude-rc")).st_mode), 0o700)

    def test_plist_with_xml_special_chars_is_valid(self):
        import plistlib
        from pathlib import Path
        home = Path("/tmp/a&b <c> \"d\"")
        text = self.cmd.render_service(home, "darwin", "/py&thon/<x>/bin/python")
        d = plistlib.loads(text.encode())
        self.assertEqual(d["ProgramArguments"][0], "/py&thon/<x>/bin/python")
        self.assertEqual(d["StandardOutPath"], f"{home}/.llm-router/logs/fwdproxy.out.log")

    def test_systemd_unit_escapes_space_and_percent(self):
        from pathlib import Path
        text = self.cmd.render_service(Path("/home/a b%u"), "linux", "/opt/my py/bin/python")
        self.assertIn("StandardOutput=append:/home/a b%%u/.llm-router/logs/fwdproxy.out.log", text)
        self.assertIn("ExecStart='/opt/my py/bin/python' -I -m", text)

    def test_linux_unit_is_rendered(self):
        text = self.cmd.render_service(__import__("pathlib").Path(self.home), "linux", "/x/python")
        self.assertIn("ExecStart=/x/python -I -m llm_router.fwdproxy", text)


class WheelTest(unittest.TestCase):
    """Verify the wheel contains the fwdproxy package and its scripts.
    Marked slow because it builds a wheel (offline, temp dir)."""

    @unittest.skip("slow: builds a wheel")
    def test_wheel_contains_fwdproxy(self):
        import zipfile
        import tempfile
        import subprocess
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run([sys.executable, "-m", "pip", "install", "uv"], check=True, capture_output=True)
            subprocess.run(["uv", "build", "--wheel", "--offline", "-o", tmpdir, "."],
                           check=True, cwd=repo, capture_output=True)
            whl = [f for f in os.listdir(tmpdir) if f.endswith(".whl")][0]
            names = {n for n in zipfile.ZipFile(os.path.join(tmpdir, whl)).namelist() if "fwdproxy" in n}
            expected = {
                "llm_router/commands/fwdproxy.py",
                "llm_router/fwdproxy/__init__.py",
                "llm_router/fwdproxy/__main__.py",
                "llm_router/fwdproxy/claude-rc",
                "llm_router/fwdproxy/make-ca.sh",
                "llm_router/fwdproxy/rollback.sh",
                "llm_router/fwdproxy/smoke.sh",
            }
            self.assertEqual(names, expected)
