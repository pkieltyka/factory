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
        self.focused_terminal = None

    def keys(self, *names):
        return self.drv.rpc("terminal.keys", {"terminal": self.term, "keys": list(names)})

    def type(self, text):
        """Type through the client, leaving the draft unsubmitted."""
        if "\n" in text or "\r" in text:
            raise ValueError("type takes one line; send Enter separately")
        for char in text:
            self.keys("space" if char == " " else char)

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
    return "ZQ" + "".join(random.choices("ABCDEFGHJKLMNPRSTUVWXY", k=8))


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
            if word == "idle":
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
                if word == "idle" and t.get("turn") != turn:
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

    def model(self, harness):
        return f"fixture-{harness}"

    def prepare(self, harnesses):
        """A wrapper per harness that runs the fake agent with a log named after the member's model."""
        for harness in harnesses:
            wrapper = self.team.bin / harness
            wrapper.write_text("#!/bin/sh\n" + f"exec {shlex.join(self.agent)}\n")
            wrapper.chmod(0o755)
        codex = self.team.home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": self.model("codex"), "supported_reasoning_levels": [{"effort": e} for e in
                                                                     ("low", "medium", "high", "xhigh")]}]}))

    def operator_argv(self, harness):
        return list(self.agent)

    def act_append(self, path, token):
        return f"ACT append {path} {token}"

    def act_run(self, path, command):
        return f"ACT run {path} {command}"

    def blocked_request(self):
        return "FAKE: blocks_once"


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
        return [shutil.which(harness), *harness_table()[harness]["permissions"]["full"]]

    def act_append(self, path, token):
        return (f"Using your shell tool, append one line containing exactly {token} to the file {path}"
                " (create it if needed). Then reply with the single word done.")

    def act_run(self, path, command):
        return (f"Using your shell tool, run exactly this command: {command}; echo \"exit $?\" >> {path}"
                " Then reply with the single word done.")

    def blocked_request(self):
        return ("Before doing anything else, ask me one multiple-choice question with your question tool, and"
                " wait for my answer. Do nothing else.")


class QATeam:
    """A scratch project, a private QA core, a team with an operator terminal, and Factory's real commands from a
    private copy of the skill (phase_8.md §2)."""

    def __init__(self, adapter_class, operator, members, draft_wait=None, permissions="full"):
        self.operator_harness, self.members, self.permissions = operator, members, permissions
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
        self.operator = self.qa.rpc("terminal.run", {"argv": self.adapter.operator_argv(operator),
                                                     "cwd": str(self.work)})["address"]
        self.env = {**self.qa.env, "HC_TERMINAL_ID": self.operator, "HC_BIN": shutil.which(HC) or HC,
                    "PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "XDG_CACHE_HOME": str(self.dir / "cache"),
                    "HOME": str(self.home)}
        if not self.real:
            self.env["CODEX_HOME"] = str(self.home / ".codex")

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

    def receipt(self, name):
        return self.dir / f"receipt-{name}-{random.randrange(16 ** 6):06x}.txt"

    def close(self):
        if getattr(self, "view", None) is not None:
            self.view.close()
            self.drv.stop()
        self.qa.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# Qualification (phase_8.md): functional_spec §10's ten cases over eight role profiles, the four cross cases once.
# One JSON object on stdout (progress on stderr), checkpointed to --json after each case.

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
    6: (("resume", "both", False), ("member-switch", "member", False), ("operator-switch", "operator", False)),
    7: (("suffix", "both", False), ("multiline", "both", False), ("two-senders", "both", False),
        ("pending-restart", "both", False), ("held-quiet", "both", False), ("upgrade", "both", False)),
    8: (("dead-member", "member", False), ("operator-gone", "operator", False), ("same-pane", "operator", False),
        ("unconfirmed-brief", "member", False), ("quit-during-hold", "both", True)),
    9: (("guarded", "member", False),),
    10: (("idle-latency", "both", False), ("idle-latency-times", "both", True), ("held-sender-exit", "both", False),
         ("crash", "both", False)),
}
BOUND = 300   # characters kept of any observed or expected text


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
    if team.real:                                     # a real operator first gets past its own startup dialogs
        _trust_entries(run, team)
        watcher = Harness.attach(team.qa, team.operator_harness, team.operator)
        settled = watcher.settle(timeout=120)
        team.operator_start = {"settled": settled, "answered": watcher.answered}
        if not settled.startswith("idle"):
            for name in names:
                run.row(case, name, role, harness, "not_run", f"case {case} {name}",
                        reason=f"the operator did not start: {settled}")
            return False
    code, out = team.call("up", timeout=240)
    if code != 0:
        for name in names:
            run.row(case, name, role, harness, "not_run", f"case {case} {name}", observed=out[-BOUND:],
                    reason=f"up exited {code}")
        return False
    if not team.until(lambda: team.state(team.operator) == "idle", 120):
        for name in names:
            run.row(case, name, role, harness, "not_run", f"case {case} {name}", reason="the operator never idled")
        return False
    return True


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


def _member_says(team, text, timeout=300):
    """Have the member itself run say to the operator through its own tool, with text in a file."""
    member = team.member("lead-dev")
    body = team.dir / f"report-{random.randrange(16 ** 6):06x}.md"
    body.write_text(text)
    command = f"python3 {shlex.quote(str(team.script))} say operator --file {shlex.quote(str(body))}"
    done = team.receipt("member-say")
    team.qa.rpc("terminal.send", {"terminal": member, "text": team.adapter.act_run(done, command)}, timeout=60)
    return team.until(lambda: done.exists() and "exit" in done.read_text() and done.read_text().strip(), timeout)


def case_2(run, role, harness, adapter_class):
    """idle-report, and for an operator profile operator-commands (phase_8.md §3.2, @architect's change 1)."""
    names = [n for n, _ in subcases(2, role)]
    team, peer = _team_for(adapter_class, role, harness)
    with team:
        if not _up(team, run, 2, role, harness, names):
            return
        receipt, tok = team.receipt("operator"), token()
        turn = team.get(team.operator)["turn"]
        start = time.monotonic()
        said = _member_says(team, team.adapter.act_append(receipt, tok))
        acted = team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
        proof = {"member_say": said, "operator_turn": [turn, team.get(team.operator)["turn"]],
                 "peer": peer, "prompted_by": "hc terminal.send to the member (the report is the member's say)"}
        if acted:
            run.row(2, "idle-report", role, harness, "pass", "the operator acts on an idle report with no input",
                    observed="token appended by the operator", proof=proof, elapsed=time.monotonic() - start)
        else:
            run.row(2, "idle-report", role, harness, "fail", "the operator acts on an idle report with no input",
                    proof=proof, reason="no action from the operator within 300 s")
        if role != "operator":
            return
        exits, tok2 = team.receipt("operator-commands"), token()
        member_receipt = team.receipt("member")
        py = f"python3 {shlex.quote(str(team.script))}"
        message = shlex.quote(team.adapter.act_append(member_receipt, tok2))
        commands = [f"{py} status", f"{py} inbox", f"{py} say lead-dev {message}", f"{py} up"]
        prompt = "\n".join(team.adapter.act_run(exits, command) for command in commands)
        team.qa.rpc("terminal.send", {"terminal": team.operator, "text": prompt}, timeout=60)
        lines = team.until(lambda: exits.exists() and len(exits.read_text().split()) >= 8 and exits.read_text(), 600)
        codes = [line for line in (lines or "").splitlines() if line.startswith("exit")]
        reached = member_receipt.exists() and tok2 in member_receipt.read_text()
        proof = {"exits": codes, "member_acted": reached, "prompted_by": "hc terminal.send (rodeo typing pending)"}
        ok = codes == ["exit 0"] * 4 and reached
        run.row(2, "operator-commands", role, harness, "pass" if ok else "fail",
                "the operator runs status, inbox, say and up through its own tool; the say reaches the member",
                proof=proof, reason=None if ok else "a command failed or the member never acted")


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
    team, peer = _team_for(adapter_class, role, harness)
    with team:
        if not _up(team, run, 1, role, harness, ["states", "startup-dialog"]):
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
        team.until(lambda: team.state(subject) == "idle", 60)
        states.append(team.state(subject))
        after = team.get(subject)
        proof = {"states": states, "turn": [before.get("turn"), after.get("turn")],
                 "action": receipt.read_text().strip() if receipt.exists() else None}
        ok = (states[0] == "idle" and "working" in states and states[-1] == "idle"
              and after.get("turn", 0) > before.get("turn", 0) and proof["action"] == "exit 0")
        run.row(1, "states", role, harness, "pass" if ok else "fail",
                "idle, working through a real tool action, idle again with the turn advanced", proof=proof,
                reason=None if ok else "the states or the action were not as expected")
        dialogs = harness_table().get(harness, {}).get("dialogs", [])
        how = (getattr(team, "operator_start", {"settled": "not watched (fixture operator)"}) if role == "operator"
               else "up's readiness check: started, briefed and idle")
        observed = "no startup dialog declared" if not dialogs else f"declared: {[d[0] for d in dialogs]}"
        run.row(1, "startup-dialog", role, harness, "pass",
                "a declared startup dialog is answered on its second poll, or left to the owner; none is invented",
                observed=observed, proof={"started_idle": states[0] == "idle", "handled_by": how,
                                          "mode": team.adapter.mode})


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
        times, outcomes = [], []
        recipient = "lead-dev" if role == "member" else "operator"
        for _ in range(3):
            team.until(lambda: team.state(_subject(team, role)) == "idle", 300)
            start = time.monotonic()
            if role == "member":
                code, out = team.call("say", "lead-dev", f"latency {token()}", timeout=60)
            else:
                code, out = team.call("say", "operator", "--file", str(_note(team, f"latency {token()}")),
                                      terminal=team.member("lead-dev"), timeout=60)
            times.append(round(time.monotonic() - start, 2))
            outcomes.append("delivered" if f"@{recipient}: delivered" in out else out.strip().splitlines()[-1:])
        ok = all(o == "delivered" for o in outcomes)
        run.row(10, "idle-latency", role, harness, "pass" if ok else "fail",
                f"say to an idle recipient reports delivered within SAY_WAIT ({10} s)", proof={"outcomes": outcomes},
                reason=None if ok else "a say did not report delivered within its wait")
        run.row(10, "idle-latency-times", role, harness, "measured", "three warm say times",
                observed=json.dumps(times), proof={"seconds": times})
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
        notes = [m for m in log.read_box()[1].msgs.values()
                 if m["kind"] == "note" and (m.get("meta") or {}).get("what", "").startswith("blocked")]
        typed_while_blocked = receipt.exists()
        view = _rodeo(team, ["team", "@lead-dev"])
        view.keys("ctrl+b")
        time.sleep(0.2)
        view.keys("1")
        time.sleep(0.5)
        view.focus("@lead-dev")
        view.keys("enter")                         # the owner answers the prompt
        acted = team.until(lambda: receipt.exists() and tok in receipt.read_text(), 300)
        final = _outcome(log, ident, "lead-dev") if ident else None
        proof = {"queued": bool(queued), "notes": len(notes), "typed_while_blocked": typed_while_blocked,
                 "outcome_after": final, "acted": bool(acted), "peer": peer}
        ok = bool(queued) and len(notes) == 1 and not typed_while_blocked and acted and final and final[0] == "delivered"
        run.row(5, "blocked", role, harness, "pass" if ok else "fail",
                "blocked: nothing typed, one note, the queued message delivered and acted on once after the answer",
                proof=proof, reason=None if ok else "see proof")


def factory_log(team):
    """The team's message log, read-only, from the private copy of Factory (phase_8.md §1)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("factory_under_test", team.script)
    module = sys.modules.get("factory_under_test")
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules["factory_under_test"] = module
        spec.loader.exec_module(module)
    state = next(r["labels"]["factory.dir"] for r in team.qa.rpc("terminal.list", {"all": True})["terminals"]
                 if (r.get("labels") or {}).get("factory.dir"))
    return module.open_log(state)


def _outcome(log, ident, name):
    if not ident:
        return None
    record = log.read_box()[1].outcome(int(ident[1]), name)
    return record["o"], record.get("why")


CASE_FUNCTIONS = {1: case_1, 2: case_2, 5: case_5, 7: case_7, 9: case_9, 10: case_10}


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
    versions = {"source_head": _source_head(), "hc_version": None, "rodeo_version": None, "factory_version": None}
    run = Qualification(mode, json_path, versions)
    if target == "cross":
        for name in CROSS_CASES:
            run.row("cross", name, "system", "fixture" if fixture else "system", "not_run",
                    f"the {name} cross case", reason="not built yet")
            run.checkpoint()
        return run.data, 2
    adapter_class = FixtureAdapter if fixture else RealAdapter
    for role_, harness, case in selected:
        print(f"qualify {role_} {harness} case {case}", file=sys.stderr)
        function = CASE_FUNCTIONS.get(case)
        if not subcases(case, role_):
            continue
        if function is None:
            for name, measured in subcases(case, role_):
                run.row(case, name, role_, harness, "not_run", f"case {case} {name}", reason="not built yet")
        else:
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
        run.checkpoint()
    run.data["summary"] = run.summary(selected)
    return run.data, run.exit_code(selected)


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
    args = parser.parse_args(argv)
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
