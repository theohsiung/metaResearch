"""``evaluate_and_record(commit=True)`` annotates the bundle with the ledger SHA.

The commit SHA is the join key between a bundle and the full-run snapshot that
contains it (autoresearch records the same key in its results.tsv). The
annotation necessarily lands *after* the commit, so the git-tracked copy of
``result.json`` lags one commit behind the filesystem copy — the SHA names the
commit that contains this bundle, which cannot contain itself.
"""

from __future__ import annotations

import json
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


def test_commit_sha_annotated_in_bundle_and_metadata(tmp_path: Path) -> None:
    experiment = _setup_run_dir(tmp_path)

    result = evaluate_and_record("d0", experiment, tmp_path, commit=True)
    assert result.ok

    head = _git(tmp_path, "rev-parse", "--short", "HEAD")
    doc = json.loads(
        (tmp_path / "experience" / "000_d0" / "result.json").read_text(encoding="utf-8")
    )
    assert doc.get("commit") == head, "bundle result.json must carry the ledger SHA"
    assert result.metadata.get("commit") == head, "returned metadata must carry the SHA"


def test_no_commit_means_no_sha(tmp_path: Path) -> None:
    experiment = _setup_run_dir(tmp_path)

    result = evaluate_and_record("d0", experiment, tmp_path, commit=False)
    assert result.ok

    doc = json.loads(
        (tmp_path / "experience" / "000_d0" / "result.json").read_text(encoding="utf-8")
    )
    assert "commit" not in doc
    assert "commit" not in result.metadata
