#!/usr/bin/env python3
"""factory: a software team of coding agents in herdr, one team per herdr session. You are the operator (CTO);
each member runs in its own pane of the team tab, on its own harness, model and thinking level.

A project has two files at its root. FACTORY.md is the brief every member reads: the project and how the team
works; it is committed and shared. factory.toml is one person's formation: the operator, the members and their
harnesses and models; it is not committed (factory.toml.sample is, as the project's suggestion). herdr holds the
rest: a member's pane is labeled with its @name and its agent carries that name. Harnesses are described in
harnesses.toml. `factory.py --help` lists the commands.
"""
import argparse
import collections
import datetime
import fcntl
import getpass
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib

SKILL = pathlib.Path(__file__).resolve().parent.parent
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


class Problem(Exception):
    """A member cannot launch: what is wrong, and how the user can fix it."""

    def __init__(self, what, fix):
        super().__init__(what)
        self.what, self.fix = what, fix


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
        if self.operator.get("messages", "inbox") not in ("inbox", "typed"):
            raise SystemExit(f"[operator] messages {self.operator['messages']!r} must be inbox or typed")
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
        unknown = self.team.keys() - {"user", "permissions", "worktree_dir", "operator", "members"}
        if unknown:
            raise SystemExit(f"{self.conf_file}: unknown top-level key(s): {sorted(unknown)};"
                             " operator fields belong under [operator].")
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
        base = pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache")
        key = f"{self.root.name[:24]}-{hashlib.sha1(str(self.root).encode()).hexdigest()[:8]}"
        return base / "factory-skill" / key

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
            "draft_dir": cfg.messages / name}


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




class InboxDamage(ValueError):
    def __init__(self, path, line, reason):
        super().__init__(f"inbox needs repair: {path}, line {line}: {reason}. Operator messages are unavailable."
                         " After repairing the file, run factory.py up.")


class Inbox:
    """Session-local transport. Hold the stable lock for file operations, never while waiting on agents."""
    def __init__(self, cfg):
        self.directory = cfg.cache
        self.path = self.directory / f"inbox-{session_key()}.jsonl"
        self.state_path = self.path.with_suffix(".json")
        self.lock_path = self.path.with_suffix(".lock")

    @staticmethod
    def private_open(path, flags, *, read_only=False):
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"not a regular file: {path}")
            if not read_only:
                os.fchmod(fd, 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def __enter__(self):
        for path in (self.directory.parent, self.directory):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not stat.S_ISDIR(path.stat().st_mode):
                raise OSError(f"not a directory: {path}")
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                path.chmod(0o700)
        self.lock = self.private_open(self.lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX)
            saved = self.read_state()
            self.has_state = saved is not None
            self.state = saved if self.has_state else {"mode": "typed", "seen": 0}
            raw = self.read(self.path) or b""
            if raw and not raw.endswith(b"\n"):
                raise InboxDamage(self.path, raw.count(b"\n") + 1, "incomplete line")
            self.rows = [self.decode(line, self.path, n) for n, line in enumerate(raw.splitlines(), 1)]
            self.validate_rows(self.rows, self.path)
            return self
        except BaseException:
            os.close(self.lock)
            raise

    def __exit__(self, *_):
        os.close(self.lock)

    def read(self, path, *, read_only=False):
        try:
            fd = self.private_open(path, os.O_RDONLY, read_only=read_only)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as f:
            return f.read()

    def read_state(self, *, read_only=False):
        # save_state replaces the file atomically, so reading the route alone needs no lock.
        raw = self.read(self.state_path, read_only=read_only)
        if raw is None:
            return None
        state = self.decode(raw, self.state_path, 1)
        if (not isinstance(state, dict) or state.get("mode") not in ("inbox", "typed")
                or type(state.get("seen")) is not int or state["seen"] < 0):
            raise InboxDamage(self.state_path, 1, "expected mode and non-negative integer seen")
        history = state.get("history", [])
        self.validate_rows(history, self.state_path)
        if len(history) > 100 or (history and history[-1]["id"] > state["seen"]):
            raise InboxDamage(self.state_path, 1, "expected at most 100 acknowledged history entries, none above seen")
        return state

    @staticmethod
    def decode(raw, path, line):
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as e:
            raise InboxDamage(path, line, str(e)) from e

    @staticmethod
    def validate_rows(rows, path):
        if not isinstance(rows, list):
            raise InboxDamage(path, 1, "expected a list of messages")
        last = 0
        for n, row in enumerate(rows, 1):
            if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= last
                    or not all(isinstance(row.get(k), str) for k in ("at", "from", "text"))):
                raise InboxDamage(path, n, "expected increasing positive id and at/from/text strings")
            last = row["id"]

    @property
    def unread(self):
        return [r for r in self.rows if r["id"] > self.state["seen"]]

    @property
    def highest(self):
        return max([self.state["seen"], *(r["id"] for r in self.rows)])

    def sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def replace(self, path, data):
        fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, path)
            self.sync_directory()
        finally:
            pathlib.Path(name).unlink(missing_ok=True)

    def save_state(self):
        self.replace(self.state_path, (json.dumps(self.state) + "\n").encode())
        self.has_state = True

    def remove_seen(self):
        unread = self.unread
        if len(unread) != len(self.rows):
            self.replace(self.path, b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode() for r in unread))
            self.rows = unread

    def acknowledge(self, seen):
        if seen < 0 or seen > self.highest:
            raise ValueError(f"invalid inbox id {seen}; highest is {self.highest}")
        if seen > self.state["seen"]:
            moved = [r for r in self.unread if r["id"] <= seen]
            self.state["history"] = (self.state.get("history", []) + moved)[-100:]
            self.state["seen"] = seen
            if not self.unread:
                self.state.pop("codex_wake", None)
            self.save_state()  # Save recovery text and seen together before removing inbox rows.
        self.remove_seen()


def print_messages(rows):
    for row in rows:
        print(f"[Message {row['id']} from @{row['from']} at {row['at']}]:")
        print("\n".join("    " + line for line in row["text"].splitlines()))


def cmd_inbox(args):
    require_herdr()
    if args.count < 1:
        raise SystemExit("give a positive number of acknowledged messages")
    cfg = cfg_of(args)
    try:
        with Inbox(cfg) as inbox:
            rows = inbox.state.get("history", [])[-args.count:]
        print_messages(rows)
        if not rows:
            print("no acknowledged messages kept")
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        raise SystemExit(2)


def configured_route(cfg, state):
    if state and (state["mode"] == "inbox" or cfg.operator.get("messages") == "typed"):
        return state["mode"]
    return "unavailable"


def message_route(cfg):
    try:
        return configured_route(cfg, Inbox(cfg).read_state(read_only=True))
    except (InboxDamage, OSError):
        return "unavailable"


def operator_wait(harness, route):
    return TABLE.get(harness, {}).get("operator_wait", "foreground") if route == "inbox" else "background"


def check_watcher_directory(directory):
    for path in (directory.parent.parent, directory.parent, directory):
        if path.is_symlink():
            raise OSError(f"expected a private directory, found a symbolic link: {path}")


def lock_watcher_directory(directory, *, register=False):
    """With the inbox mutex held, acquire the directory lock without waiting for its owner."""
    check_watcher_directory(directory)
    if register:
        directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mode = fcntl.LOCK_EX if register else fcntl.LOCK_SH
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def watcher_lock(cfg, *, register=False):
    """Retain the directory lock for a watcher, or probe it through the existing inbox mutex."""
    inbox, directory = Inbox(cfg), cfg.session_dir
    check_watcher_directory(directory)
    flags = os.O_RDWR if register else os.O_RDONLY
    mutex = inbox.private_open(inbox.lock_path, flags, read_only=True)
    try:
        fcntl.flock(mutex, fcntl.LOCK_EX if register else fcntl.LOCK_SH)
        fd = lock_watcher_directory(directory, register=register)
        if register:
            return fd
        os.close(fd)
    finally:
        os.close(mutex)


def watcher_status(cfg):
    try:
        watcher_lock(cfg)
    except BlockingIOError:
        return "running"
    except FileNotFoundError:
        return "absent"
    except OSError as e:
        return f"unknown ({e})"
    return "absent"


# Default deadline and the shorter stretch if we cannot recognize the current composer.
COMPOSER_WAIT = {"codex": (float("inf"), 60), "pi": (2_147_183, 300)}
# What a composer wait tells a Codex or pi operator when it ends while members are live.
LIVE = "Members are live: run factory.py wait again now; do not end your turn."
REARM = "After replying, run factory.py wait again."
KEEP = "Members are live: keep polling this wait until it exits; do not end your turn while it runs."
STATUS_LIVE = "Members are live: run factory.py wait now and keep it running."


def configure_messages(cfg, team, operator, restart=()):
    mode = cfg.operator.get("messages", "inbox")
    try:
        with Inbox(cfg) as inbox:
            previous = inbox.state["mode"] if inbox.has_state else None
            if not restart and not any(team.live(n) for n in cfg.members):
                inbox.remove_seen()
                if inbox.unread:
                    print(f"{len(inbox.unread)} unread message{'s' if len(inbox.unread) != 1 else ''}"
                          " from the earlier team; factory.py wait prints them")
            inbox.state["mode"] = mode
            inbox.save_state()
    except (InboxDamage, OSError) as e:
        print(str(e) if isinstance(e, InboxDamage) else
              f"inbox unavailable: {str(e).rstrip('.')}. Operator messages are unavailable."
              " After fixing it, run factory.py up.")
        return
    if mode == "inbox":
        harness = (team.pane(operator) or {}).get("agent")
        wait = operator_wait(harness, mode)
        if wait == "background":
            action = "in the background with --background-task and end your turn; it wakes you with messages and problems"
        else:
            action = "in the foreground, and again each time it returns while "
            action += "members are live, without --timeout" if harness in COMPOSER_WAIT else "a member works"
        print(f"members reach @{operator} through the inbox; run factory.py wait {action}")
    else:
        print(f'members reach @{operator} by typed herdr messages ([operator] messages = "typed")')
    if previous == "typed" and mode == "inbox" and any(
        n not in restart and (team.pane(n) or {}).get("agent") == "codex"
        and (member.get("permissions") or cfg.team.get("permissions")) == "guarded"
        for n, member in cfg.members.items()
    ):
        print("Restart any guarded Codex members launched on the typed route with factory.py up <name> --restart"
              " to grant inbox access.")


def message_text(args):
    if bool(args.file) == (args.message is not None):
        raise SystemExit("give a message or --file, not both")
    try:
        text = pathlib.Path(args.file).read_text() if args.file else args.message
    except (OSError, UnicodeError) as e:
        raise SystemExit(f"cannot read message file {args.file}: {e}") from None
    if not text.strip():
        raise SystemExit("give a non-empty message")
    return text


def remove_delivered_draft(cfg, sender, filename):
    """Only consume the sender's own managed draft. Other --file inputs belong to the caller."""
    if not filename:
        return
    path = pathlib.Path(filename).absolute()
    directory = cfg.messages / sender
    if path.parent == directory and not any(p.is_symlink() for p in (cfg.session_dir, cfg.messages, directory)):
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            print(f"delivered, but could not remove {path}: {e}. Discard it without resending.", file=sys.stderr)


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


def watcher_registered(cfg):
    """Probe under the inbox mutex. A registered reader may be waiting for that same mutex to finish."""
    try:
        os.close(lock_watcher_directory(cfg.session_dir))
    except FileNotFoundError:
        pass
    except BlockingIOError:
        return True
    return False


def codex_thread(pane):
    ref = pane.get("agent_session")
    if (pane.get("agent") != "codex" or not isinstance(ref, dict) or ref.get("agent") != "codex"
            or ref.get("kind") not in ("id", "path") or not isinstance(ref.get("value"), str)):
        return None
    thread = session_id(ref)
    return thread if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", thread) else None


def save_codex_wake(inbox, token):
    previous = inbox.state.get("codex_wake")
    inbox.state["codex_wake"] = token
    try:
        inbox.save_state()
    except OSError:
        # The state file is synced before replacement. A visible replacement only needs its directory synced.
        saved = inbox.read_state(read_only=True) or {}
        current = saved.get("codex_wake")
        if current != token:
            if previous is not None and current == previous:
                inbox.state = saved
                release_codex_wake(inbox, previous)
            raise
        try:
            inbox.sync_directory()
        except OSError:
            release_codex_wake(inbox, token)
            raise


def claim_codex_wake(inbox, cfg, team, sender, number):
    name = cfg.operator["name"]
    pane = team.pane(name) or {}
    if (pane.get("agent") or cfg.operator.get("harness")) != "codex":
        return None
    if "codex_wake" in inbox.state or watcher_registered(cfg):
        return None
    member = cfg.members[sender]
    if ((team.pane(sender) or {}).get("agent") == "codex"
            and (member.get("permissions") or cfg.team.get("permissions")) == "guarded"):
        raise ValueError("guarded Codex sender")
    pane_id, thread = pane.get("pane_id"), codex_thread(pane)
    if not pane_id or team.agent_panes.get(name) != pane_id or not thread:
        raise ValueError("could not verify the operator's Codex thread")
    token = {"id": number, "thread": thread}
    save_codex_wake(inbox, token)
    return pane_id, token


def release_codex_wake(inbox, token):
    if inbox.state.get("codex_wake") == token:
        del inbox.state["codex_wake"]
        inbox.save_state()


def live_codex_target(cfg):
    name = cfg.operator["name"]
    agent = herdr("agent", "get", name, check=False, timeout=2).get("agent") or {}
    pane_id, thread = agent.get("pane_id"), codex_thread(agent)
    if not pane_id or agent.get("name") != name or not thread:
        return None
    pane = herdr("pane", "get", pane_id, check=False, timeout=2).get("pane") or {}
    if pane.get("pane_id") != pane_id or pane.get("label") != name or codex_thread(pane) != thread:
        return None
    info = herdr("pane", "process-info", "--pane", pane_id, check=False, timeout=2)
    argv = harness_argv(info, "codex")
    if argv and not any(arg.split("=", 1)[0] == "--remote" for arg in argv):
        return pane_id, thread
    return None


class WakeRefused(ValueError):
    """The native wake command did not start."""


def dispatch_codex_wake(cfg, pane_id, token):
    thread = token["thread"]
    try:
        if live_codex_target(cfg) != (pane_id, thread):
            raise ValueError("could not verify a live local Codex operator thread")
        with Inbox(cfg) as inbox:
            if inbox.state.get("codex_wake") != token:
                return
            if watcher_registered(cfg):
                release_codex_wake(inbox, token)
                return
        # The local CLI checks its own thread store. Never resume an unloaded thread or forward the report.
        child = subprocess.Popen(["codex", "queue", "--thread", thread, "--message", "run factory.py wait"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, ValueError) as e:
        raise WakeRefused(str(e)) from e
    # Once launch might have occurred, retain the batch claim on error or interruption.
    with child:
        try:
            code = child.wait(timeout=10)
        except BaseException:
            child.kill()
            raise
    if code:
        raise ValueError(f"codex queue exited {code}")


def wake_codex_operator(cfg, pane_id, token):
    try:
        dispatch_codex_wake(cfg, pane_id, token)
    except WakeRefused:
        # Retain the reservation while refreshing the target so coalesced reports still have a sender handling the batch.
        try:
            target = live_codex_target(cfg)
        except (OSError, ValueError):
            target = None
        with Inbox(cfg) as inbox:
            if inbox.state.get("codex_wake") != token:
                return
            try:
                covered = not inbox.unread or watcher_registered(cfg)
            except OSError:
                release_codex_wake(inbox, token)
                raise
            if covered or target is None:
                release_codex_wake(inbox, token)
                if covered:
                    return
                raise ValueError("could not verify a live local Codex operator thread")
            pane_id, thread = target
            if thread != token["thread"]:
                token = {**token, "thread": thread}
                save_codex_wake(inbox, token)
        try:
            dispatch_codex_wake(cfg, pane_id, token)
        except WakeRefused:
            with Inbox(cfg) as inbox:
                release_codex_wake(inbox, token)
            raise


def queue_message(cfg, sender, text, *, team=None):
    started = durable = False
    try:
        with Inbox(cfg) as inbox:
            if not inbox.has_state or inbox.state["mode"] != "inbox":
                return "refused", "the operator inbox route changed. Check factory.py status before retrying.", None
            inbox.remove_seen()
            row = {"id": inbox.highest + 1, "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "from": sender, "text": text}
            payload = (json.dumps(row, ensure_ascii=False) + "\n").encode()
            fd = inbox.private_open(inbox.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                started = True
                while payload:
                    written = os.write(fd, payload)
                    if written == 0:
                        raise OSError("inbox write made no progress")
                    payload = payload[written:]
                os.fsync(fd)
                inbox.sync_directory()
                durable = True
            finally:
                os.close(fd)
            wake = claim_codex_wake(inbox, cfg, team, sender, row["id"]) if team else None
        if wake:
            wake_codex_operator(cfg, *wake)
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as e:
        if durable:
            return "queued", row["id"], str(e) or "wake interrupted"
        if isinstance(e, KeyboardInterrupt):
            raise
        return ("unconfirmed" if started else "refused"), str(e), None
    return "queued", row["id"], None


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


def argv_claude(m, prompt_file, resume):
    argv = ["--model", m["model"], "--effort", m["thinking"], "--append-system-prompt-file", str(prompt_file),
            "--name", m["name"], "--settings", str(prompt_file.with_suffix(".settings.json"))]
    return argv + (["--resume", session_id(resume)] if resume else []), {}


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


def argv_codex(m, prompt_file, resume):
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
        argv += ["--add-dir", str(m.get("inbox_dir") or m["draft_dir"])]
    return (["resume", session_id(resume)] if resume else []) + argv, {}


# ---- pi and omp (omp keeps pi's flags and session format)
def model_pi(checker, model, harness):
    query = model.rsplit("/", 1)[-1]
    argv = ["pi", "--list-models", query] if harness == "pi" else ["omp", "models", query]
    env = {"PI_OFFLINE": "1"} if harness == "pi" else None
    listed = listed_models(checker.probe((harness, query), lambda: run_probe(argv, env)))
    return resolve_listed(model, listed, harness), TABLE[harness]["thinking"]


def argv_pi(m, prompt_file, resume):
    argv = ["--model", m["model"], "--thinking", m["thinking"], "--append-system-prompt", str(prompt_file)]
    if m["harness"] == "pi":
        return argv + ["--name", m["name"]] + (["--session", resume["value"]] if resume else []), {}
    return argv + ([f"--resume={resume['value']}"] if resume else []), {}


# ---- opencode (v2+)
def model_opencode(checker, model, harness):
    listed = listed_models(checker.probe("opencode-models", lambda: run_probe(["opencode", "models", "--standalone"])))
    return resolve_listed(model, listed, harness), None  # thinking is a per-model variant; OpenCode reports a bad one at start


def argv_opencode(m, prompt_file, resume):
    # The interface herdr can watch has no model flag, so the model comes from inline config in the pane's
    # environment. --standalone: OpenCode's shared background server ignores a client's own config.
    model = m["model"] + (f"#{m['thinking']}" if m["thinking"] != "default" else "")
    argv = ["--standalone"] + (["--session", session_id(resume)] if resume else [])
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


def argv_agy(m, prompt_file, resume):
    model = m["model"]
    if m["thinking"] != "default" and not agy_effort(model):
        model += "-" + m["thinking"]
    return ["--model", model] + (["--conversation", session_id(resume)] if resume else []), {}


Harness = collections.namedtuple("Harness", "model argv")
CODE = {
    "claude": Harness(model_claude, argv_claude),
    "codex": Harness(model_codex, argv_codex),
    "pi": Harness(model_pi, argv_pi),
    "omp": Harness(model_pi, argv_pi),
    "opencode": Harness(model_opencode, argv_opencode),
    "agy": Harness(model_agy, argv_agy),
}


def launch_argv(m, prompt_file, resume=None):
    """-> (native argv after `--`, pane env): the harness's own arguments, then the member's permission level
    and any extra `args` from factory.toml."""
    argv, env = CODE[m["harness"]].argv(m, prompt_file, resume)
    return argv + TABLE[m["harness"]]["permissions"][m["permissions"]] + m["args"], env


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
                       "say": shlex.join(["python3", str(SKILL / "scripts/factory.py"), "say", "--cwd", str(cfg.root)]),
                       "brief": str(cfg.brief_file), "where": where, "rules": "\n".join(rules),
                       "instructions": str(cfg.session_dir / f"{m['name']}.md"),
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
        return {"text": p.stdout}  # `pane read` and `integration status` print text


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


def ensure_operator(cfg, tab=False):
    """Name the operator's agent and label its pane after `[operator] name`; when launching, label its tab
    too. Returns the name."""
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise SystemExit("HERDR_PANE_ID is not set; run this from the operator's herdr pane.")
    got = herdr("agent", "get", pane, check=False)
    info = got.get("agent") or {}
    if "error" in got and "not_found" not in got["error"]:  # herdr itself failed: say that, not "no agent"
        raise SystemExit(f"herdr agent get {pane}: {got['error']}")
    if not info:
        raise SystemExit(f"herdr sees no agent in this pane ({pane}). Run factory.py from the operator's pane.")
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
    if cfg.operator.get("messages", "inbox") == "inbox":
        m = {**m, "inbox_dir": str(cfg.cache)}
    if m["harness"] == "claude":
        # Keep the typed launch command below macOS's 1024-byte line limit.
        prompt_file.with_suffix(".settings.json").write_text(
            claude_settings(m["permissions"], m["draft_dir"]))
    argv, env = launch_argv(m, prompt_file, resume)
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
    # A harness may stop at a first-run question (trust this folder?), and herdr does not always see that it
    # did. So look at the screen: a question the harness table knows is answered, once, and must then go away.
    # A member that is not ready for another reason gets a few seconds: an update banner clears by itself.
    answered, waiting = {}, lambda: "agent_not_ready" in started.get("error", "")
    for _ in range(8):
        # A narrow pane wraps a question over several lines, so compare without whitespace.
        screen = "".join(herdr("pane", "read", pane, "--source", "visible", check=False).get("text", "").split())
        asked = [d for d in TABLE[m["harness"]].get("dialogs", []) if "".join(d[0].split()) in screen]
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
    """Members that did not start; one that is live was stopped by a dialog of its own. A member that started
    but did not get its brief (`undelivered`) fails the command too, and is told how to repair it."""
    for name, err in failed.items():
        if team.live(name):
            print(f"@{name} started but is waiting on a dialog: {err}. Read its pane (herdr agent read {name}"
                  f" --source visible), tell the user what it asks, and once it is answered run:"
                  f" factory.py brief --send {name}")
        else:
            print(f"@{name} did not come up: {err}. Read the pane, fix the cause, then run: factory.py up {name}")
    if undelivered:
        print(f"The team brief did not reach {', '.join('@' + n for n in undelivered)}. Once they can take a message,"
              f" run: factory.py brief --send {' '.join(undelivered)}")
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


def cmd_check(args):
    cfg = cfg_of(args)
    plans = selected(cfg, args.names)
    ready, problems = check_all(cfg, plans)
    ok = {p["name"]: p for p in ready}
    print_plan([ok.get(p["name"], p) for p in plans], ("Check", lambda p: "ok" if p["name"] in ok else "FAILED"))
    if problems:
        report_problems(problems)
        sys.exit(2)


def cmd_up(args):
    cfg = cfg_of(args)
    plans = selected(cfg, args.names)
    if not plans:
        raise SystemExit(f"no members in {CONF}; add one, then run `factory.py up`")
    require_herdr()
    # The operator pane may still carry a member name or label from an earlier team.
    team = Team(cfg, exclude_pane=os.environ.get("HERDR_PANE_ID"))
    if not any(team.live(n) for n in cfg.members):
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
    operator = ensure_operator(cfg, tab=True)
    configure_messages(cfg, Team(cfg), operator, restart=[m["name"] for m in plans] if args.restart else ())
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
    watcher = watcher_status(cfg)
    print(f"registered operator watcher process: {watcher}", flush=True)
    team = Team(cfg)
    rows = [("Member", "Role", "Harness", "Model", "Thinking", "Status", "Pane")]
    operator = cfg.operator["name"]
    operator_pane = team.pane(operator) or {}
    rows.append(("@" + operator, "operator", cfg.operator.get("harness", ""), cfg.operator.get("model", ""), cfg.operator.get("thinking", ""),
                 team.status(operator), operator_pane.get("pane_id", "")))
    for name in cfg.members:
        p = plan(cfg, name)
        pane = team.pane(name)
        rows.append(("@" + name, p["role"], p["harness"], p["model"] or "?", p["thinking"], team.status(name),
                     (pane or {}).get("pane_id", "")))
    print_table(rows)
    harness = operator_pane.get("agent")
    if (watcher == "absent" and operator_wait(harness, "inbox") == "background"
            and team.agent_panes.get(operator) == operator_pane.get("pane_id")
            and any(team.status(n).startswith("working") for n in cfg.members)):
        print("No operator watcher is registered. Check native tasks before starting a background wait.")
    try:
        with Inbox(cfg) as inbox:
            suffix = (f"; {len(inbox.unread)} unread message{'s' if len(inbox.unread) != 1 else ''}"
                      " (factory.py wait prints them)") if inbox.unread else ""
            route = configured_route(cfg, inbox.state if inbox.has_state else None)
            if route == "inbox":
                style = operator_wait(harness, route)
                suffix = f", {style} wait" + suffix
            print(f"operator messages: {route}{suffix}")
        # Codex and pi operators check status between waits; tell them to keep listening while members are live.
        pane = os.environ.get("HERDR_PANE_ID")
        if (route == "inbox" and harness in COMPOSER_WAIT and pane and operator_pane.get("pane_id") == pane
                and team.agent_panes.get(operator) == pane
                and any(team.pane(n) and team.status(n) != "gone" for n in cfg.members)):
            print(STATUS_LIVE)
    except (OSError, ValueError) as e:
        print(f"operator messages: unavailable ({e})")
    for state, hint in (("blocked", "blocked: waiting on an approval or a question. Read the pane and ask the user."),
                        ("gone", "gone: run `factory.py up {names}` to bring them back.")):
        hit = [r[0] for r in rows[2:] if state in r[5]]
        if hit:
            print(f"\n{', '.join(hit)} " + hint.format(names=" ".join(n.lstrip("@") for n in hit)))
    if not any(team.pane(n) for n in cfg.members):
        print(f"\nNo member of {CONF} has a pane in this herdr session. Run `factory.py up` to start or resume its members.")


def codex_wait_render(block):
    heading, _, terminal = block.partition("\n")
    if heading.startswith("• Working ("):
        # Codex can hang a hint below its status line ("  └ Tip: ..."), wrapped onto indented lines.
        return not terminal or bool(re.fullmatch(r"  └ \w[\w ]{0,15}: \S.*(?:\n {4,}\S.*)*", terminal))
    if not terminal:
        return False
    # During a poll of an existing command, Codex shows the terminal below a different status line.
    waiting = "• Waiting for background terminal"
    clipped = heading.removesuffix("…").rstrip()
    return terminal.lstrip().startswith("└ ") and (
        heading.startswith(waiting) or (clipped.startswith("• Waiting for") and waiting.startswith(clipped)))


def composer_state(harness, screen):
    """Read only the visible region adjacent to the editor. A cue needs its neighboring UI, not just its text."""
    lines = screen.splitlines()
    if harness == "codex":
        editor = next((i for i in reversed(range(len(lines))) if re.match(r"^›(?: |$)", lines[i])), None)
        if editor is None:
            return "unknown"
        blocks = re.split(r"\n[ \t]*\n\s*", "\n".join(lines[:editor]).rstrip())
        if codex_wait_render(blocks[-1]):
            return "waiting"
        head, arrow, _ = blocks[-1].partition("↳")
        if (len(blocks) > 1 and codex_wait_render(blocks[-2]) and arrow
                and "".join(head.split()) == "•Queuedfollow-upinputs"):
            return "follow-up"
        return "unknown"

    # pi draws the working line as the top border of the editor; the bottom border remains plain.
    border = next((i for i in reversed(range(len(lines))) if re.fullmatch(r"─{3,}", lines[i])), 0)
    working = next((i for i in reversed(range(border)) if lines[i].startswith("──")), None)
    if working is None or not re.fullmatch(r"─+ [\u2800-\u28ff] Working ─+", lines[working]):
        return "unknown"
    block = re.split(r"\n[ \t]*\n\s*", "\n".join(lines[:working]).rstrip())[-1]
    queued, arrow, hint = block.rpartition("↳")
    if arrow and "".join(hint.split()).startswith("Alt+Uptoedit"):
        rows = [line.strip() for line in queued.splitlines()]
        if any(line.startswith("Follow-up:") for line in rows):
            return "follow-up"
        if any(line.startswith("Steering:") for line in rows):
            return "steering"
    if re.fullmatch(r"Elapsed (?:\d+h )?(?:\d+m )?\d+(?:\.\d+)?s", block.strip()):
        return "waiting"
    return "unknown"


def read_composer(pane, harness):
    try:
        got = herdr("pane", "read", pane, "--source", "visible", check=False, timeout=1)
    except OSError:
        return "unknown", "could not start herdr"
    if "error" in got:
        return "unknown", got["error"].split("\n", 1)[0]
    return composer_state(harness, got.get("text") or ""), None


def cmd_wait(args):
    """Watch messages and lifecycle changes, and keep verified Codex/pi operators listening through idle."""
    require_herdr()
    cfg = cfg_of(args)
    try:
        with Inbox(cfg) as inbox:
            route = configured_route(cfg, inbox.state if inbox.has_state else None)
    except (InboxDamage, OSError):
        route = "unavailable"
    name = cfg.operator["name"]
    live = herdr("agent", "get", name, check=False)
    operator = live.get("agent") or {}
    pane = os.environ.get("HERDR_PANE_ID")
    verified = pane and operator.get("pane_id") == pane and operator.get("name") == name
    native = verified and operator_wait(operator.get("agent"), "inbox") == "background"
    codex = verified and route == "inbox" and operator.get("agent") == "codex"
    if not args.background_task and native:
        print("Start factory.py wait --background-task as a native background task and end your turn."
              " See leading.md.", file=sys.stderr)
        sys.exit(2)
    if args.background_task and not native:
        print("cannot verify a background-capable operator in this pane. Check factory.py status and the operator pane.",
              file=sys.stderr)
        sys.exit(2)
    if not (args.background_task or codex):
        return wait_for_events(args, cfg, route, operator)
    try:
        fd = watcher_lock(cfg, register=True)
    except BlockingIOError:
        print("operator wait already running. Reconcile the existing native task before starting another.", file=sys.stderr)
        sys.exit(2)
    except OSError as e:
        print(f"cannot register the operator watcher: {e}", file=sys.stderr)
        sys.exit(2)
    def finish():
        nonlocal fd
        # Read while holding both locks. Senders holding the mutex only probe the directory with LOCK_NB.
        try:
            with Inbox(cfg) as inbox:
                unread = inbox.unread
                os.close(fd)
                fd = None
            return unread
        except (OSError, ValueError) as e:
            print(f"cannot finish the operator inbox wait: {e}", file=sys.stderr)
            raise SystemExit(2)

    try:
        return wait_for_events(args, cfg, route, operator, finish=finish if codex else None)
    finally:
        if fd is not None:
            os.close(fd)


def wait_for_events(args, cfg, route, operator, *, finish=None):
    watch = [n.lstrip("@") for n in args.names]
    harness = operator.get("agent")
    wait = operator_wait(harness, route)
    quiet = route == "inbox" and wait == "background" and not watch
    pane = os.environ.get("HERDR_PANE_ID")
    watch_composer = (route == "inbox" and wait == "foreground" and not watch and harness in COMPOSER_WAIT
                      and bool(pane) and operator.get("pane_id") == pane)
    default_timeout, fallback = COMPOSER_WAIT[harness] if watch_composer else (60 if wait == "foreground" else 1800, None)
    if args.background_task and harness == "omp":
        default_timeout = 240
    timeout = args.timeout if args.timeout is not None else default_timeout
    interval = min(args.interval, 2) if watch_composer else args.interval
    started = time.monotonic()
    seen, worked, looks = None, set(), 0
    warned = False
    unsure_until, unsure_detail = None, ""  # the shorter deadline while the composer stays unrecognized

    def report_unread(unread, cue):
        if finish:
            unread = finish()
        if unread:
            print_messages(unread)
            next_watch = f", acknowledge by starting your next watcher in the {wait}" if route == "inbox" else ""
            background_flag = " --background-task" if args.background_task else ""
            print(f"{len(unread)} unread message{'s' if len(unread) != 1 else ''}."
                  f" After you have acted on or recorded these messages{next_watch}:"
                  f" factory.py wait{background_flag} --seen {unread[-1]['id']}")
        # The owner may submit while we are taking the inbox snapshot or printing the reports.
        if unread and watch_composer and cue not in ("follow-up", "steering"):
            cue, _ = read_composer(pane, harness)
        if cue == "follow-up":
            print(f"Owner follow-up queued. End your turn to receive it. {REARM}")
        elif cue == "steering":
            print(f"Owner steering queued. Let the harness deliver it before starting another wait. {REARM}")
        return bool(unread or cue in ("follow-up", "steering"))

    if watch_composer:  # a Codex tool yield shows only this, so it must keep the operator polling
        print(KEEP, flush=True)
    while True:
        cue = None
        if watch_composer:
            cue, error = read_composer(pane, harness)
            if cue != "unknown":  # Codex shows no working line for a moment between its own polls
                unsure_until = None
            elif unsure_until is None:
                unsure_until, unsure_detail = time.monotonic() - started + fallback, f" ({error})" if error else ""
        unread = []
        try:
            with Inbox(cfg) as inbox:
                if args.seen is not None:
                    inbox.acknowledge(args.seen)
                    args.seen = None
                unread = inbox.unread
        except (OSError, ValueError) as e:
            if args.seen is not None or quiet:
                print(e, file=sys.stderr)
                raise SystemExit(2)
            if not warned:
                print(f"inbox unavailable: {e}" if isinstance(e, OSError) else str(e), flush=True)
                warned = True
        if unread or cue in ("follow-up", "steering"):
            report_unread(unread, cue)
            return
        team = Team(cfg)
        state = {n: team.status(n) for n in cfg.members if (not watch or n in watch) and team.pane(n)}
        if seen is None:  # the members this watch is about: a pane that closes later is noticed
            missing = [n for n in watch if n not in state]
            if missing or not state:
                raise SystemExit(f"not started: {', '.join(missing) or 'any member'}")
            seen = list(state)
        looks += 1
        closed = [n for n in seen if n not in state]
        needs = [n for n, s in state.items() if s.startswith("blocked") or s == "gone"]
        working = [n for n, s in state.items() if s.startswith("working")]
        settled = [n for n, s in state.items() if s.startswith(("idle", "done"))]
        unclear = [n for n in state if n not in needs + working + settled]  # herdr reports `unknown`, or nothing
        # A prompt takes a moment to turn into `working`, so a named member counts as stopped, and the team as
        # idle, only from the second look on. The same goes for a status herdr has not worked out yet.
        stopped = [] if quiet else [n for n in settled if n in worked or (n in watch and looks > 1)]
        if closed or needs or stopped or (not quiet and looks > 1 and (unclear or (not working and not watch_composer))):
            break
        deadline = min(timeout, unsure_until) if unsure_until is not None else timeout
        remaining = deadline - (time.monotonic() - started)
        if remaining <= 0:
            if finish and report_unread([], cue):
                return
            unknown = "; status unknown: " + ", ".join("@" + n for n in unclear) if unclear else ""
            if deadline < timeout:
                print(f"cannot verify the operator composer{unsure_detail}: it stayed unrecognized for {fallback}s.")
                timeout = deadline
            if watch_composer:
                still = "; still working: " + ", ".join("@" + n for n in working) if working else ""
                print(f"timeout after {timeout:.9g}s{still}{unknown}. {LIVE}")
                sys.exit(4)
            activity = "still working: " + ", ".join("@" + n for n in working) if working else "nobody is working"
            print(f"timeout after {timeout:.9g}s; {activity}{unknown}")
            sys.exit(4)
        worked.update(working)
        time.sleep(min(interval, remaining) if watch_composer else interval)
    if finish:
        report_unread([], cue)
    for n in closed:
        print(f"@{n}'s pane was closed, so it is no longer running. `factory.py up {n}` gives it a new pane.")
    for n in needs:
        if state[n] == "gone":
            print(f"@{n} is gone: its agent exited. `factory.py up {n}` brings it back.")
            continue
        print(f"@{n} is {state[n]}: it needs an answer. Its pane ends:")
        # herdr cannot scroll a blocked agent's history, so read what is on its screen
        tail = herdr("pane", "read", team.panes[n]["pane_id"], "--source", "visible", check=False)
        print("\n".join("    " + l for l in (tail.get("text") or tail.get("error") or "").strip().splitlines()[-15:]))
    for n in unclear:
        print(f"@{n}: status unknown (herdr reports {state[n]!r}), so it may be working or stuck. Read its pane:"
              f" herdr agent read {n} --source visible")
    for n in stopped:
        print(f"@{n} stopped ({state[n]}). If it sent no report, read its pane:"
              f" herdr agent read {n} --source recent-unwrapped --lines 80")
    if watch_composer and any(n not in closed and state[n] != "gone" for n in state):
        print("Members are live: after you act on this, run factory.py wait again; do not end your turn.")
    if not (closed or needs or unclear or stopped):
        hint = ". End your turn; run factory.py wait again after you assign work." if wait == "foreground" else ""
        print(f"nobody is working: {', '.join('@' + n + ' ' + s for n, s in state.items())}{hint}")


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


def cmd_say(args):
    """Send directly from the invoking member's pane, or as the operator."""
    require_herdr()
    cfg = cfg_of(args)
    team = Team(cfg, record_sessions=False)
    pane = os.environ.get("HERDR_PANE_ID")
    member = next((n for n in cfg.members if pane and team.agent_panes.get(n) == pane), None)
    if member and (team.pane(member) or {}).get("pane_id") != pane:
        raise SystemExit(f"this @{member} agent is outside its team's labeled pane")
    if member and args.file:
        path = pathlib.Path(args.file)
        if path.parent.resolve() == (cfg.messages / member).resolve() and not re.fullmatch(r"[A-Za-z0-9-]+\.[A-Za-z0-9]+", path.name):
            raise SystemExit("nothing sent: use letters, digits and dashes in the draft name, plus a plain extension such as .md")
    text = message_text(args)
    sender = member or ensure_operator(cfg)
    targets = re.findall(r"[^,\s]+", " ".join(args.to))
    names = ([m["name"] for m in team.members() if m["name"] != member] if targets == ["all"]
             else [n.lstrip("@") for n in targets])
    operator = cfg.operator["name"]
    if sender in names:
        raise SystemExit("choose another teammate as the recipient")
    if not names:
        raise SystemExit("not running: any member")
    route = message_route(cfg) if operator in names else None
    refused, unconfirmed = [], False
    for name in dict.fromkeys(names):
        advisory = None
        if name == operator and route == "inbox":
            outcome, detail, advisory = queue_message(cfg, sender, text, team=team if member else None)
        elif name == operator and route != "typed":
            outcome, detail = "refused", "operator messages are unavailable. Check factory.py status and rerun factory.py up."
        elif (name != operator and name not in cfg.members) or not team.live(name):
            outcome, detail = "refused", f"not running: {name}"
        else:
            detail = prompt_error(name, f"{tag(sender)} {text}")
            outcome = "submitted" if detail is None else "refused" if delivery_refused(detail) else "unconfirmed"
        if outcome == "queued":
            print(f"queued message {detail} for @{name}")
            if advisory:
                print(f"Operator wake unavailable or unconfirmed ({advisory})."
                      " The operator can read the unread batch with factory.py wait.",
                      file=sys.stderr)
        elif outcome == "submitted":
            print(f"submitted your message to @{name}")
        elif outcome == "refused":
            print(f"not sent to @{name}: {detail}")
            refused.append(name)
        else:
            print(f"delivery unconfirmed to @{name}: {detail}")
            unconfirmed = True
    if refused or unconfirmed:
        if unconfirmed:
            print("Delivery unconfirmed. Keep the draft and report the result without resending.")
        elif args.file:
            print_retry(cfg, refused, args.file)
        sys.exit(1)
    remove_delivered_draft(cfg, sender, args.file)


def print_retry(cfg, names, filename):
    cmd = ["python3", str(SKILL / "scripts/factory.py"), "say", "--cwd", str(cfg.root), *names, "--file", filename]
    print(f"Retry once when ready: {shlex.join(cmd)}")


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

    wt = sub.add_parser("wait", help="return for unread inbox messages or a blocked, gone or closed member; "
                                     "an operator's Codex/pi inbox watch also returns for queued owner input")
    wt.add_argument("names", nargs="*", help="watch only these members (default: every member)")
    wt.add_argument("--background-task", action="store_true",
                    help="declare a native background launch (required for Claude Code, omp and OpenCode operators)")
    wt.add_argument("--timeout", type=float, help="seconds before giving up (exit 4). Operator Codex/pi inbox waits "
                    "default to no deadline and 2147183 seconds respectively. Native omp background waits default to 240, "
                    "other foreground inbox waits to 60, and all others to 1800")
    wt.add_argument("--interval", type=float, default=5, help="seconds between looks (up to 2 in operator Codex/pi inbox waits)")
    wt.add_argument("--seen", type=int, help="acknowledge inbox messages through this id, then wait. "
                                            "Reread them with factory.py inbox")
    common(wt)

    ib = sub.add_parser("inbox", help="reprint recent acknowledged inbox messages")
    ib.add_argument("count", nargs="?", type=int, default=5, help="last N messages (default 5, up to 100 kept)")
    common(ib)

    bf = sub.add_parser("brief", help="print the team brief as members get it, or --send it")
    bf.add_argument("names", nargs="*", help="with --send: only these members")
    bf.add_argument("--send", action="store_true")
    common(bf)

    sy = sub.add_parser("say", help="message teammates from your member or operator pane")
    sy.add_argument("to", nargs="+", help="recipient names separated by spaces or commas, or all for members only; "
                    "the last argument is the message unless --file")
    sy.add_argument("--file", help="send this file's contents")
    common(sy)

    pr = sub.add_parser("prompt", help="the standing instructions a member gets")
    pr.add_argument("name")
    common(pr)

    args = ap.parse_args(argv)
    if args.cmd == "say":
        args.message = None if args.file else (args.to.pop() if len(args.to) > 1 else None)
    {"init": cmd_init, "harnesses": cmd_harnesses, "check": cmd_check, "up": cmd_up, "down": cmd_down,
     "status": cmd_status, "wait": cmd_wait, "inbox": cmd_inbox, "brief": cmd_brief, "say": cmd_say,
     "prompt": cmd_prompt}[args.cmd](args)


if __name__ == "__main__":
    main()
