"""Tests for the frozen core contracts in ``meta_research.interfaces``.

Covers (per DESIGN.md §3, §7):
- Objective direction validation and ``is_better`` for both min and max.
- DesignSpec / EvalResult JSON round-trips.
- ``EvalResult.ok`` (finite scores, no error) and ``EvalResult.crashed``.

These tests pin behaviour of the FROZEN module; they import from it and never
mutate it.
"""

from __future__ import annotations

import math

import pytest

from meta_research.interfaces import (
    DIRECTIONS,
    DesignSpec,
    EvalResult,
    Objective,
)


# --------------------------------------------------------------------------- #
# Objective
# --------------------------------------------------------------------------- #
def test_directions_constant() -> None:
    assert DIRECTIONS == ("min", "max")


def test_objective_defaults() -> None:
    obj = Objective(name="thermal_resistance")
    assert obj.direction == "min"
    assert obj.unit == ""
    assert obj.weight == 1.0


@pytest.mark.parametrize("direction", ["min", "max"])
def test_objective_valid_direction(direction: str) -> None:
    obj = Objective(name="x", direction=direction)
    assert obj.direction == direction


@pytest.mark.parametrize("bad", ["minimize", "MAX", "", "lower", "up", "down"])
def test_objective_invalid_direction_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        Objective(name="x", direction=bad)


def test_objective_is_better_min() -> None:
    obj = Objective(name="r_th", direction="min", unit="K/W")
    # For minimization, a smaller value is strictly better.
    assert obj.is_better(0.04, 0.06) is True
    assert obj.is_better(0.06, 0.04) is False
    # Equal values are not strictly better.
    assert obj.is_better(0.05, 0.05) is False


def test_objective_is_better_max() -> None:
    obj = Objective(name="efficiency", direction="max", unit="")
    # For maximization, a larger value is strictly better.
    assert obj.is_better(0.9, 0.8) is True
    assert obj.is_better(0.8, 0.9) is False
    assert obj.is_better(0.5, 0.5) is False


def test_objective_is_frozen() -> None:
    obj = Objective(name="x")
    with pytest.raises(Exception):
        obj.name = "y"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# DesignSpec
# --------------------------------------------------------------------------- #
def test_design_spec_json_round_trip() -> None:
    spec = DesignSpec(
        params={"fin_type": "straight", "n_fins": 20, "L_mm": 40.0},
        artifacts={"step": "geom.step"},
        notes="baseline straight fins",
    )
    data = spec.to_json()
    assert data == {
        "params": {"fin_type": "straight", "n_fins": 20, "L_mm": 40.0},
        "artifacts": {"step": "geom.step"},
        "notes": "baseline straight fins",
    }
    restored = DesignSpec.from_json(data)
    assert restored == spec
    assert restored.params == spec.params
    assert restored.artifacts == spec.artifacts
    assert restored.notes == spec.notes


def test_design_spec_defaults_and_round_trip() -> None:
    spec = DesignSpec(params={"a": 1})
    assert spec.artifacts == {}
    assert spec.notes == ""
    restored = DesignSpec.from_json(spec.to_json())
    assert restored == spec


def test_design_spec_from_json_tolerates_missing_keys() -> None:
    restored = DesignSpec.from_json({"params": {"k": 2}})
    assert restored.params == {"k": 2}
    assert restored.artifacts == {}
    assert restored.notes == ""


def test_design_spec_from_json_empty() -> None:
    restored = DesignSpec.from_json({})
    assert restored.params == {}
    assert restored.artifacts == {}
    assert restored.notes == ""


def test_design_spec_is_frozen() -> None:
    spec = DesignSpec(params={"a": 1})
    with pytest.raises(Exception):
        spec.notes = "changed"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# EvalResult — ok / crashed / round-trip
# --------------------------------------------------------------------------- #
def test_eval_result_ok_true_for_finite_scores() -> None:
    res = EvalResult(scores={"thermal_resistance": 0.05, "pressure_drop": 410.0})
    assert res.ok is True
    assert res.feasible is True
    assert res.error is None


def test_eval_result_ok_false_on_nan() -> None:
    res = EvalResult(scores={"thermal_resistance": float("nan")})
    assert res.ok is False


def test_eval_result_ok_false_on_inf() -> None:
    res = EvalResult(scores={"pressure_drop": float("inf")})
    assert res.ok is False
    res_neg = EvalResult(scores={"pressure_drop": float("-inf")})
    assert res_neg.ok is False


def test_eval_result_ok_false_when_error_set() -> None:
    res = EvalResult(scores={"thermal_resistance": 0.05}, error="boom")
    assert res.ok is False


def test_eval_result_crashed() -> None:
    res = EvalResult.crashed("solver exploded")
    assert res.error == "solver exploded"
    assert res.feasible is False
    assert res.scores == {}
    assert res.metadata == {}
    assert res.artifacts == {}
    assert res.ok is False


def test_eval_result_json_round_trip() -> None:
    res = EvalResult(
        scores={"thermal_resistance": 0.042, "pressure_drop": 820.0},
        feasible=True,
        metadata={"Re": 1500.0, "fin_efficiency": 0.78},
        artifacts={"heatmap": "trace/heatmap.png"},
        error=None,
    )
    data = res.to_json()
    assert set(data) == {"scores", "feasible", "metadata", "artifacts", "error"}
    restored = EvalResult.from_json(data)
    assert restored.scores == res.scores
    assert restored.feasible == res.feasible
    assert restored.metadata == res.metadata
    assert restored.artifacts == res.artifacts
    assert restored.error == res.error


def test_eval_result_from_json_coerces_scores_to_float() -> None:
    restored = EvalResult.from_json(
        {"scores": {"thermal_resistance": 1, "pressure_drop": 2}}
    )
    assert all(isinstance(v, float) for v in restored.scores.values())
    assert restored.scores == {"thermal_resistance": 1.0, "pressure_drop": 2.0}


def test_eval_result_crashed_json_round_trip() -> None:
    res = EvalResult.crashed("import failed")
    restored = EvalResult.from_json(res.to_json())
    assert restored.error == "import failed"
    assert restored.feasible is False
    assert restored.scores == {}
    assert restored.ok is False


def test_eval_result_ok_accepts_int_scores() -> None:
    res = EvalResult(scores={"count": 3})
    assert res.ok is True


def test_eval_result_is_frozen() -> None:
    res = EvalResult(scores={"x": 1.0})
    with pytest.raises(Exception):
        res.feasible = False  # type: ignore[misc]
