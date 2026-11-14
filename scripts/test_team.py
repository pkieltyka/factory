"""Tests for team.py with a fake `herdr` and fake harness binaries; nothing real is launched."""
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

SCRIPT = pathlib.Path(__file__).with_name("team.py")
spec = importlib.util.spec_from_file_location("team", SCRIPT)
team = importlib.util.module_from_spec(spec)
spec.loader.exec_module(team)

FAKE_HERDR = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ["FAKE_HERDR"]
    s = json.load(open(path))
    s["calls"].append(sys.argv[1:])
    a = sys.argv[1:]
    def out(result):
        json.dump(s, open(path, "w")); print(json.dumps({"id": "1", "result": result})); sys.exit(0)
    def fail(msg):
        json.dump(s, open(path, "w")); print(json.dumps({"error": msg}), file=sys.stderr); sys.exit(1)
    agents = s["agents"]  # pane_id -> agent info
    if a[:2] == ["agent", "list"]:
        out({"type": "agent_list", "agents": list(agents.values())})
    if a[:2] == ["agent", "get"]:
        hit = agents.get(a[2]) or next((x for x in agents.values() if x.get("name") == a[2]), None)
        out({"type": "agent_info", "agent": hit}) if hit else fail("agent_not_found")
    if a[:2] == ["agent", "rename"]:
        agents[a[2]]["name"] = a[3]; out({"type": "agent_info", "agent": agents[a[2]]})
    if a[:2] == ["pane", "get"]:
        out({"type": "pane_info", "pane": {"pane_id": a[2]}}) if a[2] in s["panes"] else fail("pane_not_found")
    if a[:2] == ["pane", "split"]:
        pid = f"w1:p{len(s['panes']) + 1}"; s["panes"].append(pid)
        out({"type": "pane_info", "pane": {"pane_id": pid}})
    if a[:2] == ["agent", "start"]:
        name, kind, pane = a[2], a[a.index("--kind") + 1], a[a.index("--pane") + 1]
        agents[pane] = {"pane_id": pane, "name": name, "agent": kind, "agent_status": "idle"}
        out({"type": "agent_started", "agent": agents[pane], "argv": a[a.index("--") + 1:]})
    if a[:2] == ["agent", "prompt"]:
        out({"type": "agent_prompted", "agent": {}})
    fail("unexpected " + " ".join(a))
''').lstrip()

# pi/omp print a table of configured models; this one filters FAKE_MODELS by the query like pi does.
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
pax-vllm nvidia/Qwen3.8-Flash-Next-NVFP4
"""

LEVELS = ["low", "medium", "high", "xhigh", "max"]
CODEX_CACHE = {"models": [
    {"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
    {"slug": "gpt-6-astra", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6.1-sol", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
]}


class Env:
    """Temp home with a fake herdr, fake harnesses, a Codex model cache, and the lead (claude) in pane w1:p1."""

    def __init__(self, agents=None, harnesses=("claude", "codex", "pi", "omp", "opencode"), models=MODELS):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex/models_cache.json").write_text(json.dumps(CODEX_CACHE))
        self.cwd = self.root / "work"
        self.cwd.mkdir()
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
        lead = {"pane_id": "w1:p1", "name": None, "agent": "claude", "agent_status": "working"}
        self.write_fake({"calls": [], "panes": ["w1:p1"], "agents": {"w1:p1": lead, **(agents or {})}})
        self.env = {**os.environ, "HERDR_BIN": str(bin_ / "herdr"), "FAKE_HERDR": str(self.fake),
                    "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_TEAM_STATE": str(self.root / "state"),
                    "PATH": str(bin_), "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.root / "xdg"),
                    "FAKE_MODELS": models}
        self.env.pop("CODEX_HOME", None)

    def write_fake(self, s):
        self.fake.write_text(json.dumps(s))

    def fake_state(self):
        return json.loads(self.fake.read_text())

    def project(self, text):
        (self.cwd / "agent-team.toml").write_text(textwrap.dedent(text))

    def run(self, *args, check=True, **env):
        p = subprocess.run([sys.executable, str(SCRIPT), args[0], "--cwd", str(self.cwd), *args[1:]],
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
    def test_default_team_launches_and_gets_the_briefing(self):
        with Env() as e:
            out = e.run("up").stdout
            fs = e.fake_state()
            self.assertEqual(fs["agents"]["w1:p1"]["name"], "cto")
            splits = e.calls("pane", "split")
            self.assertEqual((splits[0][2], flag(splits[0], "--direction")), ("w1:p1", "right"))
            self.assertEqual((splits[1][2], flag(splits[1], "--direction")), ("w1:p2", "down"))
            self.assertTrue(all("--no-focus" in c and str(e.cwd) in c for c in splits))

            starts = e.starts()
            self.assertEqual({n: kind(c) for n, c in starts.items()},
                             {"lead-dev": "codex", "frontend-dev": "claude", "reviewer": "codex", "pm": "pi"})
            dev = native(starts["lead-dev"])
            self.assertEqual((flag(dev, "-m"), flag(dev, "-s")), ("gpt-6-luna", "workspace-write"))
            rev = native(starts["reviewer"])
            self.assertEqual(flag(rev, "-s"), "read-only")
            instructions = tomllib.loads(next(v for v in rev if v.startswith("developer_instructions=")))
            self.assertIn("You are @reviewer", instructions["developer_instructions"])
            self.assertIn("[from @reviewer]", instructions["developer_instructions"])
            fe = native(starts["frontend-dev"])
            self.assertEqual((flag(fe, "--model"), flag(fe, "--effort")), ("claude-opus-5-5", "medium"))
            prompt = pathlib.Path(flag(fe, "--append-system-prompt-file")).read_text()
            self.assertIn("Never run `git commit`", prompt)
            pm = native(starts["pm"])
            # pi was asked which provider serves the model, and gets provider/model.
            self.assertEqual(flag(pm, "--model"), "openrouter/deepseek/deepseek-v4.1-flash")
            self.assertEqual(flag(pm, "--thinking"), "low")

            briefed = {name for name, text in e.prompts() if text.startswith("# Team briefing (from @cto)")}
            self.assertEqual(briefed, {"lead-dev", "frontend-dev", "reviewer", "pm"})
            text = e.prompts()[0][1]
            for needle in ("| **@lead-dev** | Codex, gpt-6-luna, medium |", "**@pm** owns `TODO.md`",
                           "@lead-dev** works closely with @frontend-dev", "Reply only: ready."):
                self.assertIn(needle, text)
            self.assertNotIn("{", text)  # every placeholder filled; steps for absent roles dropped
            self.assertIn("say all --file", out)

    def test_failed_check_launches_nothing_and_explains_the_fix(self):
        with Env() as e:
            e.project('[members.backend-dev]\nmodel = "z-ai/glm-9"\n')
            p = e.run("up", "backend-dev", "lead-dev", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("Cannot launch: 1 member(s)", p.stdout)
            self.assertIn("pi has no model z-ai/glm-9 configured", p.stdout)
            self.assertIn("OpenRouter key", p.stdout)
            self.assertIn("Similar models that are available: openrouter/z-ai/glm-5.3", p.stdout)
            self.assertEqual(e.fake_state()["calls"], [])  # herdr never touched, lead-dev not started

    def test_thinking_levels_are_guarded(self):
        with Env() as e:
            e.project('''
                [members.frontend-dev]
                thinking = "max"
                [members.reviewer]
                thinking = "ultra"
                [members.lead-dev]
                thinking = "xhigh"
                [members.pm]
                thinking = "max"
                [members.backend-dev]
                thinking = "turbo"
            ''')
            out = e.run("check", "frontend-dev", "reviewer", "lead-dev", "pm", "backend-dev", check=False).stdout
            self.assertIn("thinking 'max' is not allowed for claude", out)
            self.assertIn("thinking 'ultra' is not allowed for codex", out)
            self.assertIn("pi openrouter/z-ai/glm-5.3 has no thinking level 'turbo'", out)
            rows = {l.split()[0]: l.split()[-1] for l in out.splitlines() if l.split() and l.split()[-1] in ("ok", "FAILED")}
            self.assertEqual(rows["lead-dev"], "ok")   # xhigh is fine for Codex
            self.assertEqual(rows["pm"], "ok")         # max is only blocked for claude and codex

    def test_model_and_harness_must_match(self):
        with Env() as e:
            e.project('''
                [members.lead-dev]
                model = "gpt-7-nova"
                [members.frontend-dev]
                harness = "claude"
                model = "z-ai/glm-5.3"
                [members.designer]
                model = "claude-opus-9"
            ''')
            out = e.run("check", "lead-dev", "frontend-dev", "designer", check=False).stdout
            self.assertIn("Codex does not offer gpt-7-nova", out)
            self.assertIn("gpt-6-luna", out)  # lists what Codex has
            self.assertIn("z-ai/glm-5.3 is not a Claude model", out)
            self.assertIn("unknown Claude model claude-opus-9", out)
            self.assertIn("claude_models in models.toml", out)

    def test_harness_not_installed(self):
        with Env(harnesses=("claude", "pi")) as e:
            out = e.run("check", "lead-dev", check=False).stdout
            self.assertIn("codex is not installed", out)
            self.assertIn("npm i -g @openai/codex", out)

    def test_local_model_and_ambiguous_providers(self):
        with Env(models=MODELS + "ollama z-ai/glm-5.3\n") as e:
            e.project('''
                [members.pm]
                harness = "omp"
                model = "nvidia/Qwen3.8-Flash-Next-NVFP4"
                [members.backend-dev]
                model = "z-ai/glm-5.3"
            ''')
            out = e.run("check", "pm", "backend-dev", check=False).stdout
            self.assertIn("pax-vllm/nvidia/Qwen3.8-Flash-Next-NVFP4", out)
            self.assertIn("serves z-ai/glm-5.3 from several providers: ollama/z-ai/glm-5.3, openrouter/z-ai/glm-5.3", out)

    def test_opencode_v2_member(self):
        with Env() as e:
            e.project('''
                [members.reviewer]
                harness = "opencode"
                model = "z-ai/glm-5.3"
                thinking = "high"
            ''')
            e.run("up", "reviewer", "--no-briefing")
            call = e.starts()["reviewer"]
            self.assertEqual(native(call), [])
            env = next(c for c in e.calls("pane", "split"))
            content = json.loads(flag(env, "--env").split("=", 1)[1])
            agent = content["agents"]["reviewer"]
            self.assertEqual(content["default_agent"], "reviewer")
            self.assertEqual(agent["model"], "openrouter/z-ai/glm-5.3#high")
            self.assertEqual(agent["permissions"], [{"action": "edit", "resource": "*", "effect": "deny"}])
            self.assertIn("You are @reviewer", e.prompts()[0][1])  # member prompt as first message

            p = e.run("check", "reviewer", check=False, FAKE_OPENCODE_VERSION="opencode 1.4.0")
            self.assertIn("this skill supports v2 or later", p.stdout)

    def test_adhoc_member_second_instance_and_clashes(self):
        running = {"w1:p9": {"pane_id": "w1:p9", "name": "lead-dev", "agent": "codex", "agent_status": "idle"}}
        with Env(running) as e:
            e.run("up", "backend-dev-2", "--role", "backend-dev", "--model", "gpt-6.1-sol", "--thinking", "high",
                  "--no-briefing", "--", "--search")
            call = e.starts()["backend-dev-2"]
            self.assertEqual(kind(call), "codex")
            self.assertEqual(native(call)[-1], "--search")
            self.assertIn("already running: lead-dev", e.run("up", "lead-dev", check=False).stderr)
            self.assertIn("is not a member or a role", e.run("check", "wizard", check=False).stderr)

    def test_say_and_resend_briefing(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-briefing")
            brief = e.cwd / "project.md"
            brief.write_text("# The project\nA Go TUI for GitHub issues.\n")
            e.run("say", "all", "--file", str(brief))
            e.run("say", "pm", "add a todo: ship it")
            sent = e.prompts()
            self.assertIn(("lead-dev", "[from @cto] # The project\nA Go TUI for GitHub issues.\n"), sent)
            self.assertIn(("pm", "[from @cto] add a todo: ship it"), sent)
            e.run("briefing", "--send")
            self.assertEqual({n for n, t in e.prompts() if t.startswith("# Team briefing")}, {"lead-dev", "pm"})
            self.assertIn("not running: reviewer", e.run("say", "reviewer", "hi", check=False).stderr)

    def test_dry_run_and_outside_herdr(self):
        with Env() as e:
            out = e.run("up", "--team", "planning", "--dry-run").stdout
            self.assertIn("would start @product", out)
            self.assertEqual(e.fake_state()["calls"], [])
            self.assertIn("not inside herdr", e.run("up", "pm", check=False, HERDR_ENV="").stderr)


class ConfigTests(unittest.TestCase):
    def test_built_in_formation_is_consistent(self):
        cfg = team.Config(tempfile.gettempdir())
        for name, m in cfg.members.items():
            p = team.plan(cfg, name)
            self.assertIn(p["harness"], team.HARNESSES, name)
            self.assertTrue(p["model"], name)
        for team_name, members in cfg.team["teams"].items():
            self.assertLessEqual(set(members), set(cfg.members), team_name)
        for harness in ("claude", "codex", "pi", "omp", "opencode"):
            for role, (meta, _) in cfg.roles.items():
                fam = "pi" if harness == "omp" else harness
                if fam in cfg.models["tiers"]:
                    self.assertTrue(cfg.models["tiers"][fam][meta["tier"]], f"{fam}.{meta['tier']}")
        self.assertEqual(cfg.board, "TODO.md")
        self.assertTrue(cfg.team["workflow"])
        for models in cfg.models["tiers"]["codex"].values():
            self.assertFalse(any("/" in m for m in models), "Codex takes bare model names")

    def test_infer_harness(self):
        for model, want in [("claude-fable-5-2", "claude"), ("opus", "claude"), ("anthropic/claude-sonnet-5-5", "claude"),
                            ("gpt-6.1-sol", "codex"), ("openai/gpt-6-luna", "codex"), ("z-ai/glm-5.3", "pi"),
                            ("deepseek/deepseek-v4.1-flash", "pi"), ("openrouter/openai/gpt-6-luna", "pi"),
                            ("nvidia/Qwen3.8-Flash-Next-NVFP4", "pi")]:
            self.assertEqual(team.infer_harness(model), want, model)

    def test_layers_and_validation(self):
        with Env() as e:
            user = e.root / "xdg/agent-team/team.toml"
            user.parent.mkdir(parents=True)
            user.write_text('[members.frontend-dev]\nthinking = "high"\n[members.pm]\nmodel = "z-ai/glm-5.3-flash"\n')
            e.project('[members.pm]\nharness = "omp"\n[members.backend-dev-2]\nrole = "backend-dev"\nmodel = "gpt-6-luna"\n')
            rows = {l.split()[0]: l.split() for l in e.run("config").stdout.splitlines() if l.split()}
            self.assertEqual(rows["frontend-dev"][2:5], ["claude", "claude-opus-5-5", "high"])  # user layer
            self.assertEqual(rows["pm"][2:4], ["omp", "z-ai/glm-5.3-flash"])  # project harness + user model
            self.assertEqual(rows["backend-dev-2"][1:3], ["backend-dev", "codex"])  # inferred from the model
            e.project('[members.wizard]\nmodel = "x"\n')
            self.assertIn("no role 'wizard'", e.run("config", check=False).stderr)
            e.project('[members.Bad]\nrole = "pm"\n')
            self.assertIn("must match", e.run("config", check=False).stderr)

    def test_init_copies_the_formation(self):
        with Env() as e:
            e.run("init")
            self.assertEqual((e.cwd / "agent-team.toml").read_text(), (team.SKILL / "team.toml").read_text())
            self.assertIn("exists", e.run("init", check=False).stderr)

    def test_bad_frontmatter_line_names_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.md"
            p.write_text("---\ntier: fast\noops\n---\nbody\n")
            with self.assertRaisesRegex(ValueError, "x.md:3"):
                team.parse_role(p)

    def test_listed_models_formats(self):
        pi = "provider  model  context\nopenrouter  z-ai/glm-5.3  1M\npax-vllm  nvidia/Q  262K\n"
        self.assertEqual(team.listed_models(pi), ["openrouter/z-ai/glm-5.3", "pax-vllm/nvidia/Q"])
        self.assertEqual(team.listed_models("openrouter/z-ai/glm-5.3\nollama/qwen3:8b\n"),
                         ["openrouter/z-ai/glm-5.3", "ollama/qwen3:8b"])


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


class CostTests(unittest.TestCase):
    def test_cost_per_agent_across_harnesses(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-briefing")
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
            jsonl(cx, [{"type": "turn_context", "payload": {"model": "gpt-6-luna"}},
                       {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                           "input_tokens": 1_000_000, "cached_input_tokens": 500_000, "output_tokens": 10_000}}}}])

            fs = e.fake_state()
            fs["agents"]["w1:p1"]["agent_session"] = {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "c-1"}
            fs["agents"]["w1:p2"]["agent_session"] = {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "x-1"}
            fs["agents"]["w1:p3"]["agent_session"] = {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": str(pi)}
            e.write_fake(fs)

            by = {a["agent"]: a for a in json.loads(e.run("cost", "--json", "--home", str(h)).stdout)["agents"]}
            self.assertEqual(by["cto"]["models"]["claude-fable-5-2"]["input"], 1000)  # duplicate block counted once
            self.assertEqual(by["cto"]["models"]["claude-haiku-5-5"]["input"], 500)   # subagent included
            self.assertAlmostEqual(by["cto"]["cost_usd"], 1.25)
            self.assertAlmostEqual(by["pm"]["cost_usd"], 0.03)
            self.assertEqual(by["lead-dev"]["models"]["gpt-6-luna"]["cache_read"], 500_000)
            self.assertIsNone(by["lead-dev"]["cost_usd"])

            table = e.run("cost", "--home", str(h)).stdout
            self.assertIn("$1.28+?", table)
            self.assertIn("Codex records tokens only", table)

            fs = e.fake_state()
            fs["agents"] = {}  # everyone exited; saved session refs still count
            e.write_fake(fs)
            gone = {a["agent"]: a for a in json.loads(e.run("cost", "--json", "--home", str(h)).stdout)["agents"]}
            self.assertEqual(gone["pm"]["status"], "gone")
            self.assertAlmostEqual(gone["pm"]["cost_usd"], 0.03)

    def test_estimate_from_prices(self):
        models = {"gpt-6-luna": {"input": 1_000_000, "cache_read": 1_000_000, "cache_write": 0, "output": 100_000}}
        self.assertAlmostEqual(team.estimate(models, {"gpt-6-luna": {"input": 2.0, "output": 10.0}}), 2.0 + 0.2 + 1.0)
        self.assertIsNone(team.estimate(models, {}))


if __name__ == "__main__":
    unittest.main()
