"""aish-replay — measured before/after on the owner's own model (#441, slice 1).

A scenario replays owner turns verbatim against an ISOLATED aish, N times per
arm, baseline and candidate interleaved; checks read each recorded run; the
report states raw counts. `docs/replay.md` is the area doc: why each piece is
shaped this way, and the table of what is isolated and what is still shared.

    aish-replay run <name-or-path> [--runs N] [--candidate-overlay PATH=FILE ...]
                                 [--baseline-ref REF] [--window TOKENS]
    aish-replay report <batch-dir> [--full | --json] [--window TOKENS]
    aish-replay check <session.jsonl> --scenario <name-or-path>
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import backends, files, replay_checks
from .paths import config_home, state_home
from .replay_checks import CheckFn, RunContext, RunRecord, Verdict
from .usage import LOCAL_WINDOW_TOKENS, _percentile, human

DEFAULT_MODEL = "local:mlx-community/Qwen3.6-35B-A3B-8bit"
DEFAULT_RUNS = 3
DEFAULT_TIMEOUT_S = 3600
REPORT_CAP = 2048
EVIDENCE_IN_REPORT = 160
#: What a target failure seen in each arm means — and nothing stronger.
_TARGET_FAILED = {"baseline": " (reproduced)", "candidate": " (the failure still occurs)"}
ARMS = ("baseline", "candidate")
REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = Path(__file__).resolve().parent / "replay_driver.py"
#: Never copied into a run's corpus. `tools/` is re-added per name only when a
#: scenario allowlists a plugin: a plugin can send mail, write the vault, or
#: spend money, and a replay must not reach any of it by default. `evals/` is
#: the harness's own scenarios, not something the agent reads.
CORPUS_EXCLUDED = (".git", ".ruff_cache", "tools", "allow.txt", "evals")
#: The one AISH_* variable a run inherits: where the local model server is.
PASSTHROUGH_ENV = ("AISH_LOCAL_URL",)
STILL_SHARED = (
    "still shared: the real network (search, page reads, live fetches by approved commands); "
    "the real shell — approved commands run for real in each run's cwd and can read, write or "
    "delete wherever their arguments point, under $HOME or anywhere else; the model server; "
    "the login Keychain (not moved; the run's empty secrets index looks nothing up)"
)
#: Top-level keys, each with the keys its table may hold (empty = not checked).
_SCENARIO_KEYS: dict[str, set[str]] = {
    "name": set(), "description": set(), "model": set(), "runs": set(), "timeout_s": set(),
    "max_steps": set(), "num_ctx": set(), "source": set(), "turns": set(), "env": set(),
    "corpus": {"tools"}, "cards": {"approve_commands", "approve_tools"},
    "checks": {"use", "target"}, "candidate": {"overlays"},
}


class ReplayError(Exception):
    """A scenario or invocation the harness refuses, with the reason."""


def replay_home() -> Path:
    """Batches live BESIDE the owner's state tree, never inside it."""
    return Path(os.environ.get("AISH_REPLAY_HOME") or
                Path.home() / ".local" / "state" / "aish-replay")


# --- scenario ----------------------------------------------------------------

@dataclass(frozen=True)
class Turn:
    text: str
    stand_in: bool = False
    note: str = ""


@dataclass
class Scenario:
    name: str
    path: Path
    model: str
    runs: int
    timeout_s: int
    turns: list[Turn]
    approve_commands: list[str]
    approve_tools: list[str]
    corpus_tools: list[str]
    env: dict[str, str]
    checks: dict[str, CheckFn]
    targets: list[str]
    overlays: dict[str, Path] = field(default_factory=dict)  # candidate arm only
    max_steps: int | None = None
    num_ctx: int | None = None
    source: dict = field(default_factory=dict)


def _local_checks(directory: Path) -> dict[str, CheckFn]:
    path = directory / "checks.py"
    if not path.is_file():
        return {}
    spec = importlib.util.spec_from_file_location(f"replay_checks_{directory.name}", path)
    if spec is None or spec.loader is None:
        raise ReplayError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks = getattr(module, "CHECKS", None)
    if not isinstance(checks, dict):
        raise ReplayError(f"{path} must define CHECKS = {{name: function}}")
    return dict(checks)


def unisolated_commands(scenario: Scenario) -> list[str]:
    """Approved command binaries with no `[env]` variable named after them
    (`TRIPPY_*` for `trippy`). Every non-AISH variable of the caller passes
    through, so such a command reads and writes this machine's own data — the
    report says so rather than letting the isolation table imply otherwise. A
    name match is all this checks; it cannot know what the variable does."""
    found: list[str] = []
    for prefix in scenario.approve_commands:
        words = prefix.split()
        if not words:
            continue
        stem = re.sub(r"\W", "_", Path(words[0]).name).upper() + "_"
        if not any(key.startswith(stem) for key in scenario.env) and words[0] not in found:
            found.append(words[0])
    return found


def resolve_scenario(name_or_path: str) -> Path:
    """A bare name is looked up in the owner's PRIVATE config tree first
    (`<config home>/evals/<name>`), then in the repo's `evals/`; anything with
    a slash is a path. Scenarios mined from his sessions hold his own words and
    family details, and the repo is public — so they live in the config tree,
    and the repo holds synthetic examples only."""
    if "/" not in name_or_path and name_or_path not in (".", ".."):
        for base in (config_home() / "evals", REPO_ROOT / "evals"):
            if (base / name_or_path / "scenario.toml").is_file():
                return base / name_or_path
    path = Path(name_or_path).expanduser()
    if (path / "scenario.toml").is_file():
        return path
    raise ReplayError(f"no scenario {name_or_path!r}: looked in {config_home() / 'evals'}, "
                      f"{REPO_ROOT / 'evals'} and as a path")


def load_scenario(directory: Path) -> Scenario:
    directory = Path(directory).resolve()
    path = directory / "scenario.toml"
    if not path.is_file():
        raise ReplayError(f"no scenario.toml in {directory}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    # A misspelt key would silently fall back to a default — for `cards` that
    # default is deny, but for `checks.target` it is "no target", so refuse.
    unknown = sorted(set(data) - set(_SCENARIO_KEYS))
    for table, allowed in _SCENARIO_KEYS.items():
        if allowed and isinstance(data.get(table), dict):
            unknown += [f"{table}.{k}" for k in sorted(set(data[table]) - allowed)]
    if unknown:
        raise ReplayError(f"unknown scenario keys: {', '.join(unknown)}")
    turns = [Turn(str(t["text"]), bool(t.get("stand_in")), str(t.get("note") or ""))
             for t in data.get("turns") or []]
    if not turns:
        raise ReplayError("a scenario needs at least one [[turns]] entry")
    env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    if any(k.startswith("AISH_") or k == "PYTHONPATH" for k in env):
        raise ReplayError("scenario env may not set AISH_* or PYTHONPATH — the runner owns "
                          "isolation")
    available = dict(replay_checks.LIBRARY)
    available.update(_local_checks(directory))
    checks_cfg = data.get("checks") or {}
    names = list(checks_cfg.get("use") or available)
    missing = [n for n in names if n not in available]
    if missing:
        raise ReplayError(f"unknown checks: {', '.join(missing)}")
    targets = list(checks_cfg.get("target") or [])
    if any(t not in names for t in targets):
        raise ReplayError("every target must be one of the checks in use")
    cards = data.get("cards") or {}
    overlays = {rel: directory / src
                for rel, src in ((data.get("candidate") or {}).get("overlays") or {}).items()}
    return Scenario(
        name=str(data.get("name") or directory.name), path=directory,
        model=str(data.get("model") or DEFAULT_MODEL),
        runs=int(data.get("runs") or DEFAULT_RUNS),
        timeout_s=int(data.get("timeout_s") or DEFAULT_TIMEOUT_S),
        turns=turns,
        approve_commands=[str(p) for p in cards.get("approve_commands") or []],
        approve_tools=[str(t) for t in cards.get("approve_tools") or []],
        corpus_tools=[str(t) for t in (data.get("corpus") or {}).get("tools") or []],
        env=env, checks={n: available[n] for n in names}, targets=targets,
        overlays=overlays, max_steps=data.get("max_steps"), num_ctx=data.get("num_ctx"),
        source=dict(data.get("source") or {}),
    )


# --- corpus ------------------------------------------------------------------

def _overlay_target(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if Path(rel).is_absolute() or not files.contains(root, target, strict=True):
        raise ReplayError(f"overlay path {rel!r} leaves the corpus")
    return target


def snapshot_corpus(source: Path, dest: Path, allowed_tools: Sequence[str]) -> list[str]:
    """Copy the owner's corpus ONCE per batch, minus everything excluded, so both
    arms read the same snapshot even if he edits his corpus mid-batch. Returns
    the top-level names that were left out."""
    excluded = sorted(p.name for p in source.iterdir() if p.name in CORPUS_EXCLUDED)
    shutil.copytree(source, dest, ignore=lambda d, names: (
        [n for n in names if n in CORPUS_EXCLUDED] if Path(d) == source else []))
    for tool in allowed_tools:
        if "/" in tool or tool.startswith("."):
            raise ReplayError(f"tool name {tool!r} is not a plugin directory name")
        if not (source / "tools" / tool).exists():
            raise ReplayError(f"allowlisted tool {tool!r} is not in {source / 'tools'}")
        shutil.copytree(source / "tools" / tool, dest / "tools" / tool)
    return excluded


def fresh_corpus(snapshot: Path, dest: Path, overlays: dict[str, Path],
                 allowed_tools: Sequence[str]) -> list[dict]:
    """A run's own copy of the snapshot, with this arm's overlays applied."""
    shutil.copytree(snapshot, dest)
    applied = []
    for rel, source in sorted(overlays.items()):
        target = _overlay_target(dest, rel)
        parts = Path(rel).parts
        if parts[0] == "tools" and (len(parts) < 2 or parts[1] not in allowed_tools):
            raise ReplayError(f"overlay {rel!r} is a tool the scenario does not allowlist")
        if parts[0] in CORPUS_EXCLUDED and parts[0] != "tools":
            raise ReplayError(f"overlay {rel!r} targets an excluded path")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        applied.append({"path": rel, "from": str(source), "sha256": _sha(target)})
    return applied


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# --- runner ------------------------------------------------------------------

@dataclass
class LaunchResult:
    exit_code: int | None
    timed_out: bool


Launch = Callable[[list[str], dict[str, str], Path, int, Path], LaunchResult]


def launch_subprocess(argv: list[str], env: dict[str, str], cwd: Path, timeout_s: int,
                      log: Path) -> LaunchResult:
    with log.open("w", encoding="utf-8") as out:
        try:
            done = subprocess.run(argv, env=env, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                  timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return LaunchResult(None, True)
    return LaunchResult(done.returncode, False)


def run_env(base: dict[str, str], run_dir: Path, code_root: Path,
            scenario_env: dict[str, str], arm_env: dict[str, str] | None = None,
            path_first: Sequence[str] = ()) -> dict[str, str]:
    """The run's environment: the caller's, minus every AISH_* knob, plus ours."""
    env = {k: v for k, v in base.items() if not k.startswith("AISH_") and k != "PYTHONPATH"}
    env.update({k: base[k] for k in PASSTHROUGH_ENV if k in base})
    env.update({k: v.replace("{run}", str(run_dir)) for k, v in scenario_env.items()})
    env.update({k: v.replace("{run}", str(run_dir)) for k, v in (arm_env or {}).items()})
    if path_first:
        env["PATH"] = os.pathsep.join([*path_first, env.get("PATH", "")])
    env.update({
        "AISH_CONFIG_HOME": str(run_dir / "config"),
        "AISH_STATE_DIR": str(run_dir / "state"),
        "AISH_NOTIFY": "0",
        "PYTHONPATH": str(code_root),
    })
    return env


def baseline_code(ref: str, dest: Path, repo: Path = REPO_ROOT) -> str:
    """An archive of REF, extracted — no worktree, no change to the repo's git state."""
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                         capture_output=True, text=True, check=False)
    if sha.returncode != 0:
        raise ReplayError(f"baseline ref {ref!r} does not resolve: {sha.stderr.strip()}")
    blob = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha.stdout.strip()],
                          capture_output=True, check=True).stdout
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        archive.extractall(dest, filter="data")
    return sha.stdout.strip()


def candidate_code(repo: Path = REPO_ROOT) -> str:
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=False).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True, check=False).stdout.strip()
    return f"{head or 'unknown'}{' (uncommitted changes)' if dirty else ''}"


@dataclass
class Arm:
    name: str
    code_root: Path
    code: str  # what the code is, as recorded
    overlays: dict[str, Path]
    # A tool under test is a binary on PATH, not aish code or a corpus file: an arm may
    # put its own build first on PATH and set its own variables, so a TOOL change gets
    # the same A/B as a code or skill change.
    env: dict[str, str] = field(default_factory=dict)
    path_first: tuple[str, ...] = ()


def run_batch(scenario: Scenario, *, runs: int, arms: Sequence[Arm], corpus_source: Path,
              embeddings_source: Path | None, batch_dir: Path,
              launch: Launch = launch_subprocess, environ: dict[str, str] | None = None,
              log: Callable[[str], None] = print) -> Path:
    """Run every arm `runs` times, INTERLEAVED (b1 c1 b2 c2 …), so drift on the
    live sites lands on both arms alike. Sequential: one local model."""
    environ = dict(os.environ if environ is None else environ)
    batch_dir.mkdir(parents=True)
    shutil.copytree(scenario.path, batch_dir / "scenario",
                    ignore=shutil.ignore_patterns("__pycache__"))
    snapshot = batch_dir / "corpus-snapshot"
    excluded = snapshot_corpus(corpus_source, snapshot, scenario.corpus_tools)
    embeddings = None
    if embeddings_source is not None and embeddings_source.is_file():
        embeddings = batch_dir / "embeddings.json"
        shutil.copyfile(embeddings_source, embeddings)
    (batch_dir / "batch.json").write_text(json.dumps({
        "scenario": scenario.name, "model": scenario.model, "runs": runs,
        "arms": [{"name": a.name, "code": a.code, "code_root": str(a.code_root),
                  "overlays": {k: str(v) for k, v in a.overlays.items()},
                  "env": a.env, "path_first": list(a.path_first)} for a in arms],
        "corpus_source": str(corpus_source), "corpus_excluded": excluded,
        "corpus_tools": scenario.corpus_tools,
        "embeddings": str(embeddings_source) if embeddings else None,
        "started": _now(),
    }, indent=2), encoding="utf-8")
    for n in range(1, runs + 1):
        for arm in arms:
            log(f"{arm.name} {n}/{runs} …")
            _one_run(scenario, arm, batch_dir / arm.name / str(n), snapshot, embeddings,
                     launch, environ)
    return batch_dir


def _one_run(scenario: Scenario, arm: Arm, run_dir: Path, snapshot: Path,
             embeddings: Path | None, launch: Launch, environ: dict[str, str]) -> None:
    for sub in ("state", "cwd"):
        (run_dir / sub).mkdir(parents=True)
    applied = fresh_corpus(snapshot, run_dir / "config", arm.overlays, scenario.corpus_tools)
    if embeddings is not None:
        shutil.copyfile(embeddings, run_dir / "state" / "embeddings.json")
    allow = run_dir / "allow.txt"
    allow.write_text("", encoding="utf-8")  # the card policy, not his allowlist, decides
    spec = {
        "model": scenario.model, "turns": [t.text for t in scenario.turns],
        "approve_commands": scenario.approve_commands, "approve_tools": scenario.approve_tools,
        "max_steps": scenario.max_steps, "num_ctx": scenario.num_ctx,
        "state_dir": str(run_dir / "state"), "cwd": str(run_dir / "cwd"),
        "allow_path": str(allow), "deny_path": str(run_dir / "config" / "deny.txt"),
        "lessons_path": str(run_dir / "config" / "lessons.md"),
        "config_path": str(run_dir / "config" / "config.toml"),
        "events_path": str(run_dir / "events.jsonl"),
    }
    (run_dir / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    env = run_env(environ, run_dir, arm.code_root, scenario.env, arm.env, arm.path_first)
    started = _now()
    result = launch([sys.executable, "-P", str(DRIVER), str(run_dir / "spec.json")], env,
                    run_dir / "cwd", scenario.timeout_s, run_dir / "driver.log")
    (run_dir / "manifest.json").write_text(json.dumps({
        "arm": arm.name, "code": arm.code, "code_root": str(arm.code_root),
        "overlays": applied, "started": started, "ended": _now(),
        "exit_code": result.exit_code, "timed_out": result.timed_out,
        "env_set": sorted(k for k in env if k.startswith("AISH_") or k in scenario.env
                          or k in arm.env or k == "PYTHONPATH"),
        "path_first": list(arm.path_first),
    }, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# --- reading a run back --------------------------------------------------------

@dataclass
class RunResult:
    arm: str
    n: int
    verdicts: list[Verdict]
    counts: dict[str, int | None]
    problem: str | None  # why the run cannot be read as a complete replay
    #: What each model call was handed (#444); None when there was no single
    #: session log to read it from.
    context: RunContext | None = None


def _events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_run(run_dir: Path, arm: str, n: int, checks: dict[str, CheckFn]) -> RunResult:
    manifest = _json(run_dir / "manifest.json")
    events = _events(run_dir)
    problem = None
    start = next((e for e in events if e.get("type") == "start"), None)
    code_root = Path(manifest.get("code_root") or "/nonexistent")
    if manifest.get("timed_out"):
        problem = "timed out"
    elif manifest.get("exit_code") not in (0, None) or not manifest:
        problem = f"driver exited {manifest.get('exit_code')}"
    elif start is None:
        problem = "driver never started"
    elif not files.contains(code_root, str(start.get("aish_file")), strict=True):
        problem = f"imported aish from {start.get('aish_file')}, not {code_root}"
    elif not any(e.get("type") == "end" for e in events):
        problem = "driver did not finish every turn"
    sessions = sorted((run_dir / "state").glob("session-*.jsonl"))
    if len(sessions) != 1:
        problem = problem or f"{len(sessions)} session logs in the run's state dir"
        verdicts = [Verdict(name, None, "no single session log to read") for name in checks]
        return RunResult(arm, n, verdicts, {}, problem)
    record = replay_checks.load_run(sessions[0])
    verdicts = replay_checks.apply(record, checks)
    if problem:
        # An incomplete run cannot vouch for anything: a failure it recorded is
        # real, but a pass is a pass of a run that never got to the hard part.
        verdicts = [v if v.passed is False else Verdict(v.check, None, f"run incomplete "
                    f"({problem}); {v.evidence}") for v in verdicts]
    counts = replay_checks.counts(record)
    counts["cards_raised"] = sum(1 for e in events if e.get("type") == "card")
    return RunResult(arm, n, verdicts, counts, problem, replay_checks.read_context(sessions[0]))


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_batch(batch_dir: Path) -> tuple[Scenario, dict, list[RunResult]]:
    scenario = load_scenario(batch_dir / "scenario")
    batch = _json(batch_dir / "batch.json")
    results = []
    for arm in ARMS:
        arm_dir = batch_dir / arm
        if not arm_dir.is_dir():
            continue
        for run_dir in sorted((p for p in arm_dir.iterdir() if p.name.isdigit()),
                              key=lambda p: int(p.name)):
            results.append(read_run(run_dir, arm, int(run_dir.name), scenario.checks))
    return scenario, batch, results


# --- the report ------------------------------------------------------------------

def _tally(results: list[RunResult], check: str) -> tuple[int, int, int]:
    passed = failed = unknown = 0
    for r in results:
        verdict = next(v for v in r.verdicts if v.check == check)
        if verdict.passed is True:
            passed += 1
        elif verdict.passed is False:
            failed += 1
        else:
            unknown += 1
    return passed, failed, unknown


def cell(passed: int, failed: int, unknown: int, *, target: bool, arm: str) -> str:
    """One arm's result for one check — raw counts, never a rate, never a verdict
    about the change. A failure seen is the only strong claim; an absence is
    only ever "not observed"."""
    total = passed + failed + unknown
    parts = []
    if failed:
        parts.append(f"failed {failed} of {total}"
                     + (_TARGET_FAILED[arm] if target else ""))
    elif passed:
        parts.append(f"not observed in {passed} run{'s' * (passed != 1)}" if target
                     else f"passed {passed} of {total}")
    if unknown:
        parts.append(f"could not tell in {unknown} of {total}")
    return ", ".join(parts) or "no runs"


def withheld_reason(scenario: Scenario, baseline: list[RunResult]) -> str | None:
    """Why candidate numbers must not be shown, or None when they may.

    A candidate that "passes" a check the baseline also passed says nothing
    about the change; reporting it anyway is how a fix gets credited for a
    failure that was never reproduced."""
    if not scenario.targets:
        return "scenario.toml declares no target failure ([checks] target)"
    for target in scenario.targets:
        _, failed, _ = _tally(baseline, target)
        if not failed:
            return (f"the baseline did not reproduce the target failure ({target}: not "
                    f"observed in {len(baseline)} runs)")
    return None


def context_summary(contexts: Sequence[RunContext], window: int) -> dict:
    """What the model calls of these runs were handed, pooled call by call (#444).

    Under the reader law of `docs/token-accounting.md`: a fact that was not
    recorded is COUNTED as not recorded and its median is ABSENT, never 0;
    tokens come only from the provider and chars only from the brief, and the
    two are never added or converted. Medians and p95 are `aish usage`'s own
    (`usage._percentile`), so the replay and the usage report say the same thing
    about the same calls. A pool whose counts carry more than one unit label
    gets no median and no overflow count — those would add quantities the
    provider counted differently.
    """
    calls = sum(c.calls for c in contexts)
    tokens = [t for c in contexts for t in c.prompt_tokens]
    units = sorted({u for c in contexts for u in c.token_semantics})
    out: dict = {"calls": calls}
    prompt: dict = {"recorded": len(tokens),
                    "not_recorded": sum(c.tokens_absent for c in contexts)}
    if units:
        prompt["semantics"] = units
    if len(units) == 1:
        prompt["median"] = _percentile(tokens, 0.5)
        prompt["p95"] = _percentile(tokens, 0.95)
        out["over_window"] = {"window": window, "calls": sum(t > window for t in tokens),
                              "of": len(tokens)}
    out["prompt_tokens"] = prompt
    system = [s for c in contexts for s in c.system_chars]
    out["system_chars"] = {"recorded": len(system),
                           "not_recorded": sum(c.system_absent for c in contexts),
                           **_median_max(system)}
    menu = [m for c in contexts for m in c.menu_chars]
    out["menu_chars"] = {"recorded": len(menu), "purged": sum(c.menu_purged for c in contexts),
                         "not_recorded": sum(c.menu_missing for c in contexts),
                         **_median_max(menu)}
    tools = [t for c in contexts for t in c.tools]
    out["tools"] = {"recorded": len(tools),
                    "not_recorded": sum(c.tools_absent for c in contexts),
                    **({"min": min(tools), "max": max(tools)} if tools else {})}
    return out


def _median_max(values: list[int]) -> dict:
    return {"median": _percentile(values, 0.5), "max": max(values)} if values else {}


#: `docs/token-accounting.md`: Ollama's `prompt_eval_count` leaves out the prefix
#: it served from its KV cache, so a count in that unit is a floor.
_KV_REUSE_NOTE = ("input_excludes_kv_reuse leaves out the prefix served from the KV cache: "
                  "these counts, and the over-window count, are floors")


def _context_line(label: str, summary: dict) -> list[str]:
    """One arm (or one run) on one line, because the report is capped and the
    evidence lines share the cap. Every absent figure is said in words."""
    calls = summary["calls"]
    if not calls:
        return [f"{label}: no model call recorded"]
    prompt = summary["prompt_tokens"]
    absent = prompt["not_recorded"]
    if not prompt["recorded"]:
        tokens = f"tokens not recorded on any of {calls} calls"
    elif "median" not in prompt:
        tokens = f"tokens in mixed units ({', '.join(prompt['semantics'])}): not combined"
    else:
        tokens = (f"tokens median {human(prompt['median'])} p95 {human(prompt['p95'])} "
                  f"{prompt['semantics'][0]}"
                  + (f", not recorded on {absent} of {calls}" if absent else ""))
    over = summary.get("over_window")
    if over is not None:
        overflow = f"over {over['window']:,}: {over['calls']} of {over['of']}"
    else:
        overflow = "over window: " + ("not combined" if prompt["recorded"] else "not recorded")
    cells = [f"{label}, {calls} calls", tokens, overflow]
    for name, key in (("sys", "system_chars"), ("menu", "menu_chars")):
        part = summary[key]
        gone = [f"{part[k]} {k.replace('_', ' ')}" for k in ("purged", "not_recorded")
                if part.get(k)]
        if "median" not in part:
            cells.append(f"{name} not recorded")
        else:
            cells.append(f"{name} {human(part['median'])}/{human(part['max'])}"
                         + (f" ({', '.join(gone)})" if gone else ""))
    tools = summary["tools"]
    if "min" not in tools:
        cells.append("tools not recorded")
    else:
        span = tools["min"] if tools["min"] == tools["max"] else f"{tools['min']}-{tools['max']}"
        cells.append(f"tools {span}")
    lines = [" · ".join(cells)]
    if backends.INPUT_EXCLUDES_KV_REUSE in prompt.get("semantics", []):
        lines.append(f"  {_KV_REUSE_NOTE}")
    return lines


def _context_rows(by_arm: dict[str, list[RunResult]], shown: Sequence[str], window: int,
                  *, full: bool) -> list[str]:
    """Pooled over each arm's COMPLETE runs: a run that did not finish made
    fewer calls than the scenario asks for, and folding it in would move the
    arm's figures for a reason nobody could see. --full adds every run."""
    rows = ["context per model call (complete runs pooled; sys/menu = chars median/max):"]
    for arm in shown:
        complete = [r.context for r in by_arm[arm] if not r.problem and r.context is not None]
        if not complete:
            rows.append(f"{arm}: no complete run to read")
        else:
            label = f"{arm} {len(complete)} run{'s' * (len(complete) != 1)}"
            rows += _context_line(label, context_summary(complete, window))
        if full:
            for r in by_arm[arm]:
                if r.context is None:
                    rows.append(f"  {r.arm[0]}{r.n}: no single session log to read")
                    continue
                rows += ["  " + line for line in _context_line(
                    f"{r.arm[0]}{r.n}", context_summary([r.context], window))]
    return rows


def render_report(scenario: Scenario, batch: dict, results: list[RunResult], batch_dir: Path,
                  *, full: bool = False, window: int = LOCAL_WINDOW_TOKENS) -> str:
    by_arm = {arm: [r for r in results if r.arm == arm] for arm in ARMS}
    arms = [a for a in ARMS if by_arm[a]]
    reason = withheld_reason(scenario, by_arm["baseline"]) if "candidate" in arms else None
    head = [f"replay {scenario.name} · {batch_dir.name} · model {batch.get('model')}"]
    for spec in batch.get("arms") or []:
        overlays = ", ".join(spec.get("overlays") or {}) or "no overlay"
        extra = "".join(f" · PATH first {p}" for p in spec.get("path_first") or [])
        extra += "".join(f" · {k}={v}" for k, v in (spec.get("env") or {}).items())
        head.append(f"{spec.get('name')}: code {str(spec.get('code'))[:60]} · {overlays}{extra}")
    done = " · ".join(f"{a} {sum(1 for r in by_arm[a] if not r.problem)} of {len(by_arm[a])} "
                      "complete" for a in arms)
    head.append(f"runs (interleaved): {done}")
    head.append(STILL_SHARED)
    if loose := unisolated_commands(scenario):
        head.append(f"no [env] row isolates approved command(s) {', '.join(loose)}: they use "
                    "this machine's own data")
    if reason:
        head.append(f"candidate numbers withheld: {reason}")

    shown = ["baseline"] if reason else arms
    width = max(len(n) for n in scenario.checks)
    rows = []
    for name in scenario.checks:
        target = name in scenario.targets
        cells = [f"{a} {cell(*_tally(by_arm[a], name), target=target, arm=a)}" for a in shown]
        rows.append(f"{name.ljust(width)}{' *' if target else '  '} " + " · ".join(cells))
    rows.append("(* = target failure)" if scenario.targets else "")
    for key in ("model_calls", "tool_calls", "cards_raised", "wall_s"):
        rows.append(f"{key}: " + " · ".join(
            f"{a} " + ",".join("?" if r.counts.get(key) is None else str(r.counts[key])
                               for r in by_arm[a]) for a in shown))
    problems = [f"{r.arm[0]}{r.n}: {r.problem}" for r in results if r.problem
                and r.arm in shown]
    if problems:
        rows.append("incomplete: " + "; ".join(problems))
    rows += [""] + _context_rows(by_arm, shown, window, full=full)

    body = "\n".join(head + [""] + rows)
    if full:
        evidence = [f"{r.arm[0]}{r.n} {v.check} ({v.label}): {v.evidence}" for r in results
                    if r.arm in shown for v in r.verdicts]
        return body + "\n" + "\n".join(["", "evidence:"] + evidence) + "\n"
    # One line per (check, arm) that failed: which runs, and the first run's
    # evidence — target checks first, because they are what the batch is for.
    evidence = []
    ordered = sorted(scenario.checks, key=lambda name: name not in scenario.targets)
    for name in ordered:
        for arm in shown:
            failing = [(r, v) for r in by_arm[arm] for v in r.verdicts
                       if v.check == name and v.passed is False]
            if failing:
                runs = ",".join(f"{r.arm[0]}{r.n}" for r, _ in failing)
                first = failing[0][1].evidence[:EVIDENCE_IN_REPORT]
                evidence.append(f"{name} [{runs}] {first}")
    lines: list[str] = []
    # Room for the omission note at its longest, so the note itself is never cut.
    reserve = len(_omitted_note(len(evidence), batch_dir)) + 2
    for line in evidence:
        candidate = "\n".join([body, "", "evidence (first failures):"] + lines + [line])
        if len(candidate) + reserve > REPORT_CAP:
            break
        lines.append(line)
    tail = ["", "evidence (first failures):"] + lines if lines else []
    omitted = len(evidence) - len(lines)
    if omitted:
        note = _omitted_note(omitted, batch_dir)
        if len("\n".join([body] + tail + [note])) >= REPORT_CAP:
            note = _omitted_note(omitted, None)  # the pointer whole, without the path
        tail.append(note)
    return "\n".join([body] + tail)[:REPORT_CAP - 1] + "\n"


def _omitted_note(omitted: int, batch_dir: Path | None) -> str:
    where = f"aish-replay report {batch_dir} --full" if batch_dir else "report --full"
    return f"({omitted} more evidence lines: {where})"


def json_report(scenario: Scenario, batch: dict, results: list[RunResult], batch_dir: Path,
                *, window: int = LOCAL_WINDOW_TOKENS) -> dict:
    """The same report for anything that is not a terminal, every run whole.

    It withholds exactly what the text withholds (R4): no candidate key at all
    when the baseline did not reproduce the target. A figure that could not be
    taken is an absent key or a `not_recorded` count, never a 0."""
    by_arm = {arm: [r for r in results if r.arm == arm] for arm in ARMS}
    arms = [a for a in ARMS if by_arm[a]]
    reason = withheld_reason(scenario, by_arm["baseline"]) if "candidate" in arms else None
    shown = ["baseline"] if reason else arms
    out: dict = {"scenario": scenario.name, "batch": str(batch_dir), "model": batch.get("model"),
                 "window": window, "targets": scenario.targets, "withheld": reason, "arms": {}}
    for arm in shown:
        complete = [r.context for r in by_arm[arm] if not r.problem and r.context is not None]
        out["arms"][arm] = {
            "checks": {name: dict(zip(("passed", "failed", "could_not_tell"),
                                      _tally(by_arm[arm], name), strict=True))
                       for name in scenario.checks},
            "complete_runs": len(complete),
            # Pooled over complete runs only, as in the text (`_context_rows`).
            **({"context": context_summary(complete, window)} if complete else {}),
            "runs": [{
                "n": r.n, "problem": r.problem, "counts": r.counts,
                "verdicts": [{"check": v.check, "passed": v.passed, "evidence": v.evidence}
                             for v in r.verdicts],
                **({"context": context_summary([r.context], window)}
                   if r.context is not None else {}),
            } for r in by_arm[arm]],
        }
    return out


# --- CLI ---------------------------------------------------------------------------

def _parse_overlays(pairs: Sequence[str]) -> dict[str, Path]:
    out = {}
    for pair in pairs:
        rel, sep, source = pair.partition("=")
        if not sep or not rel or not source:
            raise ReplayError(f"--candidate-overlay wants PATH=FILE, got {pair!r}")
        if not Path(source).is_file():
            raise ReplayError(f"overlay file {source!r} does not exist")
        out[rel] = Path(source).resolve()
    return out


def _parse_arm_env(pairs: Sequence[str], flag: str) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ReplayError(f"{flag} wants KEY=VALUE, got {pair!r}")
        if key.startswith("AISH_") or key in ("PYTHONPATH", "PATH"):
            raise ReplayError(f"{flag} may not set {key} — the runner owns it "
                              "(use --baseline-path/--candidate-path for PATH)")
        out[key] = value
    return out


def _parse_path_first(dirs: Sequence[str], flag: str) -> tuple[str, ...]:
    for d in dirs:
        if not Path(d).is_dir():
            raise ReplayError(f"{flag} {d!r} is not a directory")
    return tuple(str(Path(d).resolve()) for d in dirs)


def cmd_run(args: argparse.Namespace) -> int:
    scenario = load_scenario(resolve_scenario(args.scenario))
    for binary in unisolated_commands(scenario):
        print(f"aish-replay: warning: no [env] row isolates approved command {binary!r}; "
              "it will use this machine's own data", file=sys.stderr)
    overlays = {**scenario.overlays, **_parse_overlays(args.candidate_overlay or [])}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_dir = replay_home() / scenario.name / stamp
    candidate = Arm("candidate", REPO_ROOT, f"this tree {candidate_code()}", overlays,
                    _parse_arm_env(args.candidate_env or [], "--candidate-env"),
                    _parse_path_first(args.candidate_path or [], "--candidate-path"))
    base_env = _parse_arm_env(args.baseline_env or [], "--baseline-env")
    base_path = _parse_path_first(args.baseline_path or [], "--baseline-path")
    tool_differs = (candidate.env, candidate.path_first) != (base_env, base_path)
    base_root = batch_dir.parent / f".{stamp}-baseline-code"
    sha = baseline_code(args.baseline_ref, base_root)
    if (sha == candidate_code().split()[0] and not overlays and not tool_differs
            and "uncommitted" not in candidate.code):
        shutil.rmtree(base_root)
        raise ReplayError("baseline and candidate would run identical code and corpus")
    baseline = Arm("baseline", base_root, f"{args.baseline_ref} {sha[:12]}", {}, base_env,
                   base_path)
    try:
        run_batch(scenario, runs=args.runs or scenario.runs, arms=[baseline, candidate],
                  corpus_source=config_home(), embeddings_source=state_home() / "embeddings.json",
                  batch_dir=batch_dir)
    finally:
        shutil.rmtree(base_root, ignore_errors=True)
    print(render_report(*read_batch(batch_dir), batch_dir, window=args.window), end="")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    batch_dir = Path(args.batch_dir)
    if args.json:
        print(json.dumps(json_report(*read_batch(batch_dir), batch_dir, window=args.window),
                         indent=2))
        return 0
    print(render_report(*read_batch(batch_dir), batch_dir, full=args.full, window=args.window),
          end="")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    scenario = load_scenario(resolve_scenario(args.scenario))
    record: RunRecord = replay_checks.load_run(Path(args.session))
    for verdict in replay_checks.apply(record, scenario.checks):
        print(f"{verdict.label:>14}  {verdict.check}: {verdict.evidence}")
    counts = replay_checks.counts(record)
    print("counts: " + ", ".join(f"{k} {'?' if v is None else v}" for k, v in counts.items()))
    return 0


_WINDOW_HELP = ("count model calls whose RECORDED prompt tokens exceed this (default "
                f"{LOCAL_WINDOW_TOKENS:,}, `aish usage`'s: a default, not a fact about any "
                "backend)")


def _window(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("the window is a positive number of tokens")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aish-replay", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="replay a scenario: baseline and candidate, interleaved")
    run.add_argument("scenario")
    run.add_argument("--runs", type=int)
    run.add_argument("--candidate-overlay", action="append", metavar="PATH=FILE",
                     help="replace PATH (relative to the corpus) with FILE in the candidate arm")
    run.add_argument("--baseline-ref", default="main")
    for arm in ("baseline", "candidate"):
        run.add_argument(f"--{arm}-env", action="append", metavar="KEY=VALUE",
                         help=f"set a variable in the {arm} arm only ({{run}} = the run dir)")
        run.add_argument(f"--{arm}-path", action="append", metavar="DIR",
                         help=f"put DIR first on PATH in the {arm} arm only — e.g. a "
                              "directory holding the tool build under test")
    run.add_argument("--window", type=_window, default=LOCAL_WINDOW_TOKENS, metavar="TOKENS",
                     help=_WINDOW_HELP)
    run.set_defaults(func=cmd_run)
    report = sub.add_parser("report", help="the report for one batch directory")
    report.add_argument("batch_dir")
    report.add_argument("--full", action="store_true")
    report.add_argument("--json", action="store_true", help="the report as JSON, every run whole")
    report.add_argument("--window", type=_window, default=LOCAL_WINDOW_TOKENS, metavar="TOKENS",
                        help=_WINDOW_HELP)
    report.set_defaults(func=cmd_report)
    check = sub.add_parser("check", help="apply a scenario's checks to an existing session log")
    check.add_argument("session")
    check.add_argument("--scenario", required=True)
    check.set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ReplayError as exc:
        print(f"aish-replay: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
