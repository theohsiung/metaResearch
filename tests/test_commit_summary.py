"""The commit subject / TSV summary prefers the hypothesis ``change`` line.

The one-line summary should say WHAT was changed — the scores and ``[status]``
on the same line already say what happened. ``expected`` / ``reasoning`` remain
as fallbacks so older ``hyp.json`` files keep working.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from meta_research.evaluators import NumericalEvaluator
from meta_research.interfaces import Objective
from meta_research.runner import evaluate_and_record

OBJS = [Objective("cost", "min", "")]


def _simulate(params: dict, out_dir):
    return {"cost": float(params.get("x", 1.0))}, {"k": 1}, {}


class _Experiment:
    OBJECTIVES = OBJS
    BASELINES = ["d0"]

    def __init__(self, designs_dir: str) -> None:
        self.DESIGNS_DIR = designs_dir

    @staticmethod
    def make_evaluator() -> NumericalEvaluator:
        return NumericalEvaluator(OBJS, _simulate)


def _git(run_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=run_dir, check=True, capture_output=True, text=True
    ).stdout.strip()


def _setup_run_dir(tmp_path: Path) -> _Experiment:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test Runner")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    designs = tmp_path / "designs"
    designs.mkdir()
    (designs / "__init__.py").write_text("", encoding="utf-8")
    (designs / "d0.py").write_text(
        "from meta_research.interfaces import DesignSpec\n"
        "def build():\n    return DesignSpec(params={'x': 2.0})\n",
        encoding="utf-8",
    )
    return _Experiment(str(designs))


def test_commit_subject_prefers_change_line(tmp_path: Path) -> None:
    experiment = _setup_run_dir(tmp_path)
    hypothesis = {
        "axis": "arrangement",
        "parent": "baseline",
        "change": "inline -> staggered pin rows over the outlet half",
        "expected": "cost down ~8%",
        "reasoning": "The heatmap showed a hot band over the outlet half.",
    }
    result = evaluate_and_record(
        "d0", experiment, tmp_path, hypothesis=hypothesis, commit=True
    )
    assert result.ok
    subject = _git(tmp_path, "log", "-1", "--format=%s")
    assert "inline -> staggered pin rows" in subject
    assert "cost down ~8%" not in subject, "the prediction belongs in hypothesis.md"


def test_commit_subject_falls_back_to_expected(tmp_path: Path) -> None:
    experiment = _setup_run_dir(tmp_path)
    hypothesis = {
        "axis": "arrangement",
        "parent": "baseline",
        "expected": "cost down ~8%",
        "reasoning": "Older hyp.json without a change line.",
    }
    result = evaluate_and_record(
        "d0", experiment, tmp_path, hypothesis=hypothesis, commit=True
    )
    assert result.ok
    subject = _git(tmp_path, "log", "-1", "--format=%s")
    assert "cost down ~8%" in subject


def test_results_tsv_summary_prefers_change_line(tmp_path: Path) -> None:
    experiment = _setup_run_dir(tmp_path)
    hypothesis = {
        "change": "halve the channel pitch",
        "expected": "cost down",
        "reasoning": "why",
    }
    evaluate_and_record("d0", experiment, tmp_path, hypothesis=hypothesis, commit=False)
    tsv = (tmp_path / "results.tsv").read_text(encoding="utf-8")
    assert "halve the channel pitch" in tsv
