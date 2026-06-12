"""Command-line entry point — the deterministic steps the agent invokes (DESIGN §6.5).

This is intentionally **not** an orchestrator. There is no ``run`` subcommand that
loops; looping is the agent in the Claude Code session following the skill. The CLI
exposes only deterministic, side-effecting-but-append-only steps:

    meta-research eval <design> [--commit] [--run-dir DIR] [--hypothesis FILE]
    meta-research seed [--commit] [--run-dir DIR]
    meta-research frontier [--run-dir DIR]
    meta-research kg [--run-dir DIR]
    meta-research progress [--out DIR] [--run-dir DIR]
    meta-research init <name> [--from EXAMPLE] [--run-dir DIR]

The experiment is discovered from the working directory (autoresearch style):
``./prepare.py`` is loaded via :mod:`importlib`, its ``DESIGNS_DIR`` is resolved
relative to ``prepare.py`` and added to ``sys.path`` so candidate modules import.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from . import logfmt
from .interfaces import EvalResult, Objective
from .runner import evaluate_and_record, seed_baselines

# Module name under which the discovered prepare.py is registered.
_PREPARE_MODULE_NAME = "meta_research_experiment_prepare"


def _err(message: str) -> None:
    """Print an error to stderr with color when attached to a TTY."""
    print(logfmt.red(f"error: {message}"), file=sys.stderr)


def load_experiment(run_dir: Path) -> ModuleType:
    """Load ``<run_dir>/prepare.py`` and wire up ``DESIGNS_DIR`` on ``sys.path``.

    Resolves ``DESIGNS_DIR`` relative to the location of ``prepare.py`` (not the
    process cwd) and prepends both the run dir and the designs' parent to
    ``sys.path`` so candidate modules can be imported by bare name.

    Raises:
        FileNotFoundError: if ``prepare.py`` is missing.
        ImportError: if the module cannot be imported.
        AttributeError: if required symbols are absent.
    """
    run_dir = run_dir.resolve()
    prepare_path = run_dir / "prepare.py"
    if not prepare_path.is_file():
        raise FileNotFoundError(
            f"no prepare.py found in {run_dir} — run inside an experiment dir "
            f"or pass --run-dir (try `meta-research init <name>`)"
        )

    spec = importlib.util.spec_from_file_location(_PREPARE_MODULE_NAME, prepare_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load module spec from {prepare_path}")

    # Ensure the run dir is importable before executing prepare.py
    # (prepare.py / make_evaluator may import from designs at module load time).
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(run_dir))
    sys.modules[_PREPARE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_PREPARE_MODULE_NAME, None)
        raise

    designs_raw = getattr(module, "DESIGNS_DIR", "designs") or "designs"
    designs_path = Path(designs_raw)
    if not designs_path.is_absolute():
        designs_path = run_dir / designs_path
    # Put the designs' parent on the path so the package imports as `<dir>.<mod>`,
    # and the designs dir itself so a bare-module layout also imports.
    for p in (str(designs_path.parent), str(designs_path)):
        if p not in sys.path:
            sys.path.insert(0, p)

    _validate_experiment(module)
    return module


def _validate_experiment(module: ModuleType) -> None:
    """Fail fast with a clear message if prepare.py is missing required symbols (DESIGN §4)."""
    required = ("OBJECTIVES", "BASELINES", "make_evaluator")
    missing = [s for s in required if not hasattr(module, s)]
    if missing:
        raise AttributeError(
            f"prepare.py is missing required symbol(s): {', '.join(missing)} "
            f"(see DESIGN §4: OBJECTIVES, BUDGET, OPERATING, BASELINES, "
            f"DESIGNS_DIR, make_evaluator)"
        )
    objectives = getattr(module, "OBJECTIVES")
    if not isinstance(objectives, list) or not all(
        isinstance(o, Objective) for o in objectives
    ):
        raise AttributeError("prepare.OBJECTIVES must be a list[Objective]")
    if not callable(getattr(module, "make_evaluator")):
        raise AttributeError("prepare.make_evaluator must be callable")


def _load_hypothesis(path: str | None) -> dict[str, Any] | None:
    """Load the optional hypothesis hand-off JSON (DESIGN §7.3); validate it is an object."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"hypothesis file not found: {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"hypothesis file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"hypothesis file {path} must contain a JSON object")
    return data


def _objective_value(result: EvalResult, obj: Objective) -> str:
    value = result.scores.get(obj.name)
    if value is None:
        return "NA"
    unit = f" {obj.unit}" if obj.unit else ""
    return f"{value:g}{unit}"


def _is_on_frontier(run_dir: Path, name: str) -> bool:
    """Read back frontier.json and report whether ``name`` is currently a member."""
    fpath = run_dir / "frontier.json"
    if not fpath.is_file():
        return False
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return any(row.get("name") == name for row in data.get("pareto", []))


def _print_eval_report(
    name: str,
    result: EvalResult,
    objectives: list[Objective],
    run_dir: Path,
) -> None:
    """Print scores + heatmap path + Pareto verdict for a single eval (DESIGN §6.5)."""
    print(logfmt.bold(f"== {name} =="))
    if result.error is not None:
        print(logfmt.red(f"  crashed: {result.error}"))
    elif not result.ok:
        print(logfmt.red("  non-finite scores"))

    feas = logfmt.green("feasible") if result.feasible else logfmt.yellow("INFEASIBLE")
    print(f"  status: {feas}")
    for obj in objectives:
        arrow = "↓" if obj.direction == "min" else "↑"
        print(f"  {obj.name} ({arrow}): {logfmt.cyan(_objective_value(result, obj))}")

    heatmap = result.artifacts.get("heatmap")
    if heatmap:
        hp = Path(heatmap)
        if not hp.is_absolute():
            hp = (run_dir / hp).resolve()
        print(f"  heatmap: {logfmt.cyan(str(hp))}")

    on_frontier = (
        result.feasible
        and result.ok
        and _is_on_frontier(run_dir, name)
    )
    if on_frontier:
        print(logfmt.green("  -> extends the Pareto frontier"))
    elif result.feasible and result.ok:
        print(logfmt.dim("  -> dominated (recorded as experience)"))
    else:
        print(logfmt.dim("  -> not on frontier (recorded as experience)"))


def cmd_eval(args: argparse.Namespace) -> int:
    """`meta-research eval <design>` — score one design, store it, print the verdict."""
    run_dir = Path(args.run_dir).resolve()
    try:
        experiment = load_experiment(run_dir)
        hypothesis = _load_hypothesis(args.hypothesis)
    except (FileNotFoundError, ImportError, AttributeError, ValueError) as exc:
        _err(str(exc))
        return 2

    objectives = list(experiment.OBJECTIVES)
    result = evaluate_and_record(
        args.design,
        experiment,
        run_dir,
        hypothesis=hypothesis,
        commit=args.commit,
        tag=args.tag,
    )
    _print_eval_report(args.design, result, objectives, run_dir)
    # A crash is still recorded; report non-zero so callers/CI notice.
    return 0 if (result.feasible and result.ok) else 1


def cmd_seed(args: argparse.Namespace) -> int:
    """`meta-research seed` — evaluate every baseline once (Phase 0)."""
    run_dir = Path(args.run_dir).resolve()
    try:
        experiment = load_experiment(run_dir)
    except (FileNotFoundError, ImportError, AttributeError, ValueError) as exc:
        _err(str(exc))
        return 2

    baselines = list(getattr(experiment, "BASELINES", []))
    if not baselines:
        _err("prepare.BASELINES is empty — nothing to seed")
        return 2

    objectives = list(experiment.OBJECTIVES)
    print(logfmt.bold(f"seeding {len(baselines)} baseline(s)..."))
    results = seed_baselines(experiment, run_dir, commit=args.commit)
    failures = 0
    for name, result in zip(baselines, results):
        _print_eval_report(name, result, objectives, run_dir)
        if not (result.feasible and result.ok):
            failures += 1
    print(logfmt.bold(f"seeded {len(results)} candidate(s); {failures} not feasible/ok"))
    return 0 if failures == 0 else 1


def cmd_frontier(args: argparse.Namespace) -> int:
    """`meta-research frontier` — print the current Pareto set + best-per-objective."""
    run_dir = Path(args.run_dir).resolve()
    fpath = run_dir / "frontier.json"
    if not fpath.is_file():
        _err(f"no frontier.json in {run_dir} — run `meta-research seed` first")
        return 2
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _err(f"could not read frontier.json: {exc}")
        return 2

    objectives = data.get("objectives", [])
    obj_names = [o.get("name") for o in objectives]
    print(logfmt.bold("Pareto frontier"))
    pareto = data.get("pareto", [])
    if not pareto:
        print(logfmt.dim("  (empty — no feasible candidates yet)"))
    for row in pareto:
        scores = row.get("scores", {})
        score_str = "  ".join(
            f"{n}={scores.get(n):g}" if scores.get(n) is not None else f"{n}=NA"
            for n in obj_names
        )
        label = f"{row.get('name')} (iter {row.get('iteration')})"
        print(f"  {logfmt.green(label)}: {score_str}")

    best = data.get("best_per_objective", {})
    if best:
        print(logfmt.bold("best per objective"))
        for obj_name, entry in best.items():
            print(
                f"  {obj_name}: {logfmt.cyan(str(entry.get('value')))} "
                f"({entry.get('name')})"
            )
    return 0


def cmd_kg(args: argparse.Namespace) -> int:
    """`meta-research kg` — rebuild the derived knowledge graph (DESIGN §7.6).

    A pure ledger read like `frontier`: no prepare.py required. Useful to
    backfill kg.json for experiment dirs whose bundles predate the KG; during
    normal operation every eval/seed already rebuilds it.
    """
    run_dir = Path(args.run_dir).resolve()
    # Lazy import keeps cli import light and mirrors cmd_progress.
    from .kg import write_kg

    try:
        path = write_kg(run_dir)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        _err(f"could not rebuild the knowledge graph: {exc}")
        return 1

    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    warnings = data.get("warnings", [])
    print(
        logfmt.bold("knowledge graph: ")
        + f"{len(nodes)} nodes, {len(edges)} edges, {len(warnings)} warnings"
    )
    print(f"  {logfmt.cyan(str(path))}")
    for warning in warnings:
        print(logfmt.yellow(f"  warning: {warning}"))
    return 0


def cmd_progress(args: argparse.Namespace) -> int:
    """`meta-research progress` — plot best-so-far curves + Pareto evolution (DESIGN §6.5).

    The autoresearch ``progress.png`` analog. Reads the append-only ledger only.
    """
    run_dir = Path(args.run_dir).resolve()
    try:
        experiment = load_experiment(run_dir)
    except (FileNotFoundError, ImportError, AttributeError, ValueError) as exc:
        _err(str(exc))
        return 2

    out_dir = Path(args.out).resolve() if args.out else run_dir
    # Lazy import: keep matplotlib out of the hot path for eval/seed/frontier.
    from .progress import plot_progress

    written = plot_progress(run_dir, list(experiment.OBJECTIVES), out_dir=out_dir)
    if not written:
        _err("no experience bundles yet — run `meta-research seed` first")
        return 1
    print(logfmt.bold(f"wrote {len(written)} progress plot(s):"))
    for p in written:
        print(f"  {logfmt.cyan(str(p))}")
    return 0


# --- init: locating the shipped skill + examples (works in an editable install) ---

#: Generated/scratch paths never copied when scaffolding `--from <example>`.
_EXAMPLE_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".git", "experience", "runs",
    "results.tsv", "frontier.json", "kg.json", "*.png", "*.npz",
)
_GITIGNORE_STUB = "__pycache__/\n*.pyc\n.venv/\n"


def _repo_root() -> Path:
    """Repo root that ships ``skills/`` and ``examples/`` (one level above the package).

    Resolves correctly for an editable install (``pip install -e``), where this
    file lives at ``<repo>/meta_research/cli.py``.
    """
    return Path(__file__).resolve().parent.parent


def _packaged_skill_dir() -> Path | None:
    d = _repo_root() / "skills" / "meta-research"
    return d if d.is_dir() else None


def _examples_root() -> Path:
    return _repo_root() / "examples"


def _list_examples() -> list[str]:
    root = _examples_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "prepare.py").is_file())


def _copy_skill_into(target: Path) -> bool:
    """Copy the shipped skill into ``target/.claude/skills/meta-research/``.

    Returns True if copied, False if the skill could not be located (e.g. a
    non-editable install that did not ship ``skills/``).
    """
    skill = _packaged_skill_dir()
    if skill is None:
        return False
    dst = target / ".claude" / "skills" / "meta-research"
    dst.mkdir(parents=True, exist_ok=True)
    for fname in ("SKILL.md", "REFERENCE.md"):
        src = skill / fname
        if src.is_file():
            shutil.copyfile(src, dst / fname)
    return True


def _scaffold_stubs(target: Path, name: str) -> None:
    """Write the bare-stub experiment (generic ``cost`` template)."""
    designs = target / "designs"
    designs.mkdir(parents=True)
    (target / "prepare.py").write_text(_PREPARE_STUB, encoding="utf-8")
    (target / "program.md").write_text(_PROGRAM_STUB.format(name=name), encoding="utf-8")
    (designs / "__init__.py").write_text("", encoding="utf-8")
    (designs / "baseline.py").write_text(_BASELINE_STUB, encoding="utf-8")


def _git_init_repo(target: Path, name: str) -> str:
    """`git init` the experiment + make the initial scaffold commit.

    Ensures a usable identity *local to this repo* when none is configured, so the
    append-only ledger works out of the box without touching global git config.
    Best-effort: returns a status string; never raises.
    """
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(target), capture_output=True, text=True, check=False
        )

    if run("init", "-q").returncode != 0:
        return "git init failed"
    # Only set a local identity if neither local nor global is configured.
    if not run("config", "user.email").stdout.strip():
        run("config", "user.email", "meta-research@localhost")
        run("config", "user.name", "meta-research")
    run("add", "-A")
    commit = run("commit", "-q", "-m", f"meta-research: scaffold {name} experiment")
    if commit.returncode != 0:
        return "git init ok, initial commit skipped"
    return "git initialized + initial commit"


def cmd_init(args: argparse.Namespace) -> int:
    """`meta-research init <name> [--from EXAMPLE]` — scaffold a ready-to-run experiment.

    Creates ``<run-dir>/<name>/`` with either a bare stub (default) or a copy of a
    shipped example (``--from water_cooling``), drops the meta-research skill into
    ``.claude/skills/`` so it loads when Claude Code runs there, and ``git init`` +
    initial commit so the append-only ledger is live immediately.
    """
    parent = Path(args.run_dir).resolve()
    target = parent / args.name
    if target.exists():
        _err(f"target already exists: {target}")
        return 2

    from_example = getattr(args, "from_example", None)
    try:
        if from_example:
            src = _examples_root() / from_example
            if not (src / "prepare.py").is_file():
                avail = ", ".join(_list_examples()) or "(none found)"
                _err(f"unknown example {from_example!r}; available: {avail}")
                return 2
            shutil.copytree(src, target, ignore=_EXAMPLE_IGNORE)
        else:
            _scaffold_stubs(target, args.name)
        (target / ".gitignore").write_text(_GITIGNORE_STUB, encoding="utf-8")
        skill_ok = _copy_skill_into(target)
        git_status = _git_init_repo(target, args.name)
    except OSError as exc:
        _err(f"could not scaffold experiment: {exc}")
        return 1

    print(logfmt.green(f"scaffolded experiment at {target}"))
    print(logfmt.dim(f"  source: {'example ' + from_example if from_example else 'bare stub'}"))
    print(logfmt.dim(f"  skill: {'copied into .claude/skills/' if skill_ok else 'NOT FOUND (use a -e install or copy skills/meta-research manually)'}"))
    print(logfmt.dim(f"  git: {git_status}"))
    nxt = "review prepare.py, then `meta-research seed`" if from_example else \
        "edit prepare.py (OBJECTIVES, OPERATING, make_evaluator), then `meta-research seed`"
    print(logfmt.dim(f"  next: cd {target.name} && {nxt}"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. No looping ``run`` subcommand by design."""
    parser = argparse.ArgumentParser(
        prog="meta-research",
        description=(
            "Deterministic tools for autonomous design research. The agent runs the "
            "loop in-session; this CLI only exposes single deterministic steps."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_run_dir(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--run-dir",
            default=".",
            help="experiment directory holding prepare.py (default: cwd)",
        )

    p_eval = sub.add_parser("eval", help="evaluate one design and record it")
    p_eval.add_argument("design", help="candidate module name under DESIGNS_DIR")
    p_eval.add_argument("--commit", action="store_true", help="commit the bundle (append-only)")
    p_eval.add_argument("--tag", default=None, help="run tag -> branch meta-research/<tag>")
    p_eval.add_argument("--hypothesis", default=None, help="path to hypothesis hand-off JSON")
    add_run_dir(p_eval)
    p_eval.set_defaults(func=cmd_eval)

    p_seed = sub.add_parser("seed", help="evaluate every baseline once (Phase 0)")
    p_seed.add_argument("--commit", action="store_true", help="commit each baseline (append-only)")
    add_run_dir(p_seed)
    p_seed.set_defaults(func=cmd_seed)

    p_front = sub.add_parser("frontier", help="print the current Pareto frontier")
    add_run_dir(p_front)
    p_front.set_defaults(func=cmd_frontier)

    p_kg = sub.add_parser("kg", help="rebuild kg.json from the experience ledger")
    add_run_dir(p_kg)
    p_kg.set_defaults(func=cmd_kg)

    p_prog = sub.add_parser("progress", help="plot best-so-far curves + Pareto evolution (progress.png)")
    p_prog.add_argument("--out", default=None, help="output dir for PNGs (default: run dir)")
    add_run_dir(p_prog)
    p_prog.set_defaults(func=cmd_progress)

    p_init = sub.add_parser("init", help="scaffold a new experiment dir (git + skill ready)")
    p_init.add_argument("name", help="experiment directory name to create")
    p_init.add_argument(
        "--from", dest="from_example", default=None, metavar="EXAMPLE",
        help="scaffold from a shipped example (e.g. water_cooling) instead of bare stubs",
    )
    add_run_dir(p_init)
    p_init.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``meta-research`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


# --- init scaffolding templates (kept tiny; the real example lives in examples/) ---

_PREPARE_STUB = '''\
"""FIXED experiment configuration (configuration-as-code, DESIGN §4).

Edit OBJECTIVES / OPERATING / make_evaluator for your domain. This file is
read-only to the research agent; the agent only writes designs/.
"""

from __future__ import annotations

from meta_research.interfaces import Objective
from meta_research.evaluators import NumericalEvaluator

OBJECTIVES: list[Objective] = [
    Objective("cost", direction="min", unit=""),
]
BUDGET = {"max_iterations": 50, "candidates_per_iteration": 1,
          "proposer_model": "", "proposer_timeout_s": 0}
OPERATING: dict = {}
BASELINES: list[str] = ["baseline"]
DESIGNS_DIR = "designs"


def _simulate(params: dict, out_dir):
    # (scores, metadata, artifacts) — replace with your model.
    cost = float(params.get("cost", 1.0))
    return {"cost": cost}, {}, {}


def make_evaluator() -> NumericalEvaluator:
    return NumericalEvaluator(OBJECTIVES, _simulate)
'''

_BASELINE_STUB = '''\
"""Baseline candidate. The agent copies this and changes ONE mechanism."""

from __future__ import annotations

from meta_research.interfaces import DesignSpec

NAME = "baseline"


def build() -> DesignSpec:
    return DesignSpec(params={"cost": 1.0}, notes="baseline")
'''

_PROGRAM_STUB = '''\
# {name} — research program

This experiment uses the **meta-research** skill. Invoke it and run the loop
in-session. There is no Python orchestrator.

- Read-only: `prepare.py`. Write target: `designs/`.
- `meta-research seed` to evaluate baselines, then loop:
  write a design -> `meta-research eval <name> --hypothesis hyp.json --commit`
  -> read `result.json` + `heatmap.png` -> repeat.
- The ledger is `results.tsv`, `frontier.json`, and `experience/` — read those
  directly. git is the **append-only** audit trail: never reset history; this
  run dir must stay its own git repository.
- **NEVER STOP** until interrupted; never declare the frontier optimal.
'''


if __name__ == "__main__":
    raise SystemExit(main())
