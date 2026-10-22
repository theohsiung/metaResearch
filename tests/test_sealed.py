"""Sealed holdout scores (meta_research.sealed): never visible in the ledger, revealed only by finalize."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from meta_research.cli import main as cli_main
from meta_research.interfaces import EvalResult, Objective
from meta_research.runner import evaluate_and_record
from meta_research.sealed import finalize, split_sealed

PREPARE = '''
from meta_research import Objective
from meta_research.evaluators import NumericalEvaluator

OBJECTIVES = [Objective("acc", direction="max")]
BUDGET = {}
OPERATING = {}
BASELINES = []
DESIGNS_DIR = "designs"


HOLDOUT = {SEALED}  # the evaluator's own holdout table; never part of a design


def _simulate(params, out_dir):
    if params.get("crash"):
        raise RuntimeError("boom")
    meta = {"note": "visible", "sealed_scores": HOLDOUT[params["id"]]}
    return {"acc": params["acc"]}, meta, {}


def make_evaluator():
    return NumericalEvaluator(OBJECTIVES, _simulate)
'''

# search acc vs sealed holdout: the best search score is NOT the best on the holdout
DESIGNS = {
    "lucky": {"id": "lucky", "acc": 0.95},   # overfit to the search split
    "solid": {"id": "solid", "acc": 0.93},
    "weak": {"id": "weak", "acc": 0.70},
    "messy": {"id": "messy", "acc": 0.60},
    "broken": {"id": "broken", "acc": 0.99, "crash": True},
}
SEALED = {
    "lucky": {"acc_holdout": 0.611111},
    "solid": {"acc_holdout": 0.877777},
    "weak": {"acc_holdout": 0.655555},
    "messy": {"acc_holdout": 0.5, "bad": "nan-string"},
    "broken": {"acc_holdout": 0.99},
}


def _git(run_dir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=run_dir, check=True, capture_output=True, text=True)


@pytest.fixture()
def run_dir(tmp_path: Path):
    run = tmp_path / "run"
    (run / "designs").mkdir(parents=True)
    (run / "prepare.py").write_text(textwrap.dedent(PREPARE).replace("{SEALED}", repr(SEALED)), encoding="utf-8")
    (run / "designs" / "__init__.py").write_text("", encoding="utf-8")
    for name, params in DESIGNS.items():
        (run / "designs" / f"{name}.py").write_text(
            "from meta_research import DesignSpec\n\n\ndef build():\n"
            f"    return DesignSpec(params={params!r})\n", encoding="utf-8")
    _git(run, "init")
    _git(run, "config", "user.email", "t@example.com")
    _git(run, "config", "user.name", "T")
    _git(run, "config", "commit.gpgsign", "false")
    _git(run, "add", "-A")
    _git(run, "commit", "-m", "seed")
    prev = Path.cwd()
    os.chdir(run)
    sys.path.insert(0, str(run))
    spec = importlib.util.spec_from_file_location("sealed_prepare", run / "prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        yield module, run
    finally:
        os.chdir(prev)
        sys.path.remove(str(run))
        for mod in [m for m in sys.modules if m.startswith("designs")]:
            del sys.modules[mod]


def _evaluate_all(experiment, run: Path) -> None:
    for name in DESIGNS:
        evaluate_and_record(name, experiment, run, hypothesis={"axis": "test", "change": name}, commit=True)


def test_split_sealed_strips_and_validates():
    result = EvalResult(scores={"acc": 1.0}, metadata={"sealed_scores": {"h": 0.5, "x": "no", "y": float("inf")}, "k": 1})
    visible, sealed, error = split_sealed(result)
    assert "sealed_scores" not in visible.metadata and visible.metadata["k"] == 1
    assert sealed == {"h": 0.5}
    assert "x" in error and "y" in error
    assert split_sealed(EvalResult(scores={"acc": 1.0})) == (EvalResult(scores={"acc": 1.0}), None, None)


def test_sealed_scores_never_reach_the_visible_ledger(run_dir):
    experiment, run = run_dir
    _evaluate_all(experiment, run)
    visible_files = [run / "results.tsv", run / "frontier.json", run / "kg.json"]
    visible_files += [p for p in (run / "experience").rglob("*") if p.is_file()]
    for path in visible_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        # values and the holdout key; a crash traceback may quote the evaluator's own source, so the
        # metadata key itself is checked structurally below instead of as text
        for secret in ("0.611111", "0.877777", "0.655555", "acc_holdout"):
            assert secret not in text, f"{secret} leaked into {path.relative_to(run)}"
    for result_json in (run / "experience").glob("*/result.json"):
        assert "sealed_scores" not in json.loads(result_json.read_text())["metadata"]
    for breakdown in (run / "experience").glob("*/trace/breakdown.json"):
        assert "sealed_scores" not in json.loads(breakdown.read_text())
    stored = sorted(p.name for p in (run / ".sealed").iterdir())
    # the crashed candidate never produced metadata, so it has nothing sealed
    assert stored == ["000_lucky.json", "001_solid.json", "002_weak.json", "003_messy.json"]
    assert json.loads((run / ".sealed" / "001_solid.json").read_text())["scores"] == {"acc_holdout": 0.877777}
    messy = json.loads((run / "experience" / "003_messy" / "result.json").read_text())
    assert "bad" in messy["metadata"]["sealed_error"]
    assert messy["metadata"]["sealed_stored"] == 1
    assert json.loads((run / "experience" / "001_solid" / "result.json").read_text())["metadata"]["note"] == "visible"


def test_finalize_picks_on_the_holdout_and_logs_every_reveal(run_dir):
    experiment, run = run_dir
    _evaluate_all(experiment, run)
    record = finalize(run, experiment.OBJECTIVES, top=2, by="acc", pick="acc_holdout")
    assert [c["bundle"] for c in record["candidates"]] == ["000_lucky", "001_solid"]  # crashed 004 excluded
    assert record["chosen"] == "001_solid"  # not the best search score
    assert record["pick_direction"] == "max"
    finalize(run, experiment.OBJECTIVES, top=3, pick="acc_holdout")
    lines = (run / "finalize.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["top"] == 3


def test_finalize_ranks_by_a_signed_weighted_sum(run_dir):
    experiment, run = run_dir
    _evaluate_all(experiment, run)
    record = finalize(run, experiment.OBJECTIVES, top=2, weights={"acc": 0.5}, pick="acc_holdout")
    assert record["by"] == "weighted" and record["weights"] == {"acc": 0.5}
    assert [c["bundle"] for c in record["candidates"]] == ["000_lucky", "001_solid"]
    lower_is_better = [Objective("acc", direction="min")]
    record = finalize(run, lower_is_better, top=2, weights={"acc": 1.0}, pick="acc_holdout")
    assert [c["bundle"] for c in record["candidates"]] == ["003_messy", "002_weak"]
    with pytest.raises(ValueError, match="unknown"):
        finalize(run, experiment.OBJECTIVES, weights={"cost": 1.0}, pick="acc_holdout")
    with pytest.raises(ValueError, match="pick"):
        finalize(run, experiment.OBJECTIVES, weights={"acc": 1.0})
    assert cli_main(["finalize", "--top", "2", "--weights", "acc=0.6", "--pick", "acc_holdout"]) == 0
    assert cli_main(["finalize", "--weights", "acc", "--pick", "acc_holdout"]) == 2


def test_finalize_never_picks_a_reference_point(run_dir):
    experiment, run = run_dir
    _evaluate_all(experiment, run)
    record = finalize(run, experiment.OBJECTIVES, top=2, by="acc", pick="acc_holdout", exclude=["lucky"])
    assert [c["bundle"] for c in record["candidates"]] == ["001_solid", "002_weak"]


def test_finalize_cli_reports_and_commits(run_dir, capsys):
    experiment, run = run_dir
    _evaluate_all(experiment, run)
    assert cli_main(["finalize", "--top", "2", "--pick", "acc_holdout", "--commit"]) == 0
    out = capsys.readouterr().out
    assert "chosen: 001_solid" in out
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=run, capture_output=True, text=True).stdout
    assert "finalize: reveal sealed acc_holdout" in log
    assert cli_main(["finalize", "--pick", "missing_key"]) == 1
