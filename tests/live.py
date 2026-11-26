#!/usr/bin/env python3
"""Live checks for Factory v2 on private hc cores (specs/projects/factory/phase_plans/phase_1.md).

Never run by CI's default job. Every core here is started by this script in its own
temporary directory, with its own HC_SESSION, HC_SOCKET and XDG_RUNTIME_DIR. Every
connection checks hello's core id before it writes a request, and a core is stopped
only after server.status names that id, so the owner's sessions are never reached.

    python3 tests/live.py check lc1|lc10|lc13|all [--json PATH]
    python3 tests/live.py fixtures [--out tests/hc_fixtures.json]
"""

import argparse
import atexit
import json
import os
import pathlib
import random
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

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

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(2)
        except subprocess.TimeoutExpired:
            self.kill()


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

    def keys(self, *names):
        return self.drv.rpc("terminal.keys", {"terminal": self.term, "keys": list(names)})

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


def schema_extract(replies, version):
    """Keep the required keys and JSON types reachable from the recorded success replies."""
    schema = json.loads(subprocess.check_output([HC, "schema"], timeout=15))
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
    missing = ERROR_KINDS - set(errors)
    if problems or missing:
        raise RuntimeError(f"fixtures not written: {problems + [f'{k}: not recorded' for k in sorted(missing)]}")
    leaks = sorted(set(scrub.leaks([scrub(replies), scrub(errors)])))
    if leaks:
        raise RuntimeError(f"fixtures still contain host details: {leaks}")
    schema, exceptions = schema_extract(replies, version)
    data = {"hc_version": version, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "Recorded by tests/live.py fixtures from a private core; host details replaced by placeholders.",
            "replies": scrub(replies), "errors": scrub(errors), "schema": schema, "wire_exceptions": exceptions}
    pathlib.Path(out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return {"check": "fixtures", "out": str(out), "hc": version, "replies": sorted(replies), "errors": sorted(errors)}


CHECKS = {"lc1": lc1, "lc10": lc10, "lc13": lc13}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("name", choices=[*CHECKS, "all"])
    check.add_argument("--json", help="also write the results to this file")
    fix = sub.add_parser("fixtures")
    fix.add_argument("--out", default=str(pathlib.Path(__file__).with_name("hc_fixtures.json")))
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    if args.command == "fixtures":
        results = [fixtures(args.out)]
    else:
        names = list(CHECKS) if args.name == "all" else [args.name]
        results = [CHECKS[n]() for n in names]
        if args.json:
            pathlib.Path(args.json).write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
