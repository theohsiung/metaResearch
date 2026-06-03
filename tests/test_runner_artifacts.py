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


def _statuses(run_dir: Path) -> dict[str, str]:
    """Read each bundle's stored status from result.json + hypothesis.md."""
    import json as _json
    import re as _re

    out: dict[str, str] = {}
    for b in sorted((run_dir / "experience").iterdir()):
        name = b.name.split("_", 1)[1]
        rj = _json.loads((b / "result.json").read_text())
        hm = (b / "hypothesis.md").read_text()
        m = _re.search(r"^status:\s*(.+)$", hm, _re.M)
        out[name] = (rj.get("status"), m.group(1).strip() if m else None)
    return out


def test_bundle_status_reflects_frontier_membership_at_eval_time(tmp_path: Path) -> None:
    """A frontier-advancing design records status 'frontier'; a dominated one 'dominated'.

    Regression: the bundle previously always stored 'dominated' because the runner
    never passed the classified status into record().
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    designs = tmp_path / "designs"
    designs.mkdir()
    (designs / "__init__.py").write_text("", encoding="utf-8")
    for mod, x in (("d_good", 3.0), ("d_bad", 5.0)):  # lower cost is better (min)
        (designs / f"{mod}.py").write_text(
            "from meta_research.interfaces import DesignSpec\n"
            f"def build():\n    return DesignSpec(params={{'x': {x}}})\n",
            encoding="utf-8",
        )
    exp = _Experiment(str(designs))
    # Evaluate the better design first -> frontier; then the worse -> dominated at eval time.
    evaluate_and_record("d_good", exp, tmp_path, commit=False)
    evaluate_and_record("d_bad", exp, tmp_path, commit=False)

    st = _statuses(tmp_path)
    assert st["d_good"] == ("frontier", "frontier"), st
    assert st["d_bad"] == ("dominated", "dominated"), st
