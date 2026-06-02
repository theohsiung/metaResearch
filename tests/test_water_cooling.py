"""Tests for the water_cooling reference example (DESIGN §9).

- Add ``examples/water_cooling`` to ``sys.path`` and import ``prepare`` via importlib.
- Build the ``straight_fins`` and ``pin_fins`` baseline designs.
- Run ``prepare.make_evaluator().evaluate(design, out_dir)`` for each and assert:
    * ``EvalResult.ok`` is True and ``feasible`` is True,
    * both objective scores (thermal_resistance, pressure_drop) are finite and > 0,
    * ``heatmap.png`` and ``breakdown.json`` are written to ``out_dir``.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType

import pytest

from meta_research.interfaces import DesignSpec, EvalResult

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "water_cooling"

pytestmark = pytest.mark.skipif(
    not EXAMPLE_DIR.exists(), reason="examples/water_cooling not present yet"
)


@pytest.fixture(scope="module")
def wc_prepare() -> ModuleType:
    """Import the example's ``prepare`` with the example dir on ``sys.path``."""
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "wc_prepare_test", EXAMPLE_DIR / "prepare.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["wc_prepare_test"] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("wc_prepare_test", None)
        if str(EXAMPLE_DIR) in sys.path:
            sys.path.remove(str(EXAMPLE_DIR))


def _build_design(name: str) -> DesignSpec:
    """Import ``designs/<name>.py`` from the example and call its ``build()``."""
    design_path = EXAMPLE_DIR / "designs" / f"{name}.py"
    assert design_path.is_file(), f"missing baseline design {name}"
    # Ensure the package context is importable (designs may import siblings).
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            f"wc_design_{name}", design_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, "build"), f"{name} must expose build()"
        design = module.build()
    finally:
        if str(EXAMPLE_DIR) in sys.path:
            sys.path.remove(str(EXAMPLE_DIR))
    assert isinstance(design, DesignSpec)
    assert design.params, "DesignSpec.params must be non-empty"
    return design


# --------------------------------------------------------------------------- #
# Baseline designs build cleanly.
# --------------------------------------------------------------------------- #
def test_baselines_listed_in_prepare(wc_prepare: ModuleType) -> None:
    assert "straight_fins" in wc_prepare.BASELINES
    assert "pin_fins" in wc_prepare.BASELINES


def test_objectives_are_the_two_pinned(wc_prepare: ModuleType) -> None:
    names = [o.name for o in wc_prepare.OBJECTIVES]
    assert "thermal_resistance" in names
    assert "pressure_drop" in names


@pytest.mark.parametrize("name", ["straight_fins", "pin_fins"])
def test_build_design(name: str) -> None:
    design = _build_design(name)
    assert isinstance(design, DesignSpec)
    assert isinstance(design.params, dict) and design.params


# --------------------------------------------------------------------------- #
# Evaluate each baseline.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["straight_fins", "pin_fins"])
def test_evaluate_baseline(
    name: str, wc_prepare: ModuleType, tmp_path: Path
) -> None:
    design = _build_design(name)
    evaluator = wc_prepare.make_evaluator()

    out_dir = tmp_path / name
    out_dir.mkdir(parents=True, exist_ok=True)
    result = evaluator.evaluate(design, out_dir)

    assert isinstance(result, EvalResult)
    assert result.error is None
    assert result.ok is True
    assert result.feasible is True

    # Both objective scores present, finite and strictly positive.
    r_th = result.scores["thermal_resistance"]
    dp = result.scores["pressure_drop"]
    assert math.isfinite(r_th) and r_th > 0
    assert math.isfinite(dp) and dp > 0

    # Scores cover every declared objective.
    for obj in evaluator.objectives:
        assert obj.name in result.scores
        assert math.isfinite(result.scores[obj.name])

    # Diagnostic artifacts written to out_dir (§9): heatmap.png + breakdown.json.
    heatmap = out_dir / "heatmap.png"
    breakdown = out_dir / "breakdown.json"
    assert heatmap.is_file(), "evaluator must write heatmap.png (the diagnostic)"
    assert breakdown.is_file(), "evaluator must write breakdown.json"
    assert heatmap.stat().st_size > 0

    # The heatmap is referenced in artifacts so the agent can read it back.
    assert "heatmap" in result.artifacts


@pytest.mark.parametrize("name", ["straight_fins", "pin_fins"])
def test_breakdown_has_physics_keys(
    name: str, wc_prepare: ModuleType, tmp_path: Path
) -> None:
    import json

    design = _build_design(name)
    evaluator = wc_prepare.make_evaluator()
    out_dir = tmp_path / name
    out_dir.mkdir(parents=True, exist_ok=True)
    result = evaluator.evaluate(design, out_dir)

    breakdown = json.loads((out_dir / "breakdown.json").read_text(encoding="utf-8"))
    # §9: breakdown carries the per-mechanism resistance / flow breakdown.
    for key in ("R_th", "R_conv", "fin_efficiency"):
        assert key in breakdown


def test_straight_and_pin_differ(wc_prepare: ModuleType, tmp_path: Path) -> None:
    """Two different fin topologies should not produce identical scores."""
    evaluator = wc_prepare.make_evaluator()
    out_s = tmp_path / "straight"
    out_p = tmp_path / "pin"
    out_s.mkdir()
    out_p.mkdir()
    res_s = evaluator.evaluate(_build_design("straight_fins"), out_s)
    res_p = evaluator.evaluate(_build_design("pin_fins"), out_p)
    assert res_s.ok and res_p.ok
    # At least one objective should differ between the two topologies.
    assert res_s.scores != res_p.scores
