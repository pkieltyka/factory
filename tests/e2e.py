"""Scratch repositories and evidence checks for the live autonomy runs."""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
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
from urllib.parse import urlencode


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
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
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
    operator: dict | None = None


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
    interrupt_wait_reason: str | None = None
    kill: Fault | None = None
    recovered: dict = field(default_factory=dict)  # live replacement addresses -> observation order of their creation
    github: bool = False
    scoped: bool = False
    empty: bool = False
    moved: int | None = None
    quiet_seconds: float = 0
    ready_seconds: float = float("inf")
    member_worked_during_silence: bool = False
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


def should_interrupt(message, ticket, terminal, before_claim, *, operator_ended, pending, fired=False, reported=False):
    return (not fired and not reported and operator_ended and not pending
            and working_assignment(message, ticket.item, terminal, before_claim))


def operator_ended_turn(terminal, before):
    return (before is not None and terminal is not None and terminal.get("state") == "live"
            and terminal["address"] == before[0] and terminal["turn"] > before[1]
            and (terminal.get("status") or {}).get("state") in {"idle", "done"})


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
        return row("quiet-then-chase", "not_run", detail=evidence.interrupt_wait_reason or "interrupt trigger did not fire")
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
        self.operator_turns = {}
        self.developer = None
        self.operator = None
        self.member_working = False

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
        self.member_working = False
        for term in records:
            labels = term["labels"]
            if term["state"] != "live" or labels.get("factory.dir") != str(self.state):
                continue
            kind = labels.get("factory.kind")
            if kind == "member" and (term.get("status") or {}).get("state") == "working":
                self.member_working = True
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
        recipients = {"lead-dev"} if kill else {"lead-dev", "operator"}
        pending = any(p.recipient in recipients and p.outcome in {"sending", "queued"} for p in self.pairs.values())
        operator_transition = None
        if kill:
            if not should_kill(msg, ticket, term, baseline, evidence.tickets[0].integrated,
                               fired=evidence.kill is not None, pending=pending):
                return False
        else:
            operator = self.operator
            if operator and (operator.get("status") or {}).get("state") == "working":
                self.operator_turns.setdefault(msg.ident, (operator["address"], operator["turn"]))
            operator_before = self.operator_turns.get(msg.ident)
            operator_ended = operator_ended_turn(operator, operator_before)
            reported = any(p.sender == "lead-dev" and names_item(p, ticket.item) and p.committed > msg.committed
                           for p in self.pairs.values())
            if not reported and working_assignment(msg, ticket.item, term, baseline):
                if not operator_ended:
                    evidence.interrupt_wait_reason = (
                        "operator did not end its turn while the developer worked on the assignment"
                        if operator_before else "operator turn end after assignment delivery was not observed")
                elif pending:
                    evidence.interrupt_wait_reason = "message pending for the operator or developer"
                else:
                    evidence.interrupt_wait_reason = None
            if not should_interrupt(msg, ticket, term, baseline, operator_ended=operator_ended, pending=pending,
                                    fired=evidence.interrupt is not None, reported=reported):
                return False
            operator = self.qa.rpc("terminal.get", {"terminal": operator["address"]})
            if not operator_ended_turn(operator, operator_before):
                return False
            operator_transition = {"terminal": operator["address"], "working_turn": operator_before[1],
                                   "ended_turn": operator["turn"]}
        address = term["address"]
        # Recheck the address and turn immediately before the mutation.
        current = self.qa.rpc("terminal.get", {"terminal": address})
        if not working_assignment(msg, ticket.item, current, baseline):
            return False
        if not kill and self.log.read(self.offset)[1] != self.offset:
            return False   # a new delivery or report must be observed before interrupting
        fault = Fault(ticket.item, address, current["turn"], msg.delivered, self.stamp(), self.offset,
                      operator_transition)
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
QA_REPO = "pkieltyka/factory-test"
QA_OWNER = QA_REPO.split("/")[0]
QA_URL = f"https://github.com/{QA_REPO}"
QA_CREDENTIAL = "!gh auth git-credential"
QA_CI = """name: Tests
on:
  push:
    branches: ['qa/**']
  pull_request:
    branches: ['qa/**']
permissions:
  contents: read
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: ./ci.sh
"""


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


class GitHubFixture:
    """Create and inspect only the approved scratch repository, project, and run-named refs."""

    def __init__(self, run, skill, report, empty):
        self.root = run / "repo"
        self.root.mkdir()
        self.report = report
        self.name = f"factory-qa-{report.data['run_id']}"
        self.prefix = f"qa/{report.data['run_id']}/"
        self.base = self.prefix + "main"
        self.manifest = report.data.setdefault("artifacts", {"repo": QA_REPO, "issues": [], "branches": [], "prs": []})
        repo = self.api(f"repos/{QA_REPO}")
        if (repo.get("full_name") != QA_REPO or repo.get("private") is not True or repo.get("default_branch") != "master"
                or repo.get("has_issues") is not True or repo.get("html_url") != QA_URL):
            raise RuntimeError("factory-test is not the expected private issue-enabled repository")
        self.master = self.branch_head("master")
        self.other_refs = self.refs()
        self.manifest["master_before"] = self.master
        project = self.gh("project", "create", "--owner", QA_OWNER, "--title", self.name, "--format", "json")
        if project.get("title") != self.name or not isinstance(project.get("number"), int) or not project.get("id"):
            raise RuntimeError("GitHub did not return the new run project's identity")
        self.project = f"{QA_OWNER}/{project['number']}"
        self.project_id = project["id"]
        self.manifest["project"] = {key: project[key] for key in ("id", "number", "url", "title")}
        self.save_manifest()
        self.status_field, self.options = self.status_options()
        self.manifest["status_field"] = self.status_field
        self.manifest["options"] = self.options
        self.save_manifest()
        self.seed_repo(skill)
        for title, criteria in TASKS:
            issue = self.api(f"repos/{QA_REPO}/issues", payload={"title": f"[{self.name}] {title}",
                             "body": f"Factory autonomy fixture: {self.name}\n\nAcceptance criteria: {criteria}\n"})
            card = {"id": issue["node_id"], "number": issue["number"], "url": issue["html_url"]}
            self.manifest["issues"].append(card)
            self.save_manifest()
            if card["url"] != f"{QA_URL}/issues/{card['number']}":
                raise RuntimeError("the new issue is outside factory-test")
            added = self.gh("project", "item-add", str(project["number"]), "--owner", QA_OWNER, "--url", card["url"],
                            "--format", "json")
            card["project_item_id"] = added["id"]
            self.save_manifest()
            self.set_status(card, "Backlog" if empty else "Todo")
        self.todos("doctor")
        expected = {issue["number"]: "Backlog" if empty else "Todo" for issue in self.manifest["issues"]}
        self.wait_for_board(expected)

    def gh(self, *args, payload=None, text=False):
        operation = subprocess.run(["gh", *args], cwd=self.root, env={**os.environ, "GH_HOST": "github.com"},
                                   input=json.dumps(payload) if payload is not None else None,
                                   capture_output=True, text=True, timeout=45, check=False)
        if operation.returncode:
            raise RuntimeError(f"gh {' '.join(args[:3])}: {operation.stderr.strip()}")
        return operation.stdout if text else json.loads(operation.stdout)

    def api(self, path, *, payload=None):
        return self.gh("api", "--method", "GET" if payload is None else "POST", path,
                       *([] if payload is None else ["--input", "-"]), payload=payload)

    def graphql(self, query, **variables):
        answer = self.gh("api", "graphql", "--input", "-", payload={"query": query, "variables": variables})
        if answer.get("errors"):
            raise RuntimeError(f"GitHub project query failed: {answer['errors'][0]['message']}")
        return answer["data"]

    def status_options(self):
        field = self.graphql("""query($project:ID!) { node(id:$project) { ... on ProjectV2 {
            title public field(name:"Status") { ... on ProjectV2SingleSelectField {
                id options {id name color description} } } } } }""", project=self.project_id)["node"]
        if field.get("title") != self.name or field.get("public") is not False or not field.get("field"):
            raise RuntimeError("the run's project is not the expected private project with a Status field")
        status = field["field"]
        options = status["options"]
        if "Backlog" not in {option["name"] for option in options}:
            options = [*options, {"name": "Backlog", "color": "GRAY", "description": "Factory QA backlog"}]
            changed = self.graphql("""mutation($input:UpdateProjectV2FieldInput!) { updateProjectV2Field(input:$input) {
                projectV2Field { ... on ProjectV2SingleSelectField { id options { id name } } } } }""",
                input={"fieldId": status["id"], "singleSelectOptions": options})
            options = changed["updateProjectV2Field"]["projectV2Field"]["options"]
        by_name = {o["name"]: o["id"] for o in options}
        if not {"Backlog", "Todo", "In Progress", "Done"} <= by_name.keys():
            raise RuntimeError("the run's project does not have the four expected statuses")
        return status["id"], by_name

    def set_status(self, card, status):
        if card not in self.manifest["issues"] or card["url"] != f"{QA_URL}/issues/{card['number']}":
            raise RuntimeError("refusing a status write for a card outside this run")
        self.gh("project", "item-edit", "--id", card["project_item_id"], "--project-id", self.project_id,
                "--field-id", self.status_field, "--single-select-option-id", self.options[status], text=True)

    def todos(self, *args):
        result = subprocess.run(["todos", *args, "--repo", QA_REPO, "--project", self.project, "--json", "--fresh"],
                                cwd=self.root, capture_output=True, text=True, timeout=45, check=False)
        if result.returncode:
            raise RuntimeError(f"todos {args[0]}: {result.stderr.strip() or result.stdout.strip()}")
        return json.loads(result.stdout)

    def board(self):
        board = self.todos("board")
        project = board.get("project") or {}
        if (board.get("truncated") is not False or project.get("title") != self.name
                or f"{project.get('owner')}/{project.get('number')}" != self.project):
            raise RuntimeError("the GitHub project snapshot is incomplete or belongs to another project")
        if board.get("hidden_items") != 0:
            raise RuntimeError("the run's project has cards outside the visible scratch issues")
        cards = {}
        for column in board["columns"]:
            for card in column["cards"]:
                number = card["number"]
                if card.get("url") != f"{QA_URL}/issues/{number}":
                    raise RuntimeError("the run's project has a card outside factory-test")
                if number in cards:
                    raise RuntimeError("GitHub project returned duplicate issue numbers")
                cards[number] = column["status"]
        return cards

    def record_discoveries(self, numbers):
        """Retain every additional issue observed on the run's project."""
        discoveries = self.manifest.setdefault("discoveries", [])
        for number in sorted(numbers - {issue["number"] for issue in discoveries}):
            issue = self.api(f"repos/{QA_REPO}/issues/{number}")
            if issue.get("html_url") != f"{QA_URL}/issues/{number}" or not issue.get("node_id"):
                raise RuntimeError("the run's project has a card outside factory-test")
            discoveries.append({"number": number, "id": issue["node_id"], "url": issue["html_url"]})

    def wait_for_board(self, expected, timeout=20):
        """Read back recent GitHub writes until this exact set of cards and states is visible."""
        deadline = time.monotonic() + timeout
        while self.board() != expected:
            if time.monotonic() >= deadline:
                raise RuntimeError("the freshly created project does not show exactly this run's cards and statuses")
            time.sleep(1)

    def seed_repo(self, skill):
        git(self.root, "-c", f"credential.helper={QA_CREDENTIAL}", "clone", "--no-checkout", QA_URL + ".git", ".")
        git(self.root, "config", "credential.https://github.com.helper", QA_CREDENTIAL)
        git(self.root, "config", "user.name", "Factory QA")
        git(self.root, "config", "user.email", "qa@example.invalid")
        git(self.root, "switch", "--orphan", self.base)
        write_project(self.root)
        workflow = self.root / ".github/workflows/test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(QA_CI)
        (self.root / "FACTORY.md").write_text(self.brief(skill))
        git(self.root, "add", "--", "FACTORY.md", ".gitignore", "ci.sh", "calc", "tests", ".github")
        git(self.root, "commit", "-m", f"Seed {self.name}")
        self.initial = git(self.root, "rev-parse", "HEAD").stdout.strip()
        git(self.root, "push", "-u", "origin", f"HEAD:refs/heads/{self.base}")
        self.manifest["branches"].append(self.branch_ref(self.base))
        self.save_manifest()

    def branch_ref(self, name):
        return {"name": name, "url": f"{QA_URL}/tree/{name}"}

    def branch_head(self, name):
        return self.api(f"repos/{QA_REPO}/git/ref/heads/{name}")["object"]["sha"]

    def refs(self):
        rows = self.api(f"repos/{QA_REPO}/git/matching-refs/heads/")
        return {row["ref"].removeprefix("refs/heads/"): row["object"]["sha"] for row in rows}

    def save_manifest(self):
        (self.root.parent / "artifacts.json").write_text(json.dumps(self.manifest, indent=2) + "\n")
        self.report.checkpoint()

    def brief(self, skill):
        tasks = (skill / "recipes/tasks-todos.md").read_text().split("```markdown\n", 1)[1].split("```", 1)[0]
        tasks = (tasks.replace("OWNER/REPO", QA_REPO).replace("BOARD_OWNER/BOARD_NUMBER", self.project)
                 .replace("READY_COLUMN", "Todo").replace("ACTIVE_COLUMN", "In Progress")
                 .replace("BACKLOG_COLUMN", "Backlog"))
        pulls = (skill / "recipes/pull-requests-github.md").read_text().split("```markdown\n", 1)[1].split("```", 1)[0]
        pulls = (pulls.replace("OWNER/REPO", QA_REPO).replace("BOARD_OWNER/BOARD_NUMBER", self.project)
                 .replace('owner=OWNER -F repo=REPO', f'owner={QA_OWNER} -F repo=factory-test')
                 .replace("BASE_BRANCH", self.base).replace("`<user>/<ticket>`", f"`{self.prefix}<issue>`")
                 .replace("CI_DESCRIPTION", "the `tests` job of the Tests workflow"))
        brief = (skill / "templates/FACTORY.md").read_text()
        brief = re.sub(r"(?s)## Tasks\n.*?(?=## Workflow)", tasks + "\n", brief, count=1)
        brief = brief.replace("## Project\n", "## Project\n\nA small Python calculator used to exercise Factory.\n", 1)
        brief = re.sub(r"(?m)^\| 6\. Integrate .*", "| 6. Integrate | The operator | Confirms review and CI, then "
                      "squash-merges the reviewed PR and updates the issue and board. |", brief, count=1)
        instructions = f"""- This run is scoped to {QA_REPO}, project {self.project}, and Git branches under `{self.prefix}`.
- Always pass `--repo {QA_REPO} --project {self.project} --json` to `todos` and `--repo {QA_REPO}` to GitHub issue/PR commands.
- Process the run's Todo issues in ascending issue number. Every assignment, report and verdict names its item as `#N`.
- Before any code change, write a short issue comment starting `plan:`. Ticket branches are `{self.prefix}<issue>`.
- The reviewer records approval as `status: approve; item #N; commit <full-sha>`.
- Confirm the PR merge before closing its issue and moving its own card to Done. The integration branch is not the default.

"""
        permissions = (f"The owner authorizes pushes and opening/updating PRs only for this run's branches in {QA_REPO}.\n"
                       f"The owner authorizes squash-merging those PRs only into `{self.base}`.\n"
                       "Keep the run's branches and project after the run.\n")
        return brief.replace("## Notes\n", instructions + pulls + "\n" + permissions + "\n## Notes\n", 1)

    def ready_command(self, skill):
        recipe = (skill / "recipes/tasks-todos.md").read_text()
        command = next(line.strip("`") for line in recipe.splitlines() if line.startswith("`board=$(todos board"))
        return command.replace("OWNER/REPO", QA_REPO).replace("BOARD_OWNER/BOARD_NUMBER", self.project).replace(
            "READY_COLUMN", "Todo")


def forge_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class GitHubEvidence:
    """Observe the run's board, PR heads and CI, and verify code in a separate checkout."""

    def __init__(self, fixture, observer):
        self.repo, self.observer = fixture, observer
        self.inspection = fixture.root.parent / "inspect.git"
        git(fixture.root.parent, "clone", "--bare", "--no-local", str(fixture.root), str(self.inspection))
        git(self.inspection, "remote", "add", "forge", QA_URL + ".git")
        git(self.inspection, "config", "credential.https://github.com.helper", QA_CREDENTIAL)
        self.head = fixture.initial
        self.next_forge_read = 0
        self.previous = ()
        self.first_merges = {}
        self.open_prs = {}
        self.approvals = {}
        self.checks = {}

    def collect(self):
        self.observer.collect()
        evidence, repo = self.observer.evidence, self.repo
        branch = git(repo.root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
        changes = git(repo.root, "status", "--porcelain", "--", *CODE_PATHS).stdout
        bases = git(repo.root, "for-each-ref", "--format=%(objectname)", f"refs/heads/{repo.base}",
                    f"refs/remotes/origin/{repo.base}").stdout.split()
        tickets = []
        for ticket in evidence.tickets:
            name = repo.prefix + ticket.item[1:]
            if ticket.changed is None and ((branch == name and changes)
                    or git(repo.root, "log", "-1", "--format=%H", f"refs/heads/{name}", "--not", *bases,
                           "--", *CODE_PATHS, check=False).stdout):
                ticket = replace(self.observe_plan(ticket), changed=self.observer.stamp())
            tickets.append(ticket)
        evidence.tickets = tuple(tickets)
        self.observe_approvals()
        if time.monotonic() >= self.next_forge_read:
            self.refresh_forge()
            self.previous = evidence.messages
            self.next_forge_read = time.monotonic() + 8

    def observe_approvals(self):
        """When a verdict arrives, read the PR before the next whole-board sample."""
        for number, (item, revision) in self.open_prs.items():
            key = number, revision
            if key in self.approvals:
                continue
            boundary = Ticket(item, revision=revision, integrated=self.observer.stamp())
            verdict = approval(boundary, self.observer.evidence.messages)
            if verdict:
                pr = self.repo.api(f"repos/{QA_REPO}/pulls/{number}")
                self.approvals[key] = (verdict,) if (pr["state"] == "open" and not pr.get("merged_at")
                                                      and pr["head"]["sha"] == revision) else ()

    def observe_plan(self, ticket):
        if ticket.planned is None:
            number = ticket.item[1:]
            comments = self.repo.api(f"repos/{QA_REPO}/issues/{number}/comments?per_page=100")
            if any(re.match(r"\s*plan:\s*\S", note["body"], re.I) for note in comments):
                return replace(ticket, planned=self.observer.stamp())
        return ticket

    def refresh_forge(self):
        evidence, repo = self.observer.evidence, self.repo
        evidence.scoped = False
        project = repo.board()
        expected = {card["number"] for card in repo.manifest["issues"]}
        master_now = repo.branch_head("master")
        repo.manifest["master_after"] = master_now
        if master_now != repo.master:
            raise RuntimeError("factory-test master changed during the run")
        refs = repo.refs()
        tracking = {name: sha for name, sha in refs.items() if name.startswith(repo.prefix)}
        outside = {name: sha for name, sha in refs.items() if name not in tracking}
        repo.manifest["branches"] = [repo.branch_ref(name) for name in sorted(tracking)]
        if not expected <= project.keys() or repo.base not in tracking or outside != repo.other_refs:
            raise RuntimeError("the run's project or refs no longer match the fixture")
        repo.record_discoveries(project.keys() - expected)
        params = urlencode({"state": "all", "base": repo.base, "per_page": 100})
        prs = repo.api(f"repos/{QA_REPO}/pulls?{params}")
        by_issue = {}
        for pr in prs:
            name = pr["head"]["ref"]
            if (pr["base"]["ref"] != repo.base or pr["head"]["repo"] is None
                    or pr["head"]["repo"]["full_name"] != QA_REPO or not name.startswith(repo.prefix)):
                raise RuntimeError("a pull request into the run's branch has a head outside the fixture")
            number = name.removeprefix(repo.prefix)
            if not number.isdecimal() or int(number) not in expected or number in by_issue:
                raise RuntimeError("pull requests do not match the run's tickets one to one")
            by_issue[number] = pr
        repo.manifest["prs"] = [{"id": pr["node_id"], "number": pr["number"], "url": pr["html_url"]} for pr in prs]
        self.open_prs = {pr["number"]: (f"#{number}", pr["head"]["sha"])
                         for number, pr in by_issue.items() if pr["state"] == "open"}
        repo.save_manifest()
        evidence.scoped = True
        if tracking[repo.base] != self.head:
            self.head = self.fetch_main()
            commits = git(self.inspection, "rev-list", "--first-parent", f"{repo.initial}..{self.head}").stdout.split()
            evidence.main_code = frozenset(commits)
            evidence.ci_count = LocalEvidence.verify(self.inspection, self.head, lambda root: test_run(root, ci=True))
        tickets = []
        for ticket in evidence.tickets:
            number = ticket.item[1:]
            current = self.observe_plan(replace(ticket, state=project[int(number)]))
            stamp = self.observer.stamp()
            pr = by_issue.get(number)
            if pr and pr.get("merged_at"):
                when, earlier = self.first_merges.setdefault(pr["number"], (stamp, self.previous))
                earlier += self.approvals.get((pr["number"], pr["head"]["sha"]), ())
                if pr.get("merge_commit_sha") in evidence.main_code:
                    current = self.merge_evidence(current, pr, when, earlier)
            tickets.append(current)
        evidence.tickets = tuple(tickets)

    def fetch_main(self):
        # Publish the observation in this private bare repository so fresh verification clones can fetch it.
        observed = "refs/heads/observed"
        git(self.inspection, "fetch", "--no-tags", "forge", f"+refs/heads/{self.repo.base}:{observed}")
        return git(self.inspection, "rev-parse", observed).stdout.strip()

    def merge_evidence(self, ticket, pr, stamp, earlier):
        repo = self.repo
        revision, squash = pr["head"]["sha"], pr["merge_commit_sha"]
        parents = git(self.inspection, "show", "-s", "--format=%P", squash).stdout.split()
        if len(parents) != 1:
            return ticket
        merged_at = forge_time(pr["merged_at"])
        if ticket.integrated is None:
            candidate = replace(ticket, revision=revision, integrated=stamp)
            if approval(candidate, earlier) is None:
                return ticket
            count = self.checked(revision, merged_at)
            number = int(ticket.item[1:])
            index = next(i for i, card in enumerate(repo.manifest["issues"], 1) if card["number"] == number)
            accepted = LocalEvidence.verify(self.inspection, self.head, lambda root: accepts(root, index))
            ticket = replace(candidate, branch_proof=True, introduced=frozenset({squash}), accepted=accepted,
                             checked_revision=revision, test_count=count, timely_check=count > 0)
        if ticket.closed is None and ticket.state == "Done":
            number = int(ticket.item[1:])
            issue = repo.api(f"repos/{QA_REPO}/issues/{number}")
            card = next(card for card in repo.manifest["issues"] if card["number"] == number)
            status = repo.graphql("""query($id:ID!) { node(id:$id) { ... on ProjectV2Item {
                fieldValueByName(name:"Status") { ... on ProjectV2ItemFieldSingleSelectValue { name updatedAt } }
                } } }""", id=card["project_item_id"])["node"]["fieldValueByName"]
            closed_at = forge_time(issue.get("closed_at"))
            done_at = forge_time(status.get("updatedAt")) if status else None
            if closed_at and done_at and status["name"] == "Done" and min(closed_at, done_at) > merged_at:
                ticket = replace(ticket, closed=self.observer.stamp())
        return ticket

    def checked(self, revision, merged_at):
        if revision in self.checks:
            return self.checks[revision]
        repo = self.repo
        runs = repo.api(f"repos/{QA_REPO}/actions/runs?head_sha={revision}&status=success")["workflow_runs"]
        count = 0
        for run in runs:
            if (run["head_sha"] != revision or run["name"] != "Tests"
                    or not (run.get("head_branch") or "").startswith(repo.prefix)):
                continue
            jobs = repo.api(f"repos/{QA_REPO}/actions/runs/{run['id']}/jobs")["jobs"]
            for job in jobs:
                done = forge_time(job.get("completed_at"))
                if job["name"] != "tests" or job["conclusion"] != "success" or not done or not done < merged_at:
                    continue
                try:
                    log = repo.gh("run", "view", "--repo", QA_REPO, "--job", str(job["id"]), "--log", text=True)
                except RuntimeError as error:
                    if "pass --allow-escape-sequences" not in str(error):
                        raise
                    # The raw response is captured, never printed. The installed gh accepts this flag on api.
                    log = repo.gh("api", "--allow-escape-sequences", f"repos/{QA_REPO}/actions/jobs/{job['id']}/logs",
                                  text=True)
                log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log)
                count = max((int(n) for n in re.findall(r"\bRan ([0-9]+) tests? in ", log)), default=0)
                if count:
                    self.checks[revision] = count
                    return count
        self.checks[revision] = count
        return count


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


def write_formation(root, ready, agents, *, worktree, off=lambda harness: []):
    lines = ['user = "qa"', "quiet_grace = 30", f"ready_check_every = {READY_PERIOD}",
             f"ready_check = {json.dumps(ready)}"]
    for section, harness, model in agents:
        lines.extend((f"\n[{section}]", f"harness = {json.dumps(harness)}", f"model = {json.dumps(model)}",
                      'thinking = "medium"'))
        args = off(harness) if section.startswith("members.") else []
        if args:                      # the owner's rule: user skill discovery off where the harness can
            lines.append(f"args = {json.dumps(args)}")
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
        release = self.root.parent / "finish-operator-turn"
        wait = f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.1; done"
        self.observer.qa.rpc("terminal.send", {"terminal": self.operator, "text": f"ACT run {release}.ran {wait}"})
        self.wait_for(lambda: self.observer.operator and self.observer.operator["status"]["state"] == "working",
                      "operator working turn")
        self.factory("say", "lead-dev", f"Implement {item}\nFAKE: works_forever")
        self.wait_for(lambda: self.observer.trigger() or self.observer.operator_turns,
                      "first assignment while the operator is working")
        release.touch()
        self.wait_for(self.observer.trigger, "first assignment after the operator ends its turn")
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
    last_checkpoint = 0
    last_observation = None
    while True:
        collector.collect()
        now = time.monotonic()
        evidence.within_budget = now <= deadline
        if evidence.empty and evidence.within_budget:
            quiet = next((m for m in evidence.messages if is_note(m, "quiet") and m.delivered is not None), None)
            if quiet and quiet_since is None:
                quiet_since = now
            if quiet_since is not None and evidence.moved is None and observer.member_working:
                evidence.member_worked_during_silence = True
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
        observation = summary, observer.offset
        if observation != last_observation or now - last_checkpoint >= 30:
            checkpoint(rows)
            last_observation, last_checkpoint = observation, now
        if all(status in {"pass", "measured"} for status in summary) or not evidence.within_budget:
            return rows
        time.sleep(0.25)


def evidence_result(observer, rows, started):
    evidence = observer.evidence
    result = {"rows": rows, "cutoff_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "faults": [asdict(fault) for fault in (evidence.interrupt, evidence.kill) if fault is not None],
            "messages": [{"id": msg.ident, "kind": msg.kind, "sender": msg.sender, "recipient": msg.recipient,
                          "outcome": msg.outcome, "committed": msg.committed, "delivered": msg.delivered,
                          "terminal": msg.terminal, "turn": msg.turn,
                          "items": [ticket.item for ticket in evidence.tickets if names_item(msg, ticket.item)]}
                         for msg in evidence.messages],
            "log_offset": observer.offset, "owner_inputs": evidence.prompts}
    if evidence.empty:
        result.update(quiet_seconds=round(evidence.quiet_seconds, 2), moved=evidence.moved,
                      member_worked_during_silence=evidence.member_worked_during_silence,
                      ready_seconds=round(evidence.ready_seconds, 2) if evidence.ready_seconds != float("inf") else None)
    return result


def run_real(args, live):
    """Launch the private team through the Phase 8 adapter and submit the single owner prompt."""
    report = live.Qualification("real", args.json, versions=live._versions())
    result = report.data
    result.update(tracker=args.tracker, empty=args.empty, status="not_run")
    github = args.tracker == "github"
    budget = args.budget if args.budget is not None else 45 if args.empty else 120 if github else 60
    members = {"lead-dev": args.developer, "reviewer": args.reviewer}
    if github and any(shutil.which(command) is None for command in ("gh", "todos", "jq")):
        result["detail"] = "the GitHub workspace needs gh, todos and jq"
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
        if github:
            fixture = GitHubFixture(team.dir, team.skill, report, args.empty)
            ready = fixture.ready_command(team.skill)
        else:
            fixture = make_local(team.dir, team.skill, empty=args.empty)
            ready = ready_command(team.skill)
        write_formation(fixture.root, ready, agents, worktree=not github,
                        off=lambda harness: live.skills_off(harness, team.qa.installed))
        result["fixture"] = {"root": str(fixture.root), "initial": fixture.initial, "developer_worktree": not github}
        return fixture.root

    fixture = None
    try:
        with live.QATeam(live.RealAdapter, args.operator, members, setup=setup,
                         operator_model=args.operator_model) as team:
            trust_directories = None if github else {"lead-dev": fixture.developer}
            if not live._operator_start(team, report, 0, "operator", args.operator, ["startup"], trust_directories):
                result["detail"] = result["rows"][0]["reason"]
                return result
            result["operator_start"] = team.operator_start
            live._operator_tab(team)
            view = live._rodeo(team, ["operator"])
            factory = load_factory(team.skill)
            # This process can have another cache base. The operator and members share the QA cache.
            state = (pathlib.Path(team.env["XDG_CACHE_HOME"]) / "factory-skill"
                     / factory.team_key(fixture.root) / team.qa.id[:8])
            if github:
                items = [f"#{item['number']}" for item in fixture.manifest["issues"]]
            else:
                items = [f"({n})" for n in range(1, 2 if args.empty else 4)]
            evidence = Evidence(tuple(Ticket(item) for item in items), empty=args.empty, github=github)
            observer = Observer(team.qa, factory, state, evidence)
            collector = GitHubEvidence(fixture, observer) if github else LocalEvidence(fixture, observer)

            def move():
                if github:
                    fixture.set_status(fixture.manifest["issues"][0], "Todo")
                else:
                    move_file_item(fixture.root, "(1)", "Ready")

            def checkpoint(rows):
                result.update(evidence_result(observer, rows, started))
                report.checkpoint()

            prompt = (f"Read {team.skill / 'SKILL.md'} and follow it. Use {team.script} for all Factory commands. "
                      "You are the operator of the team in this repository. Run up, then work through every ready "
                      "item in the tracker as FACTORY.md says until none is left. I am away: do not wait for me.")
            started = time.monotonic()
            try:
                view.paste(prompt)
                evidence.prompts = 1
                result["status"] = "running"
                rows = collect_run(collector, observer, move, started + budget * 60, checkpoint)
                result["status"] = "pass" if all(r["status"] in {"pass", "measured"} for r in rows) else "fail"
            except (KeyboardInterrupt, SystemExit):
                result.update(status="fail", detail="the runner interrupted the run before a verdict")
            except (OSError, RuntimeError, live.HcDown, live.HcError, subprocess.SubprocessError,
                    factory.LogDamage) as error:
                result.update(status="fail", detail=str(error))
            finally:
                if result["status"] == "running":
                    result.update(status="fail", detail="the runner ended the run before a verdict")
                checkpoint(judge(evidence))
                if installed_verdict(result, *live.split_seen(team.qa.installed_seen([team.dir], team.harness_of))):
                    report.checkpoint()
                try:
                    code, out = team.call("down", timeout=60)
                    if code != 0:
                        result["teardown"] = {"exit": code, "output": live._bounded(out)}
                except (OSError, subprocess.SubprocessError) as error:
                    result["teardown"] = {"error": str(error)}
    except (live.NotRun, live.HcDown, live.HcError, OSError, RuntimeError, ValueError, KeyError,
            subprocess.SubprocessError) as error:
        result.update(status="not_run", detail=str(error))
    return result


def installed_verdict(result, failed, observed=()):
    """The owner's rule: an installed Factory script seen in a QA terminal or transcript fails the run where the
    harness's skill discovery was switched off (failed), and is recorded for one without a switch (observed).
    Returns whether it failed."""
    if observed:
        result["installed_factory_observed"] = list(observed)
    if not failed:
        return False
    result.update(status="fail", installed_factory_seen=failed,
                  detail=f"an installed Factory script appeared in a QA terminal or transcript: {failed}")
    return True


def run(args, core, rodeo, skill, hc, live=None):
    if args.rehearse:
        result = rehearse(args, core, rodeo, skill, hc)
    else:
        result = run_real(args, live)
    payload = json.dumps(result, indent=2) + "\n"
    if args.json:
        args.json.write_text(payload)
        if "artifacts" in result:
            manifest = {**result["artifacts"], "trust_entries": result.get("trust_entries", [])}
            args.json.with_suffix(".artifacts.json").write_text(json.dumps(manifest, indent=2) + "\n")
    sys.stdout.write(payload)
    return {"pass": 0, "fail": 1, "not_run": 2}[result["status"]]
