#!/usr/bin/env python3
"""Run a team of coding agents in herdr panes. You are the lead (CTO); members run on any harness and model.

  team.py config [--team NAME]          the formation: each member's role, harness, model, thinking
  team.py check  [NAMES | --team X]     verify harnesses, models and thinking levels; launches nothing
  team.py up     [NAMES | --team X]     check, launch members in new panes, then send the team briefing
  team.py up --role backend-dev --name backend-dev-2 --model z-ai/glm-5.3      an extra member, ad hoc
  team.py briefing [--send]             the team briefing: roster, role details, workflow, how to talk
  team.py say NAMES|all "message"       message members as the lead (or --file project.md)
  team.py roster                        who is on the team and their live status
  team.py cost [--json]                 tokens and dollars per agent from each harness's session files
  team.py brief NAME                    the system prompt a member gets
  team.py init                          copy the default formation to ./agent-team.toml

Harnesses: claude, codex, pi, omp, opencode (v2+). `up` launches nothing if any member fails `check`.
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


# ================================================================ config

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


def config_files(cwd, explicit=None):
    """Team config layers, lowest first: built-in, user, project, --config."""
    xdg = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")
    files = [SKILL / "team.toml", xdg / "agent-team/team.toml", pathlib.Path(cwd) / "agent-team.toml"]
    files = [f for f in files if f.is_file()]
    if explicit:
        p = pathlib.Path(explicit).expanduser()
        if not p.is_file():
            raise SystemExit(f"--config {explicit}: no such file")
        files.append(p)
    return files


class Config:
    def __init__(self, cwd, explicit=None):
        self.cwd = pathlib.Path(cwd)
        self.roles = {p.stem: parse_role(p) for p in sorted((SKILL / "roles").glob("*.md"))}
        self.models = tomllib.loads((SKILL / "models.toml").read_text())
        self.files = config_files(cwd, explicit)
        team = {}
        for f in self.files:
            try:
                team = merge(team, tomllib.loads(f.read_text()))
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"{f}: {e}")
        if "prices" in team:
            self.models = merge(self.models, {"prices": team["prices"]})
        self.team = team
        self.lead = {"name": "cto", **team.get("lead", {})}
        self.members = team.get("members", {})
        for name, m in self.members.items():
            if not NAME_RE.match(name):
                raise SystemExit(f"member name {name!r} must match {NAME_RE.pattern}")
            if m.get("role", name) not in self.roles:
                raise SystemExit(f"member {name!r}: no role {m.get('role', name)!r}; roles are {sorted(self.roles)}")

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
    harness = m.get("harness") or (infer_harness(model) if model else cfg.team.get("harness", "claude"))
    tier = m.get("tier") or meta["tier"]
    if not model and harness in cfg.models.get("tiers", {}):
        models = cfg.models["tiers"][harness][tier]
        model = models[min(m.get("pick", 0), len(models) - 1)]
    return {"name": name, "role": role, "harness": harness, "model": model, "tier": tier,
            "thinking": str(m.get("thinking") or m.get("effort") or meta["thinking"]),
            "readonly": meta["readonly"] == "true", "notes": m.get("notes", ""),
            "works_with": list(m.get("works_with", []))}


# ================================================================ availability checks

def run_probe(argv, env=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                           env={**os.environ, **(env or {})})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Problem(f"`{' '.join(argv[:3])}` failed: {e}",
                      "Run team.py outside any sandbox, or check that the harness starts on its own.")
    if p.returncode != 0:
        err = [l.strip() for l in (p.stderr.strip() or p.stdout.strip()).splitlines() if l.strip()]
        named = [l for l in err if re.match(r"(error|\w*Error)\b", l, re.I)]  # skip stack frames
        tail = (named or err or [f"exit {p.returncode}"])[0][:300]
        raise Problem(f"`{' '.join(argv[:3])}` failed: {tail}",
                      "If a sandbox blocks it (read-only file system), run team.py outside the sandbox.")
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
                      "Write the model as provider/model in the team config to pick one.")
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
            raise Problem(f"OpenCode {out.strip() or '?'} is too old; this skill supports v2 or later",
                          "Upgrade with `opencode upgrade`.")
        return out.strip()

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
                def listing(query):
                    argv = ["pi", "--list-models", query] if harness == "pi" else ["omp", "models", query]
                    env = {"PI_OFFLINE": "1"} if harness == "pi" else None
                    return listed_models(self.probe((harness, query), lambda: run_probe(argv, env)))
                try:
                    p["model"] = resolve_listed(model, listing(model.rsplit("/", 1)[-1]), harness)
                except Problem as e:
                    if "has no model" in e.what and stem(model):
                        raise suggest(e, listing(stem(model)), model)
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
        print(f"- {p['name']} ({p['role']}, {p['harness']} {p['model'] or '?'}, thinking {p['thinking']}):")
        print(f"    problem: {e.what}")
        print(f"    fix:     {e.fix}")
    print("\nFix these, or change the member in the team config, then run again.")


# ================================================================ briefs

def tag(name):
    return f"[from @{name}]"


def member_prompt(member, body, lead, cfg):
    """System prompt for one member: who it is, its role rules, and the protocol. The roster lives in the briefing."""
    name, role = member["name"], member["role"]
    lines = [
        f"You are @{name}, the {role} on a team of coding agents. Each agent runs in its own herdr pane in the"
        f" same repo. The lead (orchestrator and CTO) is @{lead}; the human speaks through @{lead}.",
        f"A team briefing from @{lead} lists everyone and how we work; the project brief follows it.",
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


def repo_line(cwd):
    try:
        url = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        url = ""
    url = re.sub(r"^(git@|https://)([^:/]+)[:/](.+?)(\.git)?$", r"\2/\3", url)
    return f"`{cwd}`" + (f" ({url})" if url else "")


def team_briefing(cfg, members, lead, cwd):
    """The shared briefing: roster, role details, workflow, and how to talk. No project details."""
    by_role = collections.defaultdict(list)
    for m in members:
        by_role[m["role"]].append(f"@{m['name']}")
    names = {m["name"] for m in members}

    def fill(text):
        def sub(mt):
            key = mt.group(1)
            if key == "lead":
                return f"@{lead}"
            if key == "board":
                return cfg.board
            if key in names:
                return f"@{key}"
            if by_role.get(key):
                return " and ".join(by_role[key])
            raise KeyError(key)
        return re.sub(r"\{([a-z][a-z0-9_-]*)\}", sub, text)

    L = cfg.lead
    out = [f"# Team briefing (from @{lead})", "",
           f"We are a team of {len(members) + 1} agents, each in its own herdr pane, all working in the same repo:"
           f" {repo_line(cwd)}.", "", "## Roles", "", "| Name | Agent | Role |", "|---|---|---|"]
    lead_agent = ", ".join(x for x in (LABEL.get(L.get("harness"), L.get("harness")), L.get("model"),
                                       L.get("thinking")) if x)
    out.append(f"| **@{lead}** | {lead_agent} | {fill(L.get('notes') or 'Orchestrator and lead.')} |")
    for m in members:
        meta, _ = cfg.roles[m["role"]]
        agent = f"{LABEL[m['harness']]}, {m['model']}, {m['thinking']}"
        out.append(f"| **@{m['name']}** | {agent} | {fill(m['notes']) if m['notes'] else meta['description']} |")

    details = []
    for m in members:
        pals = [f"@{w}" for w in m["works_with"] if w in names]
        if pals:
            details.append(f"- **@{m['name']}** works closely with {' and '.join(pals)}: agree on shared interfaces"
                           " before building against them, and keep each other posted when they change.")
    if "pm" in by_role:
        details.append(f"- **{' and '.join(by_role['pm'])}** owns `{cfg.board}`: short, current, ordered by priority,"
                       " with an owner and status on every item. Teammates ask @pm to add or update items.")
    for role in ("reviewer", "designer", "architect", "product"):
        if role in by_role and cfg.roles[role][0]["readonly"] == "true":
            details.append(f"- **{' and '.join(by_role[role])}** ({role}) is read-only: reports findings, the"
                           " developers fix.")
    if details:
        out += ["", "## Role details", ""] + details

    steps = []
    for step in cfg.team.get("workflow", []):
        try:
            steps.append(fill(step))
        except KeyError:
            continue  # names a role nobody on the team has
    if cfg.team.get("lead_commits", True):
        steps.append(f"Nobody but @{lead} runs `git commit` or `git push`.")
    if steps:
        out += ["", "## Workflow", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]

    names_list = ", ".join("`" + m["name"] + "`" for m in members)
    out += ["", "## Talking to each other", "",
            "- Refer to teammates by @name, and lean on them when it helps.",
            '- Message a teammate: `herdr agent prompt <name> "[from @you] your message"`',
            f"  Names: {names_list}, and `{lead}` for the lead."
            " Start every message with your `[from @you]` tag. Keep it short and specific.",
            f"- Report to @{lead} by message when you finish, are blocked, or need a decision.",
            f"- Scope and architecture questions go to @{lead}; do not settle them between yourselves.",
            "", "That is the whole team briefing. Reply only: ready. The project brief comes next."]
    return "\n".join(out) + "\n"


def launch_spec(m, brief_path, brief_text, extra):
    """-> (native argv after `--`, pane env, send the member prompt as first message?)"""
    h, model, thinking, ro = m["harness"], m["model"], m["thinking"], m["readonly"]
    if h == "claude":
        argv = ["--model", model, "--effort", thinking, "--append-system-prompt-file", str(brief_path), "--name", m["name"]]
        if ro:
            argv += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
        return argv + extra, {}, False
    if h == "pi":
        argv = ["--model", model, "--thinking", thinking, "--append-system-prompt", str(brief_path), "--name", m["name"]]
        if ro:
            argv += ["--exclude-tools", "edit,write"]
        return argv + extra, {}, False
    if h == "omp":  # omp has no tool denylist; read-only relies on the prompt
        return ["--model", model, "--thinking", thinking, "--append-system-prompt", str(brief_path)] + extra, {}, False
    if h == "codex":
        argv = ["-m", model, "-c", f"model_reasoning_effort={json.dumps(thinking)}",
                "-c", f"developer_instructions={json.dumps(brief_text, ensure_ascii=False)}",
                "-s", "read-only" if ro else "workspace-write"]
        return argv + extra, {}, False
    # opencode v2: a primary agent per member via inline config. Its `system` key would replace
    # OpenCode's base prompt, so the member prompt goes in as the first message instead.
    agent = {"mode": "primary", "description": f"{m['role']} on the agent team",
             "model": model + (f"#{thinking}" if thinking and thinking != "default" else "")}
    if ro:
        agent["permissions"] = [{"action": "edit", "resource": "*", "effect": "deny"}]
    content = {"default_agent": m["name"], "agents": {m["name"]: agent}}
    return list(extra), {"OPENCODE_CONFIG_CONTENT": json.dumps(content)}, True


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
        raise SystemExit(f"herdr sees no agent in this pane ({pane}). Run team.py from the lead agent's pane.")
    name = info.get("name")
    if not name:
        taken = {a.get("name") for a in live_agents()}
        name = unique(cfg.lead["name"], taken)
        herdr("agent", "rename", pane, name)
        print(f"named this pane's agent `{name}`")
    state["lead"] = {"name": name, "pane_id": pane, "kind": info.get("agent")}
    state["agents"].setdefault(name, {"role": "lead", "kind": info.get("agent"), "pane_id": pane})
    return name


def column_target(state):
    """First teammate goes right of the lead; later ones stack under the last teammate."""
    col = state.get("column")
    if col and "result" in herdr("pane", "get", col, check=False):
        return col, "down"
    return state["lead"]["pane_id"], "right"


# ================================================================ state

def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local/state")
    return pathlib.Path(os.environ.get("HERDR_TEAM_STATE", pathlib.Path(base) / "herdr-team"))


def state_path(cwd):
    return state_dir() / (re.sub(r"[^A-Za-z0-9]", "-", str(cwd)).strip("-") + ".json")


def load_state(cwd):
    p = state_path(cwd)
    return json.loads(p.read_text()) if p.exists() else {"cwd": str(cwd), "agents": {}}


def save_state(cwd, state):
    p = state_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2) + "\n")


def sync_sessions(state, agents):
    """Remember each team agent's session refs so its cost stays visible after it exits."""
    by_pane = {a.get("pane_id"): name for name, a in state["agents"].items()}
    for a in agents:
        name, ref = by_pane.get(a.get("pane_id")), a.get("agent_session")
        if name and ref:  # other herdr agents are not on this team
            seen = state["agents"][name].setdefault("sessions", [])
            if ref not in seen:  # a restart in the same pane adds a session; keep counting the old one
                seen.append(ref)


def team_members(state, live_names=None):
    """Members (not the lead) recorded in state, optionally only those still live."""
    return [normalize(dict(a, name=n)) for n, a in state["agents"].items()
            if a.get("role") != "lead" and (live_names is None or n in live_names)]


# ================================================================ commands

def cwd_of(args):
    return pathlib.Path(args.cwd or os.getcwd()).resolve()


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
            raise SystemExit(f"no team {team!r}; teams: {sorted(cfg.team.get('teams', {}))}")
        names += cfg.team["teams"][team]
    if len(names) > 1 and any(v is not None for v in overrides.values()):
        raise SystemExit("--harness/--model/--tier/--thinking apply to one member; name exactly one")
    return [plan(cfg, n, overrides) for n in names]


def print_plan(plans, extra_col=None):
    rows = [("Member", "Role", "Harness", "Model", "Thinking") + ((extra_col[0],) if extra_col else ())]
    for p in plans:
        rows.append((p["name"], p["role"], p["harness"], p["model"] or "?", p["thinking"])
                    + ((extra_col[1](p),) if extra_col else ()))
    print_table(rows)


def cmd_config(args):
    cfg = Config(cwd_of(args), args.config)
    print("config:", " < ".join(str(f) for f in cfg.files))
    L = cfg.lead
    print(f"lead (you): @{L['name']} on {L.get('harness', '?')} {L.get('model', '?')}, thinking {L.get('thinking', '?')}"
          f"   board: {cfg.board}\n")
    names = cfg.team["teams"][args.team] if args.team else list(cfg.members)
    print_plan([plan(cfg, n) for n in names])
    print("\nteams:")
    for k, v in cfg.team.get("teams", {}).items():
        print(f"  {k}: {' '.join(v)}")


def cmd_check(args):
    cfg = Config(cwd_of(args), args.config)
    plans = selected(cfg, args)
    ready, problems = check_all(cfg, plans, args.trust_models)
    ok = {p["name"]: p for p in ready}
    print_plan([ok.get(p["name"], p) for p in plans], ("Check", lambda p: "ok" if p["name"] in ok else "FAILED"))
    if problems:
        report_problems(problems)
        sys.exit(2)


def cmd_up(args):
    cwd = cwd_of(args)
    cfg = Config(cwd, args.config)
    plans = selected(cfg, args)
    if args.name:
        if len(plans) != 1:
            raise SystemExit("--name applies to one member")
        plans[0]["name"] = args.name
    for p in plans:
        if not NAME_RE.match(p["name"]):
            raise SystemExit(f"agent name {p['name']!r} must match {NAME_RE.pattern}")

    # Every member must pass before anything starts.
    ready, problems = check_all(cfg, plans, args.trust_models)
    if problems:
        print_plan(plans)
        report_problems(problems)
        sys.exit(2)

    if args.dry_run:
        state, lead, live = load_state(cwd), cfg.lead["name"], set()
    else:
        require_herdr()
        state = load_state(cwd)
        lead = ensure_lead(state, cfg)
        live = {a.get("name") for a in live_agents()}
    clash = [m["name"] for m in ready if m["name"] in live]
    if clash:
        raise SystemExit(f"already running: {', '.join(clash)}. Use --name for another instance.")

    briefs = state_dir() / "briefs"
    for m in ready:
        _, body = cfg.roles[m["role"]]
        text = member_prompt(m, body, lead, cfg)
        bpath = briefs / f"{state_path(cwd).stem}--{m['name']}.md"
        argv, env, first_prompt = launch_spec(m, bpath, text, args.extra)
        where = f"{m['harness']} {m['model']}, thinking {m['thinking']}"
        if args.dry_run:
            print(f"would start @{m['name']} ({m['role']}) on {where}")
            print("  herdr agent start", m["name"], "--kind", m["harness"], "--pane <new> --",
                  " ".join(a if len(a) < 80 else a[:77] + "..." for a in argv))
            continue

        bpath.parent.mkdir(parents=True, exist_ok=True)
        bpath.write_text(text)
        target, direction = column_target(state)
        split = ["pane", "split", target, "--direction", direction, "--cwd", str(cwd), "--no-focus"]
        for k, v in env.items():
            split += ["--env", f"{k}={v}"]
        pane = herdr(*split)["result"]["pane"]["pane_id"]
        state["column"] = pane
        state["agents"][m["name"]] = {
            "role": m["role"], "kind": m["harness"], "model": m["model"], "thinking": m["thinking"],
            "notes": m["notes"], "works_with": m["works_with"], "readonly": m["readonly"],
            "pane_id": pane, "started": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        save_state(cwd, state)
        started = herdr("agent", "start", m["name"], "--kind", m["harness"], "--pane", pane,
                        "--timeout", str(args.timeout), "--", *argv, check=False)
        if "result" not in started:
            print(f"@{m['name']} did not come up in pane {pane}. Stopping; the remaining members were not started.")
            print(f"Look at it with: herdr pane read {pane} --source recent-unwrapped --lines 60")
            sys.exit(3)
        if first_prompt:
            herdr("agent", "prompt", m["name"], f"{tag(lead)} {text}\nReply only: ready.", check=False)
        print(f"started @{m['name']} ({m['role']}) on {where} in {pane}")
    if args.dry_run:
        return
    if args.no_briefing:
        print("next: team.py briefing --send, then the project brief with team.py say all --file <brief.md>")
        return
    send_briefing(cfg, state, cwd, lead)
    print("next: send the project brief with: team.py say all --file <brief.md>")


def send_briefing(cfg, state, cwd, lead):
    live = {a.get("name") for a in live_agents()}
    members = team_members(state, live)
    text = team_briefing(cfg, members, lead, cwd)
    path = state_dir() / "briefs" / f"{state_path(cwd).stem}--team-briefing.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    for m in members:
        herdr("agent", "prompt", m["name"], text, check=False)
    print(f"sent the team briefing to {', '.join('@' + m['name'] for m in members)} ({path})")


def cmd_briefing(args):
    cwd = cwd_of(args)
    cfg = Config(cwd, args.config)
    state = load_state(cwd)
    if args.send:
        require_herdr()
        lead = ensure_lead(state, cfg)
        save_state(cwd, state)
        send_briefing(cfg, state, cwd, lead)
        return
    # Preview: the members recorded for this repo, or else the team that `up` would launch.
    members = team_members(state)
    if not members or args.team:
        team = args.team or "default"
        if team not in cfg.team.get("teams", {}):
            raise SystemExit(f"no team {team!r}; teams: {sorted(cfg.team.get('teams', {}))}")
        members = [plan(cfg, n) for n in cfg.team["teams"][team]]
    lead = (state.get("lead") or {}).get("name") or cfg.lead["name"]
    print(team_briefing(cfg, members, lead, cwd))


def normalize(m):
    """A state record in the shape of a member plan."""
    m = dict(m)
    m["harness"] = m.get("harness") or m.get("kind")
    m.setdefault("notes", "")
    m.setdefault("works_with", [])
    return m


def cmd_say(args):
    """Message members as the lead: `say all ...` or `say lead-dev frontend-dev ...`."""
    require_herdr()
    cwd = cwd_of(args)
    cfg = Config(cwd, args.config)
    state = load_state(cwd)
    lead = ensure_lead(state, cfg)
    save_state(cwd, state)
    text = pathlib.Path(args.file).read_text() if args.file else args.message
    if not text:
        raise SystemExit("give a message or --file")
    live = {a.get("name") for a in live_agents()}
    names = [m["name"] for m in team_members(state, live)] if args.to == ["all"] else args.to
    missing = [n for n in names if n not in live]
    if missing:
        raise SystemExit(f"not running: {', '.join(missing)}")
    for n in names:
        herdr("agent", "prompt", n, f"{tag(lead)} {text}", check=False)
    print(f"sent to {', '.join('@' + n for n in names)}")


def cmd_roster(args):
    cwd = cwd_of(args)
    state = load_state(cwd)
    live = {a.get("pane_id"): a for a in live_agents()}
    sync_sessions(state, live.values())
    save_state(cwd, state)
    rows = [("Agent", "Role", "Harness", "Model", "Thinking", "Status", "Pane")]
    for name, a in state["agents"].items():
        la = live.get(a.get("pane_id"))
        status = la.get("agent_status", "?") if la and la.get("name") == name else "gone"
        rows.append((name, a.get("role", ""), a.get("kind") or "", a.get("model") or "", a.get("thinking") or "",
                     status, a.get("pane_id", "")))
    print_table(rows)


def cmd_brief(args):
    cfg = Config(cwd_of(args), args.config)
    p = plan(cfg, args.name)
    _, body = cfg.roles[p["role"]]
    print(f"# @{p['name']} ({p['role']}) on {p['harness']} {p['model']}, thinking {p['thinking']}\n")
    print(member_prompt(p, body, args.lead or cfg.lead["name"], cfg))


def cmd_init(args):
    dest = cwd_of(args) / "agent-team.toml"
    if dest.exists():
        raise SystemExit(f"{dest} exists; edit it, or remove it first")
    dest.write_text((SKILL / "team.toml").read_text())
    print(f"wrote {dest}; edit the members and teams, then run `team.py check`")


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
    cwd = cwd_of(args)
    prices = Config(cwd, args.config).models.get("prices", {})
    home = pathlib.Path(args.home).expanduser()
    state = load_state(cwd)
    live = []
    if not args.offline:
        live = live_agents()
        sync_sessions(state, live)
        save_state(cwd, state)
    status = {a.get("name"): a.get("agent_status") for a in live}

    report = []
    for name, a in state["agents"].items():
        entry = {"agent": name, "role": a.get("role", ""), "harness": a.get("kind"), "status": status.get(name, "gone"),
                 "session_files": [], "models": {}, "cost_usd": None, "cost_source": None}
        models, dollars, sources = collections.defaultdict(collections.Counter), 0.0, set()
        for ref in a.get("sessions", []):
            kind = ref.get("agent") or a.get("kind")
            path = find_session_file(ref, home)
            if not path or kind not in PARSERS:
                continue
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
        print(json.dumps({"cwd": str(cwd), "agents": report}, indent=2))
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
        rows.append((e["agent"], e["role"], e["harness"] or "", model, fmt_tok(tin), fmt_tok(tout), cost, e["status"]))
    rows.append(("Total", "", "", "", "", "", f"${known:.2f}" + ("+?" if unknown else ""), ""))
    print_table(rows, total=True)
    notes = []
    if any(e["harness"] == "claude" and e["models"] and e["cost_source"] is None for e in report):
        notes.append("Claude Code writes dollars when a session exits or resumes; until then it shows tokens"
                     " (or add [prices] to models.toml for an estimate).")
    if any(e["harness"] == "codex" and e["models"] and e["cost_source"] is None for e in report):
        notes.append("Codex records tokens only; add its models to [prices] in models.toml for dollars.")
    if any(e["harness"] == "opencode" for e in report):
        notes.append("OpenCode keeps sessions in a database team.py does not read yet; use `opencode stats`.")
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
        sp.add_argument("--config", help="team TOML file layered on top of the others")
        sp.add_argument("--cwd")

    def selection(sp):
        sp.add_argument("names", nargs="*", help="member names (default: the `default` team)")
        sp.add_argument("--team", help="a roster from [teams]")
        sp.add_argument("--role", help="add an ad-hoc member with this role (name: first NAME, else the role)")
        sp.add_argument("--harness", choices=HARNESSES)
        sp.add_argument("--model", help="exact model id")
        sp.add_argument("--tier", choices=["top", "mid", "fast"], help="pick the model by tier from models.toml")
        sp.add_argument("--thinking", "--effort", dest="thinking", help="thinking level")
        sp.add_argument("--pick", type=int, default=0, help="Nth model of the tier list")
        sp.add_argument("--trust-models", action="store_true",
                        help="skip asking harnesses which models they have (harness and thinking checks still run)")
        common(sp)

    cf = sub.add_parser("config", help="show the formation")
    cf.add_argument("--team")
    common(cf)

    ck = sub.add_parser("check", help="verify members can launch; launches nothing")
    selection(ck)

    up = sub.add_parser("up", help="check, launch members in new herdr panes, send the team briefing",
                        epilog="Arguments after a bare -- go to every launched harness, "
                               "e.g. up frontend-dev -- --permission-mode acceptEdits")
    selection(up)
    up.add_argument("--name", help="agent name for a single member (for a second instance)")
    up.add_argument("--timeout", type=int, default=60000, help="ms to wait for each agent to be ready")
    up.add_argument("--no-briefing", action="store_true", help="do not send the team briefing afterwards")
    up.add_argument("--dry-run", action="store_true", help="check and print the launch plan only")

    bf = sub.add_parser("briefing", help="print the team briefing, or --send it to every live member")
    bf.add_argument("--send", action="store_true")
    bf.add_argument("--team", help="preview for this team when nothing is running yet")
    common(bf)

    sy = sub.add_parser("say", help="message members as the lead")
    sy.add_argument("to", nargs="+", help="member names, or `all`; the last word is the message unless --file")
    sy.add_argument("--file", help="send this file's contents (e.g. the project brief)")
    common(sy)

    ro = sub.add_parser("roster", help="team agents and their live status")
    ro.add_argument("--cwd")

    co = sub.add_parser("cost", help="tokens and dollars per agent")
    co.add_argument("--json", action="store_true")
    co.add_argument("--home", default=str(pathlib.Path.home()))
    co.add_argument("--offline", action="store_true", help="use saved session refs only; do not ask herdr")
    common(co)

    br = sub.add_parser("brief", help="print the system prompt a member gets")
    br.add_argument("name")
    br.add_argument("--lead")
    common(br)

    it = sub.add_parser("init", help="copy the default formation to ./agent-team.toml")
    it.add_argument("--cwd")

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
    {"config": cmd_config, "check": cmd_check, "up": cmd_up, "briefing": cmd_briefing, "say": cmd_say,
     "roster": cmd_roster, "cost": cmd_cost, "brief": cmd_brief, "init": cmd_init}[args.cmd](args)


if __name__ == "__main__":
    main()
