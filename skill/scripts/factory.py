#!/usr/bin/env python3
"""factory: a software team of coding agents in herdr, one team per herdr session. You are the operator (CTO);
each member runs in its own pane of the team tab, on its own harness, model and thinking level.

A project has two files at its root. FACTORY.md is the brief every member reads: the project and how the team
works; it is committed and shared. factory.toml is one person's formation: the operator, the members and their
harnesses and models; it is not committed (factory.toml.sample is, as the project's suggestion). herdr holds the
rest: a member's pane is labeled with its @name and its agent carries that name. Harnesses are described in
harnesses.toml. `factory.py --help` lists the commands.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime
import errno
import fcntl
import functools
import getpass
import hashlib
import json
import os
import pathlib
import queue
import re
import selectors
import shlex
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid

SCRIPT = pathlib.Path(__file__).resolve()
SKILL = SCRIPT.parent.parent
HERDR = os.environ.get("HERDR_BIN", "herdr")
HERDR_TIMEOUT = 90  # seconds for one herdr call, so a herdr that hangs cannot hang a command for ever
TABLE = tomllib.loads((SKILL / "harnesses.toml").read_text())  # harness -> its data
HARNESSES = tuple(TABLE)
LEVELS = ("full", "guarded")
PROBE_TIMEOUT = 60
SETTLE = float(os.environ.get("FACTORY_SETTLE", 8))  # seconds a member may take to get past a start-up notice
BRIEF = "FACTORY.md"              # the brief: shared and committed
CONF = "factory.toml"             # one person's formation: not committed
SAMPLE = "factory.toml.sample"    # the project's suggested formation: committed, copied to factory.toml by `init`
OPERATOR = "operator"  # the operator's @name, unless `[operator] name` in factory.toml says otherwise
TABS = {"operator": "factory operator", "team": "factory team"}  # herdr tab labels
HC = os.environ.get("HC_BIN", "hc")
HC_MIN = (0, 1, 20)
VERSION = hashlib.sha1(SCRIPT.read_bytes()).hexdigest()[:12]
TICK = 1.0
LATCH_AFTER = 5
BEAT_STALE = 30
KEEPER_START = 5
SAY_WAIT = 10
LAUNCH_TIMEOUT = 60
QUIET_GRACE = 120
READY_EVERY, READY_TIMEOUT = 300, 30
MAX_TEXT = 64 * 1024
CAS_TRIES = 8


class Problem(Exception):
    """A member cannot launch: what is wrong, and how the user can fix it."""

    def __init__(self, what, fix):
        super().__init__(what)
        self.what, self.fix = what, fix


# ================================================================ hc transport

class HcError(Exception):
    """An error reply from hc."""

    def __init__(self, kind, message, hint=""):
        super().__init__(f"{kind}: {message}" if kind else message)
        self.kind, self.message, self.hint = kind, message, hint


class HcDown(Exception):
    """No usable answer from hc. A sent request may already have taken effect."""

    def __init__(self, sent, detail, timeout=False):
        super().__init__(detail)
        self.sent, self.detail, self.timeout = sent, detail, timeout


class Hc:
    def __init__(self, bin: str | None = None, timeout: float | None = 30, expect_core: str | None = None):
        self.bin, self.timeout, self.expect_core = bin, timeout, expect_core
        self.version: tuple[int, ...] | None = None
        self.core: str | None = None
        self.children: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._closed = False

    def kill_children(self) -> None:
        with self._lock:
            self._closed = True
            for process in self.children:
                process.kill()

    def call(self, method, params=None, timeout=...) -> dict:
        timeout = self.timeout if timeout is ... else timeout
        setup_timeout = self.timeout if timeout is None else timeout
        deadline = None if setup_timeout is None else time.monotonic() + setup_timeout
        process = None
        complete = sent = False
        buffer = bytearray()
        try:
            stderr = tempfile.TemporaryFile()
        except OSError as error:
            raise HcDown(False, str(error)) from error

        def wait_for(fd, writing=False):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("timed out")
            with selectors.PollSelector() as poll:
                poll.register(fd, selectors.EVENT_WRITE if writing else selectors.EVENT_READ)
                if not poll.select(remaining):
                    raise TimeoutError("timed out")

        def write_line(ident, name, fields):
            nonlocal sent
            wire = json.dumps({"jsonrpc": "2.0", "id": ident, "method": name,
                               "params": {} if fields is None else fields}).encode() + b"\n"
            fd = process.stdin.fileno()
            offset = 0
            while offset < len(wire):
                wait_for(fd, writing=True)
                try:
                    count = os.write(fd, memoryview(wire)[offset:])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise OSError("hc connect stdin made no progress")
                if ident == 1:
                    sent = True
                offset += count

        def read_reply(ident, description):
            fd = process.stdout.fileno()
            end = buffer.find(b"\n")
            while end < 0:
                wait_for(fd)
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise OSError("hc connect closed its output")
                buffer.extend(chunk)
                end = buffer.find(b"\n")
            line = bytes(buffer[:end])
            del buffer[:end + 1]
            try:
                reply = json.loads(line)
            except ValueError as error:
                raise ValueError(f"unexpected {description} reply: {error}") from error
            if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
                    or type(reply.get("id")) is not int or reply["id"] != ident):
                raise ValueError(f"unexpected {description} reply: invalid JSON-RPC envelope")
            return reply

        with stderr:
            try:
                with self._lock:
                    if self._closed:
                        raise HcDown(False, "hc client closed")
                    process = subprocess.Popen([HC if self.bin is None else self.bin, "connect"],
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, bufsize=0)
                    self.children.add(process)
                os.set_blocking(process.stdin.fileno(), False)
                write_line(0, "hello", {"generation": 1, "framed": False})
                hello = read_reply(0, "hello")
                info = hello.get("result")
                if not isinstance(info, dict) or "error" in hello:
                    raise ValueError("unexpected hello reply: missing result")
                version, core = info.get("version"), info.get("core")
                if (type(info.get("generation")) is not int or info["generation"] != 1
                        or info.get("framed") is not False
                        or not isinstance(core, dict) or not isinstance(core.get("id"), str) or not core["id"]):
                    raise ValueError("unexpected hello reply: invalid generation, framing or core")
                numeric = re.fullmatch(r"([0-9]+(?:\.[0-9]+)+)(?:[-+].+)?", version) if isinstance(version, str) else None
                if numeric is None:
                    raise ValueError(f"unrecognised hc version: {version!r}")
                parsed_version = tuple(map(int, numeric[1].split(".")))
                if parsed_version < HC_MIN:
                    raise HcDown(False, f"hc {'.'.join(map(str, HC_MIN))} or newer is required (found {version})")
                if self.expect_core is not None and core["id"] != self.expect_core:
                    raise HcDown(False, f"this is another hc core ({core['id']}) than the team's ({self.expect_core})")
                self.version, self.core = parsed_version, core["id"]
                write_line(1, method, params)
                if timeout is None:
                    deadline = None  # Draft holds apply only after the request was written.
                reply = read_reply(1, "request")
                if "error" in reply:
                    error = reply["error"]
                    data = error.get("data", {}) if isinstance(error, dict) else None
                    if ("result" in reply or not isinstance(data, dict)
                            or type(error.get("code")) is not int or not isinstance(error.get("message"), str)
                            or not isinstance(data.get("kind", ""), str)
                            or (data.get("hint") is not None and not isinstance(data["hint"], str))):
                        raise ValueError("unexpected reply: malformed error")
                    raise HcError(data.get("kind", ""), error["message"], data.get("hint") or "")
                if not isinstance(reply.get("result"), dict):
                    raise ValueError("unexpected reply: missing result object")
                complete = True
                return reply["result"]
            except (OSError, ValueError) as error:
                if process is not None:
                    process.kill()
                    process.wait()
                size = stderr.seek(0, os.SEEK_END)
                stderr.seek(max(0, size - 2000))
                tail = stderr.read().decode(errors="replace").strip()
                detail = "hc not found" if process is None and isinstance(error, FileNotFoundError) else str(error)
                if tail:
                    detail = f"{detail}: {tail}"
                raise HcDown(sent, detail, timeout=isinstance(error, TimeoutError)) from error
            finally:
                if process is not None:
                    try:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
                        if complete:
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                        else:
                            process.kill()
                        process.wait()
                    finally:
                        process.stdout.close()
                        with self._lock:
                            self.children.remove(process)

    def list(self, team: str) -> list[dict]:
        return self.call("terminal.list", {"all": True, "labels": {"factory.team": team}})["terminals"]

    def get(self, term: str) -> dict:
        return self.call("terminal.get", {"terminal": term})

    def run(self, argv: list[str], cwd: str, env: dict, labels: dict) -> dict:
        return self.call("terminal.run", {"argv": argv, "cwd": cwd, "env": env, "labels": labels,
                                          "no_parent": True, "no_notify": True})

    def send(self, term: str, text: str) -> dict:
        return self.call("terminal.send", {"terminal": term, "text": text}, timeout=None)

    def keys(self, term: str, keys: list[str]) -> dict:
        return self.call("terminal.keys", {"terminal": term, "keys": keys})

    def read(self, term: str) -> str:
        return self.call("terminal.read", {"terminal": term, "lines": 0})["text"]

    def label(self, term: str, labels: dict) -> dict:
        return self.call("terminal.label", {"terminal": term, "labels": labels})

    def keep_off(self, term: str) -> None:
        self.call("terminal.keep", {"terminal": term, "keep": False})

    def kill(self, term: str) -> dict:
        return self.call("terminal.kill", {"terminal": term})

    def rm(self, term: str) -> None:
        self.call("terminal.rm", {"terminal": term})

    def doc_get(self) -> tuple[dict | None, int]:
        result = self.call("doc.get", {"name": "herdr-app"})
        return result["value"], result["version"]

    def doc_set(self, value: dict | None, version: int) -> int:
        return self.call("doc.set", {"name": "herdr-app", "value": value, "version": version})["version"]


class View:
    """Newest team terminal per member, plus the operator and keeper."""

    def __init__(self, records: list[dict]):
        self.operator: dict | None = None
        self.operator_lost: dict | None = None
        self.keeper: dict | None = None
        self.strays: list[dict] = []
        self._live: dict[str, dict] = {}
        self._finished: dict[str, dict] = {}
        for record in sorted(records, key=lambda t: t["created_ms"], reverse=True):
            labels = record["labels"]
            kind, name = labels.get("factory.kind"), labels.get("factory.member")
            if record["state"] == "live":
                if kind == "operator" and self.operator is None:
                    self.operator = record
                if kind == "operator-lost" and self.operator_lost is None:
                    self.operator_lost = record
                if kind == "keeper" and self.keeper is None:
                    self.keeper = record
                if name and kind in ("member", "operator"):
                    if name in self._live:
                        self.strays.append(record)
                    else:
                        self._live[name] = record
            elif name and kind in ("member", "operator", "operator-lost") and not labels.get("factory.down"):
                self._finished.setdefault(name, record)

    def terminal(self, name: str) -> dict | None:
        return self._live.get(name)

    def finished(self, name: str) -> dict | None:
        return None if name in self._live else self._finished.get(name)

    def state(self, name: str) -> str:
        record = self.terminal(name)
        if record is None:
            lost = self.operator_lost
            return "gone" if self.finished(name) or (lost and lost["labels"].get("factory.member") == name) else "not started"
        if record.get("status") is None:
            return "gone" if record.get("reported") else "starting"
        return record["status"]["state"]


def team_view(hc: Hc, team: str) -> View:
    return View(hc.list(team))


# ================================================================ files

def find_root(cwd):
    """The directory holding the team's files, searching up from cwd; else cwd itself."""
    cwd = pathlib.Path(cwd).resolve()
    for d in (cwd, *cwd.parents):
        if any((d / name).is_file() for name in (CONF, BRIEF)):
            return d
    return cwd


def refuse_dangling_links(root):
    """Stop before a write could follow a link to a missing file."""
    for name in (BRIEF, CONF, SAMPLE):
        path = root / name
        if path.is_symlink() and not path.exists():
            raise SystemExit(f"{path} is a symbolic link to a missing file; nothing was written."
                             " Repair or remove the link.")


def session_key():
    session = os.environ.get("HERDR_SESSION") or "default"
    return re.sub(r"[^A-Za-z0-9_.-]", "-", session)[:24] + "-" + hashlib.sha1(session.encode()).hexdigest()[:8]


def cache_base():
    return pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache")


def team_key(root):
    return f"{root.name[:24]}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"


def state_dir(root, core):
    return pathlib.Path(_absolute(cache_base() / "factory-skill" / team_key(root) / core[:8]))


def parse_role(path):
    _, head, body = path.read_text().split("---\n", 2)
    meta = {}
    for n, line in enumerate(head.splitlines(), 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"{path}:{n}: expected 'key: value', got {line!r}")
        meta[key.strip()] = value.strip()
    for key in ("description", "thinking", "readonly"):
        if key not in meta:
            raise ValueError(f"{path}: missing frontmatter key {key!r}")
    return meta, body.strip()


class Config:
    def __init__(self, cwd, require=True):
        self.root = find_root(cwd)
        self.brief_file, self.conf_file = self.root / BRIEF, self.root / CONF
        self.roles = {p.stem: parse_role(p) for p in sorted((SKILL / "roles").glob("*.md"))}
        missing = [f.name for f in (self.brief_file, self.conf_file) if not f.is_file()]
        if missing and require:
            refuse_dangling_links(self.root)
            raise SystemExit(f"no {' or '.join(missing)} in {self.root} or above. Run `/factory setup`, or"
                             f" `factory.py init`, which writes what is missing and copies the project's {SAMPLE} to"
                             f" {CONF} if there is one.")
        try:
            self.team = tomllib.loads(self.conf_file.read_text()) if self.conf_file.is_file() else {}
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{self.conf_file}: {e}")
        self.body = self.brief_file.read_text().strip() if self.brief_file.is_file() else ""
        self.operator = {"name": OPERATOR, **self.team.get("operator", {})}
        harness = self.operator.get("harness")
        if harness is not None and (not isinstance(harness, str) or harness not in TABLE):
            raise SystemExit(f"[operator] harness {harness!r} is not one of {', '.join(TABLE)}")
        self.members = self.team.get("members", {})
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", str(self.operator["name"])) or self.operator["name"] in self.members:
            raise SystemExit(f"{self.conf_file}: [operator] name {self.operator['name']!r} must be lowercase letters,"
                             " digits, - or _ (32 at most) and not a member's name")
        try:  # whose team this is: the first part of its worktree branches, so two people's never collide
            self.user = str(self.team.get("user") or getpass.getuser())
        except (KeyError, OSError):
            self.user = ""
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.user):
            raise SystemExit(f"{self.conf_file}: set `user` to your name for branches, e.g. user = \"peter\"")
        unknown = self.team.keys() - {"user", "permissions", "worktree_dir", "operator", "members",
                                      "quiet_grace", "ready_check", "ready_check_every"}
        if unknown:
            raise SystemExit(f"{self.conf_file}: unknown top-level key(s): {sorted(unknown)};"
                             " operator fields belong under [operator].")
        self.quiet_grace = self.team.get("quiet_grace", QUIET_GRACE)
        self.ready_every = self.team.get("ready_check_every", READY_EVERY)
        for key, value, minimum in (("quiet_grace", self.quiet_grace, 30),
                                    ("ready_check_every", self.ready_every, 60)):
            if type(value) is not int or value < minimum:
                raise SystemExit(f"{self.conf_file}: {key} must be an integer of at least {minimum} seconds")
        self.ready_check = self.team.get("ready_check")
        if self.ready_check is not None and (not isinstance(self.ready_check, str) or not self.ready_check.strip()):
            raise SystemExit(f"{self.conf_file}: ready_check must be a non-blank string")
        for name, m in self.members.items():
            role = m.get("role", name)
            if role not in self.roles:
                raise SystemExit(f"member {name!r}: no role {role!r}; roles are {sorted(self.roles)}")
            if len(name) > 32 or not re.fullmatch(re.escape(role) + r"(-[1-9]\d*)?", name):  # names never change
                raise SystemExit(f"{self.conf_file}: member {name!r} has role {role!r}. A role's first member is named"
                                 f" @{role}; more of that role are @{role}-2, @{role}-3, ... (32 characters at most)")
            if m.get("harness") not in TABLE:
                raise SystemExit(f"{self.conf_file}: member {name!r} needs `harness`, one of: {', '.join(TABLE)}")

    @property
    def cache(self):
        """Per-repo scratch outside the repo: members' prompt files and the sessions `up` resumes from.
        The path is short on purpose: it is typed into a shell line that macOS limits to 1024 bytes."""
        return cache_base() / "factory-skill" / team_key(self.root)

    @property
    def session_dir(self):
        return self.cache / session_key()

    @property
    def messages(self):
        return self.session_dir / "messages"

    @property
    def worktree_dir(self):
        """Where members' git worktrees live: next to the repo unless factory.toml sets `worktree_dir`."""
        return (self.root / (self.team.get("worktree_dir") or f"../{self.root.name}.worktrees")).resolve()

    @property
    def main_branch(self):
        """The branch checked out in the main checkout: what members' worktree branches are merged into."""
        try:
            p = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
            return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() != "HEAD" else "main"
        except OSError:
            return "main"


def plan(cfg, name):
    """A member's settings from factory.toml, before availability checks."""
    m = cfg.members[name]
    role = m.get("role", name)
    meta, _ = cfg.roles[role]
    permissions = m.get("permissions") or cfg.team.get("permissions") or "full"
    if permissions not in LEVELS:
        raise SystemExit(f"@{name}: permissions {permissions!r} must be one of {', '.join(LEVELS)}")
    worktree = m.get("worktree")
    branch = (f"{cfg.user}/{name}" if worktree is True else worktree) or None
    where = cfg.worktree_dir / re.sub(r"[^A-Za-z0-9._-]+", "-", branch) if branch else cfg.root
    model, thinking = m.get("model"), str(m.get("thinking") or meta["thinking"])
    if m["harness"] == "agy":
        model, thinking = agy_choice(m)
    return {"name": name, "role": role, "harness": m["harness"], "model": model, "thinking": thinking,
            "readonly": meta["readonly"] == "true", "notes": m.get("notes", ""), "permissions": permissions,
            "args": [str(a) for a in m.get("args", [])], "branch": branch, "dir": str(where),
            "draft_dir": cfg.messages / name, "inbox_dir": cfg.cache}


# ================================================================ availability checks

def run_probe(argv, env=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                           env={**os.environ, **(env or {})})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Problem(f"`{' '.join(argv[:3])}` failed: {e}",
                      "Run factory.py outside any sandbox, or check that the harness starts on its own.")
    if p.returncode != 0:
        err = [l.strip() for l in (p.stderr.strip() or p.stdout.strip()).splitlines() if l.strip()]
        named = [l for l in err if re.match(r"(error|\w*Error)\b", l, re.I)]  # skip stack frames
        tail = (named or err or [f"exit {p.returncode}"])[0][:300]
        raise Problem(f"`{' '.join(argv[:3])}` failed: {tail}",
                      "If a sandbox blocks it (read-only file system), run factory.py outside the sandbox.")
    return p.stdout


def listed_models(output):
    """provider/model ids from a harness's model listing: pi prints a `provider model ...` table, omp a box
    table under a `provider (N)` heading, opencode one provider/model per line."""
    out, style, provider = [], None, None
    for line in output.splitlines():
        toks = line.split()
        if not toks:
            continue
        heading = re.fullmatch(r"(\S+) \(\d+\)", line.strip())
        if heading:
            style, provider = "omp", heading.group(1)
        elif toks[0].lower() == "provider" and len(toks) > 1 and toks[1].lower() == "model":
            style = "pi"
        elif style == "pi" and len(toks) >= 2:
            out.append(f"{toks[0]}/{toks[1]}")
        elif style == "omp":
            if toks[0] == "│" and len(toks) > 1 and toks[1] != "model":
                out.append(f"{provider}/{toks[1]}")
        elif style is None:
            out += [t.strip(",") for t in toks if "/" in t]
    return out


def resolve_listed(model, listed, harness):
    """Exact provider/model from a harness's listing; Problem if missing or ambiguous."""
    base = model.split("#")[0]
    if base in listed:
        return base
    hits = sorted({x for x in listed if x.endswith("/" + base)})
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise Problem(f"{harness} serves {base} from several providers: {', '.join(hits)}",
                      "Write the model as provider/model in the team brief to pick one.")
    fix = {
        "pi": "Configure a provider that serves it in pi: an OpenRouter key (`pi auth --help`), or run it"
              " locally (vLLM, Ollama, llama.cpp) and add it to ~/.pi/agent/models.json (pi docs/models.md).",
        "omp": "Configure a provider that serves it in omp: an OpenRouter key, or a local server in"
               " ~/.omp/agent/models.yml.",
        "opencode": "Connect a provider that serves it in OpenCode (`/connect` in the TUI, or providers in opencode.json).",
    }.get(harness, "")
    raise Problem(f"{harness} has no model {base} configured", fix + " `factory.py harnesses` lists what it can use.")




# ================================================================ message log (v2; spec §8.5-8.7, message_log.md)

LOG_KINDS = ("say", "brief", "note")
OUTCOMES = ("sending", "delivered", "queued", "unconfirmed", "dropped")
FINAL = ("delivered", "unconfirmed", "dropped")
QUEUE_REASONS = ("waiting", "blocked", "not ready", "gone")
READ_QUEUE_REASONS = QUEUE_REASONS + ("busy",)   # "busy" is reserved for the §8.3 fallback: read, never written
MOVES = {"sending": ("queued",), "delivered": ("sending",), "unconfirmed": ("sending",),
         "queued": ("queued", "sending"), "dropped": ("queued", "sending")}   # new outcome: allowed from these
UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
STORED_WARNING = ("stored and will be delivered, but the disk reported an error: it may not survive a power loss."
                  " Do not resend.")
UNKNOWN = "storage outcome unknown; do not resend; check inbox after repair"


def private_open(path, flags, fix_mode=True, dir_fd=None):
    """Open a regular file without following a symbolic link; writers make it 0600."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        if fix_mode:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class LogDamage(Exception):
    def __init__(self, path, line, reason):
        where = f", line {line}" if line else ""
        super().__init__(f"message log needs repair: {path}{where}: {reason}. Nothing was written."
                         " Repair or move the file, then run factory.py up.")
        self.path, self.line, self.reason = path, line, reason


class NotStored(Exception):
    """The message or outcome was not stored. partial: an incomplete line may remain, which readers report."""

    def __init__(self, partial, detail):
        super().__init__(detail)
        self.partial, self.detail = partial, detail


class _Unreadable(Exception):
    """The tail could not be read back after a failed append (internal)."""

    def __init__(self, error):
        super().__init__(str(error))
        self.error = error


def _utc(value):
    """Whether value is a real UTC time written as 2026-11-26T17:00:00Z."""
    if not (isinstance(value, str) and UTC.fullmatch(value)):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _record_problem(rec):
    """Why a decoded line is not a valid record on its own, or None. Unknown extra keys are allowed."""
    if not isinstance(rec, dict):
        return "expected a JSON object"
    kind = rec.get("t")
    if kind not in ("msg", "ok", "out"):
        return "expected t of msg, ok or out"
    ident = rec.get("id")
    if type(ident) is not int or ident < 1:
        return "expected a positive integer id"
    if kind == "msg":
        to = rec.get("to")
        if not _utc(rec.get("at")):
            return "expected a UTC at"
        if not (isinstance(rec.get("from"), str) and rec["from"]):
            return "expected a sender"
        if not (isinstance(to, list) and to and all(isinstance(n, str) and n for n in to)
                and len(set(to)) == len(to) and rec["from"] not in to):
            return "expected distinct recipients other than the sender"
        if rec.get("kind") not in LOG_KINDS:
            return "expected kind say, brief or note"
        if not isinstance(rec.get("text"), str):
            return "expected text"
        if "file" in rec and not (isinstance(rec["file"], str) and os.path.isabs(rec["file"])):
            return "expected an absolute file path"
        if "meta" in rec and not isinstance(rec["meta"], dict):
            return "expected meta to be an object"
    elif kind == "out":
        o, why = rec.get("o"), rec.get("why")
        if not (isinstance(rec.get("to"), str) and rec["to"]):
            return "expected a recipient"
        if o not in OUTCOMES:
            return "expected a known outcome"
        if not _utc(rec.get("at")):
            return "expected a UTC at"
        if "why" in rec and not isinstance(why, str):
            return "expected why to be a string"
        if o == "queued" and why not in READ_QUEUE_REASONS:
            return "expected a queue reason"
        if o in ("unconfirmed", "dropped") and not (isinstance(why, str) and why.strip()):
            return f"expected a reason for {o}"
        if "term" in rec and not (isinstance(rec["term"], str) and rec["term"]):
            return "expected term to be a terminal ID"
        if "started" in rec and rec["started"] is not None and type(rec["started"]) is not bool:
            return "expected started to be true, false or null"
        if "turn" in rec and type(rec["turn"]) is not int:
            return "expected turn to be an integer"
        if o in ("sending", "delivered") and "term" not in rec:
            return f"expected the terminal ID of {o}"
        if o == "delivered" and not ("started" in rec and "turn" in rec):
            return "expected started and turn on delivered"
    return None


def _parse(data, first_line):
    """Records from whole lines of bytes; LogDamage with the 1-based line number on anything else."""
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise _LineDamage(first_line + data.count(b"\n"), "incomplete line")
    records = []
    for n, raw in enumerate(data[:-1].split(b"\n"), first_line):
        if not raw:
            raise _LineDamage(n, "blank line")
        try:
            rec = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeError, ValueError) as error:
            raise _LineDamage(n, f"not JSON: {error}") from error
        problem = _record_problem(rec)
        if problem:
            raise _LineDamage(n, problem)
        records.append(rec)
    return records


def _checkpoint_digest(check):
    """A digest of log.check's fields, so an accidental edit of one of them is a miss, not a wrong id."""
    fields = ":".join(str(check[k]) for k in ("ino", "size", "mtime_ns", "last", "lineage"))
    return hashlib.sha256(fields.encode()).hexdigest()


def _reject_constant(name):
    raise ValueError(f"{name} is not standard JSON")


def _absolute(path):
    """Absolute and lexically normalized, with one leading slash ("//x" is "/x" here); nothing is resolved."""
    path = os.path.abspath(os.fspath(path))
    return "/" + path.lstrip("/") if path.startswith("//") else path


class _LineDamage(Exception):
    def __init__(self, line, reason):
        super().__init__(reason)
        self.line, self.reason = line, reason


class Box:
    """In-memory index of the committed log, replayed from Log.read."""

    def __init__(self, path):
        self.path = path
        self._reset()

    def _reset(self):
        self.offset = 0
        self.line = 0                       # lines applied so far (full boxes only)
        self.msgs: dict[int, dict] = {}     # committed messages; text dropped once every recipient is final
        self.outcomes: dict[tuple[int, str], dict] = {}
        self.uncommitted = 0
        self.highest = 0                    # highest msg id seen, committed or not
        self._open = None                   # the msg record just read, waiting for its ok
        self._only = None                   # for_message: the one id this partial view follows
        self._lineage = None                # the checkpoint lineage this box's view was proven in, by Log.out
        self._damage = None                 # set when an apply failed: the box is unusable until rebuilt

    @classmethod
    def for_message(cls, path, ident, recipients, offset):
        """A partial, outcome-only view of one committed message, from the offset add returned."""
        box = cls(path)
        box.offset, box.line, box._only = offset, None, ident
        box.msgs[ident] = {"t": "msg", "id": ident, "to": list(recipients)}
        return box

    def _damage_at(self, index, reason):
        line = None if self.line is None else self.line + index + 1
        where = "" if line else f" (after byte {self.offset})"
        return LogDamage(self.path, line, reason + where)

    def _usable(self):
        if self._damage is not None:
            raise self._damage

    def apply(self, records):
        self._usable()
        for index, rec in enumerate(records):
            problem = self._apply(rec)
            if problem:
                self._damage = self._damage_at(index, problem)   # never leave a half-applied box usable
                raise self._damage
        if self.line is not None:
            self.line += len(records)

    def _apply(self, rec):
        kind, ident = rec["t"], rec["id"]
        if self._only is not None:
            if kind == "out" and ident == self._only:
                return self._outcome(rec)
            return None
        opened, self._open = self._open, None
        if kind == "msg":
            if ident <= self.highest:
                return f"message id {ident} is not above {self.highest}"
            self.highest = ident
            self.uncommitted += 1
            self._open = rec
            return None
        if kind == "ok":
            if opened is None or opened["id"] != ident:
                return f"commit record for {ident} does not follow its message"
            self.uncommitted -= 1
            self.msgs[ident] = dict(opened)
            return None
        if ident not in self.msgs:
            return f"outcome for unknown message {ident}"
        return self._outcome(rec)

    def _outcome(self, rec):
        ident, to = rec["id"], rec["to"]
        msg = self.msgs[ident]
        if to not in msg["to"]:
            return f"outcome for a non-recipient @{to} of message {ident}"
        current = self.outcome(ident, to)
        new = rec["o"]
        if current["o"] in FINAL:
            return f"outcome {new} after the final {current['o']} for @{to} of message {ident}"
        if new == "queued" and current["o"] == "queued" and current.get("why") == rec.get("why"):
            return None   # a redundant queued record with the same reason is harmless
        if current["o"] not in MOVES[new]:
            return f"illegal outcome {current['o']} -> {new} for @{to} of message {ident}"
        self.outcomes[(ident, to)] = dict(rec)
        if "text" in msg and all(self.outcome(ident, r)["o"] in FINAL for r in msg["to"]):
            msg.pop("text")
        return None

    def outcome(self, ident, to):
        self._usable()
        rec = self.outcomes.get((ident, to))
        return dict(rec) if rec else {"o": "queued", "why": "waiting"}

    def pending(self, to=None):
        self._usable()
        return [(msg, r) for ident, msg in sorted(self.msgs.items()) for r in msg["to"]
                if (to is None or r == to) and self.outcome(ident, r)["o"] not in FINAL]

    def sending_terms(self):
        self._usable()
        return {rec["term"] for rec in self.outcomes.values() if rec["o"] == "sending"}

    # derived facts (message_log.md "Derived facts")

    def last_to(self, name, sender=None):
        """The newest committed message delivered to name (from sender, when given)."""
        self._usable()
        for ident in sorted(self.msgs, reverse=True):
            msg = self.msgs[ident]
            if (name in msg["to"] and (sender is None or msg.get("from") == sender)
                    and self.outcome(ident, name)["o"] == "delivered"):
                return msg
        return None

    def last_from(self, name):
        """The newest committed message sent by name."""
        self._usable()
        for ident in sorted(self.msgs, reverse=True):
            if self.msgs[ident].get("from") == name:
                return self.msgs[ident]
        return None

    def briefed(self):
        """Terminal IDs that a brief was delivered to: a member terminal is ready when it is in this set."""
        self._usable()
        return {rec["term"] for (ident, _), rec in self.outcomes.items()
                if rec["o"] == "delivered" and "term" in rec and self.msgs[ident].get("kind") == "brief"}

    def last_note(self, term):
        """The meta of the newest committed member note about that terminal."""
        self._usable()
        for ident in sorted(self.msgs, reverse=True):
            msg = self.msgs[ident]
            if msg.get("kind") == "note" and (msg.get("meta") or {}).get("term") == term:
                return msg["meta"]
        return None

    def _note(self, what):
        self._usable()
        for ident in sorted(self.msgs, reverse=True):
            msg = self.msgs[ident]
            if (msg.get("kind") == "note" and (msg.get("meta") or {}).get("what") == what
                    and all(self.outcome(ident, r)["o"] != "dropped" for r in msg["to"])):
                return msg
        return None

    def quiet_note(self):
        """The newest quiet note that was not dropped."""
        return self._note("quiet")

    def ready_note(self):
        """The newest ready-work note that was not dropped."""
        return self._note("ready")


class Log:
    """The team's append-only message log in <state>/log.jsonl.

    Every operation holds a no-follow descriptor of the state directory and opens its files relative to it,
    so no component (the directory, lock, log, checkpoint) is ever followed through a symbolic link."""

    LOG, CHECK = "log.jsonl", "log.check"

    def __init__(self, state, base=None):
        """state: the team's state directory. base: the owner's cache base above it, which may be a symbolic link;
        every component from base down to state is Factory's and is never followed through a link. Without base
        only the state directory itself is."""
        self.state = pathlib.Path(_absolute(state))   # lexical: a symbolic link in the cache base is kept
        self._base = _absolute(base) if base is not None else str(self.state.parent)
        relative = os.path.relpath(self.state, self._base)
        if relative == "." or relative.startswith("..") or os.path.isabs(relative):
            raise ValueError(f"{self.state} is not below {self._base}")
        self._parts = relative.split(os.sep)
        self.path = self.state / self.LOG
        self.check_path = self.state / self.CHECK
        self._local = threading.local()     # the open state directory and lineage belong to one operation

    @property
    def _dir(self):
        return getattr(self._local, "dir", None)

    @_dir.setter
    def _dir(self, fd):
        self._local.dir = fd

    # ---------------------------------------------------------------- locking and reading

    def _open(self, name, flags, fix_mode=True):
        return private_open(name, flags, fix_mode, dir_fd=self._dir)

    def _lexists(self, name):
        try:
            os.stat(name, dir_fd=self._dir, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def open_dir(self, create):
        """A no-follow descriptor of the state directory, or None for a reader when it does not exist. The cache
        base may be the owner's symbolic link; every component below it is opened (or created) one at a time
        relative to its parent's descriptor, never through a symbolic link."""
        base, parts = self._base, self._parts
        if create:
            pathlib.Path(base).mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            if create:
                raise
            return None
        path = base
        try:
            for part in parts:
                path = os.path.join(path, part)
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        os.close(fd)
                        return None
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
        except OSError as error:
            os.close(fd)
            raise OSError(f"cannot open {path} (a symbolic link or not a directory?): {error}") from error
        if create and stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        return fd

    def _in_place(self):
        """Whether the state path still names the directory this operation holds."""
        try:
            named = os.stat(self.state, follow_symlinks=False)
        except OSError:
            return False
        held = os.fstat(self._dir)
        return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)

    def _check_moved(self, what):
        """After an append: if the state directory was moved or replaced meanwhile, the record is in the moved one."""
        if not self._in_place():
            raise OSError(f"the state directory {self.state} was moved during the write; {what} is in the moved"
                          " directory, check before resending")

    @contextlib.contextmanager
    def locked(self, exclusive):
        """The team lock is the state directory itself, flocked through the same no-follow descriptor every operation
        uses, so no two writers can append to one log under different locks. A writer then checks that the state path
        still names that directory: one moved or replaced meanwhile is damage, with nothing written. Writers create
        the private directory; readers create nothing.

        Do only descriptor-relative file operations here. Public Log methods and the team metadata helpers
        take their own lock, so calling them inside this context would wait on the lock already held."""
        directory = self.open_dir(create=exclusive)
        if directory is None:
            yield None
            return
        self._dir = directory
        try:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            except OSError as error:
                if not exclusive:
                    raise
                raise OSError(error.errno, f"cannot lock the state directory {self.state}: {error.strerror}."
                                           " If it is on a filesystem that cannot lock a directory (NFS, for"
                                           " one), set XDG_CACHE_HOME to a local path.") from error
            if exclusive and not self._in_place():
                raise LogDamage(self.path, None, "the state directory was moved or replaced while opening it")
            yield directory
        finally:
            self._dir = None
            os.close(directory)

    def _snapshot(self, offset):
        """(records, end, box) from offset, with the lock held. At offset 0 the records are also replayed into a
        full box, to check references and transitions before anything is returned; otherwise box is None."""
        try:
            fd = self._open(self.LOG, os.O_RDONLY, fix_mode=False)
        except FileNotFoundError:
            if offset:
                raise LogDamage(self.path, None, f"the log was removed (reader was at byte {offset})")
            if self._lexists(self.CHECK):
                raise LogDamage(self.path, None, "log.jsonl is missing but log.check remains")
            return [], 0, Box(self.path)
        with os.fdopen(fd, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if offset > size:
                raise LogDamage(self.path, None, f"the log was truncated (reader was at byte {offset}, size {size})")
            if offset:
                f.seek(offset - 1)
                if f.read(1) != b"\n":
                    raise LogDamage(self.path, None, f"byte {offset} is not at the start of a line")
            data = f.read()
            try:
                records = _parse(data, 1)
            except _LineDamage as damage:
                line = damage.line
                if offset:
                    f.seek(0)
                    line += f.read(offset).count(b"\n")
                raise LogDamage(self.path, line, damage.reason) from None
        end = offset + len(data)
        box = None
        if offset == 0:
            box = Box(self.path)
            box.apply(records)              # references and transitions, before anything is returned
            box.offset = end
        return records, end, box

    def read_box(self):
        """(records, box): one full, validated read and the box it built. Raises LogDamage."""
        with self.locked(exclusive=False) as fd:
            if fd is None:
                return [], Box(self.path)
            records, _, box = self._snapshot(0)
            return records, box

    def read(self, offset=0):
        """(records, end) from byte offset; offset 0 validates the whole log. Raises LogDamage."""
        if type(offset) is not int or offset < 0:
            raise ValueError(f"offset must be a non-negative integer, not {offset!r}")
        with self.locked(exclusive=False) as fd:
            if fd is None:
                if offset:
                    raise LogDamage(self.path, None, f"the log was removed (reader was at byte {offset})")
                return [], 0
            return self._snapshot(offset)[:2]

    def refresh(self, box):
        """Bring a full box up to the validated log, reading just the tail when its lineage still matches."""
        if box._only is not None:
            raise ValueError("a partial box cannot validate the log")
        with self.locked(exclusive=False) as fd:
            if fd is None:
                box._reset()
            else:
                self._refresh(box)

    # ---------------------------------------------------------------- the checkpoint

    def _stat(self):
        try:
            info = os.stat(self.LOG, dir_fd=self._dir, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"not a regular file: {self.path}")
        return info

    def _checkpoint(self, info):
        """log.check when it proves this exact file was validated ({"last", "lineage"}), else None.
        An unsafe log.check raises."""
        try:
            fd = self._open(self.CHECK, os.O_RDONLY, fix_mode=False)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as f:
            raw = f.read()
        if info is None:
            raise LogDamage(self.path, None, "log.jsonl is missing but log.check remains")
        try:
            check = json.loads(raw)
        except ValueError:
            return None
        fields = ("ino", "size", "mtime_ns", "last")
        if not (isinstance(check, dict) and all(type(check.get(k)) is int and check[k] >= 0 for k in fields)
                and isinstance(check.get("lineage"), str) and re.fullmatch(r"[0-9a-f]{16}", check["lineage"])
                and isinstance(check.get("digest"), str) and check["digest"] == _checkpoint_digest(check)):
            return None       # unparsable, or edited by hand: one full validation instead
        if (check["ino"], check["size"], check["mtime_ns"]) != (info.st_ino, info.st_size, info.st_mtime_ns):
            return None
        return check

    def _write_checkpoint(self, last):
        """Publish a fresh proof of validation; a failure only costs the next writer one full read.
        lineage names an unbroken run of appends by writers since the last full validation."""
        name, created = f"{self.CHECK}.{secrets.token_hex(8)}.tmp", False
        try:
            info = self._stat()
            if info is None:
                return
            check = {"ino": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                     "last": last, "lineage": self._local.lineage}
            data = json.dumps({**check, "digest": _checkpoint_digest(check)}).encode()
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._dir)
            created = True
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(name, self.CHECK, src_dir_fd=self._dir, dst_dir_fd=self._dir)
            return
        except OSError:
            pass
        if created:
            try:
                os.unlink(name, dir_fd=self._dir)
            except OSError:
                pass

    def _full_box(self):
        return self._snapshot(0)[2]

    def _prove(self, info):
        """The checkpoint if it proves the file, else None; records the lineage the next append continues."""
        check = self._checkpoint(info)
        self._local.lineage = check["lineage"] if check else secrets.token_hex(8)
        return check

    def _prefix(self):
        """The highest message id, after proving everything already in the log is valid (lock held)."""
        info = self._stat()
        check = self._prove(info)
        if check is not None:
            return check["last"]
        if info is None:
            return 0
        return self._full_box().highest

    # ---------------------------------------------------------------- appending

    def _append(self, line):
        """Append one record: ("synced" or "visible", end offset); "visible" when it is whole but its sync failed.
        Raises NotStored, or _Unreadable when the tail cannot be read back."""
        try:
            fd = self._open(self.LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        except OSError as error:
            raise NotStored(False, f"cannot open {self.path}: {error}") from error
        try:
            before = os.fstat(fd).st_size
            try:
                view, offset = memoryview(line), 0
                while offset < len(line):
                    count = os.write(fd, view[offset:])
                    if count <= 0:
                        raise OSError("write made no progress")
                    offset += count
                os.fsync(fd)
                self._sync_directory()
                return "synced", before + len(line)
            except OSError as error:
                failure = error
                try:
                    os.ftruncate(fd, before)
                    os.fsync(fd)
                except OSError:
                    pass
        finally:
            os.close(fd)
        try:
            rfd = self._open(self.LOG, os.O_RDONLY, fix_mode=False)
            with os.fdopen(rfd, "rb") as f:
                f.seek(before)
                tail = f.read()
        except OSError as error:
            raise _Unreadable(error) from failure
        if not tail:
            raise NotStored(False, f"the disk reported an error ({failure}); nothing was stored") from failure
        if tail == line:
            return "visible", before + len(line)
        raise NotStored(True, f"the disk reported an error ({failure}); an incomplete entry may remain") from failure

    def _sync_directory(self):
        os.fsync(self._dir)

    def _sync_ancestors(self):
        """Before the log is first created: make the state directory's entry, and each parent's up to the
        filesystem's root, durable, whoever created them (a creator may have died right after mkdir)."""
        device = os.fstat(self._dir).st_dev
        path = self.state
        while path.parent != path:
            parent = path.parent
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if os.fstat(fd).st_dev != device:
                    return            # another filesystem: the entry we depend on is in the directory below
                os.fsync(fd)
            finally:
                os.close(fd)
            path = parent

    def _first_log(self):
        """Called before an append when there is no log yet: sync the directory chain, or store nothing."""
        if self._stat() is None:
            try:
                self._sync_ancestors()
            except OSError as error:
                raise NotStored(False, f"cannot make {self.state} durable: {error}") from error

    @staticmethod
    def _line(rec):
        return (json.dumps(rec, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")

    def add(self, sender, to, text, kind="say", file=None, meta=None):
        """Store one message: (id, end offset, warning or None). Raises ValueError, LogDamage, NotStored, OSError."""
        recipients = list(to) if isinstance(to, (list, tuple)) else to
        rec = {"t": "msg", "id": 1, "at": utc_now(), "from": sender, "to": recipients, "kind": kind, "text": text}
        if file is not None:
            rec["file"] = file
        if meta is not None:
            rec["meta"] = meta
        problem = _record_problem(rec)
        if problem:
            raise ValueError(problem)
        try:
            self._line(rec)   # serializable (no NaN, no foreign objects in meta) before anything is touched
        except (TypeError, ValueError) as error:
            raise ValueError(f"message is not JSON-serializable: {error}") from error
        with self.locked(exclusive=True):
            ident = self._prefix() + 1
            self._first_log()
            rec["id"] = ident
            try:
                if self._append(self._line(rec))[0] == "visible":
                    raise NotStored(False, "the disk reported an error; the entry was left uncommitted")
            except _Unreadable as unreadable:
                raise NotStored(True, f"the disk reported an error and {self.path} could not be read back;"
                                      f" message {ident} was not committed") from unreadable.error
            try:
                status, end = self._append(self._line({"t": "ok", "id": ident}))
            except _Unreadable as unreadable:
                raise OSError(f"message {ident} in {self.path}: {UNKNOWN}") from unreadable.error
            self._check_moved(f"message {ident}")
            self._write_checkpoint(ident)
            return ident, end, None if status == "synced" else STORED_WARNING

    OUT_FIELDS = {"sending": {"term"}, "delivered": {"term", "started", "turn"}, "queued": {"why"},
                  "unconfirmed": {"why", "term"}, "dropped": {"why", "term"}}

    def _refresh(self, box):
        """Bring box up to the current, validated file (lock held). Only a box whose view belongs to the
        checkpoint's lineage (nothing but writers' appends since) refreshes its tail; any other is rebuilt."""
        if box._only is not None:
            raise ValueError("a partial box cannot authorize a write")   # before anything could widen it
        info = self._stat()
        check = self._prove(info)
        if box._damage is None and check is not None and box._lineage == check["lineage"] \
                and box.offset <= info.st_size:
            records, end, _ = self._snapshot(box.offset)
            box.apply(records)
            box.offset = end
            return
        records, end, _ = self._snapshot(0)
        box._reset()
        box.apply(records)
        box.offset, box._lineage = end, self._local.lineage   # the caller's own object, rebuilt in place

    def _check_out(self, box, ident, to, o, extra):
        if box._only is not None:
            raise ValueError("a partial box cannot authorize a write")
        if o not in OUTCOMES:
            raise ValueError(f"unknown outcome {o!r}")
        unexpected = set(extra) - self.OUT_FIELDS[o]
        if unexpected:
            raise ValueError(f"unexpected fields for {o}: {sorted(unexpected)}")
        if o == "queued" and extra.get("why") not in QUEUE_REASONS:
            raise ValueError(f"queued needs one of the reasons {QUEUE_REASONS}")
        rec = {"t": "out", "id": ident, "to": to, "o": o, "at": utc_now(), **extra}
        problem = _record_problem(rec)
        if problem:
            raise ValueError(problem)
        return rec

    def _out_locked(self, box, rec):
        ident, to, o = rec["id"], rec["to"], rec["o"]
        msg = box.msgs.get(ident)
        if msg is None or to not in msg["to"]:
            raise ValueError(f"@{to} is not a recipient of committed message {ident}")
        current = box.outcome(ident, to)
        if current["o"] in FINAL or current["o"] not in MOVES[o]:
            return False
        if o == "queued" and current["o"] == "queued" and current.get("why") == rec["why"]:
            return False
        try:
            status, end = self._append(self._line(rec))
        except _Unreadable as unreadable:
            raise OSError(f"outcome {o} for @{to} of message {ident} in {self.path}: {UNKNOWN}") from unreadable.error
        if status == "visible" and o == "sending":
            raise NotStored(False, "the sending record is not durable: do not send")
        self._check_moved(f"the {o} record for @{to} of message {ident}")
        box.apply([rec])
        box.offset, box._lineage = end, self._local.lineage
        self._write_checkpoint(box.highest)
        return True

    def out(self, box, ident, to, o, **extra):
        """Record one outcome if the pair's current outcome allows it; returns whether it wrote."""
        rec = self._check_out(box, ident, to, o, extra)
        with self.locked(exclusive=True):
            self._refresh(box)
            return self._out_locked(box, rec)

    def drop_queued(self, box, names, why="stopped by down"):
        """down's batch: drop every pair still queued to one of names, in one locked section; the count written."""
        names = set(names)
        if not (isinstance(why, str) and why.strip()):
            raise ValueError("dropped needs a reason")
        with self.locked(exclusive=True):
            self._refresh(box)
            written = 0
            for msg, to in box.pending():
                if to in names and box.outcome(msg["id"], to)["o"] == "queued":
                    rec = self._check_out(box, msg["id"], to, "dropped", {"why": why})
                    written += self._out_locked(box, rec)
            return written

    # ---------------------------------------------------------------- message files (§8.7)

    def _sender_dir(self, sender):
        """A no-follow descriptor of messages/<sender> inside the state directory (lock held), or None."""
        try:
            messages = os.open("messages", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._dir)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            raise OSError(f"not a directory (or a symbolic link): {self.state / 'messages'}") from error
        try:
            return os.open(sender, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=messages)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            raise OSError(f"not a directory (or a symbolic link): {self.state / 'messages' / sender}") from error
        finally:
            os.close(messages)

    def _unlink_in(self, sender_fd, name, path):
        """Remove one regular file by name relative to its sender directory: None, or a warning."""
        try:
            if not stat.S_ISREG(os.stat(name, dir_fd=sender_fd, follow_symlinks=False).st_mode):
                return None
            os.unlink(name, dir_fd=sender_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            return f"could not remove {path}: {error}"
        return None

    @staticmethod
    def _newest_use(box, sender, path):
        """The newest committed message from sender naming this file, by the lexical identity of the path."""
        key = lexical_path(path)
        for ident in sorted(box.msgs, reverse=True):
            msg = box.msgs[ident]
            if key and msg.get("from") == sender and "file" in msg and lexical_path(msg["file"]) == key:
                return msg
        return None

    def remove_delivered_file(self, box, ident):
        """After a delivered receipt: unlink the message's managed file once every recipient has it.
        Returns a warning naming the path when the unlink failed, else None."""
        with self.locked(exclusive=True):
            self._refresh(box)
            msg = box.msgs.get(ident)
            if msg is None or "file" not in msg or self._newest_use(box, msg["from"], msg["file"]) is not msg:
                return None
            path = lexical_path(msg["file"])
            if not all(box.outcome(ident, r)["o"] == "delivered" for r in msg["to"]):
                return None
            if not managed_file(self.state, msg["from"], path):
                return None
            sender_fd = self._sender_dir(msg["from"])
            if sender_fd is None:
                return None
            try:
                return self._unlink_in(sender_fd, os.path.basename(path), path)
            finally:
                os.close(sender_fd)

    def leftovers(self, now=None):
        """Message files left in messages/<sender>/: (rows still to check, warnings). Files whose newest message
        was delivered to every recipient are removed silently. Symbolic links and non-regular entries are ignored."""
        now = time.time() if now is None else now
        rows, warnings = [], []
        with self.locked(exclusive=True):
            box = self._full_box()
            try:
                messages = os.open("messages", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._dir)
            except (FileNotFoundError, NotADirectoryError):
                return rows, warnings
            try:
                with os.scandir(messages) as entries:
                    senders = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
            finally:
                os.close(messages)
            for sender in senders:
                sender_fd = self._sender_dir(sender)
                if sender_fd is None:
                    continue
                try:
                    with os.scandir(sender_fd) as listing:
                        entries = sorted(listing, key=lambda e: e.name)
                    for entry in entries:
                        path = str(self.state / "messages" / sender / entry.name)
                        try:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            mtime = entry.stat(follow_symlinks=False).st_mtime
                        except FileNotFoundError:
                            continue          # gone since the listing: nothing left to check
                        except OSError as error:
                            warnings.append(f"could not inspect {path}: {error}")
                            continue
                        msg = self._newest_use(box, sender, path)
                        outcomes = {r: box.outcome(msg["id"], r) for r in msg["to"]} if msg else {}
                        if (msg and MESSAGE_FILE.fullmatch(entry.name)
                                and all(o["o"] == "delivered" for o in outcomes.values())):
                            warning = self._unlink_in(sender_fd, entry.name, path)
                            if warning:
                                warnings.append(warning)
                            continue
                        rows.append({"sender": sender, "path": path, "age_s": max(0.0, now - mtime),
                                     "id": msg["id"] if msg else None,
                                     "outcomes": {r: outcome_word(o) for r, o in outcomes.items()}})
                finally:
                    os.close(sender_fd)
        return rows, warnings


def inbox_rows(log, name, count=10):
    """The last count committed messages addressed to name, oldest first, each with that recipient's current
    outcome: [(message, outcome)]. One validated snapshot; reads only (phase_3.md §7). Raises LogDamage."""
    if type(count) is not int or count < 1:
        raise ValueError(f"count must be a positive integer, not {count!r}")
    if not (isinstance(name, str) and name):
        raise ValueError("a recipient name is needed")
    records, box = log.read_box()
    originals = {r["id"]: r for r in records if r["t"] == "msg"}   # the box drops text once final; these keep it
    chosen = [ident for ident in sorted(box.msgs) if name in box.msgs[ident]["to"]][-count:]
    return [(originals[ident], box.outcome(ident, name)) for ident in chosen]


MESSAGE_FILE = re.compile(r"[A-Za-z0-9-]+\.[A-Za-z0-9]+")


def outcome_word(outcome):
    return f"queued ({outcome['why']})" if outcome["o"] == "queued" else outcome["o"]


def lexical_path(path):
    """A path's lexical identity: absolute, with "." and repeated slashes removed; None for a relative path or one
    with a ".." component. Nothing is resolved."""
    path = os.fspath(path)
    if not os.path.isabs(path) or ".." in path.split("/"):
        return None
    return _absolute(path)


def managed_file(state, sender, path):
    """Whether path is a message file Factory may remove: a regular file named like a-b-1.md directly in
    <state>/messages/<sender>/, with no symbolic link from the state directory down. Lexical: nothing is resolved."""
    path, state = lexical_path(path), _absolute(state)
    if path is None or not MESSAGE_FILE.fullmatch(os.path.basename(path)):
        return False
    if os.path.dirname(path) != os.path.join(state, "messages", sender) or not sender or "/" in sender:
        return False
    try:
        for component in (state, os.path.join(state, "messages"), os.path.dirname(path)):
            if not stat.S_ISDIR(os.lstat(component).st_mode):
                return False
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def format_leftovers(rows):
    """up's cold-start lines for leftover message files, with the reminder once."""
    lines = []
    for row in rows:
        where = (f"message {row['id']}: " + ", ".join(f"@{r} {o}" for r, o in row["outcomes"].items())
                 if row["id"] is not None else "never stored")
        lines.append(f"leftover message file for @{row['sender']} ({int(row['age_s'] // 60)} min old): "
                     f"{row['path']}: {where}")
    if lines:
        lines.append("Check before resending or discarding.")
    return lines


# ================================================================ v2 team state

def open_log(state):
    raw = os.fspath(state)
    path = pathlib.Path(raw)
    if lexical_path(raw) != raw or len(path.parents) < 3 or path.parent.parent.name != "factory-skill":
        raise Problem(f"{state} is not a Factory state directory", "run factory.py up")
    return Log(path, base=path.parents[2])


def _validate_team(team):
    if not isinstance(team, dict) or type(team.get("v")) is not int or team["v"] != 1:
        raise ValueError("expected a version 1 object")
    for key in ("root", "team", "core"):
        if not isinstance(team.get(key), str) or not team[key]:
            raise ValueError(f"expected a non-empty {key}")
    if "up" in team and type(team["up"]) is not bool:
        raise ValueError("expected up to be true or false")
    if "stopped" in team and (not isinstance(team["stopped"], list)
                              or not all(isinstance(term, str) and term for term in team["stopped"])):
        raise ValueError("expected stopped to list terminal IDs")


def _read_team_file(state, directory):
    if directory is None:
        return {}
    try:
        try:
            fd = private_open("team.json", os.O_RDONLY, fix_mode=False, dir_fd=directory)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            team = json.load(stream)
        _validate_team(team)
        return team
    except (OSError, ValueError) as error:
        raise Problem(f"{state}/team.json cannot be read: {error}", "run factory.py up") from error


def read_team(state):
    log = open_log(state)
    try:
        with log.locked(exclusive=False) as directory:
            return _read_team_file(log.state, directory)
    except (LogDamage, OSError) as error:
        raise Problem(f"{log.state}/team.json cannot be read: {error}", "run factory.py up") from error


def _write_team_file(directory, team):
    _validate_team(team)
    data = json.dumps(team, allow_nan=False).encode() + b"\n"
    temp = f"team.json.{secrets.token_hex(8)}.tmp"
    fd = private_open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, "team.json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=directory)
        raise


def update_team(state, reset=False, **changes):
    log = open_log(state)
    with log.locked(exclusive=True) as directory:
        current = {} if reset else _read_team_file(log.state, directory)
        team = {"v": 1, **current, **changes}
        _write_team_file(directory, team)
        return team


def mark_stopped(state, terms, box):
    """Remember explicit stops before removing those terminals, so delayed refusals do not requeue."""
    log = open_log(state)
    with log.locked(exclusive=True) as directory:
        team = _read_team_file(log.state, directory)
        if not team:
            raise Problem(f"no team.json in {log.state}", "run factory.py up")
        log._refresh(box)
        claimed = box.sending_terms()
        keep = [term for term in team.get("stopped", []) if term in claimed]
        team["stopped"] = list(dict.fromkeys([*keep, *terms]))
        _write_team_file(directory, team)


def settle_refusal(log, box, ident, to, term, why):
    """Settle a member's definite pre-typing refusal against stop evidence under the same lock."""
    with log.locked(exclusive=True) as directory:
        team = _read_team_file(log.state, directory)
        if not team:
            raise Problem(f"no team.json in {log.state}", "run factory.py up")
        log._refresh(box)
        current = box.outcome(ident, to)
        if current["o"] != "sending" or current["term"] != term:
            return None
        stopped = term in team.get("stopped", [])
        outcome = "dropped" if stopped else "queued"
        rec = log._check_out(box, ident, to, outcome, {"why": "stopped by down" if stopped else why})
        return outcome if log._out_locked(box, rec) else None


def _offline_state(cfg):
    """Find the checkout's newest log, or its newest metadata-only state."""
    base = pathlib.Path(_absolute(cache_base()))
    parent = base / "factory-skill" / team_key(cfg.root)
    candidates = []
    try:
        directory = Log(parent, base=base).open_dir(create=False)
        if directory is None:
            return None
        try:
            with os.scandir(directory) as entries:
                names = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
            for name in names:
                state = parent / name
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                try:
                    for priority, filename in enumerate((Log.LOG, "team.json")):
                        try:
                            info = os.stat(filename, dir_fd=child, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            raise Problem(f"not a regular file: {state / filename}", "run factory.py up")
                        candidates.append((priority, -info.st_mtime_ns, state))
                        break
                finally:
                    os.close(child)
        finally:
            os.close(directory)
    except OSError as error:
        raise Problem(f"cannot inspect {parent}: {error}", "run factory.py up") from error
    if not candidates:
        return None
    state = min(candidates)[2]
    if len(candidates) > 1:
        print(f"using {state}", file=sys.stderr)
    return state


def state_for(cfg, hc):
    if hc is None:
        return _offline_state(cfg)
    key = team_key(cfg.root)
    records = hc.list(key)
    view = View(records)
    for record in (view.operator, view.keeper, *records):
        label = record and record.get("labels", {}).get("factory.dir")
        if label:
            state = open_log(label).state
            if state.parent.name != key or state.name != hc.core[:8]:
                raise Problem(f"{state} does not match this checkout and hc core", "run factory.py up")
            return state
    return state_dir(cfg.root, hc.core)


def resolve_team(args, hc):
    """Return the formation, its state path and the caller's terminal when known."""
    caller = None
    terminal = os.environ.get("HC_TERMINAL_ID")
    if hc is not None and terminal:
        try:
            caller = hc.get(terminal)
        except HcError as error:
            if error.kind != "not_found":
                raise
        labels = caller.get("labels", {}) if caller else {}
        key, label = labels.get("factory.team"), labels.get("factory.dir")
        if key and label:
            if not isinstance(key, str) or not isinstance(label, str):
                raise Problem("this pane has invalid Factory labels", "run factory.py up")
            state = open_log(label).state
            team = read_team(state)
            if not team:
                raise Problem(f"no team.json in {state}", "run factory.py up")
            if (team["team"] != key or state.parent.name != key or state.name != team["core"][:8]
                    or not os.path.isabs(team["root"])):
                raise Problem(f"this pane's labels do not match {state}/team.json", "run factory.py up")
            if team["core"] != hc.core:
                raise Problem("this pane's hc is another core than the team's", "run factory.py up")
            hc.expect_core = team["core"]
            try:
                cfg = Config(team["root"])
            except SystemExit as error:
                raise Problem(str(error), "run factory.py up") from error
            if team_key(cfg.root) != key:
                raise Problem(f"this pane's checkout does not match {state}/team.json", "run factory.py up")
            return cfg, state, caller
    cfg = Config(getattr(args, "cwd", None) or os.getcwd())
    return cfg, state_for(cfg, hc), caller


def find_team(args):
    hc = Hc() if os.environ.get("HC_TERMINAL_ID") else None
    try:
        return resolve_team(args, hc)
    except HcDown:
        return resolve_team(args, None)


def pending_lines(state, operator, now=None):
    if state is None:
        return ["pending: none"]
    try:
        _, box = open_log(state).read_box()
    except LogDamage as error:
        return [str(error)]
    except (Problem, OSError) as error:
        return [f"message log unavailable: {error}"]
    pairs = collections.defaultdict(list)
    for msg, name in box.pending():
        pairs[name].append(msg)
    now = time.time() if now is None else now
    parts = []
    for name, messages in pairs.items():
        oldest = messages[0]
        seconds = max(0, int(now - datetime.datetime.fromisoformat(oldest["at"]).timestamp()))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        age = f"{hours}h{minutes}m" if hours else f"{minutes}m" if minutes else f"{seconds}s"
        outcome = box.outcome(oldest["id"], name)
        reason = outcome["why"] if outcome["o"] == "queued" else outcome["o"]
        parts.append(f"@{name} {len(messages)} (oldest {age}, {reason})")
    lines = ["pending: " + ("; ".join(parts) if parts else "none")]
    if operator in pairs:
        lines.append(f"read them with: factory.py inbox --for {operator}")
    if box.uncommitted:
        noun = "entry" if box.uncommitted == 1 else "entries"
        lines.append(f"log: {box.uncommitted} uncommitted {noun} (never delivered)")
    return lines


def print_pending(args, operator):
    """Bridge for the old up/status commands until Phase 5a replaces their launch and pane logic."""
    try:
        state = find_team(args)[1]
    except (Problem, HcError) as error:
        print(f"pending: unavailable ({error})")
        return
    print("\n".join(pending_lines(state, operator)))


# ================================================================ keeper delivery

def head_of(name, box, ready):
    """The next message for a recipient and any older startup briefs it supersedes."""
    pending = [msg for msg, _ in box.pending(name)]
    if not ready:
        briefs = [msg for msg in pending if msg["kind"] == "brief"]
        if briefs:
            return briefs[-1], briefs[:-1]
    return (pending[0] if pending else None), []


def _quiet_or_ready_note(msg):
    return msg["kind"] == "note" and (msg.get("meta") or {}).get("what") in ("quiet", "ready")


def decide(name, msg, view, box, cfg, now, op_null_since, still_true=True):
    """Choose the first matching delivery action from keeper.md's decision table."""
    if name == cfg.operator["name"]:
        if _quiet_or_ready_note(msg) and not still_true:
            return "drop", "no longer true"
        terminal = view.operator
        if terminal is None:
            return "queue", "gone"
        status = terminal.get("status")
        if status is None:
            if op_null_since is not None and now - op_null_since >= LATCH_AFTER:
                return "latch",
            return "hold",
        if status["state"] == "blocked":
            return "queue", "blocked"
        return "send", terminal["address"]

    state = view.state(name)
    if state == "not started":
        return "drop", "stopped by down"
    if state == "gone":
        return "queue", "gone"
    if state == "starting":
        return "hold",
    terminal = view.terminal(name)
    if msg["kind"] != "brief" and terminal["address"] not in box.briefed():
        return "queue", "not ready"
    if state == "blocked":
        return "queue", "blocked"
    return "send", terminal["address"]


class Keeper:
    """One delivery loop. Only the main thread changes the log and per-recipient claims."""

    def __init__(self, state, hc, clock=time, emit=print, spawn=None):
        self.log = open_log(state)
        self.state = self.log.state
        self.hc, self.clock, self.emit = hc, clock, emit
        self.spawn = self._spawn if spawn is None else spawn
        self.box = Box(self.log.path)
        self.cfg = None
        self._cfg_stamp = None
        self.team = {}
        self.results = queue.Queue()
        self.unsettled = []
        self.inflight = {}
        self.held = set()
        self.op_null_since = None
        self._null_terminal = None
        self.damaged = None
        self._retry_log_at = 0
        self._reported_errors = {}
        self._beat_fd = None
        self.stop = None

    @staticmethod
    def _spawn(job):
        threading.Thread(target=job, daemon=True).start()

    def _report(self, key, message):
        now = self.clock.monotonic()
        prior = self._reported_errors.get(key)
        if prior is None or now - prior >= 60:
            self.emit(message)
            self._reported_errors[key] = now

    def _stop_on_signal(self, *_):
        self.stop = "signal"

    def run(self):
        try:
            with contextlib.ExitStack() as resources:
                directory = self.log.open_dir(create=False)
                if directory is None:
                    self.emit(f"no state directory: {self.state}")
                    return 0
                resources.callback(os.close, directory)
                lock = private_open("keeper.lock", os.O_CREAT | os.O_RDWR, dir_fd=directory)
                resources.callback(os.close, lock)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.emit("keeper already running")
                    return 0
                resources.callback(self.hc.kill_children)
                if threading.current_thread() is threading.main_thread():
                    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                        previous = signal.signal(sig, self._stop_on_signal)
                        resources.callback(signal.signal, sig, previous)
                if not self._read_team():
                    return 0
                if self._load():
                    self._recover("keeper restarted during the send")
                terminal = os.environ["HC_TERMINAL_ID"]
                beat = private_open("keeper.beat", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, dir_fd=directory)
                resources.callback(os.close, beat)
                self._beat_fd = beat
                with os.fdopen(os.dup(beat), "w") as stream:
                    stream.write(f"{os.getpid()} {VERSION} {terminal}\n")
                try:
                    self.hc.label(terminal, {"factory.version": VERSION})
                except (HcError, HcDown) as error:
                    self._report("label", str(error))
                self.emit(f"keeper {VERSION} started")
                while not self.stop:
                    self.tick()
                    if not self.stop:
                        try:
                            result = self.results.get(timeout=TICK)
                        except queue.Empty:
                            continue
                        self.results.put(result)
        except Exception as error:
            self.stop = str(error)
        finally:
            self._beat_fd = None
        if self.stop:
            self.emit(f"keeper stopping: {self.stop}")
        return 0

    def _read_team(self):
        try:
            team = read_team(self.state)
            if not team:
                raise Problem(f"no team.json in {self.state}", "run factory.py up")
            if team["team"] != self.state.parent.name or team["core"][:8] != self.state.name:
                raise Problem(f"{self.state}/team.json does not match its directory", "run factory.py up")
        except Problem as error:
            self._report("team", f"team.json: {error}")
            return False
        self.team = team
        return True

    def _read_config(self):
        root = self.team["root"]
        try:
            stamp = root, (pathlib.Path(root) / CONF).stat().st_mtime_ns
            if self._cfg_stamp != stamp:
                self.cfg = Config(root)
                self._cfg_stamp = stamp
        except (SystemExit, OSError) as error:
            self._report("config", f"factory.toml: {error}")

    def _log_failed(self, error):
        if self.damaged is None:
            self._retry_log_at = self.clock.monotonic() + 60
        self.damaged = str(error)
        self._report("log", self.damaged)

    def _load(self):
        try:
            _, self.box = self.log.read_box()
        except (LogDamage, OSError) as error:
            self._log_failed(error)
            return False
        self.damaged = None
        return True

    def _recover(self, why):
        try:
            for msg, recipient in self.box.pending():
                ident = msg["id"]
                if self.inflight.get(recipient) != ident and self.box.outcome(ident, recipient)["o"] == "sending":
                    self._record(msg, recipient, "unconfirmed", why=why)
        except (LogDamage, NotStored, OSError) as error:
            self._log_failed(error)

    def tick(self):
        if self._beat_fd is not None:
            os.utime(self._beat_fd)
        previous = set(self.inflight.items())
        self._drain_results()
        if not self._read_team():
            return
        self._read_config()
        try:
            view = team_view(self.hc, self.team["team"])
        except (HcDown, HcError) as error:
            self._report("hc", f"hc unreachable: {error}")
            return
        now = self.clock.monotonic()
        if self.damaged is not None:
            self._report("log", self.damaged)
            if now >= self._retry_log_at:
                self._retry_log_at = now + 60
                if self._load():
                    self.emit("message log readable again")
                    self._recover("keeper could not record the send")
        else:
            try:
                self.log.refresh(self.box)
            except (LogDamage, OSError) as error:
                self._log_failed(error)
        null_terminal = (view.operator["address"] if view.operator and view.operator.get("status") is None else None)
        if null_terminal != self._null_terminal:
            self._null_terminal = null_terminal
            self.op_null_since = now if null_terminal is not None else None
        for recipient, ident in self.inflight.items():
            pair = recipient, ident
            if pair in previous and pair not in self.held and ident in self.box.msgs:
                self.held.add(pair)
                self._emit_outcome(self.box.msgs[ident], recipient, "sending (held)")
        if self.cfg is None or self.stop:
            return
        try:
            if self.damaged is None:
                self._deliver(view, now)
            operator = self.cfg.operator["name"]
            if (self.team.get("up") is not True and not self.inflight
                    and (not view.operator or view.operator.get("status") is None or not self.box.pending(operator))):
                self.stop = "team is down"
        except (LogDamage, NotStored, OSError) as error:
            self._log_failed(error)

    def _emit_outcome(self, msg, recipient, outcome, why=None):
        stamp = time.strftime("%H:%M:%S", time.localtime(self.clock.time()))
        sender = "factory" if msg["kind"] == "note" else f"@{msg['from']}"
        suffix = f": {why[:80]}" if why else ""
        self.emit(f"{stamp} #{msg['id']} {sender} -> @{recipient} {outcome}{suffix}")

    def _record(self, msg, name, outcome, **extra):
        if not self.log.out(self.box, msg["id"], name, outcome, **extra):
            return False
        if outcome != "sending":
            self._emit_outcome(msg, name, outcome, extra.get("why"))
        return True

    def _deliver(self, view, now):
        operator = self.cfg.operator["name"]
        recipients = dict.fromkeys(name for _, name in self.box.pending())
        briefed = self.box.briefed()
        for name in recipients:
            if self.stop:
                return
            if name in self.inflight:
                continue
            terminal = view.terminal(name)
            ready = name == operator or (terminal and terminal["address"] in briefed)
            msg, superseded = head_of(name, self.box, ready)
            for old in superseded:
                self._record(old, name, "dropped", why="superseded")
            if msg is None:
                continue
            action, *detail = decide(name, msg, view, self.box, self.cfg, now, self.op_null_since)
            if action == "hold":
                continue
            if action == "latch":
                try:
                    self.hc.label(view.operator["address"], {"factory.kind": "operator-lost"})
                except (HcError, HcDown) as error:
                    self._report("latch", f"could not mark operator lost: {error}")
                    continue
                action, detail = "queue", ["gone"]
            if action == "send":
                self._claim(msg, name, detail[0])
                continue
            why = detail[0]
            outcome = "queued" if action == "queue" else "dropped"
            if why in ("gone", "not ready", "stopped by down"):
                for pending, _ in self.box.pending(name):
                    if self.box.outcome(pending["id"], name)["o"] == "queued":
                        self._record(pending, name, outcome, why=why)
            else:
                self._record(msg, name, outcome, why=why)

    def _claim(self, msg, name, term):
        if self.stop or not self._record(msg, name, "sending", term=term) or self.stop:
            return
        ident = msg["id"]
        current = self.box.msgs[ident]
        sender = "factory" if current["kind"] == "note" else f"@{current['from']}"
        wire = f"[from {sender}]\n{current['text']}"
        if not wire.endswith("\n"):
            wire += "\n"
        self.inflight[name] = ident
        try:
            self.spawn(functools.partial(self._send, name, ident, term, wire))
        except RuntimeError as error:
            self.results.put((name, ident, term, HcDown(False, str(error))))

    def _send(self, name, ident, term, wire):
        try:
            if self.stop:
                raise HcDown(False, "keeper stopped before sending")
            result = self.hc.send(term, wire)
        except Exception as error:
            result = error
        self.results.put((name, ident, term, result))

    def _drain_results(self):
        pending, self.unsettled = self.unsettled, []
        while True:
            try:
                pending.append(self.results.get_nowait())
            except queue.Empty:
                break
        for name, ident, term, result in pending:
            if self.inflight.get(name) != ident:
                continue
            try:
                self._settle_result(name, ident, term, result)
            except Problem as error:
                self.unsettled.append((name, ident, term, result))
                self._report("team", f"team.json: {error}")
                continue
            except (LogDamage, NotStored, OSError) as error:
                self._log_failed(error)
            self.inflight.pop(name)
            self.held.discard((name, ident))

    def _settle_result(self, name, ident, term, result):
        msg = self.box.msgs.get(ident)
        if msg is None:
            return
        if isinstance(result, dict):
            started = result.get("started")
            if type(result.get("turn")) is int and (started is None or type(started) is bool):
                if self._record(msg, name, "delivered", term=term, started=started, turn=result["turn"]):
                    warning = self.log.remove_delivered_file(self.box, ident)
                    if warning:
                        self.emit(warning)
            else:
                self._record(msg, name, "unconfirmed", why="hc returned an unexpected result", term=term)
            return
        why = None
        if isinstance(result, HcDown) and not result.sent:
            why = "waiting"
        elif isinstance(result, HcError):
            why = {"blocked": "blocked", "not_found": "gone", "finished": "gone"}.get(result.kind)
        if why is None:
            detail = " ".join(str(result).split())[:200] or type(result).__name__
            self._record(msg, name, "unconfirmed", why=detail, term=term)
        elif _quiet_or_ready_note(msg):
            self._record(msg, name, "dropped", why="operator could not take it", term=term)
        elif name == self.cfg.operator["name"]:
            self._record(msg, name, "queued", why=why)
        else:
            outcome = settle_refusal(self.log, self.box, ident, name, term, why)
            if outcome:
                self._emit_outcome(msg, name, outcome, self.box.outcome(ident, name)["why"])


def cmd_keeper(args):
    if not os.environ.get("HC_TERMINAL_ID"):
        print("the keeper runs only in an hc terminal")
        return
    try:
        team = read_team(args.state)
    except Problem as error:
        print(error)
        return
    if not team:
        print(f"no team.json in {args.state}")
        return
    Keeper(args.state, Hc(timeout=10, expect_core=team["core"]), emit=functools.partial(print, flush=True)).run()


def read_beat(state):
    """Return the keeper PID, version, terminal ID and age, or None without a valid beat."""
    directory = open_log(state).open_dir(create=False)
    if directory is None:
        return None
    try:
        try:
            fd = private_open("keeper.beat", os.O_RDONLY, fix_mode=False, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            try:
                pid, version, terminal = stream.read(1024).split()
                return int(pid), version, terminal, time.time() - os.fstat(stream.fileno()).st_mtime
            except ValueError:
                return None
    finally:
        os.close(directory)


def _take_keeper_start(lock):
    deadline = time.monotonic() + 15
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise Problem("another command is starting the keeper", "run factory.py status") from None
            time.sleep(0.1)


def _wait_for_beat(state, hc, terminal):
    begin = time.monotonic()
    next_poll = begin
    while True:
        beat = read_beat(state)
        if beat and beat[2] == terminal:
            return terminal
        now = time.monotonic()
        if now >= next_poll:
            try:
                finished = hc.get(terminal)["state"] == "finished"
            except HcError as error:
                if error.kind != "not_found":
                    raise
                raise Problem("the keeper did not start: terminal disappeared", "run factory.py status") from error
            if finished:
                lines = [line.strip() for line in hc.read(terminal).splitlines() if line.strip()]
                hc.rm(terminal)
                raise Problem(f"the keeper did not start: {lines[-1] if lines else 'terminal exited'}",
                              "run factory.py status")
            next_poll = now + 1
        if now - begin >= KEEPER_START:
            return terminal
        time.sleep(0.1)


def ensure_keeper(cfg, state, hc, view, force=False):
    """Return this team's keeper terminal, starting it headless if it is missing or stale."""
    team = read_team(state)
    if not team:
        raise Problem(f"no team.json in {state}", "run factory.py up")
    log = open_log(state)
    if not force and team.get("up") is not True and not log.read_box()[1].pending(cfg.operator["name"]):
        return None
    directory = log.open_dir(create=False)
    if directory is None:
        raise Problem(f"no state directory: {state}", "run factory.py up")
    with contextlib.ExitStack() as resources:
        resources.callback(os.close, directory)
        lock = private_open("keeper.start", os.O_CREAT | os.O_RDWR, dir_fd=directory)
        resources.callback(os.close, lock)
        _take_keeper_start(lock)
        records = hc.list(team["team"])
        view = View(records)  # the caller's view preceded acquisition of the start lock
        if view.keeper is not None:
            term = view.keeper["address"]
            beat = read_beat(state)
            if (beat and beat[2] == term and beat[3] <= BEAT_STALE
                    or time.time() - view.keeper["created_ms"] / 1000 < BEAT_STALE):
                return term
            try:
                records.append(hc.kill(term))
            except HcError as error:
                if error.kind == "finished":
                    records.append({**view.keeper, "state": "finished"})
                elif error.kind != "not_found":
                    raise
        for record in records:
            if record["labels"].get("factory.kind") == "keeper" and record["state"] == "finished":
                try:
                    hc.rm(record["address"])
                except HcError as error:
                    if error.kind != "not_found":
                        raise
        argv = [sys.executable, str(SCRIPT), "keeper", "--state", str(log.state)]
        env = {"PATH": os.environ.get("PATH", "")}
        if "HC_BIN" in os.environ:
            env["HC_BIN"] = os.environ["HC_BIN"]
        labels = {"factory.team": team["team"], "factory.kind": "keeper", "factory.dir": str(log.state),
                  "factory.version": VERSION, "name": "keeper"}
        terminal = hc.run(argv, cwd=team["root"], env=env, labels=labels)["address"]
        return _wait_for_beat(log.state, hc, terminal)


def wait_outcomes(log, ident, recipients, offset, timeout=SAY_WAIT):
    """Follow only this message from its commit, without rereading the log prefix."""
    box = Box.for_message(log.path, ident, recipients, offset)
    deadline = time.monotonic() + timeout
    while True:
        records, offset = log.read(offset)
        box.apply(records)
        outcomes = {name: box.outcome(ident, name) for name in recipients}
        if all(record["o"] in FINAL or (record["o"] == "queued" and record["why"] != "waiting")
               for record in outcomes.values()):
            return outcomes
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return outcomes
        time.sleep(min(0.2, remaining))


def outcome_lines(outcomes, operator, sender):
    lines, missing = [], []
    for name, record in outcomes.items():
        outcome = record["o"]
        line = f"@{name}: {outcome_word(record)}"
        if outcome in ("unconfirmed", "dropped"):
            line += f" ({record['why']})"
            missing.append(f"@{name}")
        if name == operator and outcome == "queued" and record["why"] == "gone":
            line += ": operator not running; message queued"
        lines.append(line)
    if missing:
        instruction = "Do not resend unless asked"
        if sender != operator:
            instruction += f"; tell @{operator}"
        lines.append(f"{', '.join(missing)} may not have it. {instruction}.")
    return lines, int(bool(missing))


def _say_payload(args, state, sender, is_member):
    if args.file is None:
        if args.message is None:
            raise ValueError("give a message or --file")
        if is_member:
            raise ValueError("members send with --file")
        text, recorded = args.message, None
    else:
        path = args.file
        recorded = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
        lexical = lexical_path(recorded)
        if (lexical is not None and pathlib.Path(lexical).parent == state / "messages" / sender
                and not MESSAGE_FILE.fullmatch(os.path.basename(lexical))):
            raise ValueError("use letters, digits and dashes in the draft name, plus a plain extension such as .md")
        try:
            text = pathlib.Path(path).read_bytes().decode("utf-8")
        except (OSError, ValueError) as error:
            raise ValueError(f"cannot read message file {path}: {error}") from error
    if not text.strip():
        raise ValueError("give a non-empty message")
    size = len(text.encode("utf-8"))
    if size > MAX_TEXT:
        raise ValueError(f"message is {size} bytes; the limit is {MAX_TEXT}. Write it to a file and send the path")
    return text, recorded


def cmd_say(args):
    def refuse(reason):
        print(f"nothing sent: {reason}", file=sys.stderr)
        sys.exit(2)

    if not os.environ.get("HC_TERMINAL_ID"):
        refuse("run up first")
    hc = Hc()
    try:
        cfg, state, me = resolve_team(args, hc)
    except Problem as error:
        refuse(f"{error.what}; {error.fix}")
    except (HcDown, HcError) as error:
        refuse(str(error))
    except SystemExit:
        refuse("run up first")
    labels = me["labels"] if me else {}
    if not labels.get("factory.team") or not labels.get("factory.dir"):
        refuse("run up first")
    kind = labels.get("factory.kind")
    if kind == "keeper":
        refuse("the keeper cannot send")
    sender = labels.get("factory.member")
    if kind not in ("member", "operator") or not isinstance(sender, str) or not sender:
        refuse("run up first")
    operator = cfg.operator["name"]
    if kind == "member" and sender not in cfg.members:
        refuse(f"@{sender} is no longer in factory.toml")
    # Phase 5a: record_session(cfg, state, sender, me["address"]) for a member, before validation.
    names = list(dict.fromkeys(name.lstrip("@") for name in re.findall(r"[^,\s]+", " ".join(args.to))))
    try:
        view = team_view(hc, labels["factory.team"])
    except (HcDown, HcError) as error:
        refuse(str(error))
    if names == ["all"]:
        names = [name for name in cfg.members if name != sender and view.terminal(name)]
        if not names:
            refuse("no other member is running")
    if not names:
        refuse("give a recipient")
    for name in names:
        if name != operator and name not in cfg.members:
            refuse(f"@{name} is not on the team (members: {', '.join(cfg.members)})")
        if name == sender:
            refuse("choose another teammate as the recipient")
        if name != operator and view.state(name) == "not started":
            refuse(f"@{name} is not running")
    try:
        text, recorded = _say_payload(args, state, sender, kind == "member")
    except ValueError as error:
        refuse(str(error))
    log = open_log(state)
    try:
        ident, offset, warning = log.add(sender, names, text, kind="say", file=recorded)
    except LogDamage as error:
        refuse(str(error))
    except NotStored as error:
        if error.partial:
            print(f"not stored, and the log may now need repair: {error.detail}. Do not resend; tell the operator.",
                  file=sys.stderr)
        else:
            print(f"not stored: {error.detail}. Nothing will be delivered. Check the disk, then send again.",
                  file=sys.stderr)
        sys.exit(2)
    except OSError as error:
        print(f"not confirmed stored: {error}. Do not resend; check factory.py inbox.", file=sys.stderr)
        sys.exit(1)

    print(f"message {ident}", flush=True)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    timeout = SAY_WAIT
    try:
        if ensure_keeper(cfg, state, hc, view) is None:
            print(f"warning: the team is stopped. Message {ident} is stored and waits for factory.py up.",
                  file=sys.stderr)
            timeout = 0
    except (Problem, HcDown, HcError, LogDamage, OSError) as error:
        print(f"warning: the keeper is not running ({error}). Message {ident} is stored and will be delivered once"
              " it runs; run factory.py status.", file=sys.stderr)
        timeout = 0
    try:
        outcomes = wait_outcomes(log, ident, names, offset, timeout)
    except (LogDamage, OSError) as error:
        print(f"message {ident} is stored; its outcome could not be read: {error}. Do not resend; run factory.py inbox.",
              file=sys.stderr)
        sys.exit(1)
    lines, code = outcome_lines(outcomes, operator, sender)
    print("\n".join(lines))
    sys.exit(code)


def cmd_inbox(args):
    """Print committed messages addressed to the selected recipient."""
    if args.count < 1:
        print("give a positive number of messages", file=sys.stderr)
        sys.exit(2)
    try:
        cfg, state, caller = find_team(args)
        if state is None:
            raise Problem(f"no team state for {cfg.root}", "run factory.py up")
        labels = caller.get("labels", {}) if caller else {}
        if args.for_name is not None:
            name = args.for_name.lstrip("@")
        else:
            member = labels.get("factory.member") if labels.get("factory.team") and labels.get("factory.dir") else None
            name = member or cfg.operator["name"]
        rows = inbox_rows(open_log(state), name, args.count)
    except Problem as error:
        print(f"{error.what}. {error.fix}", file=sys.stderr)
        sys.exit(2)
    except (LogDamage, HcError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
    if not rows:
        print(f"no messages for @{name}")
    for msg, outcome in rows:
        sender = "factory" if msg["kind"] == "note" else f"@{msg['from']}"
        print(f"[Message {msg['id']} from {sender} at {msg['at']}: {outcome_word(outcome)}]")
        for line in msg["text"].splitlines():
            print(f"    {line}")


def report_leftover_drafts(cfg):
    if cfg.session_dir.is_symlink() or cfg.messages.is_symlink():
        return
    for directory in sorted(cfg.messages.glob("*")):
        try:
            if directory.is_symlink() or not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                try:
                    info = path.lstat()
                except FileNotFoundError:  # a pending send may have just finished
                    continue
                if stat.S_ISREG(info.st_mode):
                    minutes = max(0, int((time.time() - info.st_mtime) / 60))
                    print(f"leftover draft for @{directory.name} ({minutes} min old): {path}. Check delivery before resending or discarding.")
        except OSError as e:
            print(f"warning: cannot scan drafts in {directory}: {e}", file=sys.stderr)


class Checker:
    """Answers 'can this member launch?' and caches what each harness reports."""

    def __init__(self, cfg):
        self.cfg, self.cache = cfg, {}

    def probe(self, key, fn):
        if key not in self.cache:
            try:
                self.cache[key] = fn()
            except Problem as p:
                self.cache[key] = p
        if isinstance(self.cache[key], Problem):
            raise self.cache[key]
        return self.cache[key]

    def has_commits(self):
        try:
            return subprocess.run(["git", "-C", str(self.cfg.root), "rev-parse", "--verify", "--quiet", "HEAD"],
                                  capture_output=True).returncode == 0
        except OSError:
            return False

    def worktrees(self):
        """{path: branch or None} for every checkout of this repository, as git lists them."""
        out, path = {}, None
        for line in subprocess.run(["git", "-C", str(self.cfg.root), "worktree", "list", "--porcelain"],
                                   capture_output=True, text=True).stdout.splitlines():
            if line.startswith("worktree "):
                path = line[9:]
                out[path] = None
            elif line.startswith("branch refs/heads/"):
                out[path] = line[18:]
        return out

    def git(self, *args):
        """Exit status of a git command in the repository."""
        return subprocess.run(["git", "-C", str(self.cfg.root), *args], capture_output=True).returncode

    def check_worktree(self, p):
        """A member's worktree is its own branch's checkout and nobody else's. Two branch names can map to
        one path (`feature/a` and `feature-a`), and a path can hold something else already."""
        branch, path = p["branch"], p["dir"]
        if not self.probe("git", self.has_commits):
            raise Problem(f"a worktree needs a git repository with a commit in {self.cfg.root}",
                          "Run `git init` and make a first commit, or remove `worktree` from this member.")
        git = self.git
        if git("check-ref-format", "--branch", branch):
            raise Problem(f"{branch!r} is not a branch name git accepts",
                          f"Change `user`, or this member's `worktree`, in {CONF}.")
        change = f"Change `worktree` for one of them in {CONF}."
        for other in (plan(self.cfg, n) for n in self.cfg.members if n != p["name"]):
            if other["dir"] == path and other["branch"] != branch:
                raise Problem(f"its worktree for branch {branch} would be {path}, which is also the worktree of"
                              f" @{other['name']}'s branch {other['branch']}", change)
        trees = self.probe("worktrees", self.worktrees)
        if path in trees and trees[path] != branch:
            raise Problem(f"the worktree {path} is on branch {trees[path] or '(none)'}, not {branch}",
                          f"Run `git -C {path} switch {branch}`, or change this member's `worktree` in {CONF}.")
        if path not in trees and pathlib.Path(path).exists():
            raise Problem(f"the worktree path {path} exists but is not a checkout of this repository",
                          f"Move it away, or set `worktree_dir` in {CONF}.")
        held = [t for t, b in trees.items() if b == branch and t != path]
        if held:
            raise Problem(f"the worktree branch {branch} is already checked out in {held[0]}",
                          f"Give this member a branch of its own: `worktree = true`, or another branch name, in {CONF}.")

    def check(self, p):
        """-> member dict with a launch-ready model, or raises Problem."""
        harness, model, thinking = p["harness"], p["model"], p["thinking"]
        if harness not in TABLE:
            raise Problem(f"harness {harness!r} is not supported", f"Use one of: {', '.join(HARNESSES)}.")
        if not shutil.which(harness):
            raise Problem(f"{harness} is not installed (not on PATH)", TABLE[harness]["install"])
        if not model:
            raise Problem("no model set", f"Set `model` for this member in {CONF}; `factory.py harnesses` lists"
                                          " what this machine can use.")
        never = TABLE[harness].get("never", [])
        if thinking in never:
            raise Problem(f"thinking {thinking!r} is not allowed for {harness} (`never` in harnesses.toml)",
                          "Use a lower level such as high or xhigh.")
        if p.get("branch"):
            self.check_worktree(p)
        p = dict(p)
        p["model"], levels = CODE[harness].model(self, model, harness)
        if levels is not None and thinking not in levels:
            ok = [lv for lv in levels if lv not in never]
            raise Problem(f"{harness} {p['model']} has no thinking level {thinking!r}", f"Use one of: {', '.join(ok)}.")
        return p


def check_all(cfg, plans):
    """-> (ready members, [(member, Problem)])."""
    generic = {"root", "ubuntu", "runner", "user", "admin", "ec2-user", "vscode", "codespace", "node"}
    if "user" not in cfg.team and cfg.user in generic and any(p["branch"] == f"{cfg.user}/{p['name']}" for p in plans):
        print(f"note: worktree branches will start with {cfg.user!r}, this machine's user name, which other people's"
              f" machines may share. Set `user` in {CONF}, e.g. user = \"peter\".", file=sys.stderr)
    checker, ready, problems = Checker(cfg), [], []
    for p in plans:
        try:
            ready.append(checker.check(p))
        except Problem as e:
            problems.append((p, e))
    return ready, problems


def checked(cfg, plans):
    """The plans, launch-ready. Exits 2, having started nothing, if any member fails the check."""
    ready, problems = check_all(cfg, plans)
    if problems:
        print_plan(plans)
        report_problems(problems)
        sys.exit(2)
    return ready


def report_problems(problems):
    print(f"\nCannot launch: {len(problems)} member(s) failed the check. Nothing was started.\n")
    for p, e in problems:
        print(f"- @{p['name']} ({p['role']}, {p['harness']} {p['model'] or '?'}, thinking {p['thinking']}):")
        print(f"    problem: {e.what}")
        print(f"    fix:     {e.fix}")
    print(f"\nFix these, or change the member in {CONF}, then run again.")


# ================================================================ harness code: one block per harness
# model(checker, model, harness) -> (launch-ready model id, its thinking levels or None); raises Problem
# argv(m, prompt_file, resume)   -> (native arguments, pane environment); resume is a herdr session ref

def session_id(ref):
    """A session id from a herdr session ref, which may hold an id or a file path."""
    if ref.get("kind") != "path":
        return ref["value"]
    name = pathlib.Path(ref["value"]).stem
    uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", name)
    return uuids[-1] if uuids else name


def records(path):
    with path.open() as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ---- claude
def claude_settings(level, draft_dir=None):
    """Launch settings for a member. herdr's messaging commands run outside a sandbox the user has turned on,
    when they are the whole command line. At `full` herdr's socket is also allowed inside it, so a message gets
    through in any command shape (Linux can only allow every Unix socket; macOS can name herdr's). The last key
    skips the warning Claude Code shows the first time it starts in bypassPermissions mode."""
    say = shlex.join(["python3", str(SKILL / "scripts/factory.py"), "say"]) + " *"
    sandbox = {"excludedCommands": [say, "herdr agent list"]}
    if level == "full":
        sock = os.environ.get("HERDR_SOCKET_PATH")
        sandbox["network"] = {"allowUnixSockets": [sock]} if sys.platform == "darwin" and sock else {"allowAllUnixSockets": True}
    permissions = {"allow": [f"Bash({cmd})" for cmd in sandbox["excludedCommands"]]}
    if draft_dir:
        permissions["additionalDirectories"] = [str(draft_dir)]
        permissions["allow"].append(f"Write(/{draft_dir}/**)")
        sandbox["filesystem"] = {"allowWrite": [str(draft_dir)]}
    return json.dumps({"promptSuggestionEnabled": False, "sandbox": sandbox, "skipDangerousModePermissionPrompt": True,
                       "permissions": permissions},
                      separators=(",", ":"))


def model_claude(checker, model, harness):
    bare, t = model.removeprefix("anthropic/"), TABLE["claude"]
    if not bare.startswith("claude-") and bare not in t["aliases"]:
        raise Problem(f"{model} is not a Claude model; Claude Code runs Claude models only",
                      "Use a Claude model, or set this member's harness to the one that serves this model.")
    if bare not in t["models"] and bare not in t["aliases"]:  # Claude Code has no listing; it rejects a bad id itself
        print(f"note: {bare} is not among the Claude models in harnesses.toml. Claude Code will say at launch if it"
              " does not exist.", file=sys.stderr)
    return bare, t["thinking"]


def argv_claude(m, prompt_file, sess):
    argv = ["--model", m["model"], "--effort", m["thinking"], "--append-system-prompt-file", str(prompt_file),
            "--name", m["name"], "--settings", str(prompt_file.with_suffix(".settings.json"))]
    return argv + (sess["args"] if sess else []), {}


# ---- codex
def codex_models():
    home = pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex")
    path = home / "models_cache.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise Problem(f"no readable Codex model list at {path}", "Run `codex` once while logged in so it caches its models.")
    return {m["slug"]: [lv["effort"] for lv in m.get("supported_reasoning_levels", [])]
            for m in data.get("models", []) if m.get("slug") and m.get("visibility", "list") == "list"}


def model_codex(checker, model, harness):
    bare, models = model.removeprefix("openai/"), checker.probe("codex", codex_models)
    if bare not in models:
        raise Problem(f"Codex does not offer {bare}",
                      f"Codex lists: {', '.join(sorted(models))}. Pick one, or run `codex` to refresh its list.")
    return bare, models[bare]


def argv_codex(m, prompt_file, sess):
    # Codex has no way to read developer instructions from a file, and the full text would not fit the line
    # macOS lets herdr type into a shell. So the system level carries one line that names the file, and the
    # full text arrives in the member's first message.
    pointer = (f"You are @{m['name']}, the {m['role']} on a team of coding agents. Your standing instructions are in"
               f" {prompt_file}. Read that file before your first task, and again whenever your context was"
               " compacted or you are unsure of the rules.")
    # -C: a resumed session goes back to the directory it was started in, whatever the pane's is. Naming the
    # member's directory keeps it where factory.toml says, also after its worktree changed.
    argv = ["-m", m["model"], "-C", m["dir"], "-c", f"model_reasoning_effort={json.dumps(m['thinking'])}",
            "-c", f"developer_instructions={json.dumps(pointer)}"]
    if m["permissions"] == "guarded":
        argv += ["--add-dir", str(m["inbox_dir"])]
    return (sess["args"] if sess else []) + argv, {}   # resume <id> goes before the options


# ---- pi and omp (omp keeps pi's flags and session format)
def model_pi(checker, model, harness):
    query = model.rsplit("/", 1)[-1]
    argv = ["pi", "--list-models", query] if harness == "pi" else ["omp", "models", query]
    env = {"PI_OFFLINE": "1"} if harness == "pi" else None
    listed = listed_models(checker.probe((harness, query), lambda: run_probe(argv, env)))
    return resolve_listed(model, listed, harness), TABLE[harness]["thinking"]


def argv_pi(m, prompt_file, sess):
    argv = ["--model", m["model"], "--thinking", m["thinking"], "--append-system-prompt", str(prompt_file)]
    if m["harness"] == "pi":
        argv += ["--name", m["name"]]
    return argv + (sess["args"] if sess else []), {}


# ---- opencode (v2+)
def model_opencode(checker, model, harness):
    listed = listed_models(checker.probe("opencode-models", lambda: run_probe(["opencode", "models", "--standalone"])))
    return resolve_listed(model, listed, harness), None  # thinking is a per-model variant; OpenCode reports a bad one at start


def argv_opencode(m, prompt_file, sess):
    # The interface herdr can watch has no model flag, so the model comes from inline config in the pane's
    # environment. --standalone: OpenCode's shared background server ignores a client's own config.
    model = m["model"] + (f"#{m['thinking']}" if m["thinking"] != "default" else "")
    argv = ["--standalone"] + (sess["args"] if sess else [])
    return argv, {"OPENCODE_CONFIG_CONTENT": json.dumps({"model": model})}


# ---- agy (Antigravity CLI)
def agy_effort(model):
    suffix = model.rsplit("-", 1)[-1]
    return suffix if suffix in TABLE["agy"]["thinking"] else None


def agy_choice(member):
    table = TABLE["agy"]
    model = member.get("model", table["default_model"])
    default = table["default_thinking"] if model in table["models"] else "default"
    return model, str(member.get("thinking") or agy_effort(model) or default)


def agy_models():
    return {line.partition("\t")[0] for line in run_probe(["agy", "models"]).splitlines() if "\t" in line}


def model_agy(checker, model, harness):
    listed = checker.probe("agy-models", agy_models)
    levels = [lv for lv in TABLE["agy"]["thinking"] if f"{model}-{lv}" in listed]
    if model in listed:
        levels.append(agy_effort(model) or "default")
    if not levels:
        raise Problem(f"agy does not offer {model}", "Choose a model listed by `agy models`.")
    return model, levels


def argv_agy(m, prompt_file, sess):
    model = m["model"]
    if m["thinking"] != "default" and not agy_effort(model):
        model += "-" + m["thinking"]
    return ["--model", model] + (sess["args"] if sess else []), {}


Harness = collections.namedtuple("Harness", "model argv")
CODE = {
    "claude": Harness(model_claude, argv_claude),
    "codex": Harness(model_codex, argv_codex),
    "pi": Harness(model_pi, argv_pi),
    "omp": Harness(model_pi, argv_pi),
    "opencode": Harness(model_opencode, argv_opencode),
    "agy": Harness(model_agy, argv_agy),
}


def launch_argv(m, prompt_file, sess=None):
    """-> (native argv, env): the harness's own arguments with the session plan's, then the member's permission
    level and any extra `args` from factory.toml."""
    argv, env = CODE[m["harness"]].argv(m, prompt_file, sess)
    return argv + TABLE[m["harness"]]["permissions"][m["permissions"]] + m["args"], env


# ================================================================ sessions (launch_layout.md §3)

SESSIONS = "sessions.jsonl"
SESSION_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")   # one argv item, never read as an option
SESSION_HOWS = ("env", "id", "dir", "new")
RESUME_ARGS = {"claude": lambda v: ["--resume", v], "codex": lambda v: ["resume", v],
               "agy": lambda v: ["--conversation", v], "opencode": lambda v: ["--session", v],
               "pi": lambda v: ["--session", v], "omp": lambda v: [f"--resume={v}"]}
FRESH_ARGS = {"claude": lambda v: ["--session-id", v], "opencode": lambda v: ["--session", v]}


def _session_entry(line):
    """A sessions.jsonl entry, or None for a damaged line."""
    try:
        e = json.loads(line, parse_constant=_reject_constant)
    except ValueError:
        return None
    if not isinstance(e, dict) or e.get("how") not in SESSION_HOWS \
            or not all(isinstance(e.get(k), str) for k in ("member", "harness", "term", "at")) \
            or not (e.get("model") is None or isinstance(e.get("model"), str)):
        return None
    value = e.get("value")
    if e["how"] == "new":
        return e if value is None else None
    if not isinstance(value, str):
        return None
    good = os.path.isabs(value) if e["how"] == "dir" else SESSION_VALUE.fullmatch(value)
    return e if good else None


def _read_sessions(fd):
    """(newest entry per member, damaged lines skipped, file exists, ends with a newline), relative to fd."""
    try:
        f = private_open(SESSIONS, os.O_RDONLY, fix_mode=False, dir_fd=fd)
    except FileNotFoundError:
        return {}, 0, False, True
    with os.fdopen(f, "rb") as handle:
        data = handle.read()
    newest, skipped = {}, 0
    for line in data.split(b"\n"):
        if line.strip():
            e = _session_entry(line)
            if e is None:
                skipped += 1
            else:
                newest[e["member"]] = e
    return newest, skipped, True, data.endswith(b"\n") or not data


def _sessions(state, decide=None):
    """The one reader and writer of sessions.jsonl, under the team lock: shared to read; exclusive to read, let
    decide(newest) return an entry or None, and append it in one write followed by fsync. A torn last line (a writer
    killed mid-write) gets its newline first. -> (newest entry per member, damaged lines skipped)."""
    with Log(state)._locked(exclusive=decide is not None) as fd:
        if fd is None:
            return {}, 0
        newest, skipped, existed, whole = _read_sessions(fd)
        entry = decide(newest) if decide else None
        if entry is None:
            return newest, skipped
        line = (b"" if whole else b"\n") + json.dumps(entry, separators=(",", ":")).encode() + b"\n"
        out = private_open(SESSIONS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, dir_fd=fd)
        try:
            if os.write(out, line) != len(line):
                raise OSError(errno.EIO, f"short write to {pathlib.Path(state) / SESSIONS}")
            os.fsync(out)
        finally:
            os.close(out)
        if not existed:
            os.fsync(fd)   # the new file's directory entry
        return {**newest, entry["member"]: entry}, skipped


def saved_sessions(state):
    """(newest entry per member, how many damaged lines were skipped)."""
    return _sessions(state)


def sessions_writable(state):
    """down's check before it stops anyone: None, or why sessions.jsonl cannot be appended to and synced."""
    try:
        with Log(state)._locked(exclusive=True) as fd:
            out = private_open(SESSIONS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, dir_fd=fd)
            try:
                os.fsync(out)
            finally:
                os.close(out)
    except (OSError, LogDamage) as error:
        return str(error)
    return None


def _member_model(m):
    return agy_choice(m)[0] if m["harness"] == "agy" else m.get("model")


def record_session(cfg, state, member, term, environ=os.environ):
    """Called by say for a member: save the harness's session_env value when it should. -> a warning or None."""
    m = cfg.members[member]
    harness, t = m["harness"], TABLE[m["harness"]]
    value = environ.get(t["session_env"]) if t.get("session_env") else None
    if not value or not SESSION_VALUE.fullmatch(value):
        return None
    follow = t.get("session_follow", True)

    def decide(newest):
        e = newest.get(member)
        if e is not None and e["term"] == term:
            if not follow and e["how"] == "env":
                return None    # once per launch: later captures from this terminal are ignored
            if follow and e["harness"] == harness and e["value"] == value:
                return None
        return {"member": member, "harness": harness, "model": _member_model(m), "term": term, "how": "env",
                "value": value, "at": utc_now()}
    try:
        _sessions(state, decide)
    except (OSError, LogDamage) as error:
        return f"warning: could not record your session: {error}"
    return None


def launch_entry(state, m, term, how, value):
    """Called by up once m's brief is delivered: the launch's fallback entry. Never replaces an entry say already
    made for this terminal."""
    def decide(newest):
        e = newest.get(m["name"])
        if e is not None and e["term"] == term:
            return None
        return {"member": m["name"], "harness": m["harness"], "model": m.get("model"), "term": term, "how": how,
                "value": value, "at": utc_now()}
    _sessions(state, decide)


def session_folder(state, m):
    return pathlib.Path(state) / "sessions" / f"{m['name']}-{m['harness']}"


def _provider(model):
    return model.split("/", 1)[0] if model and "/" in model else None


def compatible(entry, m):
    """Whether m may resume entry's conversation: same harness, a value, and a model resume_model allows (a model
    with no provider prefix does not tell, and is allowed)."""
    if entry is None or entry["harness"] != m["harness"] or entry["value"] is None:
        return False
    if TABLE[m["harness"]].get("resume_model", "any") == "provider":
        a, b = _provider(entry.get("model")), _provider(m.get("model"))
        return a is None or b is None or a == b
    return True


def session_plan(m, state, entry, fresh):
    """{"args", "resume", "assigned", "notice"}: the session arguments for m's launch from its newest entry. A member
    that ran before with no entry at all gets its notice from the caller, which knows its finished record."""
    harness, t = m["harness"], TABLE[m["harness"]]
    sess = {"args": [], "resume": False, "assigned": None, "notice": None}
    if t.get("session") == "dir":
        folder = session_folder(state, m)
        sess["args"] = ["--session-dir", str(folder)] if harness == "pi" else [f"--session-dir={folder}"]
    if not fresh and compatible(entry, m):
        sess["resume"] = True
        sess["args"] += ["--continue"] if entry["how"] == "dir" else RESUME_ARGS[harness](entry["value"])
        return sess
    if entry is not None and not fresh:
        unrecorded = entry["value"] is None or entry["harness"] == harness and entry.get("model") == m.get("model")
        sess["notice"] = (f"@{m['name']} starts in a new conversation: none was recorded for it" if unrecorded
                          else f"@{m['name']} starts fresh: its saved conversation is for {entry['harness']}"
                               f" {entry.get('model')}")
    if t.get("session") == "id":
        sess["assigned"] = str(uuid.uuid4())
        sess["args"] += FRESH_ARGS[harness](sess["assigned"])
    return sess


def v1_session(m, ref):
    """v1's launch: its herdr session ref as a session plan, until 5a's up replaces it."""
    if not ref:
        return None
    value = ref["value"] if m["harness"] in ("pi", "omp") else session_id(ref)
    return {"args": RESUME_ARGS[m["harness"]](value), "resume": True, "assigned": None, "notice": None}


# ================================================================ starting members (launch_layout.md §4)

def team_key(root):
    """The team's name in labels and the cache: v1's cache key. #168 owns it; this copy goes at the rebase."""
    root = pathlib.Path(root)
    return f"{root.name[:24]}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"


def private_dir(parent_fd, name, shown):
    """name below parent_fd, created 0700 when missing, never through a symbolic link: its descriptor."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        raise Problem(f"expected a private directory at {shown}, found a symbolic link or another file ({error})",
                      f"Remove {shown} and run the command again.") from error
    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
        os.fchmod(fd, 0o700)
    return fd


def write_private(dir_fd, name, text):
    fd = private_open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, dir_fd=dir_fd)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def member_settings(level, message_dir):
    """Claude Code's launch settings for a v2 member (§7): say and inbox run outside a sandbox the owner turned
    on, and at full the hc socket is reachable inside it. The last key skips the first bypassPermissions warning.
    It replaces v1's claude_settings when v1's launch goes."""
    script = ["python3", str(SKILL / "scripts/factory.py")]
    commands = [shlex.join([*script, "say"]) + " *", shlex.join([*script, "inbox"]) + " *"]
    sandbox = {"excludedCommands": commands, "filesystem": {"allowWrite": [str(message_dir)]}}
    if level == "full":
        sock = os.environ.get("HC_TERMINAL_SOCKET")
        sandbox["network"] = ({"allowUnixSockets": [sock]} if sys.platform == "darwin" and sock
                              else {"allowAllUnixSockets": True})
    permissions = {"allow": [f"Bash({c})" for c in commands] + [f"Write(/{message_dir}/**)"],
                   "additionalDirectories": [str(message_dir)]}
    return json.dumps({"promptSuggestionEnabled": False, "sandbox": sandbox, "skipDangerousModePermissionPrompt": True,
                       "permissions": permissions}, separators=(",", ":"))


def start_member(cfg, state, hc, m, operator, sess):
    """Start m's harness, by itself, in a new terminal (its worktree exists). -> the terminal record; raises Problem."""
    name, harness = m["name"], m["harness"]
    state = pathlib.Path(state)
    exe = shutil.which(harness)
    if exe is None:
        raise Problem(f"{harness} is not on PATH", TABLE[harness]["install"])
    message_dir = state / "messages" / name
    try:
        root = Log(state)._open_state(create=True)
    except OSError as error:
        raise Problem(f"cannot open the state directory {state}: {error}", "Fix its permissions, then run up again.")
    opened = [root]
    try:
        messages = private_dir(root, "messages", state / "messages")
        opened.append(messages)
        opened.append(private_dir(messages, name, message_dir))
        if TABLE[harness].get("session") == "dir":
            sessions = private_dir(root, "sessions", state / "sessions")
            opened.append(sessions)
            folder = session_folder(state, m)
            opened.append(private_dir(sessions, folder.name, folder))
        prompt_file = state / f"{name}.md"
        # inbox_dir: guarded Codex's --add-dir <state>
        m = {**m, "draft_dir": message_dir, "inbox_dir": state, "instructions": prompt_file}
        try:
            write_private(root, prompt_file.name, member_prompt(m, operator, cfg))   # rewritten at every launch
            if harness == "claude":
                write_private(root, f"{name}.settings.json", member_settings(m["permissions"], message_dir))
        except OSError as error:
            raise Problem(f"cannot write {prompt_file}: {error}", "Fix the state directory, then run up again.")
    finally:
        for fd in opened:
            os.close(fd)
    argv, env = launch_argv(m, prompt_file, sess)
    labels = {"factory.team": team_key(cfg.root), "factory.kind": "member", "factory.member": name,
              "factory.dir": str(state), "name": "@" + name}
    try:
        return hc.run([exe, *argv], cwd=m["dir"], env={"PATH": os.environ.get("PATH", ""), **env}, labels=labels)
    except (HcError, HcDown) as error:
        raise Problem(f"hc could not start @{name}: {error}", "Check that hc runs (hc status), then run up again.")


def _last_line(hc, address):
    try:
        lines = [line.strip() for line in hc.read(address).splitlines() if line.strip()]
    except (HcError, HcDown):
        return ""
    return lines[-1] if lines else ""


def await_ready(hc, started, timeout=LAUNCH_TIMEOUT, clock=time):
    """started: {member: (its terminal record, its harness's dialogs)}. Polls every member every 0.5 s until each
    is settled or the deadline passes. -> {member: None when ready, else the reason it is not}."""
    deadline = clock.monotonic() + timeout
    outcome, answered, shown, words = {}, {name: set() for name in started}, {}, {}
    while True:
        for name, (record, dialogs) in started.items():
            if name in outcome:
                continue
            address = record["address"]
            try:
                t = hc.get(address)
                if t["state"] == "finished":
                    last = _last_line(hc, address)
                    outcome[name] = f"exited (code {t.get('exit_code')})" + (f": {last}" if last else "")
                    continue
                word = words[name] = (t.get("status") or {}).get("state")
                if word == "working":
                    continue
                screen = "".join(hc.read(address).split())
                asked = next(((text, keys) for text, keys in dialogs if "".join(text.split()) in screen), None)
                shown[name] = asked and asked[0]
                if asked:
                    text, keys = asked
                    if not keys:
                        outcome[name] = ("waits on its trust dialog: accept it in its pane, then run:"
                                         f" factory.py brief --send {name}")
                    elif text not in answered[name]:
                        hc.keys(address, keys)
                        answered[name].add(text)
                    continue
                if word == "idle":
                    outcome[name] = None
            except (HcError, HcDown) as error:
                outcome[name] = f"could not be watched: {error}"
        if len(outcome) == len(started):
            return outcome
        if clock.monotonic() >= deadline:
            for name in started:
                if name not in outcome:
                    text = shown.get(name)
                    outcome[name] = (f'still asks "{text}" after the answer was sent' if text in answered[name]
                                     else f"not ready after {timeout:g} s ({words.get(name) or 'no status'})")
            return outcome
        clock.sleep(0.5)


# ================================================================ briefs

def tag(name):
    return f"[from @{name}]:"


def member_prompt(m, operator, cfg):
    """Standing instructions for one member: templates/member-prompt.md, filled in with who it is, its role,
    where it works, and the rules that depend on that."""
    if m["branch"]:
        where = (f"You work in your own git worktree, `{m['dir']}`, on branch `{m['branch']}`. The main checkout is"
                 f" `{cfg.root}`, on branch `{cfg.main_branch}`. Stay in your worktree and on your branch.")
        rules = [f"- Commit your work on `{m['branch']}` as you go, in small commits with clear messages. That is the"
                 " only branch you commit to. Stage only task files, never `git add -A` or `git add .`;"
                 " harness sandboxes may expose unrelated placeholders. Never push, and never merge into or check out another branch:"
                 f" @{operator} reviews your branch and merges it. Before a new task, bring your branch up to date"
                 f" with `git merge {cfg.main_branch}`. If a commit fails (for example your sandbox blocks git),"
                 f" tell @{operator} or the teammate concerned and agree who commits it for you."]
    else:
        where = f"You work in `{cfg.root}`, the checkout you share with every teammate who has no worktree of their own."
        rules = ["- Do not stage files. Never run `git commit`, `git merge` or `git push`."
                 f" @{operator} stages only task files and commits."]
    if m["readonly"]:
        rules.append("- You are read-only: do not edit the project's files. You may read anything, build and run"
                     f" tests. Write only scratch files outside the repo, or a document @{operator} asks you for.")
    text = (SKILL / "templates/member-prompt.md").read_text()
    for key, value in {"role_rules": cfg.roles[m["role"]][1], "name": m["name"], "role": m["role"], "operator": f"@{operator}",
                       "draft_dir": str(m["draft_dir"]),
                       "draft": shlex.quote(str(m["draft_dir"])) + "/FILENAME",
                       "say": shlex.join(["python3", str(SCRIPT), "say"]),
                       "brief": str(cfg.brief_file), "where": where, "rules": "\n".join(rules),
                       "instructions": str(m.get("instructions") or cfg.session_dir / f"{m['name']}.md"),
                       "notes": str(SKILL / "references/notes.md")}.items():
        text = text.replace("{" + key + "}", value)
    return text.rstrip() + "\n"


def render_team_brief(cfg, members, operator, closing=None):
    """The team brief as sent: a roster built from factory.toml now, then FACTORY.md, then the names."""
    L = cfg.operator
    trees = any(m["branch"] for m in members)
    out = [f"# Team brief (from @{operator})", "",
           f"We are a team of {len(members) + 1} agents, each in its own herdr pane, working on the repo"
           f" `{cfg.root}`. This brief lives in `{BRIEF}` at its root.", "",
           "## Roles", "",
           "| Name | Agent | Role |" + (" Works in |" if trees else ""), "|---|---|---|" + ("---|" if trees else "")]
    operator_agent = ", ".join(x for x in (TABLE.get(L.get("harness"), {}).get("label", L.get("harness")),
                                       L.get("model"), L.get("thinking")) if x)
    out.append(f"| **@{operator}** | {operator_agent} | {L.get('notes') or 'Operator.'} |"
               + (" the main checkout |" if trees else ""))
    for m in members:
        meta, _ = cfg.roles[m["role"]]
        where = f" branch `{m['branch']}` in `{m['dir']}` |" if m["branch"] else " the main checkout |"
        out.append(f"| **@{m['name']}** | {TABLE[m['harness']]['label']}, {m['model']}, {m['thinking']}"
                   f" | {m.get('notes') or meta['description']} |" + (where if trees else ""))
    if cfg.body:  # FACTORY.md without its own title and its notes to whoever edits it
        out += ["", re.sub(r"\A# .*\n+", "", re.sub(r"<!--.*?-->\n?", "", cfg.body, flags=re.S)).strip()]
    if trees:
        out += ["", "## Worktrees", "",
                f"To review a teammate's branch: `git diff {cfg.main_branch}...<branch>` shows its commits, from any"
                " checkout, and `git -C <its path> status` shows what it has not committed yet. When you ask for a"
                " review, name your branch."]
    names = ", ".join("`" + m["name"] + "`" for m in members)
    messaging = " Use the standalone `factory.py say` command in your standing instructions to message one or more teammates."
    out += ["", "## Talking to each other", "",
            f"Everyone has an @name, which is also their herdr pane's name: {names}, and `{operator}` for the operator."
            + messaging,
            "", closing or "That is the whole team brief. No reply needed."]
    return "\n".join(out) + "\n"


def message_for(cfg, m, members, operator, closing=None):
    """What a member is sent: its standing instructions when its harness cannot hold them as a system prompt,
    then the team brief."""
    brief = render_team_brief(cfg, members, operator, closing)
    if TABLE[m["harness"]]["prompt"] == "file":
        return brief
    return f"{tag(operator)} Your standing instructions, then the team brief.\n\n{member_prompt(m, operator, cfg)}\n{brief}"


# ================================================================ herdr: the team is the panes labeled with member names

def herdr(*args, check=True, timeout=HERDR_TIMEOUT):
    """Run a herdr command -> its result. With check=False a failure returns {"error": reason} instead of exiting."""
    try:
        p = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        p = subprocess.CompletedProcess(args, 1, "", f"no answer from herdr in {timeout:g}s")
    if p.returncode != 0:
        msg = p.stderr.strip() or p.stdout.strip() or f"exit {p.returncode}"
        try:  # herdr reports errors as JSON on stderr
            err = json.loads(msg).get("error")
            msg = ": ".join(str(err[k]) for k in ("code", "message") if err.get(k)) if isinstance(err, dict) else str(err)
        except (json.JSONDecodeError, AttributeError):
            pass
        if "Operation not permitted" in msg or "PermissionDenied" in msg:
            msg += ("\nherdr's socket is blocked, probably by a sandbox. In Claude Code add `herdr *` to"
                    " sandbox.excludedCommands, or run with the sandbox off.")
        if check:
            raise SystemExit(f"herdr {' '.join(args[:3])}: {msg}")
        return {"error": msg}
    try:
        return json.loads(p.stdout).get("result") or {}
    except (json.JSONDecodeError, AttributeError):
        return {"text": p.stdout}  # Some herdr commands return plain text.


def integration_target(harness):
    return "antigravity-cli" if harness == "agy" else harness


def integration_statuses():
    """Read Herdr's text status. Omitted and unrecognized rows remain unknown."""
    try:
        got = herdr("integration", "status", check=False, timeout=5)
    except OSError as e:
        return {}, str(e)
    if error := got.get("error"):
        return {}, error.splitlines()[0]
    rows = re.findall(r"^([^:\n]+): (not installed|(?:current|needs repair|outdated) \([^)]+\)) \(.+\)$",
                      got.get("text", ""), re.MULTILINE)
    return dict(rows), None


def warn_integrations(plans):
    if not plans:
        return
    statuses, error = integration_statuses()
    verify = "Run `herdr integration status` to verify."
    if error:
        print(f"warning: could not check herdr integrations ({error}). {verify}", file=sys.stderr)
        return
    for harness in dict.fromkeys(p["harness"] for p in plans):
        target, label = integration_target(harness), TABLE[harness]["label"]
        state = statuses.get(target)
        if not state:
            print(f"warning: could not verify the {label} herdr integration. {verify}", file=sys.stderr)
        elif not state.startswith("current "):
            print(f"warning: {label} herdr integration {state}. Herdr may miss session IDs."
                  f" Ask the user before running `herdr integration install {target}`."
                  " It changes files shared with other factories.", file=sys.stderr)


def require_herdr():
    if os.environ.get("HERDR_ENV") != "1":
        raise SystemExit("not inside herdr (HERDR_ENV != 1). Start herdr and run this from the operator's pane.")


class Team:
    """The running team as herdr sees it: one team per herdr session. A member's pane carries the member's
    @name as its label, and herdr knows the pane, the agent in it (if any) and its harness session."""

    def __init__(self, cfg, exclude_pane=None, record_sessions=True):
        self.cfg = cfg
        # `pane list` has labels and sessions but no agent names; `agent list` has the names.
        self.agent_panes = {a["name"]: a.get("pane_id") for a in herdr("agent", "list").get("agents", [])
                            if a.get("name") and (not exclude_pane or a.get("pane_id") != exclude_pane)}
        self.panes = {}
        for p in herdr("pane", "list").get("panes", []):
            if p["pane_id"] == exclude_pane:
                continue
            label = p.get("label")
            if label and (label not in self.panes or p.get("agent")):  # a label can repeat: the live pane wins
                self.panes[label] = p
        if record_sessions:
            remember_sessions(cfg, {n: self.pane(n) for n in [cfg.operator["name"], *cfg.members]})

    def pane(self, name):
        return self.panes.get(name)

    def live(self, name):
        """An agent is running in the member's labeled pane."""
        p = self.panes.get(name)
        return bool(p and p.get("agent"))

    def members(self):
        """Member plans for the members that are running, in factory.toml order."""
        return [plan(self.cfg, n) for n in self.cfg.members if self.live(n)]

    def status(self, name):
        p = self.panes.get(name)
        if not p:
            return "not started"
        if not p.get("agent"):
            return "gone"
        return p.get("agent_status", "?")


def caller_agent():
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise SystemExit("HERDR_PANE_ID is not set; run this from the operator's herdr pane.")
    got = herdr("agent", "get", pane, check=False)
    info = got.get("agent") or {}
    if "error" in got and "not_found" not in got["error"]:  # herdr itself failed: say that, not "no agent"
        raise SystemExit(f"herdr agent get {pane}: {got['error']}")
    if not info or info.get("pane_id") != pane:
        raise SystemExit(f"herdr sees no agent in this pane ({pane}). Run factory.py from the operator's pane.")
    return info


def ensure_operator(cfg, tab=False, info=None):
    """Name and label the operator's pane, and its tab when launching. Return the name."""
    if info is None:
        info = caller_agent()
    pane = info["pane_id"]
    name = cfg.operator["name"]
    if info.get("name") != name:
        clash = [a for a in herdr("agent", "list").get("agents", []) if a.get("name") == name]
        if clash:
            raise SystemExit(f"another agent is already named @{name} ({clash[0].get('pane_id')}). One team runs per"
                             f" herdr session: rename that agent, change [operator] name in {CONF}, or use another session.")
        herdr("agent", "rename", pane, name)
        print(f"named this pane's agent @{name}")
    herdr("pane", "rename", pane, name, check=False)
    if tab and os.environ.get("HERDR_TAB_ID"):
        herdr("tab", "rename", os.environ["HERDR_TAB_ID"], TABS["operator"], check=False)
    return name


def ensure_worktree(cfg, m):
    """Create the member's checkout if it is not there (the check made sure an existing one is the right one).
    -> None, or why git could not."""
    path = pathlib.Path(m["dir"])
    if path.exists():
        return None
    git = ["git", "-C", str(cfg.root)]
    known = subprocess.run(git + ["rev-parse", "--verify", "--quiet", f"refs/heads/{m['branch']}"],
                           capture_output=True).returncode == 0
    add = ["worktree", "add", str(path), m["branch"]] if known else [
        "worktree", "add", "-b", m["branch"], "--no-track", str(path),
    ]
    p = subprocess.run(git + add, capture_output=True, text=True)
    if p.returncode:
        return f"could not create the worktree {path}: {p.stderr.strip()}"
    print(f"created worktree {path} on branch {m['branch']}")


# ================================================================ launching and sending

def place(team, m, env):
    """A new pane for a member in the team tab. The first member opens the tab. Each later one splits the
    tab's largest pane along its longer side, so the panes stay a balanced grid however many members join."""
    flags = ["--cwd", m["dir"], "--no-focus"] + [x for k, v in env.items() for x in ("--env", f"{k}={v}")]
    anchor = next((p["pane_id"] for n, p in reversed(team.panes.items()) if n in team.cfg.members), None)
    if not anchor:
        ws = ["--workspace", os.environ["HERDR_WORKSPACE_ID"]] if os.environ.get("HERDR_WORKSPACE_ID") else []
        return herdr("tab", "create", "--label", TABS["team"], *ws, *flags)["root_pane"]["pane_id"]
    direction = "down"
    panes = (herdr("pane", "layout", "--pane", anchor, check=False).get("layout") or {}).get("panes") or []
    if panes:
        big = max(panes, key=lambda p: p["rect"]["width"] * p["rect"]["height"])
        anchor = big["pane_id"]
        # a terminal cell is about twice as tall as wide, and an agent needs width more than height
        direction = "right" if big["rect"]["width"] >= 2.5 * big["rect"]["height"] else "down"
    return herdr("pane", "split", anchor, "--direction", direction, *flags)["pane"]["pane_id"]


def launch(cfg, team, m, operator, timeout, resume=None):
    """Start one member in its own labeled pane: the one it had if that is free, else a new pane in the team
    tab. -> None, or why it did not come up."""
    prompt_file = cfg.session_dir / f"{m['name']}.md"
    for directory in (cfg.session_dir, cfg.messages, m["draft_dir"]):
        if directory.is_symlink():
            return f"expected a private directory at {directory}, found a symbolic link"
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as e:
            return f"could not prepare the message directory: {e}"
    prompt_file.write_text(member_prompt(m, operator, cfg))  # written fresh at each launch
    if m["harness"] == "claude":
        # Keep the typed launch command below macOS's 1024-byte line limit.
        prompt_file.with_suffix(".settings.json").write_text(
            claude_settings(m["permissions"], m["draft_dir"]))
    argv, env = launch_argv(m, prompt_file, v1_session(m, resume))
    err = ensure_worktree(cfg, m) if m["branch"] else None
    if err:
        return err
    old = team.pane(m["name"])
    here = pathlib.Path((old or {}).get("foreground_cwd") or (old or {}).get("cwd") or "/").resolve()
    if old and not old.get("agent") and not env and here == pathlib.Path(m["dir"]).resolve():
        pane = old["pane_id"]  # back in the member's own pane, which is still in the member's directory
    else:
        pane = place(team, m, env)
        if old:  # a pane's environment is set when it is created, so this member got a new one
            herdr("pane", "rename", old["pane_id"], "--clear", check=False)
        herdr("pane", "rename", pane, m["name"])
        team.panes[m["name"]] = {"pane_id": pane, "label": m["name"]}
    started = herdr("agent", "start", m["name"], "--kind", m["harness"], "--pane", pane, "--timeout", str(timeout),
                    "--", *argv, check=False, timeout=timeout / 1000 + 30)
    # Answer known startup questions once. Other startup delays settle based on herdr's status.
    dialogs = TABLE[m["harness"]].get("dialogs", [])
    answered, waiting = {}, lambda: "agent_not_ready" in started.get("error", "")
    for _ in range(8):
        asked = []
        if dialogs:
            # A narrow pane wraps a question over several lines, so compare without whitespace.
            screen = "".join(herdr("pane", "read", pane, "--source", "visible", check=False).get("text", "").split())
            asked = [d for d in dialogs if "".join(d[0].split()) in screen]
        for text, keys in asked:
            if text not in answered:
                answered[text] = herdr("pane", "send-keys", pane, *keys, check=False).get("error")
        if waiting() and herdr("agent", "get", pane, check=False).get("agent", {}).get("agent_status") in ("idle", "done"):
            started = {}
        if not asked and not waiting():
            break
        time.sleep(SETTLE / 8)
    else:
        if asked and "error" not in started:
            why = answered.get(asked[0][0])
            started = {"error": f"it still asks {asked[0][0]!r} after the answer was sent" + (f" ({why})" if why else "")}
    if "error" in started:
        return f"{started['error']} (pane {pane})"
    team.panes[m["name"]]["agent"] = m["harness"]
    how = "resumed" if resume else "started"
    where = f", worktree {m['dir']}" if m["branch"] else ""
    print(f"{how} @{m['name']} ({m['role']}) on {m['harness']} {m['model']}, thinking {m['thinking']},"
          f" permissions {m['permissions']} in {pane}{where}")


def delivery_refused(reason):
    return reason.split(":", 1)[0] in ("agent_blocked", "agent_not_found", "agent_not_ready")


def prompt_error(name, text):
    # A trailing newline prevents the harness from completing a last word beginning with @ or $.
    reply = herdr("agent", "prompt", name, text if text.endswith("\n") else text + "\n", check=False)
    if reply.get("type") != "agent_prompted":
        return reply.get("error") or "unexpected reply from herdr"
    return None


def deliver(team, texts, what):
    """Send {member: text}. herdr refuses a prompt to an agent that is waiting on an approval, so say who
    did not get it. -> {member: reason}."""
    failed = {}
    for name, text in texts.items():
        error = prompt_error(name, text)
        if error:
            failed[name] = error
    ok = [n for n in texts if n not in failed]
    if ok:
        print(f"sent {what} to {', '.join('@' + n for n in ok)}")
    for name, err in failed.items():
        status = "NOT delivered" if delivery_refused(err) else "delivery unconfirmed"
        print(f"{status} to @{name}: {err}")
    return failed


def send_brief(cfg, team, operator, names=None, closing=None):
    """The team brief to running members (all, or only `names`). Closing is text or {member: text}.
    -> {member: reason} for failed deliveries."""
    members = team.members()
    targets = [m for m in members if names is None or m["name"] in names]
    if not targets:
        print("no members running")
        return {}
    return deliver(team, {m["name"]: message_for(cfg, m, members, operator,
                         closing.get(m["name"]) if isinstance(closing, dict) else closing) for m in targets},
                   "the team brief")


def report_failed(failed, team, undelivered=()):
    """Report failed launches by name. Brief delivery has its own per-recipient output."""
    for name, err in failed.items():
        if team.live(name):
            print(f"@{name} launch incomplete: {err}. Brief withheld.")
        else:
            print(f"@{name} did not come up: {err}. Fix the reported cause, then run: factory.py up {name}")
    if failed or undelivered:
        sys.exit(3)


# ================================================================ sessions seen (for up)
# herdr drops a pane's session reference when its agent exits. So every command that looks at the team notes
# each agent's current session in one small file in the cache, and `up` resumes a member's last one.
# The file is only ever appended to. Without it, a member that exited starts fresh.

def past_sessions(cfg):
    """{member: its latest noted session ref} from the per-repo cache."""
    path = cfg.cache / "sessions.jsonl"
    return {o["member"]: o for o in (records(path) if path.exists() else ()) if o.get("member") and o.get("value")}


def remember_sessions(cfg, panes):
    """Record observed sessions and return the latest saved ref for each agent."""
    last, new = past_sessions(cfg), []
    for name, pane in panes.items():
        ref = (pane or {}).get("agent_session") or {}
        entry = {"member": name, **{k: ref.get(k) for k in ("agent", "kind", "value")}}
        if entry["value"] and entry != last.get(name):
            last[name] = entry
            new.append(entry)
    if new:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        with (cfg.cache / "sessions.jsonl").open("a+b") as f:
            prefix = b""
            if f.tell():
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    prefix = b"\n"  # an interrupted append can leave a partial line
            data = "".join(json.dumps(o) + "\n" for o in new).encode()
            f.write(prefix + data)
    return last


# ================================================================ commands

def cfg_of(args, require=True):
    return Config(pathlib.Path(args.cwd or os.getcwd()), require)


def selected_names(cfg, names):
    """The named members, or every member in factory.toml."""
    names = [n.lstrip("@") for n in names] or list(cfg.members)
    strangers = [n for n in names if n not in cfg.members]
    if strangers:
        raise SystemExit(f"{', '.join(strangers)}: not a member in {CONF} (members: {', '.join(cfg.members)})."
                         f" To add a member, add its table to {CONF}.")
    return list(dict.fromkeys(names))


def selected(cfg, names):
    return [plan(cfg, n) for n in selected_names(cfg, names)]


def print_plan(plans, extra_col=None):
    rows = [("Member", "Role", "Harness", "Model", "Thinking", "Permissions") + ((extra_col[0],) if extra_col else ())]
    for p in plans:
        rows.append(("@" + p["name"], p["role"], p["harness"], p["model"] or "?", p["thinking"], p["permissions"])
                    + ((extra_col[1](p),) if extra_col else ()))
    print_table(rows)


def cmd_init(args):
    """Write what the project lacks: FACTORY.md and factory.toml.sample (shared), and this person's own factory.toml, copied
    from the project's sample. factory.toml goes in .gitignore. Nothing that exists is replaced."""
    root = find_root(pathlib.Path(args.cwd or os.getcwd()))

    def git(*a):  # -> exit status, or None without git
        try:
            return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
        except OSError:
            return None
    top = git("rev-parse", "--show-toplevel")
    if top and top.stdout.strip() and not any((root / n).exists() for n in (CONF, BRIEF)):
        root = pathlib.Path(top.stdout.strip())  # the repo root, not a subdirectory
    refuse_dangling_links(root)
    for name, source in ((BRIEF, SKILL / "templates" / BRIEF), (SAMPLE, SKILL / "templates" / CONF), (CONF, root / SAMPLE)):
        if (root / name).exists():
            print(f"kept  {root / name}")
            continue
        (root / name).write_text(source.read_text())
        print(f"wrote {root / name}")
        user = tomllib.loads(source.read_text()).get("user") if name == CONF else None
        if user:  # the sample is shared; whose branches these are is not
            print(f"note: {SAMPLE} sets user = {user!r}, so your worktree branches start with it. Confirm that, or"
                  f" change `user` in {CONF}.")
    ignore = root / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    rules = git("check-ignore", "-q", "--no-index", CONF)  # what the rules really do: 0 ignored, 1 not, else no repository
    if not (rules.returncode == 0 if rules and rules.returncode in (0, 1) else CONF in lines):  # a later `!factory.toml` undoes a line
        ignore.write_text("\n".join(lines + [CONF]) + "\n")
        print(f"added {CONF} to {ignore}: it is yours; {SAMPLE} is what the project shares")
    tracked = git("ls-files", "--error-unmatch", CONF)
    if tracked and tracked.returncode == 0:
        print(f"note: git already tracks {CONF}, and .gitignore does not change that. `git rm --cached {CONF}` stops it.")
    print(f"Edit {BRIEF} (project, workflow, where tasks are tracked) and {CONF} (your members), then run `factory.py check`.")


def cmd_harnesses(args):
    """What this machine can run: installed harnesses and which recommended models each can use."""
    cfg = cfg_of(args, require=False)
    checker = Checker(cfg)
    for h in HARNESSES:
        if not shutil.which(h):
            print(f"{h:9} not installed. {TABLE[h]['install']}")
            continue
        ok, missing, broken = [], [], None
        for model in TABLE[h]["models"]:
            try:
                ok.append(CODE[h].model(checker, model, h)[0])
            except Problem as e:
                if "has no model" in e.what or "does not offer" in e.what or "unknown Claude" in e.what:
                    missing.append(model)
                else:  # the harness itself could not answer; the same for every model
                    broken = e
                    break
        if broken:
            print(f"{h:9} installed, but could not list its models: {broken.what}\n{'':10}{broken.fix}")
            continue
        print(f"{h:9} available: {', '.join(ok) or 'none of the recommended models'}")
        if missing:
            print(f"{'':10}not configured: {', '.join(missing)}")
        if h == "codex":
            print(f"{'':10}Codex offers: {', '.join(sorted(checker.probe('codex', codex_models)))}")
        if h == "agy":
            print(f"{'':10}agy offers: {', '.join(sorted(checker.probe('agy-models', agy_models)))}")


def hc_check(environ=os.environ):
    """check's offline lines about hc (hc_client.md): its version, and draft_wait from its config. -> (lines, failed).
    A missing hc is reported and is not a failure; an hc too old or unreadable, or an unreadable config, is."""
    try:
        out = subprocess.run([HC, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError:
        return [f"hc: not found ({HC}); install hc {'.'.join(map(str, HC_MIN))} or newer before up"], False
    except (OSError, subprocess.TimeoutExpired) as error:
        return [f"hc: {HC} --version failed: {error}"], True
    first = (out.stdout.strip().splitlines() or [""])[0] if out.returncode == 0 else ""
    found = re.fullmatch(r"hc ([0-9]+(?:\.[0-9]+)+)(?:[-+ ].*)?", first)
    if not found:
        return [f"hc: unrecognised version from {HC} --version: {(first or out.stderr.strip())[:200]!r}"], True
    version = tuple(map(int, found[1].split(".")))
    if version < HC_MIN:
        return [f"hc {found[1]} is too old: Factory needs {'.'.join(map(str, HC_MIN))} or newer"], True
    config = pathlib.Path(environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config") / "herdr-core/config.toml"
    try:
        wait = tomllib.loads(config.read_text()).get("draft_wait", 60)
    except FileNotFoundError:
        wait = 60
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return [f"hc {found[1]}; cannot read {config}: {error}"], True
    if type(wait) is not int:
        return [f"hc {found[1]}; draft_wait in {config} must be a whole number of seconds, not {wait!r}"], True
    wait = max(wait, 0)
    lines = [f"hc {found[1]}, draft_wait {wait} s"]
    if wait < 600:
        lines.append(f"warning: raise draft_wait to 600 or more in {config} so a report never lands in text you"
                     " are typing")
    return lines, False


def cmd_check(args):
    cfg = cfg_of(args)
    plans = selected(cfg, args.names)
    ready, problems = check_all(cfg, plans)
    ok = {p["name"]: p for p in ready}
    print_plan([ok.get(p["name"], p) for p in plans], ("Check", lambda p: "ok" if p["name"] in ok else "FAILED"))
    lines, hc_failed = hc_check()
    for line in lines:
        print(line)
    if problems:
        report_problems(problems)
    if problems or hc_failed:
        sys.exit(2)


def cmd_up(args):
    cfg = cfg_of(args)
    plans = selected(cfg, args.names)
    if not plans:
        raise SystemExit(f"no members in {CONF}; add one, then run `factory.py up`")
    require_herdr()
    info = caller_agent()
    # The operator pane may still carry a member name or label from an earlier team.
    team = Team(cfg, exclude_pane=os.environ.get("HERDR_PANE_ID"))
    cold = not any(team.live(n) for n in cfg.members)
    if cold:
        report_leftover_drafts(cfg)
    running = [m["name"] for m in plans if not args.restart and team.live(m["name"])]
    ready = checked(cfg, [m for m in plans if m["name"] not in running])
    for m in ready:
        name = m["name"]
        if name in team.agent_panes and (not team.live(name) or team.agent_panes[name] != team.pane(name)["pane_id"]):
            print(f"an agent outside this team is already named {name}. One team runs per herdr session:"
                  " rename that agent, or use another session.", file=sys.stderr)
            sys.exit(2)
    warn_integrations(ready)
    if running:
        print(f"already running: {', '.join(running)}")
        report_launch_drift(team, [m for m in plans if m["name"] in running])
    operator = ensure_operator(cfg, tab=True, info=info)
    print_pending(args, operator)
    if not ready:
        return
    pickup_text = ("We are picking up where we left off. Check the tracker the brief above names, then send"
                   f" @{operator} a new one-line pickup report for this launch, even if you sent one before."
                   " Say what you were doing and what you will do next. Send it with the"
                   f" say command in your standing instructions. A reply in your own pane does not reach @{operator}.")
    ready_text = (f"Send @{operator} one line saying `ready` with the say command in your standing instructions,"
                  " then wait for your task.")
    fresh_text = "You are starting in a new session and know nothing of the work so far. " + ready_text
    past = past_sessions(cfg)
    closing, joined, failed = {}, [], {}
    for m in ready:
        name = m["name"]
        pane = team.pane(name)
        ref = (pane or {}).get("agent_session") or past.get(name)
        resume = ref if ref and ref.get("agent") == m["harness"] and not args.fresh else None
        was_live = team.live(name)
        if was_live:  # preflight has passed for every restart
            herdr("pane", "close", team.panes.pop(name)["pane_id"])
        err = launch(cfg, team, m, operator, args.timeout, resume=resume)
        if err:
            failed[name] = err + (f". If its session cannot be resumed, run: factory.py up {name} --fresh" if resume else "")
            continue
        if resume:
            closing[name] = pickup_text
        elif pane or ref:
            closing[name] = fresh_text
        else:
            closing[name] = ready_text
        if not was_live and not ref:  # also announce a successful retry of a first launch
            joined.append(m)
    team, undelivered = Team(cfg), {}
    if closing and args.no_brief:
        print("next: factory.py brief --send")
    elif closing:
        undelivered = send_brief(cfg, team, operator, names=closing, closing=closing)
        if joined:
            news = "; ".join(f"@{m['name']} ({m['role']}, {TABLE[m['harness']]['label']} {m['model']})" for m in joined)
            others = [m["name"] for m in team.members() if m["name"] not in closing and m["name"] not in failed]
            if others:
                deliver(team, {n: f"{tag(operator)} Joined the team: {news}. No reply needed." for n in others}, "the news")
    report_failed(failed, team, undelivered)


def member_pane_before_close(name, pane_id):
    """Return (pane, error). A missing pane or an exited agent is already stopped."""
    # Herdr has no conditional close, so an occupant change between this check and the close goes undetected.
    got = herdr("pane", "get", pane_id, check=False)
    if error := got.get("error"):
        return None, None if error.split(":", 1)[0] == "pane_not_found" else error
    pane = got.get("pane") or {}
    if pane.get("pane_id") != pane_id:
        return None, f"could not verify pane {pane_id}"
    if not pane.get("agent"):
        return None, None
    got = herdr("agent", "get", pane_id, check=False)
    if error := got.get("error"):
        return None, None if error.split(":", 1)[0] == "agent_not_found" else error
    agent = got.get("agent") or {}
    if pane.get("label") != name or agent.get("name") != name or agent.get("pane_id") != pane_id:
        return None, f"member identity changed in pane {pane_id}"
    return pane, None


def cmd_down(args):
    cfg = cfg_of(args)
    names = selected_names(cfg, args.names)
    require_herdr()
    caller = os.environ.get("HERDR_PANE_ID")
    if not caller:
        raise SystemExit("HERDR_PANE_ID is not set; run down from the operator's herdr pane.")
    got = herdr("agent", "get", caller, check=False)
    if (error := got.get("error")) and error.split(":", 1)[0] != "agent_not_found":
        raise SystemExit(f"herdr agent get {caller}: {error}")
    if (got.get("agent") or {}).get("name") in cfg.members:
        raise SystemExit("run down from the operator's pane, not a member pane.")

    team = Team(cfg, exclude_pane=caller, record_sessions=False)
    running = {name for name in cfg.members if team.live(name) or name in team.agent_panes}
    targets = {name: team.pane(name) for name in names
               if team.live(name) and team.pane(name)["pane_id"] == team.agent_panes.get(name)}
    try:
        saved = remember_sessions(cfg, targets) if targets else {}
    except OSError as e:
        raise SystemExit(f"could not record member sessions: {e}") from None

    stopped, failed = False, False
    integrations = None
    for name in names:
        if name not in running:
            print(f"@{name} was not running")
            continue
        target = targets.get(name)
        pane = None
        if target is None:
            error = "pane label and agent name do not point to the same pane"
        else:
            pane, error = member_pane_before_close(name, target["pane_id"])
        if pane:
            try:
                if pane.get("agent_session") and pane["agent_session"] != target.get("agent_session"):
                    saved = remember_sessions(cfg, {name: pane})
            except OSError as e:
                error = f"could not record member session: {e}"
            else:
                error = herdr("pane", "close", pane["pane_id"], check=False).get("error")
        if error:
            print(f"could not stop @{name}: {error}", file=sys.stderr)
            failed = True
            continue
        running.remove(name)
        if not pane:
            print(f"@{name} was not running")
            continue
        print(f"stopped @{name} (was {pane.get('agent_status', '?')})")
        harness, ref = cfg.members[name]["harness"], saved.get(name, {})
        if ref.get("agent") != harness:
            hint = ""
            if not ref and pane.get("agent") == harness:
                if integrations is None:
                    integrations, _ = integration_statuses()
                if integrations.get(integration_target(harness), "").startswith("needs repair "):
                    hint = ". Its herdr integration needs repair and may have prevented session recording."
            print(f"no compatible session recorded for @{name}: up will start it fresh{hint}")
        stopped = True
    if stopped:
        if not running:
            report_leftover_drafts(cfg)
        print("run factory.py up to bring members back")
    if failed:
        sys.exit(1)


def harness_argv(info, harness):
    """The argv of the one foreground process that is this harness, else None: no data or ambiguous data."""
    procs = [p.get("argv") for p in (info.get("process_info") or {}).get("foreground_processes") or []
             if p.get("argv") and harness in (re.sub(r"^\.|-wrapped$", "", p.get("name") or ""),
                                              os.path.basename(p["argv"][0]))]
    return procs[0] if len(procs) == 1 else None


def report_launch_drift(team, plans):
    """herdr can restore a member itself, e.g. after its server restarts, without the flags factory launched it
    with. Name the live members whose harness demonstrably lacks their permission flags or extra args."""
    drifted = []
    for m in plans:
        wanted = [w for w in (TABLE[m["harness"]]["permissions"][m["permissions"]], m["args"]) if w]
        if not wanted:
            continue
        argv = harness_argv(herdr("pane", "process-info", "--pane", team.pane(m["name"])["pane_id"], check=False),
                            m["harness"])
        if argv and not all(any(argv[i:i + len(w)] == w for i in range(len(argv) - len(w) + 1)) for w in wanted):
            drifted.append(m["name"])
    if drifted:
        print(f"running without their factory launch flags, as after a herdr restart: {', '.join(drifted)}."
              f" Run factory.py up {' '.join(drifted)} --restart")


def cmd_status(args):
    require_herdr()
    cfg = cfg_of(args)
    team = Team(cfg)
    rows = [("Member", "Role", "Harness", "Model", "Thinking", "Status", "Pane")]
    operator = cfg.operator["name"]
    rows.append(("@" + operator, "operator", cfg.operator.get("harness", ""), cfg.operator.get("model", ""), cfg.operator.get("thinking", ""),
                 team.status(operator), (team.pane(operator) or {}).get("pane_id", "")))
    for name in cfg.members:
        p = plan(cfg, name)
        pane = team.pane(name)
        rows.append(("@" + name, p["role"], p["harness"], p["model"] or "?", p["thinking"], team.status(name),
                     (pane or {}).get("pane_id", "")))
    print_table(rows)
    print_pending(args, operator)
    for state, hint in (("blocked", "blocked: waiting on an approval or a question."),
                        ("gone", "gone: run `factory.py up {names}` to bring them back.")):
        hit = [r[0] for r in rows[2:] if state in r[5]]
        if hit:
            print(f"\n{', '.join(hit)} " + hint.format(names=" ".join(n.lstrip("@") for n in hit)))
    if not any(team.pane(n) for n in cfg.members):
        print(f"\nNo member of {CONF} has a pane in this herdr session. Run `factory.py up` to start or resume its members.")


def cmd_brief(args):
    cfg = cfg_of(args)
    names = [n.lstrip("@") for n in args.names]
    if args.send:
        require_herdr()
        operator = ensure_operator(cfg)
        team = Team(cfg)
        if names == ["all"]:
            names = []
        missing = [n for n in names if n not in cfg.members or not team.live(n)]
        if missing:
            raise SystemExit(f"not running: {', '.join(missing)}")
        if send_brief(cfg, team, operator, names=set(names) or None):
            sys.exit(1)
        return
    print(render_team_brief(cfg, [plan(cfg, n) for n in cfg.members], cfg.operator["name"]))


def cmd_prompt(args):
    cfg = cfg_of(args)
    p = selected(cfg, [args.name])[0]
    print(f"# @{p['name']} ({p['role']}) on {p['harness']} {p['model']}, thinking {p['thinking']}\n")
    print(member_prompt(p, cfg.operator["name"], cfg))


# ================================================================ output helpers

def print_table(rows):
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for i, r in enumerate(rows):
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths).rstrip())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "continue":
        print("use factory.py up [names] [--fresh] [--restart]", file=sys.stderr)
        sys.exit(2)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--cwd", help="where to look for FACTORY.md and factory.toml (default: here, then parent directories)")

    def launching(sp):
        sp.add_argument("--timeout", type=int, default=60000, help="ms to wait for each agent to be ready")
        common(sp)

    def selection(sp):
        sp.add_argument("names", nargs="*", help="member names (default: every member in factory.toml)")

    it = sub.add_parser("init", help="write the FACTORY.md, factory.toml.sample and factory.toml a project lacks")
    common(it)

    common(sub.add_parser("harnesses", help="installed harnesses and which recommended models they can use"))

    ck = sub.add_parser("check", help="verify members can launch; launches nothing")
    selection(ck)
    common(ck)

    up = sub.add_parser("up", help="check and start or resume members, then send their briefs",
                        epilog="Native harness flags go in the member's `args` in factory.toml.")
    selection(up)
    up.add_argument("--no-brief", action="store_true", help="do not send the team brief afterwards")
    up.add_argument("--fresh", action="store_true", help="start new sessions instead of resuming")
    up.add_argument("--restart", action="store_true", help="also relaunch running members with current settings")
    launching(up)

    down = sub.add_parser("down", help="stop members and save their sessions for up")
    selection(down)
    common(down)

    st = sub.add_parser("status", help="members and whether they are running")
    common(st)

    ib = sub.add_parser("inbox", help="print recent committed messages for a recipient")
    ib.add_argument("count", nargs="?", type=int, default=10, help="last N messages (default 10)")
    ib.add_argument("--for", dest="for_name", help="recipient name (default: this member or the operator)")
    common(ib)

    bf = sub.add_parser("brief", help="print the team brief as members get it, or --send it")
    bf.add_argument("names", nargs="*", help="with --send: only these members")
    bf.add_argument("--send", action="store_true")
    common(bf)

    sy = sub.add_parser("say", help="message teammates from your member or operator pane")
    sy.add_argument("to", nargs="+", help="recipient names separated by spaces or commas, or all for members only; "
                    "the last argument is the message unless --file")
    sy.add_argument("--file", help="send this file's contents")

    pr = sub.add_parser("prompt", help="the standing instructions a member gets")
    pr.add_argument("name")
    common(pr)

    keeper = sub.add_parser("keeper", help="run the team's delivery process")
    keeper.add_argument("--state", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "say":
        args.message = args.to.pop() if args.file is None and len(args.to) > 1 else None
    {"init": cmd_init, "harnesses": cmd_harnesses, "check": cmd_check, "up": cmd_up, "down": cmd_down,
     "status": cmd_status, "inbox": cmd_inbox, "brief": cmd_brief, "say": cmd_say,
     "prompt": cmd_prompt, "keeper": cmd_keeper}[args.cmd](args)


if __name__ == "__main__":
    main()
