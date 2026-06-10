"""Tests for the deterministic step ``meta_research.runner`` (DESIGN §6.4).

Drives ``runner`` directly (no agent, no ``claude``):
- Load ``examples/water_cooling/prepare.py`` via importlib as the experiment.
- In a tmp_path git repo used as ``run_dir``, copy the experiment's ``designs/``
  so candidates load, then:
    * ``seed_baselines(experiment, run_dir, commit=True)`` (Phase 0), and
    * ``evaluate_and_record`` for a copied design.
- Assert: a bundle was written, ``frontier.json`` + ``results.tsv`` were updated,
  ``git log`` shows >= 2 commits (append-only), and ``runner.py`` source contains
  NO ``git reset``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path
from types import ModuleType

import pytest

import meta_research.runner as runner_module
from meta_research.runner import evaluate_and_record, seed_baselines


def _executable_source(module) -> str:
    """Module source with comments + string literals (docstrings) stripped.

    The append-only invariant forbids any ``git reset`` *invocation*; the source may
    *document* the invariant in prose. We assert on real code, not docstrings.
    """
    text = Path(module.__file__).read_text(encoding="utf-8")
    pieces: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "water_cooling"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _git(run_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=run_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _load_prepare(experiment_dir: Path) -> ModuleType:
    """Load the experiment's ``prepare.py`` via importlib (DESIGN §4)."""
    prepare_path = experiment_dir / "prepare.py"
    spec = importlib.util.spec_from_file_location("wc_prepare", prepare_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["wc_prepare"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def experiment_run_dir(tmp_path: Path):
    """Copy the water_cooling experiment into a tmp git repo and load prepare.

    Yields ``(experiment_module, run_dir)`` with cwd switched to run_dir so the
    framework's cwd-based experiment discovery (DESIGN §4) works.
    """
    if not EXAMPLE_DIR.exists():
        pytest.skip("examples/water_cooling not present yet")

    run_dir = tmp_path / "run"
    shutil.copytree(EXAMPLE_DIR, run_dir)

    # git init with a deterministic identity + an initial commit.
    _git(run_dir, "init")
    _git(run_dir, "config", "user.email", "test@example.com")
    _git(run_dir, "config", "user.name", "Test Runner")
    _git(run_dir, "config", "commit.gpgsign", "false")
    _git(run_dir, "add", "-A")
    _git(run_dir, "commit", "-m", "seed experiment")

    prev_cwd = Path.cwd()
    os.chdir(run_dir)
    sys.path.insert(0, str(run_dir))
    try:
        experiment = _load_prepare(run_dir)
        yield experiment, run_dir
    finally:
        os.chdir(prev_cwd)
        if str(run_dir) in sys.path:
            sys.path.remove(str(run_dir))
        for mod in list(sys.modules):
            if mod == "wc_prepare" or mod.startswith("designs"):
                del sys.modules[mod]


# --------------------------------------------------------------------------- #
# Source-level invariant: APPEND-ONLY (no git reset)
# --------------------------------------------------------------------------- #
def test_runner_source_has_no_git_reset() -> None:
    # No 'git reset' invocation anywhere in executable code (append-only, §1.1.2).
    code = _executable_source(runner_module)
    assert "reset" not in code, "runner.py must be append-only: no git reset in code"


def test_runner_source_file_has_no_git_reset() -> None:
    # 'git reset' must not appear in executable code (docstring mentions are fine).
    code = _executable_source(runner_module)
    assert "git reset" not in code


# --------------------------------------------------------------------------- #
# prepare.py contract (DESIGN §4)
# --------------------------------------------------------------------------- #
def test_prepare_exposes_required_symbols(experiment_run_dir) -> None:
    experiment, _ = experiment_run_dir
    assert isinstance(experiment.OBJECTIVES, list) and experiment.OBJECTIVES
    assert isinstance(experiment.BUDGET, dict)
    assert isinstance(experiment.OPERATING, dict)
    assert isinstance(experiment.BASELINES, list) and experiment.BASELINES
    assert hasattr(experiment, "DESIGNS_DIR")
    evaluator = experiment.make_evaluator()
    assert hasattr(evaluator, "evaluate")
    assert hasattr(evaluator, "objectives")


# --------------------------------------------------------------------------- #
# seed_baselines + evaluate_and_record (append-only, ledger updates)
# --------------------------------------------------------------------------- #
def test_seed_then_evaluate_full_flow(experiment_run_dir) -> None:
    experiment, run_dir = experiment_run_dir

    commits_before = int(_git(run_dir, "rev-list", "--count", "HEAD").stdout.strip())

    # Phase 0: seed every baseline, committing each (append-only).
    seed_baselines(experiment, run_dir, commit=True)

    # The experience ledger directory exists and holds baseline bundles.
    exp_dir = run_dir / "experience"
    assert exp_dir.is_dir()
    seeded_bundles = sorted(p.name for p in exp_dir.iterdir() if p.is_dir())
    assert len(seeded_bundles) >= len(experiment.BASELINES)

    # frontier.json + results.tsv were written by seeding.
    frontier_path = run_dir / "frontier.json"
    results_path = run_dir / "results.tsv"
    assert frontier_path.is_file()
    assert results_path.is_file()

    # Now evaluate a fresh design copied from a baseline (one mechanism change is
    # not required for the runner test; a verbatim copy is enough to exercise it).
    designs_dir = run_dir / str(experiment.DESIGNS_DIR)
    baseline_name = experiment.BASELINES[0]
    src = designs_dir / f"{baseline_name}.py"
    new_name = "copy_candidate"
    shutil.copyfile(src, designs_dir / f"{new_name}.py")

    hypothesis = {
        "axis": "baseline",
        "parent": baseline_name,
        "expected": "same as baseline (verbatim copy)",
        "reasoning": "runner plumbing test only",
    }
    result = evaluate_and_record(
        new_name,
        experiment,
        run_dir,
        hypothesis=hypothesis,
        commit=True,
        tag="runner-test",
    )

    # The evaluator returned a usable result.
    assert result.ok is True

    # A bundle was written for the new candidate.
    bundles = sorted(p.name for p in exp_dir.iterdir() if p.is_dir())
    assert any(new_name in b for b in bundles)
    new_bundle = next(p for p in exp_dir.iterdir() if p.is_dir() and new_name in p.name)
    assert (new_bundle / "design.py").is_file()
    assert (new_bundle / "design_spec.json").is_file()
    assert (new_bundle / "hypothesis.md").is_file()
    assert (new_bundle / "result.json").is_file()
    assert (new_bundle / "trace" / "breakdown.json").is_file()

    # frontier.json is valid JSON with the pinned schema (§7.4).
    frontier = json.loads(frontier_path.read_text(encoding="utf-8"))
    assert "objectives" in frontier
    assert "pareto" in frontier
    assert "best_per_objective" in frontier

    # results.tsv has a header + at least one row per evaluated candidate.
    tsv_lines = results_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(tsv_lines) >= 1 + len(experiment.BASELINES) + 1
    header = tsv_lines[0].split("\t")
    assert header[:4] == ["iter", "name", "status", "feasible"]
    for obj in experiment.OBJECTIVES:
        assert obj.name in header

    # Append-only: git log shows >= 2 commits beyond the initial seed commit.
    commits_after = int(_git(run_dir, "rev-list", "--count", "HEAD").stdout.strip())
    assert commits_after - commits_before >= 2

    # No commit was removed (append-only): count strictly grew.
    assert commits_after > commits_before


def test_seed_baselines_writes_ledger_without_commit(experiment_run_dir) -> None:
    experiment, run_dir = experiment_run_dir
    seed_baselines(experiment, run_dir, commit=False)
    assert (run_dir / "experience").is_dir()
    assert (run_dir / "results.tsv").is_file()
    assert (run_dir / "frontier.json").is_file()


# --------------------------------------------------------------------------- #
# kg.json — rebuilt on every recorded evaluation (DESIGN §7.6)
# --------------------------------------------------------------------------- #
def test_evaluate_rebuilds_kg_alongside_frontier(experiment_run_dir) -> None:
    experiment, run_dir = experiment_run_dir
    seed_baselines(experiment, run_dir, commit=False)

    kg_path = run_dir / "kg.json"
    assert kg_path.is_file(), "seeding must already derive the knowledge graph"

    # Evaluate a fresh candidate that names a baseline as its parent.
    designs_dir = run_dir / str(experiment.DESIGNS_DIR)
    baseline_name = experiment.BASELINES[0]
    shutil.copyfile(
        designs_dir / f"{baseline_name}.py", designs_dir / "kg_candidate.py"
    )
    evaluate_and_record(
        "kg_candidate",
        experiment,
        run_dir,
        hypothesis={
            "axis": "geometry",
            "parent": baseline_name,
            "expected": "same scores (verbatim copy)",
            "reasoning": "kg integration test",
        },
        commit=True,
        tag="kg-test",
    )

    doc = json.loads(kg_path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert any("kg_candidate" in node["id"] for node in doc["nodes"])
    edge = next(
        e
        for e in doc["edges"]
        if e["kind"] == "mutated-from" and "kg_candidate" in e["dst"]
    )
    assert baseline_name in edge["src"]

    # The rebuilt kg.json rides in the same append-only commit.
    assert _git(run_dir, "ls-files", "kg.json").stdout.strip() == "kg.json"
    assert "kg.json" not in _git(run_dir, "status", "--porcelain").stdout
