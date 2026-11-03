"""M1 and S1 on the REAL ``codex`` binary, with a stub model on loopback (no model call, nothing leaves the machine).

The independent review of PR #818 reproduced this: ``--ignore-user-config`` does not stop Codex loading execpolicy
``.rules`` files, and a rule ``prefix_rule(pattern=["curl"], decision="allow")`` makes Codex run a matching command
OUTSIDE its sandbox, with network, in ``workspace-write`` and in ``read-only`` alike. The owner's own rules allow
``curl``, ``git push``, ``gh pr merge`` and ``gh run`` (docs/bugs/BIND-6.md). A delegated run also had ``/tmp`` and
``$TMPDIR`` as writable roots.

How it works. A thread serves the OpenAI Responses API on 127.0.0.1 and plays a scripted model: each turn it asks for
one shell command. A second thread is a canary listener on 127.0.0.1. Codex runs with a temp HOME and CODEX_HOME (a
rules file that allows ``curl``), proxies pointed at a dead port, and the argv that ``codex_exec_args`` really builds,
with only the model provider swapped for the stub. A command that leaves the sandbox shows as a canary hit or as a
file where the sandbox forbids one.

Every claim is paired with a control: the same scenario with the flag removed must show the escape, or the test is
skipped (this machine's Codex or sandbox cannot show it) rather than passed. A test that cannot fail proves nothing.
Skipped without a ``codex`` binary; run without an outer ``sandbox-exec`` (a nested sandbox cannot be applied).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from llm_router.codex_agent import codex_exec_args, make_run_tmpdir, remove_run_tmpdir
from tests._bind_support import init_repo

CODEX = os.environ.get("CODEX_PATH") or shutil.which("codex")


def _has_flag(flag: str) -> bool:
    if not CODEX:
        return False
    try:
        out = subprocess.run([CODEX, "exec", "--help"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in out


pytestmark = [
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX sandbox"),
    pytest.mark.skipif(not CODEX or not shutil.which("curl"), reason="needs the real codex binary and curl"),
    pytest.mark.skipif(not _has_flag("--ignore-rules"), reason="this codex has no --ignore-rules"),
    pytest.mark.timeout(300),
    pytest.mark.xdist_group("codex-live"),  # the scenarios are shared: run them in one worker
]


class _Hits(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def _stub_model(commands: list[str]) -> http.server.ThreadingHTTPServer:
    """A scripted 'model': the n-th turn (n = tool outputs already in the conversation) asks for commands[n]; after the
    last one it answers in text. It is not a model and takes nothing from the prompt."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(json.dumps({"object": "list", "data": [], "models": []}).encode(), "application/json")

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                req = {}
            if not self.path.rstrip("/").endswith("/responses"):
                return self._send(b'{"error":"unknown"}', "application/json", 404)
            inputs = req.get("input") or []
            done = sum(1 for i in inputs if isinstance(i, dict) and str(i.get("type", "")).endswith("_output"))
            tools = [t.get("name") or t.get("type") for t in (req.get("tools") or []) if isinstance(t, dict)]
            if done < len(commands):
                cmd = commands[done]
                if "shell_command" in tools:
                    name, args = "shell_command", {"command": cmd, "timeout_ms": 20000}
                elif "shell" in tools:
                    name, args = "shell", {"command": ["bash", "-lc", cmd], "timeout_ms": 20000}
                elif "exec_command" in tools:
                    name, args = "exec_command", {"cmd": cmd, "yield_time_ms": 15000}
                else:
                    name, args = (tools[0] if tools else "shell"), {"command": ["bash", "-lc", cmd]}
                item = {"type": "function_call", "id": f"fc_{done}", "call_id": f"call_{done}", "name": name,
                        "arguments": json.dumps(args), "status": "completed"}
            else:
                item = {"type": "message", "role": "assistant", "id": "msg_1", "status": "completed",
                        "content": [{"type": "output_text", "text": "scripted run finished"}]}
            rid = f"resp_{done + 1}"
            events = [
                {"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {
                    "id": rid, "status": "completed", "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}},
            ]
            body = b"".join(b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n"
                            for e in events)
            self._send(body, "text/event-stream")

    return http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)


class _Run:
    """One ``codex exec`` run and what it left behind."""

    def __init__(self, hits, slash_tmp_file, outside_tmp, repo, rc, out, err):
        self.hits, self.slash_tmp_file, self.outside_tmp, self.repo = hits, slash_tmp_file, outside_tmp, repo
        self.rc, self.out, self.err = rc, out, err

    @property
    def curl_escaped(self) -> bool:
        return bool(self.hits)

    @property
    def wrote_slash_tmp(self) -> bool:
        return self.slash_tmp_file.exists()

    @property
    def wrote_outside_tmpdir(self) -> bool:
        return (self.outside_tmp / "outside.txt").exists()

    @property
    def wrote_repo(self) -> bool:
        return (self.repo / "inside.txt").exists()

    @property
    def wrote_git_dir(self) -> bool:
        return (self.repo / ".git" / "probe.txt").exists()

    @property
    def curl_exit_code(self) -> int | None:
        """The exit code Codex's own event stream reports for the scripted ``curl`` (None when no such command ran):
        the proof that a run without a canary hit is one where the command really ran and was stopped, not one where it
        never started (a sandbox that cannot be applied, a failed stub)."""
        for line in self.out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") if isinstance(ev, dict) else None
            if (ev.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "command_execution"
                    and "curl" in str(item.get("command", ""))):
                code = item.get("exit_code")
                return code if isinstance(code, int) else None
        return None


def _without(args: list[str], *flags: str) -> list[str]:
    """``args`` minus each of ``flags``; a flag that is a ``-c`` value is removed together with its ``-c``."""
    out = list(args)
    for flag in flags:
        if flag not in out:  # already absent: a mutation of the argv builder must fail the test, not this helper
            continue
        i = out.index(flag)
        if i > 0 and out[i - 1] == "-c":
            del out[i - 1:i + 1]
        else:
            del out[i]
    return out


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    """Runs each scenario once, lazily, and keeps the result for every test that reads it."""
    cache: dict[str, _Run] = {}
    made: list[Path] = []

    def run(name: str, mode: str, *, drop: tuple[str, ...] = (), rules: bool = True, tmpdir_inside: bool = False) -> _Run:
        if name in cache:
            return cache[name]
        base = tmp_path_factory.mktemp(f"live-{name}")
        home, codex_home, outside_tmp = base / "home", base / "codexhome", base / "outside-tmp"
        for d in (home, codex_home, outside_tmp):
            d.mkdir()
        repo = init_repo(base / "repo")
        if rules:
            (codex_home / "rules").mkdir()
            (codex_home / "rules" / "default.rules").write_text('prefix_rule(pattern=["curl"], decision="allow")\n')
        canary = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hits)
        canary.hits = []  # type: ignore[attr-defined]
        threading.Thread(target=canary.serve_forever, daemon=True).start()
        slash_tmp_file = Path("/tmp") / f"bind-live-{uuid.uuid4().hex}"
        made.append(slash_tmp_file)
        run_tmp = make_run_tmpdir(str(repo)) if tmpdir_inside else None
        tmpdir_env = run_tmp or str(outside_tmp)
        commands = [
            f"curl -s -m 5 http://127.0.0.1:{canary.server_address[1]}/probe",
            f"echo A > {slash_tmp_file}",
            f"echo B > {outside_tmp}/outside.txt",
            f"echo C > {repo}/inside.txt",
            f"echo E > {repo}/.git/probe.txt",
        ]
        if run_tmp:
            commands.append(f"echo D > {run_tmp}/t.txt")
        model = _stub_model(commands)
        threading.Thread(target=model.serve_forever, daemon=True).start()
        argv = codex_exec_args(CODEX, model="stub-model", cwd=str(repo), prompt="run the scripted commands",
                               sandbox=mode)
        argv = _without(argv, *drop) if drop else argv
        i = argv.index("model_provider=openai")
        argv[i] = 'model_provider="stub"'
        provider = ('model_providers.stub={name="stub", base_url="http://127.0.0.1:%d/v1", wire_api="responses", '
                    'env_key="STUB_KEY"}' % model.server_address[1])
        argv[i + 1:i + 1] = ["-c", provider]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "CODEX_HOME": str(codex_home),
            "STUB_KEY": "x", "LANG": "en_US.UTF-8", "TMPDIR": tmpdir_env,
            # nothing may leave the machine: any non-loopback request dies at a dead proxy
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        }
        try:
            p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = 124, "", "TIMEOUT"
        finally:
            model.shutdown()
            canary.shutdown()
        result = _Run(list(canary.hits), slash_tmp_file, outside_tmp, repo, rc, out, err)  # type: ignore[attr-defined]
        result.run_tmp = run_tmp  # type: ignore[attr-defined]
        result.run_tmp_wrote = bool(run_tmp) and (Path(run_tmp) / "t.txt").exists()  # type: ignore[attr-defined]
        remove_run_tmpdir(run_tmp)
        cache[name] = result
        return result

    yield run
    for f in made:
        f.unlink(missing_ok=True)


# A rules-and-/tmp-open control run: both mitigations removed. Its escapes are what the other runs must NOT show.
_OPEN = ("--ignore-rules", "sandbox_workspace_write.exclude_slash_tmp=true",
         "sandbox_workspace_write.exclude_tmpdir_env_var=true")


def _premise(control: _Run) -> None:
    """The control must show that the scripted commands really ran inside Codex's sandbox and that the bypass is live."""
    if not control.wrote_repo:
        pytest.skip(f"the scripted commands did not run here (a sandbox cannot be applied?): rc={control.rc} "
                    f"err={control.err[-200:]!r}")


def test_a_rules_allowed_command_does_not_leave_the_workspace_write_sandbox(scenarios):
    control = scenarios("write-open", "workspace-write", drop=_OPEN)
    _premise(control)
    if not control.curl_escaped:
        pytest.skip("without --ignore-rules this Codex no longer runs a rule-allowed curl outside its sandbox")
    fixed = scenarios("write-fixed", "workspace-write")
    assert fixed.wrote_repo, "the scripted commands did not run in the fixed run"
    assert fixed.curl_exit_code not in (None, 0), f"the sandboxed curl did not run and fail: {fixed.curl_exit_code}"
    assert not fixed.curl_escaped, f"a curl allowed by a user rule reached the network: {fixed.hits}"


def test_a_rules_allowed_command_does_not_leave_the_read_only_sandbox(scenarios):
    control = scenarios("ro-open", "read-only", drop=("--ignore-rules",))
    if not control.curl_escaped:
        pytest.skip("without --ignore-rules this Codex no longer runs a rule-allowed curl outside its sandbox "
                    f"(or the scripted commands did not run here: rc={control.rc})")
    fixed = scenarios("ro-fixed", "read-only")
    if fixed.curl_exit_code is None:
        pytest.skip(f"the sandboxed curl did not run here (a sandbox cannot be applied?): rc={fixed.rc}")
    assert fixed.curl_exit_code != 0, "the curl succeeded inside the read-only sandbox"
    assert not fixed.curl_escaped, f"a curl allowed by a user rule reached the network: {fixed.hits}"
    assert not fixed.wrote_repo  # read-only really is read-only


def test_the_work_tree_is_the_only_writable_root(scenarios):
    control = scenarios("write-open", "workspace-write", drop=_OPEN)
    _premise(control)
    if not (control.wrote_slash_tmp and control.wrote_outside_tmpdir):
        pytest.skip("this Codex does not make /tmp and $TMPDIR writable by default, so there is nothing to exclude")
    fixed = scenarios("write-fixed", "workspace-write")
    assert fixed.wrote_repo
    assert not fixed.wrote_slash_tmp, "/tmp is still writable from a delegated run"
    assert not fixed.wrote_outside_tmpdir, "the $TMPDIR the child inherited is still writable"


def test_the_runs_own_temp_dir_inside_the_work_tree_is_writable(scenarios):
    _premise(scenarios("write-open", "workspace-write", drop=_OPEN))
    inside = scenarios("write-run-tmp", "workspace-write", tmpdir_inside=True)
    assert inside.run_tmp and inside.wrote_repo, "the scripted commands did not run in the run-tmp scenario"
    assert inside.run_tmp_wrote, "the run's own temp dir, inside the work tree, is not writable"  # type: ignore[attr-defined]
    assert not inside.wrote_slash_tmp  # and /tmp is still closed in the same run


def test_the_git_dir_inside_the_root_is_read_only_on_this_codex(scenarios):
    """Not a guarantee, a record that keeps a doc claim honest: on codex-cli 0.161.0's macOS seatbelt ``.git`` is
    read-only inside the writable root, so the commit, worktree and PR steps of a delegated builder prompt end in
    blocked reports even in a repository session (docs/bugs/BIND-6.md). Skipped when this Codex lets a run write it."""
    _premise(scenarios("write-open", "workspace-write", drop=_OPEN))  # skips when the scripted commands cannot run here
    fixed = scenarios("write-fixed", "workspace-write")
    assert fixed.wrote_repo, "the scripted commands did not run in the fixed run"
    if fixed.wrote_git_dir:
        pytest.skip("this Codex lets a delegated run write .git: the BIND-6 note that commits end blocked no longer holds")
    assert not fixed.wrote_git_dir
