"""Tests for the derived hypothesis hit-rate (DESIGN §6.5/§7.6).

``build_calibration`` aggregates the per-objective ``prediction`` verdicts that
``kg.build_kg`` derives on each mutated-from edge into a run-level report:
overall, per-objective, and per-axis. ``hit_rate = confirmed / (confirmed +
refuted)`` — ``inconclusive`` verdicts are excluded from the denominator. It is a
pure ledger read (no ``prepare.py``), recomputed from the bundles, persisting
nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meta_research import build_calibration
from meta_research.experience import Experience
from meta_research.interfaces import DesignSpec, EvalResult, Objective

OBJECTIVES: list[Objective] = [
    Objective("thermal_resistance", "min", "K/W"),
    Objective("pressure_drop", "min", "Pa"),
]


def _record(
    run_dir: Path,
    iteration: int,
    name: str,
    *,
    scores: dict[str, float],
    parent: str = "",
    axis: str = "geometry",
    expected: dict[str, Any] | str | None = None,
) -> None:
    """Write a real schema-7.1 bundle through the store (the honest fixture)."""
    exp = Experience(run_dir, OBJECTIVES)
    exp.record(
        iteration=iteration,
        name=name,
        design_src_path=run_dir / "no_such.py",
        design=DesignSpec(params={"i": iteration}),
        result=EvalResult(scores=dict(scores), feasible=True),
        hypothesis={
            "axis": axis,
            "parent": parent,
            "expected": expected if expected is not None else "",
            "reasoning": "test bundle",
            "status": "dominated",
        },
    )


def _seed_mixed_ledger(run_dir: Path) -> None:
    """A base plus three children producing confirmed / refuted / inconclusive.

    base:      tr=0.06, pd=500
    down_ok:   predict tr↓ & pd↑; actual tr=0.05 (↓ confirmed), pd=550 (↑ confirmed)  [axis flow]
    up_wrong:  predict tr↓;       actual tr=0.07 (↑ refuted)                           [axis flow]
    tiny:      predict tr↓;       actual tr=0.05997 (|Δ|/0.06=5e-4 < dead-band)        [axis material]
    """
    _record(run_dir, 0, "base", scores={"thermal_resistance": 0.06, "pressure_drop": 500.0})
    _record(
        run_dir, 1, "down_ok",
        scores={"thermal_resistance": 0.05, "pressure_drop": 550.0},
        parent="base", axis="flow",
        expected={"thermal_resistance": {"direction": "down"}, "pressure_drop": {"direction": "up"}},
    )
    _record(
        run_dir, 2, "up_wrong",
        scores={"thermal_resistance": 0.07, "pressure_drop": 500.0},
        parent="base", axis="flow",
        expected={"thermal_resistance": {"direction": "down"}},
    )
    _record(
        run_dir, 3, "tiny",
        scores={"thermal_resistance": 0.05997, "pressure_drop": 500.0},
        parent="base", axis="material",
        expected={"thermal_resistance": {"direction": "down"}},
    )


def test_build_calibration_totals_and_hit_rate(tmp_path: Path) -> None:
    _seed_mixed_ledger(tmp_path)

    report = build_calibration(tmp_path)

    assert report["totals"] == {
        "confirmed": 2,
        "refuted": 1,
        "inconclusive": 1,
        "predictions": 4,
    }
    # hit_rate excludes inconclusive from the denominator: 2 / (2 + 1).
    assert report["hit_rate"] == pytest.approx(2 / 3)


def test_calibration_by_objective_and_by_axis(tmp_path: Path) -> None:
    _seed_mixed_ledger(tmp_path)

    report = build_calibration(tmp_path)

    tr = report["by_objective"]["thermal_resistance"]
    assert (tr["confirmed"], tr["refuted"], tr["inconclusive"]) == (1, 1, 1)
    assert tr["hit_rate"] == pytest.approx(0.5)
    pd = report["by_objective"]["pressure_drop"]
    assert pd["hit_rate"] == pytest.approx(1.0)

    assert report["by_axis"]["flow"]["hit_rate"] == pytest.approx(2 / 3)
    # An all-inconclusive axis has no decided predictions -> hit_rate is None.
    assert report["by_axis"]["material"]["hit_rate"] is None


def test_hit_rate_none_when_no_decided_predictions(tmp_path: Path) -> None:
    _record(tmp_path, 0, "base", scores={"thermal_resistance": 0.06})
    _record(
        tmp_path, 1, "tiny",
        scores={"thermal_resistance": 0.05997},  # within the dead-band
        parent="base",
        expected={"thermal_resistance": {"direction": "down"}},
    )

    report = build_calibration(tmp_path)
    assert report["totals"]["inconclusive"] == 1
    assert report["hit_rate"] is None


def test_empty_ledger_yields_zero_predictions(tmp_path: Path) -> None:
    report = build_calibration(tmp_path)
    assert report["totals"] == {
        "confirmed": 0,
        "refuted": 0,
        "inconclusive": 0,
        "predictions": 0,
    }
    assert report["hit_rate"] is None
    assert report["by_objective"] == {}
    assert report["by_axis"] == {}


def test_legacy_string_expected_contributes_no_predictions(tmp_path: Path) -> None:
    _record(tmp_path, 0, "base", scores={"thermal_resistance": 0.06})
    _record(
        tmp_path, 1, "child",
        scores={"thermal_resistance": 0.05},
        parent="base", expected="R_th down",  # legacy free text -> no verdict
    )
    report = build_calibration(tmp_path)
    assert report["totals"]["predictions"] == 0


# --------------------------------------------------------------------------- #
# CLI — `meta-research calibration` (pure ledger read, no prepare.py)
# --------------------------------------------------------------------------- #
def test_cli_calibration_prints_hit_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from meta_research.cli import main

    _seed_mixed_ledger(tmp_path)

    rc = main(["calibration", "--run-dir", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "hit-rate" in out
    assert "66.7%" in out  # overall 2/3 decided
