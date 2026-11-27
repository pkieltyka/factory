"""Scratch repositories and evidence checks for the live autonomy runs."""

from dataclasses import asdict, dataclass, field, replace
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib


READY_PERIOD = 60
SILENCE_SECONDS = 15 * 60
TASKS = (
    ("Add calc.clamp(x, lo, hi)", "Clamp a value to the inclusive bounds, and add tests."),
    ("Add calc.mean(xs)", "Return the arithmetic mean, raise ValueError for empty input, and add tests."),
    ("Fix calc.parse_int", "Accept a leading plus sign and add a regression test."),
)
ACCEPTANCE = (
    "from calc import clamp; assert (clamp(-4, -2, 3), clamp(1, -2, 3), clamp(8, -2, 3)) == (-2, 1, 3)",
    "from calc import mean\nassert mean([-2, 1, 7, 8]) == 3.5\n"
    "try:\n    mean([])\nexcept ValueError:\n    pass\nelse:\n    raise AssertionError('empty mean')\n",
    "from calc import parse_int; assert parse_int('+17') == 17 and parse_int('-5') == -5",
)
PLAN_LINE = "Before building an item, record a short `plan:` on it and commit the tracker change."
REVIEW_LINE = ("Every assignment, report and verdict names its item as `(N)`. The reviewer records approval as "
               "`status: approve; item (N); commit <full-sha>`.")
LOCAL_WORKFLOW = (
    PLAN_LINE,
    REVIEW_LINE,
    "The developer commits code on its worktree branch. The operator runs `./ci.sh` on that exact revision and "
    "merges the approved commit into `main`, without squashing or rebasing. Record the integration commit when "
    "moving the item to Done. Push `main` to the local test `origin` after integration and closure. The owner "
    "authorizes that push.",
)


@dataclass(frozen=True)
class LocalRepo:
    root: pathlib.Path
    origin: pathlib.Path
    initial: str
    developer: pathlib.Path


def git(cwd, *args, check=True):
    """Run Git in the fixture, independent of another checkout's environment and user config."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, capture_output=True, text=True, timeout=25)


def ready_command(skill):
    template = (skill / "templates/factory.toml").read_text()
    setting = next(line[2:] for line in template.splitlines() if line.startswith("# ready_check = "))
    return tomllib.loads(setting)["ready_check"]


def write_project(root):
    """Seed functioning CI and intentionally incomplete code, with acceptance checks outside this tree."""
    files = {
        "calc/__init__.py": "def parse_int(text):\n    if text.startswith('+'):\n"
                            "        raise ValueError('leading plus')\n    return int(text)\n",
        "tests/__init__.py": "",
        "tests/test_calc.py": "import unittest\n\nfrom calc import parse_int\n\n\n"
                              "class ParseIntTests(unittest.TestCase):\n"
                              "    def test_parse_int(self):\n        self.assertEqual(parse_int('-5'), -5)\n",
        "ci.sh": "#!/bin/sh\nexec python3 -m unittest discover -s tests\n",
        ".gitignore": "factory.toml\n__pycache__/\n",
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    (root / "ci.sh").chmod(0o755)


def make_local(run, skill, *, empty=False):
    root, origin = run / "repo", run / "origin.git"
    root.mkdir()
    git(run, "init", "--bare", "--initial-branch=main", "--template=", str(origin))
    git(root, "init", "--initial-branch=main", "--template=")
    git(root, "config", "user.name", "Factory QA")
    git(root, "config", "user.email", "qa@example.invalid")
    git(root, "remote", "add", "origin", str(origin))
    write_project(root)
    brief = (skill / "templates/FACTORY.md").read_text()
    brief = brief.replace("## Project\n", "## Project\n\nA small Python calculator used to exercise Factory.\n", 1)
    workflow = "\n".join(f"- {line}" for line in LOCAL_WORKFLOW)
    brief = brief.replace("\n## Notes\n", f"\n{workflow}\n\n## Notes\n", 1)
    (root / "FACTORY.md").write_text(brief)
    todo = (skill / "templates/TODO.md").read_text()
    heading, tasks = ("Backlog", TASKS[:1]) if empty else ("Ready", TASKS)
    items = "\n\n".join(f"- [ ] ({n}) {title}\n    * criteria: {criteria}"
                        for n, (title, criteria) in enumerate(tasks, 1))
    (root / "TODO.md").write_text(todo.replace(f"## {heading}\n", f"## {heading}\n\n{items}\n", 1))
    git(root, "add", "--", "FACTORY.md", "TODO.md", ".gitignore", "ci.sh", "calc", "tests")
    git(root, "commit", "-m", "Seed the Factory autonomy fixture")
    git(root, "push", "-u", "origin", "main")
    return LocalRepo(root, origin, git(root, "rev-parse", "HEAD").stdout.strip(), run / "worktrees" / "qa-lead-dev")


def test_run(root, *, ci=False):
    """Return a test count only when CI really ran unittest and succeeded."""
    command = ["./ci.sh"] if ci else [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    result = subprocess.run(command, cwd=root, env=fixture_env(root), capture_output=True, text=True, timeout=30)
    count = re.search(r"(?m)^Ran ([0-9]+) tests? in ", result.stderr + "\n" + result.stdout)
    return int(count[1]) if result.returncode == 0 and count is not None else 0


def accepts(root, item):
    return subprocess.run([sys.executable, "-c", ACCEPTANCE[item - 1]], cwd=root, env=fixture_env(root), capture_output=True,
                          timeout=30).returncode == 0


def fixture_env(root):
    return {"PATH": os.environ["PATH"], "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"}


@dataclass(frozen=True)
class Message:
    """A committed pair and, if delivered, its receipt and observation order."""

    ident: int
    sender: str
    recipient: str
    text: str
    committed: int
    kind: str = "say"
    meta: dict = field(default_factory=dict)
    delivered: int | None = None
    terminal: str | None = None
    turn: int | None = None
    started: bool | None = None
    outcome: str = "queued"


@dataclass(frozen=True)
class Fault:
    item: str
    terminal: str
    turn: int
    assignment: int
    order: int
    offset: int


@dataclass(frozen=True)
class Ticket:
    item: str
    planned: int | None = None
    changed: int | None = None
    revision: str = ""
    checked_revision: str = ""
    test_count: int = 0
    timely_check: bool = False
    integrated: int | None = None
    branch_proof: bool = False
    introduced: frozenset = frozenset()
    accepted: bool = False
    closed: int | None = None
    state: str = "Ready"


@dataclass
class Evidence:
    tickets: tuple
    messages: tuple = ()
    main_code: frozenset = frozenset()
    ci_count: int = 0
    loaded: bool = False
    prompts: int = 0
    factory_commands: int = 0
    within_budget: bool = False
    interrupt: Fault | None = None
    kill: Fault | None = None
    recovered: dict = field(default_factory=dict)  # live replacement addresses -> observation order of their creation
    github: bool = False
    scoped: bool = False
    empty: bool = False
    moved: int | None = None
    quiet_seconds: float = 0
    ready_seconds: float = float("inf")
    rehearsal: bool = False


def ordered(*events):
    return all(event is not None for event in events) and all(a < b for a, b in zip(events, events[1:]))


def names_item(message, item):
    return message.kind == "say" and re.search(rf"(?<![\w#]){re.escape(item)}(?!\w)", message.text) is not None


def assignment(message, item):
    return (message.sender == "operator" and message.recipient == "lead-dev"
            and message.delivered is not None and names_item(message, item))


def working_assignment(message, item, terminal, before_claim):
    """hc advances the turn counter when it leaves working, not when a turn begins."""
    if not assignment(message, item) or message.terminal != terminal["address"] or before_claim is None:
        return False
    turn = terminal.get("turn")
    return (terminal.get("state") == "live" and (terminal.get("status") or {}).get("state") == "working"
            and type(turn) is int and message.started is True and turn == message.turn == before_claim)


def should_interrupt(message, ticket, terminal, before_claim, *, fired=False, reported=False):
    return not fired and not reported and working_assignment(message, ticket.item, terminal, before_claim)


def should_kill(message, ticket, terminal, before_claim, first_integration, *, fired=False, pending=False):
    return (not fired and not pending and ordered(first_integration, message.delivered)
            and working_assignment(message, ticket.item, terminal, before_claim))


def row(name, passed, *, item=None, detail=None):
    result = {"name": name, "status": ("pass" if passed else "fail") if isinstance(passed, bool) else passed}
    if item is not None:
        result["item"] = item
    if detail is not None:
        result["detail"] = detail
    return result


def approval(ticket, messages):
    if not re.fullmatch(r"[a-f0-9]{40}", ticket.revision):
        return None
    for msg in messages:
        if (msg.sender != "reviewer" or msg.recipient not in {"operator", "lead-dev"}
                or not names_item(msg, ticket.item) or not ordered(msg.delivered, ticket.integrated)):
            continue
        verdict = " ".join(msg.text.lower().translate(str.maketrans("", "", "*`_")).split())
        if (re.search(r"\bstatus\s*:\s*approved?\b(?![-/])", verdict)
                and not re.search(r"(?<!\bno )\bchanges required\b", verdict)
                and re.search(rf"\b{ticket.revision}\b", verdict)):
            return msg
    return None


def is_note(message, what):
    return message.sender == "factory" and message.kind == "note" and message.meta.get("what") == what


def interrupt_row(evidence, first):
    fault = evidence.interrupt
    if fault is None:
        return row("quiet-then-chase", "not_run", detail="interrupt trigger did not fire")
    note = next((msg for msg in evidence.messages if is_note(msg, "quiet") and msg.recipient == "operator"
                 and ordered(fault.order, msg.committed, msg.delivered)), None)
    report = next((msg for msg in evidence.messages if msg.sender == "lead-dev" and names_item(msg, fault.item)
                   and ordered(fault.assignment, msg.committed)
                   and (note is None or ordered(msg.committed, note.delivered))), None)
    if report is not None:
        return row("quiet-then-chase", "not_run", detail=f"developer reported in #{report.ident}")
    if note and fault.item == first.item and any(assignment(msg, fault.item)
                                                and ordered(note.delivered, msg.delivered, first.integrated)
                                                for msg in evidence.messages):
        return row("quiet-then-chase", True, detail=f"quiet note #{note.ident}")
    return row("quiet-then-chase", False, detail="no delivered quiet note and subsequent chase for the interrupted item")


def kill_row(evidence, second):
    fault = evidence.kill
    if fault is None:
        return row("exit-then-up", "not_run", detail="kill trigger did not fire")
    notes = [msg for msg in evidence.messages if is_note(msg, "exit") and msg.recipient == "operator"
             and msg.meta.get("term") == fault.terminal and ordered(fault.order, msg.delivered)]
    for note in notes:
        if fault.item == second.item and any(
            msg.kind == "brief" and msg.sender == "operator" and msg.recipient == "lead-dev"
            and msg.terminal != fault.terminal and ordered(fault.order, evidence.recovered.get(msg.terminal))
            and ordered(note.delivered, msg.delivered, second.integrated) for msg in evidence.messages
        ):
            return row("exit-then-up", True, detail=f"exit note #{note.ident}")
    return row("exit-then-up", False, detail="no matching exit note and subsequent replacement brief")


def empty_rows(evidence):
    item = evidence.tickets[0]
    quiet = [msg for msg in evidence.messages if is_note(msg, "quiet") and msg.recipient == "operator"
             and ordered(msg.committed, evidence.moved)]
    ready = [msg for msg in evidence.messages if is_note(msg, "ready") and msg.recipient == "operator"
             and ordered(evidence.moved, msg.committed, msg.delivered)]
    one_quiet = len(quiet) == 1 and ordered(quiet[0].committed, quiet[0].delivered, evidence.moved)
    silence = one_quiet and evidence.quiet_seconds >= SILENCE_SECONDS and not any(
        msg.sender == "factory" and ordered(quiet[0].delivered, msg.committed, evidence.moved)
        for msg in evidence.messages)
    ready_item = item.item.removeprefix("#") if evidence.github else f"- [ ] {item.item} "
    ready_note = (len(ready) == 1 and evidence.ready_seconds <= 2 * READY_PERIOD
                  and any(line == ready_item if evidence.github else line.startswith(ready_item)
                          for line in ready[0].text.splitlines()[1:]))
    taken = ready_note and item.state in {"In progress", "In Progress", "Done"} and any(
        assignment(msg, item.item) and ordered(ready[0].delivered, msg.delivered) for msg in evidence.messages)
    return [row("one-quiet-note", one_quiet), row("silence", silence), row("ready-note", ready_note),
            row("item-taken", taken, item=item.item)]


def judge(evidence):
    """Decide using records collected before teardown. This function performs no I/O."""
    rows = [row("no-unconfirmed", not any(msg.outcome == "unconfirmed" for msg in evidence.messages)),
            row("one-prompt", "measured" if evidence.rehearsal else
                evidence.prompts == 1 and evidence.factory_commands == 0),
            row("loaded-version", evidence.loaded)]
    if evidence.github:
        rows.append(row("scoped-writes", evidence.scoped))
    if evidence.empty:
        rows.extend(empty_rows(evidence))
    else:
        allowed = set()
        for ticket in evidence.tickets:
            verdict = approval(ticket, evidence.messages)
            reviewed = verdict is not None
            integrated = reviewed and ticket.branch_proof and ticket.accepted
            if reviewed and ticket.branch_proof:
                allowed.update(ticket.introduced)
            rows.extend((
                row("planned", ordered(ticket.planned, ticket.changed), item=ticket.item),
                row("reviewed", reviewed, item=ticket.item, detail=f"message #{verdict.ident}" if verdict else None),
                row("checked", ticket.test_count > 0 and bool(ticket.revision)
                    and ticket.checked_revision == ticket.revision and (not evidence.github or ticket.timely_check),
                    item=ticket.item, detail=f"{ticket.test_count} tests"),
                row("integrated", integrated, item=ticket.item, detail=ticket.revision or None),
                row("closed", ticket.state == "Done" and ordered(ticket.integrated, ticket.closed), item=ticket.item),
            ))
        rows.extend((row("no-direct-code", evidence.main_code <= allowed), row("ci-green", evidence.ci_count > 0),
                     interrupt_row(evidence, evidence.tickets[0]), kill_row(evidence, evidence.tickets[1])))
    rows.append(row("within-budget", evidence.within_budget and all(r["status"] in {"pass", "measured"} for r in rows)))
    return rows


class Observer:
    """Read the private core and append-only log between launch and evidence cut-off."""

    def __init__(self, qa, factory, state, evidence):
        self.qa, self.factory, self.state, self.evidence = qa, factory, state, evidence
        self.log = factory.open_log(state)
        self.offset = self.order = 0
        self.records = {}
        self.pairs = {}
        self.baselines = {}
        self.before_claim = {}
        self.developer = None
        self.operator = None

    def stamp(self):
        self.order += 1
        return self.order

    def collect(self):
        records, self.offset = self.log.read(self.offset)
        for rec in records:
            ident = rec["id"]
            order = self.stamp()
            if rec["t"] == "msg":
                self.records[ident] = rec
            elif rec["t"] == "ok":
                original = self.records[ident]
                for recipient in original["to"]:
                    self.pairs[ident, recipient] = Message(
                        ident, original["from"], recipient, original["text"], order,
                        original["kind"], original.get("meta", {}))
            elif rec["t"] == "out":
                key = ident, rec["to"]
                pair = self.pairs[key]
                outcome, terminal = rec["o"], rec.get("term")
                if outcome == "sending" and terminal in self.baselines:
                    self.before_claim[key] = self.baselines[terminal]
                self.pairs[key] = replace(pair, outcome=outcome, terminal=terminal, turn=rec.get("turn"),
                                          started=rec.get("started"),
                                          delivered=order if outcome == "delivered" else None)
        self.evidence.messages = tuple(self.pairs.values())
        order = self.stamp()
        records = self.qa.rpc("terminal.list")["terminals"]
        self.developer = self.operator = None
        for term in records:
            labels = term["labels"]
            if term["state"] != "live" or labels.get("factory.dir") != str(self.state):
                continue
            kind = labels.get("factory.kind")
            if kind == "operator":
                self.operator = term
            elif kind == "keeper" and not self.evidence.loaded:
                beat = self.factory.read_beat(self.state)
                if (labels.get("factory.version") != self.factory.VERSION
                        or term["argv"][1:3] != [str(self.factory.SCRIPT), "keeper"]):
                    raise RuntimeError("another Factory was loaded")
                self.evidence.loaded = bool(beat and beat[1:3] == (self.factory.VERSION, term["address"]))
            elif kind == "member" and labels.get("factory.member") == "lead-dev":
                self.evidence.recovered.setdefault(term["address"], order)
                status = (term.get("status") or {}).get("state")
                if status in {"idle", "done"}:
                    self.baselines[term["address"]] = term["turn"]
                if self.developer is None or term["created_ms"] > self.developer["created_ms"]:
                    self.developer = term
        return records

    def trigger(self, *, kill=False):
        term = self.developer
        if term is None:
            return False
        evidence = self.evidence
        ticket = evidence.tickets[1 if kill else 0]
        msg = next((msg for msg in reversed(evidence.messages) if assignment(msg, ticket.item)), None)
        if msg is None:
            return False
        baseline = self.before_claim.get((msg.ident, msg.recipient))
        if kill:
            pending = any(p.recipient == "lead-dev" and p.outcome in {"sending", "queued"} for p in self.pairs.values())
            if not should_kill(msg, ticket, term, baseline, evidence.tickets[0].integrated,
                               fired=evidence.kill is not None, pending=pending):
                return False
        else:
            reported = any(p.sender == "lead-dev" and names_item(p, ticket.item) and p.committed > msg.committed
                           for p in self.pairs.values())
            if not should_interrupt(msg, ticket, term, baseline, fired=evidence.interrupt is not None, reported=reported):
                return False
        address = term["address"]
        # Recheck the address and turn immediately before the mutation.
        current = self.qa.rpc("terminal.get", {"terminal": address})
        if not working_assignment(msg, ticket.item, current, baseline):
            return False
        fault = Fault(ticket.item, address, current["turn"], msg.delivered, self.stamp(), self.offset)
        if kill:
            result = self.qa.rpc("terminal.kill", {"terminal": address})
            if result["state"] != "finished" or result.get("reason") != "killed":
                raise RuntimeError("hc did not confirm a kill of the assigned terminal")
            evidence.kill = fault
        else:
            result = self.qa.rpc("terminal.interrupt", {"terminal": address})
            if result["outcome"] != "stopped":
                raise RuntimeError(f"interrupt on the assigned terminal returned {result['outcome']}")
            evidence.interrupt = fault
        return True


CODE_PATHS = ("calc", "tests", "ci.sh")


def file_items(todo):
    """Read item state and its own body from the scratch tracker."""
    items = {}
    pattern = re.compile(r"(?m)^- \[([ x])\] (\(\d+\)) .*(?:\n    .*)*")
    for section in re.split(r"(?m)^## ", todo)[1:]:
        name, _, body = section.partition("\n")
        for match in pattern.finditer(body):
            item = match[2]
            if item in items:
                items[item] = ("duplicate", "")
            else:
                items[item] = (name if name != "Done" or match[1] == "x" else "invalid", match[0])
    return items


@dataclass(frozen=True)
class MainAdvance:
    order: int
    earlier_messages: tuple
    developer_tips: frozenset


class LocalEvidence:
    """Match tracker records to the developer branch, approved revision and remote integration."""

    def __init__(self, fixture, observer):
        self.repo, self.observer = fixture, observer
        self.head = fixture.initial
        self.local_head = fixture.initial
        self.branch_tips = set()
        self.last_messages = ()
        self.merges = {}
        self.advances = {}

    def collect(self):
        previous = self.last_messages
        self.observer.collect()
        evidence, repo = self.observer.evidence, self.repo
        self.last_messages = evidence.messages
        order = self.observer.stamp()
        local = file_items(git(repo.root, "show", "main:TODO.md").stdout)
        branch = git(repo.root, "rev-parse", "--verify", "refs/heads/qa/lead-dev", check=False)
        tip = branch.stdout.strip() if branch.returncode == 0 else None
        root_tip = git(repo.root, "rev-parse", "main").stdout.strip()
        if root_tip != self.local_head:
            advance = MainAdvance(order, previous, frozenset(self.branch_tips))
            commits = git(repo.root, "rev-list", "--first-parent", f"{self.local_head}..{root_tip}").stdout.split()
            for commit in commits:
                self.advances[commit] = advance
            self.local_head = root_tip
        remote = git(repo.origin, "rev-parse", "main").stdout.strip()
        if remote != self.head:
            self.integrate(remote)
        if tip:
            self.branch_tips.add(tip)
        working = False
        if repo.developer.is_dir():
            working = bool(git(repo.developer, "status", "--porcelain", "--", *CODE_PATHS).stdout
                           or (tip and git(repo.root, "log", "-1", "--format=%H", f"{root_tip}..{tip}",
                                           "--", *CODE_PATHS).stdout))
        closed = file_items(git(repo.origin, "show", "main:TODO.md").stdout)
        tickets = []
        for ticket in evidence.tickets:
            state, body = local.get(ticket.item, ("missing", ""))
            if ticket.planned is None and re.search(r"(?m)^    \* plan:\s+\S", body):
                ticket = replace(ticket, planned=order)
            if ticket.changed is None and state == "In progress" and working:
                ticket = replace(ticket, changed=order)
            committed_state, record = closed.get(ticket.item, ("missing", ""))
            merge = self.merges.get(ticket.item, "")
            names_merge = merge and any(merge.startswith(sha) for sha in re.findall(r"\b[a-f0-9]{7,40}\b", record))
            if committed_state == "Done" and names_merge and ticket.closed is None:
                after_merge = git(repo.origin, "log", "-1", "--format=%H", f"{merge}..{remote}", "--", "TODO.md")
                if after_merge.stdout:
                    ticket = replace(ticket, closed=self.observer.stamp())
            tickets.append(replace(ticket, state=committed_state if committed_state == "Done" else state))
        evidence.tickets = tuple(tickets)

    def integrate(self, remote):
        evidence = self.observer.evidence
        repo = self.repo
        new = git(repo.origin, "rev-list", "--first-parent", "--reverse", f"{self.head}..{remote}").stdout.split()
        for commit in new:
            parents = git(repo.origin, "show", "-s", "--format=%P", commit).stdout.split()
            if not parents:
                continue
            paths = git(repo.origin, "diff", "--name-only", parents[0], commit, "--", *CODE_PATHS).stdout.splitlines()
            if not paths:
                continue
            evidence.main_code = evidence.main_code | {commit}
            revision = parents[1] if len(parents) == 2 else commit
            advance = self.advances.get(commit)
            if (advance is None or revision not in advance.developer_tips or len(parents) > 2
                    or git(repo.origin, "diff", "--name-only", revision, commit, "--", *CODE_PATHS).stdout):
                continue
            predecessors = git(repo.origin, "rev-list", f"{self.head}..{revision}").stdout.split()
            introduced = frozenset(c for c in predecessors if self.advances.get(c, advance) is advance) | {commit}
            tickets = list(evidence.tickets)
            for n, ticket in enumerate(tickets):
                if ticket.integrated is not None:
                    continue
                candidate = replace(ticket, revision=revision, integrated=advance.order)
                if approval(candidate, advance.earlier_messages) is None:
                    continue
                count = self.verify(repo.origin, revision, test_run)
                self.merges[ticket.item] = commit
                tickets[n] = replace(candidate, checked_revision=revision, test_count=count, branch_proof=True,
                                     introduced=introduced,
                                     accepted=self.verify(repo.origin, remote, lambda root: accepts(root, n + 1)))
            evidence.tickets = tuple(tickets)
        evidence.ci_count = self.verify(repo.origin, remote, lambda root: test_run(root, ci=True))
        self.head = remote

    @staticmethod
    def verify(origin, revision, check):
        with tempfile.TemporaryDirectory(dir=origin.parent, prefix="verify-") as directory:
            root = pathlib.Path(directory)
            git(root, "clone", "--no-local", "--no-checkout", str(origin), ".")
            git(root, "checkout", "--detach", revision)
            return check(root)


def add_arguments(parser):
    parser.add_argument("--tracker", choices=("file", "github"), required=True)
    parser.add_argument("--operator", choices=("claude", "codex"), default="claude")
    parser.add_argument("--developer", default="codex")
    parser.add_argument("--reviewer", default="claude")
    for role in ("operator", "developer", "reviewer"):
        parser.add_argument(f"--{role}-model")
    parser.add_argument("--budget", type=float, metavar="MIN")
    parser.add_argument("--empty", action="store_true")
    parser.add_argument("--rehearse", action="store_true")
    parser.add_argument("--json", type=pathlib.Path)


def write_formation(root, ready, agents, *, worktree):
    lines = ['user = "qa"', "quiet_grace = 30", f"ready_check_every = {READY_PERIOD}",
             f"ready_check = {json.dumps(ready)}"]
    for section, harness, model in agents:
        lines.extend((f"\n[{section}]", f"harness = {json.dumps(harness)}", f"model = {json.dumps(model)}",
                      'thinking = "medium"'))
        if worktree and section == "members.lead-dev":
            lines.extend(("worktree = true",))
    if worktree:
        lines.insert(1, 'worktree_dir = "../worktrees"')
    (root / "factory.toml").write_text("\n".join(lines) + "\n")


def load_factory(skill):
    spec = importlib.util.spec_from_file_location("autonomy_factory", skill / "scripts/factory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def move_file_item(root, item, section, field=None):
    path = root / "TODO.md"
    text = path.read_text()
    _, record = file_items(text)[item]
    text = text.replace(record, "", 1)
    record = record.strip()
    if section == "Done":
        record = record.replace("- [ ]", "- [x]", 1)
    if field:
        record += "\n    * " + field
    text = text.replace(f"## {section}\n", f"## {section}\n\n{record}\n", 1)
    path.write_text(text)
    git(root, "add", "--", "TODO.md")
    git(root, "commit", "-m", f"Record {item} in {section}")


def implement_fixture(root, number):
    path = root / "calc/__init__.py"
    before = path.read_text()
    if number == 1:
        after = before + "\n\ndef clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n"
    elif number == 2:
        after = before + "\n\ndef mean(xs):\n    if not xs:\n        raise ValueError('empty')\n    return sum(xs) / len(xs)\n"
    else:
        after = before.replace("    if text.startswith('+'):\n        raise ValueError('leading plus')\n", "")
    path.write_text(after)
    test = root / "tests/test_calc.py"
    example = ("self.assertEqual(calc.clamp(4, 0, 2), 2)", "self.assertEqual(calc.mean([1, 3]), 2)",
               "self.assertEqual(calc.parse_int('+2'), 2)")[number - 1]
    with test.open("a") as stream:
        stream.write(f"\n    def test_item_{number}(self):\n        import calc\n        {example}\n")


def fake_harness(run):
    bin_dir, codex = run / "bin", run / "codex"
    bin_dir.mkdir()
    codex.mkdir()
    (codex / "models_cache.json").write_text(json.dumps({"models": [{"slug": "qa-fixture",
        "supported_reasoning_levels": [{"effort": "medium"}]}]}))
    agent = [sys.executable, str(pathlib.Path(__file__).with_name("fake_agent.py")), "reports"]
    wrapper = bin_dir / "codex"
    wrapper.write_text(f"#!/bin/sh\nexport FAKE_INTERRUPT=1\nexec {shlex.join(agent)}\n")
    wrapper.chmod(0o755)
    return agent, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CODEX_HOME": str(codex),
                   "XDG_CACHE_HOME": str(run / "cache")}


def wait_for(collect, predicate, deadline, description, *, interval=0.2):
    while time.monotonic() < deadline:
        collect()
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise RuntimeError(f"timed out waiting for {description}")


class Rehearsal:
    """Script the model-free path while exercising the real commands and evidence reads."""

    def __init__(self, fixture, observer, operator, env, deadline):
        self.root, self.observer, self.operator, self.env = fixture.root, observer, operator, env
        self.fixture, self.deadline = fixture, deadline
        self.local = LocalEvidence(fixture, observer)
        self.evidence = observer.evidence

    def factory(self, *args, member=None, allow=(0,)):
        self.evidence.factory_commands += 1
        env = {**self.env, "HC_TERMINAL_ID": member or self.operator}
        result = subprocess.run([sys.executable, str(self.observer.factory.SCRIPT), *args], cwd=self.root,
                                env=env, text=True, capture_output=True, timeout=35)
        if result.returncode not in allow or "unconfirmed" in result.stdout:
            raise RuntimeError(f"factory {args[0]} exited {result.returncode}: {result.stdout}{result.stderr}")
        return result.stdout

    def wait_for(self, predicate, description):
        return wait_for(self.local.collect, predicate, self.deadline, description)

    def note(self, what, after):
        return next((msg for msg in self.evidence.messages if is_note(msg, what) and ordered(after, msg.delivered)), None)

    def plan(self, number):
        item = f"({number})"
        move_file_item(self.root, item, "In progress", "plan: implement the requested function and test its edge cases")
        self.local.collect()
        return item

    def integrate(self, number, item):
        developer = self.fixture.developer
        implement_fixture(developer, number)
        self.local.collect()
        git(developer, "add", "--", "calc", "tests")
        git(developer, "commit", "-m", f"Implement item {item}")
        revision = git(developer, "rev-parse", "HEAD").stdout.strip()
        self.local.collect()
        reviewer = next(term for term in self.observer.qa.rpc("terminal.list")["terminals"]
                        if term["labels"].get("factory.member") == "reviewer"
                        and term["labels"].get("factory.dir") == str(self.observer.state))
        draft = self.observer.state / "messages/reviewer" / f"approval-{number}.txt"
        draft.write_text(f"status: approve; item {item}; commit {revision}\n")
        self.factory("say", "operator", "--file", str(draft), member=reviewer["address"])
        self.wait_for(lambda: any(msg.sender == "reviewer" and revision in msg.text and msg.delivered is not None
                                  for msg in self.evidence.messages), "review receipt")
        if not test_run(developer, ci=True):
            raise RuntimeError(f"CI failed at the reviewed revision for {item}")
        git(self.root, "merge", "--no-ff", "-m", f"Integrate item {item}", revision)
        merged = git(self.root, "rev-parse", "HEAD").stdout.strip()
        move_file_item(self.root, item, "Done", f"work: {merged}")
        git(self.root, "push", "origin", "main")
        self.local.collect()

    def normal(self):
        self.wait_for(lambda: self.observer.developer and self.evidence.loaded, "candidate keeper and developer")
        item = self.plan(1)
        self.factory("say", "lead-dev", f"Implement {item}\nFAKE: works_forever")
        self.wait_for(self.observer.trigger, "first assignment working turn")
        self.wait_for(lambda: self.note("quiet", self.evidence.interrupt.order), "quiet note after interrupt")
        self.factory("say", "lead-dev", f"What remains on {item}?\nFAKE: reports")
        self.integrate(1, item)
        item = self.plan(2)
        self.factory("say", "lead-dev", f"Implement {item}\nFAKE: works_forever")
        self.wait_for(lambda: self.observer.trigger(kill=True), "second assignment working turn")
        self.wait_for(lambda: self.note("exit", self.evidence.kill.order), "exit note after kill")
        self.factory("up", "lead-dev")
        self.wait_for(lambda: any(msg.kind == "brief" and msg.recipient == "lead-dev"
                                  and msg.terminal != self.evidence.kill.terminal
                                  and ordered(self.evidence.kill.order, msg.delivered)
                                  for msg in self.evidence.messages), "replacement brief")
        self.integrate(2, item)
        item = self.plan(3)
        self.factory("say", "lead-dev", f"Implement {item}")
        self.integrate(3, item)

    def empty(self):
        self.wait_for(lambda: self.note("quiet", 0), "one quiet note")
        start = time.monotonic()
        wait_for(self.observer.collect, lambda: time.monotonic() - start >= SILENCE_SECONDS,
                 self.deadline, "15 minutes of quiet", interval=2)
        self.evidence.quiet_seconds = time.monotonic() - start
        self.evidence.moved = self.observer.stamp()
        start = time.monotonic()
        move_file_item(self.root, "(1)", "Ready")
        ready = self.wait_for(lambda: self.note("ready", self.evidence.moved), "ready note after owner move")
        self.evidence.ready_seconds = time.monotonic() - start
        self.plan(1)
        self.factory("say", "lead-dev", "Implement (1)")
        self.wait_for(lambda: any(assignment(msg, "(1)") and ordered(ready.delivered, msg.delivered)
                                  for msg in self.evidence.messages), "assignment of the moved ticket")


def rehearse(args, core, rodeo, skill, hc):
    result = {"mode": "fixture", "tracker": "file", "empty": args.empty}
    budget = args.budget if args.budget is not None else 45 if args.empty else 60
    with tempfile.TemporaryDirectory(prefix="factory-qa-rehearsal-") as path:
        run = pathlib.Path(path)
        copy = run / "skill"
        shutil.copytree(skill, copy)
        factory = load_factory(copy)
        fixture = make_local(run, copy, empty=args.empty)
        agent, env = fake_harness(run)
        env["HC_BIN"] = hc
        write_formation(fixture.root, ready_command(copy), [
            ("operator", "codex", "qa-fixture"), ("members.lead-dev", "codex", "qa-fixture"),
            ("members.reviewer", "codex", "qa-fixture"),
        ], worktree=True)
        with core("e2eqa", config="notes = false\ndraft_wait = 0\n", extra_env=env) as qa, core("e2edrv") as drv:
            term = qa.rpc("terminal.run", {"argv": agent, "cwd": str(fixture.root)})["address"]
            state = pathlib.Path(env["XDG_CACHE_HOME"]) / "factory-skill" / factory.team_key(fixture.root) / qa.id[:8]
            evidence = Evidence(tuple(Ticket(f"({n})") for n in range(1, 2 if args.empty else 4)),
                                empty=args.empty, rehearsal=True)
            observer = Observer(qa, factory, state, evidence)
            started = time.monotonic()
            driver = Rehearsal(fixture, observer, term, {**qa.env, **env}, started + budget * 60)
            view = None
            try:
                driver.factory("up")
                view = rodeo(qa, drv)
                if not view.wait_for(["team", "@lead-dev", "@reviewer", "keeper"]):
                    raise RuntimeError("the scratch team did not render in rodeo")
                (driver.empty if args.empty else driver.normal)()
                evidence.within_budget = time.monotonic() <= driver.deadline
            except (RuntimeError, subprocess.SubprocessError, factory.LogDamage) as error:
                result["error"] = str(error)
            finally:
                faults = [asdict(fault) for fault in (evidence.interrupt, evidence.kill) if fault is not None]
                result.update(rows=judge(evidence), faults=faults, elapsed_seconds=round(time.monotonic() - started, 2),
                              factory_version=factory.VERSION, hc_version=qa.hello()["version"])
                if view is not None:
                    view.close()
                try:
                    driver.factory("down", allow=(0, 1, 2))
                except (RuntimeError, subprocess.SubprocessError) as error:
                    result["teardown_error"] = str(error)
    result["status"] = ("pass" if "error" not in result and "teardown_error" not in result and
                        all(r["status"] in {"pass", "measured"} for r in result["rows"]) else "fail")
    return result


def collect_run(collector, observer, move, deadline, checkpoint):
    """Observe a real team, injecting only the specified faults or empty-board move."""
    evidence = observer.evidence
    quiet_since = moved_at = None
    last_summary = None
    while True:
        collector.collect()
        now = time.monotonic()
        evidence.within_budget = now <= deadline
        if evidence.empty and evidence.within_budget:
            quiet = next((m for m in evidence.messages if is_note(m, "quiet") and m.delivered is not None), None)
            if quiet and quiet_since is None:
                quiet_since = now
            if quiet_since is not None and evidence.moved is None and now - quiet_since >= SILENCE_SECONDS:
                evidence.quiet_seconds = now - quiet_since
                evidence.moved = observer.stamp()
                moved_at = time.monotonic()
                move()
            if moved_at is not None and evidence.ready_seconds == float("inf"):
                if any(is_note(m, "ready") and ordered(evidence.moved, m.delivered) for m in evidence.messages):
                    evidence.ready_seconds = now - moved_at
        elif not evidence.empty and evidence.within_budget:
            if evidence.interrupt is None:
                observer.trigger()
            if evidence.kill is None:
                observer.trigger(kill=True)
        rows = judge(evidence)
        summary = tuple(r["status"] for r in rows)
        if summary != last_summary:
            checkpoint(rows)
            last_summary = summary
        if all(status in {"pass", "measured"} for status in summary) or not evidence.within_budget:
            return rows
        time.sleep(0.25)


def evidence_result(observer, rows, started):
    evidence = observer.evidence
    return {"rows": rows, "cutoff_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "faults": [asdict(fault) for fault in (evidence.interrupt, evidence.kill) if fault is not None],
            "messages": [{"id": msg.ident, "kind": msg.kind, "sender": msg.sender, "recipient": msg.recipient,
                          "outcome": msg.outcome, "committed": msg.committed, "delivered": msg.delivered,
                          "terminal": msg.terminal, "turn": msg.turn,
                          "items": [ticket.item for ticket in evidence.tickets if names_item(msg, ticket.item)]}
                         for msg in evidence.messages],
            "log_offset": observer.offset, "owner_inputs": evidence.prompts}


def run_real(args, live):
    """Launch the private team through the Phase 8 adapter and submit the single owner prompt."""
    report = live.Qualification("real", args.json, versions=live._versions())
    result = report.data
    result.update(tracker=args.tracker, empty=args.empty, status="not_run")
    budget = args.budget if args.budget is not None else 45 if args.empty else 60
    members = {"lead-dev": args.developer, "reviewer": args.reviewer}
    if args.tracker != "file":
        result["detail"] = "GitHub fixture is not available yet"
        return result
    if set(members.values()) - set(live.QUALIFY_HARNESSES):
        result["detail"] = "choose developer and reviewer harnesses from the qualification table"
        return result

    def setup(team):
        nonlocal fixture
        profiles = [("operator", args.operator, args.operator_model),
                    ("lead-dev", args.developer, args.developer_model),
                    ("reviewer", args.reviewer, args.reviewer_model)]
        agents = []
        for role, harness, requested in profiles:
            model = requested or team.adapter.model(harness)
            section = role if role == "operator" else f"members.{role}"
            agents.append((section, harness, model))
            report.profile(role, harness, model=model, version=team.adapter.version(harness),
                           thinking=None if role == "operator" else "medium")
        fixture = make_local(team.dir, team.skill, empty=args.empty)
        write_formation(fixture.root, ready_command(team.skill), agents, worktree=True)
        result["fixture"] = {"root": str(fixture.root), "initial": fixture.initial, "developer_worktree": True}
        return fixture.root

    fixture = None
    try:
        with live.QATeam(live.RealAdapter, args.operator, members, setup=setup,
                         operator_model=args.operator_model) as team:
            if args.developer in live.TRUST_FILES:
                result.setdefault("trust_entries", []).append({"harness": args.developer,
                    "file": live.TRUST_FILES[args.developer], "directory": str(fixture.developer)})
            if not live._operator_start(team, report, 0, "operator", args.operator, ["startup"]):
                result["detail"] = result["rows"][0]["reason"]
                return result
            result["operator_start"] = team.operator_start
            live._operator_tab(team)
            view = live._rodeo(team, ["operator"])
            factory = load_factory(team.skill)
            # This process can have another cache base. The operator and members share the QA cache.
            state = (pathlib.Path(team.env["XDG_CACHE_HOME"]) / "factory-skill"
                     / factory.team_key(fixture.root) / team.qa.id[:8])
            evidence = Evidence(tuple(Ticket(f"({n})") for n in range(1, 2 if args.empty else 4)), empty=args.empty)
            observer = Observer(team.qa, factory, state, evidence)
            collector = LocalEvidence(fixture, observer)

            def move():
                move_file_item(fixture.root, "(1)", "Ready")

            def checkpoint(rows):
                result.update(evidence_result(observer, rows, started))
                report.checkpoint()

            prompt = (f"Read {team.skill / 'SKILL.md'} and follow it. Use {team.script} for all Factory commands. "
                      "You are the operator of the team in this repository. Run up, then work through every ready "
                      "item in the tracker as FACTORY.md says until none is left. I am away: do not wait for me.")
            started = time.monotonic()
            rows = judge(evidence)
            try:
                view.paste(prompt)
                evidence.prompts = 1
                result["status"] = "running"
                rows = collect_run(collector, observer, move, started + budget * 60, checkpoint)
                result["status"] = "pass" if all(r["status"] in {"pass", "measured"} for r in rows) else "fail"
            except (OSError, RuntimeError, live.HcDown, live.HcError, subprocess.SubprocessError,
                    factory.LogDamage) as error:
                result.update(status="fail", detail=str(error))
                rows = judge(evidence)
            finally:
                checkpoint(rows)
                try:
                    code, out = team.call("down", timeout=60)
                    if code != 0:
                        result["teardown"] = {"exit": code, "output": live._bounded(out)}
                except (OSError, subprocess.SubprocessError) as error:
                    result["teardown"] = {"error": str(error)}
    except (live.NotRun, live.HcDown, live.HcError, OSError, subprocess.SubprocessError) as error:
        result.update(status="not_run", detail=str(error))
    return result


def run(args, core, rodeo, skill, hc, live=None):
    if args.rehearse:
        result = rehearse(args, core, rodeo, skill, hc)
    else:
        result = run_real(args, live)
    payload = json.dumps(result, indent=2) + "\n"
    if args.json:
        args.json.write_text(payload)
    sys.stdout.write(payload)
    return {"pass": 0, "fail": 1, "not_run": 2}[result["status"]]
