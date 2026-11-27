"""Scratch repositories and evidence checks for the live autonomy runs."""

from dataclasses import dataclass, field
import os
import pathlib
import re
import subprocess
import sys
import tomllib


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
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
    count = re.search(r"(?m)^Ran ([0-9]+) tests? in ", result.stderr + "\n" + result.stdout)
    return int(count[1]) if result.returncode == 0 and count is not None else 0


def accepts(root, item):
    return subprocess.run([sys.executable, "-c", ACCEPTANCE[item - 1]], cwd=root, capture_output=True,
                          timeout=30).returncode == 0


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
    """The receipt can be written after the reported working turn began."""
    if not assignment(message, item) or message.terminal != terminal["address"] or before_claim is None:
        return False
    turn = terminal.get("turn")
    return (terminal.get("state") == "live" and (terminal.get("status") or {}).get("state") == "working"
            and type(turn) is int and type(message.turn) is int and turn == before_claim + 1
            and message.turn in (before_claim, turn))


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
    exact = rf"(?m)^status: approve; item {re.escape(ticket.item)}; commit {ticket.revision}(?:\s|$)"
    return next((msg for msg in messages if msg.sender == "reviewer" and msg.recipient == "operator"
                 and msg.kind == "say" and ordered(msg.delivered, ticket.integrated)
                 and re.search(exact, msg.text)), None)


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
    silence = one_quiet and evidence.quiet_seconds >= 15 * 60 and not any(
        msg.sender == "factory" and ordered(quiet[0].delivered, msg.committed, evidence.moved)
        for msg in evidence.messages)
    ready_note = len(ready) == 1 and evidence.ready_seconds <= 120
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
