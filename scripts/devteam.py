#!/usr/bin/env python3
"""devteam: a software team of coding agents in herdr panes. You are the lead (CTO); each member runs
on its own harness, model and thinking level.

The team lives in the repo, under .devteam/:
  team-brief.md      settings (+++ TOML front matter +++) and prose for the team; the one source of truth
  project-brief.md   what we are building; sent after the team brief
  state/             git-ignored: panes and session ids of running members

  devteam.py init                     write .devteam/team-brief.md from the template
  devteam.py harnesses                which harnesses and recommended models this machine has
  devteam.py show [--team NAME]       the formation
  devteam.py check [NAMES|--team X]   can these members launch? launches nothing
  devteam.py up [NAMES|--team X]      check, launch in new panes, send the team and project briefs
  devteam.py continue                 restart members whose panes are gone, in their old sessions
  devteam.py status                   running members, their state, and drift from the brief
  devteam.py brief [--send]           render (or send) the team brief, then the project brief
  devteam.py say NAMES|all "msg"      message members as the lead (or --file)
  devteam.py cost [--json]            tokens and dollars per agent
  devteam.py prompt NAME              the system prompt a member gets

Harnesses: claude, codex, pi, omp, opencode (v2+). Nothing launches if any member fails the check.
"""
import argparse
import collections
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib

SKILL = pathlib.Path(__file__).resolve().parent.parent
HERDR = os.environ.get("HERDR_BIN", "herdr")
HARNESSES = ("claude", "codex", "pi", "omp", "opencode")
LABEL = {"claude": "Claude Code", "codex": "Codex", "pi": "pi", "omp": "omp", "opencode": "OpenCode"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CLAUDE_ALIASES = ("opus", "sonnet", "haiku", "fable")
PROBE_TIMEOUT = 60
DOT = ".devteam"

INSTALL = {
    "claude": "Install Claude Code (https://code.claude.com/docs) and log in once by running `claude`.",
    "codex": "Install the Codex CLI (`npm i -g @openai/codex`) and log in once by running `codex`.",
    "pi": "Install pi (`npm i -g @earendil-works/pi-coding-agent`) and configure a provider (`pi auth --help`).",
    "omp": "Install oh-my-pi so `omp` is on PATH, and configure a provider.",
    "opencode": "Install OpenCode v2 or later (https://opencode.ai) so `opencode` is on PATH.",
}


class Problem(Exception):
    """A member cannot launch: what is wrong, and how the user can fix it."""

    def __init__(self, what, fix):
        super().__init__(what)
        self.what, self.fix = what, fix


# ================================================================ files

def find_root(cwd):
    """The directory holding .devteam/, searching up from cwd; else cwd itself."""
    cwd = pathlib.Path(cwd).resolve()
    for d in (cwd, *cwd.parents):
        if (d / DOT / "team-brief.md").is_file():
            return d
    return cwd


def parse_front(text, path):
    """'+++\\n<toml>\\n+++\\n<body>' -> (settings, body)."""
    if not text.startswith("+++\n"):
        raise SystemExit(f"{path}: must start with a +++ line, then TOML settings, then +++")
    head, sep, body = text[4:].partition("\n+++\n")
    if not sep:
        raise SystemExit(f"{path}: no closing +++ line after the settings")
    try:
        return tomllib.loads(head), body.strip()
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path}: settings: {e}")


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
    for key in ("description", "tier", "thinking", "readonly"):
        if key not in meta:
            raise ValueError(f"{path}: missing frontmatter key {key!r}")
    return meta, body.strip()


def merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Config:
    def __init__(self, cwd, require=True):
        self.root = find_root(cwd)
        self.dir = self.root / DOT
        self.brief_file = self.dir / "team-brief.md"
        self.project_file = self.dir / "project-brief.md"
        self.roles = {p.stem: parse_role(p) for p in sorted((SKILL / "roles").glob("*.md"))}
        self.models = tomllib.loads((SKILL / "models.toml").read_text())
        if self.brief_file.is_file():
            self.team, self.body = parse_front(self.brief_file.read_text(), self.brief_file)
        elif require:
            raise SystemExit(f"no {DOT}/team-brief.md in {self.root} or above. Run `/devteam setup`"
                             " (or `devteam.py init`) first.")
        else:
            self.team, self.body = {}, ""
        if "prices" in self.team:
            self.models = merge(self.models, {"prices": self.team["prices"]})
        self.lead = {"name": "cto", **self.team.get("lead", {})}
        self.members = self.team.get("members", {})
        for name, m in self.members.items():
            if not NAME_RE.match(name):
                raise SystemExit(f"member name {name!r} must match {NAME_RE.pattern}")
            if m.get("role", name) not in self.roles:
                raise SystemExit(f"member {name!r}: no role {m.get('role', name)!r}; roles are {sorted(self.roles)}")
        for team, names in self.team.get("teams", {}).items():
            unknown = [n for n in names if n not in self.members]
            if unknown:
                raise SystemExit(f"team {team!r} names members not in [members]: {', '.join(unknown)}")

    @property
    def board(self):
        return self.team.get("board", "TODO.md")


def infer_harness(model):
    """Claude models -> claude, GPT models -> codex, anything else -> pi."""
    if model.startswith("openrouter/"):
        return "pi"
    m = model.lower().rsplit("/", 1)[-1].split("#")[0]
    if m.startswith("claude") or m in CLAUDE_ALIASES:
        return "claude"
    if m.startswith(("gpt-", "o3", "o4")) or "codex" in m:
        return "codex"
    return "pi"


def plan(cfg, name, overrides=None):
    """Member settings before availability checks."""
    m = {**cfg.members.get(name, {}), **{k: v for k, v in (overrides or {}).items() if v is not None}}
    role = m.get("role", name)
    if role not in cfg.roles:
        raise SystemExit(f"{name!r} is not a member or a role; members: {sorted(cfg.members)}")
    meta, _ = cfg.roles[role]
    model = m.get("model")
    harness = m.get("harness") or (infer_harness(model) if model else "claude")
    tier = m.get("tier") or meta["tier"]
    if not model and harness in cfg.models.get("tiers", {}):
        models = cfg.models["tiers"][harness][tier]
        model = models[min(m.get("pick", 0), len(models) - 1)]
    return {"name": name, "role": role, "harness": harness, "model": model, "tier": tier,
            "thinking": str(m.get("thinking") or m.get("effort") or meta["thinking"]),
            "readonly": meta["readonly"] == "true", "notes": m.get("notes", "")}


# ================================================================ availability checks

def run_probe(argv, env=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                           env={**os.environ, **(env or {})})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Problem(f"`{' '.join(argv[:3])}` failed: {e}",
                      "Run devteam.py outside any sandbox, or check that the harness starts on its own.")
    if p.returncode != 0:
        err = [l.strip() for l in (p.stderr.strip() or p.stdout.strip()).splitlines() if l.strip()]
        named = [l for l in err if re.match(r"(error|\w*Error)\b", l, re.I)]  # skip stack frames
        tail = (named or err or [f"exit {p.returncode}"])[0][:300]
        raise Problem(f"`{' '.join(argv[:3])}` failed: {tail}",
                      "If a sandbox blocks it (read-only file system), run devteam.py outside the sandbox.")
    return p.stdout


def listed_models(output):
    """provider/model ids from `pi --list-models`, `omp models` or `opencode models` output."""
    out, table = [], False
    for line in output.splitlines():
        toks = line.split()
        if not toks:
            continue
        if toks[0].lower() == "provider" and len(toks) > 1 and toks[1].lower() == "model":
            table = True  # pi style: provider  model  context ...
            continue
        if table and len(toks) >= 2:
            out.append(f"{toks[0]}/{toks[1]}")
        elif not table:
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
    raise Problem(f"{harness} has no model {base} configured", fix)


def stem(model):
    """Family name of a model id, for finding similar ones: z-ai/glm-5.3-flash -> glm."""
    return re.split(r"[-_.:\d]", model.split("#")[0].rsplit("/", 1)[-1], maxsplit=1)[0].lower()


def suggest(problem, listed, model):
    close = [x for x in listed if stem(model) and stem(model) in x.lower()][:6]
    if close:
        problem.fix += f" Similar models that are available: {', '.join(close)}."
    return problem


class Checker:
    """Answers 'can this member launch?' and caches what each harness reports."""

    def __init__(self, cfg, trust_models=False):
        self.cfg, self.trust, self.cache = cfg, trust_models, {}

    def probe(self, key, fn):
        if key not in self.cache:
            try:
                self.cache[key] = fn()
            except Problem as p:
                self.cache[key] = p
        if isinstance(self.cache[key], Problem):
            raise self.cache[key]
        return self.cache[key]

    def codex_models(self):
        home = pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex")
        path = home / "models_cache.json"
        if not path.exists():
            raise Problem(f"no Codex model list at {path}", "Run `codex` once while logged in so it caches its models.")
        data = json.loads(path.read_text())
        return {m["slug"]: [lv["effort"] for lv in m.get("supported_reasoning_levels", [])]
                for m in data.get("models", []) if m.get("slug")}

    def opencode_version(self):
        out = run_probe(["opencode", "--version"])
        m = re.search(r"(\d+)\.(\d+)", out)
        if not m or int(m.group(1)) < 2:
            raise Problem(f"OpenCode {out.strip() or '?'} is too old; devteam supports v2 or later",
                          "Upgrade with `opencode upgrade`.")
        return out.strip()

    def pi_listing(self, harness, query):
        argv = ["pi", "--list-models", query] if harness == "pi" else ["omp", "models", query]
        env = {"PI_OFFLINE": "1"} if harness == "pi" else None
        return listed_models(self.probe((harness, query), lambda: run_probe(argv, env)))

    def check(self, p):
        """-> member dict with a launch-ready model, or raises Problem."""
        harness, model, thinking = p["harness"], p["model"], p["thinking"]
        if harness not in HARNESSES:
            raise Problem(f"harness {harness!r} is not supported", f"Use one of: {', '.join(HARNESSES)}.")
        if not shutil.which(harness):
            raise Problem(f"{harness} is not installed (not on PATH)", INSTALL[harness])
        if not model:
            raise Problem("no model set", f"Set `model` for this member, or add [tiers.{harness}] in models.toml.")
        never = self.cfg.team.get("never_thinking", {}).get(harness, [])
        if thinking in never:
            raise Problem(f"thinking {thinking!r} is not allowed for {harness} (never_thinking)",
                          "Use a lower level such as high or xhigh.")
        p = dict(p)
        if harness == "claude":
            bare = model.removeprefix("anthropic/")
            known = self.cfg.models.get("claude_models", [])
            if infer_harness(bare) != "claude":
                raise Problem(f"{model} is not a Claude model; Claude Code runs Claude models only",
                              "Use a Claude model, or set harness to pi/omp/opencode (codex for GPT models).")
            if bare not in known and bare not in CLAUDE_ALIASES and not self.trust:
                raise Problem(f"unknown Claude model {bare}",
                              f"Known: {', '.join(known)}. If it is new, add it to claude_models in models.toml.")
            levels, p["model"] = self.cfg.models["thinking"]["claude"], bare
        elif harness == "codex":
            bare = model.removeprefix("openai/")
            models = self.probe("codex", self.codex_models)
            if bare not in models:
                raise Problem(f"Codex does not offer {bare}",
                              f"Codex lists: {', '.join(sorted(models))}. Pick one, or run `codex` to refresh its list.")
            levels, p["model"] = models[bare], bare
        elif harness in ("pi", "omp"):
            levels = self.cfg.models["thinking"][harness]
            if not self.trust:
                try:
                    p["model"] = resolve_listed(model, self.pi_listing(harness, model.rsplit("/", 1)[-1]), harness)
                except Problem as e:
                    if "has no model" in e.what and stem(model):
                        raise suggest(e, self.pi_listing(harness, stem(model)), model)
                    raise
        else:  # opencode
            self.probe("opencode-version", self.opencode_version)
            levels = None  # variants depend on the model; OpenCode reports a bad one at start
            if not self.trust:
                listed = listed_models(self.probe("opencode-models", lambda: run_probe(["opencode", "models"])))
                try:
                    p["model"] = resolve_listed(model, listed, harness)
                except Problem as e:
                    raise suggest(e, listed, model) if "has no model" in e.what else e
        if levels is not None and thinking not in levels:
            ok = [lv for lv in levels if lv not in never]
            raise Problem(f"{harness} {p['model']} has no thinking level {thinking!r}", f"Use one of: {', '.join(ok)}.")
        return p


def check_all(cfg, plans, trust_models=False):
    """-> (ready members, [(member, Problem)])."""
    checker, ready, problems = Checker(cfg, trust_models), [], []
    for p in plans:
        try:
            ready.append(checker.check(p))
        except Problem as e:
            problems.append((p, e))
    return ready, problems


def report_problems(problems):
    print(f"\nCannot launch: {len(problems)} member(s) failed the check. Nothing was started.\n")
    for p, e in problems:
        print(f"- @{p['name']} ({p['role']}, {p['harness']} {p['model'] or '?'}, thinking {p['thinking']}):")
        print(f"    problem: {e.what}")
        print(f"    fix:     {e.fix}")
    print(f"\nFix these, or change the member in {DOT}/team-brief.md, then run again.")


# ================================================================ briefs

def tag(name):
    return f"[from @{name}]"


def member_prompt(member, body, lead, cfg):
    """System prompt for one member: who it is, its role rules, and the protocol."""
    name, role = member["name"], member["role"]
    lines = [
        f"You are @{name}, the {role} on a software team of coding agents. Each agent runs in its own herdr pane"
        f" in the same repo. The lead (orchestrator and CTO) is @{lead}; the human speaks through @{lead}.",
        f"The team brief (`{DOT}/team-brief.md`: who is on the team and how we work) and the project brief"
        f" (`{DOT}/project-brief.md`) come from @{lead}. Re-read both whenever you lose track of context.",
        "",
        "## Your role",
        body,
        "",
        "## Talking to the team",
        f'- Message a teammate: herdr agent prompt <name> "{tag(name)} <message>"',
        f"  Always start with `{tag(name)}`. Messages to you start with `[from @sender]`. Never add --wait:"
        " two agents waiting on each other deadlock.",
        f"- Report to @{lead} when you finish a task, hit a blocker, or need a decision: one short message"
        " with what changed (paths), test status and open questions. Then stop and wait.",
        f"- Scope and architecture questions go to @{lead}; do not settle them between yourselves.",
        "- Bugs or todos outside your task: one line to @pm if the team has one, otherwise to the lead."
        f" Only @pm edits `{cfg.board}`.",
        "- Message other teammates when your role or the lead calls for it. Keep messages short and specific;"
        " never forward chatter or raw logs.",
        "- Do not start, stop, close or rename panes or agents.",
        "- If a herdr command fails (for example a sandbox blocks it), make your report your final reply;"
        " the lead can read your pane.",
    ]
    if cfg.team.get("lead_commits", True):
        lines.append(f"- Never run `git commit` or `git push`; @{lead} commits.")
    if member["readonly"]:
        lines.append("- You are read-only: never edit or write files. Report findings instead.")
    return "\n".join(lines) + "\n"


def repo_line(root):
    try:
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        url = ""
    url = re.sub(r"^(git@|https://)([^:/]+)[:/](.+?)(\.git)?$", r"\2/\3", url)
    return f"`{root}`" + (f" ({url})" if url else "")


def render_team_brief(cfg, members, lead):
    """The team brief as sent: roster built from the settings now, then the brief's prose, then how to talk."""
    L = cfg.lead
    out = [f"# Team brief (from @{lead})", "",
           f"We are a team of {len(members) + 1} agents, each in its own herdr pane, all working in the same repo:"
           f" {repo_line(cfg.root)}. This brief lives in `{DOT}/team-brief.md`.", "",
           "## Roles", "", "| Name | Agent | Role |", "|---|---|---|"]
    lead_agent = ", ".join(x for x in (LABEL.get(L.get("harness"), L.get("harness")), L.get("model"), L.get("thinking")) if x)
    out.append(f"| **@{lead}** | {lead_agent} | {L.get('notes') or 'Orchestrator and lead.'} |")
    for m in members:
        meta, _ = cfg.roles[m["role"]]
        out.append(f"| **@{m['name']}** | {LABEL.get(m['harness'], m['harness'])}, {m['model']}, {m['thinking']}"
                   f" | {m.get('notes') or meta['description']} |")
    if cfg.body:
        out += ["", cfg.body]
    names = ", ".join("`" + m["name"] + "`" for m in members)
    out += ["", "## Talking to each other", "",
            "- Refer to teammates by @name, and lean on them when it helps.",
            '- Message a teammate: `herdr agent prompt <name> "[from @you] your message"`',
            f"  Names: {names}, and `{lead}` for the lead. Start every message with your `[from @you]` tag."
            " Keep it short and specific.",
            f"- Report to @{lead} by message when you finish, are blocked, or need a decision.",
            f"- Scope and architecture questions go to @{lead}; do not settle them between yourselves.",
            "", "That is the whole team brief. Reply only: ready."
            + (" The project brief comes next." if cfg.project_file.is_file() else "")]
    return "\n".join(out).replace("{lead}", f"@{lead}") + "\n"


def launch_spec(m, prompt_path, prompt_text, extra, resume=None):
    """-> (native argv after `--`, pane env, send the member prompt as first message?)
    resume: a session id or path to reopen instead of starting fresh."""
    h, model, thinking, ro = m["harness"], m["model"], m["thinking"], m["readonly"]
    if h == "claude":
        argv = ["--model", model, "--effort", thinking, "--append-system-prompt-file", str(prompt_path), "--name", m["name"]]
        if ro:
            argv += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
        if resume:
            argv += ["--resume", session_id(resume)]
        return argv + extra, {}, False
    if h == "pi":
        argv = ["--model", model, "--thinking", thinking, "--append-system-prompt", str(prompt_path), "--name", m["name"]]
        if ro:
            argv += ["--exclude-tools", "edit,write"]
        if resume:
            argv += ["--session", resume["value"]]
        return argv + extra, {}, False
    if h == "omp":  # omp has no tool denylist; read-only relies on the prompt
        argv = ["--model", model, "--thinking", thinking, "--append-system-prompt", str(prompt_path)]
        if resume:
            argv.append(f"--resume={resume['value']}")
        return argv + extra, {}, False
    if h == "codex":
        argv = ["-m", model, "-c", f"model_reasoning_effort={json.dumps(thinking)}",
                "-c", f"developer_instructions={json.dumps(prompt_text, ensure_ascii=False)}",
                "-s", "read-only" if ro else "workspace-write"]
        if resume:
            argv = ["resume", session_id(resume)] + argv
        return argv + extra, {}, False
    # opencode v2: a primary agent per member via inline config. Its `system` key would replace
    # OpenCode's base prompt, so the member prompt goes in as the first message instead.
    agent = {"mode": "primary", "description": f"{m['role']} on the dev team",
             "model": model + (f"#{thinking}" if thinking and thinking != "default" else "")}
    if ro:
        agent["permissions"] = [{"action": "edit", "resource": "*", "effect": "deny"}]
    content = {"default_agent": m["name"], "agents": {m["name"]: agent}}
    argv = ["--session", session_id(resume)] if resume else []
    return argv + list(extra), {"OPENCODE_CONFIG_CONTENT": json.dumps(content)}, not resume


def session_id(ref):
    """A session id from a herdr session ref, which may hold an id or a file path."""
    if ref.get("kind") != "path":
        return ref["value"]
    name = pathlib.Path(ref["value"]).stem
    uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", name)
    return uuids[-1] if uuids else name


# ================================================================ herdr

def herdr(*args, check=True):
    p = subprocess.run([HERDR, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        msg = p.stderr.strip() or p.stdout.strip()
        if "Operation not permitted" in msg or "PermissionDenied" in msg:
            msg += ("\nherdr's socket is blocked, probably by a sandbox. In Claude Code add `herdr` to"
                    " sandbox.excludedCommands, or run with the sandbox off.")
        raise SystemExit(f"herdr {' '.join(args[:3])}: {msg}")
    if not p.stdout.strip():
        return {}
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"raw": p.stdout}


def live_agents():
    return herdr("agent", "list").get("result", {}).get("agents", [])


def unique(base, taken):
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def require_herdr():
    if os.environ.get("HERDR_ENV") != "1":
        raise SystemExit("not inside herdr (HERDR_ENV != 1). Start herdr and run this from the lead's pane.")


def ensure_lead(state, cfg):
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise SystemExit("HERDR_PANE_ID is not set; run this from the lead's herdr pane.")
    info = herdr("agent", "get", pane, check=False).get("result", {}).get("agent") or {}
    if not info:
        raise SystemExit(f"herdr sees no agent in this pane ({pane}). Run devteam.py from the lead agent's pane.")
    name = info.get("name")
    if not name:
        taken = {a.get("name") for a in live_agents()}
        name = unique(cfg.lead["name"], taken)
        herdr("agent", "rename", pane, name)
        print(f"named this pane's agent @{name}")
    old = state.get("lead", {}).get("name")
    if old and old != name:
        state["agents"].pop(old, None)
    state["lead"] = {"name": name, "pane_id": pane, "kind": info.get("agent")}
    rec = state["agents"].setdefault(name, {"role": "lead"})
    rec.update(kind=info.get("agent"), pane_id=pane)
    return name


def column_target(state):
    """First member goes right of the lead; later ones stack under the last member."""
    col = state.get("column")
    if col and "result" in herdr("pane", "get", col, check=False):
        return col, "down"
    return state["lead"]["pane_id"], "right"


# ================================================================ state

def state_file(cfg):
    return cfg.dir / "state" / "team.json"


def load_state(cfg):
    p = state_file(cfg)
    return json.loads(p.read_text()) if p.exists() else {"agents": {}}


def save_state(cfg, state):
    p = state_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    ignore = cfg.dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("state/\n")
    p.write_text(json.dumps(state, indent=2) + "\n")


def sync_sessions(state, agents):
    """Remember each member's session refs: cost keeps counting them, and `continue` resumes the last one."""
    by_pane = {a.get("pane_id"): name for name, a in state["agents"].items()}
    for a in agents:
        name, ref = by_pane.get(a.get("pane_id")), a.get("agent_session")
        if name and ref and a.get("name") == name:  # other herdr agents are not on this team
            seen = state["agents"][name].setdefault("sessions", [])
            if ref not in seen:
                seen.append(ref)


def normalize(m):
    """A state record in the shape of a member plan."""
    m = dict(m)
    m["harness"] = m.get("harness") or m.get("kind")
    m.setdefault("notes", "")
    m.setdefault("readonly", False)
    return m


def team_members(state, live_names=None):
    """Members (not the lead) recorded in state, optionally only those still live."""
    return [normalize(dict(a, name=n)) for n, a in state["agents"].items()
            if a.get("role") != "lead" and (live_names is None or n in live_names)]


# ================================================================ launching

def launch(cfg, state, m, lead, extra, timeout, resume=None):
    """Split a pane, start one member in it, record it. Exits on failure."""
    _, body = cfg.roles[m["role"]]
    text = member_prompt(m, body, lead, cfg)
    ppath = cfg.dir / "state" / "prompts" / f"{m['name']}.md"
    ppath.parent.mkdir(parents=True, exist_ok=True)
    ppath.write_text(text)
    argv, env, first_prompt = launch_spec(m, ppath, text, extra, resume)
    target, direction = column_target(state)
    split = ["pane", "split", target, "--direction", direction, "--cwd", str(cfg.root), "--no-focus"]
    for k, v in env.items():
        split += ["--env", f"{k}={v}"]
    pane = herdr(*split)["result"]["pane"]["pane_id"]
    state["column"] = pane
    rec = state["agents"].setdefault(m["name"], {})
    rec.update(role=m["role"], kind=m["harness"], model=m["model"], thinking=m["thinking"], notes=m["notes"],
               readonly=m["readonly"], pane_id=pane, started=datetime.datetime.now().isoformat(timespec="seconds"))
    save_state(cfg, state)
    started = herdr("agent", "start", m["name"], "--kind", m["harness"], "--pane", pane,
                    "--timeout", str(timeout), "--", *argv, check=False)
    if "result" not in started:
        print(f"@{m['name']} did not come up in pane {pane}. Stopping; later members were not started.")
        print(f"Look at it with: herdr pane read {pane} --source recent-unwrapped --lines 60")
        sys.exit(3)
    if first_prompt:
        herdr("agent", "prompt", m["name"], f"{tag(lead)} {text}\nReply only: ready.", check=False)
    how = "resumed" if resume else "started"
    print(f"{how} @{m['name']} ({m['role']}) on {m['harness']} {m['model']}, thinking {m['thinking']} in {pane}")


def send_briefs(cfg, state, lead, note=None):
    """Team brief to every live member, then the project brief if there is one, then an optional note."""
    live = {a.get("name") for a in live_agents()}
    members = team_members(state, live)
    if not members:
        print("no members running")
        return
    text = render_team_brief(cfg, members, lead)
    project = cfg.project_file.read_text() if cfg.project_file.is_file() else None
    for m in members:
        herdr("agent", "prompt", m["name"], text, check=False)
        if project:
            herdr("agent", "prompt", m["name"], f"{tag(lead)} Project brief (`{DOT}/project-brief.md`):\n\n{project}",
                  check=False)
        if note:
            herdr("agent", "prompt", m["name"], f"{tag(lead)} {note}", check=False)
    sent = "team brief" + (" and project brief" if project else "")
    print(f"sent the {sent} to {', '.join('@' + m['name'] for m in members)}")
    if not project:
        print(f"no {DOT}/project-brief.md yet: write it, then `devteam.py brief --send` (or `say all --file`).")


# ================================================================ commands

def cfg_of(args, require=True):
    return Config(pathlib.Path(args.cwd or os.getcwd()), require)


def selected(cfg, args):
    """Member plans named by the command line."""
    overrides = {"harness": args.harness, "model": args.model, "tier": args.tier, "thinking": args.thinking,
                 "pick": args.pick or None}
    if args.role:
        if args.team or len(args.names) > 1:
            raise SystemExit("--role adds one ad-hoc member; launch others separately")
        name = args.names[0] if args.names else args.role
        return [plan(cfg, name, {**overrides, "role": args.role})]
    names = list(args.names)
    if args.team or not names:
        team = args.team or "default"
        if team not in cfg.team.get("teams", {}):
            raise SystemExit(f"no team {team!r} in {DOT}/team-brief.md; teams: {sorted(cfg.team.get('teams', {}))}")
        names += cfg.team["teams"][team]
    if len(names) > 1 and any(v is not None for v in overrides.values()):
        raise SystemExit("--harness/--model/--tier/--thinking apply to one member; name exactly one")
    return [plan(cfg, n, overrides) for n in names]


def print_plan(plans, extra_col=None):
    rows = [("Member", "Role", "Harness", "Model", "Thinking") + ((extra_col[0],) if extra_col else ())]
    for p in plans:
        rows.append(("@" + p["name"], p["role"], p["harness"], p["model"] or "?", p["thinking"])
                    + ((extra_col[1](p),) if extra_col else ()))
    print_table(rows)


def cmd_init(args):
    dest = pathlib.Path(args.cwd or os.getcwd()).resolve() / DOT / "team-brief.md"
    if dest.exists() and not args.force:
        raise SystemExit(f"{dest} exists; edit it, or pass --force to replace it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((SKILL / "templates/team-brief.md").read_text())
    (dest.parent / ".gitignore").write_text("state/\n")
    print(f"wrote {dest}; edit the members, teams and prose, then run `devteam.py check`")


def cmd_harnesses(args):
    """What this machine can run: installed harnesses and which recommended models each can use."""
    cfg = cfg_of(args, require=False)
    checker = Checker(cfg)
    for h in HARNESSES:
        if not shutil.which(h):
            print(f"{h:9} not installed. {INSTALL[h]}")
            continue
        tiers = cfg.models.get("tiers", {}).get("pi" if h == "omp" else h, {})
        candidates = list(dict.fromkeys(m for tier in ("top", "mid", "fast") for m in tiers.get(tier, [])))
        ok, missing, broken = [], [], None
        for model in candidates:
            try:
                ok.append(checker.check({"name": h, "role": "", "harness": h, "model": model,
                                         "thinking": "medium", "readonly": False, "notes": ""})["model"])
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
            print(f"{'':10}Codex offers: {', '.join(sorted(checker.probe('codex', checker.codex_models)))}")


def cmd_show(args):
    cfg = cfg_of(args)
    L = cfg.lead
    print(f"{cfg.brief_file}\nlead (you): @{L['name']} on {L.get('harness', '?')} {L.get('model', '?')},"
          f" thinking {L.get('thinking', '?')}   board: {cfg.board}"
          f"   project brief: {'yes' if cfg.project_file.is_file() else 'not written yet'}\n")
    names = cfg.team["teams"][args.team] if args.team else list(cfg.members)
    print_plan([plan(cfg, n) for n in names])
    print("\nteams:")
    for k, v in cfg.team.get("teams", {}).items():
        print(f"  {k}: {' '.join(v)}")


def cmd_check(args):
    cfg = cfg_of(args)
    plans = selected(cfg, args)
    ready, problems = check_all(cfg, plans, args.trust_models)
    ok = {p["name"]: p for p in ready}
    print_plan([ok.get(p["name"], p) for p in plans], ("Check", lambda p: "ok" if p["name"] in ok else "FAILED"))
    if problems:
        report_problems(problems)
        sys.exit(2)


def cmd_up(args):
    cfg = cfg_of(args)
    plans = selected(cfg, args)
    if args.name:
        if len(plans) != 1:
            raise SystemExit("--name applies to one member")
        plans[0]["name"] = args.name
    for p in plans:
        if not NAME_RE.match(p["name"]):
            raise SystemExit(f"agent name {p['name']!r} must match {NAME_RE.pattern}")

    ready, problems = check_all(cfg, plans, args.trust_models)  # every member must pass first
    if problems:
        print_plan(plans)
        report_problems(problems)
        sys.exit(2)
    if args.dry_run:
        for m in ready:
            argv, _, _ = launch_spec(m, cfg.dir / "state/prompts" / f"{m['name']}.md", "...", args.extra)
            print(f"would start @{m['name']} ({m['role']}) on {m['harness']} {m['model']}, thinking {m['thinking']}")
            print("  herdr agent start", m["name"], "--kind", m["harness"], "--pane <new> --",
                  " ".join(a if len(a) < 80 else a[:77] + "..." for a in argv))
        return

    require_herdr()
    state = load_state(cfg)
    lead = ensure_lead(state, cfg)
    live = {a.get("name") for a in live_agents()}
    clash = [m["name"] for m in ready if m["name"] in live]
    if clash:
        raise SystemExit(f"already running: {', '.join(clash)}. Use --name for another instance.")
    for m in ready:
        launch(cfg, state, m, lead, args.extra, args.timeout)
    if args.no_brief:
        print("next: devteam.py brief --send")
        return
    send_briefs(cfg, state, lead)


def cmd_continue(args):
    """Bring the team back after its panes or sessions were lost: resume each missing member's last session."""
    require_herdr()
    cfg = cfg_of(args)
    state = load_state(cfg)
    if not team_members(state):
        raise SystemExit("no team recorded here yet; run `devteam.py up` first")
    live = live_agents()
    sync_sessions(state, live)
    lead = ensure_lead(state, cfg)
    live_names = {a.get("name") for a in live}
    gone = [m for m in team_members(state) if m["name"] not in live_names and (not args.names or m["name"] in args.names)]
    plans, skipped = [], []
    for m in gone:
        if m["name"] in cfg.members:
            plans.append(plan(cfg, m["name"]))  # current settings from the brief, not the old ones
        elif m["role"] in cfg.roles:
            plans.append(plan(cfg, m["name"], {"role": m["role"], "harness": m["harness"], "model": m.get("model"),
                                               "thinking": m.get("thinking")}))
        else:
            skipped.append(m["name"])
    for n in skipped:
        print(f"skip @{n}: no longer in the team brief and its role is gone")
    ready, problems = check_all(cfg, plans, args.trust_models)
    if problems:
        print_plan(plans)
        report_problems(problems)
        sys.exit(2)
    for m in ready:
        sessions = state["agents"][m["name"]].get("sessions", [])
        same = [s for s in sessions if s.get("agent") == m["harness"]] if not args.fresh else []
        launch(cfg, state, m, lead, args.extra, args.timeout, resume=same[-1] if same else None)
    if not ready:
        print("every member is running")
    save_state(cfg, state)
    send_briefs(cfg, state, lead, note=(
        f"We are picking up where we left off. Re-read `{DOT}/team-brief.md` and `{DOT}/project-brief.md`,"
        f" check `{cfg.board}`, then send @{lead} one line: what you were doing and what you will do next."))


def drift(cfg, name, rec):
    """Differences between a running member and what the team brief now says."""
    if name not in cfg.members:
        return ["not in the team brief"]
    want, out = plan(cfg, name), []
    for key, have in (("harness", rec.get("kind")), ("model", rec.get("model")), ("thinking", rec.get("thinking")),
                      ("role", rec.get("role"))):
        w = want[key]
        if have and w and have != w and not (key == "model" and have.endswith("/" + w)):
            out.append(f"{key} {have} -> {w}")
    return out


def cmd_status(args):
    cfg = cfg_of(args)
    state = load_state(cfg)
    live = {a.get("pane_id"): a for a in live_agents()}
    sync_sessions(state, live.values())
    if state["agents"]:
        save_state(cfg, state)
    rows = [("Agent", "Role", "Harness", "Model", "Thinking", "Status", "Pane", "Changed in brief")]
    for name, a in state["agents"].items():
        la = live.get(a.get("pane_id"))
        status = la.get("agent_status", "?") if la and la.get("name") == name else "gone"
        changes = "" if a.get("role") == "lead" else "; ".join(drift(cfg, name, a))
        rows.append(("@" + name, a.get("role", ""), a.get("kind") or "", a.get("model") or "", a.get("thinking") or "",
                     status, a.get("pane_id", ""), changes))
    if len(rows) == 1:
        print(f"No team running from {cfg.brief_file}. Next: `devteam.py check`, then `devteam.py up`.")
        return
    print_table(rows)
    gone = [r[0] for r in rows[1:] if r[5] == "gone" and r[1] != "lead"]
    if gone:
        print(f"\n{', '.join(gone)} gone: `devteam.py continue` resumes them in their last sessions.")
    if any(r[7] for r in rows[1:]):
        print("Changed members pick up the new settings when restarted (close the pane, then `continue`).")
    if not cfg.project_file.is_file():
        print(f"No {DOT}/project-brief.md yet.")


def cmd_brief(args):
    cfg = cfg_of(args)
    state = load_state(cfg)
    if args.send:
        require_herdr()
        lead = ensure_lead(state, cfg)
        save_state(cfg, state)
        send_briefs(cfg, state, lead)
        return
    members = team_members(state)
    if not members or args.team:
        team = args.team or "default"
        if team not in cfg.team.get("teams", {}):
            raise SystemExit(f"no team {team!r}; teams: {sorted(cfg.team.get('teams', {}))}")
        members = [plan(cfg, n) for n in cfg.team["teams"][team]]
    lead = (state.get("lead") or {}).get("name") or cfg.lead["name"]
    print(render_team_brief(cfg, members, lead))


def cmd_say(args):
    """Message members as the lead: `say all ...` or `say lead-dev frontend-dev ...`."""
    require_herdr()
    cfg = cfg_of(args)
    state = load_state(cfg)
    lead = ensure_lead(state, cfg)
    save_state(cfg, state)
    text = pathlib.Path(args.file).read_text() if args.file else args.message
    if not text:
        raise SystemExit("give a message or --file")
    live = {a.get("name") for a in live_agents()}
    names = [m["name"] for m in team_members(state, live)] if args.to == ["all"] else [n.lstrip("@") for n in args.to]
    missing = [n for n in names if n not in live]
    if missing:
        raise SystemExit(f"not running: {', '.join(missing)}")
    for n in names:
        herdr("agent", "prompt", n, f"{tag(lead)} {text}", check=False)
    print(f"sent to {', '.join('@' + n for n in names)}")


def cmd_prompt(args):
    cfg = cfg_of(args)
    p = plan(cfg, args.name)
    _, body = cfg.roles[p["role"]]
    print(f"# @{p['name']} ({p['role']}) on {p['harness']} {p['model']}, thinking {p['thinking']}\n")
    print(member_prompt(p, body, cfg.lead["name"], cfg))


# ================================================================ cost

def find_session_file(ref, home):
    """herdr agent_session -> local session file, or None."""
    if not ref:
        return None
    value, agent = ref.get("value", ""), ref.get("agent", "")
    if ref.get("kind") == "path":
        p = pathlib.Path(value).expanduser()
        return p if p.exists() else None
    globs = {
        "claude": [f".claude/projects/*/{value}.jsonl"],
        "pi": [f".pi/agent/sessions/*/*_{value}.jsonl"],
        "omp": [f".omp/agent/sessions/*/*_{value}.jsonl"],
        "codex": [f".codex/sessions/*/*/*/rollout-*{value}.jsonl"],
    }.get(agent, [])
    for g in globs:
        hits = sorted(home.glob(g))
        if hits:
            return hits[-1]
    return None


def records(path):
    with path.open() as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def usage_claude(path):
    """Tokens per model across the transcript and its subagents; dollars from Claude Code's cost-state."""
    models = collections.defaultdict(collections.Counter)
    total = None
    files = [path] + sorted((path.with_suffix("") / "subagents").glob("agent-*.jsonl"))
    for i, f in enumerate(files):
        seen = set()
        for o in records(f):
            if i == 0 and o.get("type") == "cost-state":
                total = o.get("totalCostUSD")
            m = o.get("message")
            if o.get("type") != "assistant" or not isinstance(m, dict) or not m.get("usage"):
                continue
            key = m.get("id") or o.get("uuid")
            if key in seen:  # one API response is logged once per content block
                continue
            seen.add(key)
            u, c = m["usage"], models[m.get("model", "?")]
            c["input"] += u.get("input_tokens", 0)
            c["cache_read"] += u.get("cache_read_input_tokens", 0)
            c["cache_write"] += u.get("cache_creation_input_tokens", 0)
            c["output"] += u.get("output_tokens", 0)
    return models, total, "recorded" if total is not None else None


def usage_pi(path):
    models = collections.defaultdict(collections.Counter)
    dollars = 0.0
    for o in records(path):
        m = o.get("message")
        if o.get("type") != "message" or not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        u = m.get("usage") or {}
        c = models[m.get("model", "?")]
        c["input"] += u.get("input", 0)
        c["cache_read"] += u.get("cacheRead", 0)
        c["cache_write"] += u.get("cacheWrite", 0)
        c["output"] += u.get("output", 0)
        dollars += (u.get("cost") or {}).get("total", 0) or 0
    return models, dollars, "recorded"


def usage_codex(path):
    model, total = "?", None
    for o in records(path):
        p = o.get("payload") or {}
        if o.get("type") == "turn_context" and p.get("model"):
            model = p["model"]
        if p.get("type") == "token_count" and (p.get("info") or {}).get("total_token_usage"):
            total = p["info"]["total_token_usage"]
    models = collections.defaultdict(collections.Counter)
    if total:
        cached = total.get("cached_input_tokens", 0)
        models[model].update({"input": total.get("input_tokens", 0) - cached, "cache_read": cached,
                              "cache_write": total.get("cache_write_input_tokens", 0),
                              "output": total.get("output_tokens", 0)})
    return models, None, None


PARSERS = {"claude": usage_claude, "pi": usage_pi, "omp": usage_pi, "codex": usage_codex}  # omp keeps pi's format


def estimate(models, prices):
    """Dollars from [prices], or None if any model with tokens is unpriced."""
    total = 0.0
    for model, c in models.items():
        p = prices.get(model) or prices.get(model.split("/")[-1])
        if not p:
            return None
        total += (c["input"] * p.get("input", 0) + c["output"] * p.get("output", 0)
                  + c["cache_read"] * p.get("cache_read", p.get("input", 0) / 10)
                  + c["cache_write"] * p.get("cache_write", p.get("input", 0) * 1.25)) / 1e6
    return total


def cmd_cost(args):
    cfg = cfg_of(args)
    prices = cfg.models.get("prices", {})
    home = pathlib.Path(args.home).expanduser()
    state = load_state(cfg)
    live = []
    if not args.offline:
        live = live_agents()
        sync_sessions(state, live)
        save_state(cfg, state)
    status = {a.get("name"): a.get("agent_status") for a in live}

    report = []
    for name, a in state["agents"].items():
        entry = {"agent": name, "role": a.get("role", ""), "harness": a.get("kind"), "status": status.get(name, "gone"),
                 "session_files": [], "models": {}, "cost_usd": None, "cost_source": None}
        models, dollars, sources = collections.defaultdict(collections.Counter), 0.0, set()
        for ref in a.get("sessions", []):
            kind = ref.get("agent") or a.get("kind")
            path = find_session_file(ref, home)
            if not path or kind not in PARSERS or str(path) in entry["session_files"]:
                continue  # a resumed session appears once even if herdr reported it twice
            entry["harness"] = kind
            entry["session_files"].append(str(path))
            m, d, source = PARSERS[kind](path)
            if d is None and m:
                d = estimate(m, prices)
                source = "estimated" if d is not None else None
            for k, c in m.items():
                models[k].update(c)
            if m or d:
                sources.add(source)
                dollars += d or 0
        if models:
            entry["models"] = {m: dict(c) for m, c in models.items()}
            if None not in sources:
                entry["cost_usd"] = dollars
                entry["cost_source"] = "estimated" if "estimated" in sources else "recorded"
        report.append(entry)

    if args.json:
        print(json.dumps({"root": str(cfg.root), "agents": report}, indent=2))
        return
    rows = [("Agent", "Role", "Harness", "Model", "In", "Out", "Cost", "Status")]
    known, unknown = 0.0, False
    for e in report:
        ms = e["models"]
        tin = sum(c.get("input", 0) + c.get("cache_read", 0) + c.get("cache_write", 0) for c in ms.values())
        tout = sum(c.get("output", 0) for c in ms.values())
        model = ", ".join(short(m) for m in ms) or (state["agents"][e["agent"]].get("model") or "")
        if e["cost_usd"] is None:
            cost, unknown = ("?" if ms else ""), unknown or bool(ms)
        else:
            known += e["cost_usd"]
            cost = ("~" if e["cost_source"] == "estimated" else "") + f"${e['cost_usd']:.2f}"
        rows.append(("@" + e["agent"], e["role"], e["harness"] or "", model, fmt_tok(tin), fmt_tok(tout), cost, e["status"]))
    rows.append(("Total", "", "", "", "", "", f"${known:.2f}" + ("+?" if unknown else ""), ""))
    print_table(rows, total=True)
    notes = []
    if any(e["harness"] == "claude" and e["models"] and e["cost_source"] is None for e in report):
        notes.append("Claude Code writes dollars when a session exits or resumes; until then it shows tokens"
                     " (or add [prices] to the team brief for an estimate).")
    if any(e["harness"] == "codex" and e["models"] and e["cost_source"] is None for e in report):
        notes.append("Codex records tokens only; add its models to [prices] for dollars.")
    if any(e["harness"] == "opencode" for e in report):
        notes.append("OpenCode keeps sessions in a database devteam does not read yet; use `opencode stats`.")
    if any(e["models"] == {} and e["status"] != "gone" and e["harness"] != "opencode" for e in report):
        notes.append("Agents without numbers have not reported a session file to herdr yet"
                     " (run `herdr integration install <harness>` once per harness).")
    for n in notes:
        print("note:", n)


# ================================================================ output helpers

def fmt_tok(n):
    if not n:
        return ""
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(int(n))


def short(model):
    return model.removeprefix("claude-").removeprefix("openrouter/")


def print_table(rows, total=False):
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for i, r in enumerate(rows):
        if total and i == len(rows) - 1:
            print("  ".join("-" * w for w in widths).rstrip())
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths).rstrip())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--cwd", help="where to look for .devteam/ (default: here, then parent directories)")

    def launching(sp):
        sp.add_argument("--timeout", type=int, default=60000, help="ms to wait for each agent to be ready")
        sp.add_argument("--trust-models", action="store_true",
                        help="skip asking harnesses which models they have (harness and thinking checks still run)")

    def selection(sp):
        sp.add_argument("names", nargs="*", help="member names (default: the `default` team)")
        sp.add_argument("--team", help="a roster from [teams]")
        sp.add_argument("--role", help="an ad-hoc member with this role (name: first NAME, else the role)")
        sp.add_argument("--harness", choices=HARNESSES)
        sp.add_argument("--model", help="exact model id")
        sp.add_argument("--tier", choices=["top", "mid", "fast"], help="pick the model by tier from models.toml")
        sp.add_argument("--thinking", "--effort", dest="thinking", help="thinking level")
        sp.add_argument("--pick", type=int, default=0, help="Nth model of the tier list")
        sp.add_argument("--trust-models", action="store_true",
                        help="skip asking harnesses which models they have (harness and thinking checks still run)")
        common(sp)

    it = sub.add_parser("init", help="write .devteam/team-brief.md from the template")
    it.add_argument("--force", action="store_true")
    common(it)

    common(sub.add_parser("harnesses", help="installed harnesses and which recommended models they can use"))

    sh = sub.add_parser("show", help="the formation")
    sh.add_argument("--team")
    common(sh)

    selection(sub.add_parser("check", help="verify members can launch; launches nothing"))

    up = sub.add_parser("up", help="check, launch members in new herdr panes, send the briefs",
                        epilog="Arguments after a bare -- go to every launched harness, "
                               "e.g. up frontend-dev -- --permission-mode acceptEdits")
    selection(up)
    up.add_argument("--name", help="agent name for a single member (for a second instance)")
    up.add_argument("--timeout", type=int, default=60000, help="ms to wait for each agent to be ready")
    up.add_argument("--no-brief", action="store_true", help="do not send the briefs afterwards")
    up.add_argument("--dry-run", action="store_true", help="check and print the launch plan only")

    co = sub.add_parser("continue", help="restart gone members in their last sessions and re-brief the team")
    co.add_argument("names", nargs="*", help="only these members")
    co.add_argument("--fresh", action="store_true", help="start new sessions instead of resuming")
    launching(co)
    common(co)

    common(sub.add_parser("status", help="running members, state, and drift from the team brief"))

    bf = sub.add_parser("brief", help="print the team brief as members get it, or --send it (and the project brief)")
    bf.add_argument("--send", action="store_true")
    bf.add_argument("--team", help="preview for this team")
    common(bf)

    sy = sub.add_parser("say", help="message members as the lead")
    sy.add_argument("to", nargs="+", help="member names, or `all`; the last word is the message unless --file")
    sy.add_argument("--file", help="send this file's contents")
    common(sy)

    cs = sub.add_parser("cost", help="tokens and dollars per agent")
    cs.add_argument("--json", action="store_true")
    cs.add_argument("--home", default=str(pathlib.Path.home()))
    cs.add_argument("--offline", action="store_true", help="use saved session refs only; do not ask herdr")
    common(cs)

    pr = sub.add_parser("prompt", help="the system prompt a member gets")
    pr.add_argument("name")
    common(pr)

    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    args = ap.parse_args(argv)
    args.extra = extra
    if args.cmd == "say":
        args.message = None if args.file else (args.to.pop() if len(args.to) > 1 else None)
    if getattr(args, "pick", 0) < 0:
        ap.error("--pick must be >= 0")
    {"init": cmd_init, "harnesses": cmd_harnesses, "show": cmd_show, "check": cmd_check, "up": cmd_up,
     "continue": cmd_continue, "status": cmd_status, "brief": cmd_brief, "say": cmd_say, "cost": cmd_cost,
     "prompt": cmd_prompt}[args.cmd](args)


if __name__ == "__main__":
    main()
