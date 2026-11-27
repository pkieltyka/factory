"""Scratch repositories and evidence checks for the live autonomy runs."""

from dataclasses import dataclass
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
