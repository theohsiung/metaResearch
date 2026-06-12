"""The deterministic step the agent calls — score one design, store it, update the frontier.

This module is the heart of the framework's *deterministic tools* layer (DESIGN
§6.4). It is **not** an orchestrator: there is no looping, no subprocess proposer,
no ``claude -p`` driver. The *agent* decides what design to try (following the
skill); ``runner`` performs the deterministic plumbing for one design:

    build_design (validate first)
      -> experiment.make_evaluator().evaluate(design, bundle trace dir)
      -> experience.record(next_iteration, ...)
      -> update_frontier
      -> classify
      -> append results.tsv row
      -> if commit: ensure_branch(tag) then commit(message)

Everything is **append-only** — we never reset history and never delete prior
experience. A dominated / infeasible / crashed candidate is still recorded and
(optionally) committed, because those traces are exactly what Meta-Harness reads
back next iteration.

``runner`` is also what ``tests/test_runner.py`` drives directly (no agent, no
``claude``), so it must be a pure, importable function with no global state.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Protocol

from .candidates import build_design, validate_candidate
from .experience import Experience
from .frontier import classify, load_frontier, update_frontier
from .interfaces import DesignSpec, EvalResult, Objective, replace
from .kg import write_kg
from .logfmt import ResultsLog
from .thinking import capture_thinking


class Experiment(Protocol):
    """Structural type for a loaded ``prepare.py`` module (DESIGN §4).

    The experiment module is discovered from the current working directory and
    exposes these module-level symbols. We only depend on the subset the runner
    needs; ``prepare.py`` may expose more (``BUDGET``, ``OPERATING``).
    """

    OBJECTIVES: list[Objective]
    BASELINES: list[str]
    DESIGNS_DIR: str

    def make_evaluator(self) -> Any:  # returns an Evaluator (Protocol)
        ...


def _designs_dir(experiment: Any, run_dir: Path) -> Path:
    """Resolve the designs package directory for an experiment.

    ``experiment.DESIGNS_DIR`` may be absolute or relative; relative paths are
    resolved against ``run_dir`` (the directory containing ``prepare.py``).
    Falls back to ``"designs"`` if the symbol is missing.
    """
    raw = getattr(experiment, "DESIGNS_DIR", "designs") or "designs"
    p = Path(raw)
    return p if p.is_absolute() else (run_dir / p)


def _design_src_path(designs_dir: Path, name: str) -> Path:
    """Locate the candidate's source module ``designs/<name>.py``.

    Accepts a bare module name, a dotted ``pkg.mod`` reference, or a path; uses
    the final stem so the verbatim source can be copied into the bundle.
    """
    stem = name.replace("/", ".").split(".")[-1]
    if stem.endswith(".py"):
        stem = stem[:-3]
    return designs_dir / f"{stem}.py"


def _commit_message(
    name: str,
    iteration: int,
    result: EvalResult,
    status: str,
    objectives: list[Objective],
    summary: str,
) -> str:
    """Format the append-only commit message (DESIGN §6.3).

    ``iter<NN> <name>: <obj1>=<v1> <obj2>=<v2> [<status>] — <one-line summary>``
    """
    parts: list[str] = []
    for obj in objectives:
        value = result.scores.get(obj.name)
        if value is None:
            parts.append(f"{obj.name}=NA")
        else:
            parts.append(f"{obj.name}={value:g}")
    scores_str = " ".join(parts) if parts else "(no scores)"
    summary = (summary or "").strip().splitlines()[0] if summary else ""
    summary = summary.strip() or status
    return f"iter{iteration:02d} {name}: {scores_str} [{status}] — {summary}"


def _status_for(result: EvalResult, label: str) -> str:
    """Derive the ledger status from an :class:`EvalResult` and the Pareto label.

    Precedence: crash > infeasible > Pareto label ("frontier" | "dominated").
    """
    if result.error is not None or not result.ok:
        return "crash"
    if not result.feasible:
        return "infeasible"
    return label


def _hypothesis_summary(hypothesis: dict[str, Any] | None) -> str:
    """Extract a one-line human summary from the hypothesis hand-off (DESIGN §7.3).

    Prefers ``change`` (WHAT was changed — the scores + status on the same log
    line already say what happened); ``expected``/``reasoning`` are fallbacks
    for hyp.json files that predate the ``change`` field.
    """
    if not hypothesis:
        return ""
    for key in ("change", "expected", "reasoning", "summary"):
        value = hypothesis.get(key)
        if value:
            return str(value).strip().splitlines()[0]
    return ""


def evaluate_and_record(
    name: str,
    experiment: Any,
    run_dir: Path | str,
    *,
    hypothesis: dict[str, Any] | None = None,
    commit: bool = False,
    tag: str | None = None,
) -> EvalResult:
    """Evaluate one design and append it to the experience ledger (DESIGN §6.4).

    Steps (all deterministic, all append-only):

    1. ``build_design(name, DESIGNS_DIR)`` — validate interface compliance first;
       a validation failure short-circuits to a recorded ``crash`` result.
    2. ``evaluator = experiment.make_evaluator()`` then
       ``evaluator.evaluate(design, bundle/trace)`` — the evaluator never raises.
    3. ``experience.record(next_iteration(), name, design_src, design, result, hypothesis)``.
    4. ``update_frontier(run_dir, OBJECTIVES)`` then ``classify`` this candidate.
    5. Append a ``results.tsv`` row.
    6. If ``commit``: ``ensure_branch(tag)`` then ``commit(message)`` — never reset.
    7. Return the :class:`EvalResult` (the CLI prints scores + heatmap path).

    Args:
        name: Candidate module name under ``DESIGNS_DIR``.
        experiment: Loaded ``prepare.py`` module (see :class:`Experiment`).
        run_dir: Experiment directory (holds ``experience/``, ``results.tsv``, ``frontier.json``).
        hypothesis: Optional reasoning hand-off (DESIGN §7.3), folded into ``hypothesis.md``.
        commit: When True, branch (if ``tag`` given) and commit the bundle append-only.
        tag: Run tag; selects/creates branch ``meta-research/<tag>``.

    Returns:
        The :class:`EvalResult` for the candidate (recorded regardless of outcome).
    """
    run_dir = Path(run_dir).resolve()
    objectives = list(getattr(experiment, "OBJECTIVES", []))
    designs_dir = _designs_dir(experiment, run_dir)

    experience = Experience(run_dir, objectives)
    iteration = experience.next_iteration()
    bundle = experience.bundle_dir(iteration, name)
    trace_dir = bundle / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)

    # 1. Validate interface compliance (import + build + type check) before scoring.
    ok, message = validate_candidate(name, str(designs_dir))
    if not ok:
        result = EvalResult.crashed(f"invalid candidate {name!r}: {message}")
        return _finalize(
            name=name,
            experiment=experiment,
            run_dir=run_dir,
            designs_dir=designs_dir,
            experience=experience,
            iteration=iteration,
            design=DesignSpec(params={}, notes=f"validation failed: {message}"),
            result=result,
            objectives=objectives,
            hypothesis=hypothesis,
            commit=commit,
            tag=tag,
        )

    # 1b. Build the concrete design. validate_candidate already ran build() once,
    #     but we rebuild to obtain the DesignSpec; any unexpected failure is wrapped.
    try:
        design = build_design(name, str(designs_dir))
    except Exception as exc:  # defensive: build_design should not raise post-validation
        result = EvalResult.crashed(f"build_design({name!r}) raised: {exc!r}")
        return _finalize(
            name=name,
            experiment=experiment,
            run_dir=run_dir,
            designs_dir=designs_dir,
            experience=experience,
            iteration=iteration,
            design=DesignSpec(params={}, notes=f"build failed: {exc!r}"),
            result=result,
            objectives=objectives,
            hypothesis=hypothesis,
            commit=commit,
            tag=tag,
        )

    # 2. Evaluate. The Evaluator contract guarantees it never raises, but we guard
    #    against a non-conforming evaluator so the runner itself never crashes.
    try:
        evaluator = experiment.make_evaluator()
        result = evaluator.evaluate(design, trace_dir)
        if not isinstance(result, EvalResult):
            result = EvalResult.crashed(
                f"evaluator returned {type(result).__name__}, expected EvalResult"
            )
    except Exception as exc:  # noqa: BLE001 — runner must survive a misbehaving evaluator
        result = EvalResult.crashed(f"evaluator raised: {exc!r}")

    return _finalize(
        name=name,
        experiment=experiment,
        run_dir=run_dir,
        designs_dir=designs_dir,
        experience=experience,
        iteration=iteration,
        design=design,
        result=result,
        objectives=objectives,
        hypothesis=hypothesis,
        commit=commit,
        tag=tag,
    )


def _finalize(
    *,
    name: str,
    experiment: Any,
    run_dir: Path,
    designs_dir: Path,
    experience: Experience,
    iteration: int,
    design: DesignSpec,
    result: EvalResult,
    objectives: list[Objective],
    hypothesis: dict[str, Any] | None,
    commit: bool,
    tag: str | None,
) -> EvalResult:
    """Classify -> record -> frontier -> tsv -> (opt) commit. Shared tail for all paths."""
    # 3. Resolve the status BEFORE recording, so the stored bundle (result.json +
    #    hypothesis.md) carries it. Classify against the frontier as it stands
    #    *before* this candidate: a candidate is "frontier" iff no existing frontier
    #    member dominates it (dominance is transitive, so the pre-existing frontier
    #    is sufficient). This is status-at-eval-time — consistent with the
    #    append-only ledger, which never rewrites a past bundle.
    if result.feasible and result.ok:
        try:
            prev_pareto = load_frontier(run_dir).get("pareto", [])
            label = classify(result.scores, prev_pareto, objectives)
        except Exception:  # noqa: BLE001
            label = "dominated"
    else:
        label = "dominated"
    status = _status_for(result, label)

    # 4. Record the experience bundle with the resolved status folded in.
    src_path = _design_src_path(designs_dir, name)
    design_src_path = src_path if src_path.is_file() else None
    bundle = experience.bundle_dir(iteration, name)
    try:
        experience.record(
            iteration=iteration,
            name=name,
            design_src_path=design_src_path,
            design=design,
            result=result,
            hypothesis={**(hypothesis or {}), "status": status},
        )
    except Exception as exc:  # noqa: BLE001 — never lose the result over a store hiccup
        # Best-effort: ensure the source is at least copied so the trace is not empty.
        _safe_copy_source(design_src_path, bundle)
        result = _attach_metadata(result, {"record_error": repr(exc)})
    else:
        # Point the returned result's artifacts at their real ledger location
        # (the bundle's trace/), so callers/the CLI resolve a path that exists
        # instead of the evaluator's bare basename. This closes the loop: the agent
        # is told to re-Read the heatmap, so the path must be correct.
        result = _relocate_artifacts(result, bundle, run_dir)

    # 4.5 Best-effort: harvest the proposer's thinking blocks for this iteration
    #     from the session transcript into the bundle (Meta-Harness "store
    #     everything" — transcripts are not durable, the ledger copy is). Runs
    #     before the commit so the trace lands in the same commit as its bundle.
    try:
        capture_thinking(run_dir, bundle, name=name)
    except Exception as exc:  # noqa: BLE001 — trace capture must never sink the step
        result = _attach_metadata(result, {"thinking_error": repr(exc)})

    # 5. Recompute and persist the full Pareto frontier (now including this candidate).
    try:
        update_frontier(run_dir, objectives)
    except Exception as exc:  # noqa: BLE001 — frontier maths must not sink the step
        result = _attach_metadata(result, {"frontier_error": repr(exc)})

    # 5b. Rebuild the derived knowledge graph (DESIGN §7.6) — same
    #     recompute-from-ledger model as the frontier, and like it, a failure
    #     must never sink the step.
    try:
        write_kg(run_dir)
    except Exception as exc:  # noqa: BLE001
        result = _attach_metadata(result, {"kg_error": repr(exc)})

    # 6. Append a results.tsv row (header written lazily by ResultsLog).
    hyp_summary = _hypothesis_summary(hypothesis)
    log = ResultsLog(run_dir / "results.tsv", objectives)
    if not (run_dir / "results.tsv").exists():
        log.write_header()
    log.append(iteration, name, status, result, hyp_summary)

    # 7. Append-only git commit (optional). Branch only if a tag was supplied.
    #    The returned SHA is the bundle's join key to the full-run snapshot; it is
    #    annotated into the bundle's result.json and the returned metadata.
    if commit:
        try:
            if tag:
                experience.ensure_branch(tag)
            message = _commit_message(
                name, iteration, result, status, objectives, hyp_summary
            )
            sha = experience.commit(message)
            if sha:
                experience.annotate_commit(bundle, sha)
                result = _attach_metadata(result, {"commit": sha})
        except Exception as exc:  # noqa: BLE001 — a commit failure must not lose the result
            result = _attach_metadata(result, {"commit_error": repr(exc)})

    # 8. Return the result; the CLI prints scores + heatmap path + frontier verdict.
    return result


def _attach_metadata(result: EvalResult, extra: dict[str, Any]) -> EvalResult:
    """Return a new EvalResult with ``extra`` merged into metadata (immutable update)."""
    merged = dict(result.metadata)
    merged.update(extra)
    return replace(result, metadata=merged)


def _relocate_artifacts(result: EvalResult, bundle: Path, run_dir: Path) -> EvalResult:
    """Rewrite artifact paths to the persisted bundle ``trace/`` location.

    The evaluator returns bare names (e.g. ``"heatmap.png"``) for files it wrote
    into its ``out_dir`` (the bundle trace dir). :func:`experience.record` keeps
    them under ``<bundle>/trace/<name>``. We rewrite to paths *relative to
    run_dir* when possible (so the ledger is portable) else absolute, so any
    consumer resolves a path that actually exists.
    """
    if not result.artifacts:
        return result
    trace = bundle / "trace"
    relocated: dict[str, str] = {}
    for key, raw in result.artifacts.items():
        dest = trace / Path(raw).name
        try:
            relocated[key] = str(dest.relative_to(run_dir))
        except ValueError:
            relocated[key] = str(dest)
    return replace(result, artifacts=relocated)


def _safe_copy_source(src_path: Path | None, bundle: Path) -> None:
    """Copy the candidate source into the bundle, swallowing any IO error."""
    if not src_path or not src_path.is_file():
        return
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_path, bundle / "design.py")
    except OSError:
        pass


def seed_baselines(
    experiment: Any,
    run_dir: Path | str,
    *,
    commit: bool = False,
) -> list[EvalResult]:
    """Evaluate every ``experiment.BASELINES`` design once to seed the population (Phase 0).

    Each baseline is run through :func:`evaluate_and_record` with a standard
    hypothesis marking it as the seed population. The frontier and ``results.tsv``
    are built incrementally; nothing is discarded. Returns the per-baseline results
    in declaration order.

    Args:
        experiment: Loaded ``prepare.py`` module exposing ``BASELINES``.
        run_dir: Experiment directory.
        commit: When True, each baseline bundle is committed append-only.
    """
    run_dir = Path(run_dir).resolve()
    baselines = list(getattr(experiment, "BASELINES", []))
    results: list[EvalResult] = []
    for name in baselines:
        hypothesis = {
            "axis": "baseline",
            "parent": "",
            "expected": "seed population (Phase 0)",
            "reasoning": f"Baseline {name!r} evaluated to seed the Pareto frontier.",
        }
        result = evaluate_and_record(
            name,
            experiment,
            run_dir,
            hypothesis=hypothesis,
            commit=commit,
            tag=None,
        )
        results.append(result)
    return results


__all__ = ["evaluate_and_record", "seed_baselines", "Experiment"]
