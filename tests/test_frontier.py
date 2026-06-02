"""Tests for the Pareto-frontier logic in ``meta_research.frontier`` (DESIGN §6.2).

- ``dominates(a, b, objectives)``: ``a`` dominates ``b`` iff no worse on all and
  strictly better on >=1, correct across min/max objective mixes.
- ``pareto_front(rows, objectives)``: returns exactly the non-dominated *feasible*
  rows (rows are ``{"name","iter","scores","metadata"}``).
- ``classify(scores, frontier_rows, objectives)``: labels ``"frontier"`` vs
  ``"dominated"``.
"""

from __future__ import annotations

from meta_research.frontier import classify, dominates, pareto_front
from meta_research.interfaces import Objective

# Two minimization objectives (the water-cooling default).
MIN_MIN: list[Objective] = [
    Objective("thermal_resistance", "min", "K/W"),
    Objective("pressure_drop", "min", "Pa"),
]

# A mix: minimize one, maximize the other.
MIN_MAX: list[Objective] = [
    Objective("cost", "min", "$"),
    Objective("efficiency", "max", ""),
]

# Both maximization.
MAX_MAX: list[Objective] = [
    Objective("throughput", "max", ""),
    Objective("accuracy", "max", ""),
]


def _row(name: str, it: int, **scores: float) -> dict:
    return {"name": name, "iter": it, "scores": dict(scores), "metadata": {}}


# --------------------------------------------------------------------------- #
# dominates
# --------------------------------------------------------------------------- #
def test_dominates_min_min_strict() -> None:
    a = {"thermal_resistance": 0.04, "pressure_drop": 400.0}
    b = {"thermal_resistance": 0.06, "pressure_drop": 500.0}
    assert dominates(a, b, MIN_MIN) is True
    assert dominates(b, a, MIN_MIN) is False


def test_dominates_requires_strict_improvement() -> None:
    # Equal on everything -> neither dominates.
    a = {"thermal_resistance": 0.05, "pressure_drop": 410.0}
    b = {"thermal_resistance": 0.05, "pressure_drop": 410.0}
    assert dominates(a, b, MIN_MIN) is False
    assert dominates(b, a, MIN_MIN) is False


def test_dominates_no_worse_and_one_strict() -> None:
    # a is equal on r_th, strictly better on dP -> a dominates b.
    a = {"thermal_resistance": 0.05, "pressure_drop": 400.0}
    b = {"thermal_resistance": 0.05, "pressure_drop": 410.0}
    assert dominates(a, b, MIN_MIN) is True
    assert dominates(b, a, MIN_MIN) is False


def test_dominates_tradeoff_neither() -> None:
    # Better on one, worse on the other -> no domination either way.
    a = {"thermal_resistance": 0.04, "pressure_drop": 820.0}
    b = {"thermal_resistance": 0.06, "pressure_drop": 410.0}
    assert dominates(a, b, MIN_MIN) is False
    assert dominates(b, a, MIN_MIN) is False


def test_dominates_min_max_mix() -> None:
    # cost minimized, efficiency maximized.
    a = {"cost": 10.0, "efficiency": 0.9}
    b = {"cost": 12.0, "efficiency": 0.8}
    assert dominates(a, b, MIN_MAX) is True
    assert dominates(b, a, MIN_MAX) is False


def test_dominates_min_max_tradeoff() -> None:
    # a cheaper but less efficient -> tradeoff, neither dominates.
    a = {"cost": 10.0, "efficiency": 0.8}
    b = {"cost": 12.0, "efficiency": 0.9}
    assert dominates(a, b, MIN_MAX) is False
    assert dominates(b, a, MIN_MAX) is False


def test_dominates_max_max() -> None:
    a = {"throughput": 100.0, "accuracy": 0.95}
    b = {"throughput": 90.0, "accuracy": 0.95}
    assert dominates(a, b, MAX_MAX) is True
    assert dominates(b, a, MAX_MAX) is False


# --------------------------------------------------------------------------- #
# pareto_front
# --------------------------------------------------------------------------- #
def test_pareto_front_basic_min_min() -> None:
    rows = [
        _row("a", 0, thermal_resistance=0.04, pressure_drop=820.0),  # frontier
        _row("b", 1, thermal_resistance=0.06, pressure_drop=410.0),  # frontier
        _row("c", 2, thermal_resistance=0.05, pressure_drop=600.0),  # frontier (tradeoff)
        _row("d", 3, thermal_resistance=0.07, pressure_drop=900.0),  # dominated by a & b
    ]
    front = pareto_front(rows, MIN_MIN)
    names = {r["name"] for r in front}
    assert names == {"a", "b", "c"}
    assert "d" not in names


def test_pareto_front_dominated_removed() -> None:
    rows = [
        _row("best", 0, thermal_resistance=0.04, pressure_drop=400.0),
        _row("worse", 1, thermal_resistance=0.06, pressure_drop=500.0),
    ]
    front = pareto_front(rows, MIN_MIN)
    assert [r["name"] for r in front] == ["best"]


def test_pareto_front_excludes_infeasible() -> None:
    rows = [
        _row("ok", 0, thermal_resistance=0.05, pressure_drop=410.0),
        {
            "name": "bad",
            "iter": 1,
            "scores": {"thermal_resistance": 0.01, "pressure_drop": 100.0},
            "metadata": {},
            "feasible": False,
        },
    ]
    front = pareto_front(rows, MIN_MIN)
    names = {r["name"] for r in front}
    # 'bad' would dominate but is infeasible -> excluded from the frontier.
    assert names == {"ok"}


def test_pareto_front_empty() -> None:
    assert pareto_front([], MIN_MIN) == []


def test_pareto_front_single_row() -> None:
    rows = [_row("only", 0, thermal_resistance=0.05, pressure_drop=410.0)]
    front = pareto_front(rows, MIN_MIN)
    assert [r["name"] for r in front] == ["only"]


def test_pareto_front_min_max_mix() -> None:
    rows = [
        _row("cheap_eff", 0, cost=10.0, efficiency=0.9),  # frontier
        _row("cheap_ineff", 1, cost=10.0, efficiency=0.7),  # dominated by cheap_eff
        _row("pricey_eff", 2, cost=20.0, efficiency=0.95),  # frontier (tradeoff)
    ]
    front = pareto_front(rows, MIN_MAX)
    names = {r["name"] for r in front}
    assert names == {"cheap_eff", "pricey_eff"}


# --------------------------------------------------------------------------- #
# classify
# --------------------------------------------------------------------------- #
def test_classify_frontier_when_not_dominated() -> None:
    frontier_rows = [
        _row("b", 1, thermal_resistance=0.06, pressure_drop=410.0),
    ]
    # New candidate strictly better on r_th, worse on dP -> tradeoff, not dominated.
    scores = {"thermal_resistance": 0.04, "pressure_drop": 820.0}
    assert classify(scores, frontier_rows, MIN_MIN) == "frontier"


def test_classify_dominated() -> None:
    frontier_rows = [
        _row("a", 0, thermal_resistance=0.04, pressure_drop=400.0),
    ]
    # New candidate worse on both -> dominated.
    scores = {"thermal_resistance": 0.06, "pressure_drop": 500.0}
    assert classify(scores, frontier_rows, MIN_MIN) == "dominated"


def test_classify_frontier_against_empty() -> None:
    scores = {"thermal_resistance": 0.05, "pressure_drop": 410.0}
    assert classify(scores, [], MIN_MIN) == "frontier"


def test_classify_strictly_better_is_frontier() -> None:
    frontier_rows = [
        _row("a", 0, thermal_resistance=0.06, pressure_drop=500.0),
    ]
    scores = {"thermal_resistance": 0.04, "pressure_drop": 400.0}
    assert classify(scores, frontier_rows, MIN_MIN) == "frontier"
