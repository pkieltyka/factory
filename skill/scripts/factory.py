#!/usr/bin/env python3
"""factory: a software team of coding agents in herdr-core (hc). You are the operator (CTO); each member runs in its
own hc terminal, on its own harness, model and thinking level, and a keeper types the team's messages.

A project has two files at its root. FACTORY.md is the brief every member reads: the project and how the team
works; it is committed and shared. factory.toml is one person's formation: the operator, the members and their
harnesses and models; it is not committed (factory.toml.sample is, as the project's suggestion). hc holds the
rest: each team terminal carries labels naming its team, kind and member. Harnesses are described in
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
import string
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid

SCRIPT = pathlib.Path(__file__).resolve()


TEAM_COMMANDS = ("up", "down", "status", "inbox", "brief", "check")   # they act on one checkout's team
TEAM_ROOT = None    # the checkout this process found its team in (Config), for the commands it prints


def factory_command(*args):
    """A Factory command an agent can run as printed: python3 and this script's absolute path, shell-quoted (#173 pi
    case 8: a bare factory.py from a keeper note was not found, and with factory and factory2 installed side by side
    it can name another copy). A command that acts on a team adds --cwd and the team's checkout once this process
    knows it, so it reaches that team from any directory (@reviewer on #234). Prose keeps the name factory.py."""
    line = shlex.join(["python3", str(SCRIPT), *map(str, args)])
    if not args or args[0] not in TEAM_COMMANDS:
        return line
    if TEAM_ROOT is None:     # no checkout verified yet: a placeholder, never the caller's directory (@lead-dev)
        return line + " --cwd <team checkout>"
    return line + " " + shlex.join(["--cwd", str(TEAM_ROOT)])
SKILL = SCRIPT.parent.parent
TABLE = tomllib.loads((SKILL / "harnesses.toml").read_text())  # harness -> its data
HARNESSES = tuple(TABLE)
LEVELS = ("full", "guarded")
PROBE_TIMEOUT = 60
BRIEF = "FACTORY.md"              # the brief: shared and committed
CONF = "factory.toml"             # one person's formation: not committed
SAMPLE = "factory.toml.sample"    # the project's suggested formation: committed, copied to factory.toml by `init`
OPERATOR = "operator"  # the operator's @name, unless `[operator] name` in factory.toml says otherwise
HC = os.environ.get("HC_BIN", "hc")
HC_MIN = (0, 1, 20)
VERSION = hashlib.sha1(SCRIPT.read_bytes()).hexdigest()[:12]
TICK = 1.0
UNSTARTED_GRACE = 5     # seconds after an unstarted send before the member's idle turn is checked (keeper.md §5)
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

    def send(self, term: str, text: str, cond: str | None = None) -> dict:
        """terminal.send; cond="idle" has hc type only into an idle terminal (status_mismatch, nothing typed)."""
        params = {"terminal": term, "text": text}
        if cond:
            params["if"] = cond
        return self.call("terminal.send", params, timeout=None)

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

    def member_terminals(self) -> list[dict]:
        return [record for record in self._live.values() if record["labels"].get("factory.kind") == "member"]

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
        global TEAM_ROOT
        self.root = TEAM_ROOT = find_root(cwd)
        self.brief_file, self.conf_file = self.root / BRIEF, self.root / CONF
        self.roles = {p.stem: parse_role(p) for p in sorted((SKILL / "roles").glob("*.md"))}
        missing = [f.name for f in (self.brief_file, self.conf_file) if not f.is_file()]
        if missing and require:
            refuse_dangling_links(self.root)
            raise SystemExit(f"no {' or '.join(missing)} in {self.root} or above. Run `/factory2 setup`, or"
                             f" `{factory_command('init')}`, which writes what is missing and copies the"
                             f" project's {SAMPLE} to"
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
        if self.operator["name"] == "factory":
            raise SystemExit(f"{self.conf_file}: [operator] name 'factory' is reserved for Factory's own notes")
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


def plan(cfg, name, state=None):
    """A member's settings from factory.toml, before availability checks. state: the team's state directory, when
    known; without it the paths are None and previews show placeholders."""
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
            "state_dir": state, "message_dir": pathlib.Path(state) / "messages" / name if state else None,
            "instructions": pathlib.Path(state) / f"{name}.md" if state else None}


# ================================================================ availability checks

def run_probe(argv, env=None, hide=()):
    """argv's output, with env added to this process's environment and the names in hide left out."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                           env={k: v for k, v in {**os.environ, **(env or {})}.items() if k not in hide})
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
    raise Problem(f"{harness} has no model {base} configured",
                  fix + f" `{factory_command('harnesses')}` lists what it can use.")




# ================================================================ message log (v2; spec §8.5-8.7, message_log.md)

LOG_KINDS = ("say", "brief", "note")
OUTCOMES = ("sending", "delivered", "queued", "unconfirmed", "dropped")
FINAL = ("delivered", "unconfirmed", "dropped")
QUEUE_REASONS = ("waiting", "blocked", "not ready", "gone", "busy")   # busy: a between-turns recipient is working
READ_QUEUE_REASONS = QUEUE_REASONS
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
                         f" Repair or move the file, then run {factory_command('up')}.")
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
        self.deliveries = 0                 # delivered outcomes of messages that are not notes (a full box only)

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
        if new == "delivered" and self._only is None and msg.get("kind") != "note":
            self.deliveries += 1            # quiet's activity count (keeper.md §6): the same on every replay
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
        raise Problem(f"{state} is not a Factory state directory", f"run {factory_command('up')}")
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
        raise Problem(f"{state}/team.json cannot be read: {error}", f"run {factory_command('up')}") from error


def read_team(state):
    log = open_log(state)
    try:
        with log.locked(exclusive=False) as directory:
            return _read_team_file(log.state, directory)
    except (LogDamage, OSError) as error:
        raise Problem(f"{log.state}/team.json cannot be read: {error}", f"run {factory_command('up')}") from error


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
            raise Problem(f"no team.json in {log.state}", f"run {factory_command('up')}")
        log._refresh(box)
        claimed = box.sending_terms()
        keep = [term for term in team.get("stopped", []) if term in claimed]
        team["stopped"] = list(dict.fromkeys([*keep, *terms]))
        _write_team_file(directory, team)


def _unsettled_refusal(error, term):
    """hc's refusal to type into a fresh terminal whose output has not settled yet. hc settles before it types
    (0.1.20 input.rs) and says "nothing was typed", so the message is still unsent. Any other unavailable reply
    may follow a partial write and stays unconfirmed."""
    return error.kind == "unavailable" and error.message == f"{term} is still starting: its output has not settled"


def settle_refusal(log, box, ident, to, term, why):
    """Settle a member's definite pre-typing refusal against stop evidence under the same lock."""
    with log.locked(exclusive=True) as directory:
        team = _read_team_file(log.state, directory)
        if not team:
            raise Problem(f"no team.json in {log.state}", f"run {factory_command('up')}")
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
                            raise Problem(f"not a regular file: {state / filename}", f"run {factory_command('up')}")
                        candidates.append((priority, -info.st_mtime_ns, state))
                        break
                finally:
                    os.close(child)
        finally:
            os.close(directory)
    except OSError as error:
        raise Problem(f"cannot inspect {parent}: {error}", f"run {factory_command('up')}") from error
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
                raise Problem(f"{state} does not match this checkout and hc core", f"run {factory_command('up')}")
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
                raise Problem("this pane has invalid Factory labels", f"run {factory_command('up')}")
            state = open_log(label).state
            team = read_team(state)
            if not team:
                raise Problem(f"no team.json in {state}", f"run {factory_command('up')}")
            if (team["team"] != key or state.parent.name != key or state.name != team["core"][:8]
                    or not os.path.isabs(team["root"])):
                raise Problem(f"this pane's labels do not match {state}/team.json", f"run {factory_command('up')}")
            if team["core"] != hc.core:
                raise Problem("this pane's hc is another core than the team's", f"run {factory_command('up')}")
            hc.expect_core = team["core"]
            try:
                cfg = Config(team["root"])
            except SystemExit as error:
                raise Problem(str(error), f"run {factory_command('up')}") from error
            if team_key(cfg.root) != key:
                raise Problem(f"this pane's checkout does not match {state}/team.json", f"run {factory_command('up')}")
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
        lines.append(f"read them with: {factory_command('inbox', '--for', operator)}")
    if box.uncommitted:
        noun = "entry" if box.uncommitted == 1 else "entries"
        lines.append(f"log: {box.uncommitted} uncommitted {noun} (never delivered)")
    return lines



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


def harness_for_app(app):
    """The harness a live status.app names: its table key, or the key whose status_app it is (agy reports
    "antigravity", Claude Code "claude-code"); None for an app no harness claims."""
    if not app:
        return None
    if app in TABLE:
        return app
    return next((key for key, t in TABLE.items() if t.get("status_app") == app), None)


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
        return between_turns(status, cfg.operator.get("harness"), terminal["address"])

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
    return between_turns(terminal.get("status") or {}, (getattr(cfg, "members", {}).get(name) or {}).get("harness"),
                         terminal["address"])


def between_turns(status, formed, address):
    """keeper.md §9, in either role: a harness that may ignore text typed into a working turn (Codex, #173, as
    operator and as member) gets Factory's messages between turns. The live app names the harness, the formation's
    only when none is reported. -> the send or busy action."""
    harness = harness_for_app(status["app"]) if status.get("app") else formed
    if TABLE.get(harness or "", {}).get("deliver") == "idle":
        if status.get("state") == "working":
            return "queue", "busy"
        if status.get("state") == "idle":
            return "send", address, "idle"         # hc refuses, typing nothing, if it starts working
    return "send", address


def _age(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m" if hours else f"{minutes}m" if minutes else f"{seconds}s"


def _member_terminals(view, cfg):
    """{address: record} of the live terminals of configured members."""
    return {t["address"]: t for t in (view.terminal(n) for n in cfg.members) if t is not None}


def quiet_now(view, box, cfg, wall_now, monotonic_now, inflight, op_null_since):
    """keeper.md §6: whether the team is quiet now. Quiet and ready notes are left out of the heads they could hide."""
    operator = view.operator
    if operator is None or operator.get("status") is None or operator["status"]["state"] == "working" or inflight:
        return False
    for name in cfg.members:
        t = view.terminal(name)
        if t is not None and (t.get("status") or {}).get("state") == "working":
            return False
        if view.state(name) == "starting" and wall_now - t["created_ms"] / 1000 < LAUNCH_TIMEOUT:
            return False
    if any(rec["o"] == "sending" for rec in box.outcomes.values()):
        return False
    briefed = box.briefed()
    for name in dict.fromkeys(name for _, name in box.pending()):
        terminal = view.terminal(name)
        ready = name == cfg.operator["name"] or (terminal is not None and terminal["address"] in briefed)
        pending = [msg for msg, _ in box.pending(name) if not _quiet_or_ready_note(msg)]
        briefs = [msg for msg in pending if msg["kind"] == "brief"] if not ready else []
        head = briefs[-1] if briefs else pending[0] if pending else None
        if head is not None and decide(name, head, view, box, cfg, monotonic_now, op_null_since)[0] == "send":
            return False
    return True


def rearmed(q, view, box, cfg):
    """Whether real activity happened after quiet note q: a member working, a new or advanced member terminal, or a
    delivered message that is not a note."""
    turns = q["meta"].get("turns") or {}
    for address, t in _member_terminals(view, cfg).items():
        if ((t.get("status") or {}).get("state") == "working" or address not in turns
                or t.get("turn", 0) > turns[address]):
            return True
    return box.deliveries > q["meta"].get("deliveries", 0)


def quiet_text(view, box, cfg, wall_now, quiet_for):
    """keeper.md §6's note, from metadata only."""
    lines = [f"team quiet for {_age(quiet_for)}: nobody is working and nothing is waiting to be delivered."]
    for name in cfg.members:
        record = view.terminal(name) or view.finished(name)
        if record is None:
            continue
        state = view.state(name)
        since = record.get("status_ms") if state not in ("gone",) else None
        line = f"@{name} {state}" + (f" {_age(wall_now - since / 1000)}" if since else "") + "."
        got, sent = box.last_to(name), box.last_from(name)
        line += f" Last received #{got['id']} from @{got['from']}." if got else " Last received none."
        line += (f" Last sent #{sent['id']} to {', '.join('@' + r for r in sent['to'])}." if sent
                 else " Last sent none.")
        lines.append(line)
    lines.append("A member whose last received message is newer than anything it sent may owe a reply.")
    return "\n".join(lines)


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
        self.unstarted_seen = {}     # (member, id) -> when this keeper first saw it typed with no turn started
        self.op_null_since = None
        self._null_terminal = None
        self.damaged = None
        self.quiet_since = None              # monotonic start of the current quiet interval
        self.ready_next = None               # monotonic time of the next ready check, while told
        self.ready_job = None                # the running check's key, or None
        self._ready_done = None              # its result, applied after the box is refreshed
        self._cfg_failed = False             # the current factory.toml does not load: no ready check
        self.draining = None                 # the newer build on disk this keeper drains for, then execs
        self._version_at = 0                 # monotonic time of the next look at the installed build
        self._title = None
        self._told = False                   # the operator has a quiet note for this interval
        self._quiet_epoch = 0                # bumped by every tick that does not see the team quiet and eligible
        self._eligible = False
        self.title_out = sys.stdout          # OSC 2 goes straight to the pane, not through emit
        self._execv = os.execv
        self._quiet_sample = None            # member turns and the delivery count at the previous quiet tick
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
                    if self.draining and not self.stop and self._drained():
                        self._reexec()
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
                raise Problem(f"no team.json in {self.state}", f"run {factory_command('up')}")
            if team["team"] != self.state.parent.name or team["core"][:8] != self.state.name:
                raise Problem(f"{self.state}/team.json does not match its directory", f"run {factory_command('up')}")
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
            self._cfg_failed = False
        except (SystemExit, OSError) as error:
            self._cfg_failed = True          # the old Config still delivers; it does not run the check
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

    def _check_version(self, now):
        """Every 30 s: a newer build on disk starts the drain (phase_6.md §5). A read failure changes nothing."""
        if now < self._version_at or self.draining:
            return
        self._version_at = now + 30
        try:
            installed = hashlib.sha1(SCRIPT.read_bytes()).hexdigest()[:12]
        except OSError:
            return
        if installed != VERSION:
            self.draining = installed
            self.emit(f"keeper {installed} installed: finishing what is in flight, then restarting")

    def _drained(self):
        """Nothing in flight, no ready check running and no withheld result: safe to exec."""
        return not self.inflight and self.ready_job is None and not self.unsettled

    def _reexec(self):
        argv = [sys.executable, str(SCRIPT), "keeper", "--state", str(self.state)]
        self.emit("keeper restarting: new Factory version")
        try:
            self._execv(sys.executable, argv)   # the lock fd is close-on-exec: the new image takes it again
        except OSError as error:
            self.emit(f"keeper could not restart itself ({error}); it goes on serving")
            self.draining = None
            self._version_at = self.clock.monotonic() + 30

    def _show_title(self):
        """OSC 2, only when it changes: pending pairs, else a reported quiet interval, else keeper."""
        try:
            pending = len(self.box.pending())
        except LogDamage:
            pending = 0
        if pending:
            title = f"keeper · {pending} pending"
        elif self._told and self.quiet_since is not None:
            title = f"keeper · quiet {int((self.clock.monotonic() - self.quiet_since) // 60)}m"
        else:
            title = "keeper"
        if title != self._title:
            self._title = title
            self.title_out.write(f"\x1b]2;{title}\x07")
            self.title_out.flush()

    def tick(self):
        self._eligible = False
        try:
            self._tick()
        finally:
            if not self._eligible:           # any interruption ends the interval a ready check belongs to
                self._quiet_epoch += 1

    def _tick(self):
        if self._beat_fd is not None:
            os.utime(self._beat_fd)
        self._check_version(self.clock.monotonic())
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
            if self.damaged is None and self.team.get("up") is True and not self.draining:
                self._member_notes(view)
                self._unstarted_notes(view)
                self._quiet(view, now)
            if self.damaged is None and not self.draining:   # draining: no new claims, only settlements
                self._deliver(view, now)
            operator = self.cfg.operator["name"]
            if (self.team.get("up") is not True and not self.inflight
                    and (not view.operator or view.operator.get("status") is None or not self.box.pending(operator))):
                self.stop = "team is down"
        except (LogDamage, NotStored, OSError) as error:
            self._log_failed(error)
        self._show_title()

    def _member_notes(self, view):
        """keeper.md §5: housekeeping on briefed member terminals, then one note per new episode for the operator."""
        operator = self.cfg.operator["name"]
        reachable = view.operator is not None and view.operator.get("status") is not None
        briefed = self.box.briefed()
        for name in self.cfg.members:
            live = view.terminal(name)
            record = live or view.finished(name)
            if record is None or record["labels"].get("factory.down") or record["address"] not in briefed:
                continue
            address, status = record["address"], (live or {}).get("status")
            try:
                if live and live.get("keep"):
                    self.hc.keep_off(address)
                if status and status["state"] != record["labels"].get("factory.last"):
                    self.hc.label(address, {"factory.last": status["state"]})
            except (HcError, HcDown) as error:
                self._report(f"member {address}", f"@{name} ({address}): {error}")
            if not reachable:
                continue
            note = self._episode(name, record, status, view)
            if note is None:
                continue
            meta, text = note
            last = self.box.last_note(address)
            if last is not None and last.get("what") == meta["what"] and last.get("since") == meta.get("since"):
                continue                     # the same episode: noted already, maybe before a restart
            ident = self.log.add("factory", [operator], text, kind="note", meta=meta)[0]
            self.log.refresh(self.box)
            stamp = time.strftime("%H:%M:%S", time.localtime(self.clock.time()))
            self.emit(f"{stamp} #{ident} note: @{name} {meta['what']}")

    def _quiet(self, view, now):
        """keeper.md §6 steps 1-3: count a quiet interval, and add one quiet note when its grace has passed."""
        wall = self.clock.time()
        q = self.box.quiet_note()
        told = q is not None and not rearmed(q, view, self.box, self.cfg)
        self._told = told
        if self._cfg_failed or not quiet_now(view, self.box, self.cfg, wall, now, self.inflight, self.op_null_since):
            self.quiet_since = self._quiet_sample = self.ready_next = self._ready_done = None
            self._told = False
            return
        sample = ({a: t.get("turn", 0) for a, t in _member_terminals(view, self.cfg).items()}, self.box.deliveries)
        if self.quiet_since is None or sample != self._quiet_sample:
            self.quiet_since = now           # activity between two quiet samples starts the grace again
        self._quiet_sample = sample
        operator = self.cfg.operator["name"]
        if view.operator["status"]["state"] == "blocked":
            return
        self._eligible = True                # quiet, reachable and unblocked: the interval goes on
        if told:
            self._ready(q, view, now)
            return
        self.ready_next = None
        if now - self.quiet_since < self.cfg.quiet_grace:
            return
        meta = {"what": "quiet", "turns": sample[0], "deliveries": sample[1]}
        text = quiet_text(view, self.box, self.cfg, wall, now - self.quiet_since)
        ident = self.log.add("factory", [operator], text, kind="note", meta=meta)[0]
        self.log.refresh(self.box)
        stamp = time.strftime("%H:%M:%S", time.localtime(wall))
        self.emit(f"{stamp} #{ident} note: team quiet for {_age(now - self.quiet_since)}")

    def _ready_key(self, q):
        """A check belongs to its quiet note, the config and command it ran with, and the eligibility interval."""
        return q["id"], self._cfg_stamp, self.cfg.ready_check, self._quiet_epoch

    def _ready(self, q, view, now):
        """keeper.md §6 step 4 and phase_6.md §4: the owner's ready check, once a period while told."""
        done, self._ready_done = self._ready_done, None
        if done is not None and done[1] == self._ready_key(q) and not self._cfg_failed:
            self._ready_result(q, *done[2])     # stale results (another interval, command or config) are dropped
        if (not self.cfg.ready_check or self._cfg_failed or self.ready_job is not None
                or self.box.outcome(q["id"], self.cfg.operator["name"])["o"] not in ("delivered", "unconfirmed")):
            return
        if self.ready_next is None:
            self.ready_next = now + self.cfg.ready_every   # the first check waits a full period
            return
        if now < self.ready_next:
            return
        self.ready_next = now + self.cfg.ready_every
        key, command, root = self._ready_key(q), self.cfg.ready_check, self.team["root"]
        self.ready_job = key

        def job():
            try:
                done = subprocess.run(["sh", "-c", command], cwd=root, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                      timeout=READY_TIMEOUT, check=False)
                result = done.returncode, done.stdout
            except subprocess.TimeoutExpired:
                result = "timed out", ""
            except (OSError, UnicodeDecodeError, ValueError) as error:
                result = f"failed ({type(error).__name__})", ""
            self.results.put(("ready", key, result))
        try:
            self.spawn(job)
        except RuntimeError:
            self.ready_job = None

    def _ready_result(self, q, code, output):
        """Apply a current check's result (keeper.md §6's table)."""
        work = output.strip() if isinstance(output, str) else ""
        if code not in (0, 1):
            self.emit(f"ready check: {'exit ' + str(code) if isinstance(code, int) else code}")
            return
        report = self.box.ready_note()
        clear = self.team.get("ready_clear", 0)
        last = report["meta"].get("digest") if report and report["id"] > clear else None
        if code == 1 or not work:
            if last is not None:                 # only when a report stands: no redundant writes
                self.team = update_team(self.state, ready_clear=self.box.highest)
            return
        digest = hashlib.sha1(work.encode()).hexdigest()
        if digest == last:
            return
        text = "ready work:\n" + "\n".join(work.splitlines()[:20])
        meta = {"what": "ready", "digest": digest, "quiet_id": q["id"]}
        ident = self.log.add("factory", [self.cfg.operator["name"]], text[:2000], kind="note", meta=meta)[0]
        self.log.refresh(self.box)
        stamp = time.strftime("%H:%M:%S", time.localtime(self.clock.time()))
        self.emit(f"{stamp} #{ident} note: ready work")

    def _still_true(self, msg, view, now):
        """For a quiet or ready note about to be handed off: (still true, the fresh view to route it by), from
        team.json, the team view and the log read again now, not from what this tick saw earlier."""
        try:
            team = read_team(self.state)
            view = team_view(self.hc, self.team["team"])
            self.log.refresh(self.box)
        except (Problem, HcDown, HcError, LogDamage, OSError):
            return False, None
        return self._true_in(msg, view, team, now), view

    def _true_in(self, msg, view, team, now):
        operator = view.operator
        if (team.get("up") is not True or operator is None or operator.get("status") is None
                or operator["status"]["state"] == "blocked"):
            return False
        if not quiet_now(view, self.box, self.cfg, self.clock.time(), now, self.inflight, self.op_null_since):
            return False
        q = self.box.quiet_note()
        if q is None or rearmed(q, view, self.box, self.cfg):
            return False
        what = msg["meta"]["what"]
        return msg["id"] == q["id"] if what == "quiet" else msg["meta"].get("quiet_id") == q["id"]

    def _episode(self, name, record, status, view):
        """(meta, text) of the note the member's current state calls for, or None."""
        address = record["address"]
        if view.state(name) == "gone":
            cause, word = end_cause(record)
            text = f"@{name} {cause} " + (f"while {word}." if word else "(state unknown).")
            last = self.box.last_to(name, sender=self.cfg.operator["name"])
            if last:
                text += f" Its last message from you was #{last['id']}."
            return {"what": "exit", "about": name, "term": address}, text + f" Run: {factory_command('up', name)}"
        if status is None or status["state"] not in ("blocked", "error"):
            return None
        detail = f": {status['msg']}" if status.get("msg") else ""
        meta = {"about": name, "term": address, "since": record.get("status_ms")}
        if status["state"] == "blocked":
            kind = status.get("kind") or ""
            text = f"@{name} is blocked" + (f" ({kind})" if kind else "") + detail + ". It needs an answer in its pane."
            return {"what": "blocked:" + kind, **meta}, text
        return {"what": "error", **meta}, f"@{name} hit an error{detail}."

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
            route = view
            if _quiet_or_ready_note(msg):            # judged and routed by the same fresh view (the operator may move)
                still, fresh = self._still_true(msg, view, now)
                route = fresh or view
            else:
                still = True
            action, *detail = decide(name, msg, route, self.box, self.cfg, now, self.op_null_since, still)
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
                self._claim(msg, name, detail[0], detail[1] if len(detail) > 1 else None)
                continue
            why = detail[0]
            outcome = "queued" if action == "queue" else "dropped"
            if why in ("gone", "not ready", "stopped by down"):
                for pending, _ in self.box.pending(name):
                    if self.box.outcome(pending["id"], name)["o"] == "queued":
                        if _quiet_or_ready_note(pending):   # never queued behind another head
                            self._record(pending, name, "dropped", why="no longer true")
                        else:
                            self._record(pending, name, outcome, why=why)
            else:
                self._record(msg, name, outcome, why=why)
            if outcome == "dropped" and _quiet_or_ready_note(msg) and msg["meta"]["what"] == "quiet":
                self.quiet_since = None              # a dropped quiet note: a full grace before the next

    def _claim(self, msg, name, term, cond=None):
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
            self.spawn(functools.partial(self._send, name, ident, term, wire, cond))
        except RuntimeError as error:
            self.results.put((name, ident, term, HcDown(False, str(error))))

    def _send(self, name, ident, term, wire, cond=None):
        try:
            if self.stop:
                raise HcDown(False, "keeper stopped before sending")
            result = self.hc.send(term, wire, cond) if cond else self.hc.send(term, wire)
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
        for item in pending:
            if len(item) == 3 and item[0] == "ready":   # (tag, job key, result): never a four-field send
                self.ready_job, self._ready_done = None, item
                continue
            name, ident, term, result = item
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

    def _unstarted_notes(self, view):
        """keeper.md §5 (#175): a member's newest delivery that hc typed without a turn starting, on a harness that
        can hold a typed message unsent (omp with a large one), gets one note to the operator naming the message
        and the pane, once the member is seen still idle at that turn: a person checks the input before pressing
        Enter (@operator). Derived from the log on every pass, so a failed write is retried and a restart or drain
        loses nothing; a stored note for that message and terminal ends it (@architect-2)."""
        now = self.clock.monotonic()
        operator = self.cfg.operator["name"]
        cases = []           # every unnoted receipt, not only a member's newest (@architect-2: #6 and #7 at one turn)
        for (ident, to), rec in sorted(self.box.outcomes.items()):
            msg = self.box.msgs.get(ident)
            if to != operator and rec.get("o") == "delivered" and msg and msg["kind"] != "note":
                cases.append((to, ident, rec))
        noted = {(m["meta"].get("id"), m["meta"].get("term")) for m in self.box.msgs.values()
                 if m["kind"] == "note" and (m.get("meta") or {}).get("what") == "unstarted"}
        live = set()
        for name, ident, rec in cases:
            term, turn = rec.get("term"), rec.get("turn")
            if rec.get("started") is not False or (ident, term) in noted:
                continue
            live.add((name, ident))
            since = self.unstarted_seen.setdefault((name, ident), now)
            if now - since < UNSTARTED_GRACE:
                continue
            record = view.terminal(name)
            status = (record or {}).get("status") or {}
            if not record or record["address"] != term or status.get("state") not in ("idle", "done") \
                    or record.get("turn") != turn:
                continue                     # it started after all, or the pane changed
            harness = harness_for_app(status["app"]) if status.get("app") else \
                (self.cfg.members.get(name) or {}).get("harness")       # the live app first (@architect-2)
            if TABLE.get(harness or "", {}).get("unstarted") != "note":
                continue
            label = TABLE[harness].get("label", harness)
            text = (f"@{name} did not start a turn: message #{ident} was typed into {term}, and {label} can hold a"
                    f" large message unsent in its input. Ask a person to inspect the input in {term} and press Enter"
                    f" only if they see message #{ident} waiting.")
            meta = {"what": "unstarted", "about": name, "term": term, "id": ident}
            note = self.log.add("factory", [operator], text, kind="note", meta=meta)[0]
            self.log.refresh(self.box)
            live.discard((name, ident))
            stamp = time.strftime("%H:%M:%S", time.localtime(self.clock.time()))
            self.emit(f"{stamp} #{note} note: @{name} unstarted #{ident}")
        for key in set(self.unstarted_seen) - live:
            del self.unstarted_seen[key]

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
            why = {"blocked": "blocked", "not_found": "gone", "finished": "gone",
                   "status_mismatch": "busy"}.get(result.kind)   # the idle guard refused: nothing was typed
            if why is None and _unsettled_refusal(result, term):
                why = "waiting"                      # nothing was typed: send it again once the output settles
        if why is None:
            detail = " ".join(str(result).split())[:200] or type(result).__name__
            self._record(msg, name, "unconfirmed", why=detail, term=term)
        elif _quiet_or_ready_note(msg):
            self._record(msg, name, "dropped", why="operator could not take it", term=term)
            if msg["meta"]["what"] == "quiet":
                self.quiet_since = None              # no retry loop on a refusal: a full grace again
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
                raise Problem("another command is starting the keeper", f"run {factory_command('status')}") from None
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
                raise Problem("the keeper did not start: terminal disappeared",
                              f"run {factory_command('status')}") from error
            if finished:
                lines = [line.strip() for line in hc.read(terminal).splitlines() if line.strip()]
                hc.rm(terminal)
                raise Problem(f"the keeper did not start: {lines[-1] if lines else 'terminal exited'}",
                              f"run {factory_command('status')}")
            next_poll = now + 1
        if now - begin >= KEEPER_START:
            return terminal
        time.sleep(0.1)


def ensure_keeper(cfg, state, hc, view, force=False):
    """Return this team's keeper terminal, starting it headless if it is missing or stale."""
    team = read_team(state)
    if not team:
        raise Problem(f"no team.json in {state}", f"run {factory_command('up')}")
    log = open_log(state)
    if not force and team.get("up") is not True and not log.read_box()[1].pending(cfg.operator["name"]):
        return None
    directory = log.open_dir(create=False)
    if directory is None:
        raise Problem(f"no state directory: {state}", f"run {factory_command('up')}")
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


# ================================================================ layout (launch_layout.md §5)

def new_id(prefix):
    return prefix + uuid.uuid4().hex


def _layout_tabs(doc):
    spaces = doc["spaces"]
    if not isinstance(spaces, list):
        raise TypeError("layout spaces must be a list")
    for space in spaces:
        if not isinstance(space["id"], str) or not isinstance(space["tabs"], list):
            raise TypeError("invalid layout space")
        for tab in space["tabs"]:
            if not isinstance(tab["id"], str) or tab["root"]["type"] == "block":
                raise TypeError("invalid layout tab")
            yield space, tab


def _pane_blocks(node):
    if not isinstance(node["id"], str) or not isinstance(node["type"], str):
        raise TypeError("invalid layout node")
    if node["type"] == "block":
        content = node["content"]
        if not isinstance(content, dict) or ("terminal" in content and not isinstance(content["terminal"], str)):
            raise TypeError("invalid pane content")
        yield node
        return
    children = node["children"]
    if not isinstance(children, list):
        raise TypeError("layout children must be a list")
    for child in children:
        yield from _pane_blocks(child)


def blocks(doc):
    """Map terminal IDs to their blocks anywhere in the layout."""
    return {terminal: block for _, tab in _layout_tabs(doc) for block in _pane_blocks(tab["root"])
            if (terminal := block["content"].get("terminal"))}


def where(doc, terminal):
    """The actual space and tab containing terminal, or None."""
    if terminal is None:
        return None
    for space, tab in _layout_tabs(doc):
        if any(block["content"].get("terminal") == terminal for block in _pane_blocks(tab["root"])):
            return space["id"], tab["id"]
    return None


def _tab_terminals(tab):
    return {term for block in _pane_blocks(tab["root"]) if (term := block["content"].get("terminal"))}


def resolve_tab(doc, layout, view, me):
    """Find the existing team tab before proposing a new one in the operator's space."""
    me_labels = me["labels"]
    operator_tab = me_labels.get("herdr.tab")
    members = view.member_terminals()
    if view.keeper:
        members.append(view.keeper)
    chosen, most = None, 0
    for space, tab in _layout_tabs(doc):
        ident = tab["id"]
        if ident == layout.get("tab"):
            chosen = space, tab
            break
        present = _tab_terminals(tab)
        if ident == operator_tab or me["address"] in present:
            continue
        count = sum(record["address"] in present or record["labels"].get("herdr.tab") == ident for record in members)
        if count > most:
            chosen, most = (space, tab), count
    if chosen:
        space, tab = chosen
        made = space["id"] == layout.get("space") and layout.get("made_space", False)
        return space["id"], tab["id"], made
    spaces = {space["id"]: space for space in doc["spaces"]}
    for ident in (me_labels.get("herdr.space"), layout.get("space")):
        if ident in spaces:
            return ident, new_id("tb"), ident == layout.get("space") and layout.get("made_space", False)
    return new_id("sp"), new_id("tb"), True


def add_panes(doc, space, tab, panes, keeper=None, *, folder=None):
    have = blocks(doc)
    area = next((area for area in doc["spaces"] if area["id"] == space), None)
    target = next((node for node in area["tabs"] if node["id"] == tab), None) if area else None
    for terminal, title in panes:
        if terminal in have:
            continue
        block = {"id": new_id("p"), "type": "block", "title": title, "content": {"terminal": terminal}}
        if target is None:
            if area is None:
                if folder is None:
                    raise KeyError(f"no layout space {space}")
                area = {"id": space, "name": "", "folder": str(folder), "tabs": []}
                doc["spaces"].append(area)
            target = {"id": tab, "name": "team", "root": {"id": new_id("s"), "type": "row", "children": []}}
            area["tabs"].append(target)
        root = target["root"]
        if root["type"] != "row":
            root = target["root"] = {"id": new_id("s"), "type": "row", "children": [root]}
        children = root["children"]
        columns = [i for i, child in enumerate(children)
                   if keeper is None or child["type"] != "block" or child["content"].get("terminal") != keeper]
        if terminal == keeper:
            block["size"] = {"weight": 0.4}
            children.append(block)
        elif len(columns) < 3:
            children.insert(columns[-1] + 1 if columns else 0, block)
        else:
            index = min(columns, key=lambda i: sum(1 for _ in _pane_blocks(children[i])))
            child = children[index]
            if child["type"] == "column":
                child["children"].append(block)
            else:
                children[index] = {"id": new_id("s"), "type": "column", "children": [child, block]}
        have[terminal] = block


def _remove_blocks(node, terminals, *, root=False):
    """Return the remaining node and whether a block was removed below it."""
    if node["type"] == "block":
        return (None, True) if node["content"].get("terminal") in terminals else (node, False)
    children, changed = [], False
    for child in node["children"]:
        kept, removed = _remove_blocks(child, terminals)
        if kept is not None:
            children.append(kept)
        changed |= removed
    if changed:
        if not root and len(children) < 2:
            return (children[0] if children else None), True
        node["children"] = children
    return node, changed


def remove_panes(doc, layout, terminals):
    terminals = {term for term in terminals if term is not None}
    for space, tab in list(_layout_tabs(doc)):
        root, _ = _remove_blocks(tab["root"], terminals, root=True)
        if tab["id"] != layout.get("tab") or any(_pane_blocks(root)):
            continue
        space["tabs"].remove(tab)
        if space["id"] == layout.get("space") and layout.get("made_space") and not space["tabs"]:
            doc["spaces"].remove(space)


def swap_terminal(doc, old, new):
    have = blocks(doc)
    if old not in have or new in have:
        return False
    have[old]["content"]["terminal"] = new
    return True


def edit_layout(hc, change):
    """Apply a layout edit by compare-and-set, recomputing it after each conflict."""
    for attempt in range(1, CAS_TRIES + 1):
        try:
            value, version = hc.doc_get()
            doc = {"spaces": []} if value is None else value
            blocks(doc)  # validate the document before the callback can edit it
            if not change(doc):
                return None
        except (KeyError, TypeError, AttributeError):
            return "the herdr-app doc has an unexpected shape"
        except (HcDown, HcError) as error:
            return str(error)
        try:
            hc.doc_set(doc, version)
            return None
        except HcDown as error:
            return str(error)
        except HcError as error:
            if error.kind != "conflict":
                return str(error)
        if attempt < CAS_TRIES:
            time.sleep(0.05 * attempt)
    return "another client kept changing it"


def _place_team(cfg, state, hc, team, me, keeper, replacements):
    """Place headless team terminals and reuse blocks for replacements, after a successful CAS."""
    try:
        view = team_view(hc, team)
        layout = read_team(state).get("layout", {})
    except (Problem, HcDown, HcError) as error:
        print(f"layout not updated ({error}): run {factory_command('up')} again to place the team", file=sys.stderr)
        return
    panes = {record["address"]: f"@{name}" for name in cfg.members if (record := view.terminal(name))}
    if keeper:
        panes[keeper] = "keeper"
    if not isinstance(layout, dict):
        layout = {}
    saved_layout, placed = None, {}

    def change(doc):
        nonlocal saved_layout, placed
        saved_layout, placed = None, {}
        swaps = dict(replacements)
        if layout.get("keeper") and keeper:
            swaps[layout["keeper"]] = keeper
        changed = [new for old, new in swaps.items() if new in panes and swap_terminal(doc, old, new)]
        space, tab, made = resolve_tab(doc, layout, view, me)
        have = blocks(doc)
        adds = [(terminal, title) for terminal, title in panes.items() if terminal not in have]
        if adds:
            add_panes(doc, space, tab, adds, keeper, folder=cfg.root)
            changed.extend(terminal for terminal, _ in adds)
        if any(area["id"] == space and target["id"] == tab for area, target in _layout_tabs(doc)):
            saved_layout = {"space": space, "tab": tab, "made_space": made}
            if keeper and where(doc, keeper):
                saved_layout["keeper"] = keeper
        placed = {terminal: where(doc, terminal) for terminal in changed}
        return bool(changed)

    reason = edit_layout(hc, change)
    if reason is not None:
        print(f"layout not updated ({reason}): run {factory_command('up')} again to place the team", file=sys.stderr)
        return
    if saved_layout is not None and saved_layout != layout:
        try:
            update_team(state, layout=saved_layout)
        except (Problem, OSError, LogDamage, ValueError) as error:
            print(f"layout placed, but team.json was not updated: {error}", file=sys.stderr)
    for terminal, (space, tab) in placed.items():
        try:
            hc.label(terminal, {"herdr.space": space, "herdr.tab": tab})
        except (HcDown, HcError) as error:
            print(f"warning: could not label the placed terminal {terminal}: {error}", file=sys.stderr)


def _unplace_team(state, hc, recorded, last, keeper):
    names = ", ".join(f"@{name}" for name in dict(recorded))
    close = f"close the panes of {names} in your client" if names else "close the keeper pane in your client"
    try:
        layout = read_team(state).get("layout", {})
    except (Problem, LogDamage, OSError) as error:
        print(f"layout not updated ({error}): {close}", file=sys.stderr)
        return
    if not isinstance(layout, dict):
        layout = {}
    terminals = {term for _, term in recorded}
    if last:
        terminals.update((keeper, layout.get("keeper")))
    saved_layout = layout

    def change(doc):
        nonlocal saved_layout
        before = blocks(doc)
        tabs = list(_layout_tabs(doc))
        remove_panes(doc, layout, terminals)
        saved_layout = dict(layout)
        if saved_layout.get("keeper") not in blocks(doc):
            saved_layout.pop("keeper", None)
        after = list(_layout_tabs(doc))
        if not any(tab["id"] == layout.get("tab") for _, tab in after):
            saved_layout.pop("tab", None)
        if not any(space["id"] == layout.get("space") for space in doc["spaces"]):
            if "space" in saved_layout:
                saved_layout.pop("space")
                saved_layout["made_space"] = False
        return bool(terminals.intersection(before) or len(after) != len(tabs))

    reason = edit_layout(hc, change)
    if reason is not None:
        print(f"layout not updated ({reason}): {close}", file=sys.stderr)
    elif saved_layout != layout:
        try:
            update_team(state, layout=saved_layout)
        except (Problem, OSError, LogDamage, ValueError) as error:
            print(f"layout updated, but team.json was not updated: {error}", file=sys.stderr)


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
        if outcome == "queued" and record["why"] == "busy":    # the recipient whose turn it waits for
            line += (": Factory will deliver it when the operator's turn ends." if name == operator
                     else f": Factory will deliver it when @{name}'s turn ends.")
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
    if kind == "member":   # before anything else can refuse: a refused say still saves the session (§3)
        warning = record_session(cfg, state, sender, me["address"], model=labels.get("factory.model"))
        if warning:
            print(warning, file=sys.stderr)
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
        print(f"not confirmed stored: {error}. Do not resend; check {factory_command('inbox')}.", file=sys.stderr)
        sys.exit(1)

    print(f"message {ident}", flush=True)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    timeout = SAY_WAIT
    try:
        if ensure_keeper(cfg, state, hc, view) is None:
            print(f"warning: the team is stopped. Message {ident} is stored and waits for {factory_command('up')}.",
                  file=sys.stderr)
            timeout = 0
    except (Problem, HcDown, HcError, LogDamage, OSError) as error:
        print(f"warning: the keeper is not running ({error}). Message {ident} is stored and will be delivered once"
              f" it runs; run {factory_command('status')}.", file=sys.stderr)
        timeout = 0
    try:
        outcomes = wait_outcomes(log, ident, names, offset, timeout)
    except (LogDamage, OSError) as error:
        print(f"message {ident} is stored; its outcome could not be read: {error}. Do not resend;"
              f" run {factory_command('inbox')}.",
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
            raise Problem(f"no team state for {cfg.root}", f"run {factory_command('up')}")
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
            raise Problem("no model set", f"Set `model` for this member in {CONF};"
                                          f" `{factory_command('harnesses')}` lists"
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
# argv(m, prompt_file, sess)     -> (native arguments, environment); sess is session_plan's dict or None



# ---- claude

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
    # Developer instructions point Codex to the private file with its full standing instructions.
    pointer = (f"You are @{m['name']}, the {m['role']} on a team of coding agents. Your standing instructions are in"
               f" {prompt_file}. Read that file before your first task, and again whenever your context was"
               " compacted or you are unsure of the rules.")
    # -C: a resumed session goes back to the directory it was started in, whatever the pane's is. Naming the
    # member's directory keeps it where factory.toml says, also after its worktree changed.
    argv = ["--no-daemon", "-m", m["model"], "-C", m["dir"],
            "-c", f"model_reasoning_effort={json.dumps(m['thinking'])}",
            "-c", f"developer_instructions={json.dumps(pointer)}"]
    if m["permissions"] == "guarded":
        argv += ["--add-dir", str(m["state_dir"])]
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
    # Read model configuration from the terminal's environment. The shared server ignores a client's config.
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


# agy 1.5.18 answers "Please sign in" while any of these is set (an SSH login), even when it is signed in
# (@lead-dev's bisect on the real HOME, db85120). Its model list is read without them. A launch needs nothing:
# hc 0.1.20 already leaves these three out of every terminal it starts.
AGY_HIDDEN_ENV = ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY")


def agy_models():
    return {line.partition("\t")[0] for line in run_probe(["agy", "models"], hide=AGY_HIDDEN_ENV).splitlines()
            if "\t" in line}


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
    permissions = list(TABLE[m["harness"]]["permissions"][m["permissions"]])
    if m["harness"] == "agy":
        permissions = agy_trusted(permissions, m["dir"])
    return argv + permissions + m["args"], env


def agy_trusted(args, folder):
    """agy trusts the member's own folder for this launch only, through the session-only --settings it already
    takes: LC-7 showed its trust dialog then does not appear and no trust list is written (spec §6.3: a launch
    argument that avoids the dialog, never accepting it into the global list). The dialog entry stays as the
    fallback."""
    out = list(args)
    at = out.index("--settings") if "--settings" in out else None
    settings = json.loads(out[at + 1]) if at is not None else {}
    settings["trustedWorkspaces"] = [str(folder)]
    value = json.dumps(settings, separators=(",", ":"))
    if at is None:
        out += ["--settings", value]
    else:
        out[at + 1] = value
    return out


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
    with open_log(state).locked(exclusive=decide is not None) as fd:
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
        with open_log(state).locked(exclusive=True) as fd:
            out = private_open(SESSIONS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, dir_fd=fd)
            try:
                os.fsync(out)
            finally:
                os.close(out)
    except (OSError, LogDamage, Problem) as error:
        return str(error)
    return None


def _member_model(m):
    return agy_choice(m)[0] if m["harness"] == "agy" else m.get("model")


def record_session(cfg, state, member, term, environ=os.environ, model=None):
    """Called by say for a member: save the harness's session_env value when it should, with the model its
    terminal was launched with (its factory.model label). -> a warning or None."""
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
        # the model this terminal was launched with, as up resolved it (an "openrouter/..." for a formation's
        # "openai/..."), so the next up's provider check compares like with like: its label, else its launch's
        # entry, else the formation's
        launched = model or (e["model"] if e is not None and e["term"] == term and e.get("model") else None)
        return {"member": member, "harness": harness, "model": launched or _member_model(m), "term": term, "how": "env",
                "value": value, "at": utc_now()}
    try:
        _sessions(state, decide)
    except (OSError, LogDamage, Problem) as error:
        return f"warning: could not record your session: {error}"
    return None


def launch_entry(state, m, term, how, value):
    """up's entry for a launch: "new" as soon as a fresh launch starts, so an older conversation is never resumed
    in its place; the assigned ID or folder once its brief is delivered. Never replaces an entry say made for this
    terminal, nor repeats one."""
    def decide(newest):
        e = newest.get(m["name"])
        if e is not None and e["term"] == term and (how == "new" or e["how"] == "env"
                                                     or (e["how"], e["value"]) == (how, value)):
            return None                              # never "new" after anything for this terminal (@architect-3)
        return {"member": m["name"], "harness": m["harness"], "model": m.get("model"), "term": term, "how": how,
                "value": value, "at": utc_now()}
    _sessions(state, decide)


def new_session_id(harness):
    """An ID for a conversation that does not exist yet, in the form the harness accepts: a UUID for Claude Code;
    for OpenCode "ses_" and 26 letters and digits (LC-4: a UUID is refused)."""
    if harness == "opencode":
        return "ses_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(26))
    return str(uuid.uuid4())


def session_folder(state, m):
    return pathlib.Path(state) / "sessions" / f"{m['name']}-{m['harness']}"


def _provider(model):
    return model.split("/", 1)[0] if model and "/" in model else None


def compatible(entry, m):
    """Whether m may resume entry's conversation: same harness, a value, and a model resume_model allows (a model
    with no provider prefix does not tell, and is allowed)."""
    if entry is None or entry["harness"] != m["harness"] or entry["value"] is None:
        return False
    if entry["how"] == "dir" and TABLE[m["harness"]].get("session") != "dir":
        return False      # --continue outside the member's own session folder would reopen another conversation
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
        sess["assigned"] = new_session_id(harness)
        sess["args"] += FRESH_ARGS[harness](sess["assigned"])
    return sess



# ================================================================ starting members (launch_layout.md §4)

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


def claude_settings(level, message_dir):
    """Claude Code's launch settings for a v2 member (§7): say and inbox run outside a sandbox the owner turned
    on, and at full the hc socket is reachable inside it. The last key skips the first bypassPermissions warning."""
    script = ["python3", str(SCRIPT)]
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
        root = open_log(state).open_dir(create=True)
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
        m = {**m, "state_dir": state, "message_dir": message_dir, "instructions": prompt_file}
        try:
            write_private(root, prompt_file.name, member_prompt(m, operator, cfg))   # rewritten at every launch
            if harness == "claude":
                write_private(root, f"{name}.settings.json", claude_settings(m["permissions"], message_dir))
        except OSError as error:
            raise Problem(f"cannot write {prompt_file}: {error}", "Fix the state directory, then run up again.")
    finally:
        for fd in opened:
            os.close(fd)
    argv, env = launch_argv(m, prompt_file, sess)
    sess = sess or {"resume": False}
    labels = {"factory.team": team_key(cfg.root), "factory.kind": "member", "factory.member": name,
              "factory.dir": str(state), "name": "@" + name}
    if m.get("model"):
        labels["factory.model"] = m["model"]     # as resolved for this launch, for the sessions say records
    fact = _launch_fact(m, state, sess)
    if fact is not None:
        how, value = fact
        labels["factory.launch"] = json.dumps({"how": how, "value": value, "fresh": not sess["resume"],
                                               "harness": harness, "model": m.get("model")},
                                              separators=(",", ":"))
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


def await_ready(hc, started, timeout=LAUNCH_TIMEOUT, clock=time, ready=("idle",)):
    """started: {member: (its terminal record, its harness's dialogs)}. Polls every member every 0.5 s until each
    is settled or the deadline passes. -> {member: None when ready, else the reason it is not}. A launch waits for
    idle; brief --send also takes done, the state a real harness rests in after any turn."""
    deadline = clock.monotonic() + timeout
    outcome, answered, shown, words = {}, {name: set() for name in started}, {}, {}
    seen = {}                                        # the dialog each member showed at its last poll
    while True:
        for name, (record, dialogs) in started.items():
            if name in outcome:
                continue
            address = record["address"]
            try:
                t = hc.get(address)
                if t["state"] == "finished":
                    last = _last_line(hc, address)
                    code = t.get("exit_code")
                    how = (f"exited (code {code})" if code is not None else f"was stopped by signal {t['signal']}"
                           if t.get("signal") is not None else f"ended ({t.get('reason') or 'unknown'})")
                    outcome[name] = how + (f": {last}" if last else "")
                    continue
                word = words[name] = (t.get("status") or {}).get("state")
                if word == "working":
                    seen.pop(name, None)             # two observations of a dialog must be consecutive
                    continue
                screen = "".join(hc.read(address).split())
                matching = [(text, keys) for text, keys in dialogs if "".join(text.split()) in screen]
                asked = next((d for d in matching if d[0] not in answered[name]), matching[0] if matching else None)
                shown[name] = asked and asked[0]
                if asked:
                    text, keys = asked
                    if not keys:
                        outcome[name] = ("waits on its trust dialog: accept it in its pane, then run:"
                                         f" {factory_command('brief', '--send', name)}")
                    elif text not in answered[name] and seen.get(name) == text:
                        hc.keys(address, keys)       # on its second poll: a harness that just drew it may not read yet
                        answered[name].add(text)
                    seen[name] = text
                    continue
                seen.pop(name, None)
                if word in ready:
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
                       "draft_dir": str(m["message_dir"] or f"<state>/messages/{m['name']}"),
                       "draft": (shlex.quote(str(m["message_dir"])) if m["message_dir"]
                                 else f"<state>/messages/{m['name']}") + "/FILENAME",
                       "say": shlex.join(["python3", str(SCRIPT), "say"]),
                       "brief": str(cfg.brief_file), "where": where, "rules": "\n".join(rules),
                       "instructions": str(m["instructions"] or f"<state>/{m['name']}.md"),
                       "notes": str(SKILL / "references/notes.md")}.items():
        text = text.replace("{" + key + "}", value)
    return text.rstrip() + "\n"


def render_team_brief(cfg, members, operator, closing=None):
    """The team brief as sent: a roster built from factory.toml now, then FACTORY.md, then the names."""
    L = cfg.operator
    trees = any(m["branch"] for m in members)
    out = [f"# Team brief (from @{operator})", "",
           f"We are a team of {len(members) + 1} agents, each in its own pane, working on the repo"
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
    messaging = " Use the send command in your standing instructions to message one or more teammates."
    out += ["", "## Talking to each other", "",
            f"Everyone has an @name, which is also their pane's title: {names}, and `{operator}` for the operator."
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


# ================================================================ worktrees


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
    print(f"Edit {BRIEF} (project, workflow, where tasks are tracked) and {CONF} (your members), then run"
          f" `{factory_command('check')}`.")


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



# ================================================================ output helpers

# ================================================================ team commands
# launch_layout.md §§2, 6, 8 and cli_skill.md §2

def stop_with(reason, code=2):
    print(reason, file=sys.stderr)
    sys.exit(code)


def closings(operator):
    """The brief's last line: (ready, pickup, new conversation) (functional_spec.md §§6.4, 9.4)."""
    ready = (f"Send @{operator} one line saying `ready` with the say command in your standing instructions,"
             " then wait for your task.")
    pickup = ("We are picking up where we left off. Check the tracker the brief above names, then send"
              f" @{operator} a new one-line pickup report for this launch, even if you sent one before."
              " Say what you were doing and what you will do next. Send it with the"
              f" say command in your standing instructions. A reply in your own pane does not reach @{operator}.")
    return ready, pickup, "You are starting in a new session and know nothing of the work so far. " + ready


def check_operator(cfg, team, me, view):
    """up's refusals, read-only (launch_layout.md §2): exits 2 with the reason."""
    labels = me.get("labels") or {}
    if labels.get("factory.team") and labels["factory.team"] != team:
        stop_with(f"this pane is @{labels.get('factory.member')} of team {labels['factory.team']}; run up from a pane"
                  " that is not on a team")
    if labels.get("factory.team") == team and labels.get("factory.kind") in ("member", "keeper"):
        stop_with("run up from the operator's pane, not a member's")
    if me.get("status") is None:
        stop_with("this pane reports no agent status to hc (OSC 7501); see the harness notes in the docs")
    other = view.operator
    if other is not None and other["address"] != me["address"] and other.get("status") is not None:
        stop_with(f"@{other['labels'].get('factory.member')} is running in {other['address']}; use that pane")


def mark_operator(cfg, hc, team, me, view, state):
    """After preflight: any other live operator terminal of this team becomes operator-lost, then this one is the
    operator."""
    name = cfg.operator["name"]
    for record in hc.list(team):
        if (record["state"] == "live" and record["address"] != me["address"]
                and record["labels"].get("factory.kind") in ("operator", "operator-lost")):
            hc.label(record["address"], {"factory.kind": "operator-lost"})
    hc.label(me["address"], {"factory.team": team, "factory.kind": "operator", "factory.member": name,
                             "factory.dir": str(state), "name": "@" + name})


def lifecycle_lock(state):
    """Hold <state>/up.lock across up, down or a manual brief, including launch fact settlement. Returns its fd."""
    try:
        directory = open_log(state).open_dir(create=True)
    except (OSError, Problem) as error:
        stop_with(f"cannot open the state directory {state}: {error}")
    try:
        fd = private_open("up.lock", os.O_RDWR | os.O_CREAT, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        stop_with("another up, down or brief --send is running for this team")
    return fd


def end_cause(record):
    """Why a gone member's terminal stopped (launch_layout.md §8), and its last observed state or None."""
    reason = record.get("reason")
    cause = ("its harness ended" if record["state"] == "live"
             else f"exited (code {record.get('exit_code')})" if reason == "exited"
             else "was killed" if reason == "killed" else "hc stopped" if reason == "server_stop"
             else f"was lost ({reason})")
    return cause, (record.get("labels") or {}).get("factory.last")


def interrupted_line(name, record, box):
    """§8: why a gone member stopped, and the last message it got."""
    cause, word = end_cause(record)
    line = f"@{name} interrupted: {cause} " + (f"while {word}." if word else "(state unknown).")
    msg = box.last_to(name) if box is not None else None
    if msg:
        line += f" Last message to it: #{msg['id']} from @{msg['from']} at {msg['at']}."
    return line


def _launch_fact(m, state, sess):
    """The fact to settle for this launch, or None when it resumed by ID."""
    if sess.get("assigned"):
        return "id", sess["assigned"]
    if TABLE[m["harness"]].get("session") == "dir":
        return "dir", str(session_folder(state, m))
    return None if sess["resume"] else ("new", None)


def _valid_launch_fact(fact):
    if not isinstance(fact, dict) or "model" not in fact or "value" not in fact \
            or type(fact.get("fresh")) is not bool \
            or not isinstance(fact.get("harness"), str) \
            or not (fact.get("model") is None or isinstance(fact["model"], str)):
        return False
    how, value = fact.get("how"), fact.get("value")
    if how == "new":
        return value is None and fact["fresh"]
    if not isinstance(value, str):
        return False
    if how == "dir":
        return os.path.isabs(value)
    return how == "id" and bool(SESSION_VALUE.fullmatch(value))


def settle_launches(cfg, state, hc, view, box):
    """Save the launch facts owed by live or finished members. Return unsaved members and warning lines."""
    failed, warnings = {}, []
    briefed = box.briefed()
    for name in cfg.members:
        record = view.terminal(name) or view.finished(name)
        raw = record["labels"].get("factory.launch") if record else None
        if not raw:
            continue
        try:
            fact = json.loads(raw, parse_constant=_reject_constant)
        except (TypeError, ValueError):
            fact = None
        if not _valid_launch_fact(fact):
            warnings.append(f"@{name}: malformed factory.launch label on {record['address']}; left in place")
            continue
        member = {"name": name, "harness": fact["harness"], "model": fact["model"]}
        address, how = record["address"], fact["how"]
        try:
            if fact["fresh"]:
                launch_entry(state, member, address, "new", None)
            if how != "new":
                if address not in briefed:
                    continue
                launch_entry(state, member, address, how, fact["value"])
        except (OSError, LogDamage, Problem) as error:
            failed[name] = str(error)
            continue
        try:
            hc.label(address, {"factory.launch": ""})
        except (HcDown, HcError) as error:
            warnings.append(f"@{name}: its conversation was saved, but the launch label could not be cleared: {error}")
    return failed, warnings


def _settle_current_launches(cfg, state, hc, team, log):
    try:
        view, box = team_view(hc, team), log.read_box()[1]
    except (HcDown, HcError, LogDamage, OSError) as error:
        print(f"warning: could not check for unsaved conversations: {error}", file=sys.stderr)
        return {}
    failed, warnings = settle_launches(cfg, state, hc, view, box)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return failed


def _warn_unsaved_launches(failed):
    for name, why in failed.items():
        print(f"warning: @{name}: its conversation could not be saved yet: {why}", file=sys.stderr)


def _brief_lines(name, ident, outcome):
    """The lines up and brief --send print for one brief, and whether it is confirmed."""
    o = outcome["o"]
    if o == "delivered":
        return [f"@{name}: brief {ident} delivered"], True
    if o in ("sending", "queued"):
        why = f" ({outcome['why']})" if o == "queued" else ""
        return [f"@{name}: brief {ident} is still {o}{why}; the keeper delivers it when it can. Run"
                f" {factory_command('status')}."], False
    return [f"@{name}: brief {ident} {o} ({outcome.get('why')}): check @{name}'s pane, then run"
            f" {factory_command('brief', '--send', name)}"], False


def _pending_brief_line(box, name):
    for msg, _ in reversed(box.pending(name)):
        if msg["kind"] == "brief":
            return f"@{name}: first brief {msg['id']} is still {outcome_word(box.outcome(msg['id'], name))}"
    return None


def _remove_record(hc, record):
    """A gone member's old record, once its new terminal exists: kill a live shell first, then rm."""
    for call in ((hc.kill,) if record["state"] == "live" else ()) + (hc.rm,):
        try:
            call(record["address"])
        except HcError as error:
            if error.kind not in ("not_found", "finished"):
                print(f"warning: could not remove {record['address']}: {error}", file=sys.stderr)
        except HcDown as error:
            print(f"warning: could not remove {record['address']}: {error}", file=sys.stderr)


def cmd_up(args):
    cfg = cfg_of(args)
    names = selected_names(cfg, args.names)
    if not names:
        raise SystemExit(f"no members in {CONF}; add one, then run `{factory_command('up')}`")
    if args.timeout <= 0:
        stop_with("--timeout must be a positive number of seconds")
    me_id = os.environ.get("HC_TERMINAL_ID")
    if not me_id:
        stop_with("run the operator inside an hc terminal, such as a rodeo pane")
    hc, team, operator = Hc(), team_key(cfg.root), cfg.operator["name"]
    try:
        state = state_for(cfg, hc)
    except HcDown as error:
        stop_with(f"hc is not reachable: {error}")
    except Problem as error:
        stop_with(f"{error.what}. Correct or clear that terminal's factory.dir label, then run"
                  f" {factory_command('up')} again.")
    hc.expect_core = hc.core
    lock = lifecycle_lock(state)
    try:
        _up(cfg, args, hc, team, operator, state, names, me_id)
    finally:
        os.close(lock)


def _up(cfg, args, hc, team, operator, state, names, me_id):
    try:
        me, view = hc.get(me_id), team_view(hc, team)
    except (HcDown, HcError) as error:
        stop_with(f"hc is not reachable: {error}")
    check_operator(cfg, team, me, view)
    try:
        current = read_team(state)
    except Problem:
        current = None                               # unreadable: rebuilt below, after preflight
    if current and (current["team"] != team or current["core"] != hc.core or current["root"] != str(cfg.root)):
        stop_with(f"{state}/team.json belongs to another checkout or hc core; remove it, then run"
                  f" {factory_command('up')}")
    log = open_log(state)
    try:
        box = log.read_box()[1]
    except (LogDamage, OSError) as error:
        stop_with(f"the message log cannot be read: {error}")
    unfinished = sorted({r["labels"].get("factory.member") for r in hc.list(team)
                         if r["labels"].get("factory.down") and r["labels"].get("factory.kind") == "member"}
                        & set(names))
    if unfinished:                                   # a stop not recorded yet: a replacement could get its messages
        stop_with("the last stop of " + ", ".join("@" + n for n in unfinished) + " was not recorded; run "
                  + factory_command("down", *unfinished) + " first")
    targets = [n for n in names if view.state(n) in ("not started", "gone")]
    running = [n for n in names if n not in targets]
    if running:
        print("already running: " + ", ".join("@" + n for n in running)
              + (" (--fresh starts only members that are not running)" if args.fresh else ""))
    plans = checked(cfg, [plan(cfg, n, state) for n in targets]) if targets else []
    # Preflight passed: from here on up changes things.
    mark_operator(cfg, hc, team, me, view, state)
    facts = {"v": 1, "root": str(cfg.root), "team": team, "core": hc.core, "up": True}
    update_team(state, reset=current is None, **facts)
    unsaved = _settle_current_launches(cfg, state, hc, team, log)
    failed, fixes = {}, {}                           # per-member launch failures and their recovery commands
    for name, why in unsaved.items():
        if name in targets:
            failed[name] = f"its conversation could not be saved ({why}); not restarted"
        else:
            _warn_unsaved_launches({name: why})
    was_live = [n for n in cfg.members if view.terminal(n) is not None and view.state(n) != "gone"]
    for m in plans:
        old = view.terminal(m["name"]) or view.finished(m["name"])
        if old is not None and view.state(m["name"]) == "gone":
            print(interrupted_line(m["name"], old, box))
    if not was_live:
        print("\n".join(pending_lines(state, operator)))
        try:
            rows, warnings = log.leftovers()
        except (LogDamage, OSError) as error:
            print(f"warning: leftover message files could not be checked: {error}", file=sys.stderr)
        else:
            for line in format_leftovers(rows):
                print(line)
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
    try:
        keeper = ensure_keeper(cfg, state, hc, view, force=True)   # also when every member runs: delivery comes back
    except (Problem, HcDown, HcError, OSError, LogDamage) as error:
        stop_with(f"the keeper could not start: {error}. No member was started.", 3)
    if not plans:
        _place_team(cfg, state, hc, team, me, keeper, {})
        return
    replacements = {}
    for m in plans:
        if m["branch"] and m["name"] not in failed:
            err = ensure_worktree(cfg, m)
            if err:
                failed[m["name"]] = err
    newest, skipped = saved_sessions(state)
    if skipped:
        print(f"skipped {skipped} damaged entries in {state / SESSIONS}")
    starts = {}                                      # name -> (m, sess, record)

    def start(m, sess):
        try:
            record = start_member(cfg, state, hc, m, operator, sess)
        except Problem as error:
            failed[m["name"]] = f"{error.what} ({error.fix})"
            return
        if not sess["resume"]:                       # before its brief: a late one must not leave the old entry newest
            try:
                launch_entry(state, m, record["address"], "new", None)
            except (OSError, LogDamage, Problem) as error:
                _remove_record(hc, record)           # an unrecorded fresh launch could let an old conversation back
                failed[m["name"]] = f"its fresh launch could not be recorded ({error}), so up stopped it"
                again = factory_command("up", m["name"], *(["--fresh"] if args.fresh else []))
                fixes[m["name"]] = f"fix {state / SESSIONS}, then {again}"
                return None
        starts[m["name"]] = (m, sess, record)
        how = "resumed" if sess["resume"] else "started"
        where = f", worktree {m['dir']}" if m["branch"] else ""
        print(f"{how} @{m['name']} ({m['role']}) on {m['harness']} {m['model']}, thinking {m['thinking']},"
              f" permissions {m['permissions']} in {record['address']}{where}")
        return record

    for m in plans:
        if m["name"] in failed:
            continue
        old = view.terminal(m["name"]) or view.finished(m["name"])
        entry = newest.get(m["name"])
        sess = session_plan(m, state, entry, args.fresh)
        if sess["notice"] is None and entry is None and old is not None and not args.fresh:
            sess["notice"] = f"@{m['name']} starts in a new conversation: none was recorded for it"
        if sess["notice"]:
            print(sess["notice"])
        record = start(m, sess)
        if record is not None and old is not None:
            replacements[old["address"]] = record["address"]
            _remove_record(hc, old)                  # the new terminal exists before the old record goes

    _place_team(cfg, state, hc, team, me, keeper, replacements)

    def ready_group(names_now):
        group = {n: (starts[n][2], TABLE[starts[n][0]["harness"]].get("dialogs", [])) for n in names_now}
        return await_ready(hc, group, timeout=args.timeout) if group else {}

    ready = ready_group(list(starts))
    ended = ("exited", "was stopped", "ended")
    retry = [n for n, why in ready.items() if why and starts[n][1]["resume"] and why.startswith(ended)]
    replacements = {}
    for n in retry:
        m, _, record = starts.pop(n)
        print(f"@{n}: its saved conversation could not be resumed ({ready.pop(n)}); starting fresh")
        fresh = start(m, session_plan(m, state, None, True))
        if fresh is not None:
            replacements[record["address"]] = fresh["address"]
            _remove_record(hc, record)
    if replacements:
        _place_team(cfg, state, hc, team, me, keeper, replacements)
    ready.update(ready_group([n for n in retry if n in starts]))
    for n, why in ready.items():
        if why:
            failed[n] = why
    ready_text, pickup_text, fresh_text = closings(operator)
    roster = [plan(cfg, n, state) for n in cfg.members]
    briefs = {}
    for n in [n for n in starts if ready.get(n) is None]:
        m, sess, _ = starts[n]
        ran_before = newest.get(n) is not None or view.terminal(n) is not None or view.finished(n) is not None
        closing = pickup_text if sess["resume"] else fresh_text if ran_before else ready_text
        try:
            ident, offset, warning = log.add(operator, [n], message_for(cfg, m, roster, operator, closing),
                                             kind="brief")
        except NotStored as error:
            failed[n] = f"its brief was not stored: {error.detail}"
            continue
        except LogDamage as error:
            failed[n], fixes[n] = f"its brief was not stored: {error}", f"repair the log, then {factory_command('up')}"
            continue
        except OSError as error:                     # the storage outcome is unknown: never a second brief
            failed[n] = f"its brief may be stored: {error}"
            fixes[n] = f"{factory_command('inbox', '--for', n)}, and {factory_command('status')}"
            continue
        if warning:
            print(f"warning: brief {ident}: {warning}", file=sys.stderr)
        briefs[n] = (ident, offset)
    deadline = time.monotonic() + SAY_WAIT
    for n, (ident, offset) in briefs.items():
        _, _, record = starts[n]
        try:
            outcome = wait_outcomes(log, ident, [n], offset, max(0.0, deadline - time.monotonic()))[n]
        except (LogDamage, OSError) as error:
            failed[n] = f"brief {ident} is stored; its outcome could not be read ({error})"
            fixes[n] = factory_command("status")
            continue
        lines, confirmed = _brief_lines(n, ident, outcome)
        print("\n".join(lines))
        if outcome["o"] in ("sending", "queued"):        # the keeper owns it: never a second brief
            failed[n], fixes[n] = f"brief {ident} is not delivered yet", factory_command("status")
        elif confirmed and outcome.get("term") != record["address"]:
            confirmed = False
            failed[n] = f"brief {ident} went to another terminal ({outcome.get('term')})"
        if not confirmed and n not in failed:
            failed[n] = f"brief {ident} was not confirmed"
    _warn_unsaved_launches(_settle_current_launches(cfg, state, hc, team, log))
    joined = [starts[n][0] for n in briefs if n not in failed and starts[n][0]["name"] not in newest
              and view.terminal(n) is None and view.finished(n) is None]
    others = [n for n in was_live if n not in starts]
    if joined and others:
        news = "; ".join(f"@{m['name']} ({m['role']}, {TABLE[m['harness']]['label']} {m['model']})" for m in joined)
        try:
            log.add(operator, others, f"Joined the team: {news}. No reply needed.")
        except (LogDamage, NotStored, OSError) as error:
            print(f"warning: the news of who joined was not stored: {error}", file=sys.stderr)
    if failed:
        ended += ("could not be watched",)
        for n, why in failed.items():
            if n in fixes:
                fix = fixes[n]
            elif n in starts and not why.startswith(ended):
                fix = factory_command("brief", "--send", n)              # it runs; it has no confirmed brief
            else:
                fix = factory_command("up", n)
            print(f"@{n} is not ready: {why}. Then run: {fix}", file=sys.stderr)
        sys.exit(3)


def cmd_down(args):
    if not os.environ.get("HC_TERMINAL_ID"):
        stop_with("run down from the operator's pane")
    hc = Hc()
    try:
        cfg, state, _ = resolve_team(args, hc)
    except HcDown as error:
        stop_with(f"hc is not reachable: {error}")
    except Problem as error:
        stop_with(f"{error.what}; {error.fix}")
    names = selected_names(cfg, args.names)
    team = team_key(cfg.root)
    lock = lifecycle_lock(state)
    try:
        sys.exit(_down(cfg, hc, team, state, names))
    finally:
        os.close(lock)


def _stop(hc, log, state, name, record):
    """factory.down and the kill, then the stop record (launch_layout.md §6). The terminal record stays until the
    queue sweep has run: while it carries factory.down, up will not replace the member. -> whether it is recorded."""
    address = record["address"]
    try:
        hc.label(address, {"factory.down": "1"})      # before the kill: the keeper drops, not requeues
        if record["state"] == "live":
            hc.kill(address)
    except HcError as error:
        if error.kind != "finished":
            print(f"@{name} ({address}) could not be stopped: {error}")
            return False
    except HcDown as error:
        print(f"@{name} ({address}) could not be stopped: {error}")
        return False
    try:
        mark_stopped(state, [address], log.read_box()[1])
    except (Problem, ValueError, OSError, LogDamage) as error:
        print(f"@{name} stopped, but the stop could not be recorded ({error}); its record {address} is kept."
              f" Run {factory_command('down', name)} again.")
        return False
    return True


def _down(cfg, hc, team, state, names):
    me_id = os.environ["HC_TERMINAL_ID"]
    try:
        records = hc.list(team)
    except (HcDown, HcError) as error:
        stop_with(f"hc is not reachable: {error}")
    view = View(records)
    if view.operator is None or view.operator["address"] != me_id:
        stop_with("run down from the operator's pane")
    problem = sessions_writable(state)
    if problem:
        print(f"sessions cannot be saved ({problem}): members left running")
        return 1
    log, code, stopped, recorded = open_log(state), 0, {}, []
    try:
        box = log.read_box()[1]
    except (LogDamage, OSError) as error:
        print(f"sessions cannot be saved ({error}): members left running")
        return 1
    unsaved, warnings = settle_launches(cfg, state, hc, view, box)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for name in names:
        if name in unsaved:
            kept = f"@{name} left running" if view.terminal(name) else f"@{name}'s record kept"
            print(f"sessions cannot be saved ({unsaved[name]}): {kept}")
            code = 1
            continue
        current = view.terminal(name) or view.finished(name)
        earlier = [r for r in records if r["state"] == "finished" and r["labels"].get("factory.down")
                   and r["labels"].get("factory.kind") == "member" and r["labels"].get("factory.member") == name]
        if current is None and not earlier:
            print(f"@{name} was not running")
            continue
        word = view.state(name) if current is not None else "stopped"
        for record in [r for r in (current, *earlier) if r is not None]:   # an earlier down's unfinished stop too
            if _stop(hc, log, state, name, record):
                stopped[name] = word
                recorded.append((name, record["address"]))
            else:
                code = 1
    if stopped:
        try:
            box = log.read_box()[1]
            queued = {n: sum(1 for msg, to in box.pending(n) if box.outcome(msg["id"], to)["o"] == "queued")
                      for n in stopped}
            log.drop_queued(box, list(stopped), why="stopped by down")
        except (LogDamage, OSError, NotStored) as error:
            print(f"queued messages to the stopped members were not dropped ({error}); their records are kept."
                  f" Run {factory_command('down', *stopped)} again.")
            return 1
        for name, word in stopped.items():
            print(f"stopped @{name} (was {word}); {queued[name]} queued messages dropped")
        for name, address in recorded:               # only now: the swept stop no longer needs its gate
            try:
                hc.rm(address)
            except (HcError, HcDown) as error:
                if not (isinstance(error, HcError) and error.kind == "not_found"):
                    print(f"@{name}'s record {address} could not be removed ({error}); run"
                          f" {factory_command('down', name)} again")
                    code = 1
    try:
        after = hc.list(team)
    except (HcDown, HcError) as error:
        print(f"could not check whether any member still runs: {error}")
        return 1
    members = [r for r in after if r["labels"].get("factory.kind") == "member"]
    last = not any(r["labels"].get("factory.member") in cfg.members for r in members)
    if recorded or last:
        keeper = View(after).keeper if last else None
        _unplace_team(state, hc, recorded, last, keeper["address"] if keeper else None)
    if last:
        for record in members:
            if record["state"] == "live":
                name, address = record["labels"].get("factory.member"), record["address"]
                print(f"@{name} ({address}) is still running outside factory.toml. Run hc kill {address} to stop it.",
                      file=sys.stderr)
                code = 1
        try:
            update_team(state, up=False)
        except (Problem, ValueError, OSError, LogDamage) as error:
            print(f"the team could not be marked down: {error}")
            code = 1
        print(f"run {factory_command('up')} to bring members back")
    return code


def operator_unstarted(terminal, box, cfg):
    """keeper.md §5, the operator's case of an unstarted delivery: shown by status, never typed into its input.
    -> a line when the newest delivery to the operator did not start a turn and it is still idle at that turn, on
    a harness that can hold a typed message unsent; else None."""
    status = terminal.get("status") or {}
    harness = harness_for_app(status.get("app")) if status.get("app") else cfg.operator.get("harness")
    if TABLE.get(harness or "", {}).get("unstarted") != "note":
        return None
    name = cfg.operator["name"]
    delivered = [(ident, rec) for (ident, to), rec in box.outcomes.items()
                 if to == name and rec.get("o") == "delivered"]
    if not delivered:
        return None
    ident, rec = max(delivered, key=lambda pair: pair[0])
    if rec.get("started") is not False or status.get("state") not in ("idle", "done") \
            or terminal.get("turn") != rec.get("turn") or rec.get("term") != terminal.get("address"):
        return None
    return (f"operator: unstarted message #{ident} in {terminal.get('address')}: typed but did not start a turn; ask a"
            f" person to look at the operator's input and press Enter there if it is still waiting.")


def cmd_status(args):
    hc = Hc()
    try:
        cfg, state, _ = resolve_team(args, hc)
        team = team_key(cfg.root)
        records = hc.list(team)
    except HcDown as error:
        stop_with(f"hc is not reachable: {error}")
    except Problem as error:
        stop_with(f"{error.what}; {error.fix}")
    view, operator = View(records), cfg.operator["name"]
    print(f"factory {VERSION} ({SCRIPT}), hc {'.'.join(map(str, hc.version))}, team {team}")
    box = None                                       # unreadable: pending_lines prints the diagnosis
    with contextlib.suppress(LogDamage, OSError, Problem):
        box = open_log(state).read_box()[1]
    rows = [("Member", "Role", "Harness", "Model", "Thinking", "State", "Turn", "Terminal")]
    o = cfg.operator
    for name, role, harness, model, thinking in (
            [(operator, "operator", o.get("harness", ""), o.get("model", ""), o.get("thinking", ""))]
            + [(n, p["role"], p["harness"], p["model"] or "?", p["thinking"])
               for n, p in ((n, plan(cfg, n, state)) for n in cfg.members)]):
        live, finished = view.terminal(name), view.finished(name)
        rows.append(("@" + name, role, harness, model, thinking, view.state(name),
                     str(live["turn"]) if live else "", (live or finished or {}).get("address", "")))
    print_table(rows)
    for name in cfg.members:
        if view.state(name) == "gone":
            print(interrupted_line(name, view.terminal(name) or view.finished(name), box))
    print("\n".join(pending_lines(state, operator)))
    keeper, keeper_error = None, None
    try:
        keeper = ensure_keeper(cfg, state, hc, view)
    except (Problem, HcDown, HcError, OSError, LogDamage) as error:
        keeper_error = error
    panes = None
    with contextlib.suppress(HcDown, HcError, KeyError, TypeError, AttributeError):
        doc, _ = hc.doc_get()
        panes = {} if doc is None else blocks(doc)
    if keeper_error is not None:
        print(f"keeper: not running ({keeper_error})")
    elif keeper is None:
        print("keeper: not running")
    else:
        beat = read_beat(state)
        pane_hint = f", no pane (run {factory_command('up')})" if panes is not None and keeper not in panes else ""
        if beat is None or beat[2] != keeper:
            print(f"keeper: starting ({keeper}){pane_hint}")
        else:
            version = "" if beat[1] == VERSION else f", replacing itself (it runs {beat[1]})"
            print(f"keeper: running, version {beat[1]}, last loop {int(beat[3])} s ago{version}{pane_hint}")
    live_members = {name: record["address"] for name in cfg.members if (record := view.terminal(name))}
    if panes is not None:
        headless = [f"@{name}" for name, term in live_members.items() if term not in panes]
        if headless:
            print(f"no pane: {', '.join(headless)} (run {factory_command('up')})")
    if box is not None:
        briefed = box.briefed()
        for name, term in live_members.items():
            if term not in briefed and view.state(name) not in ("starting", "gone"):
                pending = _pending_brief_line(box, name)
                print(pending or f"@{name} has no confirmed brief: run {factory_command('brief', '--send', name)}")
    reachable = view.operator is not None and view.operator.get("status") is not None
    print("operator: reachable" if reachable
          else f"operator: not reachable: run {factory_command('up')} in the operator's pane")
    if reachable and box is not None:
        line = operator_unstarted(view.operator, box, cfg)
        if line:
            print(line)
    for word, hint in (("blocked", "blocked: waiting on an approval or a question."),
                       ("gone", "gone: run `{command}` to bring them back.")):
        hit = [n for n in cfg.members if view.state(n) == word]
        if hit:
            print(f"{', '.join('@' + n for n in hit)} " + hint.format(command=factory_command("up", *hit)))
    for stray in view.strays:
        print(f"stray: {stray['address']} is another terminal for @{stray['labels'].get('factory.member')};"
              " Factory does not use it")
    try:
        size = (pathlib.Path(state) / Log.LOG).stat().st_size
    except OSError:
        size = 0
    if size > 64 * 1024 * 1024:
        print(f"warning: the message log is {size // (1024 * 1024)} MiB ({pathlib.Path(state) / Log.LOG})")


def _brief_preview(cfg, name, state):
    p = plan(cfg, name, state)
    if state is None:
        print(f"paths are final after {factory_command('up')}", file=sys.stderr)
    print(f"# @{p['name']} ({p['role']}) on {p['harness']} {p['model']}, thinking {p['thinking']}\n")
    print(member_prompt(p, cfg.operator["name"], cfg))


def cmd_brief(args):
    names = [n.lstrip("@") for n in args.names]
    if args.send:
        sys.exit(_brief_send(args, names))
    try:
        cfg, state, _ = find_team(args)
    except (Problem, HcError):
        cfg, state = cfg_of(args), None
    if names:
        for name in selected_names(cfg, names):
            _brief_preview(cfg, name, state)
        return
    print(render_team_brief(cfg, [plan(cfg, n, state) for n in cfg.members], cfg.operator["name"]))


def _brief_send(args, names):
    if not os.environ.get("HC_TERMINAL_ID"):
        stop_with("run brief --send from the operator's pane")
    hc = Hc()
    try:
        cfg, state, me = resolve_team(args, hc)
    except HcDown as error:
        stop_with(f"hc is not reachable: {error}")
    except Problem as error:
        stop_with(f"{error.what}; {error.fix}")
    if me is None or me["labels"].get("factory.kind") != "operator":
        stop_with("run brief --send from the operator's pane")
    lock = lifecycle_lock(state)
    try:
        return _brief_send_locked(cfg, state, hc, me, names)
    finally:
        os.close(lock)


def _brief_send_locked(cfg, state, hc, me, names):
    team = team_key(cfg.root)
    try:
        view = team_view(hc, team)
    except (HcDown, HcError) as error:
        stop_with(f"hc is not reachable: {error}")
    operator = cfg.operator["name"]
    if view.operator is None or view.operator["address"] != me["address"]:
        stop_with("run brief --send from the operator's pane")
    every = names in ([], ["all"])
    names = [n for n in cfg.members if view.terminal(n) is not None] if every else selected_names(cfg, names)
    missing = [n for n in names if view.terminal(n) is None]
    if missing or not names:
        stop_with("not running: " + ", ".join("@" + n for n in missing or cfg.members))
    log = open_log(state)
    try:
        box = log.read_box()[1]
    except (LogDamage, OSError) as error:
        stop_with(str(error))
    briefed, roster = box.briefed(), [plan(cfg, n, state) for n in cfg.members]
    ready_text = (f"Send @{operator} one line saying `ready` with the say command in your standing instructions,"
                  " and mention any work you have already picked up, then wait for your task.")
    code, sent = 0, {}
    for name in names:
        record = view.terminal(name)
        m = plan(cfg, name, state)
        if record["address"] in briefed:
            closing = "No reply needed."
        else:                                        # its first brief: only once it is ready and past its dialogs
            pending = _pending_brief_line(box, name)
            if pending:
                print(f"{pending}; run {factory_command('status')}")
                code = 1
                continue
            # done as well as idle: a replacement that already answered its unconfirmed brief rests in done
            # (Claude Code member case 8 at 97afeec), and a dialog on screen still stops it, as at launch
            why = await_ready(hc, {name: (record, TABLE[m["harness"]].get("dialogs", []))}, timeout=5,
                              ready=("idle", "done"))[name]
            if why:
                print(f"@{name} is not ready: {why}")
                code = 1
                continue
            closing = ready_text
        try:
            ident, offset, warning = log.add(operator, [name], message_for(cfg, m, roster, operator, closing),
                                             kind="brief")
        except (LogDamage, NotStored, OSError) as error:
            print(f"@{name}: brief not stored: {error}")
            code = 1
            continue
        if warning:
            print(f"warning: brief {ident}: {warning}", file=sys.stderr)
        sent[name] = (ident, offset)
    wait = SAY_WAIT
    try:
        if ensure_keeper(cfg, state, hc, view) is None:
            print(f"warning: the team is stopped; the briefs are stored and wait for {factory_command('up')}.",
                  file=sys.stderr)
            wait = 0
    except (Problem, HcDown, HcError, OSError, LogDamage) as error:
        print(f"warning: the keeper is not running ({error}); the briefs are stored. Run {factory_command('status')}.",
              file=sys.stderr)
        wait = 0                                     # read each outcome once, without waiting for nobody
    deadline = time.monotonic() + wait
    for name, (ident, offset) in sent.items():
        try:
            outcome = wait_outcomes(log, ident, [name], offset, max(0.0, deadline - time.monotonic()))[name]
        except (LogDamage, OSError) as error:
            print(f"@{name}: brief {ident} is stored; its outcome could not be read: {error}")
            code = 1
            continue
        lines, confirmed = _brief_lines(name, ident, outcome)
        print("\n".join(lines))
        code = code or (0 if confirmed else 1)
    _warn_unsaved_launches(_settle_current_launches(cfg, state, hc, team, log))
    return code


def print_table(rows):
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for i, r in enumerate(rows):
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths).rstrip())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--cwd", help="where to look for FACTORY.md and factory.toml (default: here, then parent directories)")

    def launching(sp):
        sp.add_argument("--timeout", type=float, default=LAUNCH_TIMEOUT,
                        help=f"seconds to wait for the members to be ready (default {LAUNCH_TIMEOUT})")
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
    up.add_argument("--fresh", action="store_true", help="start new conversations instead of resuming")
    launching(up)

    down = sub.add_parser("down", help="stop members; up resumes their conversations")
    selection(down)
    common(down)

    st = sub.add_parser("status", help="members and whether they are running")
    common(st)

    ib = sub.add_parser("inbox", help="print recent committed messages for a recipient")
    ib.add_argument("count", nargs="?", type=int, default=10, help="last N messages (default 10)")
    ib.add_argument("--for", dest="for_name", help="recipient name (default: this member or the operator)")
    common(ib)

    bf = sub.add_parser("brief", help="print the team brief, a member's standing instructions (NAME), or --send it")
    bf.add_argument("names", nargs="*", help="a member's name; with --send: all, or these members")
    bf.add_argument("--send", action="store_true")
    common(bf)

    sy = sub.add_parser("say", help="message teammates from your member or operator pane")
    sy.add_argument("to", nargs="+", help="recipient names separated by spaces or commas, or all for members only; "
                    "the last argument is the message unless --file")
    sy.add_argument("--file", help="send this file's contents")

    keeper = sub.add_parser("keeper", help="run the team's delivery process")
    keeper.add_argument("--state", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "say":
        args.message = args.to.pop() if args.file is None and len(args.to) > 1 else None
    {"init": cmd_init, "harnesses": cmd_harnesses, "check": cmd_check, "up": cmd_up, "down": cmd_down,
     "status": cmd_status, "inbox": cmd_inbox, "brief": cmd_brief, "say": cmd_say,
     "keeper": cmd_keeper}[args.cmd](args)


if __name__ == "__main__":
    main()
