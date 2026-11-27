#!/usr/bin/env python3
"""Live checks for Factory v2 on private hc cores (specs/projects/factory/phase_plans/phase_1.md).

Never run by CI's default job. Every core here is started by this script in its own
temporary directory, with its own HC_SESSION, HC_SOCKET and XDG_RUNTIME_DIR. Every
connection checks hello's core id before it writes a request, and a core is stopped
only after server.status names that id, so the owner's sessions are never reached.

    python3 tests/live.py check lc1|lc10|lc13|lc8|rodeo_focus|all [--json PATH]
    python3 tests/live.py check lc2|lc3|lc4|lc6|lc7|lc12|login [--json PATH]   # real harnesses, the owner's login
    python3 tests/live.py fixtures [--out tests/hc_fixtures.json]
    python3 tests/live.py smoke
    python3 tests/live.py e2e --tracker file|github [--operator claude|codex] [--empty] [--rehearse]
"""

import argparse
import atexit
import hashlib
import json
import os
import pathlib
import random
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
import time
import uuid

HC = os.environ.get("HC_BIN", "hc")
RODEO = shutil.which(os.environ.get("RODEO_BIN") or "rodeo") or ""
RODEO = os.path.abspath(RODEO) if RODEO else ""
HC_MIN = (0, 1, 20)
CONTROL, DATA, CREDIT, CLOSE = 0, 0, 1, 2
MAX_SOCKET = 95
CORES = []


class HcError(Exception):
    """hc answered with an error object (hc_client.md)."""

    def __init__(self, error):
        data = error.get("data") or {}
        self.error = error
        self.kind = data.get("kind", "")
        self.message = error.get("message", "")
        self.hint = data.get("hint", "")
        super().__init__(f"{self.kind}: {self.message}")


class HcDown(Exception):
    """No usable answer from hc connect (hc_client.md)."""

    def __init__(self, sent, detail, timeout=False):
        self.sent = sent
        self.detail = detail
        self.timeout = timeout
        super().__init__(detail)


def rpc_line(method, params, ident):
    return json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}).encode() + b"\n"


def frame(channel, kind, payload=b""):
    return struct.pack("<IBI", channel, kind, len(payload)) + payload


class Conn:
    """One hc connect process. The hello reply is checked against the core before any request."""

    def __init__(self, env, core_id=None, framed=False):
        try:
            self.p = subprocess.Popen([HC, "connect"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env)
        except FileNotFoundError:
            raise HcDown(False, "hc not found")
        self.buf = b""
        self.sent = False
        try:
            self.write(rpc_line("hello", {"generation": 1, "framed": framed}, 0))
            reply = json.loads(self.line(10))
            if "error" in reply:
                raise HcError(reply["error"])
            self.hello = reply["result"]
            version = tuple(int(x) for x in self.hello["version"].split(".")[:3])
            if version < HC_MIN:
                raise HcDown(False, f"hc {'.'.join(map(str, HC_MIN))} or newer is required (found {self.hello['version']})")
            if core_id is not None and self.hello["core"]["id"] != core_id:
                raise HcDown(False, f"reached core {self.hello['core']['id']}, expected {core_id}: refusing")
            if self.hello.get("framed") is not framed:
                raise HcDown(False, f"asked for framed={framed}, hello says {self.hello.get('framed')}")
        except BaseException:
            self.kill()
            raise

    def write(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def _fill(self, deadline):
        fd = self.p.stdout.fileno()
        wait = None if deadline is None else max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([fd], [], [], wait)
        if not ready:
            raise HcDown(self.sent, "hc connect did not answer in time", timeout=True)
        chunk = os.read(fd, 65536)
        if not chunk:
            raise HcDown(self.sent, "hc connect closed its output")
        self.buf += chunk

    def line(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buf:
            self._fill(deadline)
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def exact(self, n, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self.buf) < n:
            self._fill(deadline)
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def request(self, method, params=None):
        self.write(rpc_line(method, params, 1))
        self.sent = True

    def message(self, timeout=15):
        return json.loads(self.line(timeout))

    def reply(self, timeout=15):
        msg = self.message(timeout)
        if "error" in msg:
            raise HcError(msg["error"])
        return msg["result"]

    def kill(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()
        self.p.stdin.close()
        self.p.stdout.close()

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(2)
        except subprocess.TimeoutExpired:
            self.kill()
        finally:
            self.p.stdout.close()


def qa_environ(environ):
    """The caller's environment without any hc, rodeo or herdr variable: a runner started from the owner's herdr
    pane must not hand its session and pane IDs to the QA terminals, where a harness that finds the owner's
    installed v1 skill could act in that session (@architect, #173)."""
    return {k: v for k, v in environ.items() if not k.startswith(("HC_", "RODEO_", "HERDR"))}


HERDR_STUB = "#!/bin/sh\necho 'herdr is not available in a Factory qualification run' >&2\nexit 1\n"
def installed_factory_skills(home, exclude=(), depth=4):
    """[(path, resolved folder)] of every Factory skill installed under home's dot-directories: a folder named
    factory in a skills folder, holding SKILL.md or scripts/factory.py, found by a scan at run start so a later
    install is caught (the owner's rule: no QA run may use an installed copy). Folders under exclude are skipped."""
    home, found, seen = pathlib.Path(home), [], set()
    excluded = [pathlib.Path(e).resolve() for e in exclude]
    try:
        tops = [t for t in home.iterdir() if t.name.startswith(".") and t.is_dir() and t.name not in (".cache", ".npm")]
    except OSError:
        return found
    walked = set()
    for top in tops:                       # a symlinked dot-directory is walked too, once per target
        if top.resolve() in walked:
            continue
        walked.add(top.resolve())
        for dirpath, dirnames, _ in os.walk(top, onerror=lambda error: None):
            here = pathlib.Path(dirpath)
            if here.name == "skills" and "factory" in dirnames:
                path = here / "factory"
                resolved = path.resolve()
                if ((resolved / "SKILL.md").is_file() or (resolved / "scripts" / "factory.py").is_file()) \
                        and not any(resolved == e or e in resolved.parents for e in excluded) and path not in seen:
                    seen.add(path)
                    found.append((str(path), str(resolved)))
            if len(here.relative_to(top).parts) >= depth - 1:
                dirnames[:] = []
    return found


SKILLS_SWITCH = ("claude", "codex", "pi", "omp")   # harnesses whose user skill discovery a launch switch turns off


def skills_off(harness, installed, exempt=()):
    """QA launch arguments that turn off a harness's user skill discovery, where it has a switch (the owner's
    decision; each read from its own --help, Codex's checked offline with codex debug prompt-input on 0.172.0).
    [] for OpenCode and agy, which have none: their installed copy may run, harmless with no HERDR_* and the herdr
    stub, and is recorded, not failed. exempt: harnesses a case needs with their commands on (case 6's /clear)."""
    if harness in exempt:
        return []
    if harness in ("pi", "omp"):
        return ["--no-skills"]
    if harness == "claude":
        return ["--disable-slash-commands"]               # "Disable all skills and commands"
    if harness == "codex":
        paths = sorted({f"{folder}/SKILL.md" for path, resolved in installed for folder in (path, resolved)})
        if paths:                                         # only the factory skill, by every path it is found at
            return ["-c", "skills.config=[" + ",".join("{path=" + json.dumps(p) + ",enabled=false}"
                                                      for p in paths) + "]"]
    return []


def installed_needles(installed):
    """The whitespace-free forms of each installed copy's scripts/factory.py: its path, its target, with ~."""
    home, needles = os.path.expanduser("~"), set()
    for path, resolved in installed:
        for folder in (path, resolved):
            script = f"{folder}/scripts/factory.py"
            needles.add(script)
            if script.startswith(home + "/"):
                needles.add("~" + script[len(home):])
    return sorted("".join(n.split()) for n in needles)


def tool_calls(text):
    """[(call text, its result text or None)] for every tool call in a Claude Code or Codex transcript (JSONL),
    paired by the call's own ID: Claude Code's tool_use / tool_result, Codex's function_call or custom_tool_call /
    its _output."""
    calls, results = {}, {}

    def walk(node):
        if isinstance(node, dict):
            kind = node.get("type")
            if kind == "tool_use" and node.get("id"):
                calls[node["id"]] = json.dumps(node.get("input"))
            elif kind in ("function_call", "custom_tool_call", "local_shell_call") and node.get("call_id"):
                calls[node["call_id"]] = json.dumps(node.get("arguments") or node.get("input") or node.get("action"))
            elif kind == "tool_result" and node.get("tool_use_id"):
                results[node["tool_use_id"]] = json.dumps(node.get("content"))
            elif kind in ("function_call_output", "custom_tool_call_output") and node.get("call_id"):
                results[node["call_id"]] = json.dumps(node.get("output"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for line in text.splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return [(json.loads(call) if isinstance(json.loads(call), str) else call, results.get(ident))
            for ident, call in calls.items()]


def qa_transcripts(roots, labelled=False):
    """Claude Code and Codex session files of conversations whose working directory is under one of roots, read
    from their normal places (never anything else there); with labelled, [(harness, path)]."""
    roots = [str(pathlib.Path(r)) for r in roots]
    if not roots:
        return []
    found = []
    projects = pathlib.Path(os.path.expanduser("~/.claude/projects"))
    prefixes = [re.sub(r"[^A-Za-z0-9]", "-", r) for r in roots]
    try:
        found += [("claude", f) for d in projects.iterdir() if any(d.name.startswith(p) for p in prefixes)
                  for f in d.glob("*.jsonl")]
    except OSError:
        pass
    sessions = pathlib.Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")) / "sessions"
    for day in {time.strftime("%Y/%m/%d", time.localtime(time.time() - s)) for s in (0, 86400)}:
        for f in sorted((sessions / day).glob("rollout-*.jsonl")) if (sessions / day).is_dir() else []:
            try:
                with f.open() as stream:
                    cwd = (json.loads(stream.readline()).get("payload") or {}).get("cwd") or ""
            except (OSError, ValueError):
                continue
            if any(cwd == r or cwd.startswith(r + "/") for r in roots):
                found.append(("codex", f))
    return found if labelled else [f for _, f in found]


class Core:
    """A private hc core in its own temporary directory."""

    def __init__(self, tag, config="", extra_env=None, protect=False):
        """protect: a core for real harnesses under the owner's rule that no QA run may use an installed Factory
        skill (the owner's simpler approach): a herdr stub that always fails is first on PATH, the installed
        copies are scanned for, and installed_seen() finds any trace of one afterwards."""
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix=f"hc-{tag}-", dir=tempfile.gettempdir()))
        d = self.dir
        for sub in ("run", "state", "config/herdr-core", "home"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(d / "run", 0o700)
        if config:
            (d / "config/herdr-core/config.toml").write_text(config)
        self.bin, self.installed = None, []
        if protect:
            self.bin = d / "bin"
            self.bin.mkdir()
            stub = self.bin / "herdr"
            stub.write_text(HERDR_STUB)
            stub.chmod(0o755)
            self.installed = installed_factory_skills(os.path.expanduser("~"), exclude=(ROOT, d))
        env = qa_environ(os.environ)
        if self.bin is not None:
            env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        env.update(HC_SESSION=f"factory-{tag}-{os.getpid()}", HC_SOCKET=str(d / "hc.sock"),
                   XDG_RUNTIME_DIR=str(d / "run"), XDG_STATE_HOME=str(d / "state"),
                   XDG_CONFIG_HOME=str(d / "config"), HOME=str(d / "home"))
        for key in ("HC_SOCKET", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "HOME"):
            if not env[key].startswith(str(d) + "/"):
                raise RuntimeError(f"{key} is not private: {env[key]}")
        if len(env["HC_SOCKET"]) >= MAX_SOCKET:
            raise RuntimeError(f"socket path too long: {env['HC_SOCKET']}")
        self.env = env
        self.start_env = dict(env, **(extra_env or {}))
        self.stopped = False
        self.id = None
        try:
            self.start()
        except BaseException:
            self.stop()   # stops only a core whose id was verified; otherwise leaves the directory and says so
            raise

    def installed_seen(self, roots=(), harness_of=lambda record: None):
        """The owner's after-the-fact check: every installed factory.py path that appears in the screen of one of
        this core's terminals, or in a Claude Code or Codex transcript whose working directory is under one of
        roots. -> [(harness or None, what was seen)], bounded; [] for an unprotected core."""
        needles = installed_needles(self.installed)
        if not needles:
            return []
        hits = set()
        for record in self.rpc("terminal.list", {"all": True})["terminals"]:
            try:
                text = self.rpc("terminal.read", {"terminal": record["address"], "lines": 0}).get("text", "")
            except (HcError, HcDown):
                continue
            flat = "".join(text.split())                 # a screen wraps long paths
            hits.update((harness_of(record), f"{record['address']} screen: {n}") for n in needles if n in flat)
        for harness, path in qa_transcripts(roots, labelled=True):
            try:
                flat = "".join(path.read_text(errors="replace").split())
            except OSError:
                continue
            hits.update((harness, f"{path.name}: {n}") for n in needles if n in flat)
        return [(h, _bounded(text)) for h, text in sorted(hits, key=lambda hit: hit[1])[:10]]

    def start(self):
        out = subprocess.run([HC, "server", "start", "--json"], env=self.start_env,
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise RuntimeError(f"hc server start failed: {out.stderr.strip() or out.stdout.strip()}")
        started = json.loads(out.stdout)["core"]["id"]
        if self.id is not None and started != self.id:
            raise RuntimeError(f"core id changed across restart: {self.id} -> {started}")
        self.id = started
        if self not in CORES:
            CORES.append(self)
        self.hello()

    def connect(self, framed=False):
        return Conn(self.env, self.id, framed)

    def hello(self):
        c = self.connect()
        c.close()
        return c.hello

    def call(self, method, params=None, timeout=15):
        """The whole JSON-RPC message (result or error), for fixtures."""
        c = self.connect()
        try:
            c.request(method, params)
            return c.message(timeout)
        finally:
            c.close()

    def rpc(self, method, params=None, timeout=15):
        msg = self.call(method, params, timeout)
        if "error" in msg:
            raise HcError(msg["error"])
        return msg["result"]

    def spawn_send(self, term, text):
        c = self.connect()
        c.request("terminal.send", {"terminal": term, "text": text})
        return c

    def server_stop(self):
        status = self.rpc("server.status")
        if status["core"]["id"] != self.id:
            raise RuntimeError(f"server.status names core {status['core']['id']}, not {self.id}: not stopping")
        try:
            return self.call("server.stop", timeout=20)
        except HcDown:
            return None

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.id is None:
            print(f"warning: no core id known for {self.dir}; nothing stopped, directory left", file=sys.stderr)
            return
        try:
            reply = self.server_stop()
        except Exception as error:
            print(f"warning: could not stop core in {self.dir}: {error}", file=sys.stderr)
            return
        if reply is not None and "error" in reply:
            print(f"warning: server.stop failed for {self.dir}: {reply['error']}; directory left", file=sys.stderr)
            return
        # Confirm on the socket itself: hc connect (and so hello) would start the core again.
        deadline = time.monotonic() + 10
        state = True
        while time.monotonic() < deadline:
            state = self.listening()
            if state is False:
                shutil.rmtree(self.dir, ignore_errors=True)
                return
            time.sleep(0.2)
        what = "still listens" if state else "could not be confirmed stopped"
        print(f"warning: core {self.id} {what} after server.stop; directory {self.dir} left", file=sys.stderr)

    def listening(self):
        """True if a core accepts connections on the private socket, False only when the socket is gone or
        refuses (ENOENT, ECONNREFUSED), None when that cannot be told (a timeout, EACCES, ...). Checked
        without hc, which would start a core."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            sock.connect(self.env["HC_SOCKET"])
            return True
        except (FileNotFoundError, ConnectionRefusedError):
            return False
        except OSError:
            return None
        finally:
            sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


@atexit.register
def _stop_all():
    for core in CORES:
        core.stop()


PROBE = r'''
import json, os, sys, time, tty
log = open(sys.argv[1], "a", buffering=1)
tty.setraw(0)
os.write(1, ("\x1b[?2004h\x1b]7501;%s\x1b\\probe %s ready\r\n" % (sys.argv[2], sys.argv[3])).encode())
while True:
    data = os.read(0, 4096)
    if not data:
        break
    log.write(json.dumps({"t": time.time(), "d": data.decode("utf-8", "replace")}) + "\n")
    shown = data.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"").replace(b"\r", b"\r\n")
    os.write(1, bytes(b for b in shown if b >= 32 or b in (10, 13)))
'''


class Probe:
    """A terminal that reports OSC 7501 and logs every input chunk with its time."""

    def __init__(self, core, name, fields="state=idle:app=probe", labels=None, wait_status=True):
        self.core = core
        self.log = core.dir / f"probe-{name}.log"
        self.log.touch()
        script = core.dir / "probe.py"
        if not script.exists():
            script.write_text(PROBE)
        self.params = {"argv": [sys.executable, str(script), str(self.log), fields, name],
                       "cwd": str(core.dir), "labels": labels or {}, "size": {"cols": 100, "rows": 30}}
        self.record = core.rpc("terminal.run", self.params)
        self.address = self.record["address"]
        if wait_status:
            deadline = time.monotonic() + 10
            while (core.rpc("terminal.get", {"terminal": self.address}).get("status") or None) is None:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"probe {name} reported no status")
                time.sleep(0.1)

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def text(self):
        return "".join(e["d"] for e in self.events())

    def first(self, needle):
        """Time of the chunk where needle is complete, or None."""
        seen = ""
        for e in self.events():
            seen += e["d"]
            if needle in seen:
                return e["t"]
        return None


class Attach:
    """Minimal framed terminal.attach client. Its DATA frames count as a person's keys."""

    def __init__(self, core, term, cols=100, rows=30):
        self.c = core.connect(framed=True)
        self.lock = threading.Lock()
        self.received = bytearray()
        self._send(frame(CONTROL, DATA, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "terminal.attach",
                                                     "params": {"terminal": term, "cols": cols, "rows": rows}}).encode()))
        while True:
            channel, kind, payload = self._frame(10)
            if channel == CONTROL and kind == DATA:
                msg = json.loads(payload)
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise HcError(msg["error"])
                    self.channel = msg["result"]["channel"]
                    break
            else:
                self._take(channel, kind, payload)
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _send(self, data):
        with self.lock:
            self.c.write(data)

    def _frame(self, timeout=None):
        channel, kind, size = struct.unpack("<IBI", self.c.exact(9, timeout))
        return channel, kind, self.c.exact(size, timeout)

    def _take(self, channel, kind, payload):
        if channel != CONTROL and kind == DATA and payload:
            self.received += payload
            self._send(frame(channel, CREDIT, struct.pack("<I", len(payload))))

    def _drain(self):
        while True:
            try:
                self._take(*self._frame())
            except (HcDown, OSError, ValueError, struct.error):
                return

    def type(self, data):
        self._send(frame(self.channel, DATA, data))

    def close(self):
        try:
            self._send(frame(self.channel, CLOSE))
        except OSError:
            pass
        self.c.close()


def now():
    return time.time()


def rounded(value):
    return None if value is None else round(value, 2)


# LC-1: an in-flight terminal.send whose hc connect client dies during draft_wait.

def lc1():
    draft_wait = 12
    result = {"check": "LC-1", "draft_wait": draft_wait, "variants": {}}
    with Core("lc1", config=f"draft_wait = {draft_wait}\n") as qa:
        result["hc"] = qa.hello()["version"]
        # Baseline: a send with no draft, so only hc's quiet pause delays it.
        base = Probe(qa, "lc1-baseline")
        time.sleep(1.0)
        token = f"BASE_LC1_{os.getpid()}"
        t_send = now()
        qa.rpc("terminal.send", {"terminal": base.address, "text": token}, timeout=30)
        typed = base.first(token)
        baseline = typed - t_send if typed else None
        result["baseline_typed_after_send_s"] = rounded(baseline)
        end_at = max((baseline or 3) + 3, 6)   # the client ends here, well past the baseline and before draft_wait
        result["client_ends_after_send_s"] = rounded(end_at)
        for variant in ("control", "sigkill", "eof", "sigkill_then_enter"):
            probe = Probe(qa, f"lc1-{variant}")
            attach = Attach(qa, probe.address)
            attach.type(f"DRAFT_{variant}:".encode())
            t_key = now()
            time.sleep(0.3)
            token = f"TOKEN_LC1_{variant}_{os.getpid()}"
            t_send = now()
            conn = qa.spawn_send(probe.address, token)
            time.sleep(end_at)
            held = conn.p.poll() is None and probe.first(token) is None
            row = {"held_when_client_ended": held}
            if variant == "control":
                try:
                    row["reply"] = conn.reply(draft_wait + 15)
                except (HcError, HcDown) as error:
                    row["reply_error"] = str(error)
                row["reply_after_s"] = rounded(now() - t_send)
                conn.close()
            elif variant in ("sigkill", "sigkill_then_enter"):
                conn.kill()
                row["client_exit"] = conn.p.returncode
                if variant == "sigkill_then_enter":
                    time.sleep(0.3)
                    attach.type(b"\r")
                    row["enter_after_s"] = rounded(now() - t_send)
            else:
                conn.p.stdin.close()
                try:
                    conn.p.wait(5)
                    row["client_exit"] = conn.p.returncode
                    row["client_exited_after_s"] = rounded(now() - t_send)
                except subprocess.TimeoutExpired:
                    row["client_exit"] = "still running 5 s after EOF"
            # Watch until well past the draft hold.
            remaining = draft_wait + 3 - (now() - t_key)
            if remaining > 0:
                time.sleep(remaining)
            # Snapshot at the deadline: was the token typed, and is the client still alive?
            typed_by_deadline = probe.first(token)
            alive = conn.p.poll() is None
            if alive:
                if variant == "eof" and typed_by_deadline is None:
                    row["inconclusive"] = "EOF client still alive and token not typed by the deadline"
                conn.kill()
            time.sleep(0.5)
            typed = probe.first(token)   # final delivery comes from the complete log
            if typed is not None and typed_by_deadline is None:
                row["typed_after_deadline"] = True
                row.setdefault("inconclusive", "token typed only after the deadline")
            row["typed"] = typed is not None
            row["typed_after_send_s"] = rounded(typed - t_send) if typed else None
            row["typed_after_last_key_s"] = rounded(typed - t_key) if typed else None
            row["terminal"] = qa.rpc("terminal.get", {"terminal": probe.address})["state"]
            # Delivery goes on: a fresh send to the same terminal arrives, after the first token if that was typed.
            follow = f"FOLLOW_LC1_{variant}_{os.getpid()}"
            t_follow = now()
            try:
                qa.rpc("terminal.send", {"terminal": probe.address, "text": follow}, timeout=30)
            except (HcError, HcDown) as error:
                row["follow_error"] = str(error)
            followed = probe.first(follow)
            row["follow_typed_after_s"] = rounded(followed - t_follow) if followed else None
            text = probe.text()
            row["follow_after_first"] = (text.index(token) < text.index(follow)) if typed and followed else None
            row["first_typed_once"] = text.count(token) <= 1
            attach.close()
            result["variants"][variant] = row
        result["variants"]["mid_paste_kill"] = mid_paste_kill(qa)
        v = result["variants"]
        four = ("control", "sigkill", "eof", "sigkill_then_enter")
        control = v["control"]
        reasons = []
        if baseline is None:
            reasons.append("no baseline")
        reasons += [f"{k}: not held when the client ended" for k in four if not v[k]["held_when_client_ended"]]
        if "reply" not in control:
            reasons.append(f"control: no reply ({control.get('reply_error')})")
        if not control["typed"] or control["typed_after_send_s"] < end_at:
            reasons.append("control: not typed after the hold")
        reasons += [f"{k}: follow-up not delivered" for k in four if v[k]["follow_typed_after_s"] is None]
        reasons += [f"{k}: first token typed twice" for k in four if not v[k]["first_typed_once"]]
        reasons += [f"{k}: {v[k]['inconclusive']}" for k in four if v[k].get("inconclusive")]
        if v["mid_paste_kill"]["not_pasted"]:
            reasons.append("mid_paste_kill: some runs never pasted")
        result["result"] = "inconclusive: " + "; ".join(reasons) if reasons else "pass"
    return result


def mid_paste_kill(qa, runs=20):
    """Variant C: no draft; kill the client 0-300 ms after hc starts pasting (paste and Enter are about 0.2 s apart)."""
    probe = Probe(qa, "lc1-midpaste")
    rows = []
    for i in range(runs):
        token = f"MIDPASTE_{i}_{os.getpid()}"
        conn = qa.spawn_send(probe.address, token)
        deadline = time.monotonic() + 15
        pasted = False
        while time.monotonic() < deadline and not pasted:
            pasted = token in probe.text()
            time.sleep(0.005)
        delay = random.uniform(0, 0.3)
        time.sleep(delay)
        conn.kill()
        time.sleep(1.0)
        text = probe.text()
        at = text.find(token)
        tail = text[at + len(token):] if at >= 0 else ""
        submitted = "\r" in tail.split("MIDPASTE_", 1)[0]
        rows.append({"run": i, "kill_after_paste_ms": round(delay * 1000), "pasted": pasted,
                     "submitted": submitted})
        if at >= 0 and not submitted:
            press_enter(qa, probe.address)
    return {"runs": runs, "pasted_without_enter": sum(1 for r in rows if r["pasted"] and not r["submitted"]),
            "not_pasted": sum(1 for r in rows if not r["pasted"]), "rows": rows}


def press_enter(qa, term):
    """Clear a pasted line left without Enter, so the next run starts clean."""
    a = Attach(qa, term)
    a.type(b"\r")
    time.sleep(0.2)
    a.close()


# LC-10: does the core honour HC_DRAFT_MS, and does config.toml's draft_wait win over it?

def timed_hold(tag, config="", extra_env=None, cap=20):
    with Core(tag, config=config, extra_env=extra_env) as qa:
        probe = Probe(qa, tag)
        attach = Attach(qa, probe.address)
        attach.type(b"DRAFT10:")
        t_key = now()
        time.sleep(0.3)
        token = f"TOKEN_{tag}_{os.getpid()}"
        t_send = now()
        conn = qa.spawn_send(probe.address, token)
        row = {"config": config.strip() or None, "env": extra_env or {}}
        try:
            conn.reply(cap)
            row["reply_after_send_s"] = rounded(now() - t_send)
        except HcDown as error:
            if not error.timeout:
                row["error"] = f"connection failed: {error}"
            elif probe.first(token) is not None:
                row["reply_late"] = f"typed before the {cap} s cap; reply still pending"
            else:
                row["held_past_cap_s"] = cap
                attach.type(b"\r")
            try:
                conn.reply(30)
            except (HcDown, HcError):
                pass
        except HcError as error:
            row["error"] = str(error)
        conn.close()
        typed = probe.first(token)
        row["typed_after_last_key_s"] = rounded(typed - t_key) if typed else None
        attach.close()
        return row


def lc10():
    return {"check": "LC-10", "cases": {
        "10a_env_only": timed_hold("lc10a", extra_env={"HC_DRAFT_MS": "3000"}),
        "10b_config_6_env_2": timed_hold("lc10b", config="draft_wait = 6\n", extra_env={"HC_DRAFT_MS": "2000"}),
        "10c_neither": timed_hold("lc10c"),
        "10d_env_3_settle_0": timed_hold("lc10d", extra_env={"HC_DRAFT_MS": "3000", "HC_SETTLE_MS": "0"}),
        "10e_config_4_settle_0": timed_hold("lc10e", config="draft_wait = 4\n", extra_env={"HC_SETTLE_MS": "0"}),
    }}


# LC-13: rodeo as a test client, running in a second private core, attached to the QA core.

class Rodeo:
    def __init__(self, qa, drv, mode="unset_env", cols=160, rows=48):
        self.qa, self.drv = qa, drv
        r = qa.dir / f"rodeo-{mode}"
        r.mkdir()
        conf = r / "rodeo.toml"
        conf.write_text("[experimental]\nallow_nested = true\n" if mode == "allow_nested" else "")
        env = {k: qa.env[k] for k in ("HC_SESSION", "HC_SOCKET", "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
                                      "XDG_CONFIG_HOME", "HOME")}
        env.update(RODEO_CONFIG_PATH=str(conf), HERDR_CONFIG_PATH=str(r / "herdr-config.toml"))
        unset = ["-u", "HC_TERMINAL_ID", "-u", "HC_TERMINAL_SOCKET", "-u", "HC_SCOPE"] if mode == "unset_env" else []
        argv = ["env", *unset, RODEO]
        self.record = drv.rpc("terminal.run", {"argv": argv, "cwd": str(qa.dir), "env": env,
                                               "size": {"cols": cols, "rows": rows}})
        self.term = self.record["address"]
        self.size = cols, rows
        self.focused_terminal = None

    def keys(self, *names):
        return self.drv.rpc("terminal.keys", {"terminal": self.term, "keys": list(names)})

    def type(self, text):
        """Type through the client, leaving the draft unsubmitted."""
        if "\n" in text or "\r" in text:
            raise ValueError("type takes one line; send Enter separately")
        for char in text:   # hc's key names ignore case: an uppercase letter is shift plus the letter
            self.keys("space" if char == " " else f"shift+{char.lower()}" if char.isupper() else char)

    def paste(self, text, submit=True):
        """Paste text through the client as the owner's terminal would (bracketed, through an attach, whose
        input counts as a person's keys), then Enter; any characters, newlines included."""
        attach = Attach(self.drv, self.term, cols=self.size[0], rows=self.size[1])
        try:
            time.sleep(0.5)
            attach.type(b"\x1b[200~" + text.encode() + b"\x1b[201~")
            if submit:
                time.sleep(0.5)
                attach.type(b"\r")
            time.sleep(0.5)
        finally:
            attach.close()

    def _move(self, direction, count):
        for _ in range(count):
            self.keys("ctrl+b")
            time.sleep(0.2)
            self.keys(direction)
            time.sleep(0.2)

    def focus(self, pane_title):
        """Move within the current QA tab, starting at its first pane on attach."""
        doc = self.qa.rpc("doc.get", {"name": "herdr-app"})["value"]
        for space in doc["spaces"]:
            for tab in space["tabs"]:
                root = tab["root"]
                if root["type"] != "row":
                    raise RuntimeError("rodeo focus requires a row of panes or columns")
                columns = [node["children"] if node["type"] == "column" else [node] for node in root["children"]]
                current, targets = None, []
                for x, column in enumerate(columns):
                    for y, pane in enumerate(column):
                        address = pane["content"]["terminal"]
                        if address == self.focused_terminal or (self.focused_terminal is None and (x, y) == (0, 0)):
                            current = x, y
                        if pane["title"] == pane_title:
                            targets.append((x, y, address))
                if current is not None:
                    if not targets:
                        raise RuntimeError(f"no pane titled {pane_title} in the current rodeo tab")
                    if len(targets) != 1:
                        raise RuntimeError(f"the current rodeo tab has multiple panes titled {pane_title}")
                    self._focus_pane(current, targets[0], columns)
                    return
        raise RuntimeError("could not find the focused pane in the QA layout")

    def _focus_pane(self, current, target, columns):
        x, y = current
        to_x, to_y, terminal = target
        if x == to_x:
            self._move("j" if to_y > y else "k", abs(to_y - y))
        else:
            self._move("k", y)
            self._move("l" if to_x > x else "h", abs(to_x - x))
            # A horizontal move can land below the top of a stacked column.
            self._move("k", len(columns[to_x]) - 1)
            self._move("j", to_y)
        self.focused_terminal = terminal

    def screen(self):
        return self.drv.rpc("terminal.read", {"terminal": self.term, "lines": 0}).get("text", "")

    def wait_for(self, needles, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            screen = self.screen()
            if all(n in screen for n in needles):
                return screen
            if time.monotonic() > deadline:
                return None
            time.sleep(0.25)

    def alive(self):
        return self.drv.rpc("terminal.get", {"terminal": self.term})["state"] == "live"

    def close(self):
        try:
            self.drv.rpc("terminal.kill", {"terminal": self.term})
        except HcError:
            pass


def _focus_probe_layout(qa, probes, stack):
    def ident(prefix):
        return prefix + uuid.uuid4().hex

    panes = [{"id": ident("p"), "type": "block", "title": "@" + name,
              "content": {"terminal": probe.address}} for name, probe in probes.items()]
    if stack:
        panes = [{"id": ident("s"), "type": "column", "children": [panes[0], panes[3]]}, *panes[1:3]]
    root = {"id": ident("s"), "type": "row", "children": panes}
    tab = {"id": ident("tb"), "name": "team", "root": root}
    space = {"id": ident("sp"), "name": "focus", "folder": str(qa.dir), "tabs": [tab]}
    qa.rpc("doc.set", {"name": "herdr-app", "value": {"spaces": [space]}, "version": 0})


def rodeo_focus():
    """Exercise the driver on a three-pane row and a stacked Factory layout."""
    result = {"check": "rodeo_focus", "rodeo": RODEO}
    if not RODEO:
        return {**result, "result": "not run: rodeo not found (set RODEO_BIN)"}
    for name, order, stack in (("row", ("a", "c", "b", "a"), False),
                               ("stack", ("a", "d", "b", "d", "a", "c", "a"), True)):
        with Core("focusqa", config="draft_wait = 15\n") as qa, Core("focusdrv") as drv:
            probes = {letter: Probe(qa, letter) for letter in ("abcd" if stack else "abc")}
            _focus_probe_layout(qa, probes, stack)
            rodeo = Rodeo(qa, drv)
            try:
                if not rodeo.wait_for(["team", *("@" + letter for letter in probes)]):
                    return {**result, "result": f"fail: {name} layout did not render"}
                reached = []
                for i, target in enumerate(order):
                    rodeo.focus("@" + target)
                    marker = f"focus {i}"
                    rodeo.type(marker)
                    deadline = time.monotonic() + 3
                    recipients = []
                    while time.monotonic() < deadline:
                        recipients = [letter for letter, probe in probes.items() if marker in probe.text()]
                        if recipients:
                            break
                        time.sleep(0.1)
                    if recipients != [target]:
                        return {**result, "result": f"fail: {name} focus @{target} typed into {recipients}"}
                    rodeo.keys("enter")
                    reached.append(target)
                result[name] = reached
            finally:
                rodeo.close()
    return {**result, "result": "pass"}


def hc_env_names(core):
    """Names of the HC_* variables a terminal of this core gets."""
    out = core.dir / "env.txt"
    names_only = "env | sed -n 's/^\\(HC_[A-Za-z0-9_]*\\)=.*/\\1/p' > \"$1\""   # names only, never values
    t = core.rpc("terminal.run", {"argv": ["sh", "-c", names_only, "sh", str(out)], "cwd": str(core.dir)})
    deadline = time.monotonic() + 10
    while core.rpc("terminal.get", {"terminal": t["address"]})["state"] == "live" and time.monotonic() < deadline:
        time.sleep(0.1)
    return sorted(line for line in out.read_text().splitlines() if line)


def focused(op, dev, before):
    """Which probe received input since 'before' (lengths of their texts)."""
    got = {name: p.text()[before[name]:] for name, p in (("op", op), ("dev", dev))}
    return got


def lc13():
    result = {"check": "LC-13", "rodeo": RODEO}
    if not RODEO or not os.path.exists(RODEO):
        result["result"] = "not run: rodeo not found (set RODEO_BIN)"
        return result
    result["rodeo_version"] = subprocess.run([RODEO, "--version"], capture_output=True, text=True).stdout.strip()
    draft_wait = int(os.environ.get("LC13_DRAFT_WAIT", "15"))   # 0 is the negative control: the check must fail
    result["draft_wait"] = draft_wait
    with Core("lc13qa", config=f"draft_wait = {draft_wait}\n") as qa, Core("lc13drv") as drv:
        result["driver_terminal_hc_env"] = hc_env_names(drv)
        op = Probe(qa, "op")
        dev = Probe(qa, "dev")
        doc = {"spaces": [{"id": "sp" + "1" * 32, "name": "lc13", "folder": str(qa.dir), "tabs": [
            {"id": "tb" + "1" * 32, "name": "team", "root": {"id": "s" + "1" * 32, "type": "row", "children": [
                {"id": "p" + "1" * 32, "type": "block", "title": "@op", "content": {"terminal": op.address}},
                {"id": "p" + "2" * 32, "type": "block", "title": "@dev", "content": {"terminal": dev.address}}]}}]}]}
        qa.rpc("doc.set", {"name": "herdr-app", "value": doc, "version": 0})
        for probe, name in ((op, "@op"), (dev, "@dev")):
            qa.rpc("terminal.label", {"terminal": probe.address, "labels": {
                "herdr.space": "sp" + "1" * 32, "herdr.tab": "tb" + "1" * 32, "herdr.seen": "0", "name": name}})
        rodeo = Rodeo(qa, drv, "unset_env")
        screen = rodeo.wait_for(["team", "@op", "@dev"])
        result["render"] = screen is not None
        result["rodeo_live"] = rodeo.alive()
        if screen is None:
            result["screen"] = rodeo.screen()[-3000:]
            result["result"] = "fail: rodeo did not render the tab"
            rodeo.close()
            return result
        time.sleep(1.0)
        # Which pane has focus at start: a marker key.
        before = {"op": len(op.text()), "dev": len(dev.text())}
        rodeo.keys("m")
        time.sleep(0.8)
        start = focused(op, dev, before)
        result["focus_at_start"] = [n for n, t in start.items() if "m" in t]
        # Focus moves: prefix then h / l (focus_pane_left / right).
        moves = {}
        for name, seq in (("left", ("ctrl+b", "h")), ("right", ("ctrl+b", "l"))):
            rodeo.keys(seq[0])
            time.sleep(0.2)
            rodeo.keys(seq[1])
            time.sleep(0.5)
            before = {"op": len(op.text()), "dev": len(dev.text())}
            rodeo.keys("z")
            time.sleep(0.8)
            got = focused(op, dev, before)
            moves[name] = [n for n, t in got.items() if "z" in t]
        result["focus_after"] = moves
        # Submit the marker keys in both panes (focus is on @dev), then focus @op again.
        rodeo.keys("enter")
        time.sleep(0.3)
        rodeo.keys("ctrl+b")
        time.sleep(0.2)
        rodeo.keys("h")
        time.sleep(0.5)
        rodeo.keys("enter")
        time.sleep(1.0)
        # Baseline: no draft in @op, so only hc's quiet pause delays the send.
        base = f"BASE_LC13_{os.getpid()}"
        t_base = now()
        conn = qa.spawn_send(op.address, base)
        try:
            conn.reply(20)
        except (HcDown, HcError) as error:
            result["baseline_error"] = str(error)
        conn.close()
        typed = op.first(base)
        baseline = typed - t_base if typed else None
        result["baseline_typed_after_send_s"] = rounded(baseline)
        # Draft typed through rodeo: the send must be held well past the baseline, until Enter.
        for ch in "draft":
            rodeo.keys(ch)
        time.sleep(0.5)
        result["draft_reached_op"] = "draft" in op.text()
        t_key = op.first("draft") or now()   # the hold runs from when @op received the last draft key
        token = f"TOKEN_LC13_{os.getpid()}"
        t_send = now()
        conn = qa.spawn_send(op.address, token)
        check_at = max((baseline or 3) + 3, 6)
        time.sleep(check_at)
        result["held_check_after_send_s"] = rounded(check_at)
        result["send_held_by_rodeo_keys"] = conn.p.poll() is None and op.first(token) is None
        rodeo.keys("enter")
        t_enter = now()
        try:
            conn.reply(20)
            result["send_returned_after_enter_s"] = rounded(now() - t_enter)
        except (HcDown, HcError) as error:
            result["send_error"] = str(error)
        conn.close()
        typed = op.first(token)
        text = op.text()
        result["typed_after_send_s"] = rounded(typed - t_send) if typed else None
        result["draft_submitted_before_token"] = ("draft\r" in text and token in text
                                                  and text.index("draft\r") < text.index(token))
        result["token_on_rodeo_screen"] = rodeo.wait_for([token], timeout=5) is not None
        result["keep_after_rodeo"] = {name: qa.rpc("terminal.get", {"terminal": p.address}).get("keep")
                                      for name, p in (("op", op), ("dev", dev))}
        rodeo.close()
        # allow_nested: rodeo with the driver terminal's own HC_* identity, nesting allowed by config.
        nested = Rodeo(qa, drv, "allow_nested")
        result["allow_nested_render"] = nested.wait_for(["team", "@op", "@dev"]) is not None
        if not result["allow_nested_render"]:
            result["allow_nested_screen"] = nested.screen()[-1500:]
        nested.close()
        result["typed_after_enter_s"] = rounded(typed - t_enter) if typed else None
        released = typed is not None and typed - t_enter <= 2.0 and typed - t_key < draft_wait
        result["released_by_enter"] = released
        passed = (result["render"] and released and result["focus_at_start"] == ["op"]
                  and moves == {"left": ["op"], "right": ["dev"]} and baseline is not None
                  and result["draft_reached_op"] and result["send_held_by_rodeo_keys"]
                  and result["draft_submitted_before_token"] and result["token_on_rodeo_screen"])
        result["result"] = "pass" if passed else "fail"
    return result

# Phase 5a (phase_5a.md §2). LC-8 drives rodeo against private cores and needs no login. The others start real
# harnesses with the owner's own login: a member allowed to runs them (@operator's decision), each harness started
# normally, on a private core, in a throwaway folder. Nothing here reads a credential, a transcript or a settings
# file: a settings file is only stat()ed to show it did not change, and a variable a harness gives its commands is
# written to a private file by the harness, compared here and never recorded.

ROOT = pathlib.Path(__file__).resolve().parents[1]
OWNER_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "USER", "LOGNAME", "SHELL",
              "DBUS_SESSION_BUS_ADDRESS")
REMEMBER = ("This is a test of resuming conversations. Remember this code word: {token}. "
            "Reply with only the code word, in lowercase letters.")
RECALL = ("What code word did I give you earlier in this conversation? Reply with only that code word spelled "
          "backwards, in capital letters, or with the single word NONE if I gave you none.")
ENV_COMMAND = ("Run this exact shell command and nothing else, then reply with only the word finished: "
               "printf 'v=%s\\n' \"$" "{{{var}}}\" > '{path}'")
SUBAGENT = ("Start exactly one sub-agent ({how}) and tell it to run this exact shell command and nothing else: "
            "printf 'v=%s\\n' \"$" "{{{var}}}\" > '{child}' . When it has finished, run this exact shell command "
            "yourself: printf 'v=%s\\n' \"$" "{{{var}}}\" > '{parent}' . Then reply with only the word finished.")


class NotRun(Exception):
    pass


def harness_table():
    import tomllib
    return tomllib.loads((ROOT / "skill" / "harnesses.toml").read_text())


def owner_env():
    """What a harness started normally finds: the owner's HOME and XDG base directories (their defaults when unset),
    so that it uses its own login and settings. Passed on, never read or recorded."""
    home = os.path.expanduser("~")
    env = {"HOME": home}
    for key, default in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                         ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        env[key] = os.environ.get(key) or os.path.join(home, default)
    env.update({k: os.environ[k] for k in OWNER_KEYS if k in os.environ})
    return env


def stamps(roots, depth=2):
    """(size, mtime_ns) of the files under each root, to show that nothing changed them; nothing is read."""
    found = {}
    for root in map(pathlib.Path, roots):
        if root.is_file():
            st = root.stat()
            found[str(root)] = (st.st_size, st.st_mtime_ns)
            continue
        if not root.is_dir():
            continue
        for path, dirs, files in os.walk(root):
            if len(pathlib.Path(path).relative_to(root).parts) >= depth:
                dirs[:] = []
            for name in files:
                p = os.path.join(path, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                found[p] = (st.st_size, st.st_mtime_ns)
    return found


def changed(before, after):
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def token():
    value = "ZQ" + "".join(random.choices("ABCDEFGHJKLMNPRSTUVWXY", k=8))
    EVIDENCE["tokens"].append(value)        # the qualification's test markers, looked for on screens at close
    return value


def folder(qa, name, git=True):
    """A throwaway project folder no harness has seen."""
    d = qa.dir / "work" / f"{name}-{random.randrange(16 ** 6):06x}"
    d.mkdir(parents=True)
    if git:
        subprocess.run(["git", "init", "-q", str(d)], check=True)
    return d


def env_value(path, timeout=240):
    """The value a harness wrote with ENV_COMMAND: None when no file came, "" when the variable was empty."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text()
            if text.endswith("\n"):
                return text[2:-1] if text.startswith("v=") else text.strip()
        time.sleep(1)
    return None


class Harness:
    """A real harness in a terminal of the QA core, started as the owner would start it."""

    def __init__(self, qa, harness, args, cwd, env=None, cols=200, rows=50):
        exe = shutil.which(harness)
        if not exe:
            raise NotRun(f"{harness} is not on PATH")
        self.qa, self.harness = qa, harness
        self.dialogs = harness_table().get(harness, {}).get("dialogs", [])
        self.argv = [exe, *skills_off(harness, getattr(qa, "installed", [])), *args]   # before a subcommand
        env = {**owner_env(), **(env or {})}
        if getattr(qa, "bin", None) is not None:          # a protected core's herdr stub first
            env["PATH"] = f"{qa.bin}:{env.get('PATH', '')}"
        self.record = qa.rpc("terminal.run", {"argv": self.argv, "cwd": str(cwd), "env": env,
                                              "size": {"cols": cols, "rows": rows}, "labels": {"name": "@lc-" + harness}})
        self.term = self.record["address"]
        self.answered = []

    @classmethod
    def attach(cls, qa, harness, address):
        """Observe a terminal started by someone else (a QA operator, a member up started): no launch."""
        h = cls.__new__(cls)
        h.qa, h.harness, h.term, h.answered, h.argv = qa, harness, address, [], None
        h.dialogs = harness_table().get(harness, {}).get("dialogs", [])
        return h

    def get(self):
        return self.qa.rpc("terminal.get", {"terminal": self.term})

    def screen(self):
        try:
            return self.qa.rpc("terminal.read", {"terminal": self.term, "lines": 0}).get("text", "")
        except HcError:
            return ""

    def flat(self):
        return "".join(self.screen().split())

    def dialog(self, flat):
        for text, keys in self.dialogs:
            if "".join(text.split()) in flat:
                return text, keys
        return None

    def settle(self, timeout=120, answer=True, quiet=6.0):
        """Wait until the harness takes a prompt. Returns "idle" (its OSC status, or a screen unchanged for quiet
        seconds when it reports none), "dialog: <text>" (when answer is False or the dialog has no keys),
        "finished (...)" or "timeout". With answer, a configured dialog is answered once, as up does."""
        deadline = time.monotonic() + timeout
        last, since, seen = None, time.monotonic(), None
        while time.monotonic() < deadline:
            t = self.get()
            if t["state"] == "finished":
                return f"finished (code {t.get('exit_code')}): {self.last_line()}"
            word = (t.get("status") or {}).get("state")
            flat = self.flat()
            found = self.dialog(flat) if word != "working" else None
            if found:
                text, keys = found
                if not answer or not keys:
                    return f"dialog: {text}"
                if text not in self.answered and seen == text:   # as up does: on its second poll
                    self.qa.rpc("terminal.keys", {"terminal": self.term, "keys": keys})
                    self.answered.append(text)
                seen = text
                time.sleep(0.5 if text not in self.answered else 1.5)
                last = None
                continue
            seen = None
            if word in ("idle", "done"):        # done: idle with a turn not yet seen in the app
                return "idle"
            if flat != last:
                last, since = flat, time.monotonic()
            elif word is None and time.monotonic() - since >= quiet:
                return "idle (quiet screen; no status)"
            time.sleep(0.5)
        return "timeout"

    def last_line(self):
        lines = [line for line in self.screen().splitlines() if line.strip()]
        return lines[-1].strip()[:200] if lines else ""

    def send(self, text):
        self.qa.rpc("terminal.send", {"terminal": self.term, "text": text}, timeout=90)

    def ask(self, text, expect=None, timeout=300):
        """Send a prompt; with expect, wait for that text on the screen (whitespace ignored), else for the turn to
        end. True when it came."""
        turn = self.get().get("turn")
        self.send(text)
        time.sleep(3)
        deadline = time.monotonic() + timeout
        last, since = None, time.monotonic()
        while time.monotonic() < deadline:
            flat = self.flat()
            if expect is not None and expect in flat:
                return True
            t = self.get()
            if t["state"] == "finished":
                return False
            word = (t.get("status") or {}).get("state")
            if expect is None:
                if word in ("idle", "done") and t.get("turn") != turn:
                    return True
                if flat != last:
                    last, since = flat, time.monotonic()
                elif word is None and time.monotonic() - since >= 15:
                    return True
            time.sleep(1)
        return False

    def stop(self):
        time.sleep(2)   # let the harness save the last turn
        try:
            self.qa.rpc("terminal.kill", {"terminal": self.term})
        except HcError:
            return
        deadline = time.monotonic() + 15
        while self.get()["state"] != "finished" and time.monotonic() < deadline:
            time.sleep(0.3)


def store(h, tok):
    return h.ask(REMEMBER.format(token=tok), expect=tok.lower())


def recall(h, tok, others=()):
    """Which code word the conversation holds: tok, one of others, or None."""
    h.ask(RECALL)
    flat = h.flat()
    for t in (tok, *others):
        if t[::-1] in flat:
            return t
    return None


def capture(h, var, where):
    """The value of var in the harness's commands (kept out of the results), or None when none was written."""
    path = where / f"{var}-{random.randrange(16 ** 6):06x}.txt"
    h.ask(ENV_COMMAND.format(var=var, path=path))
    return env_value(path, timeout=60)


def harness_version(name):
    exe = shutil.which(name)
    if not exe:
        return None
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, check=False)
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else "unknown"


def split_seen(seen, exempt=()):
    """(failures, observations): a sighting fails where the harness's skill discovery was switched off (or the
    harness is unknown), and is only recorded for one without a switch or exempt from it (the owner's decision)."""
    off = [h for h in SKILLS_SWITCH if h not in exempt]
    failed = [text for harness, text in seen if harness is None or harness in off]
    return failed, [f"{harness}: {text}" for harness, text in seen if harness is not None and harness not in off]


def lc_harness(record):
    name = (record.get("labels") or {}).get("name", "")
    return name[len("@lc-"):] if name.startswith("@lc-") else None


def harness_check(name, harnesses, body):
    """Common frame: versions, a private QA core with no draft hold, NotRun as "not run: <why>"."""
    result = {"check": name, "versions": {h: harness_version(h) for h in harnesses},
              "os": f"{os.uname().sysname} {os.uname().release}"}
    missing = [h for h, v in result["versions"].items() if v is None]
    if len(missing) == len(harnesses):
        result["result"] = f"not run: {', '.join(missing)} not on PATH"
        return result
    try:
        with Core(name.lower().replace("-", "").replace("+", ""), config="draft_wait = 0\n", protect=True) as qa:
            try:
                body(qa, result)
            finally:
                seen = qa.installed_seen([qa.dir], lc_harness)
                failed, observed = split_seen(seen)
                if observed:
                    result["installed_factory_observed"] = observed
                if failed:            # discovery was off for that harness: the check fails whatever it observed
                    result.update(result="fail: an installed Factory script appeared in a QA terminal or transcript",
                                  installed_factory_seen=failed)
    except NotRun as why:
        if "installed_factory_seen" not in result:
            result["result"] = f"not run: {why}"
    return result


def lc2():
    """Codex: does -c 'projects."<dir>".trust_level="trusted"' suppress the trust dialog without writing config?"""
    def body(qa, result):
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        before = stamps([codex_home], depth=1)
        base = folder(qa, "codex-baseline")
        h = Harness(qa, "codex", ["--no-daemon"], base)
        result["baseline"] = h.settle(timeout=90, answer=False)
        h.stop()
        trusted = folder(qa, "codex-trusted")
        key = f'projects."{trusted}".trust_level="trusted"'
        h = Harness(qa, "codex", ["--no-daemon", "-c", key], trusted)
        result["with_flag"] = h.settle(timeout=90, answer=False)
        result["with_flag_answers"] = store(h, token()) if result["with_flag"].startswith("idle") else False
        h.stop()
        result["codex_home_changed"] = changed(before, stamps([codex_home], depth=1))   # paths only
        if not result["baseline"].startswith("dialog"):
            result["result"] = "inconclusive: the baseline showed no trust dialog"
        else:
            passed = result["with_flag"].startswith("idle") and result["with_flag_answers"] \
                and not result["codex_home_changed"]
            result["result"] = "pass" if passed else "fail"
    return harness_check("LC-2", ["codex"], body)


def claude_args(qa):
    d = qa.dir / "claude-files"
    d.mkdir(exist_ok=True)
    prompt = d / "probe.md"
    prompt.write_text("You are a test probe. Keep every reply as short as asked.\n")
    settings = d / "probe.settings.json"
    settings.write_text(json.dumps({"promptSuggestionEnabled": False, "skipDangerousModePermissionPrompt": True}))
    return ["--append-system-prompt-file", str(prompt), "--settings", str(settings),
            "--permission-mode", "bypassPermissions"]


def lc3():
    """Claude Code: --session-id with the prompt file and settings, then --resume of that ID."""
    import uuid

    def body(qa, result):
        work = folder(qa, "claude")
        sid = str(uuid.uuid4())
        h = Harness(qa, "claude", ["--session-id", sid, *claude_args(qa)], work)
        result["fresh"] = h.settle()
        if not result["fresh"].startswith("idle"):
            result["result"] = "fail: did not start with --session-id"
            result["screen_tail"] = h.last_line()
            h.stop()
            return
        value = capture(h, "CLAUDE_CODE_SESSION_ID", qa.dir)
        result["env_matches_assigned"] = None if value is None else value == sid
        tok = token()
        result["stored"] = store(h, tok)
        h.stop()
        h = Harness(qa, "claude", ["--resume", sid, *claude_args(qa)], work)
        result["resume"] = h.settle()
        result["recalled"] = recall(h, tok) == tok if result["resume"].startswith("idle") else False
        value = capture(h, "CLAUDE_CODE_SESSION_ID", qa.dir) if result["recalled"] else None
        result["env_after_resume_matches"] = None if value is None else value == sid
        h.stop()
        passed = result["stored"] and result["recalled"] and result["env_matches_assigned"]
        result["result"] = "pass" if passed else "fail"
    return harness_check("LC-3", ["claude"], body)


def opencode_ids():
    import uuid
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return {"uuid4": str(uuid.uuid4()), "ses_prefixed": "ses_" + "".join(random.choices(alphabet, k=26))}


def lc4():
    """OpenCode: which never-used ID does --session accept, and is it reopened (LC-4); which variable its commands
    get, before and after a switch to a new conversation (LC-11)."""
    def body(qa, result):
        result["formats"] = {}
        for fmt, sid in opencode_ids().items():
            work = folder(qa, f"opencode-{fmt}")
            seen = result["formats"][fmt] = {}
            h = Harness(qa, "opencode", ["--session", sid], work)
            seen["start"] = h.settle()
            if not seen["start"].startswith("idle"):
                seen["screen_tail"] = h.last_line()
                h.stop()
                continue
            tok = token()
            seen["stored"] = store(h, tok)
            listing = qa.dir / f"opencode-env-{fmt}.txt"
            h.ask("Run this exact shell command and nothing else, then reply with only the word finished: "
                  "{ for v in $(env | cut -d= -f1 | grep -iE 'opencode|session|conversation|thread'); do "
                  "printf '%s=%s\\n' \"$v\" \"$(printenv \"$v\")\"; done; echo end; } > '" + str(listing) + "'")
            env_value(listing, timeout=60)
            pairs = [line.split("=", 1) for line in (listing.read_text().splitlines() if listing.exists() else [])
                     if "=" in line]
            seen["variables"] = sorted(k for k, _ in pairs)                       # names only
            seen["variables_equal_to_id"] = sorted(k for k, v in pairs if v == sid)
            h.send("/new")
            time.sleep(3)
            h.settle(timeout=30)
            seen["after_new_still_equal"] = {k: capture(h, k, qa.dir) == sid for k in seen["variables_equal_to_id"]}
            h.stop()
            h = Harness(qa, "opencode", ["--session", sid], work)
            seen["reopen"] = h.settle()
            seen["recalled"] = recall(h, tok) == tok if seen["reopen"].startswith("idle") else False
            h.stop()
        ok = [f for f, s in result["formats"].items() if s.get("stored") and s.get("recalled")]
        result["accepted_formats"] = ok
        result["lc11_variable_candidates"] = sorted(   # equal to the ID, and changed by /new: it follows a switch
            {k for s in result["formats"].values() for k, same in s.get("after_new_still_equal", {}).items() if not same})
        result["result"] = "pass" if ok else "fail"
    return harness_check("LC-4+LC-11", ["opencode"], body)


def lc6():
    """pi and omp, each in its own session directories: fresh without --continue, the newest with it, members apart."""
    def flags(binary, d):
        return ["--session-dir", str(d)] if binary == "pi" else [f"--session-dir={d}", "--approval-mode", "yolo"]

    def one(qa, binary):
        seen = {}
        work = folder(qa, binary)
        a, b = qa.dir / f"{binary}-sessions-a", qa.dir / f"{binary}-sessions-b"
        a.mkdir()
        b.mkdir()
        t1, t2, t3 = token(), token(), token()
        names = {t1: "t1", t2: "t2", t3: "t3"}
        steps = [("a_first", a, [], t1, False), ("a_second_fresh", a, [], t2, True),
                 ("b_first", b, [], t3, False), ("a_continue", a, ["--continue"], None, True),
                 ("b_continue", b, ["--continue"], None, True)]
        for step, d, extra, keep, check in steps:
            h = Harness(qa, binary, [*flags(binary, d), *extra], work)
            seen[step] = {"start": h.settle()}
            if not seen[step]["start"].startswith("idle"):
                seen[step]["screen_tail"] = h.last_line()
                h.stop()
                return seen, False
            if check:
                seen[step]["holds"] = names.get(recall(h, t1, (t2, t3)))
            if keep:
                seen[step]["stored"] = store(h, keep)
            if binary == "pi" and step == "a_first":
                seen["pi_session_id_set"] = bool(capture(h, "PI_SESSION_ID", qa.dir))
            h.stop()
        ok = (seen["a_second_fresh"].get("holds") is None and seen["a_continue"].get("holds") == "t2"
              and seen["b_continue"].get("holds") == "t3"
              and all(seen[s].get("stored") for s in ("a_first", "a_second_fresh", "b_first")))
        return seen, ok

    def body(qa, result):
        verdicts = {}
        for binary in ("pi", "omp"):
            if shutil.which(binary) is None:
                result[binary] = "not run: not on PATH"
                continue
            result[binary], verdicts[binary] = one(qa, binary)
            result[f"{binary}_result"] = "pass" if verdicts[binary] else "fail"
        result["result"] = "pass" if len(verdicts) == 2 and all(verdicts.values()) else "fail"
    return harness_check("LC-6", ["pi", "omp"], body)


AGY_SETTINGS = ["~/.gemini", "~/.antigravity", "~/.config/antigravity", "~/.config/Antigravity",
                "~/.antigravity-cli", "~/.config/agy"]


def lc7():
    """agy: does the session-only --settings with trustedWorkspaces skip the trust dialog, with nothing saved?"""
    def body(qa, result):
        roots = [os.path.expanduser(p) for p in AGY_SETTINGS]
        before = stamps(roots)
        result["settings_roots_present"] = [r for r in roots if os.path.exists(r)]
        base = folder(qa, "agy-baseline")
        h = Harness(qa, "agy", ["--mode", "accept-edits", "--settings", '{"toolPermission":"always-proceed"}'], base)
        result["baseline"] = h.settle(timeout=90, answer=False)
        h.stop()
        trusted = folder(qa, "agy-trusted")
        settings = json.dumps({"toolPermission": "always-proceed", "trustedWorkspaces": [str(trusted)]})
        h = Harness(qa, "agy", ["--mode", "accept-edits", "--settings", settings], trusted)
        result["with_settings"] = h.settle(timeout=90, answer=False)
        result["answers"] = store(h, token()) if result["with_settings"].startswith("idle") else False
        h.stop()
        result["settings_changed"] = changed(before, stamps(roots))   # paths only; nothing is read
        if not result["baseline"].startswith("dialog"):
            result["result"] = "inconclusive: the baseline showed no trust dialog"
        elif not (result["answers"] and result["with_settings"].startswith("idle")):
            result["result"] = "fail"
        elif result["settings_changed"]:   # stat cannot tell a trust list from other state: never a pass
            result["result"] = "inconclusive: agy changed files under its settings; check whether one is its trust list"
        else:
            result["result"] = "pass"
    return harness_check("LC-7", ["agy"], body)


SUBAGENTS = {
    "claude": ("CLAUDE_CODE_SESSION_ID", "with your Task tool"),
    "codex": ("CODEX_THREAD_ID", "spawn one of your agents"),
    "agy": ("ANTIGRAVITY_CONVERSATION_ID", "with your sub-agent tool"),
}


def lc12():
    """Claude Code, Codex, agy: does a command run by a sub-agent see the parent's conversation ID?"""
    def body(qa, result):
        verdicts = {}
        for name, (var, how) in SUBAGENTS.items():
            if shutil.which(name) is None:
                result[name] = "not run: not on PATH"
                continue
            work = folder(qa, f"lc12-{name}")
            args = {"claude": claude_args(qa), "codex": ["--no-daemon", "--dangerously-bypass-approvals-and-sandbox"],
                    "agy": ["--mode", "accept-edits", "--settings", json.dumps(
                        {"toolPermission": "always-proceed", "trustedWorkspaces": [str(work)]})]}[name]
            h = Harness(qa, name, args, work)
            seen = result[name] = {"start": h.settle()}
            if not seen["start"].startswith("idle"):
                seen["screen_tail"] = h.last_line()
                h.stop()
                continue
            child, parent = qa.dir / f"{name}-child.txt", qa.dir / f"{name}-parent.txt"
            h.ask(SUBAGENT.format(how=how, var=var, child=child, parent=parent), timeout=420)
            c, p = env_value(child, timeout=30), env_value(parent, timeout=30)
            seen["child_ran"], seen["parent_ran"] = c is not None, p is not None
            seen["parent_has_value"] = bool(p)
            seen["child_equals_parent"] = None if c is None or p is None else c == p
            h.stop()
            if c is not None and p:
                verdicts[name] = "follow" if c == p else "session_follow = false"
        result["selected"] = verdicts
        result["result"] = "pass" if len(verdicts) == len(SUBAGENTS) else "partial" if verdicts else "fail"
    return harness_check("LC-12", list(SUBAGENTS), body)


def lc8():
    """rodeo renders the tab and panes Factory writes: a row root, member titles, the keeper last with
    size.weight 0.4; an unlabelled terminal stays out of the doc; keep as rodeo leaves it."""
    result = {"check": "LC-8", "rodeo": RODEO}
    if not RODEO or not os.path.exists(RODEO):
        result["result"] = "not run: rodeo not found (set RODEO_BIN)"
        return result
    result["rodeo_version"] = subprocess.run([RODEO, "--version"], capture_output=True, text=True,
                                             check=False).stdout.strip()
    with Core("lc8qa") as qa, Core("lc8drv") as drv:
        alice, bob, keeper = Probe(qa, "alpha"), Probe(qa, "beta"), Probe(qa, "kp")
        stray = Probe(qa, "stray")   # no herdr labels: a client must leave it alone
        space, tab = "sp" + "8" * 32, "tb" + "8" * 32
        blocks = [{"id": "p" + "1" * 32, "type": "block", "title": "@alice", "content": {"terminal": alice.address}},
                  {"id": "p" + "2" * 32, "type": "block", "title": "@bob", "content": {"terminal": bob.address}},
                  {"id": "p" + "3" * 32, "type": "block", "title": "keeper", "size": {"weight": 0.4},
                   "content": {"terminal": keeper.address}}]
        doc = {"spaces": [{"id": space, "name": "lc8", "folder": str(qa.dir), "tabs": [
            {"id": tab, "name": "team", "root": {"id": "s" + "8" * 32, "type": "row", "children": blocks}}]}]}
        qa.rpc("doc.set", {"name": "herdr-app", "value": doc, "version": 0})
        for p, name in ((alice, "@alice"), (bob, "@bob"), (keeper, "keeper")):
            qa.rpc("terminal.label", {"terminal": p.address, "labels": {
                "herdr.space": space, "herdr.tab": tab, "herdr.seen": "0", "name": name}})
        rodeo = Rodeo(qa, drv, "unset_env")
        screen = rodeo.wait_for(["team", "@alice", "@bob", "keeper"])
        result["render"] = screen is not None
        if screen is None:
            result["screen"] = rodeo.screen()[-3000:]
            result["result"] = "fail: rodeo did not render the tab"
            rodeo.close()
            return result
        time.sleep(5)   # past a client's adoption delay (about 2 s)
        screen = rodeo.screen()
        result["titles_once"] = {t: screen.count(t) == 1 for t in ("@alice", "@bob")}
        row = next((line for line in screen.splitlines() if "@alice" in line and "keeper" in line), "")
        starts = [row.find(t) for t in ("@alice", "@bob", "keeper")]
        widths = [starts[1] - starts[0], starts[2] - starts[1], len(row.rstrip()) - starts[2]] if row else []
        result["title_row_widths"] = widths
        result["keeper_narrowest"] = bool(widths) and widths[2] < 0.7 * min(widths[0], widths[1])
        value = qa.rpc("doc.get", {"name": "herdr-app"})["value"]
        kept = json.dumps(value, sort_keys=True)
        result["doc_unchanged"] = kept == json.dumps(doc, sort_keys=True)
        result["keeper_weight_in_doc"] = '"weight": 0.4' in kept
        result["stray_in_doc"] = stray.address in kept
        result["keep"] = {name: qa.rpc("terminal.get", {"terminal": p.address}).get("keep")
                          for name, p in (("alice", alice), ("bob", bob), ("keeper", keeper), ("stray", stray))}
        rodeo.close()
        result["result"] = "pass" if (result["render"] and all(result["titles_once"].values())
                                      and result["keeper_narrowest"] and result["keeper_weight_in_doc"]
                                      and not result["stray_in_doc"]) else "fail"
    return result


def rodeo_tabs():
    """check rodeo_tabs (phase_8.md §2): prefix+1..9 selects a tab by its place in the layout doc, and prefix plus
    h/l moves between its panes, both ways, with the same pane title in both tabs; a marker typed after each move
    reaches only the expected terminal."""
    result = {"check": "rodeo_tabs", "rodeo": RODEO}
    if not RODEO or not os.path.exists(RODEO):
        result["result"] = "not run: rodeo not found (set RODEO_BIN)"
        return result
    result["rodeo_version"] = subprocess.run([RODEO, "--version"], capture_output=True, text=True,
                                             check=False).stdout.strip()
    with Core("tabsqa") as qa, Core("tabsdrv") as drv:
        op, dup1, lead, dup2 = Probe(qa, "op"), Probe(qa, "dup1"), Probe(qa, "lead"), Probe(qa, "dup2")
        probes = {"op": op, "dup1": dup1, "lead": lead, "dup2": dup2}
        space = "sp" + "7" * 32

        def block(n, probe, title):
            return {"id": "p" + str(n) * 32, "type": "block", "title": title, "content": {"terminal": probe.address}}
        doc = {"spaces": [{"id": space, "name": "tabs", "folder": str(qa.dir), "tabs": [
            {"id": "tb" + "1" * 32, "name": "operator", "root": {"id": "s" + "1" * 32, "type": "row",
                                                                "children": [block(1, op, "@operator"),
                                                                             block(2, dup1, "@dup")]}},
            {"id": "tb" + "2" * 32, "name": "team", "root": {"id": "s" + "2" * 32, "type": "row",
                                                            "children": [block(3, lead, "@lead"),
                                                                         block(4, dup2, "@dup")]}}]}]}
        qa.rpc("doc.set", {"name": "herdr-app", "value": doc, "version": 0})
        for probe, tab, name in ((op, "1", "@operator"), (dup1, "1", "@dup"), (lead, "2", "@lead"),
                                 (dup2, "2", "@dup")):
            qa.rpc("terminal.label", {"terminal": probe.address, "labels": {
                "herdr.space": space, "herdr.tab": "tb" + tab * 32, "herdr.seen": "0", "name": name}})
        rodeo = Rodeo(qa, drv, "unset_env")
        try:
            if rodeo.wait_for(["operator", "team"]) is None:
                result["result"] = "fail: rodeo did not render both tabs"
                return result
            time.sleep(2)
            steps = ((["2"], "a", "lead"), (["l"], "b", "dup2"), (["1"], "c", "op"), (["l"], "d", "dup1"),
                     (["h"], "e", "op"), (["2"], "f", ("lead", "dup2")), (["h"], "g", "lead"))
            seen, ok = {}, True
            for keys, marker, expected in steps:
                before = {name: len(p.text()) for name, p in probes.items()}
                for key in keys:
                    rodeo.keys("ctrl+b")
                    time.sleep(0.2)
                    rodeo.keys(key)
                    time.sleep(0.6)
                rodeo.keys(marker)
                time.sleep(0.8)
                got = [name for name, p in probes.items() if marker in p.text()[before[name]:]]
                seen[f"prefix+{'+'.join(keys)} then {marker}"] = got
                if isinstance(expected, tuple):          # tab 2 again: whichever of its panes rodeo kept focused
                    ok = ok and len(got) == 1 and got[0] in expected
                elif got != [expected]:
                    ok = False
            result["landed"] = seen
            result["result"] = "pass" if ok else "fail"
        finally:
            rodeo.close()
    return result

def smoke():
    """Exercise the real Factory commands and keeper through rodeo on two private cores."""
    if not RODEO:
        raise RuntimeError("rodeo not found (set RODEO_BIN)")
    here = pathlib.Path(__file__).resolve().parent
    script = here.parent / "skill/scripts/factory.py"
    agent = [sys.executable, str(here / "fake_agent.py"), "reports"]
    lead, reviewer = "@lead-dev", "@reviewer"
    with Core("smokeqa", config="draft_wait = 15\n") as qa, Core("smokedrv") as drv:
        work, bin_ = qa.dir / "repo", qa.dir / "bin"
        codex = qa.dir / "home/.codex"
        for path in (work, bin_, codex):
            path.mkdir()
        (work / "FACTORY.md").write_text("Smoke-test team.\n")
        (work / "factory.toml").write_text(
            '[members.lead-dev]\nharness = "codex"\nmodel = "smoke-lead"\n'
            '[members.reviewer]\nharness = "codex"\nmodel = "smoke-review"\n')
        (codex / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": slug, "supported_reasoning_levels": [{"effort": "high"}, {"effort": "medium"}]}
            for slug in ("smoke-lead", "smoke-review")]}))
        mock = bin_ / "codex"
        mock.write_text("#!/bin/sh\n"
                        'while [ "$#" -gt 1 ] && [ "$1" != "-m" ]; do shift; done\n'
                        f'export FAKE_LOG={shlex.quote(str(qa.dir))}/"$2.jsonl"\n'
                        f"exec {shlex.join(agent)}\n")
        mock.chmod(0o755)
        agent_log = qa.dir / "smoke-lead.jsonl"
        op = qa.rpc("terminal.run", {"argv": agent, "cwd": str(work)})["address"]
        env = {**qa.env, "HC_TERMINAL_ID": op, "HC_BIN": shutil.which(HC) or HC,
               "PATH": f"{bin_}:{qa.env['PATH']}", "XDG_CACHE_HOME": str(qa.dir / "cache"),
               "CODEX_HOME": str(codex)}
        command = [sys.executable, str(script)]

        def call(*args):
            result = subprocess.run([*command, *args], cwd=work, env=env, capture_output=True, text=True,
                                    timeout=35)
            if result.returncode:
                raise RuntimeError(f"{' '.join(args)} exited {result.returncode}\n{result.stdout}{result.stderr}")
            return result.stdout

        def check(ok, label):
            if not ok:
                raise RuntimeError(label)
            print(f"PASS {label}", flush=True)

        def inputs():
            if not agent_log.exists():
                return []
            return [json.loads(line)["text"].splitlines()[-1] for line in agent_log.read_text().splitlines()]

        def until(predicate, timeout=10):
            deadline = now() + timeout
            while now() < deadline:
                if predicate():
                    return True
                time.sleep(0.1)
            return False

        def get(terminal):
            return qa.rpc("terminal.get", {"terminal": terminal})

        check(until(lambda: (get(op).get("status") or {}).get("state") == "idle"), "operator reported idle")
        out = call("up")
        check(all(re.search(rf"{name}: brief \d+ delivered", out) for name in (lead, reviewer)),
              "up delivered both briefs")
        doc = qa.rpc("doc.get", {"name": "herdr-app"})["value"]
        panes = doc["spaces"][0]["tabs"][0]["root"]["children"]
        keeper = panes[-1]["content"]["terminal"]
        check([pane["title"] for pane in panes] == [lead, reviewer, "keeper"]
              and panes[-1]["size"]["weight"] == 0.4, "doc puts the keeper last at weight 0.4")
        rodeo = Rodeo(qa, drv)
        try:
            screen = rodeo.wait_for(["team", lead, reviewer, "keeper"])
            title_row = next((line for line in (screen or "").splitlines() if lead in line and reviewer in line
                              and "keeper" in line), "")
            a, b, k = (title_row.find(title) for title in (lead, reviewer, "keeper"))
            check(0 <= a < b < k and len(title_row.rstrip()) - k < 0.7 * min(b - a, k - b)
                  and title_row.count(lead) == title_row.count(reviewer) == 1,
                  "rodeo showed team and each member title once, with the keeper last and narrowest")
            # Navigate away and back to test that the driver's keys reach the intended pane.
            rodeo.focus(reviewer)
            rodeo.focus(lead)
            rodeo.type("manual input")
            rodeo.keys("enter")
            check(until(lambda: "manual input" in inputs()), "rodeo submitted input to the selected member")

            def keeper_screen():
                return qa.rpc("terminal.read", {"terminal": keeper, "lines": 0})["text"]

            sent = call("say", "lead-dev", "first smoke message")
            ident = re.search(r"^message (\d+)$", sent, re.M)
            check(ident is not None and f"{lead}: delivered" in sent
                  and keeper_screen().count(f"#{ident[1]} @operator -> {lead} delivered") == 1,
                  "say delivered once and the keeper pane recorded it")
            rodeo.type("held draft")
            with subprocess.Popen([*command, "say", "lead-dev", "message after draft"], cwd=work, env=env,
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as sending:
                try:
                    time.sleep(6)
                    check("message after draft" not in inputs() and "held draft" not in inputs()
                          and "sending (held)" in keeper_screen(), "draft held the next say for six seconds")
                    rodeo.keys("enter")
                    out, err = sending.communicate(timeout=15)
                    check(sending.returncode == 0 and f"{lead}: delivered" in out
                          and until(lambda: inputs()[-2:] == ["held draft", "message after draft"]),
                          f"Enter released the draft before the stored message{': ' + err.strip() if err else ''}")
                finally:
                    if sending.poll() is None:
                        sending.kill()
                        sending.communicate()

            tab = doc["spaces"][0]["tabs"][0]["id"]
            call("down")
            check(until(lambda: not any(t["id"] == tab
                                        for space in qa.rpc("doc.get", {"name": "herdr-app"})["value"]["spaces"]
                                        for t in space["tabs"])), "down removed the team tab from the doc")
            check(until(lambda: all(title not in rodeo.screen() for title in (lead, reviewer, "keeper"))),
                  "rodeo removed the team panes")
        finally:
            rodeo.close()


# Fixtures: replies recorded from a real core, scrubbed of host details.

def scrubber(core):
    """Replace host details with placeholders that can never be a real name ("<host>", "<user>")."""
    import getpass
    home = os.path.expanduser("~")
    subs = [(str(core.dir), "/tmp/hc-fixture"), (core.id, "0" * 32), (home, "/home/<user>"),
            (sys.executable, "/usr/bin/python3")]
    host, user = socket.gethostname(), getpass.getuser()
    nix = re.compile(r"/nix/store/[a-z0-9]{32}-[^/\s\"]+")
    names = [(n, f"<{p}>") for n, p in ((host, "host"), (user, "user")) if len(n) > 2]

    def word(name):
        return re.compile(rf"(?<![\w.<-]){re.escape(name)}(?![\w.>-])")

    def identity_placeholder(value, key, parent):
        if (parent, key) == ("core", "name") and isinstance(value, str):
            return "<host>"
        if parent == "login_session" and key in ("id", "user"):
            # In hc 0.1.20 these are the session number and UID, both uint32.
            if type(value) is int:
                return 1
            if isinstance(value, str):
                return "<user>"
        return None

    def scrub(value, key=None, parent=None):
        if isinstance(value, dict):
            return {k: scrub(v, k, key) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, key, parent) for v in value]
        placeholder = identity_placeholder(value, key, parent)
        if placeholder is not None:
            return placeholder
        if isinstance(value, str):
            for old, new in subs:
                value = value.replace(old, new)
            value = nix.sub("/nix/store/HASH-pkg", value)
            if "/" in value:
                for name, mask in names:
                    value = word(name).sub(mask, value)
            return value
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int) and key in ("pid", "uid", "gid"):
            return 1
        if isinstance(value, int) and key and (key == "uptime_ms" or key.endswith("_ms") and value > 10**12):
            return 0
        return value

    def leaks(value, key=None, parent=None):
        """Host details still present after scrubbing, looked for where scrub looks for them."""
        if isinstance(value, dict):
            return [x for k, v in value.items() for x in leaks(v, k, key)]
        if isinstance(value, list):
            return [x for v in value for x in leaks(v, key, parent)]
        placeholder = identity_placeholder(value, key, parent)
        if placeholder is not None:
            return [f"{parent}.{key}"] if value != placeholder else []
        if not isinstance(value, str):
            return []
        found = [n for n in (home, str(core.dir), core.id) if n in value]
        found += ["/nix/store hash"] if nix.search(value) else []
        if "/" in value:
            bare = value.replace("<host>", "").replace("<user>", "")
            found += [n for n, _ in names if word(n).search(bare)]
        return found
    scrub.leaks = leaks
    return scrub


ERROR_KINDS = {"blocked", "conflict", "finished", "invalid_params", "live", "not_found", "spawn_failed",
               "status_mismatch"}
WIRE_EXCEPTIONS = (
    {"version": "0.1.20", "type": "ListResult", "field": "machines",
     "declared": {"required": True}, "observed": "omitted when there are no remote machines",
     "applied": {"required": False}},
)


def schema_extract(replies, version, env):
    """Keep the required keys and JSON types reachable from the recorded success replies."""
    schema = json.loads(subprocess.check_output([HC, "schema"], env=env, timeout=15))
    source = schema["components"]["schemas"]
    methods = {entry["name"]: entry["result"]["schema"] for entry in schema["methods"]}
    types = {}

    def describe(field):
        if field is True:
            return {"type": "any"}
        if "anyOf" in field:
            choices = [choice for choice in field["anyOf"] if choice != {"type": "null"}]
            if len(choices) == 1 and len(field["anyOf"]) == 2:
                return {**describe(choices[0]), "nullable": True}
            raise ValueError(f"unsupported schema union: {field}")
        if "$ref" in field:
            name = field["$ref"].rsplit("/", 1)[1]
            if name not in types:
                types[name] = {}  # WaitResult.children can lead back here.
                required = set(source[name].get("required", []))
                types[name].update({key: {**describe(prop), **({"required": True} if key in required else {})}
                                    for key, prop in source[name]["properties"].items()})
            return {"ref": name}
        kind = field.get("type")
        nullable = isinstance(kind, list) and "null" in kind
        if nullable:
            kind, = (entry for entry in kind if entry != "null")
        if kind not in ("string", "integer", "number", "boolean", "array", "object"):
            raise ValueError(f"unsupported schema field: {field}")
        spec = {"type": kind}
        if nullable:
            spec["nullable"] = True
        if kind == "array":
            spec["items"] = describe(field["items"])
        if kind == "object" and field.get("additionalProperties"):
            spec["values"] = describe(field["additionalProperties"])
        return spec

    results = {method: describe(methods[method])["ref"] for method in sorted({r["method"] for r in replies.values()})}
    exceptions = [rule for rule in WIRE_EXCEPTIONS if rule["version"] == version]
    for rule in exceptions:
        field = types[rule["type"]][rule["field"]]
        if any(field.get(key) != value for key, value in rule["declared"].items()):
            raise ValueError(f"remove obsolete wire exception: {rule['type']}.{rule['field']}")
        field.update(rule["applied"])
    return {"results": results, "types": types}, exceptions


def fixtures(out):
    replies, errors, problems = {}, {}, []
    with Core("fix", config="draft_wait = 0\n", extra_env={"HC_SETTLE_MS": "0"}) as qa:
        def rec(name, method, params=None, timeout=15):
            """Record one call. A case named after an error kind must fail with that kind; any other must succeed."""
            msg = qa.call(method, params, timeout)
            entry = {"method": method, "params": params or {}}
            if name in ERROR_KINDS:
                kind = ((msg.get("error") or {}).get("data") or {}).get("kind")
                if kind != name:
                    problems.append(f"{name}: expected error kind {name}, got {kind or 'a result'}")
                entry["error"] = msg.get("error")
                errors[name] = entry
            else:
                if "error" in msg:
                    problems.append(f"{name}: expected a result, got error {msg['error']}")
                entry["result"] = msg.get("result")
                replies[name] = entry
            return msg.get("result")

        replies["hello"] = {"method": "hello", "params": {"generation": 1, "framed": False},
                            "result": qa.hello()}
        rec("server.status", "server.status")
        agent = Probe(qa, "agent", labels={"factory.team": "fix", "factory.kind": "member"})
        replies["terminal.run"] = {"method": "terminal.run", "params": agent.params, "result": agent.record}
        plain = rec("terminal.run.plain", "terminal.run", {"argv": ["sleep", "300"], "cwd": str(qa.dir),
                                                            "labels": {"factory.team": "fix"}})
        rec("terminal.get.live", "terminal.get", {"terminal": agent.address})
        rec("terminal.send.agent", "terminal.send", {"terminal": agent.address, "text": "hello agent"}, timeout=30)
        rec("terminal.send.plain", "terminal.send", {"terminal": plain["address"], "text": "hello plain"}, timeout=30)
        rec("status_mismatch", "terminal.send", {"terminal": agent.address, "text": "x", "if": "working"})
        rec("terminal.keys", "terminal.keys", {"terminal": agent.address, "keys": ["a", "enter"]})
        rec("terminal.read", "terminal.read", {"terminal": agent.address, "lines": 0})
        rec("terminal.label.set", "terminal.label", {"terminal": agent.address, "labels": {"factory.ready": "1"}})
        rec("terminal.label.remove", "terminal.label", {"terminal": agent.address, "labels": {"factory.ready": ""}})
        rec("terminal.keep.on", "terminal.keep", {"terminal": agent.address, "keep": True})
        rec("terminal.keep.off", "terminal.keep", {"terminal": agent.address, "keep": False})
        blocked = Probe(qa, "blocked", fields="state=blocked:kind=permission:msg=:app=probe")
        rec("blocked", "terminal.send", {"terminal": blocked.address, "text": "x"})
        rec("not_found", "terminal.send", {"terminal": "t999", "text": "x"})
        rec("invalid_params", "terminal.send", {"terminal": agent.address})
        rec("spawn_failed", "terminal.run", {"argv": ["/nonexistent/factory-no-such-program"], "cwd": str(qa.dir)})
        rec("live", "terminal.rm", {"terminal": agent.address})
        done = qa.rpc("terminal.run", {"argv": ["true"], "cwd": str(qa.dir), "labels": {"factory.team": "fix"}})
        deadline = time.monotonic() + 10
        while qa.rpc("terminal.get", {"terminal": done["address"]})["state"] == "live" and time.monotonic() < deadline:
            time.sleep(0.1)
        rec("terminal.get.finished", "terminal.get", {"terminal": done["address"]})
        rec("finished", "terminal.send", {"terminal": done["address"], "text": "x"})
        rec("terminal.label.finished", "terminal.label", {"terminal": done["address"], "labels": {"factory.noted": "exit"}})
        rec("terminal.list.all", "terminal.list", {"all": True, "labels": {"factory.team": "fix"}})
        rec("terminal.list.live", "terminal.list", {"labels": {"factory.team": "fix"}})
        rec("terminal.kill", "terminal.kill", {"terminal": blocked.address})
        rec("terminal.rm", "terminal.rm", {"terminal": blocked.address})
        rec("doc.get.empty", "doc.get", {"name": "herdr-app"})
        doc = {"spaces": [{"id": "sp" + "a" * 32, "name": "", "folder": str(qa.dir), "tabs": [
            {"id": "tb" + "a" * 32, "name": "team", "root": {"id": "s" + "a" * 32, "type": "row", "children": [
                {"id": "p" + "a" * 32, "type": "block", "title": "@agent", "content": {"terminal": agent.address}}]}}]}]}
        rec("doc.set", "doc.set", {"name": "herdr-app", "value": doc, "version": 0})
        rec("doc.get.present", "doc.get", {"name": "herdr-app"})
        rec("conflict", "doc.set", {"name": "herdr-app", "value": doc, "version": 0})
        # A record after the core stops and starts again: reason server_stop, labels kept.
        stop = qa.server_stop()
        if stop is not None and "result" in stop:
            replies["server.stop"] = {"method": "server.stop", "params": {}, "result": stop["result"]}
        else:
            problems.append(f"server.stop: expected a result, got {stop}")
        time.sleep(0.5)
        qa.start()
        after = rec("terminal.get.after_restart", "terminal.get", {"terminal": plain["address"]}) or {}
        if (after.get("state"), after.get("reason"), (after.get("labels") or {}).get("factory.team")) != (
                "finished", "server_stop", "fix"):
            problems.append(f"terminal.get.after_restart: expected finished, reason server_stop, labels kept; got "
                            f"{after.get('state')}, {after.get('reason')}, {after.get('labels')}")
        rec("terminal.list.after_restart", "terminal.list", {"all": True, "labels": {"factory.team": "fix"}})
        scrub = scrubber(qa)
        version = replies["hello"]["result"]["version"]
        schema, exceptions = schema_extract(replies, version, qa.env)
    missing = ERROR_KINDS - set(errors)
    if problems or missing:
        raise RuntimeError(f"fixtures not written: {problems + [f'{k}: not recorded' for k in sorted(missing)]}")
    leaks = sorted(set(scrub.leaks([scrub(replies), scrub(errors)])))
    if leaks:
        raise RuntimeError(f"fixtures still contain host details: {leaks}")
    data = {"hc_version": version, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "Recorded by tests/live.py fixtures from a private core; host details replaced by placeholders.",
            "replies": scrub(replies), "errors": scrub(errors), "schema": schema, "wire_exceptions": exceptions}
    pathlib.Path(out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return {"check": "fixtures", "out": str(out), "hc": version, "replies": sorted(replies), "errors": sorted(errors)}


CHECKS = {"lc1": lc1, "lc10": lc10, "lc13": lc13, "lc8": lc8, "rodeo_tabs": rodeo_tabs, "rodeo_focus": rodeo_focus}
LOGIN_CHECKS = {"lc2": lc2, "lc3": lc3, "lc4": lc4, "lc6": lc6, "lc7": lc7, "lc12": lc12}   # start real harnesses


QA_PREFIX = "factory-qa-"   # every scratch project's name begins so: the owner can find and remove them
PEERS = {"claude": "codex", "codex": "claude"}


def _peer(role, harness):
    """The counterpart: a named member for an operator run, a Claude Code or Codex operator for a member run."""
    return PEERS.get(harness, "codex") if role == "operator" else ("claude" if harness == "codex" else "codex")


class FixtureAdapter:
    """Fake agents in place of every harness: the same case code as a real run, never a qualification."""

    mode = "fixture"

    def __init__(self, team):
        self.team = team
        self.agent = [sys.executable, str(ROOT / "tests" / "fake_agent.py"), "reports"]

    def version(self, harness):
        return "fake_agent"

    def _agent(self, harness):
        """The fake's command line: for the harness named in startup_dialog, one that first shows that harness's
        declared Enter-answered dialog, as a fresh start of it would."""
        dialog = next((text for text, keys in harness_table().get(harness, {}).get("dialogs", []) if keys == ["enter"]),
                      None) if getattr(self.team, "startup_dialog", None) == harness else None
        if dialog is None:
            return shlex.join(self.agent)
        return (f"env FAKE_DIALOG={shlex.quote(dialog)} FAKE_DIALOG_KEYS= FAKE_DIALOG_STATE=none "
                + shlex.join([*self.agent[:-1], "dialog"]))

    def model(self, harness):
        return f"fixture-{harness}"

    def prepare(self, harnesses):
        """A wrapper per harness that runs the fake agent with a log named after the member's model."""
        memory = self.team.dir / "memory"
        memory.mkdir(exist_ok=True)
        for harness in harnesses:   # a conversation per launch, or the one "resume <id>" names (Codex's argv)
            wrapper = self.team.bin / harness
            agent = self._agent(harness)
            wrapper.write_text("#!/bin/sh\n"
                               'id="thread-$(od -An -N4 -tx1 /dev/urandom | tr -d \' \\n\')"\n'
                               'while [ "$#" -gt 0 ]; do [ "$1" = resume ] && id="$2"; shift; done\n'
                               f'export CODEX_THREAD_ID="$id" FAKE_MEMORY_DIR={shlex.quote(str(memory))} FAKE_ECHO=1\n'
                               f"exec {agent}\n")
            wrapper.chmod(0o755)
        codex = self.team.home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": self.model("codex"), "supported_reasoning_levels": [{"effort": e} for e in
                                                                     ("low", "medium", "high", "xhigh")]}]}))

    def operator_argv(self, harness):
        """The fake operator in a loop that waits for Enter between runs: quitting it leaves its pane open."""
        return ["sh", "-c", f"while :; do {self._agent(harness)}; stty sane; read line; done"]

    def act_append(self, path, token):
        return f"ACT append {path} {token}"

    def act_run(self, path, command):
        return f"ACT run {path} {command}"

    def act_alone(self, command):
        """The fake runs it as ACT run does (it has no sandbox); its receipt is thrown away, and its output is kept
        where alone_output finds it (a real harness's is in its own transcript)."""
        out = self.team.dir / f"alone-{hashlib.sha1(command.encode()).hexdigest()[:12]}.txt"
        return f"ACT run {self.team.dir / 'alone-receipt.txt'} {command} > {out} 2>&1"

    def act_alone_each(self, commands):
        return "\n".join(self.act_alone(c) for c in commands)

    def alone_output(self, command, others=(), prompt=None):
        out = self.team.dir / f"alone-{hashlib.sha1(command.encode()).hexdigest()[:12]}.txt"
        return out.read_text() if out.exists() else None

    def blocked_request(self):
        return "FAKE: blocks_once"

    def draft_request(self, path, literal):
        """A one-line owner draft that rodeo can type key by key (letters, digits, spaces and ,.-/')."""
        return f"ACT append {path} {literal}"

    def remember(self, word):
        return f"ACT remember {word}"

    def long_task(self, path, started, release, finished):
        wait = f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.2; done"
        return f"ACT append {path} {started}\nACT run {path}.run {wait}\nACT append {path} {finished}"

    def interrupt_keys(self, harness):
        return ["esc"]

    def recall(self, path):
        return f"ACT recall {path}"

    def switch_request(self, harness):
        return "ACT switch"

    def quit(self, qa, terminal):
        """The fake operator quits to its loop on Ctrl+D, a key like a real harness's quit (no send, so a held
        draft does not delay it), reporting state=clear as a harness returning to its shell."""
        qa.rpc("terminal.keys", {"terminal": terminal, "keys": ["ctrl+d"]})

    def restart(self, qa, terminal):
        """Start the fake operator again in its open pane (its loop waits for Enter)."""
        qa.rpc("terminal.keys", {"terminal": terminal, "keys": ["enter"]})
        return True


class RealAdapter(FixtureAdapter):
    """Real harnesses with the owner's own login, started normally (@lead-dev-3's runs; phase_8.md §2)."""

    mode = "real"

    def version(self, harness):
        return harness_version(harness)

    def model(self, harness):
        override = os.environ.get(f"FACTORY_QA_MODEL_{harness.upper()}")
        return override or (harness_table()[harness].get("default_model") or harness_table()[harness]["models"][0])

    def prepare(self, harnesses):
        missing = [h for h in harnesses if shutil.which(h) is None]
        if missing:
            raise NotRun(f"{', '.join(missing)} not on PATH")

    def operator_argv(self, harness, model=None):
        """The operator as the owner would start it, with the profile's model pinned (as Factory pins a member's)."""
        return [shutil.which(harness), *skills_off(harness, self.team.qa.installed, self.team.skills_exempt), "--model",
                model or self.model(harness), *harness_table()[harness]["permissions"]["full"]]

    def act_append(self, path, token):
        return (f"Using your shell tool, append one line containing exactly {token} to the file {path}"
                " (create it if needed). Then reply with the single word done.")

    def act_run(self, path, command):
        return (f"Using your shell tool, run exactly this command: {command}; echo \"exit $?\" >> {path}"
                " Then reply with the single word done.")

    def act_alone(self, command):
        """One command in its own tool call, nothing added: how a member runs say (a guarded Claude Code's
        sandbox exempts exactly 'python3 <script> say *', which a compound line does not match; case 9 at
        3321bfc)."""
        return (f"Using your shell tool, run exactly this one command, by itself in its own tool call, with nothing"
                f" added before or after it: {command} Then reply with the single word done.")

    def act_alone_each(self, commands):
        listed = " ".join(f"({i}) {c}" for i, c in enumerate(commands, 1))
        return (f"Using your shell tool, run each of these {len(commands)} commands in order, each by itself in its own"
                f" tool call, exactly as written, with nothing added before or after it: {listed} Then reply with"
                " the single word done.")

    def alone_output(self, command, others=(), prompt=None):
        """What the command printed, from an actual tool call in the operator's own transcript of this run (Claude
        Code and Codex; the one holding the owner's prompt): the result paired with the call that ran this command
        and none of the others. None when no such call and result exist, so the command stays unproved: no screen,
        prose or prompt text counts (@architect-2)."""
        found = None
        for path in qa_transcripts([self.team.dir]):
            text = path.read_text(errors="replace")
            if prompt and prompt not in text and json.dumps(prompt)[1:-1] not in text:
                continue                                  # another conversation (a member's)
            for call, output in tool_calls(text):
                if command in call and not any(o in call for o in others) and output is not None:
                    found = output
        return found

    def blocked_request(self):
        return ("Before doing anything else, ask me one multiple-choice question with your question tool, and"
                " wait for my answer. Do nothing else.")

    def draft_request(self, path, literal):
        return (f"Append one line containing exactly {literal} to the file {path} with your shell tool,"
                " then reply done.")

    def remember(self, word):
        return REMEMBER.format(token=word)

    def long_task(self, path, started, release, finished):
        return (f"Using your shell tool, do these three steps in order. 1: append one line containing exactly {started}"
                f" to the file {path}. 2: run exactly this command and wait for it to end, however long it takes:"
                f" while [ ! -f {release} ]; do sleep 1; done  3: append one line containing exactly {finished} to"
                f" {path}. Then reply with the single word done.")

    def interrupt_keys(self, harness):
        return ["esc"]

    def recall(self, path):
        return ("Append the code word I gave you earlier in this conversation, spelled backwards in capital letters,"
                f" as one line to the file {path} using your shell tool; append NONE if I gave you none. Then reply"
                " done.")

    def switch_request(self, harness):
        command = {"claude": "/clear", "codex": "/new", "opencode": "/new", "pi": "/new",
                   "omp": "/new"}.get(harness)
        if command is None:
            raise NotRun(f"no known way to start a new conversation in {harness}")
        return command

    def quit(self, qa, terminal):
        """Quit the harness as its owner would, Ctrl+C twice, back to the shell (hc then clears the status); its own
        quit command if it is still reporting."""
        if not self.team.operator_command:
            raise NotRun("this team's operator was not started from a shell")
        for _ in range(2):                  # inside the harness's "press again to exit" window
            qa.rpc("terminal.keys", {"terminal": terminal, "keys": ["ctrl+c"]})
            time.sleep(0.3)
        if not self.team.until(lambda: self.team.state(terminal) is None, 15):
            qa.rpc("terminal.send", {"terminal": terminal, "text": {"claude": "/exit"}.get(self.team.operator_harness,
                                                                                         "/quit")}, timeout=60)

    def quit_keys_only(self, qa, terminal, stop=lambda: False):
        """The owner's quit, Ctrl+C twice 0.3 s apart (@lead-dev-3's probe: Claude Code and Codex exit only when the
        second press comes inside their "press again" window, under 1 s). The first press also clears any draft.
        No typed /exit, which a held send could merge with; the second press is skipped once stop() is true."""
        for _ in range(2):
            if stop():
                return
            qa.rpc("terminal.keys", {"terminal": terminal, "keys": ["ctrl+c"]})
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline and not stop():
                time.sleep(0.02)

    def restart(self, qa, terminal):
        """Type the same command at the shell prompt in that pane, once the prompt is really back: a harness clears
        its status before it has finished exiting, and keys typed then go to the dying harness (claude-op-8 at
        7a90602: the restart was lost, the next say queued gone). False when the prompt never came back."""
        if not self.team.operator_command:
            raise NotRun("this team's operator was not started from a shell")
        if not self.team.until(lambda: shell_prompt(qa, terminal), 60):
            return False
        self.team.send(terminal, self.team.operator_command)
        Harness.attach(qa, self.team.operator_harness, terminal).settle(timeout=120)
        return True


class QATeam:
    """A scratch project, a private QA core, a team with an operator terminal, and Factory's real commands from a
    private copy of the skill (phase_8.md §2)."""

    def __init__(self, adapter_class, operator, members, draft_wait=None, permissions="full", operator_shell=False,
                 startup_dialog=None, quiet_grace=None, setup=None, operator_model=None, operator_sink=False,
                 skills_exempt=()):
        self.operator_harness, self.members, self.permissions = operator, members, permissions
        self.skills_exempt = tuple(skills_exempt)   # harnesses started with their commands on (case 6's /clear)
        self.startup_dialog = startup_dialog    # fixture only: the harness whose fake shows its declared dialog
        # agy on the real HOME (@operator): its settings files' size and mtime before and after, never contents
        self.agy_roots = ([os.path.expanduser(p) for p in AGY_SETTINGS]
                          if adapter_class is RealAdapter and "agy" in {operator, *members.values()} else [])
        self.agy_before = stamps(self.agy_roots) if self.agy_roots else {}
        self.real = adapter_class is RealAdapter
        home = os.path.expanduser("~")
        extra = {"HOME": home} if self.real else None   # a real harness needs the owner's login (testing.md §6)
        # a real harness: the protected core (the owner's rule: no QA terminal may find or run an installed Factory)
        self.qa = Core("qualify", config=f"draft_wait = {draft_wait}\n" if draft_wait is not None else "",
                       extra_env=extra, protect=self.real)
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix=QA_PREFIX, dir=tempfile.gettempdir()))
        self.bin, self.home = self.dir / "bin", (pathlib.Path(home) if self.real else self.dir / "home")
        self.bin.mkdir()
        stub = self.bin / "herdr"          # first on every QA PATH: no QA agent reaches the owner's herdr
        stub.write_text(HERDR_STUB)
        stub.chmod(0o755)
        self.skill = self.dir / "skill"
        shutil.copytree(ROOT / "skill", self.skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.script = self.skill / "scripts" / "factory.py"
        self.adapter = adapter_class(self)
        try:
            self.adapter.prepare({operator, *members.values()})
            self.work = setup(self) if setup is not None else self._qualification_workspace(quiet_grace)
        except BaseException:
            try:
                self.qa.stop()
            finally:
                shutil.rmtree(self.dir, ignore_errors=True)
            raise
        # What Factory's commands see, from the driver or from the operator's own tool: the private Factory cache
        # (never the owner's), the fixture's wrappers first on PATH, and the fake Codex home.
        shared = {"HC_BIN": shutil.which(HC) or HC, "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
                  "XDG_CACHE_HOME": str(self.dir / "cache"), "HOME": str(self.home)}
        if not self.real:
            shared["CODEX_HOME"] = str(self.home / ".codex")
        argv = (self.adapter.operator_argv(operator, operator_model) if self.real
                else self.adapter.operator_argv(operator))
        params = {"argv": argv, "cwd": str(self.work), "env": dict(shared)}
        if not self.real:
            params["env"]["FAKE_LOG"] = str(self.dir / "operator-input.jsonl")   # every input the fake received
            params["env"]["FAKE_ECHO"] = "1"
            if harness_table().get(operator, {}).get("operator_deliver"):
                params["env"]["FAKE_APP"] = operator      # reported as the real one is, so Factory's rule applies
            params["env"]["CODEX_THREAD_ID"] = f"thread-{token().lower()}"   # the fake operator's conversation
        self.operator_command = None
        self.sink = None
        if self.real and operator_shell:    # an owner's shell: quitting the harness leaves the pane at its prompt
            self.operator_command = shlex.join(params["argv"])
            if operator_sink:                # or, for a quit trial, a sink that executes nothing (phase_8.md §3.8)
                self.sink = self.dir / "operator-sink.txt"
                self.operator_command += f"; exec cat > {shlex.quote(str(self.sink))}"
            params["argv"] = ["bash", "--norc", "--noprofile", "-i"]
            params["env"]["PS1"] = "qa$ "
        self.operator = self.qa.rpc("terminal.run", params)["address"]
        if self.operator_command:
            self.send(self.operator, self.operator_command)
        self.env = {**self.qa.env, **shared, "HC_TERMINAL_ID": self.operator}

    def _qualification_workspace(self, quiet_grace):
        work = self.dir / "work"
        work.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        (work / "FACTORY.md").write_text(
            "Qualification team: answer every request exactly as asked.\n"
            f"Factory's only command for this team is python3 {self.script}. Never run another copy of Factory"
            " (an installed skill's script) or herdr.\n")
        lines = [f'[operator]\nharness = "{self.operator_harness}"\n']
        if quiet_grace:
            lines.insert(0, f"quiet_grace = {quiet_grace}\n")
        for name, harness in self.members.items():
            off = skills_off(harness, self.qa.installed, self.skills_exempt) if self.real else []
            lines.append(f'[members.{name}]\nharness = "{harness}"\nmodel = "{self.adapter.model(harness)}"\n'
                         f'permissions = "{self.permissions}"\n' + (f"args = {json.dumps(off)}\n" if off else ""))
        (work / "factory.toml").write_text("\n".join(lines))
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work), "-c", "user.name=qa", "-c", "user.email=qa@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "qa"], check=True)
        return work

    def call(self, *args, terminal=None, timeout=60):
        """A Factory command as the operator (or as terminal): (exit code, output)."""
        env = dict(self.env, **({"HC_TERMINAL_ID": terminal} if terminal else {}))
        done = subprocess.run([sys.executable, str(self.script), *args], cwd=self.work, env=env,
                              capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, done.stdout + done.stderr

    def get(self, terminal):
        return self.qa.rpc("terminal.get", {"terminal": terminal})

    def state(self, terminal):
        return (self.get(terminal).get("status") or {}).get("state")

    def member(self, name):
        for record in self.qa.rpc("terminal.list", {"all": False})["terminals"]:
            labels = record.get("labels") or {}
            if labels.get("factory.member") == name and labels.get("factory.kind") == "member":
                return record["address"]
        return None

    def keeper(self):
        for record in self.qa.rpc("terminal.list", {"all": False})["terminals"]:
            if (record.get("labels") or {}).get("factory.kind") == "keeper":
                return record["address"]
        return None

    def until(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.5)
        return None

    def send(self, terminal, text, timeout=180):
        """terminal.send, waiting out hc's refusal while a fresh terminal's output has not settled (a real harness
        still drawing its startup screen); any other error, or the refusal past the timeout, is raised."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.qa.rpc("terminal.send", {"terminal": terminal, "text": text}, timeout=60)
            except HcError as error:
                if "still starting" not in str(error) or time.monotonic() >= deadline:
                    raise
            time.sleep(1)

    def harness_of(self, record):
        """The harness running in one of this team's terminals, from its Factory labels."""
        labels = record.get("labels") or {}
        if labels.get("factory.kind") in ("operator", "operator-lost"):
            return self.operator_harness
        return self.members.get(labels.get("factory.member")) if labels.get("factory.member") else None

    def receipt(self, name):
        """A file a harness writes for the driver: in its work directory, where a sandboxed shell may write (the
        owner's Claude Code sandbox applies at full too; case 6 at 14ed74a), left out of git status."""
        return self.scratch() / f"receipt-{name}-{random.randrange(16 ** 6):06x}.txt"

    def scratch(self):
        folder = self.work / ".factory-qa"
        if not folder.exists():
            folder.mkdir()
            exclude = self.work / ".git" / "info" / "exclude"
            if exclude.parent.is_dir():
                with exclude.open("a") as stream:
                    stream.write(".factory-qa/\n")
        return folder

    def close(self):
        try:
            failed, observed = split_seen(self.qa.installed_seen([self.dir], self.harness_of), self.skills_exempt)
            EVIDENCE.setdefault("installed_factory_seen", []).extend(failed)
            EVIDENCE.setdefault("installed_factory_observed", []).extend(observed)
        except (OSError, HcError, HcDown) as error:
            EVIDENCE.setdefault("installed_factory_seen", []).append(f"the check could not run: {error}"[:BOUND])
        if self.agy_roots:
            home = os.path.expanduser("~")
            touched = [p for p in changed(self.agy_before, stamps(self.agy_roots))
                       if pathlib.Path(p).name == "settings.json" or "trust" in pathlib.Path(p).name.lower()]
            EVIDENCE.setdefault("settings_changed", []).extend(p.replace(home, "~", 1) for p in touched)
        try:
            EVIDENCE["teams"].append(self.diagnostics())
        except (OSError, HcError, HcDown, RuntimeError, ValueError) as error:
            EVIDENCE["teams"].append({"unavailable": f"{type(error).__name__}: {error}"[:BOUND]})
        if getattr(self, "view", None) is not None:
            self.view.close()
            self.drv.stop()
        self.qa.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def diagnostics(self):
        """Bounded facts about this team before it goes (phase_8.md §2, @architect-2's decision): every message's
        ID, kind, sender, recipients and its outcome records in log order, and each terminal's stage, state, turn
        and which of the case's test markers its screen shows."""
        facts = {"messages": [], "terminals": []}
        state = next((r["labels"]["factory.dir"] for r in self.qa.rpc("terminal.list", {"all": True})["terminals"]
                      if (r.get("labels") or {}).get("factory.dir")), None)
        if state is not None:
            records = factory_log(self).read_box()[0]
            sent = {}
            for rec in records:
                if rec["t"] == "msg":
                    sent[rec["id"]] = {"id": rec["id"], "kind": rec.get("kind"), "from": rec.get("from"),
                                       "to": rec.get("to"), "outcomes": []}
                elif rec["t"] == "out" and rec["id"] in sent:
                    sent[rec["id"]]["outcomes"].append([rec["to"], rec["o"], rec.get("why")])
            facts["messages"] = list(sent.values())[-40:]
        for record in self.qa.rpc("terminal.list", {"all": True})["terminals"]:
            labels = record.get("labels") or {}
            entry = {"address": record["address"], "kind": labels.get("factory.kind"),
                     "member": labels.get("factory.member"), "stage": record.get("state"),
                     "state": (record.get("status") or {}).get("state"), "turn": record.get("turn")}
            if record.get("state") != "finished":
                screen = "".join(self.qa.rpc("terminal.read", {"terminal": record["address"], "lines": 0})
                                 .get("text", "").split())
                entry["markers_on_screen"] = [t for t in EVIDENCE["tokens"] if t in screen]
            facts["terminals"].append(entry)
        return facts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# Qualification (phase_8.md): functional_spec §10's ten cases over eight role profiles, the four cross cases once.
# One JSON object on stdout (progress on stderr), checkpointed to --json after each case.

# The current case's closed teams' diagnostics and test markers, and this run's rodeo_tabs probe.
EVIDENCE = {"teams": [], "tokens": [], "rodeo_tabs": None}
QUALIFY_HARNESSES = ("claude", "codex", "pi", "omp", "opencode", "agy")
OPERATOR_HARNESSES = ("claude", "codex")
CROSS_CASES = ("two-teams", "multi-down", "owner-pane", "other-version")
STATUSES = ("pass", "fail", "not_run", "measured")
CASES = {   # case -> its subcases: (name, roles it is required for, measurement only)
    1: (("states", "both", False), ("startup-dialog", "both", False)),
    2: (("idle-report", "both", False), ("operator-commands", "operator", False)),
    3: (("draft", "both", False), ("draft-cleared", "operator", False)),
    4: (("mid-turn", "both", False), ("owner-interrupt", "both", False)),
    5: (("blocked", "both", False),),
    6: (("resume", "member", False), ("member-switch", "member", False), ("operator-switch", "operator", False)),
    7: (("suffix", "both", False), ("multiline", "both", False), ("two-senders", "both", False),
        ("pending-restart", "both", False), ("held-quiet", "both", False), ("upgrade", "both", False)),
    8: (("dead-member", "member", False), ("operator-gone", "operator", False), ("same-pane", "operator", False),
        ("unconfirmed-brief", "member", False), ("quit-during-hold", "both", True)),
    9: (("guarded", "member", False),),
    10: (("idle-latency", "both", False), ("idle-latency-times", "both", True), ("held-sender-exit", "both", False),
         ("crash", "both", False)),
}
BOUND = 300   # characters kept of any observed or expected text
READY = ("idle", "done")       # hc's two waiting states: done is idle with a turn the owner has not seen


def subcases(case, role):
    """[(subcase, measurement only)] that a profile in role must cover for case."""
    return [(name, measured) for name, roles, measured in CASES[case] if roles in ("both", role)]


def select_profiles(harness, role):
    """The (role, harness) profiles a qualify run covers; ValueError for an out-of-scope choice."""
    if harness not in QUALIFY_HARNESSES:
        raise ValueError(f"{harness} is not one of {', '.join(QUALIFY_HARNESSES)}")
    roles = ("operator", "member") if harness in OPERATOR_HARNESSES else ("member",)
    if role is not None and role not in roles:
        raise ValueError(f"{harness} is qualified as {' and '.join(roles)} in this phase, not {role}")
    return [(r, harness) for r in ((role,) if role else roles)]


def _bounded(value):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text if len(text) <= BOUND else text[:BOUND] + "…"


class Qualification:
    """One run's evidence: versions, profiles and rows, checkpointed after each case."""

    def __init__(self, mode, path=None, versions=None):
        if mode not in ("real", "fixture", "cross"):
            raise ValueError(f"unknown mode {mode}")
        self.path = pathlib.Path(path) if path else None
        self.data = {"run_id": f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}",
                     "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": mode,
                     **(versions or {}), "profiles": [], "rows": []}

    def profile(self, role, harness, **facts):
        self.data["profiles"].append({"role": role, "harness": harness, **facts})

    def row(self, case, subcase, role, harness, status, expected, observed=None, proof=None, elapsed=None,
            reason=None):
        if status not in STATUSES:
            raise ValueError(f"unknown status {status}")
        if status in ("fail", "not_run") and not reason:
            raise ValueError(f"a {status} row needs a reason")
        row = {"case": case, "subcase": subcase, "role": role, "harness": harness, "status": status,
               "expected": _bounded(expected), "observed": _bounded(observed), "proof": proof}
        if elapsed is not None:
            row["elapsed"] = round(elapsed, 2)
        if reason:
            row["reason"] = _bounded(reason)
        self.data["rows"].append(row)
        self.checkpoint()                 # on disk as soon as judged: a later crash cannot lose it (@operator)
        return row

    def checkpoint(self):
        """Atomically replace --json with the evidence so far, so a later stop keeps every finished case."""
        if self.path is None:
            return
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(temporary, self.path)

    def case_status(self, role, harness, case):
        """fail if a required subcase failed, else not_run if one was not run or is missing, else pass.
        Measurement-only subcases never decide it."""
        rows = {r["subcase"]: r for r in self.data["rows"]
                if (r["role"], r["harness"], r["case"]) == (role, harness, case)}
        required = [name for name, measured in subcases(case, role) if not measured]
        statuses = [rows[name]["status"] if name in rows else "not_run" for name in required]
        return "fail" if "fail" in statuses else "not_run" if "not_run" in statuses else "pass"

    def summary(self, selected):
        """{"role harness": {case: status}} for the selected (role, harness, case) triples."""
        out = {}
        for role, harness, case in selected:
            out.setdefault(f"{role} {harness}", {})[case] = self.case_status(role, harness, case)
        return out

    def exit_code(self, selected):
        statuses = [self.case_status(*triple) for triple in selected]
        return 1 if "fail" in statuses else 2 if "not_run" in statuses else 0


def _team_for(adapter_class, role, harness, peer=None, **options):
    """A two-member-or-less team where the profile's harness has its role (phase_8.md §1); peer overrides the
    default counterpart."""
    peer = peer or _peer(role, harness)
    operator, member = (harness, peer) if role == "operator" else (peer, harness)
    if adapter_class is FixtureAdapter:
        member = "codex"           # fake agents stand in; the formation needs a harness Factory can check offline
    return QATeam(adapter_class, operator, {"lead-dev": member}, **options), peer


def _up(team, run, case, role, harness, names):
    """Bring the team up; on failure every listed subcase is not_run with up's output."""
    if not _operator_start(team, run, case, role, harness, names):
        return False
    code, out = team.call("up", timeout=240)
    if code != 0:
        for name in names:
            run.row(case, name, role, harness, "not_run", f"case {case} {name}", observed=out[-BOUND:],
                    reason=f"up exited {code}")
        return False
    if not team.until(lambda: team.state(team.operator) in READY, 120):
        for name in names:
            run.row(case, name, role, harness, "not_run", f"case {case} {name}", reason="the operator never idled")
        return False
    waiting = [name for name in team.members                 # members past their startup before any case sends
               if not team.until(lambda: team.member(name) and team.state(team.member(name)) in READY, 180)]
    quiet = _team_quiet(team, hold=8, timeout=600)    # the startup reports handled: a real operator's turn on a
    # ready report can run for minutes (a Codex operator stayed working over 300 s in OpenCode's batch)
    team.ready_states = {"operator": team.state(team.operator), "quiet_after_up": quiet,
                         **{name: team.state(team.member(name)) for name in team.members if team.member(name)}}
    if quiet is None:
        waiting = waiting or ["the team (not quiet for 8 s within 600 s)"]
    if waiting:
        for name in names:
            run.row(case, name, role, harness, "not_run", f"case {case} {name}",
                    reason=f"never idle after up: {', '.join(waiting)}")
        return False
    return True


def shell_prompt(qa, terminal):
    """The operator pane's own shell shows its prompt (QATeam's PS1) as the last line: the harness has gone."""
    lines = [line for line in qa.rpc("terminal.read", {"terminal": terminal, "lines": 0}).get("text", "").splitlines()
             if line.strip()]
    return bool(lines) and lines[-1].rstrip() == "qa$"


def _team_quiet(team, hold, timeout):
    """Wait until the operator and every member have been ready (idle or done) for hold seconds together, so
    the members' ready reports and the operator's turns on them are over. Returns the seconds it took, or None."""
    start = time.monotonic()
    since = None
    while time.monotonic() - start < timeout:
        terminals = [team.operator, *(team.member(name) for name in team.members)]
        if all(t and team.state(t) in READY for t in terminals):
            since = since or time.monotonic()
            if time.monotonic() - since >= hold:
                return round(time.monotonic() - start, 1)
        else:
            since = None
        time.sleep(0.5)
    return None


TRUST_FILES = {"claude": "~/.claude.json", "codex": "~/.codex/config.toml"}


def _trust_entries(run, team, directories=None):
    """The per-directory trust a real Claude Code or Codex start may add for this run's scratch project, listed
    for the owner to remove (@operator's decision; phase_8.md §2)."""
    entries = run.data.setdefault("trust_entries", [])
    profiles = {"operator": team.operator_harness, **team.members}
    for name, harness in profiles.items():
        if harness in TRUST_FILES:
            directory = directories.get(name, team.work) if directories is not None else team.work
            entry = {"harness": harness, "file": TRUST_FILES[harness], "directory": str(directory)}
            if entry not in entries:
                entries.append(entry)
    run.checkpoint()


def _member_says(team, text, timeout=300):
    """Have the member itself run say to the operator through its own tool, with text in a file, by itself in its
    own tool call as its standing instructions say: a sandbox exempts exactly 'python3 <script> say *', which the
    earlier compound line (redirect, '; echo') did not match (cases 6 and 9 at 14ed74a and 3321bfc). -> what the log
    shows of it, never the command's own exit or output: {"id", "from", "to", "outcome", "observed": "log"} for a
    committed message from @lead-dev to @operator with exactly this text, its outcome read after up to say's own
    wait; None when none appeared."""
    body = team.dir / f"report-{random.randrange(16 ** 6):06x}.md"
    body.write_text(text)
    log = factory_log(team)
    before = max((rec["id"] for rec in log.read_box()[0] if rec["t"] == "msg"), default=0)
    command = f"python3 {shlex.quote(str(team.script))} say operator --file {shlex.quote(str(body))}"
    team.send(team.member("lead-dev"), team.adapter.act_alone(command))

    def found():
        records, box = log.read_box()
        return next((rec["id"] for rec in records if rec["t"] == "msg" and rec["id"] > before
                     and rec["id"] in box.msgs and rec.get("from") == "lead-dev" and rec.get("to") == ["operator"]
                     and rec.get("text") == text), None)
    if not team.until(found, timeout):
        return None
    ident = found()
    deadline = time.monotonic() + SAY_WAIT            # what say itself waits for before it reports
    while time.monotonic() < deadline and _outcome(log, ident, "operator")[0] == "sending":
        time.sleep(0.2)
    return {"id": ident, "from": "lead-dev", "to": ["operator"], "outcome": list(_outcome(log, ident, "operator")),
            "observed": "log"}


# Pure judgements, each unit-tested for its false-pass paths (@architect-2's review of the first PR).

SAY_WAIT = 10   # factory.py's SAY_WAIT: how long say waits to report the outcome


def judge_latency(trials):
    """trials: [{"idle": recipient idle when timing began, "seconds": wall time, "outcome": the say's report}].
    Delivery is normally within SAY_WAIT when all three started idle, all were delivered and the median is within
    SAY_WAIT; one slower trial is allowed and reported. -> (status, observed, reason)."""
    if len(trials) < 3 or not all(t["idle"] for t in trials):
        return "not_run", None, "a trial did not start from an idle recipient"
    if not all(t["outcome"] == "delivered" for t in trials):
        return "fail", None, "a say was not delivered"
    times = sorted(t["seconds"] for t in trials)
    median, over = times[len(times) // 2], sum(1 for s in times if s > SAY_WAIT)
    observed = f"median {median} s of {len(times)} warm says; {over} over SAY_WAIT ({SAY_WAIT} s)"
    if median > SAY_WAIT:
        return "fail", observed, f"the median is over SAY_WAIT ({SAY_WAIT} s)"
    return "pass", observed, None


def judge_dialog(declared, seen, ready, suppressed=False):
    """declared: the table's [text, keys] dialogs; seen: the declared text observed on the subject's screen during
    this fresh start, or None; ready: the start finished. A known prompt answered by Factory passes once the start
    finishes; an owner-only prompt (no keys) passes when it is left. A declared dialog that never showed proves
    nothing. -> (status, observed, reason)."""
    keys = {text: k for text, k in declared}
    if seen is None:
        if not declared:
            return "pass", "no startup dialog declared; none of the known prompts appeared", None
        if ready and suppressed and not any(keys.values()):   # a launch argument avoids it (agy, LC-7): verified
            return "pass", "the owner-only dialog was avoided: the launch trusts exactly this folder, the start" \
                " finished and the member acted", None
        return "not_run", None, "the declared dialog did not appear in this start"
    if keys.get(seen):
        if ready:
            return "pass", f'"{seen}" was shown, answered and the start finished', None
        return "fail", f'"{seen}" was shown', "the start did not finish after it"
    if ready:
        return "fail", f'"{seen}" (owner only) was shown', "the start finished past an owner-only dialog"
    return "pass", f'"{seen}" (owner only) was shown and left to the owner', None


def judge_blocked(notes, member, term, queued, typed_while_blocked, final, count):
    """notes: [{"about", "term", "outcome"}] for the blocked notes this episode produced; the member's queued pair,
    whether anything was typed while it was blocked, its final outcome and how many times its action ran."""
    facts = []
    if len(notes) != 1:
        facts.append(f"{len(notes)} blocked notes")
    elif notes[0]["about"] != member or notes[0]["term"] != term or notes[0]["outcome"] != "delivered":
        facts.append(f"the note was about @{notes[0]['about']} at {notes[0]['term']}, {notes[0]['outcome']}")
    if not queued:
        facts.append("the message was not queued as blocked")
    if typed_while_blocked:
        facts.append("something was typed while blocked")
    if final != "delivered":
        facts.append(f"final outcome {final}")
    if count != 1:
        facts.append(f"the action ran {count} times")
    return ("pass", None) if not facts else ("fail", "; ".join(facts))


def judge_member_switch(switched, launch, recalled_right):
    """member-switch: a proved native switch, then a down/up that replaced the terminal (both exits 0), then the
    expected conversation's word. Without the replacement a still-running session would recall its own word."""
    if not switched:
        return "not_run"
    cycled = launch["down_exit"] == 0 and launch["up_exit"] == 0 and launch["replaced"]
    return "pass" if cycled and recalled_right else "fail"


def judge_switch(before, after):
    """A native conversation switch is shown by the harness's own session variable changing in its tool's
    environment: both values seen, and different."""
    if not before or not after:
        return False, "the session variable was not seen before and after"
    return before != after, None if before != after else "the session variable did not change"


def _operator_start(team, run, case, role, harness, names, trust_directories=None):
    """The operator past its own startup dialogs before the owner's first input, answered on the second poll as up
    does (a fake one shows one only for case 1)."""
    if team.real:
        _trust_entries(run, team, trust_directories)
    watcher = Harness.attach(team.qa, team.operator_harness, team.operator)
    settled = watcher.settle(timeout=120)
    team.operator_start = {"settled": settled, "answered": watcher.answered}
    if settled.startswith(READY):
        return True
    for name in names:
        run.row(case, name, role, harness, "not_run", f"case {case} {name}",
                reason=f"the operator did not start: {settled}")
    return False


def _operator_commands(run, team, harness, peer):
    """operator-commands (phase_8.md §3.2): the owner's first prompt, pasted through rodeo into a fresh operator,
    has it run up, say to a member, status and inbox --for itself through its own tool; no driver up."""
    if not _operator_start(team, run, 2, "operator", harness, ["operator-commands"]):
        return False
    tok, member_receipt = token(), team.receipt("member")
    py = f"python3 {shlex.quote(str(team.script))}"
    message = team.adapter.act_append(member_receipt, tok)
    # each command alone, one per tool call, as Factory's instructions say: an owner's sandbox exempts only a
    # standalone say or inbox, and a compound line ran in it first (Claude Code at 709f1a3, @operator)
    commands = {"up": f"{py} up", "say": f"{py} say lead-dev {shlex.quote(message)}", "status": f"{py} status",
                "inbox": f"{py} inbox --for operator"}
    prompt = team.adapter.act_alone_each(list(commands.values()))
    view = _rodeo(team, ["operator"])
    view.keys("ctrl+b")
    time.sleep(0.2)
    view.keys("1")
    time.sleep(0.8)
    view.focused_terminal = team.operator
    start = time.monotonic()
    view.paste(prompt)
    # up's own effect first: the member's terminal (the log exists only once up has run, @architect-2)
    member = team.until(lambda: team.member("lead-dev"), 900)
    if not member:
        run.row(2, "operator-commands", "operator", harness, "fail",
                "the owner's first prompt has the operator run up, say, status and inbox --for itself",
                proof={"up_member": None, "peer": peer}, elapsed=time.monotonic() - start,
                reason="no @lead-dev terminal within 900 s of the owner's prompt: up did not run or did not finish")
        return True
    log = factory_log(team)

    def said_by_operator():          # the committed say in the log, from the operator to the member, this text
        records, box = log.read_box()
        return next((rec["id"] for rec in records if rec["t"] == "msg" and rec.get("from") == "operator"
                     and rec.get("to") == ["lead-dev"] and rec.get("text") == message and rec["id"] in box.msgs),
                    None)
    reached = bool(team.until(lambda: member_receipt.exists() and tok in member_receipt.read_text(), 900))
    team.until(lambda: team.state(team.operator) in READY, 300)      # its turn over: status and inbox have run
    time.sleep(3)
    member = team.member("lead-dev")
    box = log.read_box()[1]
    briefed = bool(box) and member in box.briefed()
    say_id = said_by_operator()
    say_outcome = list(_outcome(log, say_id, "lead-dev")) if say_id else None
    to_operator = sorted({ident for (ident, to) in box.outcomes if to == "operator"}) if box else []
    full = {name: team.adapter.alone_output(commands[name], [c for n, c in commands.items() if n != name], prompt)
            for name in ("status", "inbox")}
    listed = sorted({int(n) for n in re.findall(r"\[Message (\d+) ", full["inbox"] or "")})
    inbox_ok = bool(full["inbox"]) and set(listed) <= set(to_operator) and (
        bool(listed) or "no messages for @operator" in full["inbox"])
    status_ok = "@lead-dev" in (full["status"] or "")
    proof = {"member_acted": reached, "member_briefed": briefed, "say_id": say_id, "say_outcome": say_outcome,
             "status_names_member": status_ok, "inbox_ids": listed, "operator_ids_in_log": to_operator,
             "inbox_seen": full["inbox"] is not None, "status_seen": full["status"] is not None, "peer": peer,
             "observed_in": "Factory's log and each command's own tool result in the operator's transcript",
             "prompted_by": "the owner's paste and Enter through rodeo into a fresh operator (prefix+1)"}
    ok = reached and briefed and bool(say_id) and (say_outcome or [""])[0] == "delivered" and status_ok and inbox_ok
    run.row(2, "operator-commands", "operator", harness, "pass" if ok else "fail",
            "the owner's first prompt has the operator run up, say, status and inbox --for itself; the brief is"
            " delivered, the member acts on the say, status names it and the inbox lists the operator's messages",
            proof=proof, elapsed=time.monotonic() - start, reason=None if ok else "see proof")
    return True


def case_2(run, role, harness, adapter_class):
    """idle-report, and for an operator profile operator-commands (phase_8.md §3.2, @architect's change 1)."""
    names = [n for n, _ in subcases(2, role)]
    team, peer = _team_for(adapter_class, role, harness)
    with team:
        if role == "operator":
            _operator_tab(team)
            if not _operator_commands(run, team, harness, peer):
                return
            if _team_quiet(team, hold=8, timeout=300) is None:     # its setup turn over before the idle report
                run.row(2, "idle-report", role, harness, "not_run", "case 2 idle-report",
                        reason="the team was not quiet after the operator's setup turn")
                return
        elif not _up(team, run, 2, role, harness, names):
            return
        receipt, tok = team.receipt("operator"), token()
        turn = team.get(team.operator)["turn"]
        start = time.monotonic()
        said = _member_says(team, team.adapter.act_append(receipt, tok))
        acted = team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
        acted_at = time.monotonic() - start
        ended = team.until(lambda: team.state(team.operator) in READY and team.get(team.operator)["turn"] > turn, 120)
        ident = said["id"] if said else None
        proof = {"member_say": said, "sender_message_id": ident,
                 "operator_turn": [turn, team.get(team.operator)["turn"]], "turn_completed": bool(ended),
                 "action_at_s": round(acted_at, 2), "peer": peer,
                 "prompted_by": "hc terminal.send to the member (the report is the member's say)"}
        if acted and not ended:
            run.row(2, "idle-report", role, harness, "fail", "the operator acts on an idle report with no input",
                    proof=proof, reason="the action landed but the operator's turn did not complete within 120 s")
        elif acted:
            run.row(2, "idle-report", role, harness, "pass", "the operator acts on an idle report with no input",
                    observed="token appended by the operator", proof=proof, elapsed=time.monotonic() - start)
        else:
            run.row(2, "idle-report", role, harness, "fail", "the operator acts on an idle report with no input",
                    proof=proof, reason="no action from the operator within 300 s")


def case_9(run, role, harness, adapter_class):
    """guarded: a guarded member restarts the keeper with its own say (phase_8.md §3.9)."""
    team, peer = _team_for(adapter_class, role, harness, permissions="guarded")
    with team:
        if not _up(team, run, 9, role, harness, ["guarded"]):
            return
        old = team.keeper()
        team.qa.rpc("terminal.kill", {"terminal": old})
        gone = team.until(lambda: team.get(old)["state"] == "finished", 30)
        receipt, tok = team.receipt("operator"), token()
        said = _member_says(team, team.adapter.act_append(receipt, tok))   # the say by itself, as members do
        said = said["id"] if said else None
        new = team.until(team.keeper, 60)
        acted = team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
        proof = {"old_keeper_finished": bool(gone), "new_keeper": bool(new) and new != old, "member_say_id": said,
                 "guarded_args": harness_table()[harness]["permissions"]["guarded"], "peer": peer}
        ok = bool(gone) and bool(new) and new != old and bool(acted) and said is not None
        run.row(9, "guarded", role, harness, "pass" if ok else "fail",
                "a guarded member's say reaches hc, restarts the keeper and is acted on", proof=proof,
                reason=None if ok else "the keeper did not restart or the operator did not act")


def _not_built(run, case, role, harness, names):
    for name in names:
        run.row(case, name, role, harness, "not_run", f"case {case} {name}", reason="not built yet")


def _subject(team, role):
    """The tested terminal: the operator for an operator profile, the member for a member profile."""
    return team.operator if role == "operator" else team.member("lead-dev")


def _ask(team, role, text):
    """Deliver a request to the subject the way Factory does: say from the operator to the member, or the
    member's own say to the operator."""
    if role == "member":
        return team.call("say", "lead-dev", text, timeout=60)
    return _member_says(team, text)


def case_1(run, role, harness, adapter_class):
    """states and startup-dialog (phase_8.md §3.1)."""
    team, peer = _team_for(adapter_class, role, harness, startup_dialog=harness)
    with team:
        declared = harness_table().get(harness, {}).get("dialogs", [])
        seen = {}

        def watch():                                  # the member's screen while up starts it
            deadline = time.monotonic() + 400
            while time.monotonic() < deadline and not seen.get("done"):
                address = team.member("lead-dev")
                if address and "dialog" not in seen:
                    try:
                        flat = "".join(team.qa.rpc("terminal.read", {"terminal": address, "lines": 0})
                                       .get("text", "").split())
                    except (HcError, HcDown):
                        flat = ""
                    found = next((text for text, _ in declared if "".join(text.split()) in flat), None)
                    if found:
                        seen["dialog"] = found
                time.sleep(0.25)
        watcher = threading.Thread(target=watch, daemon=True)
        if role == "member":
            watcher.start()
        ready = _up(team, run, 1, role, harness, [])
        seen["done"] = True
        if role == "member":
            watcher.join(timeout=5)
            observed_dialog = seen.get("dialog")
        else:
            start = getattr(team, "operator_start", {})
            settled = start.get("settled") or ""
            observed_dialog = (start.get("answered") or [None])[0] or (
                settled[len("dialog: "):] if settled.startswith("dialog: ") else None)

        def dialog_row(acted):
            suppressed = False
            if role == "member" and ready and team.member("lead-dev"):
                record = team.get(team.member("lead-dev"))       # the effective argv and the folder it runs in
                argv = record.get("argv") or []
                at = argv.index("--settings") if "--settings" in argv else None
                try:
                    trusted = json.loads(argv[at + 1]).get("trustedWorkspaces") if at is not None else None
                except (ValueError, IndexError):
                    trusted = None
                folder = str(record.get("cwd"))          # a fresh scratch folder, and exactly the one trusted
                suppressed = bool(acted) and trusted == [folder] and folder.startswith(str(team.dir))
            status, observed, reason = judge_dialog(declared, observed_dialog, bool(ready), suppressed)
            run.row(1, "startup-dialog", role, harness, status,
                    "a declared startup dialog is seen and answered on its second poll, or left to the owner; none is"
                    " invented", observed=observed, reason=reason,
                    proof={"declared": [text for text, _ in declared], "seen": observed_dialog,
                           "suppressed": suppressed, "start_finished": bool(ready),
                           "handled_by": "the driver's operator watcher, as up does" if role == "operator" else "up",
                           "mode": team.adapter.mode})

        if not ready:
            dialog_row(False)
            run.row(1, "states", role, harness, "not_run", "case 1 states", reason="the team did not come up")
            return
        subject = _subject(team, role)
        receipt = team.receipt("states")
        before = team.get(subject)
        states = [(before.get("status") or {}).get("state")]
        _ask(team, role, team.adapter.act_run(receipt, "sleep 4"))
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and not (receipt.exists() and "exit" in receipt.read_text()):
            state = team.state(subject)
            if state != states[-1]:
                states.append(state)
            time.sleep(0.25)
        team.until(lambda: team.state(subject) in READY, 60)
        states.append(team.state(subject))
        after = team.get(subject)
        proof = {"states": states, "turn": [before.get("turn"), after.get("turn")],
                 "action": receipt.read_text().strip() if receipt.exists() else None,
                 "ready_after_up": getattr(team, "ready_states", None)}
        ok = (states[0] in READY and "working" in states and states[-1] in READY
              and after.get("turn", 0) > before.get("turn", 0) and proof["action"] == "exit 0")
        run.row(1, "states", role, harness, "pass" if ok else "fail",
                "idle, working through a real tool action, idle again with the turn advanced", proof=proof,
                reason=None if ok else "the states or the action were not as expected")
        dialog_row(proof["action"] == "exit 0")


def case_7(run, role, harness, adapter_class):
    """suffix and multiline in one team; two-senders, pending-restart, upgrade and held-quiet in their own (§3.7).
    Each runs whatever happened to the ones before it."""
    for names, trial in ((("suffix", "multiline"), _case_7_main), (("two-senders",), _case_7_two_senders),
                         (("pending-restart",), _case_7_pending_restart), (("upgrade",), _case_7_upgrade),
                         (("held-quiet",), _case_7_held_quiet)):
        first_row = len(run.data["rows"])
        try:
            trial(run, role, harness, adapter_class)
        except NotRun as why:
            reason = str(why)
        except (RuntimeError, HcError, HcDown, OSError, subprocess.SubprocessError) as error:
            reason = f"the driver stopped: {type(error).__name__}: {error}"
        else:
            continue
        for name in names:                               # only the rows this trial did not record
            if not any(r["case"] == 7 and r["subcase"] == name and r["role"] == role and r["harness"] == harness
                       for r in run.data["rows"][first_row:]):
                run.row(7, name, role, harness, "not_run", f"case 7 {name}", reason=reason)


def _case_7_main(run, role, harness, adapter_class):
    team, peer = _team_for(adapter_class, role, harness)
    with team:
        if not _up(team, run, 7, role, harness, ["suffix", "multiline"]):
            return
        receipt = team.receipt("suffix")
        tokens = []
        for ending in ("@lead-dev", "$skill", "/command"):
            tok = token()
            tokens.append(tok)
            body = team.dir / f"suffix-{tok}.md"
            body.write_text(team.adapter.act_append(receipt, tok) + "\n" + f"see {ending}")   # no final newline
            if role == "member":
                team.call("say", "lead-dev", "--file", str(body), timeout=60)
            else:
                _member_says(team, body.read_text())
        got = team.until(lambda: receipt.exists() and all(t in receipt.read_text() for t in tokens) and
                         receipt.read_text(), 300)
        run.row(7, "suffix", role, harness, "pass" if got else "fail",
                "messages ending in @name, $skill and /command with no final newline are each acted on",
                proof={"acted": [t in (got or "") for t in tokens]},
                reason=None if got else "not every suffixed message was acted on")
        receipt2 = team.receipt("multiline")
        marks = [token() for _ in range(3)]
        filler = "\n".join(f"line {n}: filler for a long message" for n in range(400))
        text = "\n".join([team.adapter.act_append(receipt2, marks[0]), filler,
                          team.adapter.act_append(receipt2, marks[1]), filler,
                          team.adapter.act_append(receipt2, marks[2])])
        body = team.dir / "multiline.md"
        body.write_text(text)
        if role == "member":
            team.call("say", "lead-dev", "--file", str(body), timeout=60)
        else:
            _member_says(team, text)
        got = team.until(lambda: receipt2.exists() and all(m in receipt2.read_text() for m in marks) and
                         receipt2.read_text(), 300)
        order = [line for line in (got or "").split() if line in marks]
        ok = order == marks
        run.row(7, "multiline", role, harness, "pass" if ok else "fail",
                "a long multi-line message: the markers at its beginning, middle and end acted on in order",
                proof={"bytes": len(text.encode()), "order": [marks.index(m) for m in order]},
                reason=None if ok else "a marker was missing or out of order")

def _case_7_two_senders(run, role, harness, adapter_class):
    """two-senders (phase_8.md §3.7): two senders say to one recipient at the same moment; both IDs are acted on
    exactly once, in the order the log committed them. A second member is the other sender."""
    peer = _peer(role, harness)
    operator, member = (harness, peer) if role == "operator" else (peer, harness)
    if adapter_class is FixtureAdapter:
        member = "codex"
    team = QATeam(adapter_class, operator, {"lead-dev": member, "reviewer": member})
    with team:
        if not _up(team, run, 7, role, harness, ["two-senders"]):
            return
        recipient = "operator" if role == "operator" else "lead-dev"
        senders = (["lead-dev", "reviewer"] if role == "operator" else ["operator", "reviewer"])
        receipt, toks = team.receipt("two"), {s: token() for s in senders}
        start, outs = threading.Barrier(len(senders)), {}

        def send(sender):
            body = _note(team, team.adapter.act_append(receipt, toks[sender]))
            terminal = None if sender == "operator" else team.member(sender)
            start.wait(timeout=30)
            outs[sender] = team.call("say", recipient, "--file", str(body), terminal=terminal, timeout=120)
        threads = [threading.Thread(target=send, args=(s,), daemon=True) for s in senders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)
        team.until(lambda: receipt.exists() and all(t in receipt.read_text() for t in toks.values()), 300)
        time.sleep(10)                                   # time for a duplicate to show
        lines = _lines(receipt)
        ids = {s: (int(m.group(1)) if (m := _say_id(outs.get(s, (1, ""))[1])) else None) for s in senders}
        log = factory_log(team)
        outcomes = {s: _outcome(log, _say_id(outs.get(s, (1, ""))[1]), recipient) for s in senders}
        counts = {s: lines.count(toks[s]) for s in senders}
        acted = [s for t in lines for s in senders if t == toks[s]]
        committed = sorted((i, s) for s, i in ids.items() if i is not None)
        ok = (all(c == 1 for c in counts.values()) and None not in ids.values()
              and acted == [s for _, s in committed] and all((o or ("",))[0] == "delivered" for o in outcomes.values()))
        run.row(7, "two-senders", role, harness, "pass" if ok else "fail",
                "two senders at once to one recipient: both acted on exactly once, in committed order",
                proof={"senders": senders, "ids": ids, "outcomes": outcomes, "counts": counts, "acted_order": acted,
                       "peer": peer}, reason=None if ok else "a message was missing, repeated or out of order")


def _case_7_pending_restart(run, role, harness, adapter_class):
    """pending-restart (phase_8.md §3.7): work queued on a blocked recipient survives a keeper restart; after the
    owner answers through rodeo each stored ID is acted on once, and the member's blocked episode has one note."""
    team, peer = _team_for(adapter_class, role, harness)
    with team:
        if role == "operator":
            _operator_tab(team)
        if not _up(team, run, 7, role, harness, ["pending-restart"]):
            return
        subject = _subject(team, role)
        recipient = "operator" if role == "operator" else "lead-dev"
        view = _rodeo(team, ["operator", "team"] if role == "operator" else ["team", "@lead-dev"])   # before faults
        _ask(team, role, team.adapter.blocked_request())
        if not team.until(lambda: team.state(subject) == "blocked", 300):
            run.row(7, "pending-restart", role, harness, "not_run", "case 7 pending-restart",
                    reason=f"the {recipient} never reported blocked (last {team.state(subject)})")
            return
        log = factory_log(team)

        def say(tok):
            body = _note(team, team.adapter.act_append(receipt, tok))
            terminal = team.member("lead-dev") if role == "operator" else None
            return _say_id(team.call("say", recipient, "--file", str(body), terminal=terminal, timeout=60)[1])

        def notes():
            return [m["id"] for m in log.read_box()[1].msgs.values() if m["kind"] == "note"
                    and (m.get("meta") or {}).get("what", "").startswith("blocked")]
        receipt, toks = team.receipt("restart"), [token(), token()]
        first = say(toks[0])
        queued_before = bool(team.until(lambda: _outcome(log, first, recipient) == ("queued", "blocked"), 30))
        if role == "member":
            team.until(lambda: notes(), 60)
        notes_before = notes()
        old = team.keeper()
        team.qa.rpc("terminal.kill", {"terminal": old})
        gone = team.until(lambda: team.get(old)["state"] == "finished", 30)
        second = say(toks[1])                            # this say starts a new keeper
        new = team.until(team.keeper, 60)
        # queued behind the blocked head: the keeper marks only the head "blocked", the rest wait
        queued_after = bool(team.until(lambda: (_outcome(log, second, recipient) or ("",))[0] == "queued", 30))
        time.sleep(10)
        notes_after_restart = notes()
        typed = receipt.exists()
        view.keys("ctrl+b")
        time.sleep(0.2)
        view.keys("1")
        time.sleep(0.8)
        if role == "operator":
            view.focused_terminal = team.operator
        else:
            view.focus("@lead-dev")
        view.keys("enter")                               # the owner answers
        team.until(lambda: all(t in _lines(receipt) for t in toks), 300)
        time.sleep(15)
        counts = [_lines(receipt).count(t) for t in toks]
        finals = [_outcome(log, ident, recipient) for ident in (first, second)]
        one_note = role == "operator" or len(notes_after_restart) == 1 == len(notes_before)
        ok = (bool(gone) and bool(new) and new != old and not typed and counts == [1, 1] and one_note
              and queued_before and queued_after and all((f or ("",))[0] == "delivered" for f in finals))
        run.row(7, "pending-restart", role, harness, "pass" if ok else "fail",
                "work queued on a blocked recipient across a keeper restart: each stored ID acted on once after the"
                " answer, and one note for the episode", proof={
                    "keeper_replaced": bool(new) and new != old, "typed_while_blocked": typed, "counts": counts,
                    "queued_blocked_before_kill": queued_before, "queued_after_restart": queued_after,
                    "final_outcomes": finals, "notes": {"before": len(notes_before), "after": len(notes_after_restart)}
                    if role == "member" else "not applicable: notes are about members", "peer": peer},
                reason=None if ok else "see proof")


def case_10(run, role, harness, adapter_class):
    """idle-latency with its measured times, held-sender-exit and crash, each in its own team (§3.10)."""
    team, peer = _team_for(adapter_class, role, harness)
    with team:
        if not _up(team, run, 10, role, harness, ["idle-latency", "idle-latency-times"]):
            return
        trials = []
        recipient = "lead-dev" if role == "member" else "operator"
        for _ in range(3):
            idle = bool(team.until(lambda: team.state(_subject(team, role)) in READY, 300))
            time.sleep(1)                              # past the previous turn's end
            start = time.monotonic()
            if role == "member":
                code, out = team.call("say", "lead-dev", f"latency {token()}", timeout=60)
            else:
                code, out = team.call("say", "operator", "--file", str(_note(team, f"latency {token()}")),
                                      terminal=team.member("lead-dev"), timeout=60)
            found = _say_id(out)
            trials.append({"idle": idle, "seconds": round(time.monotonic() - start, 2),
                           "outcome": "delivered" if f"@{recipient}: delivered" in out else "not delivered",
                           "id": int(found.group(1)) if found else None, "exit": code})
        status, observed, reason = judge_latency(trials)
        run.row(10, "idle-latency", role, harness, status,
                f"say to an idle recipient is normally delivered within SAY_WAIT ({SAY_WAIT} s): all three from"
                " idle, all delivered, the median within the wait", observed=observed, proof={"trials": trials},
                reason=reason)
        run.row(10, "idle-latency-times", role, harness, "measured", "three warm say times",
                observed=json.dumps([t["seconds"] for t in trials]), proof={"seconds": [t["seconds"] for t in trials]})
    _case_10_held_sender_exit(run, role, harness, adapter_class)
    _case_10_crash(run, role, harness, adapter_class)


def _draft_in(team, role):
    """rodeo, focused on the recipient's pane, with an owner's unsent one-line draft typed there."""
    view = _rodeo(team, ["operator", "team"] if role == "operator" else ["team", "@lead-dev"])
    view.keys("ctrl+b")
    time.sleep(0.2)
    view.keys("1")
    time.sleep(0.8)
    if role == "operator":
        view.focused_terminal = team.operator
    else:
        view.focus("@lead-dev")
    view.type(f"owner draft {token()}")
    time.sleep(1)
    return view


def _say_to(team, role, text, timeout=120):
    """A say to the subject from its peer, as a driver-run command: (exit, output, seconds)."""
    start = time.monotonic()
    if role == "member":
        code, out = team.call("say", "lead-dev", "--file", str(_note(team, text)), timeout=timeout)
    else:
        code, out = team.call("say", "operator", "--file", str(_note(team, text)),
                              terminal=team.member("lead-dev"), timeout=timeout)
    return code, out, round(time.monotonic() - start, 2)


def _case_10_held_sender_exit(run, role, harness, adapter_class):
    """held-sender-exit (phase_8.md §3.10): say exits on its own at its wait while hc holds the send for the
    owner's draft; after the owner sends the draft, the recipient acts once."""
    team, peer = _team_for(adapter_class, role, harness, draft_wait=120)
    with team:
        if role == "operator":
            _operator_tab(team)
        if not _up(team, run, 10, role, harness, ["held-sender-exit"]):
            return
        recipient = "operator" if role == "operator" else "lead-dev"
        view = _draft_in(team, role)
        receipt, tok = team.receipt("held"), token()
        code, out, seconds = _say_to(team, role, team.adapter.act_append(receipt, tok))
        found = _say_id(out)
        log = factory_log(team)
        held = _outcome(log, found, recipient) if found else None
        acted_early = receipt.exists()
        view.keys("enter")                               # the owner sends the draft: the hold ends
        team.until(lambda: tok in _lines(receipt), 300)
        time.sleep(10)
        count = _lines(receipt).count(tok)
        final = _outcome(log, found, recipient) if found else None
        ok = (found is not None and SAY_WAIT - 1 <= seconds <= SAY_WAIT + 15 and (held or ("",))[0] == "sending"
              and not acted_early
              and count == 1 and (final or ("",))[0] == "delivered")
        run.row(10, "held-sender-exit", role, harness, "pass" if ok else "fail",
                "say returns at its wait while the send is held by the owner's draft; after release, one action",
                observed=f"say returned after {seconds} s with the send {(held or ('?',))[0]}",
                proof={"id": int(found.group(1)) if found else None, "say_exit": code, "say_seconds": seconds,
                       "held_at_exit": held, "acted_before_release": acted_early, "action_count": count,
                       "final": final, "draft_wait": 120, "peer": peer}, reason=None if ok else "see proof")


def _case_10_crash(run, role, harness, adapter_class):
    """crash (phase_8.md §3.10): the keeper is killed (SIGKILL, its PID from the beat, checked against its QA
    terminal) while hc holds a claimed send; a normal up restarts it. One claim total, final unconfirmed, no
    second dispatch; whether the old hc client still types after release is counted, not required."""
    team, peer = _team_for(adapter_class, role, harness, draft_wait=120)
    with team:
        if role == "operator":
            _operator_tab(team)
        if not _up(team, run, 10, role, harness, ["crash"]):
            return
        recipient = "operator" if role == "operator" else "lead-dev"
        view = _draft_in(team, role)
        receipt, tok = team.receipt("crash"), token()
        code, out, _ = _say_to(team, role, team.adapter.act_append(receipt, tok))
        found = _say_id(out)
        log = factory_log(team)
        claimed = team.until(lambda: found and _outcome(log, found, recipient) == ("sending", None), 30)
        keeper = team.keeper()
        beat = sys.modules["factory_under_test"].read_beat(log.state)
        verified = bool(beat) and beat[2] == keeper and team.get(keeper).get("pid") == beat[0]
        if not (found and claimed and verified):
            run.row(10, "crash", role, harness, "not_run", "case 10 crash", proof={
                "id": int(found.group(1)) if found else None, "claimed": bool(claimed), "beat_verified": verified},
                reason="no held claim, or the keeper's beat did not match its terminal")
            view.keys("enter")
            return
        os.kill(beat[0], signal.SIGKILL)
        died = team.until(lambda: team.get(keeper)["state"] == "finished", 30)
        up_code, _ = team.call("up", timeout=240)          # a normal command restarts the keeper
        new = team.until(lambda: team.keeper() not in (None, keeper) and team.keeper(), 60)
        team.until(lambda: _outcome(log, found, recipient)[0] != "sending", 60)
        view.keys("enter")                                 # release: the old hc client may still type
        time.sleep(30)
        ident = int(found.group(1))
        records = [(r["o"], r.get("why")) for r in log.read_box()[0]
                   if r["t"] == "out" and r["id"] == ident and r["to"] == recipient]
        claims = sum(1 for o, _ in records if o == "sending")
        final = _outcome(log, found, recipient)
        count = _lines(receipt).count(tok)
        ok = bool(died) and up_code == 0 and bool(new) and claims == 1 and final[0] == "unconfirmed" and count <= 1
        run.row(10, "crash", role, harness, "pass" if ok else "fail",
                "keeper killed during a held claim, restarted by up: one claim, final unconfirmed, no second dispatch",
                observed=f"{claims} claim(s), final {final[0]}, the held text was acted on {count} time(s)",
                proof={"id": ident, "outcomes": records, "keeper_replaced": bool(new), "up_exit": up_code,
                       "action_count": count, "peer": peer}, reason=None if ok else "see proof")


def _note(team, text):
    path = team.dir / f"note-{random.randrange(16 ** 6):06x}.md"
    path.write_text(text)
    return path


def _rodeo(team, titles):
    """rodeo on a driver core, viewing the QA core's team tab once its titles render; closed with the team."""
    if getattr(team, "view", None) is None:
        if not RODEO:
            raise NotRun("rodeo is not installed: owner input goes through rodeo")
        if EVIDENCE.get("rodeo_tabs") is None:           # once per run, before any case uses rodeo's keys
            EVIDENCE["rodeo_tabs"] = rodeo_tabs()
        if EVIDENCE["rodeo_tabs"].get("result") != "pass":
            raise NotRun(f"check rodeo_tabs did not pass: {EVIDENCE['rodeo_tabs'].get('result')}")
        team.drv = Core("qualifydrv")
        team.view = Rodeo(team.qa, team.drv)
        team.view.focused_terminal = None
        if team.view.wait_for(titles, timeout=30) is None:
            raise NotRun(f"rodeo did not show {titles}")
        time.sleep(1)
    return team.view


def case_5(run, role, harness, adapter_class):
    """blocked (phase_8.md §3.5): nothing typed while blocked, one note, the queued message delivered once after
    the owner answers through rodeo, with no Factory command between. An operator's blocked peer is Claude Code:
    its question tool blocks it, where a Codex member asked the same never did (Claude Code operator at 709f1a3)."""
    team, peer = _team_for(adapter_class, role, harness, peer="claude" if role == "operator" else None)
    with team:
        if not _up(team, run, 5, role, harness, ["blocked"]):
            return
        member = team.member("lead-dev")
        team.call("say", "lead-dev", team.adapter.blocked_request(), timeout=60)
        if not team.until(lambda: team.state(member) == "blocked", 300):
            run.row(5, "blocked", role, harness, "not_run", "a blocked member", reason=f"@lead-dev never reported"
                    f" blocked (last {team.state(member)}); a prompt could not be elicited in this mode")
            return
        receipt, tok = team.receipt("blocked"), token()
        code, out = team.call("say", "lead-dev", team.adapter.act_append(receipt, tok), timeout=60)
        ident = re.search(r"^message (\d+)$", out, re.M)
        log = factory_log(team)
        queued = team.until(lambda: _outcome(log, ident, "lead-dev") == ("queued", "blocked"), 30) if ident else None

        def blocked_notes():
            box = log.read_box()[1]
            return [{"id": m["id"], "about": m["meta"].get("about"), "term": m["meta"].get("term"),
                     "outcome": box.outcome(m["id"], "operator")["o"]} for m in box.msgs.values()
                    if m["kind"] == "note" and (m.get("meta") or {}).get("what", "").startswith("blocked")]
        team.until(lambda: any(n["outcome"] == "delivered" for n in blocked_notes()), 60)
        typed_while_blocked = receipt.exists()
        view = _rodeo(team, ["team", "@lead-dev"])
        view.keys("ctrl+b")
        time.sleep(0.2)
        view.keys("1")
        time.sleep(0.5)
        view.focus("@lead-dev")
        view.keys("enter")                         # the owner answers the prompt
        team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
        time.sleep(15)                             # time for a duplicate to show
        final = _outcome(log, ident, "lead-dev") if ident else None
        count = receipt.read_text().split().count(tok) if receipt.exists() else 0
        notes = blocked_notes()
        status, reason = judge_blocked(notes, "lead-dev", member, bool(queued), typed_while_blocked,
                                       final[0] if final else None, count)
        proof = {"message_id": int(ident.group(1)) if ident else None, "queued_blocked": bool(queued),
                 "notes": notes, "typed_while_blocked": typed_while_blocked, "final_outcome": final,
                 "action_count": count, "peer": peer}
        run.row(5, "blocked", role, harness, status,
                "blocked: nothing typed, one note about this member's episode delivered to the operator, the queued"
                " message delivered and acted on exactly once after the answer", proof=proof, reason=reason)


def factory_log(team):
    """The team's message log, read-only, from the private copy of Factory (phase_8.md §1)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("factory_under_test", team.script)
    module = sys.modules.get("factory_under_test")
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules["factory_under_test"] = module
        spec.loader.exec_module(module)
    state = next((r["labels"]["factory.dir"] for r in team.qa.rpc("terminal.list", {"all": True})["terminals"]
                  if (r.get("labels") or {}).get("factory.dir")), None)
    if state is None:
        raise RuntimeError("no Factory terminal in the QA core: up never ran")
    return module.open_log(state)


def _outcome(log, ident, name):
    if not ident:
        return None
    record = log.read_box()[1].outcome(ident if isinstance(ident, int) else int(ident[1]), name)
    return record["o"], record.get("why")


def _say_id(out):
    found = re.search(r"^message (\d+)$", out, re.M)
    return found


def case_8(run, role, harness, adapter_class):
    """dead-member, operator-gone and same-pane in one team; unconfirmed-brief and quit-during-hold each in their
    own (phase_8.md §3.8)."""
    _case_8_main(run, role, harness, adapter_class)
    if role == "member":
        _case_8_unconfirmed_brief(run, role, harness, adapter_class)
    _case_8_quit_during_hold(run, role, harness, adapter_class)


def _case_8_main(run, role, harness, adapter_class):
    team, peer = _team_for(adapter_class, role, harness, operator_shell=role == "operator")
    with team:
        names = [n for n, _ in subcases(8, role) if n not in ("unconfirmed-brief", "quit-during-hold")]
        if not _up(team, run, 8, role, harness, names):
            return
        log = factory_log(team)
        if role == "member":
            member = team.member("lead-dev")
            # The harness ends: killed, since a real harness asked to quit does not reliably do so (@lead-dev).
            team.qa.rpc("terminal.kill", {"terminal": member})
            ended = team.until(lambda: team.get(member)["state"] == "finished", 60)
            if not ended:
                run.row(8, "dead-member", role, harness, "not_run", "case 8 dead-member",
                        reason=f"the member's terminal is {team.get(member)['state']} after terminal.kill")
                return
            receipt, tok = team.receipt("dead"), token()
            code, out = team.call("say", "lead-dev", team.adapter.act_append(receipt, tok), timeout=60)
            ident = _say_id(out)
            gone = team.until(lambda: _outcome(log, ident, "lead-dev") == ("queued", "gone"), 30)
            nothing = not receipt.exists()
            up_code, up_out = team.call("up", "lead-dev", timeout=240)
            acted = team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
            fresh = team.member("lead-dev")
            proof = {"ended": bool(ended), "queued_gone": bool(gone), "nothing_typed": nothing,
                     "up_exit": up_code, "new_terminal": fresh not in (None, member),
                     "acted_after_up": bool(acted), "message_id": int(ident.group(1)) if ident else None,
                     "final_outcome": _outcome(log, ident, "lead-dev"),
                     "new_terminal_state": team.state(fresh) if fresh else None}
            ok = bool(ended) and bool(gone) and nothing and up_code == 0 and fresh not in (None, member) and acted
            run.row(8, "dead-member", role, harness, "pass" if ok else "fail",
                    "a member whose harness ended gets nothing typed; up brings a new terminal that gets the message",
                    proof=proof, reason=None if ok else "see proof")
            return
        operator = team.operator
        # operator-gone: the harness stops reporting in an open pane; messages queue until up. Each quit is of an
        # idle operator: Ctrl+C in a working turn interrupts it instead.
        if _team_quiet(team, hold=8, timeout=600) is None:
            run.row(8, "operator-gone", role, harness, "not_run", "case 8 operator-gone",
                    reason="the team was not quiet before the quit")
            run.row(8, "same-pane", role, harness, "not_run", "case 8 same-pane",
                    reason="operator-gone did not start")
            return
        team.adapter.quit(team.qa, operator)
        stopped = team.until(lambda: team.state(operator) is None, 120)
        receipt, toks, ids = team.receipt("gone"), [token(), token()], []
        for tok in toks:
            code, out = team.call("say", "operator", "--file", str(_note(team, team.adapter.act_append(receipt, tok))),
                                  terminal=team.member("lead-dev"), timeout=60)
            ids.append(_say_id(out))
        latched = team.until(lambda: (team.get(operator).get("labels") or {}).get("factory.kind")
                             == "operator-lost", 60)
        back = team.adapter.restart(team.qa, operator) and team.until(lambda: team.state(operator) in READY, 120)
        time.sleep(5)
        held = not receipt.exists()
        up_code, _ = team.call("up", timeout=240)
        acted = team.until(lambda: receipt.exists() and all(t in receipt.read_text() for t in toks) and
                           receipt.read_text(), 300)
        order = [line for line in (acted or "").split() if line in toks]
        proof = {"stopped_reporting": bool(stopped), "latched_lost": bool(latched), "restarted": bool(back),
                 "held_until_up": held, "up_exit": up_code, "order": [toks.index(t) for t in order]}
        ok = bool(stopped) and bool(latched) and bool(back) and held and up_code == 0 and order == toks
        run.row(8, "operator-gone", role, harness, "pass" if ok else "fail",
                "an operator gone from an open pane: messages queue until up, then arrive in order",
                proof=proof, reason=None if ok else "see proof")
        # same-pane: a quick quit and restart with nothing pending, then a message succeeds without up.
        quiet = _team_quiet(team, hold=8, timeout=600)
        team.adapter.quit(team.qa, operator)
        stopped2 = team.until(lambda: team.state(operator) is None, 60)
        restarted2 = bool(stopped2) and team.adapter.restart(team.qa, operator)
        back2 = restarted2 and team.until(lambda: team.state(operator) in READY, 120)
        if quiet is None or not back2:
            run.row(8, "same-pane", role, harness, "not_run", "case 8 same-pane",
                    reason="the team was not quiet before the quit" if quiet is None else
                    "the operator did not stop reporting after the quit" if not stopped2 else
                    "the shell prompt did not come back after the quit" if not restarted2 else
                    "the restarted operator did not report ready within 120 s",
                    proof={"stopped": bool(stopped2), "restarted": bool(restarted2), "state": team.state(operator)})
            return
        receipt2, tok2 = team.receipt("same-pane"), token()
        code, out = team.call("say", "operator", "--file", str(_note(team, team.adapter.act_append(receipt2, tok2))),
                              terminal=team.member("lead-dev"), timeout=60)
        acted2 = team.until(lambda: receipt2.exists() and tok2 in receipt2.read_text(), 300)
        same = team.get(operator)["address"] == operator
        ok2 = bool(acted2) and same and "@operator: delivered" in out
        run.row(8, "same-pane", role, harness, "pass" if ok2 else "fail",
                "an operator restarted in the same pane with nothing pending: the next say is delivered without up",
                proof={"same_terminal": same, "say": out.strip().splitlines()[-1:], "acted": bool(acted2)},
                reason=None if ok2 else "see proof")


def _case_8_unconfirmed_brief(run, role, harness, adapter_class):
    """unconfirmed-brief (phase_8.md §3.8): the keeper is killed (SIGKILL) with a replacement terminal's first brief
    claimed and not yet receipted. On recovery the brief is unconfirmed, an ordinary message is held not ready, and
    it is delivered once only after a new brief --send is delivered. No log row is written by the driver."""
    team, peer = _team_for(adapter_class, role, harness)
    with team:
        if not _up(team, run, 8, role, harness, ["unconfirmed-brief"]):
            return
        log = factory_log(team)
        module = sys.modules["factory_under_test"]
        old = team.member("lead-dev")
        team.qa.rpc("terminal.kill", {"terminal": old})
        team.until(lambda: team.get(old)["state"] == "finished", 60)
        hit = {}

        def interrupt():                                  # watch for the new terminal's brief claim, then kill
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline and "id" not in hit:
                records, box = log.read_box()
                for (ident, to), rec in box.outcomes.items():
                    msg = box.msgs.get(ident) or {}
                    if (to == "lead-dev" and msg.get("kind") == "brief" and rec["o"] == "sending"
                            and rec.get("term") not in (None, old)):
                        beat = module.read_beat(log.state)
                        if beat and team.get(beat[2]).get("pid") == beat[0]:
                            os.kill(beat[0], signal.SIGKILL)
                            hit.update(id=ident, term=rec["term"], keeper=beat[2])
                        break
                time.sleep(0.05)
        watcher = threading.Thread(target=interrupt, daemon=True)
        watcher.start()
        relaunch = threading.Thread(target=lambda: hit.update(up=team.call("up", "lead-dev", timeout=240)[0]),
                                    daemon=True)
        relaunch.start()
        watcher.join(timeout=250)
        if "id" not in hit:
            run.row(8, "unconfirmed-brief", role, harness, "not_run", "case 8 unconfirmed-brief",
                    reason="the new terminal's brief was never seen claimed, so the keeper was not interrupted")
            return
        team.until(lambda: team.get(hit["keeper"])["state"] == "finished", 30)
        relaunch.join(timeout=250)
        receipt, tok = team.receipt("after-brief"), token()
        code, out = team.call("say", "lead-dev", team.adapter.act_append(receipt, tok), timeout=60)   # a keeper again
        ordinary = _say_id(out)
        team.until(lambda: _outcome(log, ordinary, "lead-dev") == ("queued", "not ready"), 60)
        brief = log.read_box()[1].outcome(hit["id"], "lead-dev")
        held = _outcome(log, ordinary, "lead-dev")
        time.sleep(10)
        early = tok in _lines(receipt)
        send_code, send_out = team.call("brief", "--send", "lead-dev", timeout=240)
        team.until(lambda: tok in _lines(receipt), 300)
        time.sleep(10)
        count = _lines(receipt).count(tok)
        box = log.read_box()[1]
        briefs = sorted(i for i, m in box.msgs.items() if m.get("kind") == "brief" and "lead-dev" in m["to"])
        new_brief = next((i for i in briefs if i > hit["id"]), None)
        order = [(r["id"], r["o"]) for r in log.read_box()[0] if r["t"] == "out" and r["to"] == "lead-dev"
                 and r["o"] == "delivered" and r["id"] in (new_brief, int(ordinary.group(1)) if ordinary else None)]
        delivered_after_brief = [i for i, _ in order] == [new_brief, int(ordinary.group(1))] if ordinary else False
        ok = (brief.get("o") == "unconfirmed" and held == ("queued", "not ready") and not early and send_code == 0
              and count == 1 and delivered_after_brief)
        run.row(8, "unconfirmed-brief", role, harness, "pass" if ok else "fail",
                "an interrupted first brief is unconfirmed; an ordinary message waits not ready and is delivered once,"
                " after a new brief --send", proof={
                    "brief_id": hit["id"], "brief_outcome": [brief.get("o"), brief.get("why")], "held": held,
                    "acted_before_new_brief": early, "brief_send_exit": send_code,
                    "brief_send_output": _bounded(send_out.strip()), "new_brief_id": new_brief,
                    "delivery_order": order, "action_count": count, "peer": peer},
                reason=None if ok else "see proof")


def _case_8_quit_during_hold(run, role, harness, adapter_class):
    """quit-during-hold (phase_8.md §3.8), measurement only: the operator quits while hc holds a claimed send for the
    owner's draft; what reached its pane's sink after the hold ended is recorded. An inert token, no action."""
    if role == "member":
        run.row(8, "quit-during-hold", role, harness, "not_run", "case 8 quit-during-hold",
                reason="a member's pane closes when its harness ends: there is no sink to read")
        return
    team, peer = _team_for(adapter_class, role, harness, operator_shell=True, draft_wait=60, operator_sink=True)
    with team:
        _operator_tab(team)
        if not _up(team, run, 8, role, harness, ["quit-during-hold"]):
            return
        log = factory_log(team)
        if _team_quiet(team, hold=8, timeout=600) is None:   # a working operator's Ctrl+C interrupts, not quits
            run.row(8, "quit-during-hold", role, harness, "not_run", "case 8 quit-during-hold",
                    reason="the team was not quiet before the draft")
            return
        view = _rodeo(team, ["operator", "team"])
        view.keys("ctrl+b")
        time.sleep(0.2)
        view.keys("1")
        time.sleep(0.8)
        view.focused_terminal = team.operator
        view.type(f"owner draft {token()}")
        time.sleep(1)
        tok = token()                                    # inert: a bare word, nothing to act on
        code, out = team.call("say", "operator", "--file", str(_note(team, tok)), terminal=team.member("lead-dev"),
                              timeout=60)
        found = _say_id(out)
        claimed = team.until(lambda: found and _outcome(log, found, "operator") == ("sending", None), 60)
        state_at_quit = team.state(team.operator)
        held_at_quit = _outcome(log, found, "operator") if found else None
        start = time.monotonic()
        if team.sink is not None:                        # a real harness: its exit starts cat into a fresh file
            team.adapter.quit_keys_only(team.qa, team.operator, stop=team.sink.exists)
            exited = team.until(lambda: team.sink.exists(), 60)
        else:                                            # the fake's loop reads a line and runs nothing
            team.adapter.quit(team.qa, team.operator)
            exited = team.until(lambda: team.state(team.operator) is None, 60)
        quit_s = round(time.monotonic() - start, 1) if exited else None
        held_at_exit = _outcome(log, found, "operator") if found else None

        def sink():
            if team.sink is not None:                    # only what arrived after the exit
                return team.sink.read_text() if team.sink.exists() else ""
            return "".join(team.qa.rpc("terminal.read", {"terminal": team.operator, "lines": 0}).get("text", "")
                           .split())
        at_exit = sink().count(tok)                      # what was already there when the harness had gone
        time.sleep(75)                                   # past draft_wait: the hold has ended either way
        after = sink().count(tok)
        recorder_alive = team.sink is None or team.get(team.operator)["state"] == "live"   # cat still reading
        final = _outcome(log, found, "operator") if found else None
        missing = ("the send was not claimed and held" if not claimed else "the send was no longer held when the quit"
                   " began" if (held_at_quit or ("",))[0] != "sending" else "the harness was not seen to exit"
                   if not exited else "the send was no longer held when the harness had exited"
                   if (held_at_exit or ("",))[0] != "sending" else "the sink stopped reading before the hold ended"
                   if not recorder_alive else None)
        reached = after > at_exit
        observed = None if missing else (
            f"the harness exited after {quit_s} s with the send held; afterwards the token"
            f" {'reached' if reached else 'did not reach'} the pane's sink; the send ended {(final or ('?',))[0]}")
        run.row(8, "quit-during-hold", role, harness, "not_run" if missing else "measured",
                "what reaches the pane after a quit during a held send", observed=observed,
                proof={"id": int(found.group(1)) if found else None, "claimed": bool(claimed), "quit_s": quit_s,
                       "held_at_quit": held_at_quit, "held_at_exit": held_at_exit, "state_at_quit": state_at_quit,
                       "token_count_at_exit": at_exit, "token_count_after": after,
                       "recorder_alive": recorder_alive,
                       "reached_after_exit": reached if not missing else None, "final": final,
                       "peer": peer}, reason=missing)


def _operator_tab(team):
    """Give the QA operator a tab of its own in the layout doc, so rodeo shows it and up adds the team tab in the
    same space (launch_layout.md §5): prefix+1 is the operator, prefix+2 the team."""
    space, tab = "sp" + "a" * 32, "tb" + "a" * 32
    doc = {"spaces": [{"id": space, "name": "qa", "folder": str(team.work), "tabs": [
        {"id": tab, "name": "operator", "root": {"id": "s" + "a" * 32, "type": "row", "children": [
            {"id": "p" + "a" * 32, "type": "block", "title": "@operator",
             "content": {"terminal": team.operator}}]}}]}]}
    current = team.qa.rpc("doc.get", {"name": "herdr-app"})
    if current.get("value") is None:
        team.qa.rpc("doc.set", {"name": "herdr-app", "value": doc, "version": current["version"]})
        team.qa.rpc("terminal.label", {"terminal": team.operator, "labels": {
            "herdr.space": space, "herdr.tab": tab, "herdr.seen": "0"}})


def case_3(run, role, harness, adapter_class):
    """draft (phase_8.md §3.3): a report to an idle operator with an unsent owner draft is held; the draft
    survives byte for byte, then the report is acted on after it. draft-cleared is not built yet."""
    team, peer = _team_for(adapter_class, role, harness, draft_wait=60)
    with team:
        _operator_tab(team)
        if not _up(team, run, 3, role, harness, [n for n, _ in subcases(3, role)]):
            return
        view = _rodeo(team, ["operator", "team"])
        view.keys("ctrl+b")
        time.sleep(0.2)
        view.keys("1")
        time.sleep(0.8)
        view.focused_terminal = team.operator
        name = f"draft-{random.randrange(16 ** 6):06x}.txt"     # relative to the operator's cwd: typeable keys only
        draft_receipt, literal = team.dir / name, f"MiXeD  case,  punct. {token()}"
        draft = team.adapter.draft_request(f"../{name}", literal)
        view.type(draft)
        time.sleep(1)
        receipt, tok = team.receipt("report"), token()
        report = team.dir / "report.md"
        report.write_text(team.adapter.act_append(receipt, tok))
        member = team.member("lead-dev")
        command = f"python3 {shlex.quote(str(team.script))} say operator --file {shlex.quote(str(report))}"
        team.send(member, team.adapter.act_alone(command))       # alone, as members say (not a compound line)
        log = factory_log(team)
        claimed = team.until(lambda: next((ident for (ident, to), rec in log.read_box()[1].outcomes.items()
                                           if rec["o"] == "sending" and to == "operator"), None), 300)
        if not claimed:                                 # the member never said: nothing about the draft hold is shown
            run.row(3, "draft", role, harness, "not_run", "a report held by the owner's draft",
                    proof={"member_state": team.state(member), "peer": peer},
                    reason="the member's say was never claimed for the operator within 300 s")
            view.keys("enter")
            if role == "operator":
                _not_built(run, 3, role, harness, ["draft-cleared"])
            return
        time.sleep(8)                                   # past hc's quiet pause: only the draft can hold it now
        held = not receipt.exists() and not draft_receipt.exists()
        view.keys("enter")                              # the owner sends the draft
        drafted = team.until(lambda: draft_receipt.exists() and draft_receipt.read_text(), 300)
        acted = team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
        exact = drafted == literal + "\n"
        final = log.read_box()[1].outcome(claimed, "operator") or {}
        screen = _screen(team, "operator", team.operator_harness, team.operator)
        proof = {"claimed": bool(claimed), "report_id": claimed, "report_outcome": [final.get("o"), final.get("why")],
                 "held_past_8s": held, "draft_exact": exact, "report_acted": bool(acted), "draft_wait": 60,
                 "peer": peer, "draft_seen": _bounded(drafted), "draft_sent": _bounded(literal),
                 "markers_on_operator_screen": [m for m in (tok, literal.split()[-1]) if m in "".join(screen.split())]}
        ok = bool(claimed) and held and exact and bool(acted)
        run.row(3, "draft", role, harness, "pass" if ok else "fail",
                "a report is held while the owner has an unsent draft; the draft arrives byte for byte, then the report",
                proof=proof, reason=None if ok else "see proof")
        if role == "operator":
            _case_3_draft_cleared(run, team, harness, view, peer)


def _case_3_draft_cleared(run, team, harness, view, peer):
    """draft-cleared (phase_8.md §3.3), once per operator profile: a new draft, a member's report held by it, then
    the owner deletes the whole draft through rodeo without sending it. The report is then acted on, the cleared
    draft causes no action, and the next line the owner sends arrives with no stray characters."""
    team.until(lambda: team.state(team.operator) in READY, 300)
    time.sleep(3)
    name = f"draft-{random.randrange(16 ** 6):06x}.txt"
    cleared, literal = team.dir / name, f"cleared draft {token()}"
    draft = team.adapter.draft_request(f"../{name}", literal)
    view.focused_terminal = team.operator
    view.type(draft)
    time.sleep(1)
    receipt, tok = team.receipt("report-cleared"), token()
    said = _member_says(team, team.adapter.act_append(receipt, tok)) or {}
    found = said.get("id")
    log = factory_log(team)
    claimed = team.until(lambda: found and _outcome(log, found, "operator") == ("sending", None), 60) or (
        found and _outcome(log, found, "operator")[0] == "sending")
    time.sleep(8)                                    # past hc's quiet pause: only the draft holds it
    held = tok not in _lines(receipt)
    cleared_at = time.monotonic()
    for _ in draft:                                  # the owner deletes the whole draft, one key per character
        view.keys("backspace")
    acted = team.until(lambda: tok in _lines(receipt), 300)
    released_after = round(time.monotonic() - cleared_at, 1) if acted else None
    team.until(lambda: team.state(team.operator) in READY, 300)
    time.sleep(10)
    no_draft_action = not cleared.exists()
    name2 = f"next-{random.randrange(16 ** 6):06x}.txt"     # the next deliberate input
    nxt, literal2 = team.dir / name2, f"next line {token()}"
    view.type(team.adapter.draft_request(f"../{name2}", literal2))
    time.sleep(0.5)
    view.keys("enter")
    seen = team.until(lambda: nxt.exists() and nxt.read_text(), 300)
    exact = seen == literal2 + "\n"
    count = _lines(receipt).count(tok)
    ok = bool(claimed) and held and count == 1 and no_draft_action and exact
    run.row(3, "draft-cleared", "operator", harness, "pass" if ok else "fail",
            "a report held by a draft the owner then deletes without sending: the report is acted on once, the"
            " deleted draft does nothing, and the next sent line has no stray characters",
            observed=f"report acted on {released_after} s after the draft was deleted" if acted else None,
            proof={"report_id": found, "claimed": bool(claimed),
                   "held_past_8s": held, "keyboard": f"backspace x{len(draft)}",
                   "released_after_clear_s": released_after,
                   "report_count": count, "cleared_draft_acted": not no_draft_action, "next_line_exact": exact,
                   "peer": peer}, reason=None if ok else "see proof")


def case_6(run, role, harness, adapter_class):
    """resume and member-switch for a member; operator-switch for an operator (phase_8.md §3.6)."""
    # Claude Code's skills switch also turns off its native /clear (@lead-dev's pilot at 0748216: "Unknown command"),
    # which the switch trials need: its teams here start with commands on, and a sighting is recorded, not failed
    team, peer = _team_for(adapter_class, role, harness, skills_exempt=("claude",))
    with team:
        names = [n for n, _ in subcases(6, role)]
        if role == "operator":
            _operator_tab(team)
        if not _up(team, run, 6, role, harness, names):
            return
        adapter = team.adapter

        def switch():
            """The owner starts a new conversation natively, typing the harness's command through rodeo (a say
            would arrive as "[from @operator] /new", which no harness reads as a command)."""
            view = _rodeo(team, ["operator", "team"] if role == "operator" else ["team", "@lead-dev"])
            view.keys("ctrl+b")
            time.sleep(0.2)
            view.keys("1")
            time.sleep(0.8)
            if role == "operator":
                view.focused_terminal = team.operator
            else:
                view.focus("@lead-dev")
            view.type(adapter.switch_request(harness))
            time.sleep(0.5)
            view.keys("enter")
        variable = harness_table().get(harness, {}).get("session_env")

        def session(terminal):
            """The harness's own session variable, as its tool sees it (observation through hc)."""
            if not variable:
                return None
            out, done = team.receipt("session"), team.receipt("session-run")
            team.send(terminal, adapter.act_run(done, f'printf "%s\\n" "${{{variable}:-}}" > {shlex.quote(str(out))}'))
            team.until(lambda: done.exists() and "exit" in done.read_text(), 300)
            team.until(lambda: team.state(terminal) in READY, 120)
            return out.read_text().strip() if out.exists() else None
        if role == "operator":
            before = session(team.operator)
            switch()
            team.until(lambda: team.state(team.operator) in READY, 120)
            after = session(team.operator)
            switched, why = judge_switch(before, after)
            receipt, tok = team.receipt("operator-switch"), token()
            said = _member_says(team, adapter.act_append(receipt, tok))
            acted = team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
            count = receipt.read_text().split().count(tok) if receipt.exists() else 0
            status = ("pass" if count == 1 else "fail") if switched else "not_run"   # an unproved switch proves nothing
            run.row(6, "operator-switch", role, harness, status,
                    "after the operator switches conversation natively in its terminal (its session variable changes),"
                    " a member's report reaches it and is acted on once",
                    proof={"variable": variable, "switched": switched, "member_say": said, "action_count": count},
                    reason=None if status == "pass" else why or f"the action ran {count} times" if switched
                    else why or f"no {variable or 'session variable'} to show the switch")
            return

        def idle():
            return team.until(lambda: team.member("lead-dev") and team.state(team.member("lead-dev")) in READY, 300)

        def relaunch():
            old = team.member("lead-dev")
            down, _ = team.call("down", "lead-dev", timeout=120)
            code, out = team.call("up", "lead-dev", timeout=240)
            new = team.member("lead-dev")
            return {"down_exit": down, "up_exit": code, "replaced": bool(new) and new != old,
                    "resumed": "resumed @lead-dev" in out}

        def recall():
            receipt = team.receipt("recall")
            team.call("say", "lead-dev", adapter.recall(receipt), timeout=60)
            got = team.until(lambda: receipt.exists() and receipt.read_text().strip(), 300)
            return (got or "").strip().splitlines()[-1:] and (got or "").strip().splitlines()[-1]

        _member_says(team, "ready")                      # the member's own say records its conversation
        first = token()
        team.call("say", "lead-dev", adapter.remember(first), timeout=60)
        idle()
        time.sleep(2)
        launch = relaunch()
        cycled = launch["down_exit"] == 0 and launch["up_exit"] == 0 and launch["replaced"]
        if cycled:
            idle()
        got = recall() if cycled else None
        ok = cycled and got == first[::-1]
        run.row(6, "resume", role, harness, "pass" if ok else "fail",
                "down then up starts a new terminal that reopens the member's conversation: it recalls a word from"
                " before", observed=f"recalled {'the word' if got == first[::-1] else repr(got)}", proof=launch,
                reason=None if ok else "down or up failed, or the same terminal stayed" if not cycled
                else "the recalled word did not match")
        _member_says(team, "ready again")               # this launch's first say: its ready line
        before = session(team.member("lead-dev"))
        switch()
        idle()
        after = session(team.member("lead-dev"))
        switched, why = judge_switch(before, after)
        second = token()
        team.call("say", "lead-dev", adapter.remember(second), timeout=60)
        idle()
        _member_says(team, "switched")                   # its next say sees the new conversation's ID
        time.sleep(2)
        launch = relaunch()
        fresh = team.member("lead-dev")
        if judge_member_switch(switched, launch, True) != "pass":   # the cycle failed: no terminal to ask
            run.row(6, "member-switch", role, harness, judge_member_switch(switched, launch, True),
                    "after a native switch, down then up replaces the terminal", proof=dict(launch, switched=switched),
                    reason=why if not switched else "down or up failed, or the same terminal stayed")
            return
        idle()
        argv = " ".join(team.get(fresh).get("argv") or [])
        launch["resumed_conversation"] = ("launch" if before and before in argv       # the ID the new terminal's argv
                                          else "switched" if after and after in argv else "neither")   # carries
        launch["ids"] = {"launch": _alias(before), "switched": _alias(after)}
        got = recall()
        follow = harness_table()[harness].get("session_follow", True) if team.real else False
        expected = second if follow else first
        which = "second" if got == second[::-1] else "first" if got == first[::-1] else repr(got)
        status = judge_member_switch(switched, launch, got == expected[::-1])
        run.row(6, "member-switch", role, harness, status,
                f"after a native switch (its session variable changes) and a say, up resumes the"
                f" {'switched' if follow else 'launch'} conversation (session_follow = {str(follow).lower()})",
                observed=f"recalled the {which} word", proof=dict(launch, session_follow=follow, variable=variable,
                                                               switched=switched),
                reason=None if status == "pass" else why if not switched
                else "down or up failed, or the same terminal stayed" if not (launch["down_exit"] == 0
                                                                              and launch["up_exit"] == 0
                                                                              and launch["replaced"])
                else "another conversation")


def between_turns(harness, role):
    """Whether Factory delivers to this profile only between turns (keeper.md §9): Codex as operator."""
    return role == "operator" and harness_table().get(harness, {}).get("operator_deliver") == "idle"


def _app_selects(app):
    """Whether a live status app selects between-turns delivery in the table (keeper.md §9's lookup)."""
    return bool(app) and harness_table().get(app, {}).get("operator_deliver") == "idle"


def _ask_later(team, role, text):
    """_ask in a thread: a say to a working recipient may wait for delivery, and the case must go on meanwhile.
    box["out"] gets the say's own output (its message ID) when it returns."""
    box = {}

    def ask():
        if role == "member":
            code, out = team.call("say", "lead-dev", text, timeout=600)
            box.update(out=out, code=code)
        else:
            box.update(_member_says(team, text, timeout=600) or {"id": None}, done=True)   # this job's own
    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    return thread, box


def _screen(team, role, harness, terminal):
    """The subject's visible screen, as hc read gives it."""
    return Harness.attach(team.qa, harness if role == "operator" else team.members["lead-dev"],
                          terminal).screen().rstrip()


def _released_by(proof, count, offer):
    """What let the held message run: its action before the next input was offered is by itself."""
    acted, offered = proof.get("second_acted_after_esc_s"), proof.get("release_offer_after_esc_s")
    if count != 1 or acted is None:
        return "nothing" if count == 0 else f"{count} actions"
    return "itself" if offered is None or acted < offered else offer


def _alias(value):
    """A session ID's stand-in for exported proof (phase_8.md §2)."""
    return hashlib.sha1(value.encode()).hexdigest()[:8] if value else None


def _lines(path):
    return path.read_text().splitlines() if path.exists() else []


def case_4(run, role, harness, adapter_class):
    """mid-turn and owner-interrupt (phase_8.md §3.4 and its Claude Code Esc trial), each trial in a fresh team.
    mid-turn: a second instruction while the subject works on a task held open by an external release. The two
    Esc trials share a start: that second instruction, said by the member itself, delivered while the subject
    works and not yet acted on when the owner presses Esc through rodeo. next-delivery: touch nothing, a further
    say after draft_wait. owner-line: the owner types a line straight after Esc, it must still be whole once the
    queued message's turn has started, then one Enter. owner-interrupt passes when both trials act on every
    instruction exactly once, the owner's literal intact."""
    trials, results = ("mid-turn", "next-delivery", "owner-line"), {}
    recipient = "operator" if role == "operator" else "lead-dev"
    for trial in trials:
        team, peer = _team_for(adapter_class, role, harness)
        with team:
            if role == "operator":
                _operator_tab(team)
            row = "mid-turn" if trial == "mid-turn" else "owner-interrupt"
            if not _up(team, run, 4, role, harness, [row] if row != "owner-interrupt" else []):
                results[trial] = {"ran": False, "why": "the team did not come up"}
                continue
            subject = _subject(team, role)
            work, release = team.receipt("task"), team.dir / f"release-{token()}"
            started, finished, second = token(), token(), token()

            def said(box, log=None):
                found = box.get("id") if "id" in box else _say_id(box.get("out") or "")
                ident = found if isinstance(found, int) or found is None else int(found.group(1))
                return ident, (_outcome(log or factory_log(team), ident, recipient) if ident else None)

            began_at = time.monotonic()

            def flat():
                return "".join(_screen(team, role, harness, subject).split())

            def look(screen=None):
                """Facts, not the screen: the offset, state, turn and which test markers are visible."""
                screen = flat() if screen is None else screen
                return {"t": round(time.monotonic() - began_at, 1), "state": team.state(subject),
                        "turn": team.get(subject)["turn"], "markers": [m for m in EVIDENCE["tokens"] if m in screen]}
            try:
                first = _ask_later(team, role, team.adapter.long_task(work, started, release, finished))
                if not team.until(lambda: started in _lines(work) and team.state(subject) == "working", 300):
                    why = f"the task never started (state {team.state(subject)})"
                    results[trial] = {"ran": False, "why": why}
                    if trial == "mid-turn":
                        run.row(4, trial, role, harness, "not_run", f"case 4 {trial}", reason=why)
                    continue
                turn = team.get(subject)["turn"]
                between = between_turns(harness, role)
                app = (team.get(subject).get("status") or {}).get("app")     # the live root status, working
                later = _ask_later(team, role, team.adapter.act_append(work, second))
                third, later3 = token(), None
                if between and trial == "mid-turn":     # two reports in one working turn (keeper.md §9)
                    later3 = _ask_later(team, role, team.adapter.act_append(work, third))
                if between:                             # Factory keeps it queued while the operator works
                    delivered = team.until(lambda: later[1].get("done") and said(later[1])[1] == ("queued", "busy"),
                                           60)
                else:
                    delivered = team.until(lambda: (later[1].get("out") or later[1].get("done")) and said(later[1])[1]
                                           and said(later[1])[1][0] == "delivered", 30)
                time.sleep(5)
                proof = {"peer": peer, "turn_at_start": turn, "second_id": said(later[1])[0],
                         "second_outcome_while_working": said(later[1])[1],
                         "working_when_delivered": team.state(subject) == "working",
                         "pending_before_esc": second not in _lines(work),
                         "operator_app": app, "between_turns": between}
                if between:
                    proof["app_selects_between_turns"] = _app_selects(app)
                if later3 is not None:
                    team.until(lambda: later3[1].get("done"), 60)
                    proof.update(third_id=said(later3[1])[0], third_outcome_while_working=said(later3[1])[1],
                                 held_while_working=(sorted([said(later[1])[1] or ("",), said(later3[1])[1] or ("",)])
                                                     == [("queued", "busy"), ("queued", "waiting")]
                                                     and second not in _lines(work) and third not in _lines(work)
                                                     and team.state(subject) == "working"))
                if trial != "mid-turn":
                    view = _rodeo(team, ["operator", "team"] if role == "operator" else ["team", "@lead-dev"])
                    view.keys("ctrl+b")
                    time.sleep(0.2)
                    view.keys("1")
                    time.sleep(0.8)
                    if role == "operator":
                        view.focused_terminal = team.operator
                    else:
                        view.focus("@lead-dev")
                    keys = team.adapter.interrupt_keys(harness)
                    pending_at_esc = second not in _lines(work) and team.state(subject) == "working"
                    if between:                      # the same terminal, just before the key (@architect-2)
                        snap = team.get(subject)
                        proof["before_esc"] = {"terminal": subject, "state": (snap.get("status") or {}).get("state"),
                                               "turn": snap.get("turn"), "app": (snap.get("status") or {}).get("app"),
                                               "outcome": said(later[1])[1], "marker_on_screen": second in flat()}
                    view.keys(*keys)
                    esc_at = time.monotonic()
                    watch = {}

                    def watch_second():                      # when the held message's action lands after Esc
                        while time.monotonic() - esc_at < 600 and "s" not in watch:
                            if second in _lines(work):
                                watch["s"] = round(time.monotonic() - esc_at, 2)
                            time.sleep(0.25)
                    threading.Thread(target=watch_second, daemon=True).start()
                    if trial == "owner-line":                # the owner types straight after Esc, no Enter yet
                        extra, owner = f"owner line {token()}", team.receipt("owner")
                        view.paste(team.adapter.act_append(owner, extra), submit=False)
                        if between:    # still Factory's: it waits behind the owner's line, which goes first
                            time.sleep(5)             # well inside draft_wait: past it hc types after the line
                            proof["held_behind_owner_line"] = (second not in _lines(work)
                                                               and (said(later[1])[1] or ("",))[0] in ("queued",
                                                                                                       "sending"))
                        else:
                            moved = team.until(lambda: second in _lines(work), 120)   # Claude Code took 5-21 s
                            proof["queued_turn_started"] = bool(moved)
                        screen = flat()
                        proof["input_once_queued_ran"] = look(screen)
                        proof["owner_line_whole_before_enter"] = "".join(extra.split()) in screen
                    time.sleep(2)                            # what the harness did with the pending text
                    proof["interrupt_keys"] = keys
                    screen = flat()
                    proof["after_esc"] = look(screen)
                    # on screen at all: in the input or in the transcript, which a screen cannot tell apart
                    proof["second_on_screen_after_esc"] = second in screen
                    proof["pending_and_working_at_esc"] = pending_at_esc
                    # between turns: committed and still queued in Factory at Esc, nothing typed to it yet
                    proof["precondition"] = (bool(delivered) and proof["working_when_delivered"]
                                             and proof["pending_before_esc"] and pending_at_esc)
                    if between:                      # still Factory's at Esc: queued, nothing of it typed
                        proof["precondition"] = judge_before_esc(proof["before_esc"]) and pending_at_esc
                if trial == "next-delivery":                # touch nothing: the keeper's next delivery
                    while time.monotonic() - esc_at < 65:    # past the QA core's draft_wait (60 s) since Esc
                        time.sleep(1)
                    proof["draft_wait"] = 60
                    proof["release_offer_after_esc_s"] = round(time.monotonic() - esc_at, 1)
                    extra = token()
                    nxt = _ask_later(team, role, team.adapter.act_append(work, extra))
                    nxt[0].join(timeout=660)
                    team.until(lambda: extra in _lines(work) and second in _lines(work), 300)
                elif trial == "owner-line":                 # then one Enter for the owner's line
                    team.until(lambda: team.state(subject) in READY, 300)
                    proof["owner_line_on_screen_before_enter"] = "".join(extra.split()) in flat()
                    proof["release_offer_after_esc_s"] = round(time.monotonic() - esc_at, 1)
                    view.keys("enter")
                    team.until(lambda: owner.exists() and extra in owner.read_text()
                               and second in _lines(work), 300)
                if between and trial == "mid-turn":      # neither report typed into the original turn
                    screen = flat()
                    proof["before_release"] = {"turn": team.get(subject)["turn"], "second_on_screen": second in screen,
                                               "third_on_screen": third in screen,
                                               "outcomes": [said(later[1])[1], said(later3[1])[1]]}
                release.write_text("go\n")
                team.until(lambda: second in _lines(work), 300)
                if trial == "mid-turn":
                    team.until(lambda: finished in _lines(work), 300)
                team.until(lambda: team.state(subject) in READY, 120)
                time.sleep(15)                               # time for a duplicate to show
                for thread, _ in (first, later, *((later3,) if later3 else ())):
                    thread.join(timeout=60)
                proof["at_deadline"] = look()
                if trial != "mid-turn":
                    proof["second_acted_after_esc_s"] = watch.get("s")
                lines = _lines(work)
                count = lines.count(second)
                order = [{started: "started", finished: "finished", second: "second"}.get(line, "next")
                         for line in lines if line in (started, finished, second)
                         or (trial == "next-delivery" and line == extra)]
                log = factory_log(team)
                proof.update(turn=[turn, team.get(subject)["turn"]], order=order, second_count=count,
                             second_final=said(later[1], log)[1], at_end=look())
                if trial == "mid-turn" and between:
                    order3 = [{finished: "finished", second: "second", third: "third"}[line] for line in lines
                              if line in (finished, second, third)]
                    proof.update(order=order3, third_count=lines.count(third),
                                 third_final=said(later3[1], log)[1])
                    committed = [name for _, name in sorted([(proof["second_id"] or 0, "second"),
                                                             (proof["third_id"] or 0, "third")])]
                    proof["committed_order"] = committed          # the two says can commit in either order
                    box = log.read_box()[1]
                    proof["deliveries"] = {name: {k: box.outcome(i, recipient).get(k) for k in ("o", "term", "turn")}
                                           for name, i in (("second", proof["second_id"]), ("third", proof["third_id"]))
                                           if i}
                    proof["subject"] = subject
                    ok = judge_between_mid_turn(proof)
                    how = "held by Factory until the turn ended, then both acted on once, in order"
                    proof["classification"] = how if ok else "not held, lost or repeated"
                    run.row(4, "mid-turn", role, harness, "pass" if ok else "fail",
                            "two instructions while working: Factory types neither until the turn ends, then each"
                            " is acted on exactly once, in order", observed=how if ok else None, proof=proof,
                            reason=None if ok else "see proof" if proof["app_selects_between_turns"] else
                            f"the live operator reported app {app!r}, which does not select between-turns delivery")
                    continue
                if trial == "mid-turn":
                    ok = count == 1 and finished in lines
                    how = ("steered this turn" if ok and order.index("second") < order.index("finished")
                           else "queued for the next turn" if ok else "lost or duplicated")
                    proof["classification"] = how
                    run.row(4, "mid-turn", role, harness, "pass" if ok else "fail",
                            "a second instruction while working: both effects, the second exactly once",
                            observed=how, proof=proof, reason=None if ok else "an effect was lost or repeated")
                    continue
                proof["original_task"] = "completed" if finished in lines else "cancelled by the interrupt"
                if trial == "next-delivery":
                    proof.update(next_id=said(nxt[1], log)[0], next_outcome=said(nxt[1], log)[1],
                                 next_count=lines.count(extra))
                    proof["holds"] = count == 1 and proof["next_count"] == 1
                    proof["released_by"] = _released_by(proof, count, "the next delivery")
                else:
                    seen = owner.read_text() if owner.exists() else ""
                    proof.update(owner_exact=seen == extra + "\n", owner_seen=_bounded(seen))
                    # the receipt's exact bytes show the line arrived whole; a screen may fold a paste
                    # ("[Pasted text]"), so the before-Enter screen fact is recorded, not required
                    proof["holds"] = count == 1 and proof["owner_exact"] and (
                        not between or proof.get("held_behind_owner_line") is True)
                    proof["released_by"] = _released_by(proof, count, "the owner's Enter")
                results[trial] = dict(proof, ran=True)
            finally:
                release.write_text("go\n")                   # never leave the subject's tool waiting
    a, b = results.get("next-delivery", {}), results.get("owner-line", {})
    proof = {"next-delivery": a, "owner-line": b}
    if not (a.get("ran") and b.get("ran") and a.get("precondition") and b.get("precondition")):
        run.row(4, "owner-interrupt", role, harness, "not_run", "case 4 owner-interrupt", proof=proof,
                reason="inconclusive: a trial did not run, or its message was not pending when Esc was pressed")
        return
    ok = a["holds"] and b["holds"]
    observed = (f"the held message ran by {a['released_by']} / {b['released_by']}; original task {a['original_task']};"
                f" held message acted {a.get('second_acted_after_esc_s')} s / {b.get('second_acted_after_esc_s')} s"
                " after Esc")
    run.row(4, "owner-interrupt", role, harness, "pass" if ok else "fail",
            "a message pending at the owner's Esc is acted on exactly once, with the next delivery and with the"
            " owner's next line, each intact", observed=observed, proof=proof, reason=None if ok else "see proof")


def _upgrade(team, peer):
    """The other-version trial (phase_8.md §4) on a team that is up: with a send held by the owner's draft, a second
    valid copy of the script is installed atomically at the same path. A second message is queued during the drain;
    the old send finishes; the same keeper terminal then reports the new version in its beat and label, and the
    queued message is delivered once by the new image, with no wakeup. A failed exec keeps the old beat and fails.
    -> (ok, proof)."""
    keeper = team.keeper()
    old = hashlib.sha1(team.script.read_bytes()).hexdigest()[:12]
    view = _rodeo(team, ["operator", "team"])
    view.keys("ctrl+b")
    time.sleep(0.2)
    view.keys("1")
    time.sleep(0.8)
    view.focused_terminal = team.operator
    between = between_turns(team.operator_harness, "operator")

    def operator_now():                   # the operator at a moment of the trial (@architect-2)
        snap = team.get(team.operator)
        return {"state": (snap.get("status") or {}).get("state"), "turn": snap.get("turn"),
                "app": (snap.get("status") or {}).get("app")}
    seen = {"owner_first_key": operator_now()}
    name = f"draft-{random.randrange(16 ** 6):06x}.txt"          # the owner's draft holds the first send
    draft_receipt, literal = team.dir / name, f"owner draft {token()}"
    view.type(team.adapter.draft_request(f"../{name}", literal))
    time.sleep(1)
    effects, tok1 = team.receipt("upgrade"), token()             # one file: both effects, in the order they ran
    first = second = effects
    # both sends are driver-run says from the member's terminal, instant: a slow harness's own say (omp, agy) let
    # the owner's 60 s hold lapse before the claim, so nothing was ever held
    team.call("say", "operator", "--file", str(_note(team, team.adapter.act_append(first, tok1))),
              terminal=team.member("lead-dev"), timeout=60)
    log = factory_log(team)
    held = team.until(lambda: next((ident for (ident, to), rec in log.read_box()[1].outcomes.items()
                                    if rec["o"] == "sending" and to == "operator"), None), 120)
    seen["claim"] = operator_now()
    text = team.script.read_text()                               # a second valid copy, comment-only change
    staged = team.script.with_name(".factory.py.new")
    staged.write_text(text + f"\n# qualification other-version {token()}\n")
    os.replace(staged, team.script)
    new = hashlib.sha1(team.script.read_bytes()).hexdigest()[:12]

    def keeper_screen():
        return "".join(team.qa.rpc("terminal.read", {"terminal": keeper, "lines": 0}).get("text", "").split())
    draining = team.until(lambda: f"keeper{new}installed" in keeper_screen(), 90)
    still_held = tok1 not in _lines(effects)
    tok2 = token()
    # queued during the drain, before release: a driver-run say from the member's terminal, instant, so the owner's
    # hold cannot lapse while a slow harness runs it (agy's own say took long enough for the keeper to finish)
    _, out = team.call("say", "operator", "--file", str(_note(team, team.adapter.act_append(second, tok2))),
                       terminal=team.member("lead-dev"), timeout=60)
    queued = _say_id(out)
    queued_while_draining = (bool(queued) and _outcome(log, queued, "operator")[0] == "queued"
                             and tok2 not in _lines(effects))
    module = sys.modules["factory_under_test"]
    claimed_under = {}

    def watch_claim():                    # the image whose beat stands when the queued claim is first seen
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and "version" not in claimed_under and queued:
            outcome = _outcome(log, queued, "operator")[0]
            if outcome != "queued":
                beat = module.read_beat(log.state)
                claimed_under.update(version=beat[1] if beat else None, seen_as=outcome)
            time.sleep(0.02)
    watcher = threading.Thread(target=watch_claim, daemon=True)
    watcher.start()
    seen["release"] = operator_now()
    view.keys("enter")                                           # the owner sends the draft: the send finishes
    finished = team.until(lambda: tok1 in _lines(effects), 300)

    def running_new():                                           # observed passively: the keeper's own beat and label
        beat = module.read_beat(log.state)
        return bool(beat) and beat[1] == new and beat[2] == keeper and (
            (team.get(keeper).get("labels") or {}).get("factory.version") == new)
    reexeced = team.until(running_new, 120)
    acted = team.until(lambda: tok2 in _lines(effects), 300)
    time.sleep(10)
    watcher.join(timeout=5)
    screen = keeper_screen()
    # a failed exec says so and keeps serving with the old image: its later claim must not count as the new one
    exec_failed = "keepercouldnotrestartitself" in screen
    restart_seen = "keeperrestarting:newFactoryversion" in screen
    keepers = [r["address"] for r in team.qa.rpc("terminal.list", {"all": False})["terminals"]
               if (r.get("labels") or {}).get("factory.kind") == "keeper"]
    first_outcome = log.read_box()[1].outcome(held, "operator") if held else {}
    beat = module.read_beat(log.state)
    records, box = log.read_box()
    queued_id = int(queued.group(1)) if queued else None

    def history(ident):                   # every outcome the log holds for this pair, in order
        return [[rec["o"], rec.get("why")] for rec in records
                if rec["t"] == "out" and rec["id"] == ident and rec["to"] == "operator"]

    def delivery(ident):
        rec = box.outcome(ident, "operator") if ident else {}
        return {k: rec.get(k) for k in ("o", "term", "turn", "started")}
    lines = _lines(effects)
    proof = {"old_version": old, "new_version": new, "keeper_before": keeper, "keepers_after": keepers,
             "held_send_id": held, "held_send_outcome": [first_outcome.get("o"), first_outcome.get("why")],
             "draining_seen": bool(draining), "still_held_while_draining": still_held,
             "old_send_finished": bool(finished), "queued_id": int(queued.group(1)) if queued else None,
             "queued_while_draining": queued_while_draining, "beat_version": beat[1] if beat else None,
             "new_image_running": bool(reexeced), "queued_claimed_under": claimed_under.get("version"),
             "restart_seen": restart_seen, "exec_failed": exec_failed,
             "queued_final": _outcome(log, queued, "operator") if queued else None,
             "new_message_count": lines.count(tok2), "held_message_count": lines.count(tok1),
             "effect_order": [{tok1: "held", tok2: "queued"}[t] for t in lines if t in (tok1, tok2)],
             "histories": {"held": history(held), "queued": history(queued_id)},
             "deliveries": {"held": delivery(held), "queued": delivery(queued_id)},
             "operator_seen": seen, "between_turns": between, "operator": team.operator,
             "draft_exact": draft_receipt.exists() and draft_receipt.read_text() == literal + "\n", "peer": peer}
    ok = (old != new and keepers == [keeper] and bool(held) and bool(draining) and still_held and bool(finished)
          and first_outcome.get("o") == "delivered" and queued_while_draining and bool(reexeced) and bool(acted)
          and claimed_under.get("version") == new and restart_seen and not exec_failed
          and proof["new_message_count"] == 1 and (proof["queued_final"] or ("",))[0] == "delivered"
          and proof["held_message_count"] == 1 and proof["effect_order"] == ["held", "queued"])
    return ok, proof


def judge_between_upgrade(p):
    """case 7 upgrade with a between-turns operator: the live app selected the rule, both messages ended delivered
    each starting a turn of its own (none typed into the owner's turn), each acted on once, held first; a held
    message the guard refused shows the requeue (sending, then queued busy) in its history."""
    seen, histories, deliveries = p.get("operator_seen") or {}, p.get("histories") or {}, p.get("deliveries") or {}
    held = histories.get("held") or []
    # positive evidence of the overlap: the guard refused the held send in the owner's turn (sending, then
    # queued busy), and it was sent again afterwards (@architect-2: a sending-then-delivered history proves nothing)
    requeued = any(a == ["sending", None] and b == ["queued", "busy"] for a, b in zip(held, held[1:]))
    release = (seen.get("release") or {}).get("turn")
    after = all(d.get("o") == "delivered" and d.get("started") is True and d.get("term") == p.get("operator")
                and isinstance(d.get("turn"), int) and isinstance(release, int) and d["turn"] > release
                for d in deliveries.values())
    return bool(p.get("between_turns") and any(_app_selects(s.get("app")) for s in seen.values())
                and len(deliveries) == 2 and after and requeued and p.get("held_message_count") == 1
                and p.get("new_message_count") == 1 and p.get("effect_order") == ["held", "queued"])


def cross_other_version(run, fixture):
    """other-version (phase_8.md §4), once for the system, on real hc and rodeo with scripted fake agents: the case is
    Factory's, and a scripted peer may stand in (§1). Case 7's upgrade runs the same trial with each real profile."""
    team, peer = _team_for(FixtureAdapter, "operator", "codex", draft_wait=60)
    with team:
        _operator_tab(team)
        if not _up(team, run, "cross", "system", "system", []):
            run.row("cross", "other-version", "system", "system", "not_run", "the other-version cross case",
                    reason="the team did not come up")
            return
        ok, proof = _upgrade(team, peer)
        proof["agents"] = "fake agents (the case is Factory's, not a harness's)"
        run.row("cross", "other-version", "system", "system", "pass" if ok else "fail",
                "a new script under a live keeper: the held send finishes, the keeper restarts in its pane, the next"
                " message is delivered once by the new image", proof=proof, reason=None if ok else "see proof")


def _case_7_held_quiet(run, role, harness, adapter_class):
    """held-quiet (phase_8.md §3.7): the team's quiet note is claimed and held by the owner's draft in the operator
    pane; member work resumes; the owner sends the draft. One late informational note is allowed, and it is never
    delivered twice."""
    team, peer = _team_for(adapter_class, role, harness, draft_wait=120, quiet_grace=30)
    with team:
        _operator_tab(team)
        if not _up(team, run, 7, role, harness, ["held-quiet"]):
            return
        log = factory_log(team)
        view = _rodeo(team, ["operator", "team"])
        view.keys("ctrl+b")
        time.sleep(0.2)
        view.keys("1")
        time.sleep(0.8)
        view.focused_terminal = team.operator
        view.type(f"owner draft {token()}")

        def quiet_notes():
            box = log.read_box()[1]
            return [(i, box.outcome(i, "operator")["o"]) for i, m in box.msgs.items()
                    if m["kind"] == "note" and (m.get("meta") or {}).get("what") == "quiet"]
        claimed = team.until(lambda: next((i for i, o in quiet_notes() if o == "sending"), None), 120)
        if not claimed:
            view.keys("enter")
            run.row(7, "held-quiet", role, harness, "not_run", "case 7 held-quiet", proof={"quiet": quiet_notes()},
                    reason="no quiet note was claimed while the owner's draft was there")
            return
        work, tok = team.receipt("resumed"), token()
        team.call("say", "lead-dev", "--file", str(_note(team, team.adapter.act_append(work, tok))), timeout=60)
        resumed = team.until(lambda: tok in _lines(work), 300)
        view.keys("enter")                               # the owner sends the draft: the hold ends
        time.sleep(20)                                   # shorter than quiet_grace: no new quiet period yet
        notes = quiet_notes()
        if team.real:
            seen = None                                  # a real operator's screen does not count deliveries
        else:
            inputs = (team.dir / "operator-input.jsonl").read_text().splitlines()
            seen = sum(1 for line in inputs if "team quiet" in json.loads(line)["text"])
        final = dict(notes).get(claimed)
        delivered = sum(1 for _, o in notes if o == "delivered")
        ok = bool(resumed) and final in ("delivered", "dropped") and delivered <= 1 and (seen is None or seen <= 1)
        run.row(7, "held-quiet", role, harness, "pass" if ok else "fail",
                "a quiet note held by the owner's draft while member work resumes: at most one late note, never"
                " delivered twice", observed=f"the held note ended {final}",
                proof={"held_note": claimed, "final": final, "quiet_notes": notes, "delivered": delivered,
                       "quiet_text_typed": seen, "work_acted": bool(resumed), "peer": peer},
                reason=None if ok else "see proof")


def _case_7_upgrade(run, role, harness, adapter_class):
    """upgrade (phase_8.md §3.7): the other-version mechanism with this profile as operator or member."""
    team, peer = _team_for(adapter_class, role, harness, draft_wait=60)
    with team:
        _operator_tab(team)
        if not _up(team, run, 7, role, harness, ["upgrade"]):
            return
        ok, proof = _upgrade(team, peer)
        status, reason = ("pass", None) if ok else ("fail", "see proof")
        if ok and proof.get("between_turns") and not judge_between_upgrade(proof):
            # a between-turns operator (keeper.md §9) must show the overlap; one that never came is not run
            held = (proof.get("histories") or {}).get("held") or []
            requeued = any(a == ["sending", None] and b == ["queued", "busy"] for a, b in zip(held, held[1:]))
            status, reason = (("fail", "see proof: a message reached the owner's turn") if requeued else
                              ("not_run", "the owner's turn never overlapped the held send (no busy requeue):"
                                          " between-turns delivery was not exercised"))
        run.row(7, "upgrade", role, harness, status,
                "a new Factory version under a live keeper while this profile works: the held send finishes, the"
                " keeper restarts in its pane, and the next message is delivered once by the new image",
                proof=proof, reason=reason)

def _second_team(team, name="b"):
    """A second checkout on the same QA core, with the same member names and its own fake operator: (work, operator,
    call). Its team key, state and keeper are its own."""
    work = team.dir / f"work-{name}"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "FACTORY.md").write_text("Second qualification team.\n")
    (work / "factory.toml").write_text((team.work / "factory.toml").read_text())
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.name=qa", "-c", "user.email=qa@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-q", "-m", "qa"], check=True)
    env = {k: team.env[k] for k in ("HC_BIN", "PATH", "XDG_CACHE_HOME", "HOME") if k in team.env}
    if "CODEX_HOME" in team.env:
        env["CODEX_HOME"] = team.env["CODEX_HOME"]
    operator = team.qa.rpc("terminal.run", {"argv": team.adapter.operator_argv(team.operator_harness),
                                            "cwd": str(work), "env": env})["address"]

    def call(*args, terminal=None, timeout=120):
        done = subprocess.run([sys.executable, str(team.script), *args], cwd=work,
                              env=dict(team.env, HC_TERMINAL_ID=terminal or operator), capture_output=True,
                              text=True, timeout=timeout, check=False)
        return done.returncode, done.stdout + done.stderr
    return work, operator, call


def _team_terms(team, key):
    return [r for r in team.qa.rpc("terminal.list", {"all": False})["terminals"]
            if (r.get("labels") or {}).get("factory.team") == key]


def cross_two_teams(run, fixture):
    """two-teams (phase_8.md §4): two checkouts with identical member names on one QA core. Each say's action names
    the terminal that ran it, so a delivery to the other team's member fails even when the receipt is right; each
    team's member block sits in its own tab; each inbox holds only its own token; after team A is stopped, team B
    still sends and its own member acts. Fake agents: the case is Factory's."""
    team = QATeam(FixtureAdapter, "codex", {"lead-dev": "codex"})
    with team:
        if not _up(team, run, "cross", "system", "system", []):
            run.row("cross", "two-teams", "system", "system", "not_run", "the two-teams cross case",
                    reason="team A did not come up")
            return
        work_b, op_b, call_b = _second_team(team)
        code_b, out_b = call_b("up", timeout=240)
        factory_log(team)
        module = sys.modules["factory_under_test"]
        key_a, key_b = module.team_key(team.work), module.team_key(work_b)
        if code_b != 0 or key_a == key_b:
            run.row("cross", "two-teams", "system", "system", "not_run", "the two-teams cross case",
                    observed=_bounded(out_b), reason="team B did not come up" if code_b else "the team keys match")
            return

        def member_of(key):
            return next((r["address"] for r in _team_terms(team, key)
                         if (r.get("labels") or {}).get("factory.kind") == "member"), None)
        members = {"a": member_of(key_a), "b": member_of(key_b)}
        actions, toks = team.receipt("two-teams"), {"a": token(), "b": token()}
        calls = {"a": team.call, "b": call_b}

        def ask(t, tok):                                 # the action names the terminal that ran it
            done = team.receipt(f"run-{t}")
            command = f'printf "%s %s\\n" "$HC_TERMINAL_ID" {tok} >> {shlex.quote(str(actions))}'
            calls[t]("say", "lead-dev", team.adapter.act_run(done, command), timeout=120)
        threads = [threading.Thread(target=ask, args=(t, toks[t]), daemon=True) for t in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)
        team.until(lambda: all(any(line.endswith(" " + toks[t]) for line in _lines(actions)) for t in toks), 300)
        time.sleep(10)
        lines = _lines(actions)
        acted_by = {t: [line.split(" ", 1)[0] for line in lines if line.endswith(" " + toks[t])] for t in toks}
        own = {t: acted_by[t] == [members[t]] for t in toks}
        doc = team.qa.rpc("doc.get", {"name": "herdr-app"})["value"] or {"spaces": []}
        tab_of = {}
        for space in doc["spaces"]:
            for tab in space["tabs"]:
                def walk(node, tab_id=tab["id"]):
                    if node.get("type") == "block":
                        tab_of[(node.get("content") or {}).get("terminal")] = tab_id
                    for child in node.get("children", []):
                        walk(child)
                walk(tab["root"])
        tabs = {t: tab_of.get(members[t]) for t in toks}
        tabs_separate = None not in tabs.values() and tabs["a"] != tabs["b"]
        inboxes = {t: calls[t]("inbox", "--for", "lead-dev", timeout=60)[1] for t in toks}
        inbox_own = {t: toks[t] in inboxes[t] and toks["b" if t == "a" else "a"] not in inboxes[t] for t in toks}
        down_a, _ = team.call("down", timeout=120)
        team.until(lambda: member_of(key_a) is None, 60)
        tok2 = token()
        ask("b", tok2)
        still = team.until(lambda: any(line == f"{members['b']} {tok2}" for line in _lines(actions)), 300)
        ok = (all(own.values()) and tabs_separate and all(inbox_own.values()) and down_a == 0 and bool(still)
              and member_of(key_b) == members["b"])
        run.row("cross", "two-teams", "system", "system", "pass" if ok else "fail",
                "two teams with the same member names on one core stay apart, and B works after A stops",
                proof={"keys_differ": key_a != key_b, "acted_by_own_member": own, "member_tabs_distinct": tabs_separate,
                       "inbox_holds_only_own_token": inbox_own, "down_a_exit": down_a, "b_after_a_down": bool(still),
                       "agents": "fake agents (the case is Factory's)"}, reason=None if ok else "see proof")


def cross_multi_down(run, fixture):
    """multi-down (phase_8.md §4): two live, briefed members stopped together. No member-exit or lost note follows
    an intentional stop, their terminals go, and a message pending for one of them is dropped as stopped by down."""
    team = QATeam(FixtureAdapter, "codex", {"lead-dev": "codex", "reviewer": "codex"})
    with team:
        if not _up(team, run, "cross", "system", "system", []):
            run.row("cross", "multi-down", "system", "system", "not_run", "the multi-down cross case",
                    reason="the team did not come up")
            return
        log = factory_log(team)
        reviewer = team.member("reviewer")
        team.call("say", "reviewer", "FAKE: blocks", timeout=60)
        team.until(lambda: team.state(reviewer) == "blocked", 60)
        code, out = team.call("say", "reviewer", f"pending {token()}", timeout=60)
        pending = _say_id(out)
        team.until(lambda: _outcome(log, pending, "reviewer") == ("queued", "blocked"), 30)
        cursor = len(log.read_box()[0])
        terms = [team.member("lead-dev"), reviewer]
        down, _ = team.call("down", "lead-dev", "reviewer", timeout=120)
        gone = team.until(lambda: all(team.member(n) is None for n in ("lead-dev", "reviewer")), 60)
        time.sleep(15)                                   # past the keeper's next looks
        records, box = log.read_box()
        new_notes = [box.msgs[r["id"]]["meta"].get("what") for r in records[cursor:]
                     if r["t"] == "ok" and box.msgs.get(r["id"], {}).get("kind") == "note"]
        exit_notes = [w for w in new_notes if w in ("exit", "lost") or (w or "").startswith("exit")]
        final = _outcome(log, pending, "reviewer")
        ok = down == 0 and bool(gone) and not exit_notes and final == ("dropped", "stopped by down")
        run.row("cross", "multi-down", "system", "system", "pass" if ok else "fail",
                "two members stopped together: no exit or lost notes, terminals gone, pending work dropped",
                proof={"down_exit": down, "terminals": len(terms), "gone": bool(gone), "new_notes": new_notes,
                       "pending_final": final, "agents": "fake agents (the case is Factory's)"},
                reason=None if ok else "see proof")


def cross_owner_pane(run, fixture):
    """owner-pane (phase_8.md §4, spec §10): after the first up, the owner adds a block of their own to the team tab
    that holds the members. It survives a repeated up, down of one member and then the last, with its identity,
    terminal and contents, in that tab; each stopped member's block goes; the owner's pane still takes a marker
    typed through rodeo."""
    team = QATeam(FixtureAdapter, "codex", {"lead-dev": "codex", "reviewer": "codex"})
    with team:
        _operator_tab(team)
        if not _up(team, run, "cross", "system", "system", []):
            run.row("cross", "owner-pane", "system", "system", "not_run", "the owner-pane cross case",
                    reason="the team did not come up")
            return

        def where():                                      # terminal -> (space id, tab id, tab index), block ids
            current = team.qa.rpc("doc.get", {"name": "herdr-app"})
            places, blocks = {}, {}
            for space in (current["value"] or {"spaces": []})["spaces"]:
                for index, tab in enumerate(space["tabs"], 1):
                    def walk(node, tab=tab, index=index, space=space):
                        if node.get("type") == "block":
                            terminal = (node.get("content") or {}).get("terminal")
                            places[terminal] = (space["id"], tab["id"], index)
                            blocks[node["id"]] = (terminal, tab["id"], node.get("title"))
                        for child in node.get("children", []):
                            walk(child)
                    walk(tab["root"])
            return current, places, blocks
        members = {n: team.member(n) for n in ("lead-dev", "reviewer")}
        current, places, _ = where()
        if not all(m in places for m in members.values()) or places[members["lead-dev"]][1] != places[
                members["reviewer"]][1]:
            run.row("cross", "owner-pane", "system", "system", "not_run", "the owner-pane cross case",
                    reason="up did not put both members in one team tab")
            return
        space_id, tab_id, tab_index = places[members["lead-dev"]]
        owner = Probe(team.qa, "owner", fields="none", wait_status=False)
        team.until(lambda: "ready" in team.qa.rpc("terminal.read", {"terminal": owner.address, "lines": 0})
                   .get("text", ""), 30)
        value = current["value"]
        tab = next(t for s in value["spaces"] if s["id"] == space_id for t in s["tabs"] if t["id"] == tab_id)
        block = {"id": "p" + "e" * 32, "type": "block", "title": "owner", "content": {"terminal": owner.address}}
        tab["root"].setdefault("children", []).append(block)   # the owner's own block, in the team tab
        team.qa.rpc("doc.set", {"name": "herdr-app", "value": value, "version": current["version"]})
        team.qa.rpc("terminal.label", {"terminal": owner.address, "labels": {
            "herdr.space": space_id, "herdr.tab": tab_id, "herdr.seen": "0"}})
        seed = f"seed{token().lower()}"
        team.qa.rpc("terminal.keys", {"terminal": owner.address, "keys": list(seed)})
        if not team.until(lambda: seed in owner.text(), 30):
            run.row("cross", "owner-pane", "system", "system", "not_run", "the owner-pane cross case",
                    reason="the owner's pane never recorded its seed text")
            return
        steps = []

        def snapshot(what, code, stopped=()):
            _, places, blocks = where()
            mine = blocks.get(block["id"])
            steps.append({"step": what, "exit": code,
                          "owner_block_in_team_tab": mine == (owner.address, tab_id, "owner"),
                          "owner_live": team.get(owner.address)["state"] == "live",
                          "stopped_blocks_gone": all(members[n] not in places for n in stopped),
                          "members_live": len([n for n in members if team.member(n)])})
        snapshot("up again", team.call("up", timeout=240)[0])
        snapshot("down lead-dev", team.call("down", "lead-dev", timeout=120)[0], ["lead-dev"])
        snapshot("down reviewer", team.call("down", "reviewer", timeout=120)[0], ["lead-dev", "reviewer"])
        contents = seed in owner.text()
        _, places, _ = where()
        accepts = False
        if owner.address in places:
            view = _rodeo(team, ["operator"])
            view.keys("ctrl+b")
            time.sleep(0.2)
            view.keys(str(places[owner.address][2]))   # the tab that holds the owner's block
            time.sleep(0.8)
            doc_now = team.qa.rpc("doc.get", {"name": "herdr-app"})["value"]
            team_tab = next(t for s in doc_now["spaces"] for t in s["tabs"] if t["id"] == tab_id)
            first = team_tab["root"]["children"][0]                # rodeo starts on a tab's first pane
            view.focused_terminal = ((first.get("children") or [first])[0].get("content") or {}).get("terminal")
            if view.focused_terminal != owner.address:
                view.focus("owner")
            marker = "m" + token().lower()
            before = len(owner.text())
            view.type(marker)
            time.sleep(1)
            accepts = marker in owner.text()[before:]
        ok = (all(s["owner_block_in_team_tab"] and s["owner_live"] and s["stopped_blocks_gone"] and s["exit"] == 0
                  for s in steps) and steps[-1]["members_live"] == 0 and contents and accepts)
        run.row("cross", "owner-pane", "system", "system", "pass" if ok else "fail",
                "the owner's own block in the team tab survives a repeated up and both downs, the stopped members'"
                " blocks go, and the owner's pane still takes input", proof={
                    "steps": steps, "contents_kept": contents, "accepts_marker": accepts, "team_tab": tab_index,
                    "agents": "fake agents (the case is Factory's)"}, reason=None if ok else "see proof")


CROSS_FUNCTIONS = {"two-teams": cross_two_teams, "multi-down": cross_multi_down, "owner-pane": cross_owner_pane,
                   "other-version": cross_other_version}


CASE_FUNCTIONS = {1: case_1, 2: case_2, 3: case_3, 4: case_4, 5: case_5, 6: case_6, 7: case_7, 8: case_8, 9: case_9,
                  10: case_10}


def _file_evidence(run, first_row):
    """Attach the case's team diagnostics to each of its rows (bounded; for a team per trial, in order). A settings
    file agy changed during the case withholds its passes: the stamp cannot tell which process wrote it."""
    touched = sorted(set(EVIDENCE.get("settings_changed") or []))
    installed = EVIDENCE.get("installed_factory_seen") or []
    for row in run.data["rows"][first_row:]:
        row["diagnostics"] = EVIDENCE["teams"]
        if EVIDENCE.get("installed_factory_observed"):   # a harness with no switch: recorded, not failed
            row["installed_factory_observed"] = EVIDENCE["installed_factory_observed"]
        if installed:          # the owner's rule was broken: the case fails whatever it observed
            row.update(status="fail", reason=_bounded("an installed Factory script appeared in a QA terminal or"
                                                      f" transcript: {installed}"))
            row["installed_factory_seen"] = installed
        if touched and row["status"] == "pass":
            row.update(status="not_run", reason=_bounded(f"a settings file changed during the case: {touched}"))
        if touched:
            row["settings_changed"] = touched


def _tool_version(executable):
    if not executable or not shutil.which(executable):
        return None
    out = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30, check=False)
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else "unknown"


def _versions():
    """What every qualification records: the source, hc, rodeo and the candidate factory.py's VERSION."""
    script = ROOT / "skill" / "scripts" / "factory.py"
    return {"source_head": _source_head(), "hc_version": _tool_version(HC), "rodeo_version": _tool_version(RODEO),
            "factory_version": hashlib.sha1(script.read_bytes()).hexdigest()[:12]}


def _source_head():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def qualify(target, role=None, cases=None, json_path=None, fixture=False):
    """Run the selected cases; -> (evidence, exit code)."""
    if target == "cross":
        if role is not None or cases:
            raise ValueError("qualify cross takes no --role or --case")
        selected, mode = [], "cross"
    else:
        cases = cases or list(range(1, 11))
        bad = [c for c in cases if c not in CASES]
        if bad:
            raise ValueError(f"cases are 1 to 10, not {bad}")
        selected = [(r, h, c) for r, h in select_profiles(target, role) for c in cases]
        mode = "fixture" if fixture else "real"
    run = Qualification(mode, json_path, _versions())
    EVIDENCE["rodeo_tabs"] = None                       # probed once, by the first case that uses rodeo
    if target == "cross":
        for name in CROSS_CASES:
            EVIDENCE.update(teams=[], tokens=[], settings_changed=[], installed_factory_seen=[],
                            installed_factory_observed=[])
            first_row = len(run.data["rows"])
            function = CROSS_FUNCTIONS.get(name)
            try:
                if function is None:
                    raise NotRun("not built yet")
                function(run, fixture)
            except NotRun as why:
                run.row("cross", name, "system", "system", "not_run", f"the {name} cross case", reason=str(why))
            except (RuntimeError, HcError, HcDown, OSError, subprocess.SubprocessError) as error:
                if not any(r["case"] == "cross" and r["subcase"] == name for r in run.data["rows"]):
                    run.row("cross", name, "system", "system", "not_run", f"the {name} cross case",
                            reason=f"the driver stopped: {type(error).__name__}: {error}")
            _file_evidence(run, first_row)
            run.checkpoint()
        rows = [r for r in run.data["rows"] if r["case"] == "cross"]
        run.data["summary"] = {r["subcase"]: r["status"] for r in rows}
        if EVIDENCE["rodeo_tabs"] is not None:
            run.data["rodeo_tabs"] = {k: EVIDENCE["rodeo_tabs"].get(k) for k in ("result", "rodeo_version", "landed")}
        run.checkpoint()
        return run.data, 1 if any(r["status"] == "fail" for r in rows) else (
            2 if any(r["status"] == "not_run" for r in rows) else 0)
    adapter_class = FixtureAdapter if fixture else RealAdapter
    adapter = adapter_class(None)                     # version and model need no team
    for role_, harness, case in selected:
        print(f"qualify {role_} {harness} case {case}", file=sys.stderr)
        if not any(p["role"] == role_ and p["harness"] == harness for p in run.data["profiles"]):
            run.profile(role_, harness, version=adapter.version(harness), model=adapter.model(harness))
        function = CASE_FUNCTIONS.get(case)
        if not subcases(case, role_):
            continue
        if function is None:
            for name, measured in subcases(case, role_):
                run.row(case, name, role_, harness, "not_run", f"case {case} {name}", reason="not built yet")
        else:
            EVIDENCE.update(teams=[], tokens=[], settings_changed=[], installed_factory_seen=[],
                            installed_factory_observed=[])
            first_row = len(run.data["rows"])
            try:
                function(run, role_, harness, adapter_class)
            except NotRun as why:                         # only the rows this case did not get to (@reviewer)
                for name, _ in subcases(case, role_):
                    if not any(r["subcase"] == name and r["case"] == case and r["role"] == role_
                               and r["harness"] == harness for r in run.data["rows"][first_row:]):
                        run.row(case, name, role_, harness, "not_run", f"case {case} {name}", reason=str(why))
            except Exception as error:            # any driver stop: its rows say so, later cases still run
                where = traceback.extract_tb(error.__traceback__)[-1]
                for name, _ in subcases(case, role_):
                    if not any(r["subcase"] == name and r["case"] == case and r["role"] == role_
                               for r in run.data["rows"][first_row:]):
                        run.row(case, name, role_, harness, "not_run", f"case {case} {name}",
                                reason=f"the driver stopped: {type(error).__name__}: {error}"
                                       f" (tests/live.py:{where.lineno} {where.name})")
            _file_evidence(run, first_row)
        run.checkpoint()
    run.data["summary"] = run.summary(selected)
    if EVIDENCE["rodeo_tabs"] is not None:
        run.data["rodeo_tabs"] = {k: EVIDENCE["rodeo_tabs"].get(k) for k in ("result", "rodeo_version", "landed")}
    run.checkpoint()
    return run.data, run.exit_code(selected)


# The qualification report (phase_8.md §5.4): docs/qualification/<harness>.md from the runs' JSON evidence.

# A pass counts only with the fact the current judgement needs (@architect-2): older drivers passed these rows on
# weaker evidence, and the report shows that gap instead of the pass.
PROOF_NEEDED = {(1, "startup-dialog"): ("seen",), (2, "idle-report"): ("turn_completed",),
                (2, "operator-commands"): ("member_briefed",), (4, "owner-interrupt"): ("next-delivery", "trials"),
                (5, "blocked"): ("action_count",), (6, "operator-switch"): ("switched",), (6, "resume"): ("replaced",),
                (9, "guarded"): ("member_say_id",),     # a compound say's "exit 0" may have run unsandboxed
                (6, "member-switch"): ("switched",), (10, "idle-latency"): ("trials",)}

def _cycled(proof):
    return proof.get("down_exit") == 0 and proof.get("up_exit") == 0 and proof.get("replaced") is True


# A stored pass is judged again by today's rule where its proof holds the facts (@reviewer): an older proof with
# switched but no successful replacement stays unproved.
def judge_before_esc(before):
    """A between-turns owner-interrupt precondition, from the snapshot taken just before Esc: the operator is
    working, its live app selects the rule, the report is still queued in Factory, and none of it is on screen."""
    return (isinstance(before, dict) and before.get("state") == "working" and _app_selects(before.get("app"))
            and (before.get("outcome") or ("",))[0] == "queued" and before.get("marker_on_screen") is False)


def judge_between_mid_turn(p):
    """case 4 mid-turn for a between-turns operator (keeper.md §9): both reports held while it worked and neither on
    screen before the release, both delivered to the subject in a later turn than the original one, each acted on
    once, in committed order, after the original task finished, and the live app selected the rule."""
    deliveries, before = p.get("deliveries") or {}, p.get("before_release") or {}
    later = all(isinstance(d.get("turn"), int) and isinstance(p.get("turn_at_start"), int)
                and d["turn"] > p["turn_at_start"] and d.get("o") == "delivered" and d.get("term") == p.get("subject")
                for d in deliveries.values()) and len(deliveries) == 2
    return bool(p.get("between_turns") and p.get("app_selects_between_turns") and p.get("held_while_working")
                and before.get("second_on_screen") is False and before.get("third_on_screen") is False and later
                and p.get("second_count") == 1 and p.get("third_count") == 1
                and p.get("order") == ["finished", *(p.get("committed_order") or ["?"])])


def _esc_trials_hold(proof):
    """case 4 owner-interrupt by today's rule: both Esc trials delivered M1 while the subject worked, found it still
    pending with the subject working at the moment of Esc, and held. The imported Claude Code trials (their own
    runner, checked by @architect-2) carry "trials" instead."""
    if "trials" in proof:
        return True
    trials = [proof.get(t) for t in ("next-delivery", "owner-line")]
    if not all(isinstance(t, dict) for t in trials):
        return False
    if any(t.get("between_turns") for t in trials):  # the report was Factory's at Esc, then acted on once
        return all(t.get("between_turns") and judge_before_esc(t.get("before_esc")) and t.get("holds") is True
                   for t in trials) and trials[1].get("held_behind_owner_line") is True
    return all(t.get("working_when_delivered") is True and t.get("pending_and_working_at_esc") is True
               and t.get("holds") is True for t in trials)


REJUDGE = {(6, "resume"): _cycled, (6, "member-switch"): lambda p: p.get("switched") is True and _cycled(p),
           (4, "owner-interrupt"): _esc_trials_hold,
           # both queued observations, required since 7038415 (@architect-2)
           (7, "pending-restart"): lambda p: p.get("queued_blocked_before_kill") is True
           and p.get("queued_after_restart") is True,
           # the queued message claimed under the new image, required since 7038415
           (7, "upgrade"): lambda p: p.get("queued_claimed_under") is not None
           and p.get("queued_claimed_under") == p.get("new_version"),
           # say returned at its wait, not at once, required since 7038415
           (10, "held-sender-exit"): lambda p: isinstance(p.get("say_seconds"), (int, float))
           and p["say_seconds"] >= SAY_WAIT - 1}

# A Codex operator takes Factory's messages between turns from 539acb1 (keeper.md §9): its case 4 rows, and any
# upgrade with it as operator, count only with the between-turns facts (@architect-2). Older steering passes stay
# in the report as history, shown unproved.
BETWEEN_TURNS = {(4, "mid-turn"): judge_between_mid_turn,
                 (4, "owner-interrupt"): lambda p: _esc_trials_hold(p) and all(
                     isinstance(p.get(t), dict) and p[t].get("between_turns") for t in ("next-delivery", "owner-line")),
                 (7, "upgrade"): judge_between_upgrade}


def _between_needed(case, subcase, role, harness, proof):
    if (case, subcase) not in BETWEEN_TURNS:
        return False
    if case == 4:
        return between_turns(harness, role)
    operator = harness if role == "operator" else proof.get("peer")
    return between_turns(operator, "operator")


REPORT_BEGIN, REPORT_END = "<!-- qualify report: generated, do not edit -->", "<!-- end of generated report -->"


def _commit_rank(head):
    """How far along this branch's history a run's source head is (newer is larger); unknown heads rank first."""
    if not head:
        return -1
    done = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count", head], capture_output=True, text=True,
                          timeout=30, check=False)
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else -1


def report(paths, out):
    """For each role, harness, case and subcase keep the row of the newest attempt (by UTC, then source head) and
    write one Markdown report per harness plus trust-entries.md; text outside the generated block is kept, and
    trust entries already listed stay listed (a partial import must not drop one the owner still has to remove)."""
    runs, chosen, trust, older = [], {}, set(), {}
    paths = [*paths, *sorted(str(p) for p in (ROOT / "docs" / "qualification" / "imported").glob("*.json"))]
    loaded, snapshots = [], {}
    for path in dict.fromkeys(paths):
        data = json.loads(pathlib.Path(path).read_text())
        if not isinstance(data, dict) or data.get("mode") != "real" or "rows" not in data:
            continue                                  # a check's results, or a fixture run
        for entry in data.get("trust_entries", []):  # every snapshot's cleanup entries count
            trust.add((entry["harness"], entry["file"], entry["directory"]))
        if not data.get("run_id"):
            loaded.append(data)
            continue
        best = snapshots.get(data["run_id"])          # checkpoints are cumulative: keep the most complete one
        if best is None or (len(data["rows"]), data.get("utc") or "") > (len(best["rows"]), best.get("utc") or ""):
            snapshots[data["run_id"]] = data
    for data in [*loaded, *snapshots.values()]:
        runs.append(data)
        rank = (data.get("utc") or "", _commit_rank(data.get("source_head")))   # newest attempt by UTC (§1)
        for row in data["rows"]:
            key = (row["role"], row["harness"], row["case"], row["subcase"])
            same_run = key in chosen and chosen[key][2] is data     # a repeated --case: its last row stands
            if key not in chosen or rank >= chosen[key][0]:
                if key in chosen and not same_run:
                    older.setdefault(key, []).append(chosen[key])
                chosen[key] = (rank, row, data)
            elif not same_run:
                older.setdefault(key, []).append((rank, row, data))
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for harness in QUALIFY_HARNESSES:
        mine = {k: v for k, v in chosen.items() if k[1] == harness}
        if not mine:
            continue
        lines = [REPORT_BEGIN, "", f"Generated by `tests/live.py report` from {len(runs)} real runs. A result marked"
                 " unproved passed under an older driver's weaker check and needs a run at a current head.", ""]
        for role in ("operator", "member"):
            rows = {k: v for k, v in mine.items() if k[0] == role
                    and (k[2] not in CASES or k[3] in {n for n, _ in subcases(k[2], role)})}   # in today's table
            if not rows:
                continue
            versions = sorted({(d.get("source_head") or "?")[:7] + f" (Factory {d.get('factory_version')},"
                               f" {d.get('hc_version')}, {d.get('rodeo_version')}, {d.get('utc', '')[:10]})"
                               for _, _, d in rows.values()})
            profiles = sorted({f"{p.get('version')}, model {p.get('model')}" for _, _, d in rows.values()
                               for p in d.get("profiles", []) if p["role"] == role and p["harness"] == harness})
            lines += [f"## As {role}", "", f"- Harness: {'; '.join(profiles) or 'unknown'}",
                      f"- Runs: {'; '.join(versions)}", "", "| Case | Subcase | Result | Observed or reason | Run |",
                      "|---|---|---|---|---|"]
            open_rows = []
            def place(key):
                names = [n for n, _, _ in CASES.get(key[2], ())]
                return (key[2], names.index(key[3]) if key[3] in names else len(names))
            for key in sorted(rows, key=place):
                case, subcase = key[2], key[3]
                _, row, data = rows[key]
                proof = row.get("proof") if isinstance(row.get("proof"), dict) else {}
                status = row["status"]
                needed = PROOF_NEEDED.get((case, subcase))
                if status == "pass" and needed and not any(key in proof for key in needed):
                    status = "unproved (an older driver's weaker check)"
                elif status == "pass" and (case, subcase) in REJUDGE and not REJUDGE[(case, subcase)](proof):
                    status = "unproved (its proof does not meet today's check)"
                elif status == "pass" and _between_needed(case, subcase, role, harness, proof) \
                        and not BETWEEN_TURNS[(case, subcase)](proof):
                    status = "unproved (not yet proved between turns, keeper.md §9)"
                text = str(row.get("observed") or row.get("reason") or "")
                false = [k for k, v in proof.items() if v is False]
                if row.get("reason") == "see proof" and false:   # name what the proof found false
                    text += ": " + ", ".join(false)
                text = " ".join(text.split()).replace("|", "/")[:300]
                if proof.get("peer"):
                    text += ("; " if text else "") + f"partner {proof['peer']}"
                run_head = (data.get("source_head") or "?")[:7]
                lines.append(f"| {case} | {subcase} | {status} | {text} | {run_head} |")
                measured_only = dict(subcases(case, role)).get(subcase, False) if case in CASES else False
                if status not in ("pass", "measured") and not measured_only:   # a measurement never decides
                    open_rows.append(f"{case} {subcase}")
            for case in CASES:
                for name, measured_only in subcases(case, role):
                    if (role, harness, case, name) not in rows and not measured_only:
                        open_rows.append(f"{case} {name} (no run)")
            superseded = [f"- {key[2]} {key[3]}: {row['status']} at {(data.get('source_head') or '?')[:7]}, superseded"
                          f" by the {rows[key][1]['status']} above"
                          for key in sorted(rows, key=place) for _, row, data in older.get(key, [])
                          if row["status"] in ("pass", "fail") and row["status"] != rows[key][1]["status"]]
            lines += ["", f"Unresolved as {role}: {', '.join(open_rows) or 'none'}.", ""]
            if superseded:
                lines += [f"Superseded results as {role}:", "", *superseded, ""]
        lines.append(REPORT_END)
        target = out / f"{harness}.md"
        old = target.read_text() if target.exists() else f"# {harness} qualification\n\n{REPORT_BEGIN}\n{REPORT_END}\n"
        if REPORT_BEGIN in old and REPORT_END in old:
            head, rest = old.split(REPORT_BEGIN, 1)
            new = head + "\n".join(lines) + rest.split(REPORT_END, 1)[1]
        else:
            new = old.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"
        target.write_text(new)
        written.append(str(target))
    entries = ["# Trust entries added by qualification runs", "",
               "Claude Code and Codex may record per-directory trust for each QA scratch project",
               "(@operator's decision, phase_8.md §2). Every entry a run may have added, for the owner to remove:", "",
               "| Harness | File | Directory |", "|---|---|---|"]
    listed = out / "trust-entries.md"
    if listed.exists():
        trust |= set(re.findall(r"^\| (\S+) \| `([^`]+)` \| `([^`]+)` \|$", listed.read_text(), re.M))
    entries += [f"| {h} | `{f}` | `{d}` |" for h, f, d in sorted(trust)]
    listed.write_text("\n".join(entries) + "\n")
    return written + [str(out / "trust-entries.md")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("name", choices=[*CHECKS, *LOGIN_CHECKS, "all", "login"])
    check.add_argument("--json", help="also write the results to this file")
    fix = sub.add_parser("fixtures")
    fix.add_argument("--out", default=str(pathlib.Path(__file__).with_name("hc_fixtures.json")))
    sub.add_parser("smoke")
    qual = sub.add_parser("qualify", help="the live qualification of phase_8.md: a harness, or cross")
    qual.add_argument("target", help=f"one of {', '.join(QUALIFY_HARNESSES)}, or cross")
    qual.add_argument("--role", choices=["operator", "member"])
    qual.add_argument("--case", type=int, action="append", dest="cases", help="1 to 10; repeat for several")
    qual.add_argument("--json", help="checkpoint the evidence to this file after each case")
    qual.add_argument("--fixture", action="store_true", help="fake agents only: never a qualification")
    rep = sub.add_parser("report", help="write docs/qualification/<harness>.md from qualify --json evidence")
    rep.add_argument("evidence", nargs="+", help="qualify --json files from real runs")
    rep.add_argument("--out", default=str(ROOT / "docs" / "qualification"))
    if __package__:
        from . import e2e
    else:
        import e2e
    e2e.add_arguments(sub.add_parser("e2e"))
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))   # cleanup (with-blocks, atexit) runs on SIGTERM too
    if args.command == "report":
        for path in report(args.evidence, args.out):
            print(path)
        return
    if args.command == "qualify":
        try:
            evidence, code = qualify(args.target, args.role, args.cases, args.json, args.fixture)
        except ValueError as error:
            parser.exit(2, f"qualify: {error}\n")
        print(json.dumps(evidence, indent=2))
        sys.exit(code)
    if args.command == "e2e":
        if args.rehearse and args.tracker != "file":
            parser.error("the rehearsal uses the file tracker")
        if args.budget is not None and not 0 < args.budget < float("inf"):
            parser.error("--budget must be positive and finite")
        if not RODEO:
            parser.exit(2, "rodeo not found (set RODEO_BIN)\n")
        try:
            return e2e.run(args, Core, Rodeo, ROOT / "skill", shutil.which(HC) or HC, sys.modules[__name__])
        except (RuntimeError, HcError, HcDown, subprocess.SubprocessError) as error:
            parser.exit(1, f"FAIL {error}\n")
    if args.command == "smoke":
        try:
            smoke()
        except (RuntimeError, HcError, HcDown, subprocess.TimeoutExpired) as error:
            parser.exit(1, f"FAIL {error}\n")
        return
    if args.command == "fixtures":
        results = [fixtures(args.out)]
    else:
        every = {**CHECKS, **LOGIN_CHECKS}
        names = list(CHECKS) if args.name == "all" else list(LOGIN_CHECKS) if args.name == "login" else [args.name]
        results = []
        for n in names:   # one at a time, each result kept as it lands
            results.append(every[n]())
            if args.json:
                pathlib.Path(args.json).write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    sys.exit(main())
