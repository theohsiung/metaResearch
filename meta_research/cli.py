"""Command-line entry point — the deterministic steps the agent invokes (DESIGN §6.5).

This is intentionally **not** an orchestrator. There is no ``run`` subcommand that
loops; looping is the agent in the Claude Code session following the skill. The CLI
exposes only deterministic, side-effecting-but-append-only steps:

    meta-research eval <design> [--commit] [--run-dir DIR] [--hypothesis FILE]
    meta-research seed [--commit] [--run-dir DIR]
    meta-research frontier [--run-dir DIR]
    meta-research kg [--run-dir DIR]
    meta-research calibration [--run-dir DIR]
    meta-research ideas <card|cards|add|review|schedule|match|rank|proximity|standin|link|sync|stats|digest|metareview>
    meta-research progress [--out DIR] [--run-dir DIR]
    meta-research resources <probe|check|show>
    meta-research ui [--out FILE] | serve [--port 8765]
    meta-research init <name> [--from EXAMPLE] [--run-dir DIR]

The experiment is discovered from the working directory (autoresearch style):
``./prepare.py`` is loaded via :mod:`importlib`, its ``DESIGNS_DIR`` is resolved
relative to ``prepare.py`` and added to ``sys.path`` so candidate modules import.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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


#: Default for ``prepare.IDEA_GATE_MINUTES``: from this evaluation cost on, a design must name the idea it tests.
IDEA_GATE_MINUTES = 60


def _idea_gate(experiment: ModuleType, run_dir: Path, hypothesis: dict[str, Any] | None,
               no_idea: str | None) -> str | None:
    """Why this evaluation may not run, or None.

    A named idea must be on the board. When ``prepare.EVAL_MINUTES`` (one evaluation's cost) reaches
    ``prepare.IDEA_GATE_MINUTES`` (default 60), the hypothesis must name one: an expensive run should come
    from an ideation round, or from a direction recorded as a ``directed`` idea, so its outcome reaches the
    Elo ratings through reality matches. ``--no-idea REASON`` skips the requirement and records the reason.
    """
    from . import ideas as ib

    idea = (hypothesis or {}).get("idea")
    if idea:
        if idea not in ib.Board(run_dir).ideas:
            return (f"the hypothesis names idea {idea}, which is not on {ib.BOARD_DIRNAME}/{ib.BOARD_FILENAME}; "
                    "add it first (meta-research ideas add ...)")
        return None
    minutes = getattr(experiment, "EVAL_MINUTES", None)
    gate = getattr(experiment, "IDEA_GATE_MINUTES", IDEA_GATE_MINUTES)
    if minutes is None or minutes < gate or no_idea:
        return None
    return (f"one evaluation costs about {minutes:g} min (prepare.EVAL_MINUTES >= IDEA_GATE_MINUTES = {gate:g}): "
            "name the idea this design tests in hyp.json ('idea'), from an ideation round or recorded with "
            "'meta-research ideas add --origin directed', or pass --no-idea REASON")


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

    problem = _idea_gate(experiment, run_dir, hypothesis, getattr(args, "no_idea", None))
    if problem:
        _err(problem)
        return 2
    if hypothesis is not None and getattr(args, "no_idea", None) and not hypothesis.get("idea"):
        hypothesis = {**hypothesis, "reasoning": f"(no idea: {args.no_idea}) {hypothesis.get('reasoning') or ''}".strip()}

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


def cmd_finalize(args: argparse.Namespace) -> int:
    """`meta-research finalize` — reveal sealed holdout scores of the top-k and pick one.

    The only command that reads ``.sealed/``; every reveal is appended to
    ``finalize.jsonl`` (and committed with --commit), so the ledger shows when the
    holdout was looked at.
    """
    from .sealed import finalize

    run_dir = Path(args.run_dir).resolve()
    try:
        experiment = load_experiment(run_dir)
        weights = None
        if getattr(args, "weights", None):
            weights = {}
            for part in args.weights.split(","):
                name, sep, value = part.strip().partition("=")
                if not sep:
                    raise ValueError(f"--weights expects name=weight pairs, got {part!r}")
                weights[name.strip()] = float(value)
        record = finalize(run_dir, list(experiment.OBJECTIVES), top=args.top, by=args.by,
                          pick=args.pick, pick_direction=args.direction, weights=weights,
                          exclude=list(getattr(experiment, "REFERENCE_POINTS", [])))
    except (FileNotFoundError, ImportError, AttributeError, ValueError) as exc:
        _err(str(exc))
        return 2
    print(logfmt.bold(f"top {record['top']} by {record['by']} (search); pick {record['pick']} "
                      f"({record['pick_direction']}) on the sealed holdout"))
    for cand in record["candidates"]:
        sealed = cand["sealed"]
        sealed_str = "  ".join(f"{k}={v:g}" for k, v in sorted(sealed.items())) if sealed else "(no sealed scores)"
        mark = logfmt.green("* ") if cand["bundle"] == record["chosen"] else "  "
        shown = record.get("weights") or {record["by"]: None}
        search_str = "  ".join(f"{k}={cand['search'][k]:g}" for k in sorted(shown))
        print(f"{mark}{cand['bundle']}: search {search_str} | {sealed_str}")
    if record["chosen"] is None:
        _err(f"no revealed candidate has sealed key {record['pick']!r}")
        return 1
    print(logfmt.bold(f"chosen: {record['chosen']}"))
    if args.commit:
        from .experience import Experience

        sha = Experience(run_dir, list(experiment.OBJECTIVES)).commit(
            f"finalize: reveal sealed {record['pick']} of top {record['top']} by {record['by']} -> {record['chosen']}"
        )
        print(logfmt.dim(f"  committed {sha}"))
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    """`meta-research suggest` — recommend an operator and parent(s) from the ledger (deterministic)."""
    from .suggest import suggest

    run_dir = Path(args.run_dir).resolve()
    try:
        references = list(getattr(load_experiment(run_dir), "REFERENCE_POINTS", []))
    except (FileNotFoundError, ImportError, AttributeError, SyntaxError):
        references = []  # suggest works from the ledger alone; prepare.py only adds the reference list
    rec = suggest(run_dir, seed=args.seed, temperature=args.temperature, crossover_rate=args.crossover_rate,
                  draft_rate=args.draft_rate, stagnation=args.stagnation, references=references)
    if args.json:
        print(json.dumps(rec, indent=2, sort_keys=True))
        return 0
    parents = ", ".join(rec["parents"]) or "(none)"
    print(logfmt.bold(f"suggested operator: {rec['operator']}   parents: {parents}"))
    for fact in rec["facts"]:
        print(f"  - {fact}")
    print(logfmt.dim(f"  (seed {rec['seed']}; cite it in hyp.json, or state why you overrule it)"))
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """`meta-research context` — the bundles to read for an operator (scoped experience)."""
    from .suggest import context

    run_dir = Path(args.run_dir).resolve()
    try:
        picks = context(run_dir, args.operator, args.parent or [])
    except ValueError as exc:
        _err(str(exc))
        return 2
    print(logfmt.bold(f"read for {args.operator}:"))
    for bundle, why in picks:
        print(f"  experience/{bundle}  — {why}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """`meta-research recall "<query>"` — earlier candidates whose hypothesis matches the query."""
    from .recall import recall

    run_dir = Path(args.run_dir).resolve()
    hits = recall(run_dir, args.query, k=args.k)
    if args.json:
        print(json.dumps(hits, indent=2, sort_keys=True))
        return 0
    if not hits:
        print(logfmt.dim("no earlier candidate matches"))
        return 0
    for hit in hits:
        scores = "  ".join(f"{k}={v:g}" for k, v in sorted(hit["scores"].items()))
        verdicts = ", ".join(f"{k}:{v}" for k, v in sorted(hit["verdicts"].items()))
        print(f"  experience/{hit['id']} [{hit['status']}] {scores}" + (f" | predictions {verdicts}" if verdicts else ""))
        if hit["change"]:
            print(logfmt.dim(f"      change: {hit['change']}"))
    return 0


def _weights(raw: str | None) -> dict[str, float] | None:
    """Parse ``name=weight,...`` (as finalize's ``--weights``)."""
    if not raw:
        return None
    weights = {}
    for part in raw.split(","):
        name, sep, value = part.partition("=")
        if not sep:
            raise ValueError(f"expected name=number pairs, got {part!r}")
        weights[name.strip()] = float(value)
    return weights


def _load_rescored(raw: str | None, run_dir: Path) -> dict[str, dict[str, float]] | None:
    """``--rescored`` JSON ``{bundle: {name: value, ...}}`` (non-numeric fields are notes); a relative path that
    does not exist from the cwd is read from the run dir, where the file is kept beside the ledger."""
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() and not path.exists():
        path = run_dir / path
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {b: {k: float(v) for k, v in vals.items() if isinstance(v, (int, float))} for b, vals in doc.items()}


def _fields(args: argparse.Namespace, keys: tuple[str, ...]) -> dict[str, Any]:
    """Event fields from ``--file`` JSON, overridden by the flags given on the command line."""
    fields: dict[str, Any] = {}
    if getattr(args, "file", None):
        doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError(f"{args.file} must hold a JSON object")
        fields.update(doc)
    for key in keys:
        value = getattr(args, key, None)
        if value is not None:
            fields[key] = value
    for key in ("tags", "parents", "cites"):
        if isinstance(fields.get(key), str):
            fields[key] = [x.strip() for x in fields[key].split(",") if x.strip()]
    return {k: v for k, v in fields.items() if k in keys}


def cmd_ideas(args: argparse.Namespace) -> int:
    """`meta-research ideas <action>` — the Co-Scientist idea board (ideas/board.jsonl)."""
    from . import ideas as ib

    board = ib.Board(Path(args.run_dir).resolve())
    try:
        weights = _weights(getattr(args, "weights", None))
        rescored = _load_rescored(getattr(args, "rescored", None), Path(args.run_dir))
        action = args.action
        if action == "card":
            e = board.add_card(**_fields(args, ("title", "source", "technique", "claim", "conditions", "cost", "tags")))
            print(logfmt.bold(f"card {e['id']}: {e['title']}"))
        elif action == "cards":
            for c in ib.search_cards(board, args.query or "", k=args.k):
                print(f"  {c['id']}  {c['title']}  [{c['source']}]")
                print(logfmt.dim(f"      {c['technique']}" + (f" | claim: {c['claim']}" if c["claim"] else "")))
        elif action == "add":
            e = board.add_idea(**_fields(args, ("title", "statement", "change", "origin", "strategy", "parents",
                                                "cites", "tags", "gist")))
            print(logfmt.bold(f"idea {e['id']}: {e['title']}"))
            near = [(s, a if b == e["id"] else b) for (a, b), s in ib.similarity(board).items()
                    if e["id"] in (a, b) and s >= args.duplicate]
            for s, other in sorted(near, reverse=True):
                print(logfmt.yellow(f"  near-duplicate of {other} ({board.ideas[other]['title']}), similarity {s:.2f}"))
        elif action == "review":
            fields = _fields(args, ("kind", "verdict", "critique"))
            if args.scores:
                fields["scores"] = _weights(args.scores)
            board.add_review(args.idea, **fields)
            print(f"{args.idea}: {fields['kind']} review -> {fields['verdict']}")
        elif action == "schedule":
            tested = set(ib.outcomes(board, weights=weights, scores=rescored))
            plan = ib.schedule(board, n=args.n, top_fraction=args.top_fraction, tested=tested)
            if args.json:
                print(json.dumps(plan, indent=2))
                return 0
            if not plan:
                print(logfmt.dim("fewer than two ideas passed review; nothing to match"))
            for m in plan:
                print(f"  {m['a']} vs {m['b']}  [{m['format']}]  similarity {m['similarity']:.2f}, met {m['met']}x")
                print(logfmt.dim(f"      {board.ideas[m['a']]['title']}  |  {board.ideas[m['b']]['title']}"))
        elif action == "match":
            board.add_match(args.a, args.b, winner=args.winner, format=args.format, rationale=args.rationale or "")
            r = ib.elo(board)
            print(f"{args.a} {r[args.a]['elo']:.0f}  {args.b} {r[args.b]['elo']:.0f}")
        elif action == "rank":
            rows = ib.rank(board, weights=weights, scores=rescored)
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            for row in rows:
                real = "failed" if row["failed"] else ("" if row["gain"] is None else f"gain {row['gain']:+.4f}")
                print(f"  {row['id']}  {row['elo']:7.1f}  {row['wins']}-{row['losses']}  {row['status']:8}"
                      f"  {row['origin'][:3]}:{row['strategy'] or '-':12} "
                      f"{('[' + row['gist'] + '] ') if row['gist'] else ''}{row['title']}  {real}")
        elif action == "proximity":
            graph = ib.proximity(board, threshold=args.threshold, duplicate=args.duplicate)
            if args.json:
                print(json.dumps(graph, indent=2))
                return 0
            for cluster in graph["clusters"]:
                print("  " + ", ".join(f"{i} {board.ideas[i]['title']}" for i in cluster))
            for d in graph["duplicates"]:
                print(logfmt.yellow(f"  near-duplicates: {d['a']} ~ {d['b']} ({d['similarity']:.2f})"))
        elif action == "standin":
            board.standin(args.name, args.bundle)
            print(f"parent {args.name} -> experience/{args.bundle}")
        elif action == "gist":
            board.set_gist(args.idea, args.text)
            print(f"{args.idea}: {args.text.strip()}")
        elif action == "start":
            board.start(args.idea, args.run, where=args.where or "", note=args.note or "")
            print(f"{args.idea} running: {args.run}" + (f" on {args.where}" if args.where else ""))
        elif action == "link":
            board.link(args.idea, args.bundle)
            print(f"{args.idea} <- experience/{args.bundle}")
        elif action == "sync":
            added = ib.sync(board, weights=weights, deadband=args.deadband, scores=rescored)
            for m in added:
                loser = m["b"] if m["winner"] == m["a"] else m["a"]
                print(f"  reality: {m['winner']} beat {loser} ({m['rationale']})")
            print(logfmt.dim(f"  {len(added)} reality match(es) played"))
        elif action == "stats":
            print(json.dumps(ib.stats(board, weights=weights, deadband=args.deadband, scores=rescored), indent=2))
        elif action == "digest":
            print(ib.digest(board, last=args.last))
        elif action == "metareview":
            text = Path(args.file).read_text(encoding="utf-8") if args.file else (args.text or "")
            board.add_metareview(text)
            print("meta-review recorded")
    except (ib.BoardError, ValueError, TypeError, FileNotFoundError) as exc:
        _err(str(exc))
        return 2
    return 0


def cmd_trajectory(args: argparse.Namespace) -> int:
    """`meta-research trajectory` — candidates over time with lineage edges (trajectory.png)."""
    from .suggest import objectives_from_frontier
    from .trajectory import TRAJECTORY_FILENAME, render_trajectory, trajectory_data

    run_dir = Path(args.run_dir).resolve()
    objectives = objectives_from_frontier(run_dir)
    if not objectives:
        _err(f"no frontier.json in {run_dir} — run `meta-research seed` first")
        return 2
    try:
        rescored = _load_rescored(args.rescored, run_dir)
    except (OSError, ValueError) as exc:
        _err(str(exc))
        return 2
    objective = next((o for o in objectives if o.name == (args.objective or objectives[0].name)), None)
    if objective is None and rescored and any(args.objective in v for v in rescored.values()):
        objective = Objective(args.objective, direction="max")  # a rescored-only score, higher is better
    if objective is None:
        _err(f"unknown objective {args.objective!r}; known: {', '.join(o.name for o in objectives)}")
        return 2
    refs = {}
    for item in args.ref or []:
        label, _, value = item.partition("=")
        try:
            refs[label] = float(value)
        except ValueError:
            _err(f"--ref expects LABEL=NUMBER, got {item!r}")
            return 2
    data = trajectory_data(run_dir, objective, refs=refs, scores=rescored)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    path = render_trajectory(data, Path(args.out) if args.out else run_dir / TRAJECTORY_FILENAME)
    print(logfmt.bold(f"wrote {path}"))
    print(logfmt.dim(f"  {len(data['points'])} candidates, {len(data['edges'])} lineage edges, "
                     f"{len(data['annotations'])} annotated"))
    return 0


def _ui_builder(args: argparse.Namespace):
    """A zero-argument function returning the page data, re-reading the ledger (and --rescored) on every call.
    Raises ValueError on bad arguments, checked once up front."""
    from .suggest import objectives_from_frontier
    from .ui import ui_data

    run_dir = Path(args.run_dir).resolve()
    weights = _weights(args.weights)
    refs = {}
    for item in args.ref or []:
        label, _, value = item.partition("=")
        try:
            refs[label] = float(value)
        except ValueError:
            raise ValueError(f"--ref expects LABEL=NUMBER, got {item!r}") from None

    def build() -> dict[str, Any]:
        rescored = _load_rescored(args.rescored, run_dir)
        objectives = list(objectives_from_frontier(run_dir) or [])
        known = {o.name for o in objectives}
        extra = sorted({k for v in (rescored or {}).values() for k in v} - known)
        objectives = [Objective(k, direction="max") for k in extra] + objectives  # a rescored metric is the one to read
        if args.objective:
            first = [o for o in objectives if o.name == args.objective]
            if not first:
                raise ValueError(f"unknown objective {args.objective!r}; known: {', '.join(o.name for o in objectives)}")
            objectives = first + [o for o in objectives if o.name != args.objective]
        return ui_data(run_dir, objectives, refs=refs, scores=rescored, weights=weights, title=args.title)

    build()  # fail fast on bad arguments
    return build


def cmd_ui(args: argparse.Namespace) -> int:
    """`meta-research ui` — one self-contained HTML page: trajectory per objective + the idea tournament."""
    from .ui import UI_FILENAME, render_ui

    try:
        data = _ui_builder(args)()
    except (OSError, ValueError) as exc:
        _err(str(exc))
        return 2
    run_dir = Path(args.run_dir).resolve()
    path = render_ui(data, Path(args.out) if args.out else run_dir / UI_FILENAME)
    t = data["tournament"]
    print(logfmt.bold(f"wrote {path}"))
    print(logfmt.dim(f"  {len(data['runs'])} runs, {len(data['views'])} objective view(s)"
                     + (f", {len(t['ideas'])} ideas, {len(t['matches'])} matches" if t else ", no idea board")))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """`meta-research serve` — the research page on http://localhost:PORT, rebuilt from the ledger on every load and
    reloaded in the browser when a result or idea-board event lands."""
    from .ui import serve

    try:
        build = _ui_builder(args)
    except (OSError, ValueError) as exc:
        _err(str(exc))
        return 2
    run_dir = Path(args.run_dir).resolve()
    extra = [run_dir / args.rescored] if args.rescored else []
    print(logfmt.bold(f"serving {run_dir.name} on http://{args.host}:{args.port}/  (Ctrl-C to stop)"))
    if args.host not in ("127.0.0.1", "localhost"):
        print(logfmt.yellow("  warning: reachable from other machines; the page shows the whole ledger"))
    try:
        serve(build, run_dir, host=args.host, port=args.port, extra=extra)
    except OSError as exc:
        _err(f"could not serve on {args.host}:{args.port}: {exc}")
        return 1
    except KeyboardInterrupt:
        pass
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


def _fmt_rate(rate: float | None, tally: dict[str, Any]) -> str:
    """Render a hit-rate as ``66.7% (2/3 decided)`` or ``n/a`` when undecided."""
    decided = int(tally.get("confirmed", 0)) + int(tally.get("refuted", 0))
    if rate is None:
        return logfmt.dim(f"n/a (0 of {int(tally.get('predictions', 0))} decided)")
    return f"{rate * 100:.1f}% ({int(tally['confirmed'])}/{decided} decided)"


def cmd_calibration(args: argparse.Namespace) -> int:
    """`meta-research calibration` — print the proposer's hypothesis hit-rate (DESIGN §6.5).

    A pure ledger read like `frontier`/`kg`: no prepare.py required. Derives from
    the KG edges' prediction verdicts (§7.6); persists nothing.
    """
    run_dir = Path(args.run_dir).resolve()
    from .calibration import build_calibration

    try:
        report = build_calibration(run_dir)
    except (OSError, TypeError, ValueError) as exc:
        _err(f"could not compute calibration: {exc}")
        return 1

    totals = report["totals"]
    print(
        logfmt.bold("hypothesis calibration: ")
        + f"{totals['predictions']} prediction(s) — "
        + f"{totals['confirmed']} confirmed, {totals['refuted']} refuted, "
        + f"{totals['inconclusive']} inconclusive"
    )
    print(f"  hit-rate: {_fmt_rate(report['hit_rate'], totals)}")

    by_objective = report.get("by_objective", {})
    if by_objective:
        print(logfmt.bold("by objective"))
        for name, summary in sorted(by_objective.items()):
            print(f"  {name}: {_fmt_rate(summary['hit_rate'], summary)}")

    by_axis = report.get("by_axis", {})
    if by_axis:
        print(logfmt.bold("by axis"))
        for axis, summary in sorted(by_axis.items()):
            label = axis or "(unlabelled)"
            print(f"  {label}: {_fmt_rate(summary['hit_rate'], summary)}")
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
    for name in ("meta-research", "meta-research-setup"):
        src_dir = skill.parent / name
        dst = target / ".claude" / "skills" / name
        dst.mkdir(parents=True, exist_ok=True)
        for fname in ("SKILL.md", "REFERENCE.md"):
            src = src_dir / fname
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

    Commits as the user, never as an invented identity: the global config or
    ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` when set, else the identity of the
    repository the experiment is scaffolded inside (copied into this repo's local
    config). With none of these, the commit is skipped and the status says why.
    Best-effort: returns a status string; never raises.
    """
    def run(*args: str, cwd: Path = target) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
        )

    if run("init", "-q").returncode != 0:
        return "git init failed"
    has_env = all(os.environ.get(k) for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"))
    if not has_env and not run("config", "user.email").stdout.strip():
        enclosing = {key: run("config", key, cwd=target.parent).stdout.strip()
                     for key in ("user.name", "user.email")}
        if not all(enclosing.values()):
            return ("git initialized, initial commit skipped: no git identity "
                    "(set user.name and user.email, then commit)")
        for key, value in enclosing.items():
            run("config", key, value)
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
    print(logfmt.dim("  compute: before the first run, use the meta-research-setup skill to record the machines "
                     "you may use (resources.json)"))
    return 0


def cmd_resources(args: argparse.Namespace) -> int:
    """`meta-research resources probe|check|show` — facts about the machines; the agent decides where to run."""
    import datetime as dt

    from . import resources

    if args.action == "probe":
        try:
            info = resources.parse_probe(resources.run_probe(args.host, args.path or None))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            _err(str(exc))
            return 1
        info["host"] = args.host
        info["endpoints"] = [resources.endpoint_up(u) for u in args.endpoint]
        if args.json:
            print(json.dumps(info, indent=2))
            return 0
        print(f"{info['hostname']} ({args.host or 'local'}): {info['cpus']} CPUs, {info['ram_gb']} GB RAM")
        for g in info["accelerators"] or [{"name": "no GPU found"}]:
            mem = (f", {g.get('memory_gb')} GB ({g.get('used_gb')} used, {g.get('util_pct')}% busy)"
                   if g.get("memory_gb") else "")
            print(f"  gpu  {g.get('name')}{mem}")
        for d in info["disk"]:
            print(f"  disk {d['mount']}: {d['free_gb']} GB free of {d['size_gb']}")
        print(f"  docker: {', '.join(info['docker_images'][:5]) or 'none'}; group ids: {info['group_gids'] or 'none'}")
        for e in info["endpoints"]:
            print(f"  endpoint {e['url']}: " + ("up " + ", ".join(map(str, e["models"])) if e["up"]
                                                else f"DOWN ({e['error']})"))
        return 0

    path = Path(args.resources) if args.resources else Path(args.run_dir) / "resources.json"
    if not path.is_file():
        _err(f"no {path}: run the meta-research-setup skill first")
        return 2
    try:
        res = resources.load(path)
    except ValueError as exc:
        _err(str(exc))
        return 2
    hosts = [h for h in res["hosts"] if args.host in (None, h["name"])]
    if not hosts:
        _err(f"no host named {args.host!r} in {path}")
        return 2
    if args.action == "check":
        failed = 0
        for h in hosts:
            print(logfmt.bold(f"{h['name']} ({h.get('ssh') or 'local'})"))
            for kind, item, ok, detail in resources.run_check(h):
                failed += not ok
                mark = logfmt.green("ok  ") if ok else logfmt.red("FAIL")
                shown = detail if (not ok or kind == "free disk") else ""
                print(f"  {mark} {kind:<11} {item}  {shown}".rstrip())
        print(logfmt.red(f"{failed} check(s) failed") if failed else logfmt.green("all checks passed"))
        return 1 if failed else 0

    now = dt.datetime.now()
    print(f"now {now:%a %H:%M} (local time)")
    for h in hosts:
        gpus = ", ".join(sorted({str(g.get("name")) for g in h.get("accelerators", [])})) or "no GPU listed"
        n = len(h.get("accelerators", []))
        state = (logfmt.green("available now") if resources.available(h, now)
                 else logfmt.yellow("outside its windows"))
        print(f"{h['name']:<14} {state}  {n} x {gpus}  ({h.get('ssh') or 'local'})")
        for w in h.get("windows", []):
            print(logfmt.dim(f"    window {w.get('days', 'all')} {w['start']}-{w['end']} {w.get('note', '')}"))
        stop = resources.next_stop(h, now)
        if stop:
            print(f"    next stop {stop[0]}: {stop[1]}")
        if h.get("container"):
            print(logfmt.dim(f"    container {h['container'].get('image')} via {h['container'].get('run')}"))
    for s in res.get("services", []):
        state = "available now" if resources.available(s, now) else "outside its windows"
        print(f"service {s['name']}: {s['url']}, max {s.get('max_concurrency', '?')} concurrent, {state}")
    if res.get("confirm_before"):
        print("ask the user before: " + "; ".join(res["confirm_before"]))
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
    p_eval.add_argument("--no-idea", metavar="REASON", default=None,
                        help="run an expensive evaluation without a board idea (the reason is recorded)")
    add_run_dir(p_eval)
    p_eval.set_defaults(func=cmd_eval)

    p_seed = sub.add_parser("seed", help="evaluate every baseline once (Phase 0)")
    p_seed.add_argument("--commit", action="store_true", help="commit each baseline (append-only)")
    add_run_dir(p_seed)
    p_seed.set_defaults(func=cmd_seed)

    p_front = sub.add_parser("frontier", help="print the current Pareto frontier")
    add_run_dir(p_front)
    p_front.set_defaults(func=cmd_frontier)

    p_fin = sub.add_parser(
        "finalize", help="reveal sealed holdout scores of the top-k candidates and pick one (logged)"
    )
    p_fin.add_argument("--top", type=int, default=3, help="candidates to reveal (default 3)")
    p_fin.add_argument("--by", default=None, help="search objective that ranks them (default: first)")
    p_fin.add_argument("--pick", default=None, help="sealed key the choice optimizes (default: --by)")
    p_fin.add_argument("--weights", default=None,
                       help="rank by a weighted sum instead of --by, e.g. 's=0.6,o=0.4' (needs --pick)")
    p_fin.add_argument("--direction", choices=["max", "min"], default=None,
                       help="direction for --pick (default: that objective's, else max)")
    p_fin.add_argument("--commit", action="store_true", help="commit the reveal (append-only)")
    add_run_dir(p_fin)
    p_fin.set_defaults(func=cmd_finalize)

    p_sug = sub.add_parser("suggest", help="recommend an operator + parent(s) (rank selection, stagnation)")
    p_sug.add_argument("--seed", type=int, default=0)
    p_sug.add_argument("--temperature", type=float, default=0.2, help="rank-selection temperature")
    p_sug.add_argument("--crossover-rate", type=float, default=0.15)
    p_sug.add_argument("--draft-rate", type=float, default=0.1)
    p_sug.add_argument("--stagnation", type=int, default=3, help="non-improving children that stall a parent")
    p_sug.add_argument("--json", action="store_true")
    add_run_dir(p_sug)
    p_sug.set_defaults(func=cmd_suggest)

    p_ctx = sub.add_parser("context", help="list the bundles to read for an operator")
    p_ctx.add_argument("--operator", required=True, choices=["draft", "improve", "debug", "crossover"])
    p_ctx.add_argument("--parent", action="append", help="parent bundle id (repeat for crossover)")
    add_run_dir(p_ctx)
    p_ctx.set_defaults(func=cmd_context)

    p_rec = sub.add_parser("recall", help="find earlier candidates whose hypothesis matches a query (BM25)")
    p_rec.add_argument("query")
    p_rec.add_argument("-k", type=int, default=5)
    p_rec.add_argument("--json", action="store_true")
    add_run_dir(p_rec)
    p_rec.set_defaults(func=cmd_recall)

    p_ideas = sub.add_parser("ideas", help="Co-Scientist idea board: cards, ideas, reviews, Elo tournament")
    acts = p_ideas.add_subparsers(dest="action", required=True)

    def act(name: str, text: str) -> argparse.ArgumentParser:
        p = acts.add_parser(name, help=text)
        add_run_dir(p)
        p.set_defaults(func=cmd_ideas)
        return p

    p = act("card", "add a literature card (one technique from a paper, model card or report)")
    p.add_argument("--file", help="JSON object with the fields below")
    for flag in ("title", "source", "technique", "claim", "conditions", "cost", "tags"):
        p.add_argument(f"--{flag}")
    p = act("cards", "list or search (BM25) the literature cards")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("-k", type=int, default=10)
    p = act("add", "add an idea (Generation), an evolved one (--origin evolution --parents) or a direction the "
                   "user or an audit chose (--origin directed --strategy user|audit)")
    p.add_argument("--file", help="JSON object with the fields below")
    for flag in ("title", "statement", "change", "strategy", "parents", "cites", "tags"):
        p.add_argument(f"--{flag}")
    p.add_argument("--gist", help="a label of a few words naming the core mechanism, for tables")
    p.add_argument("--origin", choices=["generation", "evolution", "directed"])
    p.add_argument("--duplicate", type=float, default=0.75, help="similarity that flags a near-duplicate")
    p = act("gist", "give an idea a short label of its core mechanism (the latest one wins)")
    p.add_argument("idea")
    p.add_argument("text")
    p = act("review", "record a Reflection review of an idea")
    p.add_argument("idea")
    p.add_argument("--kind", required=True,
                   choices=["initial", "full", "deep", "observation", "simulation", "tournament"])
    p.add_argument("--verdict", required=True, choices=["pass", "revise", "fail"])
    p.add_argument("--scores", help="e.g. correctness=4,novelty=3,feasibility=5")
    p.add_argument("--critique")
    p.add_argument("--file", help="JSON object holding the critique")
    p = act("schedule", "the next tournament matches (new and top ideas, similar opponents)")
    p.add_argument("-n", type=int, default=4)
    p.add_argument("--top-fraction", type=float, default=0.25, help="share of top ideas whose matches are debates")
    p.add_argument("--weights", help="objective weights for realized gains, e.g. 's=0.6,o=0.4'")
    p.add_argument("--rescored", help="JSON {bundle: {name: value}} overriding ledger scores (a changed metric)")
    p.add_argument("--json", action="store_true")
    p = act("match", "record a judged match and update Elo")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--winner", required=True)
    p.add_argument("--format", default="single", choices=["single", "debate"])
    p.add_argument("--rationale")
    p = act("rank", "ideas by Elo with status and realized gain")
    p.add_argument("--weights")
    p.add_argument("--rescored")
    p.add_argument("--json", action="store_true")
    p = act("proximity", "similarity clusters and near-duplicates")
    p.add_argument("--threshold", type=float, default=0.3)
    p.add_argument("--duplicate", type=float, default=0.75)
    p.add_argument("--json", action="store_true")
    p = act("standin", "name the bundle that scored a run's model when that run's own bundle is missing or failed")
    p.add_argument("name", help="design name a hypothesis names as parent, e.g. d7_all_pools")
    p.add_argument("bundle", help="experience/ bundle id that scored its model, e.g. 029_d7_score")
    p = act("start", "record that a run testing an idea was launched (the idea is then 'running')")
    p.add_argument("idea")
    p.add_argument("--run", required=True, help="the design name of the run, e.g. d11_minimal_pairs")
    p.add_argument("--where", help="where it runs, e.g. a host name from resources.json")
    p.add_argument("--note")
    p = act("link", "mark an evaluated bundle as the test of an idea")
    p.add_argument("idea")
    p.add_argument("bundle", help="experience/ bundle id, e.g. 012_d1_same_skill_data")
    p = act("sync", "play reality matches between tested ideas from their realized gains")
    p.add_argument("--weights")
    p.add_argument("--rescored")
    p.add_argument("--deadband", type=float, default=0.002, help="gains closer than this are a tie")
    p = act("stats", "Supervisor statistics: methods, reviews, matches, judge-vs-reality concordance")
    p.add_argument("--weights")
    p.add_argument("--rescored")
    p.add_argument("--deadband", type=float, default=0.002)
    p = act("digest", "latest reviews and match rationales, for the meta-review")
    p.add_argument("--last", type=int, default=40)
    p = act("metareview", "record a meta-review (fed into the next round's prompts)")
    p.add_argument("--file")
    p.add_argument("--text")

    p_traj = sub.add_parser("trajectory", help="plot candidates over time with lineage edges (trajectory.png)")
    p_traj.add_argument("--objective", default=None, help="objective on the y axis (default: first)")
    p_traj.add_argument("--ref", action="append", help="reference line LABEL=VALUE (repeatable)")
    p_traj.add_argument("--out", default=None, help="output PNG (default: <run dir>/trajectory.png)")
    p_traj.add_argument("--json", action="store_true", help="print the plotted data instead of drawing")
    p_traj.add_argument("--rescored", help="JSON {bundle: {name: value}} overriding ledger scores (a changed metric); "
                                           "--objective may then name a score that only it holds")
    add_run_dir(p_traj)
    p_traj.set_defaults(func=cmd_trajectory)

    def ui_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ref", action="append", help="reference line LABEL=VALUE on the trajectory (repeatable)")
        p.add_argument("--rescored", help="JSON {bundle: {name: value}} overriding ledger scores; its names become "
                                          "extra objectives (higher is better)")
        p.add_argument("--weights", help="name=weight pairs for realized gains (as in `ideas rank`)")
        p.add_argument("--title", default=None, help="page title (default: the run dir's name)")
        p.add_argument("--objective", default=None, help="objective shown first (default: first rescored, else first)")
        add_run_dir(p)

    p_ui = sub.add_parser("ui", help="write research.html: an interactive page with the trajectory and the tournament")
    p_ui.add_argument("--out", default=None, help="output HTML (default: <run dir>/research.html)")
    ui_args(p_ui)
    p_ui.set_defaults(func=cmd_ui)

    p_srv = sub.add_parser("serve", help="the research page on http://localhost:PORT, live: it reloads when the "
                                         "ledger or idea board changes")
    p_srv.add_argument("--port", type=int, default=8765)
    p_srv.add_argument("--host", default="127.0.0.1", help="bind address (default: this machine only)")
    ui_args(p_srv)
    p_srv.set_defaults(func=cmd_serve)

    p_kg = sub.add_parser("kg", help="rebuild kg.json from the experience ledger")
    add_run_dir(p_kg)
    p_kg.set_defaults(func=cmd_kg)

    p_calib = sub.add_parser(
        "calibration", help="print the proposer's hypothesis hit-rate (predicted vs realized)"
    )
    add_run_dir(p_calib)
    p_calib.set_defaults(func=cmd_calibration)

    p_prog = sub.add_parser("progress", help="plot best-so-far curves + Pareto evolution (progress.png)")
    p_prog.add_argument("--out", default=None, help="output dir for PNGs (default: run dir)")
    add_run_dir(p_prog)
    p_prog.set_defaults(func=cmd_progress)

    p_res = sub.add_parser("resources", help="compute resources: probe a machine, check hosts, show what is usable now")
    racts = p_res.add_subparsers(dest="action", required=True)
    p = racts.add_parser("probe", help="GPUs, CPU, RAM, disk, docker and group ids of this machine or one ssh host")
    p.add_argument("--host", default=None, help="user@host to probe over one ssh call (default: this machine)")
    p.add_argument("--path", action="append", default=[], help="report free disk at this path (repeatable)")
    p.add_argument("--endpoint", action="append", default=[], help="check that URL/v1/models answers (repeatable)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_resources)
    for name, text in (("check", "verify required paths, executable bits and free disk on every host"),
                       ("show", "summarise resources.json and mark what is usable now")):
        p = racts.add_parser(name, help=text)
        p.add_argument("--resources", default=None, help="resources.json (default: <run-dir>/resources.json)")
        p.add_argument("--host", default=None, help="only this host name")
        add_run_dir(p)
        p.set_defaults(func=cmd_resources)

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
