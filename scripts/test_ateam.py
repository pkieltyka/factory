"""Tests for ateam.py with a fake `herdr` and fake harness binaries; nothing real is launched.

Two test classes touch real tools and skip when they cannot: HerdrContract reads the installed herdr's API
schema, and LiveFlags (ATEAM_LIVE=1, outside any sandbox) greps each installed harness's --help for the
flags ateam.py passes."""
import importlib.util
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest

SCRIPT = pathlib.Path(__file__).with_name("ateam.py")
spec = importlib.util.spec_from_file_location("ateam", SCRIPT)
ateam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ateam)
TEMPLATE = (ateam.SKILL / "templates/team-brief.md").read_text()
GIT = shutil.which("git")

FAKE_HERDR = textwrap.dedent(r'''
    #!/usr/bin/env python3
    # A herdr stand-in. Like herdr: `pane list` carries labels but no agent names, `agent list` carries names
    # but no labels; a pane keeps its label but loses its agent_session when its agent exits; a prompt to a
    # blocked agent is refused; errors are JSON on stderr with exit status 1.
    import json, os, sys
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
                      "cwd": opt("--cwd"), "rect": rect, "env": [a[i + 1] for i, x in enumerate(a) if x == "--env"]}
        return panes[pid]
    if a[:2] == ["agent", "list"]:
        out({"type": "agent_list", "agents": [view(p, "agent") for p in panes.values() if p.get("agent")]})
    if a[:2] == ["agent", "get"]:
        p = find(a[2])
        out({"type": "agent_info", "agent": view(p, "agent")}) if p and p.get("agent") else fail("agent_not_found")
    if a[:2] == ["agent", "rename"]:
        find(a[2])["name"] = a[3]; out({"type": "agent_info", "agent": view(find(a[3]), "agent")})
    if a[:2] == ["pane", "list"]:
        out({"type": "pane_list", "panes": [view(p, "pane") for p in panes.values()]})
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
    if a[:2] == ["pane", "read"]:
        save(); print(f"...\nAllow this command? (y/n)  [{a[2]}]"); sys.exit(0)  # plain text, like herdr
    if a[:2] == ["agent", "start"]:
        name, kind, pane = a[2], opt("--kind"), opt("--pane")
        how = dict(x.split(":")[::-1] for x in os.environ.get("FAKE_START_FAIL", "").split(",") if x).get(name)
        if panes[pane].get("agent"):
            fail("pane_busy")
        if how == "dead":
            fail("timeout", "agent did not appear")
        panes[pane].update(agent=kind, name=name, agent_status="blocked" if how == "blocked" else "idle")
        if how in ("blocked", "banner"):  # a banner clears by itself: the agent is idle a moment later
            fail("agent_not_ready", "agent is waiting on a dialog")
        out({"type": "agent_started", "agent": view(panes[pane], "agent")})
    if a[:2] == ["agent", "prompt"]:
        p = find(a[2])
        if not p or not p.get("agent"):
            fail("agent_not_found")
        if p.get("agent_status") == "blocked":
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
    if sys.argv[1:] == ["--version"]:
        print(os.environ.get("FAKE_OPENCODE_VERSION", "opencode v2.2.4+abc")); sys.exit(0)
    if sys.argv[1:] == ["models", "--standalone"]:
        for l in os.environ.get("FAKE_MODELS", "").splitlines():
            if l.strip():
                p, m = l.split(); print(f"{p}/{m}")
''').lstrip()

MODELS = """\
openrouter z-ai/glm-5.3
openrouter z-ai/glm-5.3-flash
openrouter deepseek/deepseek-v4.1-flash
openrouter openai/gpt-6-astra
pax-vllm nvidia/Qwen3.8-Flash-Next-NVFP4
"""

LEVELS = ["low", "medium", "high", "xhigh", "max"]
CODEX_CACHE = {"models": [
    {"slug": "gpt-6-luna", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
    {"slug": "gpt-6-astra", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6.1-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
    {"slug": "gpt-reserve", "visibility": "hide", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
]}


def team_brief(settings, body="## Workflow\n\n1. {lead} hands out work.\n"):
    return "+++\n" + textwrap.dedent(settings).strip() + "\n+++\n" + body


class Env:
    """A temp repo with team-brief.md at its root (the template unless given), a fake herdr, fake harnesses,
    a Codex model cache, and the lead (a claude agent) in pane w1:p1 of tab w1:t1."""

    def __init__(self, brief=TEMPLATE, harnesses=("claude", "codex", "pi", "omp", "opencode"), models=MODELS, git=False):
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
        scripts = {"herdr": FAKE_HERDR, "pi": FAKE_PI, "omp": FAKE_OMP, "opencode": FAKE_OPENCODE,
                   "claude": "#!/bin/sh\n", "codex": "#!/bin/sh\n"}
        for name, body in scripts.items():
            if name == "herdr" or name in harnesses:
                (bin_ / name).write_text(body)
                (bin_ / name).chmod(0o755)
        self.fake = self.root / "herdr.json"
        lead = {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "label": None, "agent": "claude",
                "name": None, "agent_status": "working", "rect": {"x": 0, "y": 0, "width": 200, "height": 50}}
        self.write_fake({"calls": [], "next": 1, "tabs": {"w1:t1": "1"}, "panes": {"w1:p1": lead}})
        self.env = {**os.environ, "HERDR_BIN": str(bin_ / "herdr"), "FAKE_HERDR": str(self.fake),
                    "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_TAB_ID": "w1:t1", "HERDR_WORKSPACE_ID": "w1",
                    "PATH": str(bin_), "HOME": str(self.home), "XDG_CACHE_HOME": str(self.root / "cache"),
                    "FAKE_MODELS": models, "ATEAM_SETTLE": "0.2"}
        self.env.pop("CODEX_HOME", None)
        if git:  # a real repo with one commit, for worktrees
            (bin_ / "git").symlink_to(GIT)
            for cmd in (["init", "-q", "-b", "main"], ["add", "-A"],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"]):
                subprocess.run([GIT, "-C", str(self.repo), *cmd], check=True, capture_output=True,
                               env={**self.env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def write_brief(self, text):
        (self.repo / "team-brief.md").write_text(text)

    def write_fake(self, s):
        self.fake.write_text(json.dumps(s))

    def fake_state(self):
        return json.loads(self.fake.read_text())

    def run(self, *args, check=True, cwd=None, **env):
        p = subprocess.run([sys.executable, str(SCRIPT), args[0], "--cwd", str(cwd or self.repo), *args[1:]],
                           capture_output=True, text=True, env={**self.env, **env})
        if check and p.returncode:
            raise AssertionError(p.stderr + p.stdout)
        return p

    def calls(self, *prefix):
        return [c for c in self.fake_state()["calls"] if c[:len(prefix)] == list(prefix)]

    def starts(self):
        return {c[2]: c for c in self.calls("agent", "start")}

    def prompts(self):
        return [(c[2], c[3]) for c in self.calls("agent", "prompt")]

    def pane_of(self, name):
        return next(p for p in self.fake_state()["panes"].values() if p.get("label") == name)

    def update(self, name, **fields):
        fs = self.fake_state()
        next(p for p in fs["panes"].values() if p.get("label") == name).update(fields)
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
    def test_default_team_launches_in_the_team_tab_and_is_briefed(self):
        with Env() as e:
            e.run("up", cwd=e.repo / "sub")  # team-brief.md is found from a subdirectory too
            fs = e.fake_state()
            self.assertEqual((fs["panes"]["w1:p1"]["name"], fs["panes"]["w1:p1"]["label"]), ("cto", "cto"))
            self.assertEqual(fs["tabs"], {"w1:t1": "a-team driver", "w1:t2": "a-team workers"})  # the lead's tab, and one for the team
            e.write_brief(TEMPLATE.replace("# [tabs]", "[tabs]").replace('# team = "a-team workers"', 'team = "crew"'))
            self.assertEqual(ateam.Config(e.repo).tabs, {"lead": "a-team driver", "team": "crew"})
            e.write_brief(TEMPLATE)
            tab = e.calls("tab", "create")
            self.assertEqual(len(tab), 1)
            self.assertEqual((flag(tab[0], "--cwd"), flag(tab[0], "--workspace")), (str(e.repo), "w1"))
            self.assertTrue(all("--no-focus" in c and flag(c, "--cwd") == str(e.repo) for c in tab + e.calls("pane", "split")))
            labels = {p["label"]: (k, p["tab_id"]) for k, p in fs["panes"].items()}
            self.assertEqual(labels, {"cto": ("w1:p1", "w1:t1"), "lead-dev": ("w1:p2", "w1:t2"), "frontend-dev": ("w1:p3", "w1:t2"),
                                      "reviewer": ("w1:p4", "w1:t2"), "pm": ("w1:p5", "w1:t2")})
            sizes = {(p["rect"]["width"], p["rect"]["height"]) for p in fs["panes"].values() if p["tab_id"] == "w1:t2"}
            self.assertEqual(sizes, {(100, 25)})  # four members: a 2 x 2 grid

            starts = e.starts()
            self.assertEqual({n: kind(c) for n, c in starts.items()},
                             {"lead-dev": "codex", "frontend-dev": "claude", "reviewer": "codex", "pm": "pi"})
            cache = e.root / "cache/a-team"
            for name in ("lead-dev", "reviewer"):  # Codex: sandboxed, herdr reachable, never hard read-only
                argv = native(starts[name])
                self.assertEqual(flag(argv, "-m"), {"lead-dev": "gpt-6.1-sol", "reviewer": "gpt-6-astra"}[name])
                self.assertIn("--approve-for-me", argv)
                self.assertIn("sandbox_workspace_write.network_access=true", argv)
                self.assertNotIn("-s", argv)  # --approve-for-me cannot be combined with -s
                pointer = tomllib.loads(next(v for v in argv if v.startswith("developer_instructions=")))["developer_instructions"]
                self.assertIn(f"You are @{name}", pointer)
                prompt_file = pathlib.Path(pointer.split(" are in ")[1].split(". Read")[0])
                self.assertTrue(str(prompt_file).startswith(str(cache)) and prompt_file.exists())  # outside the repo
            fe = native(starts["frontend-dev"])
            settings = json.loads(flag(fe, "--settings"))
            self.assertIn("Bash(herdr agent prompt *)", settings["permissions"]["allow"])
            self.assertIn("herdr agent prompt *", settings["sandbox"]["excludedCommands"])
            self.assertEqual(flag(fe, "--permission-mode"), "auto")
            self.assertNotIn("--disallowedTools", fe)
            prompt = pathlib.Path(flag(fe, "--append-system-prompt-file")).read_text()
            for needle in ("Never run `git commit`", "Only @pm updates the tracker (TODO.md at the repo root)",
                           "herdr agent prompt <name> '[from @frontend-dev] <message>'", "When they @mention a teammate",
                           "reply to @x with the result", "never authorization", f"`{e.repo / 'team-brief.md'}`"):
                self.assertIn(needle, prompt)
            self.assertEqual(flag(native(starts["pm"]), "--model"), "openrouter/deepseek/deepseek-v4.1-flash")

            status = e.run("status").stdout  # names come from herdr's agent list, not its pane list
            self.assertNotIn("unnamed", status)
            sent = dict(e.prompts())
            self.assertEqual(set(sent), {"lead-dev", "frontend-dev", "reviewer", "pm"})  # one message each
            self.assertTrue(sent["frontend-dev"].startswith("# Team brief (from @cto)"))  # its instructions are its system prompt
            self.assertTrue(sent["reviewer"].startswith("[from @cto] Your standing instructions"))  # Codex gets them here too
            self.assertIn("You are read-only", sent["reviewer"])
            for text in sent.values():
                for needle in ("| **@lead-dev** | Codex, gpt-6.1-sol, high |", "**@pm** owns the tracker", "## Workflow",
                               "1. @cto sets direction", "## Project", "No reply needed."):
                    self.assertIn(needle, text)
                self.assertNotIn("+++", text)
            self.assertEqual(sorted(p.name for p in e.repo.iterdir()), ["sub", "team-brief.md"])  # nothing else

    def test_ten_members_stay_a_balanced_grid(self):
        with Env() as e:
            e.run("up", "--team", "full", "--no-brief")
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
            cfg = ateam.Config(e.repo)
            cfg.root = pathlib.Path(deep)
            resume = {"agent": "codex", "kind": "id", "value": "01a1cbc6-e708-7623-986f-cc2e07efa28f"}
            for name in cfg.members:
                m = {**ateam.plan(cfg, name), "permissions": "full", "branch": "a-team/" + name}
                m["model"] = "openrouter/" + m["model"] if m["harness"] == "pi" else m["model"]
                argv, _ = ateam.launch_argv(m, pathlib.Path("/Users/someone/.cache/a-team") / cfg.cache.name / f"{name}.md", resume)
                self.assertLess(len(shlex.join([m["harness"], *argv])), 900, name)

    def test_failed_check_launches_nothing_and_explains_the_fix(self):
        with Env(team_brief('''
            [members.backend-dev]
            model = "z-ai/glm-9"
            [members.lead-dev]
            model = "gpt-6-luna"
            [teams]
            default = ["backend-dev", "lead-dev"]
        ''')) as e:
            p = e.run("up", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("Cannot launch: 1 member(s)", p.stdout)
            self.assertIn("pi has no model z-ai/glm-9 configured", p.stdout)
            self.assertIn("OpenRouter key", p.stdout)
            self.assertIn("Similar models that are available: openrouter/z-ai/glm-5.3", p.stdout)
            self.assertEqual(e.fake_state()["calls"], [])

    def test_up_can_be_run_again_after_a_partial_launch(self):
        with Env() as e:
            p = e.run("up", check=False, FAKE_START_FAIL="dead:frontend-dev,blocked:pm,banner:reviewer")
            self.assertEqual(p.returncode, 3)
            self.assertIn("started @reviewer", p.stdout)  # a start-up notice that clears is not a failure
            self.assertIn("@frontend-dev did not come up: timeout: agent did not appear", p.stdout)  # herdr's reason is shown
            self.assertIn("ateam.py up frontend-dev", p.stdout)
            self.assertIn("@pm started but is waiting on a dialog: agent_not_ready", p.stdout)
            self.assertIn("ateam.py brief --send pm", p.stdout)
            self.assertEqual(sorted(e.starts()), ["frontend-dev", "lead-dev", "pm", "reviewer"])  # it went on past both
            self.assertEqual({n for n, _ in e.prompts()}, {"lead-dev", "reviewer"})  # only those that came up were briefed

            e.update("pm", agent_status="idle")  # the user answered pm's dialog
            before = len(e.prompts())
            out = e.run("up").stdout
            self.assertIn("already running: lead-dev, reviewer, pm", out)
            self.assertIn("started @frontend-dev", out)
            later = e.prompts()[before:]
            self.assertTrue(next(t for n, t in later if n == "frontend-dev").startswith("# Team brief"))
            for name in ("lead-dev", "reviewer", "pm"):  # the others hear who joined, not the whole brief again
                text = next(t for n, t in later if n == name)
                self.assertTrue(text.startswith("[from @cto] Joined the team: @frontend-dev (frontend-dev, Claude Code"), text)
            self.assertEqual(len(e.calls("tab", "create")), 1)  # the failed member's pane was reused
            self.assertEqual(len([p for p in e.fake_state()["panes"].values() if p["label"] == "frontend-dev"]), 1)
            e.run("brief", "--send", "pm")
            self.assertIn("# Team brief", e.prompts()[-1][1])
            self.assertEqual(e.prompts()[-1][0], "pm")

    def test_thinking_levels_are_guarded(self):
        brief = '''
            [members.frontend-dev]
            model = "claude-opus-5-5"
            thinking = "max"
            [members.reviewer]
            model = "gpt-6-astra"
            thinking = "ultra"
            [members.lead-dev]
            model = "gpt-6-luna"
            thinking = "xhigh"
            [members.pm]
            model = "z-ai/glm-5.3"
            thinking = "max"
            [members.backend-dev]
            model = "z-ai/glm-5.3"
            thinking = "turbo"
        '''
        with Env(team_brief(brief)) as e:  # the default limits come from harnesses.toml
            out = e.run("check", "frontend-dev", "reviewer", "lead-dev", "pm", "backend-dev", check=False).stdout
            self.assertIn("thinking 'max' is not allowed for claude", out)
            self.assertIn("thinking 'ultra' is not allowed for codex", out)
            self.assertIn("has no thinking level 'turbo'", out)
            rows = {l.split()[0]: l.split()[-1] for l in out.splitlines() if l.split() and l.split()[-1] in ("ok", "FAILED")}
            self.assertEqual(rows["@lead-dev"], "ok")
            self.assertEqual(rows["@pm"], "ok")  # max is only blocked for claude and codex
            e.write_brief(team_brief("[never_thinking]\nclaude = []\n" + textwrap.dedent(brief)))  # a team can change them
            self.assertIn("@frontend-dev", e.run("check", "frontend-dev").stdout)

    def test_model_and_harness_must_match(self):
        with Env(team_brief('''
            [members.lead-dev]
            model = "gpt-7-nova"
            [members.frontend-dev]
            harness = "claude"
            model = "z-ai/glm-5.3"
            [members.designer]
            model = "claude-opus-9"
            [members.pm]
        ''')) as e:
            out = e.run("check", "lead-dev", "frontend-dev", "designer", "pm", check=False).stdout
            self.assertIn("Codex does not offer gpt-7-nova", out)
            self.assertNotIn("gpt-reserve", out)  # hidden Codex models are not offered
            self.assertIn("z-ai/glm-5.3 is not a Claude model", out)
            self.assertIn("unknown Claude model claude-opus-9", out)
            self.assertIn("no model set", out)

    def test_harness_not_installed(self):
        with Env(harnesses=("claude", "pi")) as e:
            out = e.run("check", "lead-dev", check=False).stdout
            self.assertIn("codex is not installed", out)
            self.assertIn("npm i -g @openai/codex", out)

    def test_local_model_and_ambiguous_providers(self):
        with Env(team_brief('''
            [members.pm]
            harness = "omp"
            model = "nvidia/Qwen3.8-Flash-Next-NVFP4"
            [members.backend-dev]
            model = "z-ai/glm-5.3"
            [members.qa-backend]
            harness = "omp"
            model = "z-ai/glm-5.3"
        '''), models=MODELS + "ollama z-ai/glm-5.3\n") as e:
            out = e.run("check", "pm", "backend-dev", "qa-backend", check=False).stdout
            self.assertIn("pax-vllm/nvidia/Qwen3.8-Flash-Next-NVFP4", out)  # omp's box table keeps the provider
            self.assertIn("pi serves z-ai/glm-5.3 from several providers: ollama/z-ai/glm-5.3, openrouter/z-ai/glm-5.3", out)
            self.assertIn("omp serves z-ai/glm-5.3 from several providers", out)

    def test_opencode_member_has_its_own_server_and_agent(self):
        with Env(team_brief('''
            [members.reviewer]
            harness = "opencode"
            model = "z-ai/glm-5.3"
            thinking = "high"
        ''')) as e:
            e.run("up", "reviewer")
            self.assertEqual(native(e.starts()["reviewer"]), ["--standalone", "--auto"])
            content = json.loads(e.pane_of("reviewer")["env"][0].split("=", 1)[1])
            agent = content["agents"]["reviewer"]
            self.assertEqual(content["default_agent"], "reviewer")
            self.assertEqual(agent["model"], "openrouter/z-ai/glm-5.3#high")
            self.assertNotIn("permissions", agent)  # read-only is a prompt rule on every harness
            name, text = e.prompts()[0]
            self.assertIn("You are @reviewer", text)  # OpenCode gets its instructions as a message
            self.assertIn("# Team brief", text)
            p = e.run("check", "reviewer", check=False, FAKE_OPENCODE_VERSION="opencode 1.4.0")
            self.assertIn("a-team supports v2 or later", p.stdout)

    def test_permissions_and_extra_args_come_from_the_brief(self):
        with Env(team_brief('''
            permissions = "ask"
            [members.lead-dev]
            model = "gpt-6-luna"
            args = ["--search"]
            [members.frontend-dev]
            model = "claude-opus-5-5"
            [members.reviewer]
            model = "gpt-6-astra"
            permissions = "full"
            [members.pm]
            harness = "omp"
            model = "z-ai/glm-5.3"
        ''')) as e:
            e.run("up", "lead-dev", "frontend-dev", "reviewer", "pm", "--no-brief")
            starts = {n: native(c) for n, c in e.starts().items()}
            self.assertEqual(starts["lead-dev"][-5:], ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true", "--search"])
            self.assertNotIn("--permission-mode", starts["frontend-dev"])
            self.assertEqual(starts["reviewer"][-1], "--dangerously-bypass-approvals-and-sandbox")
            self.assertEqual(starts["pm"][-2:], ["--approval-mode", "always-ask"])
            self.assertIn("--permission-mode bypassPermissions", e.run("up", "frontend-dev", "--permissions", "full", "--dry-run").stdout)
            e.write_brief(team_brief('permissions = "yolo"\n[members.pm]\n'))
            self.assertIn("must be one of ask, auto, full", e.run("show", check=False).stderr)

    def test_one_of_a_role_has_the_role_name_and_several_are_numbered(self):
        with Env() as e:
            e.run("up", "backend-dev", "pm", "--no-brief")
            self.assertIn("Give no names", e.run("up", "backend-dev-2", "--role", "backend-dev", check=False).stderr)
            self.assertIn("would rename @backend-dev to @backend-dev-1",
                          e.run("up", "--role", "backend-dev", "--model", "gpt-6-luna", "--dry-run").stdout)
            out = e.run("up", "--role", "backend-dev", "--model", "gpt-6-luna", "--thinking", "high").stdout
            self.assertIn("renamed @backend-dev to @backend-dev-1", out)
            self.assertEqual(kind(e.starts()["backend-dev-2"]), "codex")  # the harness follows the model
            cfg = ateam.Config(e.repo)
            self.assertEqual(cfg.members["backend-dev-2"], {"role": "backend-dev", "harness": "codex", "model": "gpt-6-luna", "thinking": "high"})
            self.assertEqual(cfg.members["backend-dev-1"], {"role": "backend-dev", "harness": "pi", "model": "z-ai/glm-5.3", "thinking": "medium"})
            self.assertNotIn("backend-dev", cfg.members)
            self.assertEqual(cfg.team["teams"]["backend"][:2], ["lead-dev", "backend-dev-1"])  # rosters follow the rename
            self.assertIn("## Workflow", cfg.body)  # the prose survived the edit
            first = e.pane_of("backend-dev-1")  # the running agent and its pane were renamed too
            self.assertEqual((first["name"], first["pane_id"]), ("backend-dev-1", "w1:p2"))
            news = next(t for n, t in e.prompts() if n == "backend-dev-1")
            self.assertIn("@backend-dev is now @backend-dev-1", news)
            self.assertIn("Joined the team: @backend-dev-2", next(t for n, t in e.prompts() if n == "pm"))
            self.assertIn("| **@backend-dev-1** |", next(t for n, t in e.prompts() if n == "backend-dev-2"))

            e.run("up", "--role", "backend-dev", "--no-brief")  # a third starts from the first one's settings
            third = e.starts()["backend-dev-3"]
            self.assertEqual((kind(third), flag(native(third), "--model")), ("pi", "openrouter/z-ai/glm-5.3"))
            self.assertEqual(sorted(n for n in ateam.Config(e.repo).members if n.startswith("backend")),
                             ["backend-dev-1", "backend-dev-2", "backend-dev-3"])
            for stale in ("backend-dev", "wizard"):  # a name must be a member; --role is how one is added
                self.assertIn("is not a member", e.run("check", stale, check=False).stderr)
            self.assertIn("no role 'wizard'", e.run("up", "--role", "wizard", check=False).stderr)

    def test_say_reports_who_did_not_get_the_message(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            note = e.root / "note.md"
            note.write_text("Priorities changed: ship the list view first.\n")
            e.run("say", "all", "--file", str(note))
            e.run("say", "@pm", "add a todo: ship it")
            sent = e.prompts()
            self.assertIn(("lead-dev", "[from @cto] Priorities changed: ship the list view first.\n"), sent)
            self.assertIn(("pm", "[from @cto] add a todo: ship it"), sent)
            self.assertIn("not running: reviewer", e.run("say", "reviewer", "hi", check=False).stderr)
            e.update("pm", agent_status="blocked")  # herdr refuses a prompt to an agent waiting on an approval
            p = e.run("say", "all", "status?", check=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("sent your message to @lead-dev", p.stdout)
            self.assertIn("NOT delivered to @pm: agent_blocked", p.stdout)

    def test_dry_run_outside_herdr_and_no_brief(self):
        with Env() as e:
            out = e.run("up", "--team", "planning", "--dry-run").stdout
            self.assertIn("would start @product", out)
            self.assertEqual(e.fake_state()["calls"], [])
            for cmd in (["up", "pm"], ["status"], ["cost"], ["wait"]):
                self.assertIn("not inside herdr", e.run(*cmd, check=False, HERDR_ENV="").stderr)
        with Env(brief=None) as e:
            self.assertIn("Run `/a-team setup`", e.run("show", check=False).stderr)


@unittest.skipUnless(GIT, "git is not installed")
class WorktreeTests(unittest.TestCase):
    BRIEF = '''
        [members.backend-dev-1]
        role = "backend-dev"
        model = "gpt-6-luna"
        worktree = true
        [members.backend-dev-2]
        role = "backend-dev"
        model = "z-ai/glm-5.3"
        worktree = "feature/login"
        [members.frontend-dev]
        model = "claude-opus-5-5"
        worktree = "feature/login"
        [members.reviewer]
        model = "gpt-6-astra"
    '''

    def test_worktree_members_get_their_own_checkout_and_commit_there(self):
        with Env(team_brief(self.BRIEF), git=True) as e:
            out = e.run("up", "backend-dev-1", "backend-dev-2", "frontend-dev", "reviewer").stdout
            trees = e.root / "repo.worktrees"
            self.assertEqual(sorted(p.name for p in trees.iterdir()), ["a-team-backend-dev-1", "feature-login"])  # one is shared
            self.assertEqual(out.count("created worktree"), 2)
            branch = lambda d: subprocess.run([GIT, "-C", str(d), "rev-parse", "--abbrev-ref", "HEAD"],
                                              capture_output=True, text=True).stdout.strip()
            self.assertEqual((branch(trees / "a-team-backend-dev-1"), branch(trees / "feature-login")),
                             ("a-team/backend-dev-1", "feature/login"))
            where = {n: e.pane_of(n)["cwd"] for n in ("backend-dev-1", "backend-dev-2", "frontend-dev", "reviewer")}
            self.assertEqual(where, {"backend-dev-1": str(trees / "a-team-backend-dev-1"), "backend-dev-2": str(trees / "feature-login"),
                                     "frontend-dev": str(trees / "feature-login"), "reviewer": str(e.repo)})
            prompt = e.run("prompt", "backend-dev-1").stdout
            self.assertIn("Commit your work on `a-team/backend-dev-1`", prompt)
            self.assertIn("`git merge main`", prompt)
            self.assertIn("agree who commits it for you", prompt)
            self.assertNotIn("Never run `git commit`", prompt)
            self.assertIn("Never run `git commit`", e.run("prompt", "reviewer").stdout)  # the main checkout keeps lead_commits
            brief = next(t for n, t in e.prompts() if n == "reviewer")
            self.assertIn("| branch `feature/login` in `" + str(trees / "feature-login") + "` |", brief)
            self.assertIn("`git diff main...<branch>`", brief)
            self.assertIn("| the main checkout |", brief)
            e.kill("backend-dev-1")
            self.assertNotIn("created worktree", e.run("continue", "backend-dev-1").stdout)  # the checkout is kept

    def test_adhoc_worktree_and_no_repository(self):
        with Env(git=True) as e:
            e.run("up", "--role", "reviewer", "--worktree", "--no-brief")  # the template's reviewer becomes reviewer-1
            self.assertTrue((e.root / "repo.worktrees/a-team-reviewer-2").is_dir())
            self.assertIs(ateam.Config(e.repo).members["reviewer-2"]["worktree"], True)
        with Env(team_brief(self.BRIEF)) as e:  # not a git repository
            p = e.run("up", "backend-dev-1", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("a worktree needs a git repository with a commit", p.stdout)
            self.assertEqual(e.fake_state()["calls"], [])


class WaitTests(unittest.TestCase):
    def test_wait_returns_when_the_lead_is_needed(self):
        with Env() as e:
            self.assertIn("not started: any member", e.run("wait", check=False).stderr)
            e.run("up", "lead-dev", "reviewer", "pm", "--no-brief")
            for name in ("lead-dev", "reviewer", "pm"):
                e.update(name, agent_status="working")
            p = e.run("wait", "--interval", "0.05", "--timeout", "0.2", check=False)
            self.assertEqual(p.returncode, 4)
            self.assertIn("still working: @lead-dev, @reviewer, @pm", p.stdout)

            e.update("reviewer", agent_status="blocked")  # an approval dialog: back at once, with what it shows
            out = e.run("wait").stdout
            self.assertIn("@reviewer is blocked: it needs an answer", out)
            self.assertIn("Allow this command? (y/n)  [w1:p3]", out)
            e.update("reviewer", agent_status="working")
            e.kill("pm")
            self.assertIn("@pm is gone: its agent exited. `ateam.py continue pm`", e.run("wait").stdout)

            e.run("continue", "pm")
            e.update("pm", agent_status="working")
            e.update("lead-dev", agent_status="idle")  # a watched member stopped while others still work
            self.assertIn("@lead-dev stopped (idle)", e.run("wait", "lead-dev", "--interval", "0.05").stdout)
            p = e.run("wait", "--interval", "0.05", "--timeout", "0.2", check=False)
            self.assertEqual(p.returncode, 4)  # without names, others working means: keep waiting
            for name in ("reviewer", "pm"):
                e.update(name, agent_status="idle")
            self.assertIn("nobody is working: @lead-dev idle, @reviewer idle, @pm idle", e.run("wait", "--interval", "0.05").stdout)


class ContinueTests(unittest.TestCase):
    def test_brings_members_back_in_their_panes_and_sessions(self):
        with Env() as e:
            e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
            e.set_session("lead-dev", {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "cx-1"})
            e.set_session("frontend-dev", {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "cl-1"})
            e.set_session("pm", {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/s/2026_pi-1.jsonl"})
            self.assertIn("every member is running", e.run("continue").stdout)  # named, running members are left alone
            self.assertNotIn("adopted", e.run("continue").stdout)
            self.assertEqual(e.calls("agent", "rename"), [["agent", "rename", "w1:p1", "cto"]])
            e.kill("lead-dev", "frontend-dev", "pm")
            panes_before = {n: e.pane_of(n)["pane_id"] for n in ("lead-dev", "frontend-dev", "pm")}
            # The brief changed while they were gone: lead-dev moves to Luna.
            e.write_brief(TEMPLATE.replace('model = "gpt-6.1-sol"\nthinking = "high"',
                                           'model = "gpt-6-luna"\nthinking = "medium"'))
            self.assertIn("gone", e.run("status").stdout)

            before = len(e.prompts())
            e.run("continue")
            by = {c[2]: c for c in e.calls("agent", "start")[3:]}
            self.assertEqual({n: flag(c, "--pane") for n, c in by.items()}, panes_before)  # same panes, no new ones
            self.assertEqual(len(e.calls("tab", "create")) + len(e.calls("pane", "split")), 3)
            self.assertEqual(native(by["lead-dev"])[:2], ["resume", "cx-1"])
            self.assertEqual(flag(native(by["lead-dev"]), "-m"), "gpt-6-luna")  # current settings apply
            self.assertEqual(flag(native(by["frontend-dev"]), "--resume"), "cl-1")
            self.assertEqual(flag(native(by["pm"]), "--session"), "/s/2026_pi-1.jsonl")
            sent = e.prompts()[before:]
            self.assertEqual(sorted(n for n, _ in sent), ["frontend-dev", "lead-dev", "pm"])  # one message each
            for _, text in sent:
                self.assertIn("picking up where we left off", text)
                self.assertIn("# Team brief", text)
                self.assertNotIn("No reply needed", text)

            e.kill("pm")
            before = len(e.prompts())
            e.run("continue", "pm")  # one member comes back: only it is re-briefed
            self.assertEqual([n for n, _ in e.prompts()[before:]], ["pm"])

    def test_adopts_members_herdr_resumed_after_a_restart(self):
        with Env() as e:
            e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
            # herdr restarted and resumed two agents in their labeled panes, without their names.
            fs = e.fake_state()
            for p in fs["panes"].values():
                if p["label"] in ("lead-dev", "frontend-dev"):
                    p["name"] = None
            e.write_fake(fs)
            self.assertIn("idle (unnamed)", e.run("status").stdout)
            self.assertIn("already running: lead-dev", e.run("up", "lead-dev").stdout)
            out = e.run("continue").stdout
            self.assertIn("adopted @lead-dev", out)
            self.assertNotIn("adopted @pm", out)
            fs = e.fake_state()
            self.assertEqual({p["label"]: p["name"] for p in fs["panes"].values()},
                             {"cto": "cto", "lead-dev": "lead-dev", "frontend-dev": "frontend-dev", "pm": "pm"})
            self.assertEqual(len(e.calls("agent", "start")), 3)  # nothing relaunched
            sent = dict(e.prompts())
            self.assertIn("You are @frontend-dev", sent["frontend-dev"])  # adopted: standing instructions again
            self.assertTrue(sent["pm"].startswith("# Team brief"))  # not adopted: the brief only

    def test_closed_pane_or_changed_harness_starts_fresh(self):
        with Env() as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("pm", {"agent": "pi", "kind": "path", "value": "/s/p.jsonl"})
            e.kill("pm")
            e.close("reviewer")
            e.write_brief(TEMPLATE.replace('model = "deepseek/deepseek-v4.1-flash"', 'model = "gpt-6-sol"')
                          .replace('[members.pm]\nharness = "pi"', '[members.pm]\nharness = "codex"'))
            out = e.run("continue", "pm", "reviewer").stdout
            self.assertIn("@reviewer has no pane any more; it gets a new one", out)
            by = {c[2]: native(c) for c in e.calls("agent", "start")[2:]}
            self.assertNotIn("resume", by["pm"])  # a pi session can't open in codex
            self.assertNotIn("resume", by["reviewer"])
            self.assertEqual(e.pane_of("reviewer")["pane_id"], "w1:p4")  # a new, labeled pane
            self.assertEqual(e.pane_of("reviewer")["tab_id"], e.pane_of("pm")["tab_id"])  # in the team tab

    def test_opencode_member_gets_a_new_pane_and_the_old_label_is_cleared(self):
        with Env(team_brief('[members.reviewer]\nharness = "opencode"\nmodel = "z-ai/glm-5.3"\n[members.pm]\nmodel = "z-ai/glm-5.3"\n')) as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("reviewer", {"agent": "opencode", "kind": "id", "value": "ses_1"})
            e.kill("reviewer")
            e.run("continue", "reviewer")
            labeled = [p for p in e.fake_state()["panes"].values() if p["label"] == "reviewer"]
            self.assertEqual([p["pane_id"] for p in labeled], ["w1:p4"])  # its environment is set at creation
            self.assertEqual(native(e.calls("agent", "start")[-1]), ["--standalone", "--session", "ses_1", "--auto"])

    def test_failed_resume_says_how_to_start_fresh(self):
        with Env() as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("pm", {"agent": "pi", "kind": "id", "value": "old"})
            e.kill("pm", "reviewer")
            p = e.run("continue", check=False, FAKE_START_FAIL="dead:pm")
            self.assertEqual(p.returncode, 3)
            self.assertIn("ateam.py continue pm --fresh", p.stdout)
            self.assertIn("started @reviewer", p.stdout)  # the others still came back


class ConfigTests(unittest.TestCase):
    def test_template_is_consistent(self):
        with Env() as e:
            cfg = ateam.Config(e.repo)
            for name in cfg.members:
                p = ateam.plan(cfg, name)
                self.assertIn(p["harness"], ateam.HARNESSES, name)
                self.assertIn(p["model"], ateam.TABLE[p["harness"]]["models"], name)  # the template uses recommended models
                self.assertEqual(p["permissions"], "auto")
            self.assertEqual(cfg.tracker, "TODO.md at the repo root")
            self.assertIn("## Project", cfg.body)
            out = e.run("show").stdout
            self.assertIn("@lead-dev", out)
            self.assertIn("tracker: TODO.md at the repo root", out)
        self.assertFalse(any("/" in m for m in ateam.TABLE["codex"]["models"]), "Codex takes bare model names")
        for h, t in ateam.TABLE.items():  # every harness has the same shape, and code to go with it
            self.assertEqual(set(t["permissions"]), set(ateam.LEVELS), h)
            self.assertIn(t["prompt"], ("file", "pointer", "message"), h)
            self.assertIn(h, ateam.CODE)

    def test_no_tracker(self):
        with Env(team_brief('[members.pm]\n')) as e:
            self.assertIsNone(ateam.Config(e.repo).tracker)
            self.assertNotIn("tracker", e.run("prompt", "pm").stdout.split("## Talking to the team")[1])

    def test_infer_harness(self):
        for model, want in [("claude-fable-5-2", "claude"), ("opus", "claude"), ("anthropic/claude-sonnet-5-5", "claude"),
                            ("gpt-6.1-sol", "codex"), ("openai/gpt-6-luna", "codex"), ("z-ai/glm-5.3", "pi"),
                            ("deepseek/deepseek-v4.1-flash", "pi"), ("openrouter/openai/gpt-6-luna", "pi"),
                            ("nvidia/Qwen3.8-Flash-Next-NVFP4", "pi")]:
            self.assertEqual(ateam.infer_harness(model), want, model)

    def test_brief_validation(self):
        with Env(team_brief('[members.wizard]\nmodel = "x"\n')) as e:
            self.assertIn("no role 'wizard'", e.run("show", check=False).stderr)
            e.write_brief(team_brief('[members.Bad]\nrole = "pm"\n'))
            self.assertIn("must match", e.run("show", check=False).stderr)
            e.write_brief(team_brief('[members.pm]\n[teams]\ndefault = ["pm", "ghost"]\n'))
            self.assertIn("ghost", e.run("show", check=False).stderr)
            e.write_brief("no front matter\n")
            self.assertIn("must start with a +++ line", e.run("show", check=False).stderr)
            e.write_brief("+++\ntracker = 'x'\n")
            self.assertIn("no closing +++", e.run("show", check=False).stderr)
            e.write_brief(team_brief('[lead]\nname = "pm"\n[members.pm]\n'))
            self.assertIn("not be a member's name", e.run("show", check=False).stderr)
            # one of a role carries the role's name; several are all numbered
            e.write_brief(team_brief('[members.backend-dev]\n[members.backend-dev-2]\nrole = "backend-dev"\n'))
            self.assertIn("name them @backend-dev-1, @backend-dev-2", e.run("show", check=False).stderr)
            e.write_brief(team_brief('[members.dev]\nrole = "backend-dev"\n'))
            self.assertIn("name them @backend-dev.", e.run("show", check=False).stderr)
            e.write_brief(team_brief('[members.backend-dev-1]\nrole = "backend-dev"\n[members.backend-dev-2]\nrole = "backend-dev"\n'))
            self.assertIn("@backend-dev-2", e.run("show").stdout)

    def test_init(self):
        with Env(brief=None) as e:
            e.run("init", cwd=e.repo / "sub")
            self.assertEqual((e.repo / "sub/team-brief.md").read_text(), TEMPLATE)  # no git: where you are
            self.assertIn("exists", e.run("init", cwd=e.repo / "sub", check=False).stderr)
            e.run("init", "--force", cwd=e.repo / "sub")

    def test_brief_preview_and_member_prompt(self):
        with Env() as e:
            text = e.run("brief", "--team", "planning").stdout
            self.assertIn("| **@product** | Claude Code, claude-opus-5-5, medium |", text)
            self.assertIn("No reply needed.", text)
            self.assertNotIn("Works in", text)  # no worktrees on this team
            prompt = e.run("prompt", "reviewer").stdout
            self.assertIn("You are read-only", prompt)
            self.assertIn("build and run", prompt)  # by prompt only: a reviewer can still build and test

    def test_session_id_from_ref(self):
        self.assertEqual(ateam.session_id({"kind": "id", "value": "abc"}), "abc")
        path = "/x/rollout-2026-11-11T13-02-52-01a1cbc6-e708-7623-986f-cc2e07efa28f.jsonl"
        self.assertEqual(ateam.session_id({"kind": "path", "value": path}), "01a1cbc6-e708-7623-986f-cc2e07efa28f")

    def test_bad_role_frontmatter_names_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.md"
            p.write_text("---\nthinking: low\noops\n---\nbody\n")
            with self.assertRaisesRegex(ValueError, "x.md:3"):
                ateam.parse_role(p)


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def pi_session(path, *costs):
    jsonl(path, [{"type": "session"}] + [{"type": "message", "message": {
        "role": "assistant", "model": "deepseek/deepseek-v4.1-flash",
        "usage": {"input": 1000, "output": 100, "cacheRead": 0, "cost": {"total": c}}}} for c in costs])


class CostTests(unittest.TestCase):
    def test_cost_per_agent_across_harnesses(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            h = e.home
            claude = h / ".claude/projects/-work/c-1.jsonl"
            msg = lambda mid, model, i, o: {"type": "assistant", "message": {
                "id": mid, "model": model, "usage": {"input_tokens": i, "output_tokens": o}}}
            jsonl(claude, [msg("a", "claude-fable-5-2", 1000, 10), msg("a", "claude-fable-5-2", 1000, 10),
                           {"type": "cost-state", "totalCostUSD": 1.25}])
            jsonl(claude.with_suffix("") / "subagents/agent-x.jsonl", [msg("b", "claude-haiku-5-5", 500, 5)])
            pi = h / ".pi/agent/sessions/--work--/2026_p-1.jsonl"
            pi_session(pi, 0.01, 0.02)
            cx = h / ".codex/sessions/2026/11/14/rollout-2026-11-14T00-00-00-x-1.jsonl"
            jsonl(cx, [{"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
                       {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                           "input_tokens": 1_000_000, "cached_input_tokens": 500_000, "output_tokens": 10_000}}}}])
            e.set_session("cto", {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "c-1"})
            e.set_session("lead-dev", {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "x-1"})
            e.set_session("pm", {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": str(pi)})

            by = {a["agent"]: a for a in json.loads(e.run("cost", "--json", "--home", str(h)).stdout)["agents"]}
            self.assertEqual(by["cto"]["models"]["claude-fable-5-2"]["input"], 1000)
            self.assertEqual(by["cto"]["models"]["claude-haiku-5-5"]["input"], 500)
            self.assertAlmostEqual(by["cto"]["cost_usd"], 1.25)
            self.assertAlmostEqual(by["pm"]["cost_usd"], 0.03)
            self.assertEqual(by["lead-dev"]["models"]["gpt-6.1-sol"]["cache_read"], 500_000)
            self.assertIsNone(by["lead-dev"]["cost_usd"])

            table = e.run("cost", "--home", str(h)).stdout
            self.assertIn("$1.28+?", table)
            self.assertIn("Codex records tokens only", table)

            e.kill("pm")  # exited, and herdr forgot its session; the note of it is what still counts
            gone = {a["agent"]: a for a in json.loads(e.run("cost", "--json", "--home", str(h)).stdout)["agents"]}
            self.assertEqual(gone["pm"]["status"], "gone")
            self.assertAlmostEqual(gone["pm"]["cost_usd"], 0.03)

    def test_a_fresh_restart_keeps_the_earlier_spend(self):
        with Env() as e:
            e.run("up", "pm", "--no-brief")
            first, second = (e.home / f".pi/agent/sessions/--work--/2026_p-{i}.jsonl" for i in (1, 2))
            pi_session(first, 0.10, 0.20)
            pi_session(second, 0.05)
            e.set_session("pm", {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": str(first)})
            e.kill("pm")
            e.run("continue", "pm", "--fresh")  # `continue` notes the session before the member starts a new one
            e.set_session("pm", {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": str(second)})
            pm = next(a for a in json.loads(e.run("cost", "--json", "--home", str(e.home)).stdout)["agents"] if a["agent"] == "pm")
            self.assertEqual(pm["sessions"], 2)
            self.assertAlmostEqual(pm["cost_usd"], 0.35)
            self.assertEqual(pm["models"]["deepseek/deepseek-v4.1-flash"]["input"], 3000)
            seen = list((e.root / "cache/a-team").glob("*/sessions.jsonl"))
            self.assertEqual(len(seen), 1)  # the one thing kept outside herdr and the brief
            seen[0].unlink()  # without it only the current session counts, and an exited member starts fresh
            pm = next(a for a in json.loads(e.run("cost", "--json", "--home", str(e.home)).stdout)["agents"] if a["agent"] == "pm")
            self.assertAlmostEqual(pm["cost_usd"], 0.05)

    def test_estimate_from_prices(self):
        models = {"gpt-6-luna": {"input": 1_000_000, "cache_read": 1_000_000, "cache_write": 0, "output": 100_000}}
        self.assertAlmostEqual(ateam.estimate(models, {"gpt-6-luna": {"input": 2.0, "output": 10.0}}), 2.0 + 0.2 + 1.0)
        self.assertIsNone(ateam.estimate(models, {}))


class ListingTests(unittest.TestCase):
    def test_listed_models_formats(self):
        pi = "provider  model  context\nopenrouter  z-ai/glm-5.3  1M\npax-vllm  nvidia/Q  262K\n"
        self.assertEqual(ateam.listed_models(pi), ["openrouter/z-ai/glm-5.3", "pax-vllm/nvidia/Q"])
        self.assertEqual(ateam.listed_models("openrouter/z-ai/glm-5.3\nollama/qwen3:8b\n"),
                         ["openrouter/z-ai/glm-5.3", "ollama/qwen3:8b"])
        omp = ("openrouter (2)\n┌──────┬─────┐\n│ model              │ context │\n├─────┼─────┤\n"
               "│ z-ai/glm-5.3       │      1M │\n│ z-ai/glm-5.3-flash │      1M │\n└─────┴─────┘\n"
               "ollama (1)\n┌─────┬─────┐\n│ model    │ context │\n├─────┼─────┤\n│ qwen3:8b │    128K │\n└─────┴─────┘\n")
        self.assertEqual(ateam.listed_models(omp), ["openrouter/z-ai/glm-5.3", "openrouter/z-ai/glm-5.3-flash", "ollama/qwen3:8b"])


class HerdrContract(unittest.TestCase):
    """The fields ateam.py reads must exist where it reads them. The fake above follows the same split."""

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
        self.assertEqual(set(defs["ErrorBody"]["properties"]), {"code", "message"})
        for params, field in (("TabCreateParams", "env"), ("PaneSplitParams", "env"), ("AgentStartParams", "args")):
            self.assertIn(field, defs[params]["properties"])


@unittest.skipUnless(os.environ.get("ATEAM_LIVE"), "set ATEAM_LIVE=1 (outside any sandbox) to check real harness flags")
class LiveFlags(unittest.TestCase):
    """Harness flags drift between releases. Each installed harness's --help must still name what we pass."""
    FLAGS = {
        "claude": (["--help"], ["--model", "--effort", "--append-system-prompt", "--name", "--settings", "--permission-mode", "--resume"]),
        "codex": (["--help"], ["--model", "--config", "--sandbox", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox", "resume"]),
        "pi": (["--help"], ["--model", "--thinking", "--append-system-prompt", "--name", "--session", "--list-models"]),
        "omp": (["--help"], ["--model", "--thinking", "--append-system-prompt", "--resume", "--approval-mode"]),
        "opencode": (["--help"], ["--standalone", "--session", "--auto", "models"]),
    }

    def test_flags_exist(self):
        for harness, (argv, flags) in self.FLAGS.items():
            if not shutil.which(harness):
                continue
            p = subprocess.run([harness, *argv], capture_output=True, text=True, timeout=60)
            for f in flags:
                self.assertIn(f, p.stdout + p.stderr, f"{harness} --help no longer mentions {f}")


if __name__ == "__main__":
    unittest.main()
