"""Tests for factory.py with a fake `herdr` and fake harness binaries; nothing real is launched.

Two test classes touch real tools and skip when they cannot: HerdrContract reads the installed herdr's API
schema, and LiveFlags (FACTORY_LIVE=1, outside any sandbox) greps each installed harness's --help for the
flags factory.py passes."""
import importlib.util
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import types
import unittest
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

import live

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "skill/scripts/factory.py"
spec = importlib.util.spec_from_file_location("factory", SCRIPT)
factory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(factory)
os.environ["LOGNAME"] = "ada"  # whose team the tests run as: the first part of worktree branches
# Fixtures are written as `+++ settings +++ prose`; Env.write_brief puts them on disk as factory.toml and FACTORY.md.
TEMPLATE = ("+++\n" + (factory.SKILL / "templates/factory.toml").read_text().strip() + "\n+++\n"
            + (factory.SKILL / "templates/FACTORY.md").read_text())
GIT = shutil.which("git")
HC_FIXTURES = json.loads(pathlib.Path(__file__).with_name("hc_fixtures.json").read_text())


def conforms(value, spec, named_types, path="$"):
    kind = spec.get("type")
    if kind == "any" or (value is None and spec.get("nullable")):
        return []
    if "ref" in spec:
        if not isinstance(value, dict):
            return [f"{path}: expected {spec['ref']} object"]
        fields = named_types[spec["ref"]]
        missing = [f"{path}.{key}: required" for key in fields if fields[key].get("required") and key not in value]
        unknown = [f"{path}.{key}: unknown" for key in value if key not in fields]
        return missing + unknown + [problem for key in value.keys() & fields.keys()
                                    for problem in conforms(value[key], fields[key], named_types, f"{path}.{key}")]
    valid_types = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
                   "array": (list,), "object": (dict,)}
    if type(value) not in valid_types[kind]:
        return [f"{path}: expected {kind}"]
    if kind == "array":
        return [problem for i, item in enumerate(value)
                for problem in conforms(item, spec["items"], named_types, f"{path}[{i}]")]
    if "values" in spec:
        return [problem for key, item in value.items()
                for problem in conforms(item, spec["values"], named_types, f"{path}.{key}")]
    return []


RPC_ERROR_TYPES = {
    "Error": {"code": {"type": "integer", "required": True}, "message": {"type": "string", "required": True},
              "data": {"ref": "ErrorData", "required": True}},
    "ErrorData": {"kind": {"type": "string", "required": True}, "hint": {"type": "string"}},
}


class HcContract(unittest.TestCase):
    @staticmethod
    def check_result(method, value):
        schema = HC_FIXTURES["schema"]
        return conforms(value, {"ref": schema["results"][method]}, schema["types"])

    def test_recorded_replies_conform(self):
        for name, example in HC_FIXTURES["replies"].items():
            with self.subTest(name=name):
                self.assertEqual(self.check_result(example["method"], example["result"]), [])
        for kind, example in HC_FIXTURES["errors"].items():
            with self.subTest(kind=kind):
                self.assertEqual(conforms(example["error"], {"ref": "Error"}, RPC_ERROR_TYPES), [])
                self.assertEqual(example["error"]["data"]["kind"], kind)

    def test_validator_rejects(self):
        hello = HC_FIXTURES["replies"]["hello"]["result"]
        terminal = HC_FIXTURES["replies"]["terminal.get.live"]["result"]
        for method, value in (
            ("hello", {k: v for k, v in hello.items() if k != "version"}),
            ("hello", {**hello, "extra": True}),
            ("hello", {**hello, "version": None}),
            ("hello", {**hello, "generation": True}),
            ("terminal.get", {**terminal, "labels": {"team": 42}}),
            ("terminal.get", {**terminal, "records": {"agent": {"state": 42}}}),
        ):
            with self.subTest(method=method, value=value):
                self.assertTrue(self.check_result(method, value))

    def test_any_accepts_null(self):
        self.assertEqual(self.check_result("doc.get", {"value": None, "version": 0}), [])

    def test_wire_exceptions_are_applied(self):
        for rule in HC_FIXTURES["wire_exceptions"]:
            with self.subTest(type=rule["type"], field=rule["field"]):
                self.assertEqual(rule["version"], HC_FIXTURES["hc_version"])
                field = HC_FIXTURES["schema"]["types"][rule["type"]][rule["field"]]
                self.assertEqual(field["required"], rule["applied"]["required"])
        result = HC_FIXTURES["replies"]["terminal.list.all"]["result"]
        self.assertNotIn("machines", result)
        self.assertEqual(self.check_result("terminal.list", result), [])
        self.assertEqual(self.check_result("terminal.list", {**result, "machines": []}), [])
        self.assertTrue(self.check_result("terminal.list", {**result, "machines": [False]}))

FAKE_HERDR = textwrap.dedent(r'''
    #!/usr/bin/env python3
    # A herdr stand-in. Like herdr: `pane list` carries labels but no agent names, `agent list` carries names
    # but no labels; a pane keeps its label but loses its agent_session when its agent exits; a prompt to a
    # blocked agent is refused; errors are JSON on stderr with exit status 1.
    import json, os, sys, textwrap
    path = os.environ["FAKE_HERDR"]
    s = json.load(open(path))
    s["calls"].append(sys.argv[1:])
    a = sys.argv[1:]
    panes = s["panes"]
    def save(): json.dump(s, open(path, "w"))
    def out(result):
        save(); print(json.dumps({"id": "1", "result": result})); sys.exit(0)
    def fail(code, message=""):
        save(); print(json.dumps({"id": "1", "error": {"code": code, "message": message}}), file=sys.stderr); sys.exit(1)
    def find(target):
        return panes.get(target) or next((p for p in panes.values() if p.get("agent") and p.get("name") == target), None)
    def view(p, kind):
        hide = {"pane": ("name", "rect"), "agent": ("label", "rect")}[kind]
        return {k: v for k, v in p.items() if k not in hide}
    def opt(name):
        return a[a.index(name) + 1] if name in a else None
    def new_pane(tab, rect):
        s["next"] += 1
        pid = f"w1:p{s['next']}"
        panes[pid] = {"pane_id": pid, "tab_id": tab, "workspace_id": "w1", "label": None, "agent_status": "unknown",
                      "foreground_cwd": opt("--cwd"), "cwd": opt("--cwd"), "rect": rect, "env": [a[i + 1] for i, x in enumerate(a) if x == "--env"]}
        return panes[pid]
    if a == ["integration", "status"]:
        if os.environ.get("FAKE_INTEGRATION_FAIL"):
            fail("status_failed", "cannot read integration status")
        current = "\n".join(f"{h}: current (v1) (/hooks/{h})"
                            for h in ("claude", "codex", "pi", "omp", "opencode", "antigravity-cli"))
        save(); print(os.environ.get("FAKE_INTEGRATION_STATUS", current)); sys.exit(0)
    if a[:2] == ["agent", "list"]:
        if os.environ.get("FAKE_ROSTER_FAIL"):
            fail("roster_failed")
        change = s.get("on_second_poll")
        if change:
            s["polls"] = s.get("polls", 0) + 1
            if s["polls"] == 2:
                target = next(k for k, p in panes.items() if p.get("label") == change["name"])
                if change.get("close"):
                    del panes[target]
                elif change.get("exit"):
                    panes[target].update(agent=None, name=None, agent_status="unknown", agent_session=None)
                else:
                    panes[target]["agent_status"] = change["status"]

        out({"type": "agent_list", "agents": [view(p, "agent") for p in panes.values() if p.get("agent")]})
    if a[:2] == ["agent", "get"]:
        if os.environ.get("FAKE_GET_FAIL"):
            fail("PermissionDenied", "Operation not permitted: socket blocked")
        p = find(a[2])
        out({"type": "agent_info", "agent": view(p, "agent")}) if p and p.get("agent") else fail("agent_not_found")
    if a[:2] == ["agent", "rename"]:
        find(a[2])["name"] = a[3]; out({"type": "agent_info", "agent": view(find(a[3]), "agent")})
    if a[:2] == ["pane", "list"]:
        out({"type": "pane_list", "panes": [view(p, "pane") for p in panes.values()]})
    if a[:2] == ["pane", "get"]:
        p = panes.get(a[2])
        out({"type": "pane_info", "pane": view(p, "pane")}) if p else fail("pane_not_found")
    if a[:2] == ["pane", "close"]:
        if a[2] == os.environ.get("FAKE_CLOSE_FAIL"):
            fail("close_refused", "could not close pane")
        del panes[a[2]]
        change = s.pop("after_close", None)
        if change:
            panes[change["pane"]].update(change["fields"])
        out({"type": "pane_closed"})
    if a[:2] == ["pane", "rename"]:
        panes[a[2]]["label"] = None if a[3] == "--clear" else a[3]; out({"type": "pane_info", "pane": view(panes[a[2]], "pane")})
    if a[:2] == ["tab", "rename"]:
        s["tabs"][a[2]] = a[3]; out({"type": "tab_info", "tab": {"tab_id": a[2], "label": a[3]}})
    if a[:2] == ["tab", "create"]:
        tab = f"w1:t{len(s['tabs']) + 1}"
        s["tabs"][tab] = opt("--label")
        out({"type": "tab_created", "tab": {"tab_id": tab, "label": opt("--label")},
             "root_pane": view(new_pane(tab, {"x": 0, "y": 0, "width": 200, "height": 50}), "pane")})
    if a[:2] == ["pane", "split"]:
        r = panes[a[2]]["rect"]
        if opt("--direction") == "right":
            r["width"] //= 2; new = {**r, "x": r["x"] + r["width"]}
        else:
            r["height"] //= 2; new = {**r, "y": r["y"] + r["height"]}
        out({"type": "pane_info", "pane": view(new_pane(panes[a[2]]["tab_id"], new), "pane")})
    if a[:2] == ["pane", "layout"]:
        tab = panes[opt("--pane")]["tab_id"]
        out({"type": "pane_layout", "layout": {"tab_id": tab, "panes": [
            {"pane_id": p["pane_id"], "rect": p["rect"]} for p in panes.values() if p["tab_id"] == tab]}})
    if a[:2] == ["pane", "process-info"]:
        p = panes[opt("--pane")]
        procs = p.get("procs")  # a test's own foreground processes; else the harness as factory started it
        if procs is None:
            start = next((c for c in reversed(s["calls"]) if c[:2] == ["agent", "start"] and c[c.index("--pane") + 1] == p["pane_id"]), None)
            procs = [{"pid": 1, "name": p["agent"], "argv": [p["agent"], *start[start.index("--") + 1:]]}] if start and p.get("agent") else []
        out({"type": "pane_process_info", "process_info": {"pane_id": p["pane_id"], "foreground_processes": procs}})
    if a[:2] == ["pane", "read"]:
        asks = panes[a[2]].get("dialog", [""])[0]
        if width := int(os.environ.get("FAKE_DIALOG_WRAP", "0")):  # a narrow pane wraps the question
            asks = "\n".join(textwrap.wrap(asks, width))
        save(); print(f"...\n{asks}  [{a[2]}]"); sys.exit(0)  # plain text, like herdr
    if a[:2] == ["pane", "send-keys"]:
        if os.environ.get("FAKE_DIALOG_STUCK"):
            fail("input_failed", "keypress was not delivered")
        if panes[a[2]].pop("dialog", [0, 0])[1] == a[3:]:  # the keys that accept
            panes[a[2]]["agent_status"] = "idle"
        out({"type": "ok"})
    if a[:2] == ["agent", "start"]:
        name, kind, pane = a[2], opt("--kind"), opt("--pane")
        how = dict(x.split(":")[::-1] for x in os.environ.get("FAKE_START_FAIL", "").split(",") if x).get(name)
        if panes[pane].get("agent"):
            fail("pane_busy")
        if how == "dead":
            fail("timeout", "agent did not appear")
        panes[pane].update(agent=kind, name=name, agent_status="idle" if how in (None, "banner") else "blocked")
        if how == "dialog":  # a first-run question the harness table knows
            panes[pane]["dialog"] = {
                "codex": ["> 1. Trust and continue  2. Back", ["enter"]],
                "agy": ["Do you trust the contents of this project?\n> Yes, I trust this folder", ["enter"]],
            }.get(kind, ["> No, exit\n  Yes, I trust this folder", ["down", "enter"]])
            if os.environ.get("FAKE_DIALOG_TEXT"):  # a question the harness table does not know
                panes[pane]["dialog"][0] = os.environ["FAKE_DIALOG_TEXT"]
            if kind == "codex" or os.environ.get("FAKE_DIALOG_WRAP"):  # herdr 0.9.5 calls Codex, and agy in a
                panes[pane]["agent_status"] = how = "idle"  # narrow pane, ready while its trust question is up
        if how in ("blocked", "banner", "dialog"):  # a banner clears by itself: the agent is idle a moment later
            fail("agent_not_ready", "agent is waiting on a dialog")
        out({"type": "agent_started", "agent": view(panes[pane], "agent")})
    if a[:2] == ["agent", "prompt"]:
        p = find(a[2])
        if not p or not p.get("agent"):
            fail("agent_not_found")
        if p.get("label") == os.environ.get("FAKE_UNCONFIRMED"):
            out({})  # herdr returned without confirming the submission
        if p.get("agent_status") == "blocked" or p.get("label") in os.environ.get("FAKE_PROMPT_FAIL", "").split(","):
            fail("agent_blocked", "agent is waiting on an approval")
        out({"type": "agent_prompted", "agent": view(p, "agent")})
    fail("unexpected", " ".join(a))
''').lstrip()

# pi prints a table of configured models, filtered by the query like pi does.
FAKE_PI = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import os, sys
    rows = [l.split() for l in os.environ.get("FAKE_MODELS", "").splitlines() if l.strip()]
    q = sys.argv[-1].lower()
    hits = [r for r in rows if q in r[1].lower()]
    if not hits:
        print(f'No models matching "{q}"'); sys.exit(0)
    print("provider    model    context  max-out  thinking  images")
    for p, m in hits:
        print(f"{p}  {m}  1M  64K  yes  no")
''').lstrip()

# omp prints one box table per provider.
FAKE_OMP = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import collections, os, sys
    rows = [l.split() for l in os.environ.get("FAKE_MODELS", "").splitlines() if l.strip()]
    q = sys.argv[-1].lower()
    by = collections.defaultdict(list)
    for p, m in rows:
        if q in m.lower():
            by[p].append(m)
    if not by:
        print(f'No models matching "{q}"'); sys.exit(0)
    for p, ms in by.items():
        print(f"{p} ({len(ms)})\n┌───┬───┐\n│ model │ context │\n├───┼───┤")
        for m in ms:
            print(f"│ {m} │ 1M │")
        print("└───┴───┘")
''').lstrip()

FAKE_OPENCODE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import os, sys
    if sys.argv[1:] == ["models", "--standalone"]:
        for l in os.environ.get("FAKE_MODELS", "").splitlines():
            if l.strip():
                p, m = l.split(); print(f"{p}/{m}")
''').lstrip()

FAKE_AGY = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import sys
    if sys.argv[1:] != ["models"]:
        sys.exit(2)
    print("Fetching available models...")
    print("gemini-4-neon-high\tGemini 4 Neon (High)")
    print("gemini-4-neon-low\tGemini 4 Neon (Low)")
    print("gemini-pro-orchestrator-neon\tOrchestrator (Neon)")
''').lstrip()

MODELS = """\
openrouter z-ai/glm-5.3
openrouter z-ai/glm-5.3-flash
openrouter deepseek/deepseek-v4.1-flash-1017
openrouter openai/gpt-6-astra
pax-vllm nvidia/Qwen3.8-Flash-Next-NVFP4
"""

LEVELS = ["low", "medium", "high", "xhigh", "max"]
READY = "Send @operator one line saying `ready` with the say command in your standing instructions, then wait for your task."
CODEX_CACHE = {"models": [
    {"slug": "gpt-6.2-astra", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6.2-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6-luna", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
    {"slug": "gpt-6-astra", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6.1-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
    {"slug": "gpt-reserve", "visibility": "hide", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
]}


def team_brief(settings, body="## Workflow\n\n1. @operator hands out work.\n"):
    return "+++\n" + textwrap.dedent(settings).strip() + "\n+++\n" + body


def formation(members, **tables):
    """Ordinary fixture tables; syntax/error tests use literal TOML instead."""
    sections = {**tables, **{f"members.{name}": fields for name, fields in members.items()}}
    return team_brief("\n".join(f"[{name}]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in fields.items())
                                for name, fields in sections.items()))


def assert_todo_items(case, text):
    """Check ticket numbers, item spacing, and indentation of details and nested lists."""
    lines = text.splitlines()
    checkbox = ("- [ ]", "- [x]")
    numbers = set()
    in_item = False
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_item = False
        elif line.startswith(checkbox):
            match = re.match(r"^- \[[ x]\] \(([1-9][0-9]*)\) \S", line)
            case.assertIsNotNone(match, f"expected a numbered TODO item in {line!r}")
            number = match[1]
            case.assertNotIn(number, numbers, f"duplicate TODO number ({number})")
            numbers.add(number)
            if in_item:
                case.assertEqual(lines[i - 1], "", f"missing blank line before {line}")
                case.assertTrue(lines[i - 2].strip(), f"extra blank line before {line}")
            in_item = True
        elif in_item and line.strip():
            case.assertTrue(lines[i - 1].strip(), f"blank line inside item before {line}")
            if lines[i - 1].startswith(checkbox):
                case.assertRegex(line, r"^    \* \S")
            elif re.match(r"\s*(?:[-+*]|\d+[.)])(?:\s|$)", line):
                case.assertRegex(line, r"^(?:    \*|        (?:[-+*]|\d+[.)])) \S")
    case.assertTrue(numbers, "expected a TODO checkbox item")


CATALOG_MEMBERS = {
    "architect": {"harness": "claude", "model": "claude-fable-5-2", "thinking": "high"},
    "product": {"harness": "claude", "model": "claude-opus-5-5", "thinking": "medium"},
    "lead-dev": {"harness": "codex", "model": "gpt-6-astra", "thinking": "high"},
    "backend-dev": {"harness": "pi", "model": "z-ai/glm-5.3", "thinking": "medium"},
    "frontend-dev": {"harness": "claude", "model": "claude-opus-5-5", "thinking": "medium"},
    "designer": {"harness": "claude", "model": "claude-sonnet-5-5", "thinking": "high"},
    "reviewer": {"harness": "codex", "model": "gpt-6-luna", "thinking": "high"},
    "pm": {"harness": "pi", "model": "deepseek/deepseek-v4.1-flash-1017", "thinking": "low"},
    "qa-frontend": {"harness": "claude", "model": "claude-sonnet-5-5", "thinking": "medium"},
    "qa-backend": {"harness": "pi", "model": "z-ai/glm-5.3-flash", "thinking": "medium"},
}
CATALOG = formation(CATALOG_MEMBERS)
UI = formation({n: CATALOG_MEMBERS[n] for n in ("lead-dev", "frontend-dev", "reviewer", "pm")})


class Env:
    """A temp repo with a ten-role fixture brief by default, a fake herdr, fake harnesses,
    a Codex model cache, and the operator (a claude agent) in pane w1:p1 of tab w1:t1."""

    def __init__(self, brief=CATALOG, harnesses=factory.HARNESSES, models=MODELS, git=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex/models_cache.json").write_text(json.dumps(CODEX_CACHE))
        self.repo = self.root / "repo"
        (self.repo / "sub").mkdir(parents=True)
        if brief is not None:
            self.write_brief(brief)
        bin_ = self.root / "bin"
        bin_.mkdir()
        (bin_ / "python3").symlink_to(sys.executable)
        scripts = {"herdr": FAKE_HERDR, "pi": FAKE_PI, "omp": FAKE_OMP, "opencode": FAKE_OPENCODE, "agy": FAKE_AGY,
                   "claude": "#!/bin/sh\n", "codex": "#!/bin/sh\n"}
        for name, body in scripts.items():
            if name == "herdr" or name in harnesses:
                (bin_ / name).write_text(body)
                (bin_ / name).chmod(0o755)
        self.fake = self.root / "herdr.json"
        operator = {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "label": None, "agent": "claude",
                "name": None, "agent_status": "working", "rect": {"x": 0, "y": 0, "width": 200, "height": 50}}
        self.write_fake({"calls": [], "next": 1, "tabs": {"w1:t1": "1"}, "panes": {"w1:p1": operator}})
        self.env = {**os.environ, "HERDR_BIN": str(bin_ / "herdr"), "FAKE_HERDR": str(self.fake),
                    "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_TAB_ID": "w1:t1", "HERDR_WORKSPACE_ID": "w1",
                    "PATH": str(bin_), "HOME": str(self.home), "XDG_CACHE_HOME": str(self.root / "cache"),
                    "FAKE_MODELS": models, "FACTORY_SETTLE": "0.2"}
        self.env.pop("CODEX_HOME", None)
        if git:  # a real repo with one commit, for worktrees
            (bin_ / "git").symlink_to(GIT)
            for cmd in (["init", "-q", "-b", "main"], ["add", "-A"],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"]):
                subprocess.run([GIT, "-C", str(self.repo), *cmd], check=True, capture_output=True,
                               env={**self.env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def write_brief(self, text):
        """A fixture's settings become factory.toml and its prose FACTORY.md."""
        head, _, body = text.removeprefix("+++\n").partition("\n+++\n")
        (self.repo / "factory.toml").write_text(head + "\n")
        (self.repo / "FACTORY.md").write_text(body)

    def write_fake(self, s):
        self.fake.write_text(json.dumps(s))

    def fake_state(self):
        return json.loads(self.fake.read_text())

    def run(self, *args, check=True, cwd=None, **env):
        command = list(args)
        command[1:1] = ["--cwd", str(cwd or self.repo)]
        p = subprocess.run([sys.executable, str(SCRIPT), *command],
                           capture_output=True, text=True, env={**self.env, **env})
        if check and p.returncode:
            raise AssertionError(p.stderr + p.stdout)
        return p

    def say(self, sender, *args, **kwargs):
        return self.run("say", *args, HERDR_PANE_ID=self.pane_of(sender)["pane_id"], **kwargs)

    def calls(self, *prefix):
        return [c for c in self.fake_state()["calls"] if c[:len(prefix)] == list(prefix)]

    def starts(self):
        return {c[2]: c for c in self.calls("agent", "start")}

    def prompts(self):
        return [(c[2], c[3]) for c in self.calls("agent", "prompt")]

    def pane_of(self, name):
        return next(p for p in self.fake_state()["panes"].values() if p.get("label") == name)

    def update(self, pane_label, **fields):
        fs = self.fake_state()
        next(p for p in fs["panes"].values() if p.get("label") == pane_label).update(fields)
        self.write_fake(fs)

    def set_session(self, name, ref):
        self.update(name, agent_session=ref)

    def kill(self, *names):
        """Members' agents exit. Like herdr, their panes keep the label and lose the session reference;
        a command that looked at the team before (here: status) has noted the session."""
        self.run("status")
        fs = self.fake_state()
        for p in fs["panes"].values():
            if p.get("label") in names:
                p.update(agent=None, name=None, agent_status="unknown", agent_session=None)
        self.write_fake(fs)

    def close(self, *names):
        fs = self.fake_state()
        fs["panes"] = {k: p for k, p in fs["panes"].items() if p.get("label") not in names}
        self.write_fake(fs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()


def native(call):
    return call[call.index("--") + 1:]


def flag(args, name):
    return args[args.index(name) + 1]


def kind(call):
    return flag(call, "--kind")


class UpTests(unittest.TestCase):
    def test_up_warns_once_for_each_integration_it_will_use(self):
        members = {n: CATALOG_MEMBERS[n] for n in ("lead-dev", "reviewer", "pm", "frontend-dev")}
        members["designer"] = {"harness": "agy"}
        status = textwrap.dedent("""\
            claude: outdated (v2 < v3) (/hooks/claude)
            codex: not installed (/hooks/codex)
            pi: current (v2) (/hooks/pi)
            opencode: needs repair (v13) (/hooks/opencode)
            antigravity-cli: needs repair (v4) (/hooks/antigravity-cli)
            """)
        with Env(formation(members)) as e:
            p = e.run("up", "--fresh", "--no-brief", FAKE_INTEGRATION_STATUS=status)
            self.assertEqual(set(e.starts()), set(members))
            self.assertEqual(p.stderr.count("Codex herdr integration not installed"), 1)
            self.assertIn("Claude Code herdr integration outdated (v2 < v3)", p.stderr)
            self.assertIn("Antigravity CLI herdr integration needs repair (v4)", p.stderr)
            for target in ("claude", "codex", "antigravity-cli"):
                self.assertIn(f"Ask the user before running `herdr integration install {target}`", p.stderr)
            self.assertNotIn("pi herdr integration", p.stderr)
            self.assertNotIn("OpenCode", p.stderr)
            e.run("up", "--no-brief", FAKE_INTEGRATION_STATUS=status)  # already running: no query
            self.assertEqual(e.calls("integration"), [["integration", "status"]])
            calls = e.calls()
            self.assertLess(calls.index(["integration", "status"]), calls.index(e.calls("pane", "rename")[0]))

    def test_up_integration_status_failure_and_unknown_rows_only_warn(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]})
        cases = (
            ({"FAKE_INTEGRATION_FAIL": "1"}, "could not check herdr integrations (status_failed:"),
            ({"FAKE_INTEGRATION_STATUS": "pi: current (v2) (/hooks/pi)"}, "could not verify the Codex herdr integration"),
            ({"FAKE_INTEGRATION_STATUS": "codex: new state (v2) (/hooks/codex)"}, "could not verify the Codex herdr integration"),
        )
        for env, warning in cases:
            with self.subTest(env=env), Env(brief) as e:
                p = e.run("up", "--no-brief", **env)
                self.assertIn(warning, p.stderr)
                self.assertIn("Run `herdr integration status` to verify", p.stderr)
                self.assertNotIn("integration install", p.stderr)
                self.assertEqual(e.calls("integration"), [["integration", "status"]])
                self.assertIn("lead-dev", e.starts())

    def test_ui_team_launches_in_the_team_tab_and_is_briefed(self):
        with Env(UI) as e:
            self.assertEqual(factory.Config(e.repo).operator["name"], "operator")  # the default name
            e.run("up", cwd=e.repo / "sub")  # the team's files are found from a subdirectory too
            fs = e.fake_state()
            self.assertEqual((fs["panes"]["w1:p1"]["name"], fs["panes"]["w1:p1"]["label"]), ("operator", "operator"))
            self.assertEqual(fs["tabs"], {"w1:t1": "factory operator", "w1:t2": "factory team"})  # the operator's tab, and one for the team
            tab = e.calls("tab", "create")
            self.assertEqual(len(tab), 1)
            self.assertEqual((flag(tab[0], "--cwd"), flag(tab[0], "--workspace")), (str(e.repo), "w1"))
            self.assertTrue(all("--no-focus" in c and flag(c, "--cwd") == str(e.repo) for c in tab + e.calls("pane", "split")))
            labels = {p["label"]: (k, p["tab_id"]) for k, p in fs["panes"].items()}
            self.assertEqual(labels, {"operator": ("w1:p1", "w1:t1"), "lead-dev": ("w1:p2", "w1:t2"), "frontend-dev": ("w1:p3", "w1:t2"),
                                      "reviewer": ("w1:p4", "w1:t2"), "pm": ("w1:p5", "w1:t2")})
            sizes = {(p["rect"]["width"], p["rect"]["height"]) for p in fs["panes"].values() if p["tab_id"] == "w1:t2"}
            self.assertEqual(sizes, {(100, 25)})  # four members: a 2 x 2 grid

            starts = e.starts()
            self.assertEqual({n: kind(c) for n, c in starts.items()},
                             {"lead-dev": "codex", "frontend-dev": "claude", "reviewer": "codex", "pm": "pi"})
            cache = e.root / "cache/factory-skill"
            for name in ("lead-dev", "reviewer"):  # Codex: never stops to ask, never hard read-only
                argv = native(starts[name])
                self.assertEqual(flag(argv, "-m"), {"lead-dev": "gpt-6-astra", "reviewer": "gpt-6-luna"}[name])
                self.assertEqual(argv[-1], "--dangerously-bypass-approvals-and-sandbox")
                pointer = tomllib.loads(next(v for v in argv if v.startswith("developer_instructions=")))["developer_instructions"]
                self.assertIn(f"You are @{name}", pointer)
                prompt_file = pathlib.Path(pointer.split(" are in ")[1].split(". Read")[0])
                self.assertTrue(str(prompt_file).startswith(str(cache)) and prompt_file.exists())  # outside the repo
            fe = native(starts["frontend-dev"])
            settings = json.loads(pathlib.Path(flag(fe, "--settings")).read_text())
            self.assertIn(f"Bash(python3 {SCRIPT} say *)", settings["permissions"]["allow"])
            self.assertIn(f"python3 {SCRIPT} say *", settings["sandbox"]["excludedCommands"])
            self.assertNotIn("herdr agent prompt *", settings["sandbox"]["excludedCommands"])
            sock = os.environ.get("HERDR_SOCKET_PATH")
            expected_network = {"allowUnixSockets": [sock]} if sys.platform == "darwin" and sock else {"allowAllUnixSockets": True}
            self.assertEqual(settings["sandbox"]["network"], expected_network)
            self.assertEqual(flag(fe, "--permission-mode"), "bypassPermissions")  # the default never stops to ask
            self.assertTrue(settings["skipDangerousModePermissionPrompt"])  # nor at its first-run warning
            self.assertNotIn("--disallowedTools", fe)
            prompt = pathlib.Path(flag(fe, "--append-system-prompt-file")).read_text()
            self.assertIn(str(e.repo / "FACTORY.md"), prompt)
            self.assertNotIn("{instructions}", prompt)
            self.assertEqual(flag(native(starts["pm"]), "--model"), "openrouter/deepseek/deepseek-v4.1-flash-1017")

            status = e.run("status").stdout  # names come from herdr's agent list, not its pane list
            self.assertNotIn("unnamed", status)
            sent = dict(e.prompts())
            self.assertEqual(set(sent), {"lead-dev", "frontend-dev", "reviewer", "pm"})  # one message each
            self.assertTrue(sent["frontend-dev"].startswith("# Team brief (from @operator)"))  # its instructions are its system prompt
            self.assertTrue(sent["reviewer"].startswith("[from @operator]: Your standing instructions"))  # Codex gets them here too
            self.assertIn("You are read-only", sent["reviewer"])
            for text in sent.values():
                for needle in ("| **@lead-dev** | Codex, gpt-6-astra, high |", "## Workflow", READY):
                    self.assertIn(needle, text)
                self.assertNotIn("+++", text)
            self.assertEqual(sorted(p.name for p in e.repo.iterdir()), ["FACTORY.md", "factory.toml", "sub"])  # nothing else

    def test_ten_members_stay_a_balanced_grid(self):
        with Env() as e:
            e.run("up", "--no-brief")
            team = [p for p in e.fake_state()["panes"].values() if p["tab_id"] == "w1:t2"]
            self.assertEqual(len(team), 10)
            self.assertEqual(len(e.calls("tab", "create")), 1)
            areas = [p["rect"]["width"] * p["rect"]["height"] for p in team]
            self.assertLessEqual(max(areas) / min(areas), 2.2)  # no pane is squeezed: each split halves the largest
            self.assertGreaterEqual(min(p["rect"]["width"] for p in team), 50)
            self.assertGreaterEqual(min(p["rect"]["height"] for p in team), 12)

    def test_typed_command_fits_the_macos_line_limit(self):
        # herdr types `<harness> <argv>` into the pane's shell, and macOS limits that line to 1024 bytes.
        deep = "/Users/someone/Development/some-organization/a-rather-long-repository-name-for-a-project"
        with Env() as e:
            cfg = factory.Config(e.repo)
            cfg.root = pathlib.Path(deep)
            resume = {"agent": "codex", "kind": "id", "value": "01a1cbc6-e708-7623-986f-cc2e07efa28f"}
            for name in cfg.members:
                m = {**factory.plan(cfg, name), "permissions": "full", "branch": "ada/" + name}
                m["model"] = "openrouter/" + m["model"] if m["harness"] == "pi" else m["model"]
                argv, _ = factory.launch_argv(m, pathlib.Path("/Users/someone/.cache/factory-skill") / cfg.cache.name / cfg.session_dir.name / f"{name}.md", resume)
                self.assertLess(len(shlex.join([m["harness"], *argv]).encode("utf-8")), 900, name)

    def test_failed_check_launches_nothing_and_explains_the_fix(self):
        with Env(team_brief('''
            [members.backend-dev]
            harness = "pi"
            model = "z-ai/glm-9"
            [members.lead-dev]
            harness = "codex"
            model = "gpt-6-luna"
        ''')) as e:
            p = e.run("up", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("Cannot launch: 1 member(s)", p.stdout)
            self.assertIn("pi has no model z-ai/glm-9 configured", p.stdout)
            self.assertIn("OpenRouter key", p.stdout)
            self.assertEqual(e.calls(), [["agent", "get", e.env["HERDR_PANE_ID"]], ["agent", "list"], ["pane", "list"]])

    def test_up_can_be_run_again_after_a_partial_launch(self):
        with Env(UI) as e:
            p = e.run("up", check=False, FAKE_START_FAIL="dead:frontend-dev,blocked:pm,banner:reviewer,dialog:lead-dev")
            self.assertEqual(p.returncode, 3)
            self.assertIn("started @reviewer", p.stdout)  # a start-up notice that clears is not a failure
            self.assertIn("started @lead-dev", p.stdout)  # a question the harness table knows is answered
            self.assertEqual([c[3:] for c in e.calls("pane", "send-keys")], [["enter"]])
            self.assertIn("@frontend-dev did not come up: timeout: agent did not appear", p.stdout)  # herdr's reason is shown
            self.assertIn("factory.py up frontend-dev", p.stdout)
            self.assertIn("@pm launch incomplete: agent_not_ready", p.stdout)
            self.assertIn("Brief withheld.", p.stdout)
            self.assertEqual(sorted(e.starts()), ["frontend-dev", "lead-dev", "pm", "reviewer"])  # it went on past both
            self.assertEqual({n for n, _ in e.prompts()}, {"lead-dev", "reviewer"})  # only those that came up were briefed

            e.update("pm", agent_status="idle")  # the user answered pm's dialog
            before = len(e.prompts())
            out = e.run("up", "frontend-dev").stdout
            self.assertIn("started @frontend-dev", out)
            later = dict(e.prompts()[before:])
            self.assertIn("starting in a new session", later.pop("frontend-dev"))
            self.assertEqual(set(later), {"lead-dev", "reviewer", "pm"})
            for text in later.values():
                self.assertTrue(text.startswith("[from @operator]: Joined the team: @frontend-dev (frontend-dev, Claude Code"))
            self.assertEqual(len(e.calls("tab", "create")), 1)  # the failed member's pane was reused
            self.assertEqual(len([p for p in e.fake_state()["panes"].values() if p["label"] == "frontend-dev"]), 1)
            e.run("brief", "--send", "pm")
            self.assertIn("# Team brief", e.prompts()[-1][1])
            self.assertEqual(e.prompts()[-1][0], "pm")

    def test_startup_dialog_must_disappear_before_briefing(self):
        with Env() as e:
            result = e.run("up", "lead-dev", check=False,
                           FAKE_START_FAIL="dialog:lead-dev", FAKE_DIALOG_STUCK="1")
            self.assertEqual(result.returncode, 3)
            self.assertIn("still asks", result.stdout)
            self.assertIn("input_failed", result.stdout)
            self.assertEqual(e.prompts(), [])

    def test_wrapped_startup_question_is_answered_once_before_the_brief(self):
        # A narrow pane wraps agy's trust question and herdr calls agy ready. Unanswered, the question took the
        # Enter that ends the brief, and the brief was lost (gate-010).
        agy = formation({"pm": {"harness": "agy"}}, operator={"harness": "claude"})
        for width in ("27", "4"):
            with self.subTest(width=width), Env(agy) as e:
                e.run("up", FAKE_START_FAIL="dialog:pm", FAKE_DIALOG_WRAP=width)
                calls = e.fake_state()["calls"]
                keys = [i for i, c in enumerate(calls) if c[:2] == ["pane", "send-keys"]]
                prompts = [i for i, c in enumerate(calls) if c[:3] == ["agent", "prompt", "pm"]]
                self.assertEqual([calls[i][3:] for i in keys], [["enter"]])
                self.assertEqual(len(prompts), 1)
                self.assertLess(keys[0], prompts[0])
        with self.subTest("a question the table does not know"), Env(agy) as e:
            e.run("up", FAKE_START_FAIL="dialog:pm", FAKE_DIALOG_WRAP="27",
                  FAKE_DIALOG_TEXT="Do you trust the contents of this?")
            self.assertEqual(e.calls("pane", "send-keys"), [])
        with self.subTest("the question stays after its answer"), Env(agy) as e:
            result = e.run("up", check=False, FAKE_START_FAIL="dialog:pm", FAKE_DIALOG_WRAP="27", FAKE_DIALOG_STUCK="1")
            self.assertEqual(result.returncode, 3)
            self.assertIn("still asks", result.stdout)
            self.assertEqual(len(e.calls("pane", "send-keys")), 1)
            self.assertEqual(e.prompts(), [])

    def test_lead_detection_preserves_socket_error(self):
        with Env() as e:
            result = e.run("up", "lead-dev", check=False, FAKE_GET_FAIL="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Operation not permitted", result.stderr)
            self.assertNotIn("herdr sees no agent", result.stderr)
            self.assertEqual(e.starts(), {})

    def test_thinking_levels_are_guarded(self):
        cases = [
            ("claude", "claude-opus-5-5", "max", "not allowed"),
            ("codex", "gpt-6-astra", "ultra", "not allowed"),
            ("codex", "gpt-6.2-astra", "xhigh", None),
            ("codex", "gpt-6.2-sol", "xhigh", None),
            ("codex", "gpt-6-luna", "xhigh", None),
            ("pi", "z-ai/glm-5.3", "max", None),
            ("pi", "z-ai/glm-5.3", "turbo", "has no thinking level"),
        ]
        for harness, model, thinking, error in cases:
            with self.subTest(harness=harness, thinking=thinking), Env(formation({
                "lead-dev": {"harness": harness, "model": model, "thinking": thinking},
            })) as e:
                result = e.run("check", "lead-dev", check=False)
                self.assertEqual(result.returncode, 2 if error else 0)
                if error:
                    self.assertIn(error, result.stdout)

    def test_model_availability_and_missing_model(self):
        with Env(formation({
            'lead-dev': {'harness': 'codex', 'model': 'gpt-7-nova'},
            'frontend-dev': {'harness': 'claude', 'model': 'z-ai/glm-5.3'},
            'designer': {'harness': 'claude', 'model': 'claude-opus-9'},
            'pm': {'harness': 'claude'},
        })) as e:
            result = e.run("check", "lead-dev", "frontend-dev", "designer", "pm", check=False)
            out = result.stdout
            self.assertIn("Codex does not offer gpt-7-nova", out)
            self.assertNotIn("gpt-reserve", out)  # hidden Codex models are not offered
            self.assertIn("z-ai/glm-5.3 is not a Claude model", out)
            self.assertIn("claude-opus-9 is not among", result.stderr)
            self.assertIn("no model set", out)

    def test_unknown_claude_model_warns_without_blocking(self):
        with Env(formation({"reviewer": {"harness": "claude", "model": "claude-future-9"}})) as e:
            result = e.run("up", "reviewer")
            self.assertIn("claude-future-9 is not among", result.stderr)
            self.assertEqual(flag(native(e.starts()["reviewer"]), "--model"), "claude-future-9")

    def test_harness_not_installed(self):
        with Env(harnesses=("claude", "pi")) as e:
            out = e.run("check", "lead-dev", check=False).stdout
            self.assertIn("codex is not installed", out)
            self.assertIn("npm i -g @openai/codex", out)

    def test_local_model_and_ambiguous_providers(self):
        with Env(formation({
            'pm': {'harness': 'omp', 'model': 'nvidia/Qwen3.8-Flash-Next-NVFP4'},
            'backend-dev': {'harness': 'pi', 'model': 'z-ai/glm-5.3'},
            'qa-backend': {'harness': 'omp', 'model': 'z-ai/glm-5.3'},
        }), models=MODELS + "ollama z-ai/glm-5.3\n") as e:
            out = e.run("check", "pm", "backend-dev", "qa-backend", check=False).stdout
            self.assertIn("pax-vllm/nvidia/Qwen3.8-Flash-Next-NVFP4", out)  # omp's box table keeps the provider
            self.assertIn("pi serves z-ai/glm-5.3 from several providers: ollama/z-ai/glm-5.3, openrouter/z-ai/glm-5.3", out)
            self.assertIn("omp serves z-ai/glm-5.3 from several providers", out)

    def test_opencode_member_uses_its_config_and_standalone_server(self):
        with Env(formation({
            'reviewer': {'harness': 'opencode', 'model': 'z-ai/glm-5.3', 'thinking': 'high'},
        })) as e:
            e.run("up", "reviewer")
            self.assertEqual(native(e.starts()["reviewer"]), ["--standalone", "--auto"])
            content = json.loads(e.pane_of("reviewer")["env"][0].split("=", 1)[1])
            self.assertEqual(content, {"model": "openrouter/z-ai/glm-5.3#high"})
            name, text = e.prompts()[0]
            self.assertIn("You are @reviewer", text)  # OpenCode gets its instructions as a message
            self.assertIn("# Team brief", text)

    def test_agy_default_launch_trust_message_and_resume(self):
        with Env(formation({"pm": {"harness": "agy"}}, operator={"harness": "claude"})) as e:
            e.run("up", FAKE_START_FAIL="dialog:pm")
            args = ["--model", "gemini-4-neon-high", "--mode", "accept-edits",
                    "--settings", '{"toolPermission":"always-proceed"}']
            self.assertEqual(kind(e.starts()["pm"]), "agy")
            self.assertEqual(native(e.starts()["pm"]), args)
            self.assertEqual([c[3:] for c in e.calls("pane", "send-keys")], [["enter"]])
            brief = dict(e.prompts())["pm"]
            self.assertIn("You are @pm", brief)
            self.assertIn("Antigravity CLI, gemini-4-neon, high", brief)
            self.assertIn("factory.py say", brief)
            self.assertEqual(e.say("pm", "operator", "report").stdout.strip(), "queued message 1 for @operator")
            e.set_session("pm", {"agent": "agy", "kind": "id", "value": "conversation-94"})
            e.kill("pm")
            e.run("up", "pm")
            self.assertEqual(native(e.starts()["pm"]), args[:2] + ["--conversation", "conversation-94"] + args[2:])

    def test_agy_model_variants_and_guarded_permissions(self):
        cases = {"pm": ("gemini-4-neon", "low", "gemini-4-neon-low"),
                 "reviewer": ("gemini-4-neon-low", None, "gemini-4-neon-low"),
                 "designer": ("gemini-pro-orchestrator-neon", None, "gemini-pro-orchestrator-neon")}
        members = {name: {"harness": "agy", "model": model, "permissions": "guarded",
                          **({"thinking": level} if level else {})} for name, (model, level, _) in cases.items()}
        with Env(formation(members)) as e:
            e.run("up")
            for name, (_, _, wanted) in cases.items():
                self.assertEqual(native(e.starts()[name]), ["--model", wanted, "--mode", "accept-edits",
                                 "--settings", '{"toolPermission":"always-proceed"}'])
            brief = dict(e.prompts())["pm"]
            self.assertIn("gemini-4-neon-low, low", brief)
            self.assertIn("gemini-pro-orchestrator-neon, default", brief)
            self.assertIn("agy offers: gemini-4-neon-high, gemini-4-neon-low, gemini-pro-orchestrator-neon",
                          e.run("harnesses").stdout)

    def test_agy_preflight_rejects_missing_models_and_conflicting_levels(self):
        with Env(formation({
            "pm": {"harness": "agy", "model": "gemini-4-neon-low", "thinking": "high"},
            "reviewer": {"harness": "agy", "model": "gemini-4-neon", "thinking": "max"},
            "designer": {"harness": "agy", "model": "Gemini 4 Neon (High)"},
        })) as e:
            result = e.run("up", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(e.starts(), {})
            self.assertIn("gemini-4-neon-low has no thinking level 'high'", result.stdout)
            self.assertIn("Use one of: low.", result.stdout)
            self.assertIn("gemini-4-neon has no thinking level 'max'", result.stdout)
            self.assertIn("agy does not offer Gemini 4 Neon (High)", result.stdout)
        with Env(formation({"pm": {"harness": "agy"}}), harnesses=("claude",)) as e:
            self.assertIn("agy is not installed", e.run("check", check=False).stdout)

    def test_permissions_and_extra_args_come_from_the_brief(self):
        with Env(team_brief('''
            permissions = "guarded"
            [members.lead-dev]
            harness = "codex"
            model = "gpt-6-luna"
            args = ["--search"]
            [members.frontend-dev]
            harness = "claude"
            model = "claude-opus-5-5"
            [members.reviewer]
            harness = "codex"
            model = "gpt-6-astra"
            permissions = "full"
            [members.pm]
            harness = "omp"
            model = "z-ai/glm-5.3"
        ''')) as e:
            e.run("up", "lead-dev", "frontend-dev", "reviewer", "pm", "--no-brief", FAKE_START_FAIL="dialog:frontend-dev")
            starts = {n: native(c) for n, c in e.starts().items()}
            self.assertEqual(starts["lead-dev"][-4:], ["--approve-for-me", "-c", "sandbox_workspace_write.network_access=true", "--search"])
            self.assertNotIn("-s", starts["lead-dev"])  # --approve-for-me cannot be combined with -s
            self.assertEqual(flag(starts["frontend-dev"], "--permission-mode"), "auto")
            self.assertNotIn("network", json.loads(pathlib.Path(flag(starts["frontend-dev"], "--settings")).read_text())["sandbox"])
            self.assertEqual([c[3:] for c in e.calls("pane", "send-keys")], [["down", "enter"]])  # Claude's trust question
            self.assertEqual(starts["reviewer"][-1], "--dangerously-bypass-approvals-and-sandbox")
            self.assertEqual(starts["pm"][-2:], ["--approval-mode", "yolo"])  # omp has no guard that lets messages through
            e.write_brief(team_brief('''
            permissions = "yolo"
            [members.pm]
            harness = "claude"
        '''))
            self.assertIn("must be one of full, guarded", e.run("check", "pm", check=False).stderr)

    def test_claude_members_disable_prompt_suggestions_on_start_and_resume(self):
        members = {"frontend-dev": {**CATALOG_MEMBERS["frontend-dev"], "permissions": "full"},
                   "designer": {**CATALOG_MEMBERS["designer"], "permissions": "guarded"}}
        with Env(formation(members)) as e:
            e.run("up", "--no-brief")
            for name in members:
                e.set_session(name, {"agent": "claude", "kind": "id", "value": f"saved-{name}"})
            e.run("up", "--restart")
            for name in members:
                fresh, resumed = [native(call) for call in e.calls("agent", "start", name)]
                self.assertNotIn("--resume", fresh)
                self.assertEqual(flag(resumed, "--resume"), f"saved-{name}")
                for argv in (fresh, resumed):
                    self.assertIs(json.loads(pathlib.Path(flag(argv, "--settings")).read_text())["promptSuggestionEnabled"], False)

    def test_claude_socket_settings_follow_platform_and_permission_level(self):
        for platform in ("linux", "darwin"):
            for socket_path in ("", "/tmp/test-herdr.sock"):
                with self.subTest(platform=platform, socket=socket_path), patch.object(factory.sys, "platform", platform), \
                        patch.dict(os.environ, {"HERDR_SOCKET_PATH": socket_path}):
                    full = json.loads(factory.claude_settings("full"))["sandbox"]
                    expected = {"allowUnixSockets": [socket_path]} if platform == "darwin" and socket_path else {"allowAllUnixSockets": True}
                    self.assertEqual(full["network"], expected)
                    self.assertNotIn("network", json.loads(factory.claude_settings("guarded"))["sandbox"])

    def test_new_members_never_rename_existing_members(self):
        with Env() as e:
            e.run("up", "backend-dev", "pm", "--no-brief")
            first = e.pane_of("backend-dev")
            original_settings = factory.Config(e.repo).members["backend-dev"]
            original_prompt = pathlib.Path(flag(native(e.starts()["backend-dev"]), "--append-system-prompt"))
            original_text = original_prompt.read_text()
            for number in (2, 3):
                name = f"backend-dev-{number}"
                members = {**CATALOG_MEMBERS, **{f"backend-dev-{i}": {
                    "role": "backend-dev", "harness": "codex", "model": "gpt-6-luna",
                } for i in range(2, number + 1)}}
                e.write_brief(formation(members))
                original_brief = (e.repo / "factory.toml").read_bytes()
                self.assertIn(f"started @{name}", e.run("up", name).stdout)
                self.assertEqual((e.repo / "factory.toml").read_bytes(), original_brief)
                self.assertEqual(factory.Config(e.repo).members["backend-dev"], original_settings)
                self.assertEqual({k: e.pane_of("backend-dev")[k] for k in ("pane_id", "name", "label", "cwd")},
                                 {k: first[k] for k in ("pane_id", "name", "label", "cwd")})
                self.assertEqual(original_prompt.read_text(), original_text)
            self.assertEqual(e.calls("agent", "rename"), [["agent", "rename", "w1:p1", "operator"]])
            self.assertNotIn("backend-dev-1", factory.Config(e.repo).members)

    def test_existing_member_settings_only_come_from_the_brief(self):
        with Env(git=True) as e:
            original = (e.repo / "factory.toml").read_bytes()
            for args in (("--role", "reviewer"), ("--model", "gpt-6-luna"), ("--harness", "claude"),
                         ("--thinking", "low"), ("--worktree",), ("--team", "ui")):
                p = e.run("up", "lead-dev", *args, check=False)
                self.assertEqual(p.returncode, 2)
                self.assertIn("unrecognized arguments", p.stderr)
            self.assertEqual((e.repo / "factory.toml").read_bytes(), original)
            self.assertEqual(e.starts(), {})

    def test_up_reports_brief_delivery_failure_on_start_and_recovery(self):
        with Env() as e:
            p = e.run("up", "lead-dev", check=False, FAKE_PROMPT_FAIL="lead-dev")
            self.assertEqual(p.returncode, 3)
            self.assertIn("NOT delivered to @lead-dev: agent_blocked", p.stdout)
            self.assertEqual(len(e.prompts()), 1)
            e.kill("lead-dev")
            p = e.run("up", "lead-dev", check=False, FAKE_UNCONFIRMED="lead-dev")
            self.assertEqual(p.returncode, 3)
            self.assertIn("delivery unconfirmed to @lead-dev: unexpected reply from herdr", p.stdout)
            self.assertNotIn("brief --send", p.stdout)
            self.assertEqual(len(e.prompts()), 2)
            self.assertEqual(e.run("brief", "--send", "lead-dev").returncode, 0)

    def test_say_reports_who_did_not_get_the_message(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            note = e.root / "note.md"
            note.write_text("Priorities changed: ship the list view first.\n")
            e.run("say", "all", "--file", str(note))
            e.run("say", "@pm", "add a todo: ship it")
            sent = e.prompts()
            self.assertIn(("lead-dev", "[from @operator]: Priorities changed: ship the list view first.\n"), sent)
            self.assertIn(("pm", "[from @operator]: add a todo: ship it\n"), sent)
            self.assertIn("not running: reviewer", e.run("say", "reviewer", "hi", check=False).stdout)
            e.update("pm", agent_status="blocked")  # herdr refuses a prompt to an agent waiting on an approval
            p = e.run("say", "all", "status?", check=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("submitted your message to @lead-dev", p.stdout)
            self.assertIn("not sent to @pm: agent_blocked", p.stdout)

    def test_say_terminates_text_and_files_without_changing_the_payload(self):
        with Env() as e:
            e.run("up", "lead-dev", "--no-brief")
            note = e.root / "note.md"
            for message, expected in (("Ask @pm.", "Ask @pm.\n"),
                                      ("Read $herdr", "Read $herdr\n"),
                                      ("Keep this line.\nAsk @pm.", "Keep this line.\nAsk @pm.\n"),
                                      ("Ask @pm.\n", "Ask @pm.\n"),
                                      ("Keep trailing space. \n\n", "Keep trailing space. \n\n")):
                for source in ("text", "file"):
                    with self.subTest(message=message, source=source):
                        note.write_text(message)
                        args = (message,) if source == "text" else ("--file", str(note))
                        e.run("say", "lead-dev", *args)
                        self.assertEqual(e.prompts()[-1], ("lead-dev", "[from @operator]: " + expected))

    def test_delivery_preserves_the_briefs_final_newline(self):
        with Env() as e:
            e.run("up", "--no-brief")
            preview = e.run("brief").stdout
            e.run("brief", "--send", "frontend-dev")  # Claude gets the brief without a standing-instructions prefix
            self.assertEqual(dict(e.prompts())["frontend-dev"] + "\n", preview)  # CLI print adds one newline

    def test_member_draft_delivers_literal_verdict_and_retries_only_refused_peer(self):
        members = {**CATALOG_MEMBERS, "reviewer": {**CATALOG_MEMBERS["reviewer"], "worktree": True}}
        with Env(formation(members), git=True) as e:
            e.run("up", "reviewer", "lead-dev", "pm", "--no-brief")
            member = {**e.env, "HERDR_PANE_ID": e.pane_of("reviewer")["pane_id"]}
            prompt = e.run("prompt", "reviewer").stdout
            send, = re.findall(r"```bash\n([^\n]+)\n```", prompt)
            directory = pathlib.Path(shlex.split(send)[-1]).parent
            self.assertIn(f"new file in `{directory}`", prompt)
            self.assertIn("Never overwrite a pending draft", prompt)
            def run(command):
                return subprocess.run([shutil.which("bash"), "-c", command], cwd=e.pane_of("reviewer")["cwd"],
                                      env=member, text=True, capture_output=True, check=False)
            draft = directory / "lead-dev-review-105-1.md"
            other = directory / "pm-review-105-2.md"
            text = ('Review: "the reviewer\'s patch" keeps $HOME and `status` literal.\n' * 11) + "Ask @pm"
            draft.write_text(text)
            other.write_text("Second verdict")
            calls = len(e.calls("agent", "rename"))
            baseline = e.prompts()
            old = "herdr agent prompt lead-dev '[from @reviewer]: " + "old verdict " * 55
            self.assertNotEqual(run(old).returncode, 0)
            self.assertEqual(e.prompts(), baseline)
            e.update("pm", agent_status="blocked")
            first = run(send.replace(" NAME ", " all ").replace("FILENAME", draft.name))
            self.assertNotEqual(first.returncode, 0, first.stderr)
            expected = "[from @reviewer]: " + text + "\n"
            self.assertEqual(e.prompts().count(("lead-dev", expected)), 1)
            self.assertNotIn(("reviewer", expected), e.prompts())
            self.assertEqual(draft.read_text(), text)
            retry = next(line.split(": ", 1)[1] for line in first.stdout.splitlines() if line.startswith("Retry once"))
            self.assertNotIn("lead-dev", shlex.split(retry))
            e.update("pm", agent_status="idle")
            self.assertEqual(run(retry).returncode, 0)
            self.assertFalse(draft.exists())
            self.assertTrue(other.exists())
            self.assertNotEqual(run(retry).returncode, 0)
            self.assertEqual(e.prompts().count(("lead-dev", expected)), 1)
            self.assertEqual(e.prompts()[-1], ("pm", expected))
            self.assertEqual(len(e.calls("agent", "rename")), calls)
            member["FAKE_UNCONFIRMED"] = "pm"
            uncertain = run(send.replace(" NAME ", " pm ").replace("FILENAME", other.name))
            self.assertNotEqual(uncertain.returncode, 0)
            self.assertIn("delivery unconfirmed to @pm", uncertain.stdout)
            self.assertNotIn("Retry once", uncertain.stdout)
            self.assertTrue(other.exists())

    def test_session_local_drafts_instructions_settings_and_cold_start(self):
        with Env(formation({"architect": CATALOG_MEMBERS["architect"]}, operator={"harness": "claude"})) as e:
            fresh = e.fake.read_bytes()
            drafts, instructions = [], []
            for session in ("team/one", "team-one"):  # identical readable prefix and roster
                e.fake.write_bytes(fresh)
                e.run("up", "--no-brief", HERDR_SESSION=session)
                launch = e.starts()["architect"]
                prompt = pathlib.Path(flag(launch, "--append-system-prompt-file"))
                instructions.append((prompt, prompt.read_text()))
                settings = json.loads(pathlib.Path(flag(launch, "--settings")).read_text())
                directory = pathlib.Path(settings["permissions"]["additionalDirectories"][0])
                self.assertIn(str(directory), prompt.read_text())
                self.assertIn(str(directory), settings["sandbox"]["filesystem"]["allowWrite"])
                self.assertIn(f"python3 {SCRIPT} say *", settings["sandbox"]["excludedCommands"])
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                path = directory / "message-same.md"
                path.write_text(session)
                drafts.append(path)
            first, second = drafts
            self.assertNotEqual(first, second)
            self.assertNotEqual(instructions[0][0], instructions[1][0])
            for path, original in instructions:
                self.assertEqual(path.read_text(), original)
            e.kill("architect")
            cold = e.run("up", "--no-brief", HERDR_SESSION="team-one").stdout
            self.assertIn(str(second), cold)
            self.assertNotIn(str(first), cold)
            e.say("architect", "operator", "--file", str(first), HERDR_SESSION="team-one")
            self.assertTrue(first.exists())  # an external --file is never consumed
            e.say("architect", "operator", "--file", str(second), HERDR_SESSION="team-one")
            self.assertFalse(second.exists())
            self.assertTrue(first.exists())

    def test_cold_up_warns_about_unreadable_drafts_and_lists_readable_siblings(self):
        members = {name: CATALOG_MEMBERS[name] for name in ("architect", "reviewer")}
        with Env(formation(members)) as e, patch.dict(os.environ, e.env), \
                patch.object(factory, "HERDR", e.env["HERDR_BIN"]):
            drafts = factory.Config(e.repo).messages
            unreadable = drafts / "architect"
            readable = drafts / "reviewer" / "pending-review.md"
            unreadable.mkdir(parents=True)
            readable.parent.mkdir()
            readable.write_text("A pending review")
            iterdir = pathlib.Path.iterdir

            def scan(path):
                if path == unreadable:
                    raise PermissionError("permission denied")
                return iterdir(path)

            out, err = io.StringIO(), io.StringIO()
            with patch.object(pathlib.Path, "iterdir", scan), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                factory.main(["up", "--cwd", str(e.repo), "--no-brief"])

            self.assertIn(f"warning: cannot scan drafts in {unreadable}: permission denied", err.getvalue())
            self.assertIn(str(readable), out.getvalue())
            self.assertIn("leftover draft for @reviewer", out.getvalue())
            self.assertEqual(set(e.starts()), set(members))
            self.assertEqual(readable.read_text(), "A pending review")

    def test_brief_send_all_matches_implicit_broadcast(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            e.run("brief", "--send")
            expected = e.prompts()
            self.assertEqual([name for name, text in expected], ["lead-dev", "pm"])
            e.run("brief", "--send", "all")
            self.assertEqual(e.prompts(), expected + expected)

    def test_member_queues_for_configured_operator_without_a_pane(self):
        with Env(formation({"reviewer": CATALOG_MEMBERS["reviewer"]}, operator={"name": "lead"})) as e:
            e.run("up", "--no-brief")
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                draft = cfg.messages / "reviewer/message-one.md"
                inbox = factory.Inbox(cfg)
            text = "Reviewer's report with `literal` $ and a final @lead"
            draft.write_text(text)
            e.close("lead")
            self.assertIn("queued message 1 for @lead", e.say("reviewer", "lead", "--file", str(draft)).stdout)
            self.assertFalse(e.prompts())
            self.assertFalse(draft.exists())
            row = json.loads(inbox.path.read_text())
            self.assertEqual((row["from"], row["text"]), ("reviewer", text))


    def test_draft_name_validation_is_scoped_to_the_sender_folder(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            with patch.dict(os.environ, e.env):
                directory = factory.Config(e.repo).messages / "reviewer"
            for name in ("review notes.md", "review_name.txt", "review.one.txt", "review", "review$.md"):
                draft = directory / name
                draft.write_text("verdict")
                result = e.say("reviewer", "operator", "lead-dev", "--file", str(draft), check=False)
                self.assertIn("letters, digits and dashes", result.stderr)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(draft.exists())
            self.assertFalse(e.prompts())
            self.assertNotIn("unread", e.run("status").stdout)
            external = e.root / "release notes.md"
            external.write_text("preserved")
            e.run("say", "lead-dev", "--file", str(external))
            self.assertEqual(e.prompts(), [("lead-dev", "[from @operator]: preserved\n")])
            self.assertTrue(external.exists())

    def test_say_recipient_lists_accept_spaces_and_commas(self):
        with Env(UI) as e:
            e.run("up", "reviewer", "lead-dev", "--no-brief")
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                inbox = factory.Inbox(cfg)
                directory = cfg.messages / "reviewer"
            message = "@operator Please record the verdict. @lead-dev Please check the fix."
            for i, targets in enumerate((("operator", "lead-dev"), ("operator,lead-dev",), ("all",)), 1):
                with self.subTest(targets=targets):
                    draft = directory / f"multi-{i}.md"
                    draft.write_text(message)
                    result = e.say("reviewer", *targets, "--file", str(draft))
                    self.assertIn("submitted your message to @lead-dev", result.stdout)
                    if targets != ("all",):
                        self.assertIn(f"queued message {i} for @operator", result.stdout)
                    self.assertFalse(draft.exists())
            self.assertEqual(e.prompts(), [("lead-dev", "[from @reviewer]: " + message + "\n")] * 3)
            self.assertEqual([row["text"] for row in map(json.loads, inbox.path.read_text().splitlines())], [message] * 2)

    def test_mixed_send_reports_each_target_without_replaying_a_queue(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                draft = cfg.messages / "reviewer/mixed-one.txt"
                inbox = factory.Inbox(cfg)
            draft.write_text("first verdict")
            e.update("pm", agent_status="blocked")
            result = e.say("reviewer", "operator", "pm", "--file", str(draft), check=False)
            self.assertIn("queued message 1 for @operator", result.stdout)
            self.assertIn("not sent to @pm: agent_blocked", result.stdout)
            self.assertEqual(result.returncode, 1)
            retry = shlex.split(next(line.split(": ", 1)[1] for line in result.stdout.splitlines()
                                     if line.startswith("Retry once")))
            self.assertEqual(retry[-3:], ["pm", "--file", str(draft)])
            self.assertTrue(draft.exists())
            e.update("pm", agent_status="idle")
            env = {**e.env, "HERDR_PANE_ID": e.pane_of("reviewer")["pane_id"]}
            self.assertEqual(subprocess.run(retry, env=env, capture_output=True, text=True, check=False).returncode, 0)
            self.assertFalse(draft.exists())
            self.assertEqual([row["id"] for row in map(json.loads, inbox.path.read_text().splitlines())], [1])
            self.assertEqual(e.prompts()[-1], ("pm", "[from @reviewer]: first verdict\n"))

            draft.write_text("second verdict")
            e.update("pm", agent_status="blocked")
            result = e.say("reviewer", "operator", "pm", "lead-dev", "--file", str(draft),
                           check=False, FAKE_UNCONFIRMED="lead-dev")
            self.assertEqual(result.returncode, 1)
            self.assertIn("queued message 2 for @operator", result.stdout)
            self.assertIn("not sent to @pm: agent_blocked", result.stdout)
            self.assertIn("delivery unconfirmed to @lead-dev", result.stdout)
            self.assertNotIn("Retry once", result.stdout)
            self.assertEqual(draft.read_text(), "second verdict")

    def test_herdr_required_and_brief_missing(self):
        with Env() as e:
            self.assertEqual(e.run("status", "--all", check=False).returncode, 2)
            for command in ("up", "check", "prompt"):
                missing = e.run(command, "ghost", check=False)
                self.assertEqual(missing.returncode, 1)
                self.assertIn("not a member", missing.stderr)
                self.assertNotIn("Traceback", missing.stderr)
            status = e.run("status").stdout
            self.assertEqual(status.count("not started"), len(CATALOG_MEMBERS) + 1)
            self.assertIn("No member of factory.toml has a pane in this herdr session. "
                          "Run `factory.py up` to start or resume its members.", status)
            for command in ("cost", "show"):
                self.assertEqual(e.run(command, check=False).returncode, 2)
            for option in ("--dry-run", "--trust-models", "--effort"):
                self.assertEqual(e.run("up", "lead-dev", option, check=False).returncode, 2)
            for cmd in (["up", "pm"], ["status"], ["wait"]):
                self.assertIn("not inside herdr", e.run(*cmd, check=False, HERDR_ENV="").stderr)
        with Env(brief=None) as e:
            self.assertIn("Run `/factory setup`", e.run("check", "pm", check=False).stderr)


@unittest.skipUnless(GIT, "git is not installed")
class WorktreeTests(unittest.TestCase):
    BRIEF = '''
        [members.backend-dev]
        harness = "codex"
        role = "backend-dev"
        model = "gpt-6-luna"
        worktree = true
        [members.backend-dev-2]
        harness = "pi"
        role = "backend-dev"
        model = "z-ai/glm-5.3"
        worktree = "feature/login"
        [members.frontend-dev]
        harness = "claude"
        model = "claude-opus-5-5"
        worktree = "feature/login"
        [members.reviewer]
        harness = "codex"
        model = "gpt-6-astra"
    '''

    def test_worktree_members_get_their_own_checkout_and_commit_there(self):
        with Env(team_brief(self.BRIEF), git=True) as e:
            out = e.run("up", "backend-dev", "backend-dev-2", "frontend-dev", "reviewer").stdout
            trees = e.root / "repo.worktrees"
            self.assertEqual(sorted(p.name for p in trees.iterdir()), ["ada-backend-dev", "feature-login"])  # one is shared
            self.assertEqual(out.count("created worktree"), 2)
            branch = lambda d: subprocess.run([GIT, "-C", str(d), "rev-parse", "--abbrev-ref", "HEAD"],
                                              capture_output=True, text=True).stdout.strip()
            self.assertEqual((branch(trees / "ada-backend-dev"), branch(trees / "feature-login")),
                             ("ada/backend-dev", "feature/login"))
            where = {n: e.pane_of(n)["cwd"] for n in ("backend-dev", "backend-dev-2", "frontend-dev", "reviewer")}
            self.assertEqual(where, {"backend-dev": str(trees / "ada-backend-dev"), "backend-dev-2": str(trees / "feature-login"),
                                     "frontend-dev": str(trees / "feature-login"), "reviewer": str(e.repo)})
            prompt = e.run("prompt", "backend-dev").stdout
            self.assertIn("Commit your work on `ada/backend-dev`", prompt)
            self.assertIn("`git merge main`", prompt)
            self.assertIn("agree who commits it for you", prompt)
            self.assertNotIn("Never run `git commit`", prompt)
            staging = "Stage only task files, never `git add -A` or `git add .`"
            self.assertIn(staging, prompt)
            self.assertNotIn("Do not stage files", prompt)
            shared = e.run("prompt", "reviewer").stdout
            self.assertIn("Do not stage files. Never run `git commit`, `git merge` or `git push`. "
                          "@operator stages only task files and commits.", shared)
            self.assertNotIn(staging, shared)
            brief = next(t for n, t in e.prompts() if n == "reviewer")
            self.assertIn("| branch `feature/login` in `" + str(trees / "feature-login") + "` |", brief)
            self.assertIn("`git diff main...<branch>`", brief)
            self.assertIn("| the main checkout |", brief)
            e.kill("backend-dev")
            self.assertNotIn("created worktree", e.run("up", "backend-dev").stdout)  # the checkout is kept

    def test_new_worktree_branch_passes_no_track(self):
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "branch.autoSetupMerge", "GIT_CONFIG_VALUE_0": "always"}
        with Env(team_brief(self.BRIEF), git=True) as e, patch.dict(os.environ, {**e.env, **env}):
            cfg = factory.Config(e.repo)
            member = factory.plan(cfg, "backend-dev")
            config = e.repo / ".git/config"
            before = config.read_bytes()
            with patch.object(factory.subprocess, "run", wraps=subprocess.run) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(factory.ensure_worktree(cfg, member))
                self.assertEqual(run.call_args.args[0], [
                    "git", "-C", str(e.repo), "worktree", "add", "-b", "ada/backend-dev", "--no-track", member["dir"],
                ])
            self.assertEqual(config.read_bytes(), before)

    def test_numbered_worktree_and_no_repository(self):
        with Env(formation({**CATALOG_MEMBERS, "reviewer-2": {
            "role": "reviewer", "harness": "codex", "model": "gpt-6-luna", "worktree": True,
        }}), git=True) as e:
            e.run("up", "reviewer-2", "--no-brief")  # the existing reviewer keeps its name
            self.assertTrue((e.root / "repo.worktrees/ada-reviewer-2").is_dir())
            self.assertIs(factory.Config(e.repo).members["reviewer-2"]["worktree"], True)
        with Env(team_brief(self.BRIEF)) as e:  # not a git repository
            p = e.run("up", "backend-dev", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("a worktree needs a git repository with a commit", p.stdout)
            self.assertEqual(e.calls(), [["agent", "get", e.env["HERDR_PANE_ID"]], ["agent", "list"], ["pane", "list"]])

    def test_distinct_branches_cannot_share_a_worktree_path(self):
        brief = formation({
            'backend-dev': {'harness': 'codex', 'model': 'gpt-6-luna', 'worktree': 'feature/a'},
            'backend-dev-2': {'role': 'backend-dev', 'harness': 'codex', 'model': 'gpt-6-luna', 'worktree': 'feature-a'},
        })
        with Env(brief, git=True) as e:
            for command in ("check", "up"):
                result = e.run(command, "backend-dev", "backend-dev-2", check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                for needle in ("worktree", "feature/a", "feature-a"):
                    self.assertIn(needle, result.stdout)
            self.assertEqual(e.starts(), {})
            self.assertFalse((e.root / "repo.worktrees").exists())

    def test_check_rejects_unregistered_or_wrong_branch_directory(self):
        for existing in ("directory", "wrong-branch"):
            with self.subTest(existing=existing), Env(git=True) as e:
                e.write_brief(CATALOG.replace('[members.lead-dev]', '[members.lead-dev]\nworktree = true'))
                target = e.root / "repo.worktrees/ada-lead-dev"
                if existing == "directory":
                    target.mkdir(parents=True)
                else:
                    subprocess.run([GIT, "-C", str(e.repo), "worktree", "add", "-b", "unrelated", str(target)],
                                   check=True, capture_output=True, env=e.env)
                result = e.run("up", "lead-dev", check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("worktree", result.stdout)
                self.assertEqual(e.starts(), {})

    def test_branch_names_git_rejects_fail_the_check(self):
        team = ('[members.reviewer]\nharness = "codex"\nmodel = "gpt-6-luna"\n'
                '[members.lead-dev]\nharness = "codex"\nmodel = "gpt-6-luna"\nworktree = {}\n')
        cases = [(f'user = "{u}"\n', "true") for u in (".ada", "ada..x", "ada.lock", "-ada")] + [("", '"feature//x"'), ("", '"x.lock"')]
        for settings, worktree in cases:
            with self.subTest(settings=settings, worktree=worktree), Env(team_brief(settings + team.format(worktree)), git=True) as e:
                for command in ("check", "up"):
                    p = e.run(command, check=False)
                    self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                    self.assertIn("is not a branch name git accepts", p.stdout)
                    self.assertIn("Change `user`, or this member's `worktree`", p.stdout)
                self.assertEqual(e.starts(), {})  # the reviewer, checked first, was not started either
        with Env(team_brief(team.format("true")), git=True) as e:  # a running member is not closed for a bad restart
            e.run("up", "--no-brief")
            pane = e.pane_of("lead-dev")["pane_id"]
            e.write_brief(team_brief('user = ".ada"\n' + team.format("true")))
            p = e.run("up", "lead-dev", "--restart", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertEqual(e.calls("pane", "close"), [])
            self.assertEqual(e.pane_of("lead-dev")["pane_id"], pane)

    def test_up_moves_member_to_newly_configured_worktree(self):
        with Env(git=True) as e:
            e.run("up", "lead-dev", "--no-brief")
            old_id = e.pane_of("lead-dev")["pane_id"]
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "saved-codex"})
            e.kill("lead-dev")
            e.write_brief(CATALOG.replace('[members.lead-dev]', '[members.lead-dev]\nworktree = true'))
            e.run("up", "lead-dev")
            current = e.pane_of("lead-dev")
            self.assertNotEqual(current["pane_id"], old_id)
            self.assertEqual(current["cwd"], str(e.root / "repo.worktrees/ada-lead-dev"))
            self.assertIsNone(e.fake_state()["panes"][old_id]["label"])
            self.assertEqual(native(e.starts()["lead-dev"])[:2], ["resume", "saved-codex"])
            # Codex restores its saved tool cwd unless explicitly overridden, even in a new pane.
            self.assertEqual(flag(native(e.starts()["lead-dev"]), "-C"), current["cwd"])

    def test_up_uses_foreground_directory_when_shell_moved(self):
        with Env(git=True) as e:
            e.run("up", "lead-dev", "--no-brief")
            old_id = e.pane_of("lead-dev")["pane_id"]
            e.kill("lead-dev")
            e.update("lead-dev", foreground_cwd=str(e.repo / "sub"))
            e.run("up", "lead-dev")
            self.assertNotEqual(e.pane_of("lead-dev")["pane_id"], old_id)
            self.assertEqual(e.pane_of("lead-dev")["cwd"], str(e.repo))


class InboxTests(unittest.TestCase):
    def prepare(self, e):
        with patch.dict(os.environ, e.env), factory.Inbox(factory.Config(e.repo)) as inbox:
            inbox.save_state()
            return inbox

    def send(self, e, text="done", **options):
        return e.say("lead-dev", "operator", text, **options)

    def seed(self, e, text="done", sender="lead-dev"):
        with patch.dict(os.environ, e.env):
            outcome, number = factory.queue_message(factory.Config(e.repo), sender, text)
        self.assertEqual(outcome, "queued", number)
        return number

    def invoke(self, e):
        with patch.dict(os.environ, e.env):
            return factory.queue_message(factory.Config(e.repo), "lead-dev", "literal '$herdr' @pm.")

    def test_inbox_and_generated_text_for_every_member_harness(self):
        members = {"lead-dev": {**CATALOG_MEMBERS["lead-dev"], "permissions": "guarded"},
                   "architect": CATALOG_MEMBERS["architect"], "pm": CATALOG_MEMBERS["pm"],
                   "reviewer": {"harness": "omp", "model": "openai/gpt-6-astra", "thinking": "high"},
                   "backend-dev": {"harness": "opencode", "model": "openrouter/z-ai/glm-5.3", "thinking": "high"}}
        with Env(formation(members, operator={"harness": "claude"})) as e:
            self.assertIn("through the inbox", e.run("up").stdout)
            for name in members:
                prompt = e.run("prompt", name).stdout
                self.assertIn("including @operator", prompt)
                self.assertIn("Report queued for @<requester>", prompt)
                self.assertIn("queued message N", prompt)
                self.assertIn(f"say --cwd {e.repo} NAME --file", prompt)
                self.assertNotIn("inbox send", prompt)
            brief = e.run("brief").stdout
            self.assertIn("factory.py say", brief)
            for name, settings in members.items():
                e.set_session(name, {"agent": settings["harness"], "kind": "id", "value": f"saved-{name}"})
            e.run("up", "--restart")
            for name, text in e.prompts()[-len(members):]:
                self.assertIn("Send it with the say command", text, name)
                self.assertNotIn("Send it: `herdr agent prompt operator", text)
            starts = {c[2]: c for c in e.calls("agent", "start")}
            claude = starts["architect"]
            settings = json.loads(pathlib.Path(claude[claude.index("--settings") + 1]).read_text())
            prefix = f"python3 {SCRIPT} say *"
            self.assertIn(prefix, settings["sandbox"]["excludedCommands"])
            self.assertIn(f"Bash({prefix})", settings["permissions"]["allow"])
            codex = starts["lead-dev"]
            with patch.dict(os.environ, e.env):
                cache = str(factory.Config(e.repo).cache)
            self.assertEqual(codex[codex.index("--add-dir") + 1], cache)
            self.assertIn("Use this command for teammates", e.run("prompt", "lead-dev", HERDR_ENV="").stdout)

    def test_inbox_and_instructions_ignore_configured_harness(self):
        with Env(UI) as e:
            prompt, brief = e.run("prompt", "lead-dev").stdout, e.run("brief").stdout
            unset = e.run("up").stdout
            self.assertIn("through the inbox", unset)
            self.assertIn("wait in a native background task", unset)
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "saved"})
            e.run("up", "lead-dev", "--restart")
            closing = dict(e.prompts())["lead-dev"].split("We are picking up")[1]
            e.write_brief(formation({n: CATALOG_MEMBERS[n] for n in ("lead-dev", "frontend-dev", "reviewer", "pm")},
                                    operator={"harness": "codex"}))
            e.update("operator", agent="codex")
            e.run("up", "lead-dev", "--restart")
            self.assertEqual(prompt, e.run("prompt", "lead-dev").stdout)
            # The Roles row changes with configuration; the generated messaging sentence does not.
            self.assertEqual(brief.split("## Talking to each other")[1],
                             e.run("brief").stdout.split("## Talking to each other")[1])
            self.assertEqual(closing, dict(e.prompts())["lead-dev"].split("We are picking up")[1])
            e.write_brief(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"}))
            e.update("operator", agent="codex")
            self.assertIn("wait in the foreground", e.run("up").stdout)
            e.update("operator", agent="claude")
            self.assertIn("wait in a native background task", e.run("up").stdout)
            e.write_brief(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "unknown"}))
            self.assertIn("[operator] harness 'unknown' is not one of", e.run("check", check=False).stderr)
        self.assertNotIn("inbox", factory.claude_settings("guarded"))

    def test_stale_operator_sample_keeps_invoking_codex_pane(self):
        members = {"architect": CATALOG_MEMBERS["architect"]}
        with Env(formation(members, operator={"harness": "claude"})) as e:
            local = e.repo / factory.CONF
            sample = e.repo / factory.SAMPLE
            local.rename(sample)
            e.run("init")
            e.update(None, agent="codex")
            stale = e.run("up").stdout
            self.assertIn("through the inbox", stale)
            self.assertIn("wait in the foreground", stale)
            self.assertIn("not qualified for unattended idle delivery", stale)
            self.assertEqual(e.pane_of("operator")["pane_id"], e.env["HERDR_PANE_ID"])
            self.assertEqual(e.pane_of("operator")["agent"], "codex")
            self.assertEqual(list(e.starts()), ["architect"])
            self.assertEqual(kind(e.starts()["architect"]), "claude")
            self.assertEqual(flag(native(e.starts()["architect"]), "--model"), members["architect"]["model"])
            self.assertEqual(len(e.calls("tab", "create")), 1)  # the member tab

            # Setup reconciles the local operator fields. Raw up leaves the sample alone.
            local.write_text(local.read_text().replace('harness = "claude"', 'harness = "codex"', 1))
            corrected = e.run("up").stdout
            self.assertIn("through the inbox", corrected)
            self.assertIn("wait in the foreground", corrected)
            self.assertEqual(len(e.calls("agent", "start")), 1)
            self.assertEqual(tomllib.loads(sample.read_text())["operator"]["harness"], "claude")

    def test_up_supports_pi_and_omp_foreground_operators(self):
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness), Env(UI) as e:
                e.update(None, agent=harness)
                result = e.run("up", "lead-dev", "--no-brief")
                self.assertIn("wait in the foreground during active work", result.stdout)
                self.assertIn("not qualified for unattended idle delivery", result.stdout)
                self.assertEqual(e.pane_of("operator")["agent"], harness)
                self.assertEqual(list(e.starts()), ["lead-dev"])

    def test_up_supports_agy_background_operator(self):
        with Env(UI) as e:
            e.update(None, agent="agy")
            result = e.run("up", "lead-dev", "--no-brief")
            self.assertIn("wait in a native background task", result.stdout)
            self.assertEqual(e.pane_of("operator")["agent"], "agy")
            self.assertEqual(list(e.starts()), ["lead-dev"])


    def test_worktree_uses_explicit_main_root_and_rejects_invalid_messages(self):
        with Env(formation({"lead-dev": {**CATALOG_MEMBERS["lead-dev"], "worktree": True}},
                           operator={"harness": "claude"}), git=True) as e:
            e.run("up")
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                worktree = factory.plan(cfg, "lead-dev")["dir"]
                inbox = factory.Inbox(cfg)
            command = [sys.executable, str(SCRIPT), "say", "--cwd", str(e.repo), "operator", "hi"]
            env = {**e.env, "HERDR_PANE_ID": e.pane_of("lead-dev")["pane_id"]}
            calls = e.prompts()
            for i in (1, 2):
                result = subprocess.run(command, cwd=worktree, env=env, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"queued message {i}", result.stdout)
            before = inbox.path.read_bytes()
            self.assertNotEqual(e.say("lead-dev", "operator", "  ", check=False).returncode, 0)
            self.assertEqual(inbox.path.read_bytes(), before)
            self.assertEqual(e.prompts(), calls)

    def test_payloads_replay_acknowledgement_and_ids(self):
        with Env() as e:
            inbox = self.prepare(e)
            text = "line one\nUnicode: café 日本語\n@pm.\n"
            inbox.path.write_text(json.dumps({"id": 1, "at": "2026-11-19T00:00:00Z",
                                              "from": "lead-dev", "text": text}) + "\n")
            e.run("up", "lead-dev", "reviewer", "--no-brief")
            note = e.root / "message.txt"
            note.write_text(text)
            self.assertIn("queued message 2", e.say("reviewer", "operator", "--file", str(note)).stdout)
            self.assertEqual(e.prompts(), [])
            first = e.run("wait").stdout
            self.assertEqual(first, e.run("wait").stdout)
            self.assertIn("    Unicode: café 日本語", first)
            self.assertIn("[Message 1 from @lead-dev at ", first)
            self.assertIn("[Message 2 from @reviewer at ", first)
            self.assertNotIn("#1", first)
            self.assertIn("2 unread messages. After you have acted on or recorded these messages:", first)
            self.assertIn("wait --seen 2", first)
            rows = [json.loads(s) for s in inbox.path.read_text().splitlines()]
            self.assertEqual([r["text"] for r in rows], [text, text])
            self.assertRegex(rows[0]["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            e.run("wait", "--seen", "2", "--timeout", "0", check=False)
            self.assertEqual(inbox.path.read_bytes(), b"")
            self.assertIn("queued message 3", self.send(e).stdout)
            self.assertIn("1 unread message.", e.run("wait").stdout)
            self.assertIn("; 1 unread message (", e.run("status").stdout)
            e.run("wait", "--seen", "1", "--timeout", "0")  # an old acknowledgement cannot move seen backwards
            before = (inbox.path.read_bytes(), inbox.state_path.read_bytes())
            self.assertEqual(e.run("wait", "--seen", "9", check=False).returncode, 2)
            self.assertEqual(before, (inbox.path.read_bytes(), inbox.state_path.read_bytes()))
            self.assertIn("1 unread", e.run("status").stdout)

    def test_acknowledged_history_recovers_text_without_watching_or_writing(self):
        with Env() as e:
            self.assertIn("inbox reprint recent acknowledged inbox messages", " ".join(e.run("--help").stdout.split()))
            self.assertIn("Reread them with factory.py inbox", " ".join(e.run("wait", "--help").stdout.split()))
            inbox = self.prepare(e)
            original = inbox.state_path.read_bytes()  # Old state has no history field.
            self.assertEqual(e.run("inbox").stdout, "no acknowledged messages kept\n")
            self.assertEqual(inbox.state_path.read_bytes(), original)
            self.assertFalse(inbox.path.exists())
            text = "A report accidentally acknowledged\n" + "a" * 500 + "\nDecision: approve café 日本語"
            self.seed(e, text)
            self.seed(e, "still unread")
            e.run("wait", "--seen", "1", "--timeout", "0")
            before = inbox.path.read_bytes(), inbox.state_path.read_bytes(), e.calls()
            result = e.run("inbox")
            self.assertIn("[Message 1 from @lead-dev at ", result.stdout)
            self.assertIn("    " + text.replace("\n", "\n    "), result.stdout)
            self.assertNotIn("still unread", result.stdout)
            self.assertNotIn("wait --seen", result.stdout)
            self.assertEqual(e.run("inbox", "seen", check=False).returncode, 2)
            self.assertEqual((inbox.path.read_bytes(), inbox.state_path.read_bytes(), e.calls()), before)
            self.assertIn("no acknowledged messages kept", e.run("inbox", HERDR_SESSION="other").stdout)
            for count in ("0", "-1", "invalid"):
                self.assertNotEqual(e.run("inbox", count, check=False).returncode, 0)

    def test_history_is_bounded_ordered_and_acknowledged_only_once(self):
        with Env() as e:
            inbox = self.prepare(e)
            rows = [{"id": n, "from": "lead-dev", "at": "2026-11-18T12:00:00Z", "text": f"report {n}"}
                    for n in range(1, 107)]
            inbox.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            e.run("wait", "--seen", "105")
            history = json.loads(inbox.state_path.read_text())["history"]
            self.assertEqual([r["id"] for r in history], list(range(6, 106)))
            before = inbox.path.read_bytes(), inbox.state_path.read_bytes()
            for seen in (105, 104):
                e.run("wait", "--seen", str(seen))
            for count, expected in ((None, range(101, 106)), ("2", range(104, 106)), ("200", range(6, 106))):
                result = e.run("inbox", *([] if count is None else [count]))
                ids = [line.split()[1] for line in result.stdout.splitlines() if line.startswith("[Message ")]
                self.assertEqual(ids, [str(n) for n in expected])
            self.assertEqual((inbox.path.read_bytes(), inbox.state_path.read_bytes()), before)

    def test_failed_acknowledgement_keeps_text_unread_and_retry_archives_it(self):
        with Env() as e:
            inbox = self.prepare(e)
            self.seed(e, "recover me")
            before = inbox.path.read_bytes(), inbox.state_path.read_bytes()
            with patch.dict(os.environ, e.env), factory.Inbox(factory.Config(e.repo)) as current:
                with patch.object(current, "replace", side_effect=OSError("state save failed")), self.assertRaises(OSError):
                    current.acknowledge(1)
            self.assertEqual((inbox.path.read_bytes(), inbox.state_path.read_bytes()), before)
            self.assertIn("recover me", e.run("wait").stdout)
            e.run("wait", "--seen", "1", "--timeout", "0")
            self.assertIn("recover me", e.run("inbox").stdout)

    def test_history_damage_and_unavailable_state_fail_without_writes(self):
        row = {"id": 1, "from": "lead-dev", "at": "time", "text": "old"}
        for history in ({}, [None], [{**row, "id": True}], [row, row], [{**row, "text": None}],
                        [{**row, "id": 2}], [{**row, "id": n} for n in range(1, 102)]):
            with self.subTest(history=history), Env() as e:
                inbox = self.prepare(e)
                self.seed(e, "unread")
                inbox.state_path.write_text(json.dumps({"seen": 1, "history": history}))
                before = inbox.path.read_bytes(), inbox.state_path.read_bytes()
                for command in (("inbox",), ("wait", "--seen", "1")):
                    result = e.run(*command, check=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("inbox needs repair", result.stderr)
                self.assertEqual((inbox.path.read_bytes(), inbox.state_path.read_bytes()), before)
        with Env() as e:
            inbox = self.prepare(e)
            target = e.root / "state"
            inbox.state_path.rename(target)
            inbox.state_path.symlink_to(target)
            before = target.read_bytes()
            result = e.run("inbox", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(target.read_bytes(), before)

    def test_concurrent_sends_and_acknowledgement(self):
        with Env() as e:
            inbox = self.prepare(e)
            self.seed(e)
            inode = inbox.lock_path.stat().st_ino
            waiter = subprocess.Popen([sys.executable, str(SCRIPT), "wait", "--cwd", str(e.repo), "--seen", "1"],
                                      env=e.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            with patch.dict(os.environ, e.env), ThreadPoolExecutor(max_workers=6) as pool:
                cfg = factory.Config(e.repo)
                results = list(pool.map(lambda n: factory.queue_message(cfg, "lead-dev", str(n)), range(6)))
            waiter.communicate(timeout=15)
            self.assertTrue(all(outcome == "queued" for outcome, _ in results))
            rows = [json.loads(s) for s in inbox.path.read_text().splitlines()]
            self.assertEqual([r["id"] for r in rows], list(range(2, 8)))
            self.assertEqual({r["text"] for r in rows}, {str(n) for n in range(6)})
            self.assertEqual([r["id"] for r in json.loads(inbox.state_path.read_text())["history"]], [1])
            self.assertEqual(inode, inbox.lock_path.stat().st_ino)

    def test_queue_distinguishes_a_refusal_from_an_uncertain_append(self):
        with Env() as e:
            self.prepare(e)
            with patch.object(factory.Inbox, "private_open", side_effect=PermissionError("denied")):
                self.assertEqual(self.invoke(e), ("refused", "denied"))
            with patch.object(factory.os, "fsync", side_effect=OSError("failed fsync")):
                self.assertEqual(self.invoke(e), ("unconfirmed", "failed fsync"))
            self.assertEqual(e.calls(), [])

    def test_queue_accepts_missing_acknowledgement_state(self):
        with Env() as e:
            self.assertEqual(self.invoke(e), ("queued", 1))
            self.assertIn("literal '$herdr' @pm.", e.run("wait", "--timeout", "0").stdout)


    def test_short_writes_and_private_modes(self):
        with Env() as e:
            inbox = self.prepare(e)
            inbox.directory.chmod(0o755)
            inbox.state_path.chmod(0o644)
            inbox.lock_path.chmod(0o644)
            write = os.write
            with patch.object(factory.os, "write", side_effect=lambda fd, data: write(fd, data[:3])):
                self.assertEqual(self.invoke(e), ("queued", 1))
            self.assertEqual(inbox.directory.stat().st_mode & 0o777, 0o700)
            for path in (inbox.path, inbox.state_path, inbox.lock_path):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(inbox.path.read_text())["text"], "literal '$herdr' @pm.")

    def test_interrupted_acknowledgement_is_reconciled_on_next_send(self):
        with Env() as e:
            inbox = self.prepare(e)
            self.seed(e, "one")
            self.seed(e, "two")
            with patch.dict(os.environ, e.env), factory.Inbox(factory.Config(e.repo)) as current:
                replace = current.replace
                def interrupt(path, data):
                    if path == current.path:
                        raise OSError("crash after state commit")
                    return replace(path, data)
                with patch.object(current, "replace", side_effect=interrupt), self.assertRaises(OSError):
                    current.acknowledge(1)
            before = inbox.path.read_bytes()
            self.assertNotIn("    one", e.run("wait").stdout)
            self.assertEqual(before, inbox.path.read_bytes())
            self.assertEqual(3, self.seed(e, "three"))
            self.assertEqual([r["id"] for r in map(json.loads, inbox.path.read_text().splitlines())], [2, 3])
            e.run("wait", "--seen", "1", "--timeout", "0")
            history = e.run("inbox").stdout
            self.assertEqual(history.count("[Message 1 from"), 1)
            self.assertIn("    one", history)

    def test_damage_refuses_send_and_seen_but_lifecycle_wait_survives(self):
        for damaged in ("inbox", "state"):
            with self.subTest(damaged=damaged), Env(UI) as e:
                e.run("up", "--no-brief")
                inbox = self.prepare(e)
                self.send(e)
                path = inbox.path if damaged == "inbox" else inbox.state_path
                path.write_bytes(b'{"torn":')
                before = (inbox.path.read_bytes(), inbox.state_path.read_bytes())
                failure = self.send(e, check=False)
                self.assertEqual(failure.returncode, 1)
                self.assertIn("not sent to @operator", failure.stdout)
                self.assertFalse(e.prompts())
                self.assertEqual(e.run("wait", "--seen", "1", check=False).returncode, 2)
                result = e.run("wait", "lead-dev", "--interval", "0.01")
                self.assertEqual(result.stdout.count("inbox needs repair"), 1)
                self.assertIn("@lead-dev stopped (idle)", result.stdout)
                self.assertIn("inbox needs repair", e.run("status").stdout)
                self.assertEqual(before, (inbox.path.read_bytes(), inbox.state_path.read_bytes()))
                fake = e.fake_state()
                fake["panes"] = {"w1:p1": fake["panes"]["w1:p1"]}
                e.write_fake(fake)
                result = e.run("up")
                self.assertIn("inbox needs repair", result.stdout)
                self.assertIn("started @lead-dev", result.stdout)
                self.assertIn("Use this command for teammates", e.run("prompt", "lead-dev").stdout)
                self.assertEqual(before, (inbox.path.read_bytes(), inbox.state_path.read_bytes()))

    def test_unavailable_inbox_degrades_but_cache_directories_may_be_symlinks(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        with Env(brief) as e:
            inbox = self.prepare(e)
            target = e.root / "external-state"
            inbox.state_path.rename(target)
            inbox.state_path.symlink_to(target)
            before = target.read_bytes()
            self.assertIn("inbox unavailable:", e.run("up").stdout)
            self.assertIn("Use this command for teammates", e.run("prompt", "lead-dev").stdout)
            self.assertIn("Use the standalone `factory.py say`", e.run("brief").stdout)
            self.assertIn("operator messages: unavailable", e.run("status").stdout)
            waiting = e.run("wait", "--timeout", "0", check=False)
            self.assertEqual(waiting.returncode, 2)
            self.assertIn(str(inbox.state_path), waiting.stderr)
            self.assertTrue(inbox.state_path.is_symlink())
            self.assertEqual(target.read_bytes(), before)
        for parent in (False, True):
            with self.subTest(parent=parent), Env(brief) as e:
                inbox = self.prepare(e)
                path = inbox.directory.parent if parent else inbox.directory
                target = e.root / "linked-cache"
                path.rename(target)
                path.symlink_to(target, target_is_directory=True)
                target.chmod(0o755)
                self.assertIn("through the inbox", e.run("up").stdout)
                self.assertIn("queued message 1", self.send(e).stdout)
                self.assertTrue(path.is_symlink())
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_message_file_read_errors_are_clean_and_do_not_queue(self):
        with Env() as e:
            e.run("up", "lead-dev", "--no-brief")
            inbox = self.prepare(e)
            before = inbox.state_path.read_bytes()
            missing, invalid = e.root / "missing", e.root / "invalid-utf8"
            invalid.write_bytes(b"\xff\xfe")
            for path in (missing, invalid, e.root):
                result = e.say("lead-dev", "operator", "--file", str(path), check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"cannot read message file {path}:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("herdr agent prompt", result.stderr)
                self.assertFalse(inbox.path.exists())
                self.assertEqual(inbox.state_path.read_bytes(), before)
            self.assertEqual(e.prompts(), [])

    def test_cold_retention_clears_only_acknowledged_in_current_session(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        for acknowledge in (False, True):
            with self.subTest(acknowledge=acknowledge), Env(brief) as e:
                inbox = self.prepare(e)
                self.seed(e, "earlier message")
                if acknowledge:
                    e.run("wait", "--seen", "1", "--timeout", "0")
                with patch.dict(os.environ, {**e.env, "HERDR_SESSION": "another/session"}):
                    with factory.Inbox(factory.Config(e.repo)) as other:
                        other.save_state()
                    other.path.write_bytes(b"untouched")
                before_other = (other.path.read_bytes(), other.state_path.read_bytes())
                inode = inbox.lock_path.stat().st_ino
                result = e.run("up")
                self.assertEqual(inbox.lock_path.stat().st_ino, inode)
                self.assertEqual(before_other, (other.path.read_bytes(), other.state_path.read_bytes()))
                state = json.loads(inbox.state_path.read_text())
                self.assertEqual(state["seen"], 1 if acknowledge else 0)
                if acknowledge:
                    self.assertEqual(inbox.path.read_bytes(), b"")
                    self.assertNotIn("unread messages from the earlier team", result.stdout)
                    self.assertIn("earlier message", e.run("inbox").stdout)
                    self.assertIn("queued message 2", self.send(e).stdout)
                else:
                    self.assertIn("1 unread message from the earlier team", result.stdout)
                    self.assertIn("earlier message", e.run("wait").stdout)

    def test_partial_restart_and_failed_query_do_not_clear(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"], "reviewer": CATALOG_MEMBERS["reviewer"]},
                          operator={"harness": "claude"})
        with Env(brief) as e:
            e.run("up", "lead-dev")
            inbox = self.prepare(e)
            self.send(e)
            e.run("wait", "--seen", "1", "--interval", "0.01", "--timeout", "0", check=False)
            inode = inbox.lock_path.stat().st_ino
            for command in (("up", "reviewer"), ("up", "--restart")):
                e.run(*command)
                self.assertTrue(inbox.path.exists())
                self.assertEqual(json.loads(inbox.state_path.read_text())["seen"], 1)
                self.assertEqual(inbox.lock_path.stat().st_ino, inode)
            # Even a restart with no members live must not clear acknowledged transport state.
            fake = e.fake_state()
            fake["panes"] = {"w1:p1": fake["panes"]["w1:p1"]}
            e.write_fake(fake)
            e.run("up", "--restart")
            self.assertEqual(json.loads(inbox.state_path.read_text())["seen"], 1)
            before = (inbox.path.read_bytes(), inbox.state_path.read_bytes())
            args = types.SimpleNamespace(cwd=str(e.repo), names=[], restart=False, fresh=False, timeout=1000, no_brief=False)
            with patch.dict(os.environ, e.env), patch.object(factory, "ensure_operator", return_value="operator"), \
                    patch.object(factory, "Team", side_effect=SystemExit("query failed")):
                with self.assertRaisesRegex(SystemExit, "query failed"):
                    factory.cmd_up(args)
            self.assertEqual(before, (inbox.path.read_bytes(), inbox.state_path.read_bytes()))

    def test_named_watch_wakes_on_any_sender_and_sessions_are_isolated(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            inbox = self.prepare(e)
            e.update("lead-dev", agent_status="working")
            watcher = subprocess.Popen([sys.executable, str(SCRIPT), "wait", "lead-dev", "--cwd", str(e.repo),
                                        "--interval", "0.05", "--timeout", "5"], env=e.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.seed(e, "question", sender="reviewer")
            out, err = watcher.communicate(timeout=10)
            self.assertEqual(watcher.returncode, 0, err)
            self.assertIn("from @reviewer", out)
            other = e.run("wait", "--interval", "0.01", "--timeout", "0.1", check=False, HERDR_SESSION="other")
            self.assertNotIn("question", other.stdout)
            self.assertIn("question", e.run("wait").stdout)


class StockTransportTests(unittest.TestCase):
    def test_snapshot_ack_and_errors_without_herdr_metadata(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "--no-brief")
            for i, report in enumerate(("first report", "second report"), 1):
                self.assertIn(f"queued message {i}", e.say("lead-dev", "operator", report).stdout)
            e.run("down")
            e.close("operator")
            calls = e.calls()
            blocked = {"FAKE_ROSTER_FAIL": "1", "FAKE_GET_FAIL": "1"}
            with patch.dict(os.environ, e.env):
                inbox = factory.Inbox(factory.Config(e.repo))
            self.assertFalse(inbox.state_path.exists())
            before = inbox.path.read_bytes()
            self.assertIn("second report", e.run("wait", **blocked).stdout)
            first = e.run("wait", "--timeout", "0", **blocked).stdout
            self.assertIn("first report", first)
            self.assertIn("second report", first)
            invalid = e.run("wait", "--timeout", "0", "--seen", "3", check=False, **blocked)
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("invalid inbox id 3", invalid.stderr)
            self.assertFalse(inbox.state_path.exists())
            self.assertEqual(inbox.path.read_bytes(), before)
            remaining = e.run("wait", "--timeout", "0", "--seen", "1", **blocked).stdout
            self.assertNotIn("first report", remaining)
            self.assertIn("second report", remaining)
            self.assertIn("wait --seen 2 --timeout 0", remaining)
            self.assertEqual(e.run("wait", "--timeout", "0", "--seen", "2", **blocked).stdout, "")
            self.assertIn("first report", e.run("inbox", **blocked).stdout)
            inbox.path.write_text('{"torn":')
            damaged = e.run("wait", "--timeout", "0", check=False, **blocked)
            self.assertEqual(damaged.returncode, 2)
            self.assertIn("inbox needs repair", damaged.stderr)
            self.assertEqual(e.calls(), calls)

    def test_send_to_codex_only_appends_and_consumes_draft(self):
        with Env(UI) as e:
            e.update(None, agent="codex")
            e.run("up", "lead-dev", "--no-brief")
            e.update("operator", agent_session={"agent": "codex", "kind": "id",
                                                "value": "01234567-89ab-cdef-0123-456789abcdef"},
                     procs=[{"name": "codex", "argv": ["codex"]}])
            native_calls = e.root / "codex-invoked"
            (e.root / "bin/codex").write_text(f"#!/bin/sh\nprintf called > {shlex.quote(str(native_calls))}\n")
            with patch.dict(os.environ, e.env):
                draft = factory.Config(e.repo).messages / "lead-dev/report.md"
            draft.write_text("a Codex operator report")
            before = len(e.calls())
            result = e.say("lead-dev", "operator", "--file", str(draft))
            self.assertEqual(result.stdout, "queued message 1 for @operator\n")
            self.assertEqual(result.stderr, "")
            self.assertFalse(draft.exists())
            self.assertFalse(native_calls.exists())
            self.assertEqual(e.calls()[before:], [["agent", "list"], ["pane", "list"]])
            snapshot = e.run("wait", "--timeout", "0").stdout
            self.assertEqual(snapshot.count("[Message 1 from @lead-dev"), 1)
            self.assertIn("a Codex operator report", snapshot)


class WaitTests(unittest.TestCase):
    def test_wait_deadline_uses_the_first_verified_operator(self):
        codex = {"agent": "codex", "pane_id": "w1:p1"}
        pi = {"agent": "pi", "pane_id": "w1:p1"}
        omp = {"agent": "omp", "pane_id": "w1:p1"}
        claude = {"agent": "claude", "pane_id": "w1:p1"}
        opencode = {"agent": "opencode", "pane_id": "w1:p1"}

        def roster(operator, named_pane):
            return types.SimpleNamespace(
                agent_panes={"captain": named_pane} if named_pane else {},
                pane={"captain": operator, "lead-dev": {"pane_id": "w1:p2"}}.get,
                status=lambda _name: "working",
            )

        cases = (
            ("Codex", codex, "w1:p1", None, 60, 60),
            ("pi", pi, "w1:p1", None, 60, 60),
            ("omp", omp, "w1:p1", None, 240, 240),
            ("Claude", claude, "w1:p1", None, 1800, None),
            ("OpenCode", opencode, "w1:p1", None, 1800, None),
            ("missing pane", None, "w1:p1", None, 1800, None),
            ("unbound Codex pane", codex, None, None, 1800, None),
            ("mismatched Codex", codex, "w1:p2", None, 1800, None),
            ("unbound pi pane", pi, None, None, 1800, None),
            ("mismatched pi", pi, "w1:p2", None, 1800, None),
            ("unbound omp pane", omp, None, None, 1800, None),
            ("mismatched omp", omp, "w1:p2", None, 1800, None),
            ("explicit Codex", codex, "w1:p1", 12, 12, 12),
            ("explicit pi", pi, "w1:p1", 12, 12, 12),
            ("explicit omp", omp, "w1:p1", 12, 12, 12),
            ("explicit background", claude, "w1:p1", 12, 12, 12),
        )
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]},
                          operator={"name": "captain", "harness": "codex"})
        for case, operator, named_pane, explicit, named_default, unnamed_default in cases:
            for names, expected in ((["@lead-dev"], named_default), ([], unnamed_default)):
                with self.subTest(case=case, names=names), Env(brief) as e, patch.dict(os.environ, e.env):
                    cfg = factory.Config(e.repo)
                    args = types.SimpleNamespace(cwd=str(e.repo), names=names, seen=None, timeout=explicit, interval=0)
                    now = 0
                    ticks = iter((59, 1800) if expected is None else (expected - 1, expected))

                    def advance(_):
                        nonlocal now
                        now = next(ticks)
                        if expected is None and now == 1800:
                            self.assertEqual(factory.queue_message(cfg, "lead-dev", "still listening"), ("queued", 1))

                    first = roster(operator, named_pane)
                    # Later polls cannot start or reset the invocation's default deadline.
                    later = roster(claude if expected == 60 else codex, "w1:p1")
                    clock = types.SimpleNamespace(monotonic=lambda: now, sleep=advance)
                    out = io.StringIO()
                    with patch.object(factory, "Team", side_effect=[first, later, later]) as team, \
                            patch.object(factory, "time", clock), contextlib.redirect_stdout(out):
                        if expected is None:
                            factory.cmd_wait(args)
                            self.assertIn("still listening", out.getvalue())
                            self.assertEqual(team.call_count, 2)
                        else:
                            with self.assertRaises(SystemExit) as exited:
                                factory.cmd_wait(args)
                            self.assertEqual(exited.exception.code, 4)
                            self.assertIn(f"timeout after {expected}s", out.getvalue())
                            self.assertEqual(team.call_count, 3)

    def test_quiet_watch_ignores_idle_until_message_or_inbox_failure(self):
        for harness, event in (("claude", "message"), ("claude", "damage"), ("claude", "unavailable"),
                               ("agy", "message")):
            brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": harness})
            with self.subTest(harness=harness, event=event), Env(brief) as e:
                e.update(None, agent=harness)
                e.run("up", "--no-brief")
                e.update("lead-dev", agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "lead-dev", "status": "done"}
                e.write_fake(state)
                sleeps = []
                with patch.dict(os.environ, e.env):
                    inbox = factory.Inbox(factory.Config(e.repo))
                def after_poll(_):
                    sleeps.append(e.fake_state()["polls"])
                    if len(sleeps) == 2:
                        if event == "message":
                            self.assertEqual(factory.queue_message(factory.Config(e.repo), "lead-dev",
                                                                   "report after idle"), ("queued", 1))
                        elif event == "damage":
                            inbox.path.write_bytes(b'{"torn":')
                        else:
                            inbox.path.symlink_to(e.root / "missing")
                    elif len(sleeps) > 2:
                        self.fail("watch did not return after message or inbox error")
                args = types.SimpleNamespace(cwd=str(e.repo), names=[], seen=None, timeout=None, interval=0)
                out, err = io.StringIO(), io.StringIO()
                # Patch factory's binding: changing time.sleep also intercepts subprocess timeout polling.
                with patch.dict(os.environ, e.env), patch.object(factory, "HERDR", e.env["HERDR_BIN"]), \
                        patch.object(factory, "time", types.SimpleNamespace(sleep=after_poll)), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    if event == "message":
                        factory.cmd_wait(args)
                    else:
                        with self.assertRaises(SystemExit) as ex:
                            factory.cmd_wait(args)
                        self.assertEqual(ex.exception.code, 2)
                self.assertEqual(sleeps, [1, 2])  # The done transition did not end the watch.
                self.assertNotIn("stopped", out.getvalue())
                if event == "message":
                    self.assertIn("report after idle", out.getvalue())
                else:
                    self.assertIn(str(inbox.path), err.getvalue())

    def test_quiet_watch_returns_problems_but_named_watch_keeps_lifecycle(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        for change, expected in (({"exit": True}, "gone"), ({"close": True}, "closed"),
                                 ({"status": "blocked"}, "blocked"), ({"status": "idle"}, "stopped")):
            with self.subTest(change=change), Env(brief) as e:
                e.run("up", "--no-brief")
                e.update("lead-dev", agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "lead-dev", **change}
                e.write_fake(state)
                names = ["lead-dev"] if expected == "stopped" else []
                result = e.run("wait", *names, "--interval", "0.01", "--timeout", "2")
                self.assertIn(expected, result.stdout)

    def test_quiet_timeout_shows_unknown_and_working_members(self):
        brief = formation({n: CATALOG_MEMBERS[n] for n in ("lead-dev", "reviewer")}, operator={"harness": "claude"})
        with Env(brief) as e:
            e.run("up", "--no-brief")
            e.update("lead-dev", agent_status="unknown")
            e.update("reviewer", agent_status="working")
            result = e.run("wait", "--interval", "0.01", "--timeout", "0.2", check=False)
            self.assertEqual(result.returncode, 4)
            self.assertIn("still working: @reviewer; status unknown: @lead-dev", result.stdout)
            e.update("reviewer", agent_status="done")
            result = e.run("wait", "--interval", "0.01", "--timeout", "0.2", check=False)
            self.assertEqual(result.returncode, 4)
            self.assertIn("nobody is working; status unknown: @lead-dev", result.stdout)

    def test_wait_returns_when_the_lead_is_needed(self):
        with Env() as e:
            e.update(None, agent="codex")
            self.assertIn("not started: any member", e.run("wait", check=False).stderr)
            e.run("up", "lead-dev", "reviewer", "pm", "--no-brief")
            for name in ("lead-dev", "reviewer", "pm"):
                e.update(name, agent_status="working")
            p = e.run("wait", "--interval", "0.05", "--timeout", "0.2", check=False)
            self.assertEqual(p.returncode, 4)
            self.assertIn("still working: @lead-dev, @reviewer, @pm", p.stdout)

            e.update("reviewer", agent_status="blocked")
            out = e.run("wait").stdout
            self.assertIn("@reviewer is blocked: it needs an answer", out)
            e.update("reviewer", agent_status="working")
            e.kill("pm")
            self.assertIn("@pm is gone: its agent exited. `factory.py up pm`", e.run("wait").stdout)

            e.run("up", "pm")
            e.update("pm", agent_status="working")
            e.update("lead-dev", agent_status="idle")  # a watched member stopped while others still work
            self.assertIn("@lead-dev stopped (idle)", e.run("wait", "lead-dev", "--interval", "0.05").stdout)
            p = e.run("wait", "--interval", "0.05", "--timeout", "0.2", check=False)
            self.assertEqual(p.returncode, 4)  # without names, others working means: keep waiting
            for name in ("reviewer", "pm"):
                e.update(name, agent_status="idle")
            self.assertIn("nobody is working: @lead-dev idle, @reviewer idle, @pm idle", e.run("wait", "--interval", "0.05").stdout)

    def test_default_wait_notices_a_finished_member_while_others_work(self):
        with Env() as e:
            e.update(None, agent="codex")
            e.run("up", "lead-dev", "reviewer", "--no-brief")
            e.update("lead-dev", agent_status="working")
            e.update("reviewer", agent_status="working")
            state = e.fake_state()
            state["on_second_poll"] = {"name": "lead-dev", "status": "done"}
            e.write_fake(state)
            result = e.run("wait", "--interval", "0.01", "--timeout", "2")
            self.assertIn("@lead-dev stopped (done)", result.stdout)
            self.assertEqual(e.pane_of("reviewer")["agent_status"], "working")

    def test_named_wait_ignores_other_members(self):
        for change in (*({"status": s} for s in ("idle", "done", "blocked", "unknown")),
                       {"close": True}, {"exit": True}):
            with self.subTest(change=change), Env() as e:
                e.run("up", "lead-dev", "reviewer", "--no-brief")
                for name in ("lead-dev", "reviewer"):
                    e.update(name, agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "reviewer", **change}
                e.write_fake(state)
                result = e.run("wait", "@lead-dev", "--interval", "0.01", "--timeout", "1", check=False)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("still working: @lead-dev", result.stdout)
                self.assertNotIn("reviewer", result.stdout)
                self.assertGreaterEqual(e.fake_state()["polls"], 2)  # the unrelated transition really happened

    def test_named_wait_notices_selected_members(self):
        changes = [({"status": s}, s) for s in ("idle", "done", "blocked", "unknown")]
        for change, expected in changes + [({"close": True}, "closed"), ({"exit": True}, "gone")]:
            with self.subTest(change=change), Env() as e:
                e.run("up", "lead-dev", "reviewer", "pm", "--no-brief")
                for name in ("lead-dev", "reviewer", "pm"):
                    e.update(name, agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "reviewer", **change}
                e.write_fake(state)
                result = e.run("wait", "lead-dev", "@reviewer", "--interval", "0.01", "--timeout", "2")
                self.assertIn("@reviewer", result.stdout)
                self.assertIn(expected, result.stdout)
                self.assertNotIn("@pm", result.stdout)
        with Env() as e:
            e.run("up", "lead-dev", "--no-brief")
            e.update("lead-dev", agent_status="working")
            for name in ("reviewer", "operator"):
                result = e.run("wait", "lead-dev", name, "--timeout", "0.2", "--interval", "0.01", check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"not started: {name}", result.stderr)

    def test_wait_reports_disappearance_and_unknown(self):
        for change, expected in (({"close": True}, "closed"), ({"status": "unknown"}, "unknown")):
            with self.subTest(change=change), Env() as e:
                e.update(None, agent="codex")
                e.run("up", "lead-dev", "reviewer", "--no-brief")
                e.update("lead-dev", agent_status="working")
                e.update("reviewer", agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "lead-dev", **change}
                e.write_fake(state)
                result = e.run("wait", "--interval", "0.01", "--timeout", "2")
                self.assertIn("@lead-dev", result.stdout)
                self.assertIn(expected, result.stdout)
                self.assertNotIn("@lead-dev stopped", result.stdout)
                self.assertNotIn("nobody is working", result.stdout)

    def test_terminal_reads_and_keys_are_limited_to_known_startup_dialogs(self):
        with Env() as e:
            launched = e.run("up", "lead-dev", "pm", check=False, FAKE_START_FAIL="dialog:lead-dev,blocked:pm")
            self.assertEqual(launched.returncode, 3)
            self.assertIn("@pm launch incomplete", launched.stdout)
            pane = e.pane_of("lead-dev")["pane_id"]
            self.assertIn("@pm blocked:", e.run("status").stdout)
            for state, expected in (("blocked", "@pm is blocked"), ("unknown", "@pm: status unknown"),
                                    ("idle", "@pm stopped (idle)")):
                e.update("pm", agent_status=state)
                self.assertIn(expected, e.run("wait", "pm", "--interval", "0", "--timeout", "20").stdout)
            failed = e.run("brief", "--send", "pm", check=False, FAKE_UNCONFIRMED="pm")
            self.assertIn("delivery unconfirmed to @pm", failed.stdout)
            self.assertIn("queued message 1", e.say("lead-dev", "operator", "a report").stdout)
            self.assertIn("a report", e.run("wait").stdout)
            e.run("wait", "--seen", "1", "--timeout", "0", check=False)
            self.assertIn("a report", e.run("inbox").stdout)
            e.run("down", "lead-dev", "pm")

            read = ["pane", "read", pane, "--source", "visible"]
            terminal_calls = [c for c in e.calls() if len(c) > 1 and c[0] in ("pane", "agent", "terminal")
                              and c[1] in ("read", "snapshot", "wait-output", "send-keys")]
            self.assertEqual(terminal_calls, [read, ["pane", "send-keys", pane, "enter"], read])


class UpRecoveryTests(unittest.TestCase):
    def test_launch_matrix(self):
        for flags in ((), ("--fresh",), ("--restart",), ("--restart", "--fresh")):
            with self.subTest(flags=flags), Env(UI) as e:
                # One live member, two closed panes (matching and incompatible sessions), one newcomer.
                e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
                for name, agent in (("lead-dev", "codex"), ("frontend-dev", "claude"), ("pm", "codex")):
                    e.set_session(name, {"agent": agent, "kind": "id", "value": f"saved-{name}"})
                e.run("status")
                e.close("frontend-dev", "pm")
                before = e.pane_of("lead-dev")["pane_id"]
                out = e.run("up", *flags).stdout
                restarted = "--restart" in flags
                fresh = "--fresh" in flags
                targets = ["frontend-dev", "reviewer", "pm"]
                if restarted:
                    targets.insert(0, "lead-dev")
                self.assertEqual([c[2] for c in e.calls("agent", "start")[3:]], targets)
                self.assertEqual(e.calls("pane", "close"), [["pane", "close", before]] if restarted else [])
                prompts = dict(e.prompts())
                self.assertEqual(len(e.prompts()), 4)
                for name in targets:
                    resumed = name in ("lead-dev", "frontend-dev") and not fresh
                    self.assertEqual(f"saved-{name}" in native(e.starts()[name]), resumed)
                    self.assertIn(f"{'resumed' if resumed else 'started'} @{name} (", out)
                    closing = prompts[name].rstrip().rsplit("\n\n", 1)[-1]
                    if resumed:
                        self.assertIn("picking up where we left off", closing)
                        self.assertNotIn("`ready`", closing)
                    elif name == "reviewer":
                        self.assertEqual(closing, READY)
                    else:
                        self.assertEqual(closing, "You are starting in a new session and know nothing of the work so far. " + READY)
                if not restarted:
                    self.assertIn("Joined the team: @reviewer", prompts["lead-dev"])
                    self.assertNotIn("@frontend-dev", prompts["lead-dev"])
                    self.assertNotIn("@pm", prompts["lead-dev"])

    def test_restart_with_no_saved_session_sends_no_join(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            e.run("up", "pm", "--restart")
            self.assertEqual([name for name, _ in e.prompts()], ["pm"])
            self.assertIn("starting in a new session", e.prompts()[0][1])

    def test_launch_reports_name_the_operator_and_manual_briefs_ask_nothing(self):
        with Env(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"name": "boss"})) as e:
            e.run("up")
            self.assertTrue(e.prompts()[0][1].endswith(READY.replace("@operator", "@boss") + "\n"))
            self.assertEqual(len(e.prompts()), 1)
            e.run("up")  # already running: nothing sent
            e.run("brief", "--send", "lead-dev")
            self.assertEqual(len(e.prompts()), 2)
            self.assertTrue(e.prompts()[1][1].endswith("That is the whole team brief. No reply needed.\n"))
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "saved"})
            e.run("up", "lead-dev", "--restart")
            self.assertEqual(len(e.prompts()), 3)
            self.assertIn("send @boss a new one-line pickup report for this launch, even if you sent one before.",
                          e.prompts()[2][1])

    def test_up_names_running_members_without_their_launch_flags(self):
        restored = {"lead-dev": [{"pid": 7, "name": "codex", "argv": ["codex", "resume", "cx-1"]}],
                    "frontend-dev": [{"pid": 8, "name": ".claude-wrapped", "argv": ["/bin/claude", "--resume", "c",
                                                                                     "--permission-mode", "auto"]}]}
        cases = {
            "matching": ({}, None),
            "missing flags": (restored, "lead-dev frontend-dev"),
            "unrelated foreground process": ({"lead-dev": [{"pid": 9, "name": "bash", "argv": ["bash"]}]}, None),
            "ambiguous": ({"lead-dev": restored["lead-dev"] * 2}, None),
            "no data": ({"lead-dev": []}, None),
            "no argv": ({"lead-dev": [{"pid": 7, "name": "codex"}]}, None),
        }
        for case, (procs, named) in cases.items():
            with self.subTest(case), Env(UI) as e:
                e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
                for name, p in procs.items():
                    e.update(name, procs=p)
                before = len(e.calls())
                out = e.run("up", "lead-dev", "frontend-dev", "pm").stdout
                self.assertIn("already running: lead-dev, frontend-dev, pm", out)
                # pi has no permission flags, so its process is never read
                read = [c[3] for c in e.calls("pane", "process-info")]
                self.assertEqual(read, [e.pane_of("lead-dev")["pane_id"], e.pane_of("frontend-dev")["pane_id"]])
                if named:
                    self.assertIn("running without their factory launch flags, as after a herdr restart:"
                                  f" {named.replace(' ', ', ')}. Run factory.py up {named} --restart", out)
                    self.assertNotIn("--resume", out)  # no raw argv
                else:
                    self.assertNotIn("launch flags", out)
                calls = e.calls()[before:]
                self.assertFalse([c for c in calls if c[:2] in (["pane", "close"], ["agent", "start"], ["agent", "prompt"])])

    def test_launch_flags_check_needs_args_too_and_skips_restarts(self):
        with Env(formation({"lead-dev": {**CATALOG_MEMBERS["lead-dev"], "args": ["-c", "x=1"]}})) as e:
            e.run("up", "--no-brief")
            argv = native(e.starts()["lead-dev"])
            e.update("lead-dev", procs=[{"pid": 7, "name": "codex", "argv": ["codex", *[a for a in argv if a != "x=1"]]}])
            self.assertIn("Run factory.py up lead-dev --restart", e.run("up").stdout)
            e.run("up", "--restart")
            self.assertNotIn("launch flags", e.run("up").stdout)  # the relaunch is checked by its own argv
            self.assertEqual(len(e.calls("pane", "process-info")), 2)

    def test_taken_name_prevents_an_earlier_restart(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            state = e.fake_state()
            state["panes"][e.pane_of("pm")["pane_id"]]["name"] = "reviewer"
            e.write_fake(state)
            before = len(e.calls())
            p = e.run("up", "lead-dev", "reviewer", "--restart", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("outside this team", p.stderr)
            calls = e.calls()[before:]
            self.assertEqual(calls, [["agent", "get", e.env["HERDR_PANE_ID"]], ["agent", "list"], ["pane", "list"]])

    def test_operator_pane_with_an_old_member_label_is_not_a_restart_target(self):
        with Env(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})) as e:
            state = e.fake_state()
            pane = state["panes"]["w1:p1"]
            pane.update(label="lead-dev", name="lead-dev",
                        agent_session={"agent": "claude", "kind": "id", "value": "operator-session"})
            e.write_fake(state)
            self.assertIn("through the inbox", e.run("up", "--restart").stdout)
            self.assertEqual(e.calls("pane", "close"), [])
            self.assertEqual(e.pane_of("operator")["pane_id"], "w1:p1")
            self.assertNotEqual(e.pane_of("lead-dev")["pane_id"], "w1:p1")
            self.assertIn(READY, dict(e.prompts())["lead-dev"])

    def test_continue_stub_never_launches(self):
        with Env(UI) as e:
            for args in ((), ("pm", "--restart", "--fresh")):
                p = e.run("continue", *args, check=False)
                self.assertEqual(p.returncode, 2)
                self.assertEqual((p.stdout + p.stderr).splitlines(), ["use factory.py up [names] [--fresh] [--restart]"])
            self.assertEqual(e.calls(), [])

    def test_no_member_panes_resumes_noted_sessions(self):
        marker = "This brief applies to the resumed team."
        with Env(UI + "\n" + marker + "\n") as e:
            e.run("up", "--no-brief")
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "cx-1"})
            e.set_session("pm", {"agent": "pi", "kind": "path", "value": "/s/p.jsonl"})
            e.run("status")
            e.close("lead-dev", "frontend-dev", "reviewer", "pm")
            out = e.run("up").stdout
            self.assertEqual(len(e.calls("agent", "start")), 8)
            by = {n: native(c) for n, c in e.starts().items()}
            self.assertEqual(by["lead-dev"][:2], ["resume", "cx-1"])
            self.assertEqual(flag(by["pm"], "--session"), "/s/p.jsonl")
            for name in ("frontend-dev", "reviewer"):
                self.assertTrue({"resume", "--resume", "--session"}.isdisjoint(by[name]))
            sent = dict(e.prompts())
            self.assertEqual(len(sent), 4)
            for message in sent.values():
                self.assertEqual(message.count(marker), 1)
                closing = message.rstrip().rsplit("\n\n", 1)[-1]
                self.assertNotIn("re-read", closing.lower())
            for name in ("lead-dev", "pm"):
                self.assertIn("picking up where we left off", sent[name])
                self.assertNotIn("No reply needed", sent[name])
            for name in ("frontend-dev", "reviewer"):
                self.assertTrue(sent[name].endswith(READY + "\n"))
                self.assertIn(f"started @{name}", out)

    def test_bare_up_recovers_exited_and_closed_members_without_disturbing_live_members(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            for name in ("lead-dev", "reviewer"):
                e.set_session(name, {"agent": "codex", "kind": "id", "value": f"saved-{name}"})
            e.kill("lead-dev")
            e.close("reviewer")
            e.run("up")
            self.assertEqual([c[2] for c in e.calls("agent", "start")[4:]], ["lead-dev", "reviewer"])
            self.assertEqual([n for n, _ in e.prompts()], ["lead-dev", "reviewer"])

    def test_up_without_members_says_so(self):
        with Env(formation({})) as e:
            result = e.run("up", check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no members in factory.toml; add one, then run `factory.py up`", result.stderr)
            self.assertEqual(e.starts(), {})

    def test_cold_up_checks_every_member_before_launch(self):
        with Env(UI.replace('model = "gpt-6-luna"', 'model = "missing-model"')) as e:
            result = e.run("up", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(e.starts(), {})
            self.assertEqual(e.calls("tab", "create"), [])

    def test_failed_fresh_launch_is_not_reported_as_fresh(self):
        with Env(UI) as e:
            result = e.run("up", check=False, FAKE_START_FAIL="dead:lead-dev")
            self.assertEqual(result.returncode, 3)
            self.assertNotIn("started @lead-dev (", result.stdout)
            for name in ("frontend-dev", "reviewer", "pm"):
                self.assertIn(f"started @{name} (", result.stdout)
            self.assertEqual({n for n, _ in e.prompts()}, {"frontend-dev", "reviewer", "pm"})

    def test_brings_members_back_in_their_panes_and_sessions(self):
        with Env() as e:
            e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
            e.set_session("lead-dev", {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "cx-1"})
            e.set_session("frontend-dev", {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "cl-1"})
            e.set_session("pm", {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/s/2026_pi-1.jsonl"})
            self.assertIn("already running", e.run("up", "lead-dev", "frontend-dev", "pm").stdout)
            self.assertEqual(e.prompts(), [])
            self.assertEqual(e.calls("agent", "rename"), [["agent", "rename", "w1:p1", "operator"]])
            e.kill("lead-dev", "frontend-dev", "pm")
            panes_before = {n: e.pane_of(n)["pane_id"] for n in ("lead-dev", "frontend-dev", "pm")}
            # The brief changed while they were gone: lead-dev moves to Luna.
            e.write_brief(CATALOG.replace('model = "gpt-6-astra"\nthinking = "high"',
                                           'model = "gpt-6-luna"\nthinking = "medium"'))
            self.assertIn("`factory.py up lead-dev frontend-dev pm`", e.run("status").stdout)

            before = len(e.prompts())
            e.run("up", "lead-dev", "frontend-dev", "pm")
            by = {c[2]: c for c in e.calls("agent", "start")[3:]}
            self.assertEqual({n: flag(c, "--pane") for n, c in by.items()}, panes_before)  # same panes, no new ones
            self.assertEqual(len(e.calls("tab", "create")) + len(e.calls("pane", "split")), 3)
            self.assertEqual(native(by["lead-dev"])[:2], ["resume", "cx-1"])
            self.assertEqual(flag(native(by["lead-dev"]), "-C"), str(e.repo))
            self.assertEqual(flag(native(by["lead-dev"]), "-m"), "gpt-6-luna")  # current settings apply
            self.assertEqual(flag(native(by["frontend-dev"]), "--resume"), "cl-1")
            self.assertEqual(flag(native(by["pm"]), "--session"), "/s/2026_pi-1.jsonl")
            sent = e.prompts()[before:]
            self.assertEqual(sorted(n for n, _ in sent), ["frontend-dev", "lead-dev", "pm"])  # one message each
            for _, text in sent:
                self.assertIn("picking up where we left off", text)
                self.assertIn("Send it with the say command", text)
                self.assertIn("A reply in your own pane does not reach @operator", text)
                self.assertIn("# Team brief", text)
                self.assertNotIn("No reply needed", text)

            e.kill("pm")
            before = len(e.prompts())
            e.run("up", "pm")  # one member comes back: only it is re-briefed
            self.assertEqual([n for n, _ in e.prompts()[before:]], ["pm"])

    def test_restart_checks_before_closing_and_reapplies_settings(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            old = e.pane_of("lead-dev")["pane_id"]
            others = {n: {k: v for k, v in e.pane_of(n).items() if k != "rect"}
                      for n in ("operator", "reviewer", "frontend-dev", "pm")}
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "restart-session"})
            e.write_brief(UI.replace('model = "gpt-6-astra"', 'model = "missing-model"'))
            failed = e.run("up", "lead-dev", "--restart", check=False)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(e.calls("pane", "close"), [])
            self.assertEqual(e.pane_of("lead-dev")["pane_id"], old)
            e.write_brief(UI.replace('model = "gpt-6-astra"', 'model = "gpt-6-luna"'))
            before = len(e.prompts())
            e.run("up", "lead-dev", "--restart")
            argv = native(e.starts()["lead-dev"])
            self.assertEqual(argv[:2], ["resume", "restart-session"])
            self.assertEqual(flag(argv, "-m"), "gpt-6-luna")
            self.assertEqual(flag(argv, "-C"), str(e.repo))
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertEqual(e.calls("pane", "close"), [["pane", "close", old]])
            self.assertNotIn(old, e.fake_state()["panes"])
            self.assertNotEqual(e.pane_of("lead-dev")["pane_id"], old)
            self.assertEqual([n for n, _ in e.prompts()[before:]], ["lead-dev"])
            self.assertIn("Send it with the say command", e.prompts()[-1][1])
            self.assertIn("picking up where we left off", e.prompts()[-1][1])
            self.assertEqual({n: {k: v for k, v in e.pane_of(n).items() if k != "rect"} for n in others}, others)
            e.run("up", "lead-dev", "--fresh", "--restart")
            self.assertNotIn("resume", native(e.starts()["lead-dev"]))
            self.assertIn("starting in a new session", e.prompts()[-1][1])

    def test_closed_pane_or_changed_harness_starts_fresh(self):
        with Env() as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("pm", {"agent": "pi", "kind": "path", "value": "/s/p.jsonl"})
            e.kill("pm")
            e.close("reviewer")
            e.write_brief(CATALOG.replace('model = "deepseek/deepseek-v4.1-flash-1017"', 'model = "gpt-6-sol"')
                          .replace('[members.pm]\nharness = "pi"', '[members.pm]\nharness = "codex"'))
            out = e.run("up", "pm", "reviewer").stdout
            self.assertIn("started @reviewer", out)
            by = {c[2]: native(c) for c in e.calls("agent", "start")[2:]}
            self.assertNotIn("resume", by["pm"])  # a pi session can't open in codex
            self.assertNotIn("resume", by["reviewer"])
            self.assertEqual(e.pane_of("reviewer")["pane_id"], "w1:p4")  # a new, labeled pane
            self.assertEqual(e.pane_of("reviewer")["tab_id"], e.pane_of("pm")["tab_id"])  # in the team tab
            self.assertIn("starting in a new session", dict(e.prompts())["pm"])
            self.assertTrue(dict(e.prompts())["pm"].endswith(READY + "\n"))  # a changed harness starts fresh
            self.assertTrue(dict(e.prompts())["reviewer"].endswith(READY + "\n"))

    def test_opencode_member_gets_a_new_pane_and_the_old_label_is_cleared(self):
        with Env(formation({
            'reviewer': {'harness': 'opencode', 'model': 'z-ai/glm-5.3'},
            'pm': {'harness': 'pi', 'model': 'z-ai/glm-5.3'},
        })) as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("reviewer", {"agent": "opencode", "kind": "id", "value": "ses_1"})
            e.kill("reviewer")
            e.run("up", "reviewer")
            labeled = [p for p in e.fake_state()["panes"].values() if p["label"] == "reviewer"]
            self.assertEqual([p["pane_id"] for p in labeled], ["w1:p4"])  # its environment is set at creation
            self.assertEqual(native(e.calls("agent", "start")[-1]), ["--standalone", "--session", "ses_1", "--auto"])

    def test_failed_resume_says_how_to_start_fresh(self):
        with Env() as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("pm", {"agent": "pi", "kind": "id", "value": "old"})
            e.kill("pm", "reviewer")
            p = e.run("up", check=False, FAKE_START_FAIL="dead:pm")
            self.assertEqual(p.returncode, 3)
            self.assertIn("factory.py up pm --fresh", p.stdout)
            self.assertIn("started @reviewer", p.stdout)  # the others still came back

    def test_duplicate_session_observations_keep_latest_resume(self):
        with Env() as e:
            e.run("up", "lead-dev", "--no-brief")
            for value in ("first", "second"):
                e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": value})
                e.run("status")
            history = next((e.root / "cache/factory-skill").glob("*/sessions.jsonl"))
            lines = history.read_text().splitlines()
            with history.open("a") as f:
                f.write(lines[-1] + "\n")
            e.kill("lead-dev")
            e.run("up", "lead-dev")
            self.assertEqual(native(e.starts()["lead-dev"])[:2], ["resume", "second"])


class DownTests(unittest.TestCase):
    def test_down_hints_at_repair_only_when_no_session_was_recorded(self):
        names = ("architect", "designer", "lead-dev", "reviewer")
        members = {name: {"harness": "opencode", "model": "openrouter/z-ai/glm-5.3"} for name in names}
        with Env(formation(members)) as e:
            e.run("up", "--no-brief")
            e.set_session("architect", {"agent": "opencode", "kind": "id", "value": "compatible"})
            e.set_session("designer", {"agent": "claude", "kind": "id", "value": "older"})
            e.run("status")  # save both older refs
            for name in ("architect", "designer"):
                e.set_session(name, None)
            p = e.run("down", FAKE_INTEGRATION_STATUS="opencode: needs repair (v13) (/hooks/opencode)")
            hints = [line for line in p.stdout.splitlines() if "may have prevented session recording" in line]
            self.assertEqual(len(hints), 2)
            self.assertIn("@lead-dev", hints[0])
            self.assertIn("@reviewer", hints[1])
            self.assertIn("no compatible session recorded for @designer: up will start it fresh\n", p.stdout)
            self.assertNotIn("no compatible session recorded for @architect", p.stdout)
            self.assertEqual(e.calls("integration"), [["integration", "status"]] * 2)  # one for up, one for down

    def test_down_records_sessions_for_up_and_preserves_messages(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "old"})
            e.run("status")
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "latest"})
            e.set_session("pm", {"agent": "pi", "kind": "path", "value": "/sessions/pm.jsonl"})
            e.update("lead-dev", agent_status="working")
            e.update("pm", agent_status="blocked")
            e.say("lead-dev", "operator", "acknowledged report")
            e.run("wait", "--seen", "1", "--timeout", "0")
            self.assertIn("queued message", e.say("lead-dev", "operator", "unread report").stdout)
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                draft = cfg.messages / "pm" / "pending.md"
                draft.write_text("unsent report")
                instruction = cfg.session_dir / "pm.md"
                with factory.Inbox(cfg) as inbox:
                    self.assertEqual([row["text"] for row in inbox.unread], ["unread report"])
                    preserved = {p: p.read_bytes() for p in (draft, instruction, inbox.path, inbox.state_path)}
            operator = e.pane_of("operator")
            targets = [e.pane_of(name)["pane_id"] for name in ("lead-dev", "pm")]
            out = e.run("down").stdout
            self.assertIn("stopped @lead-dev (was working)", out)
            self.assertIn("stopped @pm (was blocked)", out)
            self.assertIn("@reviewer was not running", out)
            self.assertIn(str(draft), out)
            self.assertEqual(e.calls("pane", "close"), [["pane", "close", pane] for pane in targets])
            self.assertEqual(e.pane_of("operator"), operator)
            self.assertEqual({p: p.read_bytes() for p in preserved}, preserved)
            self.assertNotIn("stopped @", e.run("down").stdout)

            # A plain up with current arguments resumes the sessions just saved by down.
            e.write_brief(formation({"lead-dev": {**CATALOG_MEMBERS["lead-dev"], "args": ["--no-alt-screen"]},
                                     "pm": CATALOG_MEMBERS["pm"]}))
            e.run("up")
            codex = native(e.starts()["lead-dev"])
            self.assertEqual(codex[:2], ["resume", "latest"])
            self.assertIn("--no-alt-screen", codex)
            self.assertEqual(flag(native(e.starts()["pm"]), "--session"), "/sessions/pm.jsonl")
            self.assertEqual([n for n, _ in e.prompts()], ["lead-dev", "pm"])
            for _, brief in e.prompts():
                self.assertIn("picking up where we left off", brief)

    def test_down_selects_names_and_leaves_empty_panes_and_caller(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
            e.kill("frontend-dev")
            empty = e.pane_of("frontend-dev")
            e.set_session("pm", {"agent": "pi", "kind": "id", "value": "not-stopping"})
            other = e.pane_of("pm")
            e.update("operator", label="reviewer", agent=None, name=None)  # invoking shell has a stale member label
            caller = e.pane_of("reviewer")
            out = e.run("down", "@lead-dev", "lead-dev", "frontend-dev", "reviewer").stdout
            self.assertEqual(out.count("stopped @lead-dev"), 1)
            self.assertIn("no compatible session recorded for @lead-dev: up will start it fresh", out)
            self.assertIn("@frontend-dev was not running", out)
            self.assertIn("@reviewer was not running", out)
            self.assertEqual(e.pane_of("frontend-dev"), empty)
            self.assertEqual(e.pane_of("reviewer"), caller)
            self.assertEqual(e.pane_of("pm"), other)
            self.assertEqual(len(e.calls("pane", "close")), 1)
            with patch.dict(os.environ, e.env):
                self.assertNotIn("pm", factory.past_sessions(factory.Config(e.repo)))

    def test_down_validates_before_closing_anything(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            cases = [("not a member", ("lead-dev", "unknown"), {}),
                     ("not a member", ("lead-dev", "operator"), {}),
                     ("not inside herdr", (), {"HERDR_ENV": "0"}),
                     ("HERDR_PANE_ID is not set", (), {"HERDR_PANE_ID": ""}),
                     ("not a member pane", (), {"HERDR_PANE_ID": e.pane_of("pm")["pane_id"]})]
            for message, args, env in cases:
                with self.subTest(message=message, args=args):
                    p = e.run("down", *args, check=False, **env)
                    self.assertNotEqual(p.returncode, 0)
                    self.assertIn(message, p.stderr)
            self.assertEqual(e.calls("pane", "close"), [])

    def test_down_leaves_mismatched_pane_and_continues_after_refusal(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "reviewer", "pm", "--no-brief")
            e.update("reviewer", name="unrelated")
            unrelated = e.pane_of("reviewer")
            lead = e.pane_of("lead-dev")["pane_id"]
            pm = e.pane_of("pm")["pane_id"]
            p = e.run("down", check=False, FAKE_CLOSE_FAIL=lead)
            self.assertEqual(p.returncode, 1)
            self.assertIn("could not stop @reviewer: pane label and agent name", p.stderr)
            self.assertIn("could not stop @lead-dev: close_refused", p.stderr)
            self.assertIn("stopped @pm", p.stdout)
            self.assertEqual(e.calls("pane", "close"), [["pane", "close", pane] for pane in (lead, pm)])
            self.assertEqual(e.pane_of("reviewer"), unrelated)
            self.assertIn("stopped @lead-dev", e.run("down", "lead-dev").stdout)

    def test_down_leaves_members_running_when_sessions_cannot_be_recorded(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "--no-brief")
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "cx-1"})
            with patch.dict(os.environ, e.env):
                (factory.Config(e.repo).cache / "sessions.jsonl").mkdir()
            p = e.run("down", check=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("could not record member sessions", p.stderr)
            self.assertEqual(e.calls("pane", "close"), [])

    def test_down_checks_each_target_again_before_closing(self):
        for fields, error in (({"agent": None, "name": None}, False),
                              ({"name": "unrelated", "label": "other"}, True)):
            with self.subTest(fields=fields), Env(UI) as e:
                e.run("up", "lead-dev", "pm", "--no-brief")
                lead = e.pane_of("lead-dev")["pane_id"]
                pm = e.pane_of("pm")
                state = e.fake_state()
                state["after_close"] = {"pane": pm["pane_id"], "fields": fields}
                e.write_fake(state)
                p = e.run("down", "lead-dev", "pm", check=False)
                self.assertEqual(p.returncode, int(error))
                if error:
                    self.assertIn("could not stop @pm: member identity changed", p.stderr)
                else:
                    self.assertIn("@pm was not running", p.stdout)
                self.assertEqual(e.calls("pane", "close"), [["pane", "close", lead]])
                self.assertEqual(e.fake_state()["panes"][pm["pane_id"]], {**pm, **fields})

    def test_down_resumes_with_an_interrupted_history_and_same_session_value(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "--no-brief")
            current = {"agent": "codex", "kind": "id", "value": "same"}
            e.set_session("lead-dev", current)
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                stale = {"member": "lead-dev", **current, "agent": "claude", "kind": "path"}
                (cfg.cache / "sessions.jsonl").write_text(json.dumps(stale) + '\n{"member":')
            e.run("down", "lead-dev")
            with patch.dict(os.environ, e.env):
                self.assertEqual(factory.past_sessions(cfg)["lead-dev"], {"member": "lead-dev", **current})
            e.run("up", "lead-dev")
            self.assertEqual(native(e.starts()["lead-dev"])[:2], ["resume", "same"])


class LiveToolTests(unittest.TestCase):
    @staticmethod
    def scrubber(host="local-host", user="local-user"):
        core = types.SimpleNamespace(dir="/tmp/factory-scratch-core", id="local-core-id")
        with patch("socket.gethostname", return_value=host), patch("getpass.getuser", return_value=user):
            return live.scrubber(core)

    def test_identity_fields_redacted_by_position(self):
        scrub = self.scrubber()
        for value in ("ci", "x", "another-user", 1000):
            with self.subTest(value=value):
                raw = {"core": {"name": str(value)}, "login_session": {"user": value, "id": 42, "gui": None}}
                expected_user = 1 if isinstance(value, int) else "<user>"
                self.assertEqual(scrub(raw), {"core": {"name": "<host>"},
                                              "login_session": {"user": expected_user, "id": 1, "gui": None}})

    def test_leaks_flags_any_unredacted_identity_field(self):
        scrub = self.scrubber()
        self.assertEqual(set(scrub.leaks({"core": {"name": "ci"}, "login_session": {"user": "x", "id": 8}})),
                         {"core.name", "login_session.user", "login_session.id"})

    def test_placeholders_are_not_leaks(self):
        scrub = self.scrubber(host="host", user="user")
        raw = {"core": {"name": "host"}, "login_session": {"user": 1000, "id": 2}, "cwd": "/home/user/host"}
        masked = scrub(raw)
        self.assertEqual(masked["cwd"], "/home/<user>/<host>")
        self.assertEqual(scrub(masked), masked)
        self.assertFalse(scrub.leaks(masked))

    def test_short_names_left_alone_inside_paths(self):
        scrub = self.scrubber(host="ci", user="ci")
        self.assertEqual(scrub("/srv/ci/build"), "/srv/ci/build")
        self.assertFalse(scrub.leaks("/srv/ci/build"))

    def test_protocol_words_that_look_like_names_survive(self):
        scrub = self.scrubber(host="idle", user="live")
        payload = {"state": "live", "reason": "idle", "status": {"state": "idle"},
                   "foreground": {"name": "idle"}, "labels": {"name": "live"}}
        self.assertEqual(scrub(payload), payload)
        self.assertFalse(scrub.leaks(payload))

    def test_scrubbed_fixture_still_conforms(self):
        original = {**HC_FIXTURES["replies"]["hello"]["result"], "login_session": {"id": 51, "user": 103}}
        cleaned = self.scrubber()(original)
        self.assertEqual(cleaned["login_session"], {"id": 1, "user": 1})
        self.assertEqual(HcContract.check_result("hello", cleaned), [])


class ConstantsTests(unittest.TestCase):
    def test_version_is_sha1_of_script(self):
        self.assertEqual(factory.VERSION, hashlib.sha1(SCRIPT.read_bytes()).hexdigest()[:12])


class ConfigTests(unittest.TestCase):
    @staticmethod
    def load_config(settings):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "factory.toml").write_text(settings)
            (root / "FACTORY.md").write_text("# Factory\n")
            return factory.Config(root)

    def test_new_keys_default(self):
        cfg = self.load_config("")
        self.assertEqual((cfg.quiet_grace, cfg.ready_check, cfg.ready_every), (120, None, 300))

    def test_new_keys_accepted(self):
        cfg = self.load_config('quiet_grace = 30\nready_check = "printf todo"\nready_check_every = 60\n')
        self.assertEqual((cfg.quiet_grace, cfg.ready_check, cfg.ready_every), (30, "printf todo", 60))

    def test_invalid_new_keys_are_rejected(self):
        for key, values in {
            "quiet_grace": ("false", "29", "30.5", '"30"'),
            "ready_check": ('""', '"  "', "true", "30"),
            "ready_check_every": ("true", "59", "60.5", '"60"'),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(SystemExit, rf"factory\.toml: {key}"):
                    self.load_config(f"{key} = {value}\n")

    def test_unknown_top_level_key_still_rejected(self):
        with self.assertRaisesRegex(SystemExit, r"unknown top-level key.*ready_checks"):
            self.load_config("ready_checks = 10\n")

    def test_v1_sample_loads_unchanged(self):
        cfg = self.load_config((factory.SKILL / "templates/factory.toml").read_text())
        self.assertEqual((cfg.quiet_grace, cfg.ready_check, cfg.ready_every), (120, None, 300))

    @staticmethod
    def files(root):
        return {str(p.relative_to(root)): ("symlink", os.readlink(p)) if p.is_symlink() else p.read_bytes()
                for p in root.rglob("*") if p.is_file() or p.is_symlink()}

    def test_dangling_links_stop_without_writes(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        for name in ("FACTORY.md", "factory.toml", "factory.toml.sample"):
            with self.subTest(name=name), Env(brief) as e:
                link = e.repo / name
                link.unlink(missing_ok=True)
                target = e.root / "missing target"
                link.symlink_to(target)
                before = self.files(e.repo)
                commands = ("init",) if name == "factory.toml.sample" else ("init", "check", "up")
                for command in commands:
                    result = e.run(command, check=False)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f"{link} is a symbolic link to a missing file", result.stderr)
                    self.assertIn("Repair or remove the link.", result.stderr)
                    self.assertEqual(self.files(e.repo), before)
                    self.assertFalse(target.exists())
                if name == "factory.toml.sample":
                    for command in (("check",), ("up", "--no-brief")):
                        e.run(*command)
                    self.assertIn("queued message 1", e.say("lead-dev", "operator", "report").stdout)
                    self.assertEqual(self.files(e.repo), before)
                    self.assertFalse(target.exists())

    def test_valid_formation_link_keeps_its_target_and_allows_reports(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        with Env(brief) as e:
            link = e.repo / "factory.toml"
            target = e.repo / "settings.toml"
            link.rename(target)
            link.symlink_to(target.name)
            before = self.files(e.repo)
            e.run("check")
            e.run("up", "--no-brief")
            self.assertIn("queued message 1", e.say("lead-dev", "operator", "report").stdout)
            self.assertEqual(self.files(e.repo), before)

    @unittest.skipUnless(GIT, "git is not installed")
    def test_parent_formation_is_a_marker_but_a_sample_is_not(self):
        for marker in ("factory.toml", "factory.toml.sample"):
            with self.subTest(marker=marker), Env(brief=None) as e:
                (e.root / "bin/git").symlink_to(GIT)
                (e.repo / marker).write_text("# parent file\n")
                nested = e.repo / "sub"
                subprocess.run([GIT, "init", "-q", str(nested)], check=True, capture_output=True)
                e.run("init", cwd=nested)
                chosen = e.repo if marker == "factory.toml" else nested
                other = nested if chosen == e.repo else e.repo
                self.assertTrue((chosen / "FACTORY.md").is_file())
                self.assertFalse((other / "FACTORY.md").exists())
                self.assertEqual((e.repo / marker).read_text(), "# parent file\n")

    def test_published_text_has_no_earlier_names(self):
        paths = [factory.SKILL / "SKILL.md", factory.SKILL / "harnesses.toml", factory.SKILL / "scripts/factory.py",
                 factory.SKILL.parent / "README.md"]
        paths.extend(factory.SKILL.parent / "docs" / name for name in ("README.md", "getting-started.md"))
        for directory in ("references", "roles", "templates", "recipes"):
            paths.extend(p for p in (factory.SKILL / directory).iterdir() if p.is_file())
        for path in paths:
            text = path.read_text()
            for name in ("TEAM.md", "team.toml", "team-brief", "a-team", "ateam.py", "ATEAM_"):
                self.assertNotIn(name, text, str(path))
        self.assertIn("name: factory\n", (factory.SKILL / "SKILL.md").read_text())
        self.assertTrue((factory.SKILL / "templates/FACTORY.md").read_text().startswith("# Factory\n"))

    def test_todo_item_format(self):
        first = "- [x] (1) First\n    * verdict: approved"
        second = "- [ ] (2) Second"
        assert_todo_items(self, first + "\n\n" + second)
        assert_todo_items(self, "## Ready\n\n" + second + "\n\n## Done\n\n" + first)
        for bad in (first + "\n" + second, first + "\n\n\n" + second, second + "\n\n    * detail",
                    first + "\n  * misindented detail", first + "\n    - wrong marker",
                    "- [ ] Title", "- [ ] (0) Title", "- [ ] (2)", "- [ ] Title (2)", "- [ ](2) Title",
                    "## Ready\n\n- [ ] (1) Duplicate\n\n## Done\n\n" + first):
            with self.subTest(text=bad), self.assertRaises(AssertionError):
                assert_todo_items(self, bad)

    def test_template_is_consistent(self):
        with Env(TEMPLATE) as e:
            cfg = factory.Config(e.repo)
            for name in cfg.members:
                p = factory.plan(cfg, name)
                self.assertIn(p["harness"], factory.HARNESSES, name)
                self.assertIn(p["model"], factory.TABLE[p["harness"]]["models"], name)  # the template uses recommended models
                self.assertEqual(p["permissions"], "full")
            self.assertEqual(cfg.operator["name"], "operator")
            self.assertTrue({"harness", "model", "thinking"}.isdisjoint(cfg.operator))
            self.assertIn("tracked in `TODO.md`", cfg.body)  # where tasks live is prose in FACTORY.md, not a setting
            todo = (factory.SKILL / "templates/TODO.md").read_text().splitlines()
            self.assertEqual(todo[:2], ["TODO", "===="])
            headings = ["## Ready", "## In progress", "## Backlog", "## Done"]
            self.assertEqual([line for line in todo if line.startswith("## ")], headings)
            tasks = cfg.body.split("## Tasks\n", 1)[1].split("\n## Workflow", 1)[0]
            for heading in headings:
                self.assertIn(heading, tasks)
            self.assertNotIn(" -- ", tasks)
            example = re.search(r"```[^\n]*\n(.*?)\n```", tasks, re.S)
            self.assertIsNotNone(example)
            assert_todo_items(self, example.group(1))
            self.assertRegex(example.group(1), r"\n\n- \[ \] \([1-9][0-9]*\) [^\n]+\Z")  # the second example is title-only
            self.assertRegex(example.group(1), r"(?m)^        (?:\* |\d+\. )")
            self.assertNotIn("tracker", cfg.team)
            self.assertEqual(list(cfg.members), ["lead-dev", "reviewer", "pm"])
            self.assertEqual(cfg.members["lead-dev"]["model"], "gpt-6-astra")
            self.assertEqual(cfg.members["reviewer"]["model"], "gpt-6-luna")
            self.assertIn("## Project", cfg.body)
        self.assertFalse(any("/" in m for m in factory.TABLE["codex"]["models"]), "Codex takes bare model names")
        for h, t in factory.TABLE.items():  # every harness has the same shape, and code to go with it
            self.assertEqual(set(t["permissions"]), set(factory.LEVELS), h)
            self.assertIn(t["prompt"], ("file", "pointer", "message"), h)
            self.assertIn(t.get("operator_wait"), ("background", "foreground", None), h)
            self.assertNotIn("operator_messages", t, h)
            self.assertIn(h, factory.CODE)

    def test_operator_table_and_name(self):
        team = '[members.lead-dev]\nharness = "codex"\nmodel = "gpt-6-luna"\n'
        stock = (factory.SKILL / "templates/FACTORY.md").read_text()  # the shared brief people really get
        with Env(team_brief('[operator]\nname = "boss"\nharness = "claude"\n' + team, stock)) as e:
            e.run("up")
            pane = e.fake_state()["panes"]["w1:p1"]
            self.assertEqual((pane["name"], pane["label"]), ("boss", "boss"))  # its @name, pane label and agent name
            self.assertRegex(e.run("status").stdout, r"@boss +operator")
            brief = dict(e.prompts())["lead-dev"]
            self.assertIn("# Team brief (from @boss)", brief)
            self.assertIn("1. The operator assigns tasks", brief)  # the shared brief names the role ...
            self.assertNotIn("@operator", brief)                   # ... never one person's operator
            e.write_brief(team_brief(team, "## Workflow\n\n{operator} and {name} stay as written.\n"))
            self.assertIn("{operator} and {name} stay as written.", e.run("brief").stdout)  # FACTORY.md is sent as it is
            e.write_brief(team_brief('[operator]\nname = "boss"\nharness = "claude"\n' + team, stock))
            prompt = e.run("prompt", "lead-dev").stdout
            self.assertIn("led by its operator, @boss", prompt)  # roles and the member prompt follow the name
            self.assertNotIn("@operator", prompt)
            for settings, why in (('[operator]\nname = "Boss!"\n', "[operator] name 'Boss!' must be"),
                                  ('[operator]\nname = "lead-dev"\n', "not a member's name")):
                e.write_brief(team_brief(settings + team))
                result = e.run("check", check=False)
                self.assertEqual(result.returncode, 1, settings)
                self.assertIn(why, result.stderr)

    def test_unknown_top_level_keys_fail(self):
        for key in ("name", "harness", "model", "thinking", "notes", "typo"):
            brief = CATALOG.replace("+++\n", f'+++\n{key} = "misplaced"\n', 1)
            with self.subTest(key=key), Env(brief) as e:
                for command in ("check", "brief"):
                    result = e.run(command, check=False)
                    self.assertEqual(result.returncode, 1, command)
                    self.assertIn(f"{e.repo / 'factory.toml'}: unknown top-level key(s): ['{key}']", result.stderr)
                    self.assertIn("operator fields belong under [operator]", result.stderr)

    def test_member_requires_explicit_harness(self):
        with Env('+++\n[members.lead-dev]\nmodel = "gpt-6-luna"\n+++\n') as e:
            result = e.run("check", "lead-dev", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("harness", result.stderr + result.stdout)

    def test_brief_and_formation_are_two_files(self):
        team = '[members.lead-dev]\nharness = "codex"\nmodel = "gpt-6-luna"\nworktree = true\n'
        with Env(team_brief(team, "# Team\n\n<!-- for the editor -->\n## Tasks\n\nGitHub Issues on o/r, with `gh issue`.\n"), git=True) as e:
            sent = e.run("brief").stdout
            self.assertIn("GitHub Issues on o/r", sent)       # FACTORY.md is the prose of the brief ...
            self.assertNotIn("\n# Team\n", sent)              # ... without its own title
            self.assertNotIn("for the editor", sent)           # ... and without its comments
            self.assertIn("branch `ada/lead-dev`", sent)       # worktree branches start with the user's name
            self.assertIn("The team brief says where tasks are tracked", e.run("prompt", "lead-dev").stdout)
            e.write_brief(team_brief('user = "peter"\n' + team))
            self.assertEqual(factory.plan(factory.Config(e.repo), "lead-dev")["branch"], "peter/lead-dev")
            e.write_brief(team_brief(team))  # a VM or CI user name that someone else's machine may share: a note, not a failure
            generic = e.run("check", LOGNAME="root")
            self.assertIn("note: worktree branches will start with 'root'", generic.stderr)
            self.assertIn("Set `user` in factory.toml", generic.stderr)
            self.assertNotIn("note:", e.run("check").stderr)               # an ordinary system user name
            e.write_brief(team_brief('user = "root"\n' + team))
            self.assertNotIn("note:", e.run("check", LOGNAME="root").stderr)  # chosen in factory.toml
            e.write_brief(team_brief(team.replace("worktree = true\n", "")))
            self.assertNotIn("note:", e.run("check", LOGNAME="root").stderr)  # no branch depends on it
            e.write_brief(team_brief('user = "a b"\n' + team))
            self.assertIn("set `user`", e.run("check", check=False).stderr)
            (e.repo / "factory.toml").unlink()                    # a teammate's clone: the brief is there, the formation is not
            p = e.run("check", check=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("no factory.toml in", p.stderr)
            self.assertIn("factory.py init", p.stderr)

    def test_brief_validation(self):
        cases = [
            (team_brief('[members.wizard]\nharness = "claude"'), "no role 'wizard'"),
            (team_brief('[members.Bad]\nharness = "claude"\nrole = "pm"'), "role"),
            (team_brief('[tabs]\nteam = "crew"'), "unknown top-level key(s): ['tabs']"),
            (formation({"backend-dev-" + "2" * 25: {"role": "backend-dev", "harness": "codex"}}), "member"),
            (team_brief("members = [unclosed"), "factory.toml:"),
            (team_brief('[members.dev]\nrole = "backend-dev"\nharness = "codex"'), "role"),
        ]
        for brief, error in cases:
            with self.subTest(error=error), Env(brief) as e:
                result = e.run("check", "pm", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
        with Env(formation({
            "backend-dev": {"harness": "codex", "model": "gpt-6-luna"},
            "backend-dev-2": {"harness": "codex", "role": "backend-dev", "model": "gpt-6-luna"},
        })) as e:
            self.assertIn("@backend-dev-2", e.run("check", "backend-dev", "backend-dev-2").stdout)

    @unittest.skipUnless(GIT, "git is not installed")
    def test_init(self):
        with Env(brief=None) as e:
            (e.root / "bin/git").symlink_to(GIT)
            subprocess.run([GIT, "init", "-q", str(e.repo)], check=True, capture_output=True)
            out = e.run("init", cwd=e.repo / "sub").stdout     # in a repository: at its root
            self.assertEqual(sorted(p.name for p in e.repo.iterdir() if p.name != ".git"),
                             [".gitignore", "FACTORY.md", "factory.toml", "factory.toml.sample", "sub"])
            self.assertEqual((e.repo / "factory.toml").read_text(), (factory.SKILL / "templates/factory.toml").read_text())
            self.assertEqual((e.repo / ".gitignore").read_text(), "factory.toml\n")
            self.assertEqual(out.count("wrote"), 3)
            e.run("check", "lead-dev")                         # what init writes is a team that checks
            self.assertIn("`## Ready` holds, in the order they will be done, the items the operator may assign "
                          "without asking the user; only a person puts an item there or reorders it, never the team.",
                          e.run("brief").stdout)
            # a teammate: the shared files are there, edited; only their own factory.toml is missing
            (e.repo / "factory.toml.sample").write_text('[members.pm]\nharness = "pi"\nmodel = "z-ai/glm-5.3"\n')
            (e.repo / "FACTORY.md").write_text("# Team\n\nOurs.\n")
            (e.repo / "factory.toml").unlink()
            out = e.run("init").stdout
            self.assertEqual((out.count("kept"), out.count("wrote")), (2, 1))
            self.assertEqual(list(factory.Config(e.repo).members), ["pm"])        # copied from the project's sample
            self.assertEqual((e.repo / "FACTORY.md").read_text(), "# Team\n\nOurs.\n")
            self.assertEqual((e.repo / ".gitignore").read_text(), "factory.toml\n")  # listed once
            self.assertEqual(e.run("init").stdout.count("wrote"), 0)             # nothing is ever replaced
            self.assertNotIn("sets user", out)
            # the project's sample names a user: whose branches these are is said, not guessed
            (e.repo / "factory.toml.sample").write_text('user = "peter"\n[members.pm]\nharness = "pi"\nmodel = "z-ai/glm-5.3"\n')
            (e.repo / "factory.toml").unlink()
            out = e.run("init", LOGNAME="jane").stdout
            self.assertIn("factory.toml.sample sets user = 'peter'", out)
            self.assertIn("change `user` in factory.toml", out)
            # a later negation undoes the ignore line: what git really does decides
            (e.repo / ".gitignore").write_text("factory.toml\n!factory.toml\n")
            self.assertIn("added factory.toml", e.run("init").stdout)
            self.assertEqual((e.repo / ".gitignore").read_text(), "factory.toml\n!factory.toml\nfactory.toml\n")
            self.assertNotIn("added factory.toml", e.run("init").stdout)
            self.assertNotIn("already tracks", e.run("init").stdout)
            subprocess.run([GIT, "-C", str(e.repo), "add", "-f", "factory.toml"], check=True, capture_output=True)
            self.assertIn("git already tracks factory.toml", e.run("init").stdout)  # ignoring it does not untrack it
        with Env(brief=None) as e:
            e.run("init", cwd=e.repo / "sub")                  # no repository: where you are
            self.assertTrue((e.repo / "sub/factory.toml").is_file())

    def test_brief_preview_and_member_prompt(self):
        with Env() as e:
            text = e.run("brief").stdout
            self.assertIn("| **@product** | Claude Code, claude-opus-5-5, medium |", text)
            self.assertIn("No reply needed.", text)
            self.assertIn("factory.py say", text)
            self.assertNotIn("Works in", text)  # no worktrees on this team
            prompt = e.run("prompt", "reviewer").stdout
            self.assertIn("run it only when the brief's Review section or the requester calls for the deep lane", prompt)
            self.assertIn(" NAME --file ", prompt)
            self.assertIn("/messages/reviewer/FILENAME", prompt)
            self.assertIn("Start a task only when @operator or the user assigns it to you; an item the brief or "
                          "tracker marks ready is not an assignment. A teammate's request for a review or advice "
                          "needs none.", prompt)
            self.assertIn("You are read-only", prompt)
            self.assertIn("build and run", prompt)  # read-only permits builds/tests
            self.assertIn("## Reports", prompt)
            learned = "learned: <what a later task would otherwise get wrong>"
            self.assertNotIn(learned, prompt)
            self.assertIn("printed retry command once", prompt)
            self.assertNotIn("herdr agent prompt", prompt)
            self.assertNotIn("{instructions}", prompt)
            notes = factory.SKILL / "references/notes.md"
            self.assertIn(f"read `{notes}` before your first task: it covers searching, writing and the "
                          "`learned:` line of a report", prompt)
            self.assertIn(learned, notes.read_text())
            self.assertIn("reviewer.md", prompt)
            self.assertNotIn("{operator}", prompt)

    def test_brief_operator_details_and_fallbacks(self):
        operator = {"harness": "claude", "model": "claude-opus-5-5", "thinking": "medium"}
        agent = "Claude Code, claude-opus-5-5, medium"
        notes = "Decides scope and architecture."
        for brief, expected in (
            (formation(CATALOG_MEMBERS, operator={**operator, "notes": notes}), f"{agent} | {notes}"),
            (formation(CATALOG_MEMBERS, operator=operator), f"{agent} | Operator."),
            (formation(CATALOG_MEMBERS, operator={**operator, "notes": ""}), f"{agent} | Operator."),
            (CATALOG, " | Operator."),
        ):
            with self.subTest(expected=expected), Env(brief) as e:
                self.assertIn(f"| **@operator** | {expected} |", e.run("brief").stdout)

    def test_every_role_has_the_same_four_parts(self):
        for path in sorted((factory.SKILL / "roles").glob("*.md")):
            _, body = factory.parse_role(path)
            heads = [line for line in body.splitlines() if line.startswith("**")]
            self.assertEqual(heads, ["**You own**", "**You output**", "**Done when**", "**Never**"], path.name)
            self.assertNotIn("LSP", body, path.name)  # nothing harness-specific in a role

    def test_session_id_from_ref(self):
        self.assertEqual(factory.session_id({"kind": "id", "value": "abc"}), "abc")
        path = "/x/rollout-2026-11-11T13-02-52-01a1cbc6-e708-7623-986f-cc2e07efa28f.jsonl"
        self.assertEqual(factory.session_id({"kind": "path", "value": path}), "01a1cbc6-e708-7623-986f-cc2e07efa28f")

    def test_bad_role_frontmatter_names_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.md"
            p.write_text("---\nthinking: low\noops\n---\nbody\n")
            with self.assertRaisesRegex(ValueError, "x.md:3"):
                factory.parse_role(p)


class ListingTests(unittest.TestCase):
    def test_listed_models_formats(self):
        pi = "provider  model  context\nopenrouter  z-ai/glm-5.3  1M\npax-vllm  nvidia/Q  262K\n"
        self.assertEqual(factory.listed_models(pi), ["openrouter/z-ai/glm-5.3", "pax-vllm/nvidia/Q"])
        self.assertEqual(factory.listed_models("openrouter/z-ai/glm-5.3\nollama/qwen3:8b\n"),
                         ["openrouter/z-ai/glm-5.3", "ollama/qwen3:8b"])
        omp = ("openrouter (2)\n┌──────┬─────┐\n│ model              │ context │\n├─────┼─────┤\n"
               "│ z-ai/glm-5.3       │      1M │\n│ z-ai/glm-5.3-flash │      1M │\n└─────┴─────┘\n"
               "ollama (1)\n┌─────┬─────┐\n│ model    │ context │\n├─────┼─────┤\n│ qwen3:8b │    128K │\n└─────┴─────┘\n")
        self.assertEqual(factory.listed_models(omp), ["openrouter/z-ai/glm-5.3", "openrouter/z-ai/glm-5.3-flash", "ollama/qwen3:8b"])


class DocumentationTests(unittest.TestCase):
    def test_relative_links_and_headings(self):
        root = factory.SKILL.parent
        for page in [root / "README.md", *sorted((root / "docs").glob("*.md"))]:
            for href in re.findall(r"\]\(([^\s)]+)\)", page.read_text()):
                link = urlsplit(href)
                if link.scheme or link.netloc:
                    continue
                with self.subTest(page=page.name, href=href):
                    target = page.parent / unquote(link.path) if link.path else page
                    self.assertTrue(target.exists(), str(target))
                    if link.fragment:
                        document = target / "README.md" if target.is_dir() else target
                        headings = re.findall(r"^#{1,6} (.+)$", document.read_text(), re.MULTILINE)
                        anchors = [re.sub(r"[^\w -]", "", h.lower()).replace(" ", "-") for h in headings]
                        self.assertIn(unquote(link.fragment), anchors)


class RecipeTests(unittest.TestCase):
    def test_recipes_are_pasteable_sections_linked_from_the_map(self):
        setup = (factory.SKILL / "references/cmd_setup.md").read_text()
        overview = (factory.SKILL.parent / "docs/README.md").read_text()
        recipes = sorted((factory.SKILL / "recipes").glob("*.md"))
        self.assertTrue(recipes)
        for path in recipes:
            with self.subTest(recipe=path.name):
                headings, blocks, block = [], [], None
                for line in path.read_text().splitlines():
                    if line.startswith("```"):
                        if block is None:
                            self.assertEqual(headings[-1], "## FACTORY.md section")
                            block = []
                        else:
                            blocks.append(block)
                            block = None
                    elif block is not None:
                        block.append(line)
                    elif line.startswith("#"):
                        headings.append(line)
                self.assertIsNone(block, "unclosed fence")
                self.assertEqual(len(blocks), 1)
                self.assertTrue(blocks[0][0].startswith("## "))
                section = blocks[0][0]
                self.assertIn(f"`{section}`", setup)
                self.assertTrue(headings[0].startswith(f"# {section[3:]}: "))
                self.assertEqual(headings[1:], ["## Needs", "## Check", "## FACTORY.md section", "## Tested with"])
                self.assertIn(f"(../skill/recipes/{path.name})", overview)


class CacheNameTests(unittest.TestCase):
    def test_new_projects_use_factory_cache_with_xdg_or_home(self):
        with Env() as e:
            for xdg in (str(e.root / "cache"), ""):
                with self.subTest(xdg=xdg), patch.dict(os.environ, {**e.env, "XDG_CACHE_HOME": xdg}):
                    cache = factory.Config(e.repo).cache
                    base = pathlib.Path(xdg) if xdg else e.home / ".cache"
                    (base / "a-team" / cache.name).mkdir(parents=True)
                    self.assertEqual(factory.Config(e.repo).cache, cache)
                    self.assertEqual(cache.parent, base / "factory-skill")
                    self.assertFalse(cache.exists())


class InstalledCopyTests(unittest.TestCase):
    def test_skill_runs_without_the_rest_of_the_checkout(self):
        head = (factory.SKILL / "SKILL.md").read_text().split("---\n", 2)[1]
        name = next(line.split(":", 1)[1].strip() for line in head.splitlines() if line.startswith("name:"))
        with Env(brief=None) as e:
            installed = e.root / "installed skills" / name
            shutil.copytree(factory.SKILL, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            script = installed / "scripts" / SCRIPT.name

            def run(*args):
                result = subprocess.run([sys.executable, str(script), *args, "--cwd", str(e.repo)],
                                        cwd=e.repo, env=e.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(str(factory.SKILL), result.stdout + result.stderr)
                return result.stdout

            harnesses = run("harnesses")
            self.assertRegex(harnesses, r"codex\s+available: gpt-6\.2-astra, gpt-6\.2-sol,")
            for harness in factory.HARNESSES:
                self.assertRegex(harnesses, rf"{harness}\s+available:")
            run("init")
            self.assertEqual((e.repo / factory.BRIEF).read_text(),
                             (installed / "templates" / factory.BRIEF).read_text())
            self.assertEqual((e.repo / factory.CONF).read_text(),
                             (installed / "templates" / factory.CONF).read_text())
            self.assertIn("ok", run("check"))
            prompt = run("prompt", "reviewer")
            self.assertIn("You are @reviewer, the reviewer", prompt)
            self.assertIn("You are read-only", prompt)
            self.assertIn(str(installed / "references/notes.md"), prompt)


@unittest.skipUnless(GIT, "git is not installed")
class GateFixtureTests(unittest.TestCase):
    def test_each_build_is_fresh_and_preserves_existing_fixtures_and_caches(self):
        with tempfile.TemporaryDirectory(prefix="gate1 test ") as directory:
            parent = pathlib.Path(directory)
            env = {**os.environ, "XDG_CACHE_HOME": str(parent / "cache"), "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"}
            unrelated = parent / "cache/factory-skill/factory-gate1-unrelated/sessions.jsonl"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("unrelated session")
            fixtures, caches = [], []

            def run(*args, cwd=parent):
                return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True)

            for _ in range(2):
                before = set(parent.glob("factory-gate1.*"))
                run("bash", str(pathlib.Path(__file__).with_name("gate1-setup.sh")), str(factory.SKILL), str(parent))
                created = set(parent.glob("factory-gate1.*")) - before
                self.assertEqual(len(created), 1)
                repo = created.pop()
                fixtures.append(repo)
                with patch.dict(os.environ, env):
                    cache = factory.Config(repo).cache
                self.assertNotIn(cache, caches)
                self.assertFalse(cache.exists())
                caches.append(cache)
                self.assertEqual(run(GIT, "rev-list", "--count", "HEAD", cwd=repo).stdout.strip(), "3")
                self.assertIn("slug: a one-function", (repo / "FACTORY.md").read_text())
                todo = (repo / "TODO.md").read_text()
                self.assertEqual(todo.splitlines()[:2], ["TODO", "===="])
                self.assertEqual([line for line in todo.splitlines() if line.startswith("## ")],
                                 ["## Ready", "## In progress", "## Backlog", "## Done"])
                t1 = run(GIT, "rev-parse", "--short", "HEAD~1", cwd=repo).stdout.strip()
                self.assertNotIn(" -- ", todo)
                self.assertEqual([line for line in todo.splitlines() if line.startswith("- [")], [
                    "- [ ] (3) Command line",
                    "- [ ] (2) max_length (@lead-dev)",
                    f"- [x] (1) Slugify joins words ({t1})",
                    "- [x] (4) Strip accents (0f3a9c1)",
                ])
                assert_todo_items(self, todo)
                self.assertIn("    * verdict: changes required by @reviewer", todo)
                self.assertIn("        2. Add a test where max_length cuts right after a word.", todo)
                self.assertNotEqual(subprocess.run([GIT, "cat-file", "-e", "0f3a9c1"], cwd=repo,
                                                   env=env, capture_output=True).returncode, 0)
                self.assertEqual(set(run(GIT, "status", "--short", cwd=repo).stdout.splitlines()),
                                 {" M README.md", " M slug.py", "?? test_slug_length.py"})
                tests = run(sys.executable, "-m", "unittest", cwd=repo)
                self.assertIn("Ran 3 tests", tests.stderr)
                result = run(sys.executable, "-c",
                             'from slug import slugify; print(slugify("hello world", max_length=6))', cwd=repo)
                self.assertEqual(result.stdout.strip(), "hello-")
                cache.mkdir(parents=True)
                (cache / "sessions.jsonl").write_text("previous fixture session")
                (repo / "preserve-me").write_text("previous fixture")
            self.assertEqual(unrelated.read_text(), "unrelated session")
            for repo, cache in zip(fixtures, caches):
                self.assertEqual((repo / "preserve-me").read_text(), "previous fixture")
                self.assertEqual((cache / "sessions.jsonl").read_text(), "previous fixture session")


class HerdrContract(unittest.TestCase):
    """The fields factory.py reads must exist where it reads them. The fake above follows the same split."""

    @unittest.skipUnless(shutil.which("herdr"), "herdr is not installed")
    def test_agy_member_kind(self):
        help_text = subprocess.run(["herdr", "agent"], capture_output=True, text=True)
        self.assertRegex(help_text.stderr + help_text.stdout, r"kinds: [^\n]*\bagy\b")

    def test_pane_and_agent_records(self):
        if not shutil.which("herdr"):
            self.skipTest("herdr is not installed")
        p = subprocess.run(["herdr", "api", "schema", "--json"], capture_output=True, text=True)
        if p.returncode:
            self.skipTest("herdr api schema is not available")
        defs = {}

        def walk(o):
            if isinstance(o, dict):
                defs.update(o.get("$defs", {}))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(json.loads(p.stdout))
        pane, agent = defs["PaneInfo"]["properties"], defs["AgentInfo"]["properties"]
        for field in ("pane_id", "tab_id", "label", "agent", "agent_status", "agent_session"):
            self.assertIn(field, pane)
        self.assertNotIn("name", pane)  # which is why Team reads `agent list` as well
        for field in ("pane_id", "name", "agent"):
            self.assertIn(field, agent)
        self.assertEqual(set(defs["AgentSessionInfo"]["properties"]), {"agent", "kind", "source", "value"})
        self.assertIn("text", defs["PaneReadResult"]["properties"])
        self.assertIn("foreground_processes", defs["PaneProcessInfo"]["properties"])
        self.assertLessEqual({"name", "argv"}, set(defs["PaneProcessInfoProcess"]["properties"]))
        self.assertEqual(set(defs["ErrorBody"]["properties"]), {"code", "message"})
        for params, field in (("TabCreateParams", "env"), ("PaneSplitParams", "env"), ("AgentStartParams", "args")):
            self.assertIn(field, defs[params]["properties"])


@unittest.skipUnless(os.environ.get("FACTORY_LIVE"), "set FACTORY_LIVE=1 (outside any sandbox) to check real harness flags")
class LiveFlags(unittest.TestCase):
    """Harness flags drift between releases. Each installed harness's --help must still name what we pass."""
    FLAGS = {
        "claude": (["--help"], ["--model", "--effort", "--append-system-prompt", "--name", "--settings", "--permission-mode",
                                '"auto"', '"bypassPermissions"', "--resume"]),
        "codex": (["--help"], ["--model", "--config", "--cd", "--approve-for-me", "--add-dir",
                               "--dangerously-bypass-approvals-and-sandbox", "resume"]),
        "pi": (["--help"], ["--model", "--thinking", "--append-system-prompt", "--name", "--session", "--list-models"]),
        "omp": (["--help"], ["--model", "--thinking", "--append-system-prompt", "--resume", "--approval-mode"]),
        "opencode": (["--help"], ["--standalone", "--session", "--auto", "models"]),
        "agy": (["--help"], ["--model", "--mode", "accept-edits", "--settings", "--conversation", "models"]),
    }

    @unittest.skipUnless(shutil.which("agy"), "agy is not installed")
    def test_agy_model_listing(self):
        self.assertTrue(factory.agy_models(), "agy models returned no tab-separated model IDs")

    def test_flags_exist(self):
        for harness, (argv, flags) in self.FLAGS.items():
            if not shutil.which(harness):
                continue
            # Claude's help can truncate when piped; a regular file preserves the full output.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
                p = subprocess.run([harness, *argv], stdout=output, stderr=subprocess.STDOUT, timeout=60)
                output.seek(0)
                help_text = output.read()
            self.assertEqual(p.returncode, 0, f"{harness} --help failed: {help_text}")
            for f in flags:
                self.assertIn(f, help_text, f"{harness} --help no longer mentions {f}")


if __name__ == "__main__":
    unittest.main()
