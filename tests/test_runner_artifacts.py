"""The returned result's artifact paths must point at the persisted bundle trace.

Regression test: the evaluator returns a bare basename (``heatmap.png``); the runner
must rewrite it to the real ledger location so the agent (told to re-Read the heatmap)
gets a path that exists. Previously the CLI resolved it against run_dir -> a missing file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from meta_research.evaluators import NumericalEvaluator
from meta_research.interfaces import Objective
from meta_research.runner import evaluate_and_record

OBJS = [Objective("cost", "min", "")]


def _simulate(params: dict, out_dir):
    # Write an artifact into out_dir (as a real evaluator does) and return its basename.
    (Path(out_dir) / "plot.png").write_bytes(b"\x89PNG\r\n")
    return {"cost": float(params.get("x", 1.0))}, {"k": 1}, {"plot": "plot.png"}


class _Experiment:
    OBJECTIVES = OBJS
    BASELINES = ["d0"]

    def __init__(self, designs_dir: str) -> None:
        self.DESIGNS_DIR = designs_dir

    @staticmethod
    def make_evaluator() -> NumericalEvaluator:
        return NumericalEvaluator(OBJS, _simulate)


def test_returned_artifact_resolves_to_existing_bundle_file(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    designs = tmp_path / "designs"
    designs.mkdir()
    (designs / "__init__.py").write_text("", encoding="utf-8")
    (designs / "d0.py").write_text(
        "from meta_research.interfaces import DesignSpec\n"
        "def build():\n    return DesignSpec(params={'x': 2.0})\n",
        encoding="utf-8",
    )

    result = evaluate_and_record("d0", _Experiment(str(designs)), tmp_path, commit=False)
    assert result.ok

    rel = result.artifacts["plot"]
    # path is relative to run_dir and lives under the bundle trace dir
    assert "experience" in rel and "trace" in rel
    resolved = tmp_path / rel
    assert resolved.is_file(), f"artifact path does not resolve to a file: {resolved}"
