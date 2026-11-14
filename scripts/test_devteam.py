"""Tests for devteam.py with a fake `herdr` and fake harness binaries; nothing real is launched."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest

SCRIPT = pathlib.Path(__file__).with_name("devteam.py")
spec = importlib.util.spec_from_file_location("devteam", SCRIPT)
devteam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devteam)
TEMPLATE = (devteam.SKILL / "templates/team-brief.md").read_text()

FAKE_HERDR = textwrap.dedent(r'''
    #!/usr/bin/env python3
    # A herdr stand-in. Panes keep their label and agent_session after their agent exits, like herdr does.
    import json, os, sys
    path = os.environ["FAKE_HERDR"]
    s = json.load(open(path))
    s["calls"].append(sys.argv[1:])
    a = sys.argv[1:]
    panes = s["panes"]  # pane_id -> {pane_id, tab_id, label, agent, name, agent_status, agent_session}
    def out(result):
        json.dump(s, open(path, "w")); print(json.dumps({"id": "1", "result": result})); sys.exit(0)
    def fail(msg):
        json.dump(s, open(path, "w")); print(json.dumps({"error": msg}), file=sys.stderr); sys.exit(1)
    def find(target):
        return panes.get(target) or next((p for p in panes.values() if p.get("agent") and p.get("name") == target), None)
    if a[:2] == ["agent", "list"]:
        out({"type": "agent_list", "agents": [p for p in panes.values() if p.get("agent")]})
    if a[:2] == ["agent", "get"]:
        p = find(a[2])
        out({"type": "agent_info", "agent": p}) if p and p.get("agent") else fail("agent_not_found")
    if a[:2] == ["agent", "rename"]:
        find(a[2])["name"] = a[3]; out({"type": "agent_info", "agent": find(a[3])})
    if a[:2] == ["pane", "list"]:
        out({"type": "pane_list", "panes": list(panes.values())})
    if a[:2] == ["pane", "rename"]:
        panes[a[2]]["label"] = a[3]; out({"type": "pane_info", "pane": panes[a[2]]})
    if a[:2] == ["pane", "split"]:
        s["next"] += 1
        pid = f"w1:p{s['next']}"
        panes[pid] = {"pane_id": pid, "tab_id": panes[a[2]]["tab_id"], "label": None}
        out({"type": "pane_info", "pane": panes[pid]})
    if a[:2] == ["agent", "start"]:
        name, kind, pane = a[2], a[a.index("--kind") + 1], a[a.index("--pane") + 1]
        if panes[pane].get("agent"):
            fail("pane_busy")
        panes[pane].update(agent=kind, name=name, agent_status="idle")
        out({"type": "agent_started", "agent": panes[pane], "argv": a[a.index("--") + 1:]})
    if a[:2] == ["agent", "prompt"]:
        out({"type": "agent_prompted", "agent": {}})
    fail("unexpected " + " ".join(a))
''').lstrip()

# pi/omp print a table of configured models; this filters FAKE_MODELS by the query like pi does.
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

FAKE_OPENCODE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import os, sys
    if sys.argv[1:] == ["--version"]:
        print(os.environ.get("FAKE_OPENCODE_VERSION", "opencode v2.2.4+abc")); sys.exit(0)
    if sys.argv[1:] == ["models"]:
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
    {"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
    {"slug": "gpt-6-astra", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6.1-sol", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6-sol", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
]}


def team_brief(settings, body="## Workflow\n\n1. {lead} hands out work.\n"):
    return "+++\n" + textwrap.dedent(settings).strip() + "\n+++\n" + body


class Env:
    """A temp repo with team-brief.md at its root (the template unless given), a fake herdr, fake harnesses,
    a Codex model cache, and the lead (a claude agent) in pane w1:p1."""

    def __init__(self, brief=TEMPLATE, panes=None, harnesses=("claude", "codex", "pi", "omp", "opencode"), models=MODELS):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
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
        scripts = {"herdr": FAKE_HERDR, "pi": FAKE_PI, "omp": FAKE_PI, "opencode": FAKE_OPENCODE,
                   "claude": "#!/bin/sh\n", "codex": "#!/bin/sh\n"}
        for name, body in scripts.items():
            if name == "herdr" or name in harnesses:
                (bin_ / name).write_text(body)
                (bin_ / name).chmod(0o755)
        self.fake = self.root / "herdr.json"
        lead = {"pane_id": "w1:p1", "tab_id": "w1:t1", "label": None, "agent": "claude", "name": None,
                "agent_status": "working"}
        self.write_fake({"calls": [], "next": 1, "panes": {"w1:p1": lead, **(panes or {})}})
        self.env = {**os.environ, "HERDR_BIN": str(bin_ / "herdr"), "FAKE_HERDR": str(self.fake),
                    "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_WORKSPACE_ID": "w1", "PATH": str(bin_),
                    "HOME": str(self.home), "XDG_CACHE_HOME": str(self.root / "cache"), "FAKE_MODELS": models}
        self.env.pop("CODEX_HOME", None)

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

    def set_session(self, name, ref):
        fs = self.fake_state()
        next(p for p in fs["panes"].values() if p.get("label") == name)["agent_session"] = ref
        self.write_fake(fs)

    def kill(self, *names):
        """Members' agents exit; their panes stay, with label and session reference."""
        fs = self.fake_state()
        for p in fs["panes"].values():
            if p.get("label") in names:
                for k in ("agent", "name", "agent_status"):
                    p.pop(k, None)
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
    def test_default_team_launches_labeled_and_briefed(self):
        with Env() as e:
            e.run("up", cwd=e.repo / "sub")  # team-brief.md is found from a subdirectory too
            fs = e.fake_state()
            self.assertEqual(fs["panes"]["w1:p1"]["name"], "cto")
            self.assertEqual(fs["panes"]["w1:p1"]["label"], "cto")
            splits = e.calls("pane", "split")
            self.assertEqual((splits[0][2], flag(splits[0], "--direction")), ("w1:p1", "right"))
            self.assertEqual((splits[1][2], flag(splits[1], "--direction")), ("w1:p2", "down"))
            self.assertTrue(all("--no-focus" in c and flag(c, "--cwd") == str(e.repo) for c in splits))
            labels = {p["label"]: k for k, p in fs["panes"].items()}
            self.assertEqual(labels, {"cto": "w1:p1", "lead-dev": "w1:p2", "frontend-dev": "w1:p3",
                                      "reviewer": "w1:p4", "pm": "w1:p5"})

            starts = e.starts()
            self.assertEqual({n: kind(c) for n, c in starts.items()},
                             {"lead-dev": "codex", "frontend-dev": "claude", "reviewer": "codex", "pm": "pi"})
            dev = native(starts["lead-dev"])
            self.assertEqual((flag(dev, "-m"), flag(dev, "-s")), ("gpt-6.1-sol", "workspace-write"))
            rev = native(starts["reviewer"])
            self.assertEqual(flag(rev, "-s"), "read-only")
            instructions = tomllib.loads(next(v for v in rev if v.startswith("developer_instructions=")))
            self.assertIn("You are @reviewer", instructions["developer_instructions"])
            fe = native(starts["frontend-dev"])
            prompt_file = pathlib.Path(flag(fe, "--append-system-prompt-file"))
            self.assertTrue(str(prompt_file).startswith(str(e.root / "cache/devteam")))  # outside the repo
            self.assertIn("Never run `git commit`", prompt_file.read_text())
            self.assertIn("Only @pm updates the tracker (TODO.md at the repo root)", prompt_file.read_text())
            self.assertEqual(flag(native(starts["pm"]), "--model"), "openrouter/deepseek/deepseek-v4.1-flash")

            briefs = [(n, t) for n, t in e.prompts() if t.startswith("# Team brief (from @cto)")]
            self.assertEqual({n for n, _ in briefs}, {"lead-dev", "frontend-dev", "reviewer", "pm"})
            text = briefs[0][1]
            for needle in ("| **@lead-dev** | Codex, gpt-6.1-sol, high |", "**@pm** owns the tracker",
                           "## Workflow", "## Project", "Reply only: ready."):
                self.assertIn(needle, text)
            self.assertNotIn("+++", text)
            self.assertEqual(sorted(p.name for p in e.repo.iterdir()), ["sub", "team-brief.md"])  # nothing else

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

    def test_thinking_levels_are_guarded(self):
        with Env(team_brief('''
            [never_thinking]
            claude = ["max", "ultra"]
            codex = ["max", "ultra"]
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
        ''')) as e:
            out = e.run("check", "frontend-dev", "reviewer", "lead-dev", "pm", "backend-dev", check=False).stdout
            self.assertIn("thinking 'max' is not allowed for claude", out)
            self.assertIn("thinking 'ultra' is not allowed for codex", out)
            self.assertIn("has no thinking level 'turbo'", out)
            rows = {l.split()[0]: l.split()[-1] for l in out.splitlines() if l.split() and l.split()[-1] in ("ok", "FAILED")}
            self.assertEqual(rows["@lead-dev"], "ok")
            self.assertEqual(rows["@pm"], "ok")  # max is only blocked for claude and codex

    def test_model_and_harness_must_match(self):
        with Env(team_brief('''
            [members.lead-dev]
            model = "gpt-7-nova"
            [members.frontend-dev]
            harness = "claude"
            model = "z-ai/glm-5.3"
            [members.designer]
            model = "claude-opus-9"
        ''')) as e:
            out = e.run("check", "lead-dev", "frontend-dev", "designer", check=False).stdout
            self.assertIn("Codex does not offer gpt-7-nova", out)
            self.assertIn("z-ai/glm-5.3 is not a Claude model", out)
            self.assertIn("unknown Claude model claude-opus-9", out)

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
        '''), models=MODELS + "ollama z-ai/glm-5.3\n") as e:
            out = e.run("check", "pm", "backend-dev", check=False).stdout
            self.assertIn("pax-vllm/nvidia/Qwen3.8-Flash-Next-NVFP4", out)
            self.assertIn("from several providers: ollama/z-ai/glm-5.3, openrouter/z-ai/glm-5.3", out)

    def test_opencode_v2_member(self):
        with Env(team_brief('''
            [members.reviewer]
            harness = "opencode"
            model = "z-ai/glm-5.3"
            thinking = "high"
        ''')) as e:
            e.run("up", "reviewer", "--no-brief")
            self.assertEqual(native(e.starts()["reviewer"]), [])
            split = e.calls("pane", "split")[0]
            content = json.loads(flag(split, "--env").split("=", 1)[1])
            agent = content["agents"]["reviewer"]
            self.assertEqual(content["default_agent"], "reviewer")
            self.assertEqual(agent["model"], "openrouter/z-ai/glm-5.3#high")
            self.assertEqual(agent["permissions"], [{"action": "edit", "resource": "*", "effect": "deny"}])
            self.assertIn("You are @reviewer", e.prompts()[0][1])
            p = e.run("check", "reviewer", check=False, FAKE_OPENCODE_VERSION="opencode 1.4.0")
            self.assertIn("devteam supports v2 or later", p.stdout)

    def test_adhoc_member_joins_the_brief(self):
        with Env() as e:
            e.run("up", "backend-dev-2", "--role", "backend-dev", "--model", "gpt-6-luna", "--thinking", "high",
                  "--no-brief", "--", "--search")
            call = e.starts()["backend-dev-2"]
            self.assertEqual(kind(call), "codex")
            self.assertEqual(native(call)[-1], "--search")
            cfg = devteam.Config(e.repo)
            self.assertEqual(cfg.members["backend-dev-2"],
                             {"role": "backend-dev", "harness": "codex", "model": "gpt-6-luna", "thinking": "high"})
            self.assertIn("## Workflow", cfg.body)  # the prose survived the edit
            self.assertIn("@backend-dev-2", e.run("status").stdout)
            self.assertIn("already running: backend-dev-2", e.run("up", "backend-dev-2", check=False).stderr)
            self.assertIn("is not a member or a role", e.run("check", "wizard", check=False).stderr)

    def test_say(self):
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

    def test_dry_run_outside_herdr_and_no_brief(self):
        with Env() as e:
            out = e.run("up", "--team", "planning", "--dry-run").stdout
            self.assertIn("would start @product", out)
            self.assertEqual(e.fake_state()["calls"], [])
            self.assertIn("not inside herdr", e.run("up", "pm", check=False, HERDR_ENV="").stderr)
        with Env(brief=None) as e:
            self.assertIn("Run `/devteam setup`", e.run("show", check=False).stderr)


class ContinueTests(unittest.TestCase):
    def test_brings_members_back_in_their_panes_and_sessions(self):
        with Env() as e:
            e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
            e.set_session("lead-dev", {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "cx-1"})
            e.set_session("frontend-dev", {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "cl-1"})
            e.set_session("pm", {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/s/2026_pi-1.jsonl"})
            e.kill("lead-dev", "frontend-dev", "pm")
            panes_before = {n: e.pane_of(n)["pane_id"] for n in ("lead-dev", "frontend-dev", "pm")}
            # The brief changed while they were gone: lead-dev moves to Luna.
            e.write_brief(TEMPLATE.replace('model = "gpt-6.1-sol"\nthinking = "high"',
                                           'model = "gpt-6-luna"\nthinking = "medium"'))
            self.assertIn("gone", e.run("status").stdout)

            e.run("continue")
            by = {c[2]: c for c in e.calls("agent", "start")[3:]}
            self.assertEqual({n: flag(c, "--pane") for n, c in by.items()}, panes_before)  # same panes, no splits
            self.assertEqual(len(e.calls("pane", "split")), 3)
            self.assertEqual(native(by["lead-dev"])[:2], ["resume", "cx-1"])
            self.assertEqual(flag(native(by["lead-dev"]), "-m"), "gpt-6-luna")  # current settings apply
            self.assertEqual(flag(native(by["frontend-dev"]), "--resume"), "cl-1")
            self.assertEqual(flag(native(by["pm"]), "--session"), "/s/2026_pi-1.jsonl")
            notes = sorted(n for n, t in e.prompts() if "picking up where we left off" in t)
            self.assertEqual(notes, ["frontend-dev", "lead-dev", "pm"])
            self.assertIn("every member is running", e.run("continue").stdout)

    def test_adopts_members_herdr_resumed_after_a_restart(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            # herdr restarted and resumed both agents in their labeled panes; names do not survive that.
            fs = e.fake_state()
            for p in fs["panes"].values():
                p["name"] = None
            e.write_fake(fs)
            self.assertIn("idle (unnamed)", e.run("status").stdout)
            self.assertIn("already running: lead-dev", e.run("up", "lead-dev", check=False).stderr)
            out = e.run("continue").stdout
            self.assertIn("adopted @lead-dev", out)
            self.assertIn("not necessarily gpt-6.1-sol", out)
            fs = e.fake_state()
            self.assertEqual({p["label"]: p["name"] for p in fs["panes"].values()},
                             {"cto": "cto", "lead-dev": "lead-dev", "pm": "pm"})
            self.assertEqual(len(e.calls("agent", "start")), 2)  # nothing relaunched
            standing = [n for n, t in e.prompts() if "herdr resumed your session" in t]
            self.assertEqual(sorted(standing), ["lead-dev", "pm"])
            self.assertIn("You are @pm", next(t for n, t in e.prompts() if n == "pm" and "standing" in t))

    def test_closed_pane_or_changed_harness_starts_fresh(self):
        with Env() as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("pm", {"agent": "pi", "kind": "path", "value": "/s/p.jsonl"})
            e.kill("pm")
            e.close("reviewer")
            e.write_brief(TEMPLATE.replace('model = "deepseek/deepseek-v4.1-flash"', 'model = "gpt-6-sol"')
                          .replace('[members.pm]\nharness = "pi"', '[members.pm]\nharness = "codex"'))
            out = e.run("continue", "pm", "reviewer").stdout
            self.assertIn("@reviewer has no pane any more", out)
            by = {c[2]: native(c) for c in e.calls("agent", "start")[2:]}
            self.assertNotIn("resume", by["pm"])  # a pi session can't open in codex
            self.assertNotIn("resume", by["reviewer"])
            self.assertEqual(e.pane_of("reviewer")["pane_id"], "w1:p4")  # a new, labeled pane


class ConfigTests(unittest.TestCase):
    def test_template_is_consistent(self):
        with Env() as e:
            cfg = devteam.Config(e.repo)
            for name in cfg.members:
                p = devteam.plan(cfg, name)
                self.assertIn(p["harness"], devteam.HARNESSES, name)
                self.assertTrue(p["model"], name)
            self.assertEqual(cfg.tracker, "TODO.md at the repo root")
            self.assertIn("## Project", cfg.body)
            out = e.run("show").stdout
            self.assertIn("@lead-dev", out)
            self.assertIn("tracker: TODO.md at the repo root", out)
        models = tomllib.loads((devteam.SKILL / "models.toml").read_text())
        for tiers in models["tiers"]["codex"].values():
            self.assertFalse(any("/" in m for m in tiers), "Codex takes bare model names")

    def test_no_tracker(self):
        with Env(team_brief('[members.pm]\n')) as e:
            self.assertIsNone(devteam.Config(e.repo).tracker)
            self.assertNotIn("tracker", e.run("prompt", "pm").stdout.split("## Talking to the team")[1])

    def test_infer_harness(self):
        for model, want in [("claude-fable-5-2", "claude"), ("opus", "claude"), ("anthropic/claude-sonnet-5-5", "claude"),
                            ("gpt-6.1-sol", "codex"), ("openai/gpt-6-luna", "codex"), ("z-ai/glm-5.3", "pi"),
                            ("deepseek/deepseek-v4.1-flash", "pi"), ("openrouter/openai/gpt-6-luna", "pi"),
                            ("nvidia/Qwen3.8-Flash-Next-NVFP4", "pi")]:
            self.assertEqual(devteam.infer_harness(model), want, model)

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
            self.assertIn("Reply only: ready.", text)
            self.assertIn("You are read-only", e.run("prompt", "reviewer").stdout)

    def test_session_id_from_ref(self):
        self.assertEqual(devteam.session_id({"kind": "id", "value": "abc"}), "abc")
        path = "/x/rollout-2026-11-11T13-02-52-01a1cbc6-e708-7623-986f-cc2e07efa28f.jsonl"
        self.assertEqual(devteam.session_id({"kind": "path", "value": path}), "01a1cbc6-e708-7623-986f-cc2e07efa28f")

    def test_bad_role_frontmatter_names_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.md"
            p.write_text("---\ntier: fast\noops\n---\nbody\n")
            with self.assertRaisesRegex(ValueError, "x.md:3"):
                devteam.parse_role(p)


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


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
            pimsg = lambda i, o, c: {"type": "message", "message": {"role": "assistant", "model": "deepseek/deepseek-v4.1-flash",
                                     "usage": {"input": i, "output": o, "cacheRead": 0, "cost": {"total": c}}}}
            jsonl(pi, [{"type": "session"}, pimsg(2000, 100, 0.01), pimsg(3000, 200, 0.02)])
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

            e.kill("pm")  # exited, but its pane still holds the session reference
            gone = {a["agent"]: a for a in json.loads(e.run("cost", "--json", "--home", str(h)).stdout)["agents"]}
            self.assertEqual(gone["pm"]["status"], "gone")
            self.assertAlmostEqual(gone["pm"]["cost_usd"], 0.03)

    def test_estimate_from_prices(self):
        models = {"gpt-6-luna": {"input": 1_000_000, "cache_read": 1_000_000, "cache_write": 0, "output": 100_000}}
        self.assertAlmostEqual(devteam.estimate(models, {"gpt-6-luna": {"input": 2.0, "output": 10.0}}), 2.0 + 0.2 + 1.0)
        self.assertIsNone(devteam.estimate(models, {}))


class ListingTests(unittest.TestCase):
    def test_listed_models_formats(self):
        pi = "provider  model  context\nopenrouter  z-ai/glm-5.3  1M\npax-vllm  nvidia/Q  262K\n"
        self.assertEqual(devteam.listed_models(pi), ["openrouter/z-ai/glm-5.3", "pax-vllm/nvidia/Q"])
        self.assertEqual(devteam.listed_models("openrouter/z-ai/glm-5.3\nollama/qwen3:8b\n"),
                         ["openrouter/z-ai/glm-5.3", "ollama/qwen3:8b"])


if __name__ == "__main__":
    unittest.main()
