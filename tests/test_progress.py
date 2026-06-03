"""Tests for the progress plotter (the autoresearch ``progress.png`` analog)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meta_research.experience import Experience
from meta_research.interfaces import DesignSpec, EvalResult, Objective
from meta_research.progress import gather_rows, plot_progress

OBJECTIVES = [
    Objective("thermal_resistance", "min", "K/W", 1.0),
    Objective("pressure_drop", "min", "Pa", 0.3),
]


def _record(exp: Experience, i: int, name: str, scores: dict, *, feasible=True, status="dominated"):
    """Write a minimal bundle via the real store so the plotter reads it back."""
    result = EvalResult(scores=scores, feasible=feasible, metadata={}, artifacts={})
    exp.record(
        iteration=i,
        name=name,
        design_src_path="/nonexistent.py",  # store tolerates a missing source
        design=DesignSpec(params={"k": i}),
        result=result,
        hypothesis={"axis": "geometry", "reasoning": "test", "status": status},
    )


@pytest.fixture
def seeded_run(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    exp = Experience(tmp_path, OBJECTIVES)
    _record(exp, 0, "a", {"thermal_resistance": 0.10, "pressure_drop": 20.0})
    _record(exp, 1, "b", {"thermal_resistance": 0.07, "pressure_drop": 40.0})  # frontier
    _record(exp, 2, "c", {"thermal_resistance": 0.12, "pressure_drop": 15.0})  # frontier
    _record(exp, 3, "d", {"thermal_resistance": 0.20, "pressure_drop": 90.0},  # dominated
            status="dominated")
    return tmp_path


def test_gather_rows_orders_and_flags_feasibility(seeded_run: Path) -> None:
    rows = gather_rows(seeded_run, OBJECTIVES)
    assert [r["name"] for r in rows] == ["a", "b", "c", "d"]
    assert [r["idx"] for r in rows] == [1, 2, 3, 4]
    assert all(r["feasible"] for r in rows)


def test_plot_progress_writes_expected_pngs(seeded_run: Path) -> None:
    written = plot_progress(seeded_run, OBJECTIVES)
    names = {p.name for p in written}
    # one curve per objective + one pareto scatter + the headline montage
    assert "progress_thermal_resistance.png" in names
    assert "progress_pressure_drop.png" in names
    assert "progress_pareto.png" in names
    assert "progress.png" in names
    for p in written:
        assert p.is_file() and p.stat().st_size > 0


def test_plot_progress_empty_ledger_returns_empty(tmp_path: Path) -> None:
    # No experience/ dir at all -> no plots, no exception.
    assert plot_progress(tmp_path, OBJECTIVES) == []


def test_single_objective_has_no_pareto_scatter(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    single = [Objective("cost", "min", "")]
    exp = Experience(tmp_path, single)
    _record(exp, 0, "a", {"cost": 5.0})
    _record(exp, 1, "b", {"cost": 3.0})
    written = plot_progress(tmp_path, single)
    names = {p.name for p in written}
    assert "progress_cost.png" in names
    assert "progress.png" in names
    assert not any("pareto" in n for n in names)
