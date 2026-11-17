#!/usr/bin/env python3
"""a-team: a software team of coding agents in herdr, one team per herdr session. You are the operator (CTO);
each member runs in its own pane of the team tab, on its own harness, model and thinking level.

A project has two files at its root. TEAM.md is the brief every member reads: the project and how the team
works; it is committed and shared. team.toml is one person's formation: the operator, the members and their
harnesses and models; it is not committed (team.toml.sample is, as the project's suggestion). herdr holds the
rest: a member's pane is labeled with its @name and its agent carries that name. Harnesses are described in
harnesses.toml. `ateam.py --help` lists the commands.
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
SETTLE = float(os.environ.get("ATEAM_SETTLE", 8))  # seconds a member may take to get past a start-up notice
BRIEF = "TEAM.md"              # the brief: shared and committed
CONF = "team.toml"             # one person's formation: not committed
SAMPLE = "team.toml.sample"    # the project's suggested formation: committed, copied to team.toml by `init`
LEGACY = "team-brief.md"       # the single file of earlier versions: found only to say how to split it
OPERATOR = "operator"  # the operator's @name, unless `[operator] name` in team.toml says otherwise
TABS = {"operator": "a-team operator", "team": "a-team workers"}  # herdr tab labels


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
        if any((d / name).is_file() for name in (CONF, BRIEF, LEGACY)):
            return d
    return cwd


def refuse_legacy(root):
    """With an old team-brief.md and not both new files, stop before anything is read or written: defaults
    would quietly replace the team it describes."""
    if (root / LEGACY).is_file() and not ((root / BRIEF).is_file() and (root / CONF).is_file()):
        raise SystemExit(f"{root / LEGACY} is the single-file brief of earlier versions; nothing was written."
                         f" Split it by hand:\n"
                         f"  1. Move the settings between its +++ lines to {CONF}.\n"
                         f"  2. In {CONF}, rename [lead] or [orchestrator] to [operator] and remove `tracker`.\n"
                         f"  3. Move the prose to {BRIEF}, and say there where tasks are tracked.\n"
                         f"  4. Delete {LEGACY}, add {CONF} to .gitignore, and run `ateam.py check`.")


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
            refuse_legacy(self.root)
            raise SystemExit(f"no {' or '.join(missing)} in {self.root} or above. Run `/a-team setup`, or"
                             f" `ateam.py init`, which writes what is missing and copies the project's {SAMPLE} to"
                             f" {CONF} if there is one.")
        try:
            self.team = tomllib.loads(self.conf_file.read_text()) if self.conf_file.is_file() else {}
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{self.conf_file}: {e}")
        self.body = self.brief_file.read_text().strip() if self.brief_file.is_file() else ""
        for old in ("lead", "orchestrator"):
            if old in self.team:
                raise SystemExit(f"{self.conf_file}: rename [{old}] to [operator]")
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
        if "teams" in self.team:  # from before: starting "every member" would start a whole catalog
            raise SystemExit(f"{self.conf_file}: [teams] is no longer used. The members in {CONF} are the team:"
                             " remove [teams] and the members you do not want.")
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
        """Per-repo scratch outside the repo: members' prompt files and the sessions `continue` resumes from.
        The path is short on purpose: it is typed into a shell line that macOS limits to 1024 bytes."""
        base = pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache") / "a-team"
        return base / f"{self.root.name[:24]}-{hashlib.sha1(str(self.root).encode()).hexdigest()[:8]}"

    @property
    def worktree_dir(self):
        """Where members' git worktrees live: next to the repo unless team.toml sets `worktree_dir`."""
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
    """A member's settings from team.toml, before availability checks."""
    m = cfg.members[name]
    role = m.get("role", name)
    meta, _ = cfg.roles[role]
    permissions = m.get("permissions") or cfg.team.get("permissions") or "full"
    if permissions not in LEVELS:
        raise SystemExit(f"@{name}: permissions {permissions!r} must be one of {', '.join(LEVELS)}")
    worktree = m.get("worktree")
    branch = (f"{cfg.user}/{name}" if worktree is True else worktree) or None
    where = cfg.worktree_dir / re.sub(r"[^A-Za-z0-9._-]+", "-", branch) if branch else cfg.root
    return {"name": name, "role": role, "harness": m["harness"], "model": m.get("model"),
            "thinking": str(m.get("thinking") or meta["thinking"]),
            "readonly": meta["readonly"] == "true", "notes": m.get("notes", ""), "permissions": permissions,
            "args": [str(a) for a in m.get("args", [])], "branch": branch, "dir": str(where)}


# ================================================================ availability checks

def run_probe(argv, env=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                           env={**os.environ, **(env or {})})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Problem(f"`{' '.join(argv[:3])}` failed: {e}",
                      "Run ateam.py outside any sandbox, or check that the harness starts on its own.")
    if p.returncode != 0:
        err = [l.strip() for l in (p.stderr.strip() or p.stdout.strip()).splitlines() if l.strip()]
        named = [l for l in err if re.match(r"(error|\w*Error)\b", l, re.I)]  # skip stack frames
        tail = (named or err or [f"exit {p.returncode}"])[0][:300]
        raise Problem(f"`{' '.join(argv[:3])}` failed: {tail}",
                      "If a sandbox blocks it (read-only file system), run ateam.py outside the sandbox.")
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
    raise Problem(f"{harness} has no model {base} configured", fix + " `ateam.py harnesses` lists what it can use.")




class InboxDamage(ValueError):
    def __init__(self, path, line, reason):
        super().__init__(f"inbox needs repair: {path}, line {line}: {reason}. This session runs on typed messages."
                         " After repairing the file, run ateam.py continue --restart.")


class Inbox:
    """Session-local transport. Hold the stable lock for file operations, never while waiting on agents."""
    def __init__(self, cfg):
        self.directory = cfg.cache
        session = os.environ.get("HERDR_SESSION") or "default"
        key = re.sub(r"[^A-Za-z0-9_.-]", "-", session)[:24] + "-" + hashlib.sha1(session.encode()).hexdigest()[:8]
        self.path = self.directory / f"inbox-{key}.jsonl"
        self.state_path = self.path.with_suffix(".json")
        self.lock_path = self.path.with_suffix(".lock")

    @staticmethod
    def private_open(path, flags):
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"not a regular file: {path}")
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
            raw = self.read(self.state_path)
            self.state = {"mode": "typed", "seen": 0} if raw is None else self.decode(raw, self.state_path, 1)
            if (not isinstance(self.state, dict) or self.state.get("mode") not in ("inbox", "typed")
                    or type(self.state.get("seen")) is not int or self.state["seen"] < 0):
                raise InboxDamage(self.state_path, 1, "expected mode and non-negative integer seen")
            self.rows = []
            raw = self.read(self.path) or b""
            if raw and not raw.endswith(b"\n"):
                raise InboxDamage(self.path, raw.count(b"\n") + 1, "incomplete line")
            last = 0
            for n, line in enumerate(raw.splitlines(), 1):
                row = self.decode(line, self.path, n)
                if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= last
                        or not all(isinstance(row.get(k), str) for k in ("at", "from", "text"))):
                    raise InboxDamage(self.path, n, "expected increasing positive id and at/from/text strings")
                self.rows.append(row)
                last = row["id"]
            return self
        except BaseException:
            os.close(self.lock)
            raise

    def __exit__(self, *_):
        os.close(self.lock)

    def read(self, path):
        try:
            fd = self.private_open(path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as f:
            return f.read()

    @staticmethod
    def decode(raw, path, line):
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as e:
            raise InboxDamage(path, line, str(e)) from e

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

    def remove_seen(self):
        unread = self.unread
        if len(unread) != len(self.rows):
            self.replace(self.path, b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode() for r in unread))
            self.rows = unread

    def acknowledge(self, seen):
        if seen < 0 or seen > self.highest:
            raise ValueError(f"invalid inbox id {seen}; highest is {self.highest}")
        if seen > self.state["seen"]:
            self.state["seen"] = seen
            self.save_state()  # A crash before removing the lines cannot make them unread again.
        self.remove_seen()


def message_route(cfg):
    """Previews outside herdr and damaged transport files use the original typed instructions."""
    if os.environ.get("HERDR_ENV") != "1" or getattr(cfg, "inbox_unavailable", False):
        return "typed"
    try:
        with Inbox(cfg) as inbox:
            return inbox.state["mode"]
    except (InboxDamage, OSError):
        return "typed"


def configure_messages(cfg, team, operator, restart=()):
    conf, live = cfg.operator.get("harness"), (team.pane(operator) or {}).get("agent")
    mode = TABLE[conf]["operator_messages"] if conf and conf == live else "typed"
    reason = (f"[operator] harness is not set in {CONF}" if not conf else
              f"{CONF} says {conf}, herdr sees {live} in this pane" if conf != live else "")
    try:
        with Inbox(cfg) as inbox:
            previous = inbox.state["mode"]
            if not restart and not any(team.live(n) for n in cfg.members):
                inbox.remove_seen()
                if inbox.unread:
                    print(f"{len(inbox.unread)} unread from the earlier team; ateam.py wait prints them")
                else:
                    inbox.path.unlink(missing_ok=True)
                    inbox.state_path.unlink(missing_ok=True)
                    inbox.sync_directory()
                    inbox.state["seen"] = 0
            inbox.state["mode"] = mode
            inbox.save_state()
    except (InboxDamage, OSError) as e:
        print(str(e) if isinstance(e, InboxDamage) else
              f"inbox unavailable: {str(e).rstrip('.')}. This session runs on typed messages."
              " After fixing it, run ateam.py continue --restart.")
        mode, previous = "typed", "typed"
        cfg.inbox_unavailable = True  # Later rendering in this invocation must not reselect the stored route.
    if mode == "inbox":
        print(f"members reach @{operator} through the inbox (ateam.py inbox send); wait prints their messages")
    else:
        print(f"members reach @{operator} by typed herdr messages" + (": " + reason if reason else ""))
    if mode != previous and any(team.live(n) and n not in restart for n in cfg.members):
        print(f"the route to @{operator} changed to {mode}: run ateam.py continue --restart so running members get it")


def cmd_inbox_send(args):
    cfg = cfg_of(args)
    if args.sender not in cfg.members:
        raise SystemExit(f"not a member of {CONF}: {args.sender}")
    if bool(args.file) == (args.message is not None):
        raise SystemExit("give a message or --file, not both")
    try:
        text = pathlib.Path(args.file).read_text() if args.file else args.message
    except (OSError, UnicodeError) as e:
        raise SystemExit(f"cannot read message file {args.file}: {e}") from None
    if not text.strip():
        raise SystemExit("give a non-empty message")
    started = False
    try:
        with Inbox(cfg) as inbox:
            if inbox.state["mode"] != "inbox":
                raise ValueError("the operator takes typed messages in this session; this member has inbox instructions,"
                                 f" restart it with ateam.py continue {args.sender} --restart")
            inbox.remove_seen()
            row = {"id": inbox.highest + 1, "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "from": args.sender, "text": text}
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
            finally:
                os.close(fd)
        print(f"queued #{row['id']}")
    except (OSError, ValueError) as e:
        if started:
            print(f"not confirmed: {e}. The message may or may not be stored. Do not send it again by any route.",
                  file=sys.stderr)
            raise SystemExit(1)
        reason = str(e).rstrip(".")
        cause = "needs repair" if isinstance(e, InboxDamage) else "cannot write" if isinstance(e, OSError) else "no inbox route"
        print(f"not stored: {reason}. Nothing was queued. Send it typed, once, exactly as printed:", file=sys.stderr)
        fallback = f"{tag(args.sender)} Inbox unavailable: {cause}.\n{text}\n"
        print(shlex.join(["herdr", "agent", "prompt", cfg.operator["name"], fallback]), file=sys.stderr)
        raise SystemExit(5)


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
        # Defaults used to be a-team/<name>. A member on `worktree = true` whose new default branch does not exist
        # yet, while its old checkout is still there, may be leaving unmerged work behind. A branch that
        # team.toml names is a decision and is left alone.
        old, implicit = f"a-team/{p['name']}", self.cfg.members[p["name"]].get("worktree") is True
        if implicit and branch != old and old in trees.values() and git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            raise Problem(f"this member has an earlier worktree on branch {old}, which may hold unmerged work",
                          f'Set `worktree = "{old}"` for it in {CONF} to keep using it, or merge and remove it first.')
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
            raise Problem("no model set", f"Set `model` for this member in {CONF}; `ateam.py harnesses` lists"
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
    if (cfg.root / LEGACY).is_file():
        print(f"note: {cfg.root / LEGACY} is no longer read; {BRIEF} and {CONF} replace it. Delete it.", file=sys.stderr)
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
def claude_settings(level, inbox=False):
    """Inline settings for a member. herdr's messaging commands run outside a sandbox the user has turned on,
    when they are the whole command line. At `full` herdr's socket is also allowed inside it, so a message gets
    through in any command shape (Linux can only allow every Unix socket; macOS can name herdr's). The last key
    skips the warning Claude Code shows the first time it starts in bypassPermissions mode."""
    sandbox = {"excludedCommands": ["herdr agent prompt *", "herdr agent list"]}
    if inbox:
        sandbox["excludedCommands"].append(shlex.join(["python3", str(SKILL / "scripts/ateam.py"), "inbox", "send"]) + " *")
    if level == "full":
        sock = os.environ.get("HERDR_SOCKET_PATH")
        sandbox["network"] = {"allowUnixSockets": [sock]} if sys.platform == "darwin" and sock else {"allowAllUnixSockets": True}
    return json.dumps({"sandbox": sandbox, "skipDangerousModePermissionPrompt": True,
                       "permissions": {"allow": [f"Bash({cmd})" for cmd in sandbox["excludedCommands"]]}},
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
            "--name", m["name"], "--settings", claude_settings(m["permissions"], bool(m.get("inbox_dir")))]
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
    # member's directory keeps it where team.toml says, also after its worktree changed.
    argv = ["-m", m["model"], "-C", m["dir"], "-c", f"model_reasoning_effort={json.dumps(m['thinking'])}",
            "-c", f"developer_instructions={json.dumps(pointer)}"]
    if m["permissions"] == "guarded" and m.get("inbox_dir"):
        argv += ["--add-dir", m["inbox_dir"]]
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


Harness = collections.namedtuple("Harness", "model argv")
CODE = {
    "claude": Harness(model_claude, argv_claude),
    "codex": Harness(model_codex, argv_codex),
    "pi": Harness(model_pi, argv_pi),
    "omp": Harness(model_pi, argv_pi),
    "opencode": Harness(model_opencode, argv_opencode),
}


def launch_argv(m, prompt_file, resume=None):
    """-> (native argv after `--`, pane env): the harness's own arguments, then the member's permission level
    and any extra `args` from team.toml."""
    argv, env = CODE[m["harness"]].argv(m, prompt_file, resume)
    return argv + TABLE[m["harness"]]["permissions"][m["permissions"]] + m["args"], env


# ================================================================ briefs

def tag(name):
    return f"[from @{name}]"


def member_prompt(m, operator, cfg):
    """Standing instructions for one member: templates/member-prompt.md, filled in with who it is, its role,
    where it works, and the rules that depend on that."""
    if m["branch"]:
        where = (f"You work in your own git worktree, `{m['dir']}`, on branch `{m['branch']}`. The main checkout is"
                 f" `{cfg.root}`, on branch `{cfg.main_branch}`. Stay in your worktree and on your branch.")
        rules = [f"- Commit your work on `{m['branch']}` as you go, in small commits with clear messages. That is the"
                 f" only branch you commit to. Never push, and never merge into or check out another branch:"
                 f" @{operator} reviews your branch and merges it. Before a new task, bring your branch up to date"
                 f" with `git merge {cfg.main_branch}`. If a commit fails (for example your sandbox blocks git),"
                 f" tell @{operator} or the teammate concerned and agree who commits it for you."]
    else:
        where = f"You work in `{cfg.root}`, the checkout you share with every teammate who has no worktree of their own."
        rules = [f"- Never run `git commit`, `git merge` or `git push`; @{operator} commits."]
    if m["readonly"]:
        rules.append("- You are read-only: do not edit the project's files. You may read anything, build and run"
                     f" tests. Write only scratch files outside the repo, or a document @{operator} asks you for.")
    text = (SKILL / "templates/member-prompt.md").read_text()
    inbox = message_route(cfg) == "inbox"
    to_operator = "" if not inbox else """
To reach {operator}, do not use herdr. Run this as a standalone command, your message as one single-quoted argument or `--file <path>`:
```bash
{inbox_send} '<message>'
```
It queues the message without typing into {operator}'s pane. `queued #N` means stored, not read: finish with `Report queued for {operator}`, not "sent". `not stored` prints a herdr command: run it once, exactly as printed. Any other failure: do not send the message again by any route; say so in your pane and stop. In Reports below, "keep it pending" and the `agent_blocked` retry apply to herdr messages only.
"""
    text = text.replace("{teammates}", "teammates other than {operator}" if inbox else "teammates")
    text = text.replace("{to_operator}", to_operator)
    text = text.replace("{inbox_send}", shlex.join(["python3", str(SKILL / "scripts/ateam.py"), "inbox", "send",
                                                   "--cwd", str(cfg.root), "--from", m["name"]]))
    for key, value in {"role_rules": cfg.roles[m["role"]][1], "name": m["name"], "role": m["role"], "operator": f"@{operator}",
                       "brief": str(cfg.brief_file), "where": where, "rules": "\n".join(rules),
                       "instructions": str(cfg.cache / f"{m['name']}.md"),
                       "notes": str(SKILL / "references/notes.md")}.items():
        text = text.replace("{" + key + "}", value)
    return text.rstrip() + "\n"


def render_team_brief(cfg, members, operator, closing=None):
    """The team brief as sent: a roster built from team.toml now, then TEAM.md, then the names."""
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
    if cfg.body:  # TEAM.md without its own title and its notes to whoever edits it
        out += ["", re.sub(r"\A# .*\n+", "", re.sub(r"<!--.*?-->\n?", "", cfg.body, flags=re.S)).strip()]
    if trees:
        out += ["", "## Worktrees", "",
                f"To review a teammate's branch: `git diff {cfg.main_branch}...<branch>` shows its commits, from any"
                " checkout, and `git -C <its path> status` shows what it has not committed yet. When you ask for a"
                " review, name your branch."]
    names = ", ".join("`" + m["name"] + "`" for m in members)
    messaging = " Message anyone with `herdr agent prompt <name> '[from @you] ...'`; your standing instructions say how."
    if message_route(cfg) == "inbox":
        messaging = (" Message a teammate with `herdr agent prompt <name> '[from @you] ...'` and "
                     f"`{operator}` with the inbox command in your standing instructions; they say how.")
    out += ["", "## Talking to each other", "",
            f"Everyone has an @name, which is also their herdr pane's name: {names}, and `{operator}` for the operator."
            + messaging,
            "", closing or "That is the whole team brief. No reply needed."]
    return "\n".join(out) + "\n"


def message_for(cfg, m, members, operator, closing=None):
    """What a member is sent: its standing instructions when its harness cannot hold them as a system prompt
    (or when asked), then the team brief."""
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
        return {"text": p.stdout}  # `pane read` prints the pane's text


def require_herdr():
    if os.environ.get("HERDR_ENV") != "1":
        raise SystemExit("not inside herdr (HERDR_ENV != 1). Start herdr and run this from the operator's pane.")


class Team:
    """The running team as herdr sees it: one team per herdr session. A member's pane carries the member's
    @name as its label, and herdr knows the pane, the agent in it (if any) and its harness session."""

    def __init__(self, cfg):
        self.cfg = cfg
        # `pane list` has labels and sessions but no agent names; `agent list` has the names.
        self.names = {a.get("name") for a in herdr("agent", "list").get("agents", [])}
        self.panes = {}
        for p in herdr("pane", "list").get("panes", []):
            label = p.get("label")
            if label and (label not in self.panes or p.get("agent")):  # a label can repeat: the live pane wins
                self.panes[label] = p
        remember_sessions(cfg, self)

    def pane(self, name):
        return self.panes.get(name)

    def live(self, name):
        """An agent is running in the member's labeled pane."""
        p = self.panes.get(name)
        return bool(p and p.get("agent"))

    def members(self):
        """Member plans for the members that are running, in team.toml order."""
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
        raise SystemExit(f"herdr sees no agent in this pane ({pane}). Run ateam.py from the operator's pane.")
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
    add = ["worktree", "add", str(path), m["branch"]] if known else ["worktree", "add", "-b", m["branch"], str(path)]
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
    prompt_file = cfg.cache / f"{m['name']}.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(member_prompt(m, operator, cfg))  # written fresh at each launch
    if message_route(cfg) == "inbox":
        m = {**m, "inbox_dir": str(cfg.cache)}
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
        screen = herdr("pane", "read", pane, "--source", "visible", check=False).get("text", "")
        asked = [d for d in TABLE[m["harness"]].get("dialogs", []) if d[0] in screen]
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


def deliver(team, texts, what):
    """Send {member: text}. herdr refuses a prompt to an agent that is waiting on an approval, so say who
    did not get it. -> {member: reason}.
    A harness may complete a last word that starts with @ or $ and swallow Enter; a final newline prevents it."""
    failed = {}
    for name, text in texts.items():
        err = herdr("agent", "prompt", name, text if text.endswith("\n") else text + "\n", check=False).get("error")
        if err:
            failed[name] = err
    ok = [n for n in texts if n not in failed]
    if ok:
        print(f"sent {what} to {', '.join('@' + n for n in ok)}")
    for name, err in failed.items():
        print(f"NOT delivered to @{name}: {err}. Read its pane (herdr agent read {name}"
              " --source visible) and send it again.")
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
                  f" ateam.py brief --send {name}")
        else:
            print(f"@{name} did not come up: {err}. Read the pane, fix the cause, then run: ateam.py up {name}")
    if undelivered:
        print(f"The team brief did not reach {', '.join('@' + n for n in undelivered)}. Once they can take a message,"
              f" run: ateam.py brief --send {' '.join(undelivered)}")
    if failed or undelivered:
        sys.exit(3)


# ================================================================ sessions seen (for continue)
# herdr drops a pane's session reference when its agent exits. So every command that looks at the team notes
# each agent's current session in one small file in the cache, and `continue` resumes a member's last one.
# The file is only ever appended to. Without it, a member that exited starts fresh.

def past_sessions(cfg):
    """{member: its latest noted session ref} from the per-repo cache."""
    path = cfg.cache / "sessions.jsonl"
    return {o["member"]: o for o in (records(path) if path.exists() else ()) if o.get("member") and o.get("value")}


def remember_sessions(cfg, team):
    """Note each agent's current harness session, unless it is the one noted last."""
    last, new = past_sessions(cfg), []
    for name in [cfg.operator["name"], *cfg.members]:
        ref = (team.pane(name) or {}).get("agent_session") or {}
        if ref.get("value") and ref["value"] != last.get(name, {}).get("value"):
            new.append({"member": name, **{k: ref.get(k) for k in ("agent", "kind", "value")}})
    if new:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        with (cfg.cache / "sessions.jsonl").open("a") as f:
            f.writelines(json.dumps(o) + "\n" for o in new)


# ================================================================ commands

def cfg_of(args, require=True):
    return Config(pathlib.Path(args.cwd or os.getcwd()), require)


def selected(cfg, names):
    """Plans for the named members, or for every member in team.toml."""
    names = [n.lstrip("@") for n in names] or list(cfg.members)
    strangers = [n for n in names if n not in cfg.members]
    if strangers:
        raise SystemExit(f"{', '.join(strangers)}: not a member in {CONF} (members: {', '.join(cfg.members)})."
                         f" To add a member, add its table to {CONF}.")
    return [plan(cfg, n) for n in names]


def print_plan(plans, extra_col=None):
    rows = [("Member", "Role", "Harness", "Model", "Thinking", "Permissions") + ((extra_col[0],) if extra_col else ())]
    for p in plans:
        rows.append(("@" + p["name"], p["role"], p["harness"], p["model"] or "?", p["thinking"], p["permissions"])
                    + ((extra_col[1](p),) if extra_col else ()))
    print_table(rows)


def cmd_init(args):
    """Write what the project lacks: TEAM.md and team.toml.sample (shared), and this person's own team.toml, copied
    from the project's sample. team.toml goes in .gitignore. Nothing that exists is replaced."""
    root = find_root(pathlib.Path(args.cwd or os.getcwd()))

    def git(*a):  # -> exit status, or None without git
        try:
            return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
        except OSError:
            return None
    top = git("rev-parse", "--show-toplevel")
    if top and top.stdout.strip() and not any((root / n).exists() for n in (CONF, BRIEF, LEGACY)):
        root = pathlib.Path(top.stdout.strip())  # the repo root, not a subdirectory
    refuse_legacy(root)
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
    if not (rules.returncode == 0 if rules and rules.returncode in (0, 1) else CONF in lines):  # a later `!team.toml` undoes a line
        ignore.write_text("\n".join(lines + [CONF]) + "\n")
        print(f"added {CONF} to {ignore}: it is yours; {SAMPLE} is what the project shares")
    tracked = git("ls-files", "--error-unmatch", CONF)
    if tracked and tracked.returncode == 0:
        print(f"note: git already tracks {CONF}, and .gitignore does not change that. `git rm --cached {CONF}` stops it.")
    print(f"Edit {BRIEF} (project, workflow, where tasks are tracked) and {CONF} (your members), then run `ateam.py check`.")


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
    ready = checked(cfg, plans)  # every member must pass first
    require_herdr()
    operator = ensure_operator(cfg, tab=True)
    team = Team(cfg)
    running = [m["name"] for m in ready if team.live(m["name"])]
    if running:  # so `up` can be run again after a partial launch
        print(f"already running: {', '.join(running)}")
    todo = [m for m in ready if m["name"] not in running]
    taken = [m["name"] for m in todo if m["name"] in team.names]
    if taken:
        raise SystemExit(f"an agent outside this team is already named {', '.join(taken)}. One team runs per herdr"
                         " session: rename that agent, or use another session.")
    configure_messages(cfg, team, operator)
    started, failed = [], {}
    for m in todo:
        err = launch(cfg, team, m, operator, args.timeout)
        if err:
            failed[m["name"]] = err
        else:
            started.append(m)
    team, undelivered = Team(cfg), {}
    if started and args.no_brief:
        print("next: ateam.py brief --send")
    elif started:
        new = {m["name"] for m in started}
        undelivered = send_brief(cfg, team, operator, names=new)
        others = [m["name"] for m in team.members() if m["name"] not in new and m["name"] not in failed]
        if others:  # the rest of the team hears who joined, without the whole brief again
            joined = "; ".join(f"@{m['name']} ({m['role']}, {TABLE[m['harness']]['label']} {m['model']})" for m in started)
            deliver(team, {n: f"{tag(operator)} Joined the team: {joined}. No reply needed." for n in others}, "the news")
    report_failed(failed, team, undelivered)


def cmd_continue(args):
    """Bring members back after their agents exited, resuming their sessions; with --restart, running ones too."""
    require_herdr()
    cfg = cfg_of(args)
    operator = ensure_operator(cfg)
    team = Team(cfg)
    names = [n.lstrip("@") for n in args.names] or [n for n in cfg.members if team.pane(n)]
    if not names:
        names = list(cfg.members)
        if not names:
            raise SystemExit(f"no members in {CONF}; add one, then run `ateam.py up`")
        print(f"no member panes in this herdr session: continuing every member in {CONF}")
        if os.environ.get("HERDR_TAB_ID"):
            herdr("tab", "rename", os.environ["HERDR_TAB_ID"], TABS["operator"], check=False)
    elif not args.names:
        missing = [n for n in cfg.members if n not in names]
        if missing:
            print(f"no pane, not continued: {', '.join('@' + n for n in missing)}."
                  f" To bring them back: ateam.py continue {' '.join(missing)}")
    unknown = [n for n in names if n not in cfg.members]
    if unknown:
        raise SystemExit(f"not in {CONF}: {', '.join(unknown)}")
    restart = [n for n in names if args.restart and team.live(n)]
    ready = checked(cfg, [plan(cfg, n) for n in names if n in restart or not team.live(n)])  # with team.toml's settings as they are now
    configure_messages(cfg, team, operator, restart=names if args.restart else ())
    failed, fresh = {}, []
    past = past_sessions(cfg)
    for m in ready:
        ref = (team.pane(m["name"]) or {}).get("agent_session") or past.get(m["name"])
        resume = ref if ref and ref.get("agent") == m["harness"] and not args.fresh else None
        if m["name"] in restart:  # closing its pane ends the agent; it starts again, from its session, in a new one
            herdr("pane", "close", team.panes.pop(m["name"])["pane_id"])
        elif not team.pane(m["name"]):
            print(f"@{m['name']} has no pane any more; it gets a new one")
        err = launch(cfg, team, m, operator, args.timeout, resume=resume)
        if err:
            failed[m["name"]] = err + (f". If its session cannot be resumed, run: ateam.py continue {m['name']} --fresh"
                                       if resume else "")
        elif not resume:
            fresh.append(m["name"])
    if not ready:
        print("every member is running")
    team = Team(cfg)
    pickup_text = (f"We are picking up where we left off. Re-read `{BRIEF}`, check the tracker it names"
                   f", then tell @{operator} in one line what you were doing and what you will do next. Send it:"
                   f" `herdr agent prompt {operator} '[from @you] ...'`. A reply in your own pane does not reach @{operator}.")
    if message_route(cfg) == "inbox":
        pickup_text = pickup_text.replace(f"Send it: `herdr agent prompt {operator} '[from @you] ...'`.",
                                          "Send it with the inbox command in your standing instructions.")
    fresh_text = (f"You are starting in a new session and know nothing of the work so far. Re-read `{BRIEF}`"
                  f" and do not start anything yet: @{operator} will send your task, its criteria and the evidence"
                  " so far. No reply needed.")
    closing = {name: fresh_text if name in fresh else pickup_text for name in cfg.members}
    # With names, only those members are re-briefed. With none, every running member is re-briefed.
    undelivered = send_brief(cfg, team, operator, names=set(names) - set(failed) if args.names else None,
                            closing=closing)
    if fresh:
        print(f"fresh sessions, no earlier context: {', '.join('@' + n for n in fresh)}."
              " Send each its task, criteria and the evidence so far.")
    report_failed(failed, team, undelivered)


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
    try:
        with Inbox(cfg) as inbox:
            suffix = f"; {len(inbox.unread)} unread (ateam.py wait prints them)" if inbox.unread else ""
            print(f"operator messages: {inbox.state['mode']}{suffix}")
    except (OSError, ValueError) as e:
        print(f"inbox unavailable: {e}" if isinstance(e, OSError) else str(e))
    for state, hint in (("blocked", "blocked: waiting on an approval or a question. Read the pane and ask the user."),
                        ("gone", "gone: `ateam.py continue` brings them back in their panes and sessions.")):
        hit = [r[0] for r in rows[2:] if state in r[5]]
        if hit:
            print(f"\n{', '.join(hit)} {hint}")
    if not any(team.pane(n) for n in cfg.members):
        print(f"\nNo member of {CONF} has a pane in this herdr session. A team that ran before: `ateam.py continue`."
              " A new team: `ateam.py check`, then `ateam.py up`.")


def cmd_wait(args):
    """Block until the operator is needed. Run it in the background after handing out work. It returns when a
    member that was working stops (its report may not have been sent), when a member is blocked, gone or
    closed, when herdr cannot tell what a member is doing, or when nobody is working any more. With names,
    only those members' lifecycle changes count. Inbox messages from any member always end the wait."""
    require_herdr()
    cfg = cfg_of(args)
    watch = [n.lstrip("@") for n in args.names]
    deadline, seen, worked, looks = time.monotonic() + args.timeout, None, set(), 0
    warned = False
    while True:
        try:
            with Inbox(cfg) as inbox:
                if args.seen is not None:
                    inbox.acknowledge(args.seen)
                    args.seen = None
                unread = inbox.unread
            if unread:
                for row in unread:
                    print(f"#{row['id']} from @{row['from']} at {row['at']}")
                    print("\n".join("    " + line for line in row["text"].splitlines()))
                print(f"{len(unread)} unread. After you have acted on or recorded them:"
                      f" ateam.py wait --seen {unread[-1]['id']}")
                return
        except (OSError, ValueError) as e:
            if args.seen is not None:
                print(e, file=sys.stderr)
                raise SystemExit(2)
            if not warned:
                print(f"inbox unavailable: {e}" if isinstance(e, OSError) else str(e), flush=True)
                warned = True
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
        stopped = [n for n in settled if n in worked or (n in watch and looks > 1)]
        if closed or needs or stopped or (looks > 1 and (unclear or not working)):
            break
        if time.monotonic() >= deadline:
            print(f"timeout after {args.timeout:g}s; still working: {', '.join('@' + n for n in working)}")
            sys.exit(4)
        worked.update(working)
        time.sleep(args.interval)
    for n in closed:
        print(f"@{n}'s pane was closed, so it is no longer running. `ateam.py continue {n}` gives it a new pane.")
    for n in needs:
        if state[n] == "gone":
            print(f"@{n} is gone: its agent exited. `ateam.py continue {n}` brings it back.")
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
    if not (closed or needs or unclear or stopped):
        print(f"nobody is working: {', '.join('@' + n + ' ' + s for n, s in state.items())}")


def cmd_brief(args):
    cfg = cfg_of(args)
    names = [n.lstrip("@") for n in args.names]
    if args.send:
        require_herdr()
        operator = ensure_operator(cfg)
        team = Team(cfg)
        missing = [n for n in names if not team.live(n)]
        if missing:
            raise SystemExit(f"not running: {', '.join(missing)}")
        if send_brief(cfg, team, operator, names=set(names) or None):
            sys.exit(1)
        return
    print(render_team_brief(cfg, [plan(cfg, n) for n in cfg.members], cfg.operator["name"]))


def cmd_say(args):
    """Message members as the operator: `say all ...` or `say lead-dev frontend-dev ...`."""
    require_herdr()
    cfg = cfg_of(args)
    operator = ensure_operator(cfg)
    team = Team(cfg)
    text = pathlib.Path(args.file).read_text() if args.file else args.message
    if not text:
        raise SystemExit("give a message or --file")
    names = [m["name"] for m in team.members()] if args.to == ["all"] else [n.lstrip("@") for n in args.to]
    missing = [n for n in names if not team.live(n)]
    if missing or not names:
        raise SystemExit(f"not running: {', '.join(missing) or 'any member'}")
    if deliver(team, {n: f"{tag(operator)} {text}" for n in names}, "your message"):
        sys.exit(1)


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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--cwd", help="where to look for TEAM.md and team.toml (default: here, then parent directories)")

    def launching(sp):
        sp.add_argument("--timeout", type=int, default=60000, help="ms to wait for each agent to be ready")
        common(sp)

    def selection(sp):
        sp.add_argument("names", nargs="*", help="member names (default: every member in team.toml)")

    it = sub.add_parser("init", help="write the TEAM.md, team.toml.sample and team.toml a project lacks")
    common(it)

    common(sub.add_parser("harnesses", help="installed harnesses and which recommended models they can use"))

    ck = sub.add_parser("check", help="verify members can launch; launches nothing")
    selection(ck)
    common(ck)

    up = sub.add_parser("up", help="check, launch each member in a pane of the team tab, send the team brief",
                        epilog="Native harness flags go in the member's `args` in team.toml.")
    selection(up)
    up.add_argument("--no-brief", action="store_true", help="do not send the team brief afterwards")
    launching(up)

    co = sub.add_parser("continue", help="bring members back in their panes and sessions, and re-brief them")
    co.add_argument("names", nargs="*", help="only these members")
    co.add_argument("--fresh", action="store_true", help="start new sessions instead of resuming")
    co.add_argument("--restart", action="store_true",
                    help="also restart members that are running, so they get team.toml's settings (after a herdr restart)")
    launching(co)

    st = sub.add_parser("status", help="members and whether they are running")
    common(st)

    wt = sub.add_parser("wait", help="return when a member stops, is blocked or is gone, or nobody is working")
    wt.add_argument("names", nargs="*", help="watch only these members (default: every member)")
    wt.add_argument("--timeout", type=float, default=1800, help="seconds before giving up (exit 4)")
    wt.add_argument("--interval", type=float, default=5, help="seconds between looks")
    wt.add_argument("--seen", type=int, help="acknowledge and delete inbox messages through this id, then wait")
    common(wt)

    ib = sub.add_parser("inbox", help="send messages to an inbox operator without typing into its pane")
    send = ib.add_subparsers(dest="inbox_cmd", required=True).add_parser("send", help="queue a message")
    send.add_argument("--from", dest="sender", required=True, help="your member name")
    send.add_argument("message", nargs="?")
    send.add_argument("--file", help="send this file's contents")
    common(send)

    bf = sub.add_parser("brief", help="print the team brief as members get it, or --send it")
    bf.add_argument("names", nargs="*", help="with --send: only these members")
    bf.add_argument("--send", action="store_true")
    common(bf)

    sy = sub.add_parser("say", help="message members as the operator")
    sy.add_argument("to", nargs="+", help="member names, or `all`; the last word is the message unless --file")
    sy.add_argument("--file", help="send this file's contents")
    common(sy)

    pr = sub.add_parser("prompt", help="the standing instructions a member gets")
    pr.add_argument("name")
    common(pr)

    args = ap.parse_args(argv)
    if args.cmd == "say":
        args.message = None if args.file else (args.to.pop() if len(args.to) > 1 else None)
    {"init": cmd_init, "harnesses": cmd_harnesses, "check": cmd_check, "up": cmd_up,
     "continue": cmd_continue, "status": cmd_status, "wait": cmd_wait, "brief": cmd_brief, "say": cmd_say,
     "prompt": cmd_prompt, "inbox": cmd_inbox_send}[args.cmd](args)


if __name__ == "__main__":
    main()
