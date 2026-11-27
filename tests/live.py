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


class Core:
    """A private hc core in its own temporary directory."""

    def __init__(self, tag, config="", extra_env=None):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix=f"hc-{tag}-", dir=tempfile.gettempdir()))
        d = self.dir
        for sub in ("run", "state", "config/herdr-core", "home"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(d / "run", 0o700)
        if config:
            (d / "config/herdr-core/config.toml").write_text(config)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HC_", "RODEO_")) and k != "HERDR_CONFIG_PATH"}
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
        self.argv = [exe, *args]
        self.record = qa.rpc("terminal.run", {"argv": self.argv, "cwd": str(cwd), "env": {**owner_env(), **(env or {})},
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


def harness_check(name, harnesses, body):
    """Common frame: versions, a private QA core with no draft hold, NotRun as "not run: <why>"."""
    result = {"check": name, "versions": {h: harness_version(h) for h in harnesses},
              "os": f"{os.uname().sysname} {os.uname().release}"}
    missing = [h for h, v in result["versions"].items() if v is None]
    if len(missing) == len(harnesses):
        result["result"] = f"not run: {', '.join(missing)} not on PATH"
        return result
    try:
        with Core(name.lower().replace("-", "").replace("+", ""), config="draft_wait = 0\n") as qa:
            body(qa, result)
    except NotRun as why:
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
    """check rodeo_tabs (phase_8.md §2): prefix+1..9 selects a tab by its place in the layout doc, so a marker
    typed after it reaches only that tab's focused pane."""
    result = {"check": "rodeo_tabs", "rodeo": RODEO}
    if not RODEO or not os.path.exists(RODEO):
        result["result"] = "not run: rodeo not found (set RODEO_BIN)"
        return result
    result["rodeo_version"] = subprocess.run([RODEO, "--version"], capture_output=True, text=True,
                                             check=False).stdout.strip()
    with Core("tabsqa") as qa, Core("tabsdrv") as drv:
        op, lead, rev = Probe(qa, "op"), Probe(qa, "lead"), Probe(qa, "rev")
        space = "sp" + "7" * 32

        def block(n, probe, title):
            return {"id": "p" + str(n) * 32, "type": "block", "title": title, "content": {"terminal": probe.address}}
        doc = {"spaces": [{"id": space, "name": "tabs", "folder": str(qa.dir), "tabs": [
            {"id": "tb" + "1" * 32, "name": "operator", "root": {"id": "s" + "1" * 32, "type": "row",
                                                                "children": [block(1, op, "@operator")]}},
            {"id": "tb" + "2" * 32, "name": "team", "root": {"id": "s" + "2" * 32, "type": "row",
                                                            "children": [block(2, lead, "@lead"),
                                                                         block(3, rev, "@rev")]}}]}]}
        qa.rpc("doc.set", {"name": "herdr-app", "value": doc, "version": 0})
        for probe, tab, name in ((op, "1", "@operator"), (lead, "2", "@lead"), (rev, "2", "@rev")):
            qa.rpc("terminal.label", {"terminal": probe.address, "labels": {
                "herdr.space": space, "herdr.tab": "tb" + tab * 32, "herdr.seen": "0", "name": name}})
        rodeo = Rodeo(qa, drv, "unset_env")
        try:
            if rodeo.wait_for(["operator", "team"]) is None:
                result["result"] = "fail: rodeo did not render both tabs"
                return result
            time.sleep(2)
            seen = {}
            for tab, marker in (("2", "x"), ("1", "y"), ("2", "z")):
                before = {name: len(p.text()) for name, p in (("op", op), ("lead", lead), ("rev", rev))}
                rodeo.keys("ctrl+b")
                time.sleep(0.2)
                rodeo.keys(tab)
                time.sleep(0.6)
                rodeo.keys(marker)
                time.sleep(0.8)
                got = [name for name, p in (("op", op), ("lead", lead), ("rev", rev))
                       if marker in p.text()[before[name]:]]
                seen[f"prefix+{tab} then {marker}"] = got
            result["landed"] = seen
            ok = (seen["prefix+2 then x"] == ["lead"] and seen["prefix+1 then y"] == ["op"]
                  and seen["prefix+2 then z"] == ["lead"])
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
        """The fake operator quits to its loop: it reports state=clear, as a harness returning to its shell."""
        qa.rpc("terminal.send", {"terminal": terminal, "text": "FAKE: quits"}, timeout=60)

    def restart(self, qa, terminal):
        """Start the fake operator again in its open pane (its loop waits for Enter)."""
        qa.rpc("terminal.keys", {"terminal": terminal, "keys": ["enter"]})


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

    def operator_argv(self, harness):
        """The operator as the owner would start it, with the profile's model pinned (as Factory pins a member's)."""
        return [shutil.which(harness), "--model", self.model(harness), *harness_table()[harness]["permissions"]["full"]]

    def act_append(self, path, token):
        return (f"Using your shell tool, append one line containing exactly {token} to the file {path}"
                " (create it if needed). Then reply with the single word done.")

    def act_run(self, path, command):
        return (f"Using your shell tool, run exactly this command: {command}; echo \"exit $?\" >> {path}"
                " Then reply with the single word done.")

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
        for _ in range(2):
            qa.rpc("terminal.keys", {"terminal": terminal, "keys": ["ctrl+c"]})
            time.sleep(1)
        if not self.team.until(lambda: self.team.state(terminal) is None, 15):
            qa.rpc("terminal.send", {"terminal": terminal, "text": {"claude": "/exit"}.get(self.team.operator_harness,
                                                                                         "/quit")}, timeout=60)

    def restart(self, qa, terminal):
        """Type the same command at the shell prompt in that pane."""
        if not self.team.operator_command:
            raise NotRun("this team's operator was not started from a shell")
        self.team.send(terminal, self.team.operator_command)
        Harness.attach(qa, self.team.operator_harness, terminal).settle(timeout=120)


class QATeam:
    """A scratch project, a private QA core, a team with an operator terminal, and Factory's real commands from a
    private copy of the skill (phase_8.md §2)."""

    def __init__(self, adapter_class, operator, members, draft_wait=None, permissions="full", operator_shell=False,
                 startup_dialog=None):
        self.operator_harness, self.members, self.permissions = operator, members, permissions
        self.startup_dialog = startup_dialog    # fixture only: the harness whose fake shows its declared dialog
        self.real = adapter_class is RealAdapter
        home = os.path.expanduser("~")
        extra = {"HOME": home} if self.real else None   # a real harness needs the owner's login (testing.md §6)
        self.qa = Core("qualify", config=f"draft_wait = {draft_wait}\n" if draft_wait is not None else "",
                       extra_env=extra)
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix=QA_PREFIX, dir=tempfile.gettempdir()))
        self.bin, self.home = self.dir / "bin", (pathlib.Path(home) if self.real else self.dir / "home")
        self.bin.mkdir()
        self.skill = self.dir / "skill"
        shutil.copytree(ROOT / "skill", self.skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.script = self.skill / "scripts" / "factory.py"
        self.work = self.dir / "work"
        self.work.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)], check=True)
        self.adapter = adapter_class(self)
        self.adapter.prepare({operator, *members.values()})
        (self.work / "FACTORY.md").write_text("Qualification team: answer every request exactly as asked.\n")
        lines = [f'[operator]\nharness = "{operator}"\n']
        for name, harness in members.items():
            lines.append(f'[members.{name}]\nharness = "{harness}"\nmodel = "{self.adapter.model(harness)}"\n'
                         f'permissions = "{permissions}"\n')
        (self.work / "factory.toml").write_text("\n".join(lines))
        subprocess.run(["git", "-C", str(self.work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.work), "-c", "user.name=qa", "-c", "user.email=qa@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "qa"], check=True)
        # What Factory's commands see, from the driver or from the operator's own tool: the private Factory cache
        # (never the owner's), the fixture's wrappers first on PATH, and the fake Codex home.
        shared = {"HC_BIN": shutil.which(HC) or HC, "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
                  "XDG_CACHE_HOME": str(self.dir / "cache"), "HOME": str(self.home)}
        if not self.real:
            shared["CODEX_HOME"] = str(self.home / ".codex")
        params = {"argv": self.adapter.operator_argv(operator), "cwd": str(self.work), "env": dict(shared)}
        if not self.real:
            params["env"]["FAKE_LOG"] = str(self.dir / "operator-input.jsonl")   # every input the fake received
            params["env"]["FAKE_ECHO"] = "1"
            params["env"]["CODEX_THREAD_ID"] = f"thread-{token().lower()}"   # the fake operator's conversation
        self.operator_command = None
        if self.real and operator_shell:    # an owner's shell: quitting the harness leaves the pane at its prompt
            self.operator_command = shlex.join(params["argv"])
            params["argv"] = ["bash", "--norc", "--noprofile", "-i"]
            params["env"]["PS1"] = "qa$ "
        self.operator = self.qa.rpc("terminal.run", params)["address"]
        if self.operator_command:
            self.send(self.operator, self.operator_command)
        self.env = {**self.qa.env, **shared, "HC_TERMINAL_ID": self.operator}

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

    def receipt(self, name):
        return self.dir / f"receipt-{name}-{random.randrange(16 ** 6):06x}.txt"

    def close(self):
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

EVIDENCE = {"teams": [], "tokens": []}   # the current case's closed teams' diagnostics, and its test markers
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


def _team_for(adapter_class, role, harness, **options):
    """A two-member-or-less team where the profile's harness has its role (phase_8.md §1)."""
    peer = _peer(role, harness)
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
    quiet = _team_quiet(team, hold=8, timeout=300)    # the startup reports handled before any case samples
    team.ready_states = {"operator": team.state(team.operator), "quiet_after_up": quiet,
                         **{name: team.state(team.member(name)) for name in team.members if team.member(name)}}
    if quiet is None:
        waiting = waiting or ["the team (not quiet for 8 s within 300 s)"]
    if waiting:
        for name in names:
            run.row(case, name, role, harness, "not_run", f"case {case} {name}",
                    reason=f"never idle after up: {', '.join(waiting)}")
        return False
    return True


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


def _trust_entries(run, team):
    """The per-directory trust a real Claude Code or Codex start may add for this run's scratch project, listed
    for the owner to remove (@operator's decision; phase_8.md §2)."""
    entries = run.data.setdefault("trust_entries", [])
    for harness in {team.operator_harness, *team.members.values()} & set(TRUST_FILES):
        entry = {"harness": harness, "file": TRUST_FILES[harness], "directory": str(team.work)}
        if entry not in entries:
            entries.append(entry)
    run.checkpoint()


def _member_says(team, text, timeout=300, record=None):
    """Have the member itself run say to the operator through its own tool, with text in a file."""
    member = team.member("lead-dev")
    body = team.dir / f"report-{random.randrange(16 ** 6):06x}.md"
    body.write_text(text)
    out = team.dir / f"say-out-{random.randrange(16 ** 6):06x}.txt"     # the say's own output: its message ID
    command = (f"python3 {shlex.quote(str(team.script))} say operator --file {shlex.quote(str(body))}"
               f" > {shlex.quote(str(out))} 2>&1")
    done = team.receipt("member-say")
    team.send(member, team.adapter.act_run(done, command))
    result = team.until(lambda: done.exists() and "exit" in done.read_text() and done.read_text().strip(), timeout)
    team.last_say = out.read_text() if out.exists() else None
    if record is not None:
        record["out"] = team.last_say
    return result


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


def judge_dialog(declared, seen, ready):
    """declared: the table's [text, keys] dialogs; seen: the declared text observed on the subject's screen during
    this fresh start, or None; ready: the start finished. A known prompt answered by Factory passes once the start
    finishes; an owner-only prompt (no keys) passes when it is left. A declared dialog that never showed proves
    nothing. -> (status, observed, reason)."""
    keys = {text: k for text, k in declared}
    if seen is None:
        if not declared:
            return "pass", "no startup dialog declared; none of the known prompts appeared", None
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


def judge_switch(before, after):
    """A native conversation switch is shown by the harness's own session variable changing in its tool's
    environment: both values seen, and different."""
    if not before or not after:
        return False, "the session variable was not seen before and after"
    return before != after, None if before != after else "the session variable did not change"


def _operator_start(team, run, case, role, harness, names):
    """The operator past its own startup dialogs before the owner's first input, answered on the second poll as up
    does (a fake one shows one only for case 1)."""
    if team.real:
        _trust_entries(run, team)
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
    exits, tok = team.receipt("operator-commands"), token()
    member_receipt = team.receipt("member")
    py = f"python3 {shlex.quote(str(team.script))}"
    message = shlex.quote(team.adapter.act_append(member_receipt, tok))
    outs = {name: team.dir / f"out-{name}-{token().lower()}.txt" for name in ("up", "say", "status", "inbox")}
    commands = {"up": f"{py} up", "say": f"{py} say lead-dev {message}", "status": f"{py} status",
                "inbox": f"{py} inbox --for operator"}
    prompt = "\n".join(team.adapter.act_run(exits, f"{commands[name]} > {outs[name]} 2>&1") for name in commands)
    view = _rodeo(team, ["operator"])
    view.keys("ctrl+b")
    time.sleep(0.2)
    view.keys("1")
    time.sleep(0.8)
    view.focused_terminal = team.operator
    start = time.monotonic()
    view.paste(prompt)
    lines = team.until(lambda: exits.exists() and len(exits.read_text().split()) >= 8 and exits.read_text(), 900)
    codes = [line for line in (lines or "").splitlines() if line.startswith("exit")]
    reached = bool(codes == ["exit 0"] * 4
                   and team.until(lambda: member_receipt.exists() and tok in member_receipt.read_text(), 300))
    full = {name: path.read_text() if path.exists() else None for name, path in outs.items()}

    member = team.member("lead-dev")
    box = factory_log(team).read_box()[1] if member else None
    briefed = bool(box) and member in box.briefed()
    to_operator = sorted({ident for (ident, to) in box.outcomes if to == "operator"}) if box else []
    listed = sorted({int(n) for n in re.findall(r"^\[Message (\d+) ", full["inbox"] or "", re.M)})
    inbox_ok = bool(full["inbox"]) and set(listed) <= set(to_operator) and (
        bool(listed) or "no messages for @operator" in full["inbox"])
    status_ok = "@lead-dev" in (full["status"] or "")
    proof = {"exits": codes, "member_acted": reached, "member_briefed": briefed, "status_names_member": status_ok,
             "inbox_ids": listed, "operator_ids_in_log": to_operator,
             "exits_by_command": dict(zip(commands, codes)), "peer": peer,
             "prompted_by": "the owner's paste and Enter through rodeo into a fresh operator (prefix+1)"}
    ok = codes == ["exit 0"] * 4 and reached and briefed and status_ok and inbox_ok
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
        found = _say_id(team.last_say or "")
        ident = int(found.group(1)) if found else None
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
        said = _member_says(team, team.adapter.act_append(receipt, tok))
        new = team.until(team.keeper, 60)
        acted = team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
        proof = {"old_keeper_finished": bool(gone), "new_keeper": bool(new) and new != old, "member_say": said,
                 "guarded_args": harness_table()[harness]["permissions"]["guarded"], "peer": peer}
        ok = bool(gone) and bool(new) and new != old and bool(acted) and "exit 0" in (said or "")
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
        status, observed, reason = judge_dialog(declared, observed_dialog, bool(ready))
        run.row(1, "startup-dialog", role, harness, status,
                "a declared startup dialog is seen and answered on its second poll, or left to the owner; none is"
                " invented", observed=observed, reason=reason,
                proof={"declared": [text for text, _ in declared], "seen": observed_dialog,
                       "start_finished": bool(ready),
                       "handled_by": "the driver's operator watcher, as up does" if role == "operator" else "up",
                       "mode": team.adapter.mode})
        if not ready:
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


def case_7(run, role, harness, adapter_class):
    """suffix and multiline; the rest of case 7 is not built yet (phase_8.md §3.7)."""
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
        _not_built(run, 7, role, harness, ["two-senders", "pending-restart", "held-quiet", "upgrade"])


def case_10(run, role, harness, adapter_class):
    """idle-latency with its measured times; held-sender-exit and crash are not built yet (phase_8.md §3.10)."""
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
        _not_built(run, 10, role, harness, ["held-sender-exit", "crash"])


def _note(team, text):
    path = team.dir / f"note-{random.randrange(16 ** 6):06x}.md"
    path.write_text(text)
    return path


def _rodeo(team, titles):
    """rodeo on a driver core, viewing the QA core's team tab once its titles render; closed with the team."""
    if getattr(team, "view", None) is None:
        team.drv = Core("qualifydrv")
        team.view = Rodeo(team.qa, team.drv)
        team.view.focused_terminal = None
        if team.view.wait_for(titles, timeout=30) is None:
            raise NotRun(f"rodeo did not show {titles}")
        time.sleep(1)
    return team.view


def case_5(run, role, harness, adapter_class):
    """blocked (phase_8.md §3.5): nothing typed while blocked, one note, the queued message delivered once after
    the owner answers through rodeo, with no Factory command between."""
    team, peer = _team_for(adapter_class, role, harness)
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
    record = log.read_box()[1].outcome(int(ident[1]), name)
    return record["o"], record.get("why")


def _say_id(out):
    found = re.search(r"^message (\d+)$", out, re.M)
    return found


def case_8(run, role, harness, adapter_class):
    """dead-member, operator-gone and same-pane; unconfirmed-brief and quit-during-hold are not built yet
    (phase_8.md §3.8)."""
    team, peer = _team_for(adapter_class, role, harness, operator_shell=role == "operator")
    with team:
        names = [n for n, _ in subcases(8, role)]
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
                _not_built(run, 8, role, harness, ["unconfirmed-brief", "quit-during-hold"])
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
            _not_built(run, 8, role, harness, ["unconfirmed-brief", "quit-during-hold"])
            return
        operator = team.operator
        # operator-gone: the harness stops reporting in an open pane; messages queue until up.
        team.adapter.quit(team.qa, operator)
        stopped = team.until(lambda: team.state(operator) is None, 120)
        receipt, toks, ids = team.receipt("gone"), [token(), token()], []
        for tok in toks:
            code, out = team.call("say", "operator", "--file", str(_note(team, team.adapter.act_append(receipt, tok))),
                                  terminal=team.member("lead-dev"), timeout=60)
            ids.append(_say_id(out))
        latched = team.until(lambda: (team.get(operator).get("labels") or {}).get("factory.kind")
                             == "operator-lost", 60)
        team.adapter.restart(team.qa, operator)
        back = team.until(lambda: team.state(operator) in READY, 120)
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
        team.adapter.quit(team.qa, operator)
        team.until(lambda: team.state(operator) is None, 60)
        team.adapter.restart(team.qa, operator)
        team.until(lambda: team.state(operator) in READY, 60)
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
        _not_built(run, 8, role, harness, ["quit-during-hold"])


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
        said = team.receipt("member-say")
        command = f"python3 {shlex.quote(str(team.script))} say operator --file {shlex.quote(str(report))}"
        team.send(member, team.adapter.act_run(said, command))
        log = factory_log(team)
        claimed = team.until(lambda: next((ident for (ident, to), rec in log.read_box()[1].outcomes.items()
                                           if rec["o"] == "sending" and to == "operator"), None), 300)
        if not claimed:                                 # the member never said: nothing about the draft hold is shown
            run.row(3, "draft", role, harness, "not_run", "a report held by the owner's draft",
                    proof={"member_say": said.read_text().strip() if said.exists() else None,
                           "member_state": team.state(member), "peer": peer},
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
            _not_built(run, 3, role, harness, ["draft-cleared"])


def case_6(run, role, harness, adapter_class):
    """resume and member-switch for a member; operator-switch for an operator (phase_8.md §3.6)."""
    team, peer = _team_for(adapter_class, role, harness)
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
        idle()
        got = recall()
        cycled = launch["down_exit"] == 0 and launch["up_exit"] == 0 and launch["replaced"]
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
        idle()
        argv = " ".join(team.get(team.member("lead-dev")).get("argv") or [])
        launch["resumed_conversation"] = ("launch" if before and before in argv       # the ID the new terminal's argv
                                          else "switched" if after and after in argv else "neither")   # carries
        launch["ids"] = {"launch": _alias(before), "switched": _alias(after)}
        got = recall()
        follow = harness_table()[harness].get("session_follow", True) if team.real else False
        expected = second if follow else first
        which = "second" if got == second[::-1] else "first" if got == first[::-1] else repr(got)
        status = "pass" if switched and got == expected[::-1] else "not_run" if not switched else "fail"
        run.row(6, "member-switch", role, harness, status,
                f"after a native switch (its session variable changes) and a say, up resumes the"
                f" {'switched' if follow else 'launch'} conversation (session_follow = {str(follow).lower()})",
                observed=f"recalled the {which} word", proof=dict(launch, session_follow=follow, variable=variable,
                                                               switched=switched),
                reason=None if status == "pass" else why if not switched else "another conversation")


def _ask_later(team, role, text):
    """_ask in a thread: a say to a working recipient may wait for delivery, and the case must go on meanwhile.
    box["out"] gets the say's own output (its message ID) when it returns."""
    box = {}

    def ask():
        if role == "member":
            code, out = team.call("say", "lead-dev", text, timeout=600)
            box.update(out=out, code=code)
        else:
            box["code"] = _member_says(team, text, timeout=600, record=box)
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
                found = _say_id(box.get("out") or "")
                return (int(found.group(1)) if found else None,
                        _outcome(log or factory_log(team), found, recipient) if found else None)

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
                later = _ask_later(team, role, team.adapter.act_append(work, second))
                delivered = team.until(lambda: later[1].get("out") and said(later[1])[1]
                                       and said(later[1])[1][0] == "delivered", 30)
                time.sleep(5)
                proof = {"peer": peer, "turn_at_start": turn, "second_id": said(later[1])[0],
                         "second_outcome_while_working": said(later[1])[1],
                         "working_when_delivered": team.state(subject) == "working",
                         "pending_before_esc": second not in _lines(work)}
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
                    proof["precondition"] = bool(delivered) and proof["pending_before_esc"]
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
                release.write_text("go\n")
                team.until(lambda: second in _lines(work), 300)
                if trial == "mid-turn":
                    team.until(lambda: finished in _lines(work), 300)
                team.until(lambda: team.state(subject) in READY, 120)
                time.sleep(15)                               # time for a duplicate to show
                for thread, _ in (first, later):
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
                    proof["holds"] = count == 1 and proof["owner_exact"]
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


def cross_other_version(run, fixture):
    """other-version (phase_8.md §4): with the keeper alive and a send held by the owner's draft, a second valid
    copy of the script is installed atomically at the same path. The old send finishes, the keeper restarts in the
    same pane, and a message queued after that is delivered once by the new image without a wakeup."""
    adapter_class = FixtureAdapter if fixture else RealAdapter
    team, peer = _team_for(adapter_class, "operator", "codex" if fixture else "claude", draft_wait=60)
    with team:
        _operator_tab(team)
        if not _up(team, run, "cross", "system", "system", []):
            run.row("cross", "other-version", "system", "system", "not_run", "the other-version cross case",
                    reason="the team did not come up")
            return
        keeper = team.keeper()
        old = hashlib.sha1(team.script.read_bytes()).hexdigest()[:12]
        view = _rodeo(team, ["operator", "team"])
        view.keys("ctrl+b")
        time.sleep(0.2)
        view.keys("1")
        time.sleep(0.8)
        view.focused_terminal = team.operator
        name = f"draft-{random.randrange(16 ** 6):06x}.txt"          # the owner's draft holds the first send
        draft_receipt, literal = team.dir / name, f"owner draft {token()}"
        view.type(team.adapter.draft_request(f"../{name}", literal))
        time.sleep(1)
        first, tok1 = team.receipt("first"), token()
        _member_says(team, team.adapter.act_append(first, tok1))
        log = factory_log(team)
        held = team.until(lambda: next((ident for (ident, to), rec in log.read_box()[1].outcomes.items()
                                        if rec["o"] == "sending" and to == "operator"), None), 120)
        text = team.script.read_text()                               # a second valid copy, comment-only change
        staged = team.script.with_name(".factory.py.new")
        staged.write_text(text + f"\n# qualification other-version {token()}\n")
        os.replace(staged, team.script)
        new = hashlib.sha1(team.script.read_bytes()).hexdigest()[:12]
        def keeper_screen():
            return "".join(team.qa.rpc("terminal.read", {"terminal": keeper, "lines": 0}).get("text", "").split())
        draining = team.until(lambda: f"keeper{new}installed" in keeper_screen(), 90)
        still_held = not first.exists()
        view.keys("enter")                                           # the owner sends the draft: the send finishes
        finished = team.until(lambda: first.exists() and tok1 in first.read_text(), 300)
        restarted = team.until(lambda: "keeperrestarting:newFactoryversion" in keeper_screen(), 120)
        time.sleep(3)
        second, tok2 = team.receipt("second"), token()
        _member_says(team, team.adapter.act_append(second, tok2))
        acted = team.until(lambda: second.exists() and tok2 in second.read_text(), 300)
        time.sleep(10)
        keepers = [r["address"] for r in team.qa.rpc("terminal.list", {"all": False})["terminals"]
                   if (r.get("labels") or {}).get("factory.kind") == "keeper"]
        first_outcome = log.read_box()[1].outcome(held, "operator") if held else {}
        proof = {"old_version": old, "new_version": new, "keeper_before": keeper, "keepers_after": keepers,
                 "held_send_id": held, "held_send_outcome": [first_outcome.get("o"), first_outcome.get("why")],
                 "draining_seen": bool(draining), "still_held_while_draining": still_held,
                 "old_send_finished": bool(finished), "restart_seen": bool(restarted),
                 "new_message_count": second.read_text().split().count(tok2) if second.exists() else 0,
                 "draft_exact": draft_receipt.exists() and draft_receipt.read_text() == literal + "\n",
                 "agents": "fake agents (the case is Factory's, not a harness's)" if fixture else "real", "peer": peer}
        ok = (old != new and keepers == [keeper] and bool(held) and bool(draining) and still_held and bool(finished)
              and first_outcome.get("o") == "delivered" and bool(restarted) and bool(acted)
              and proof["new_message_count"] == 1)
        run.row("cross", "other-version", "system", "system", "pass" if ok else "fail",
                "a new script under a live keeper: the held send finishes, the keeper restarts in its pane, the next"
                " message is delivered once by the new image", proof=proof, reason=None if ok else "see proof")


CROSS_FUNCTIONS = {"other-version": cross_other_version}


CASE_FUNCTIONS = {1: case_1, 2: case_2, 3: case_3, 4: case_4, 5: case_5, 6: case_6, 7: case_7, 8: case_8, 9: case_9,
                  10: case_10}


def _file_evidence(run, first_row):
    """Attach the case's team diagnostics to each of its rows (bounded; for a team per trial, in order)."""
    for row in run.data["rows"][first_row:]:
        row["diagnostics"] = EVIDENCE["teams"]


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
    if target == "cross":
        for name in CROSS_CASES:
            EVIDENCE.update(teams=[], tokens=[])
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
            EVIDENCE.update(teams=[], tokens=[])
            first_row = len(run.data["rows"])
            try:
                function(run, role_, harness, adapter_class)
            except NotRun as why:
                for name, _ in subcases(case, role_):
                    run.row(case, name, role_, harness, "not_run", f"case {case} {name}", reason=str(why))
            except (RuntimeError, HcError, HcDown, OSError, subprocess.SubprocessError) as error:
                for name, _ in subcases(case, role_):
                    if not any(r["subcase"] == name and r["case"] == case and r["role"] == role_
                               for r in run.data["rows"]):
                        run.row(case, name, role_, harness, "not_run", f"case {case} {name}",
                                reason=f"the driver stopped: {type(error).__name__}: {error}")
            _file_evidence(run, first_row)
        run.checkpoint()
    run.data["summary"] = run.summary(selected)
    run.checkpoint()
    return run.data, run.exit_code(selected)


# The qualification report (phase_8.md §5.4): docs/qualification/<harness>.md from the runs' JSON evidence.

# A pass counts only with the fact the current judgement needs (@architect-2): older drivers passed these rows on
# weaker evidence, and the report shows that gap instead of the pass.
PROOF_NEEDED = {(1, "startup-dialog"): ("seen",), (2, "idle-report"): ("turn_completed",),
                (2, "operator-commands"): ("member_briefed",), (4, "owner-interrupt"): ("next-delivery", "trials"),
                (5, "blocked"): ("action_count",), (6, "operator-switch"): ("switched",), (6, "resume"): ("replaced",),
                (6, "member-switch"): ("switched",), (10, "idle-latency"): ("trials",)}

REPORT_BEGIN, REPORT_END = "<!-- qualify report: generated, do not edit -->", "<!-- end of generated report -->"


def _commit_rank(head):
    """How far along this branch's history a run's source head is (newer is larger); unknown heads rank first."""
    if not head:
        return -1
    done = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count", head], capture_output=True, text=True,
                          timeout=30, check=False)
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else -1


def report(paths, out):
    """For each role, harness, case and subcase keep the row of the newest run (by source head, then time) and
    write one Markdown report per harness plus trust-entries.md; text outside the generated block is kept."""
    runs, chosen, trust, older = [], {}, set(), {}
    paths = [*paths, *sorted(str(p) for p in (ROOT / "docs" / "qualification" / "imported").glob("*.json"))]
    for path in dict.fromkeys(paths):
        data = json.loads(pathlib.Path(path).read_text())
        if not isinstance(data, dict) or data.get("mode") != "real" or "rows" not in data:
            continue                                  # a check's results, or a fixture run
        runs.append(data)
        rank = (_commit_rank(data.get("source_head")), data.get("utc") or "")
        for entry in data.get("trust_entries", []):
            trust.add((entry["harness"], entry["file"], entry["directory"]))
        for row in data["rows"]:
            key = (row["role"], row["harness"], row["case"], row["subcase"])
            if key not in chosen or rank >= chosen[key][0]:
                if key in chosen:
                    older.setdefault(key, []).append(chosen[key])
                chosen[key] = (rank, row, data)
            else:
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
                text = str(row.get("observed") or row.get("reason") or "")
                false = [k for k, v in proof.items() if v is False]
                if row.get("reason") == "see proof" and false:   # name what the proof found false
                    text += ": " + ", ".join(false)
                text = " ".join(text.split()).replace("|", "/")[:300]
                if proof.get("peer"):
                    text += ("; " if text else "") + f"partner {proof['peer']}"
                run_head = (data.get("source_head") or "?")[:7]
                lines.append(f"| {case} | {subcase} | {status} | {text} | {run_head} |")
                if status != "pass" and status != "measured":
                    open_rows.append(f"{case} {subcase}")
            for case in CASES:
                for name, _ in subcases(case, role):
                    if (role, harness, case, name) not in rows:
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
    entries += [f"| {h} | `{f}` | `{d}` |" for h, f, d in sorted(trust)]
    (out / "trust-entries.md").write_text("\n".join(entries) + "\n")
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
    args = parser.parse_args(argv)
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
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
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
    main()
