"""Tests for the derived knowledge graph (design doc: docs/features/kg-graph/design.md).

The KG is a purely *derived* navigation index over the experience ledger:
nodes are evaluated candidates, edges are facts (param diffs, score deltas,
lineage pointers). It is rebuilt deterministically from bundles — the LLM
never writes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meta_research import build_kg
from meta_research.experience import Experience
from meta_research.interfaces import DesignSpec, EvalResult, Objective
from meta_research.kg import param_diff, score_delta

OBJECTIVES: list[Objective] = [
    Objective("thermal_resistance", "min", "K/W"),
    Objective("pressure_drop", "min", "Pa"),
]


def _record_bundle(
    run_dir: Path,
    iteration: int,
    name: str,
    *,
    params: dict[str, Any],
    scores: dict[str, float] | None = None,
    status: str = "dominated",
    parent: str = "",
    axis: str = "geometry",
    inspired_by: list[str] | None = None,
    error: str | None = None,
    feasible: bool = True,
) -> None:
    """Write a real schema-7.1 bundle through the store (the honest fixture)."""
    exp = Experience(run_dir, OBJECTIVES)
    if error is not None:
        result = EvalResult.crashed(error)
    else:
        result = EvalResult(scores=dict(scores or {}), feasible=feasible)
    hypothesis: dict[str, Any] = {
        "axis": axis,
        "parent": parent,
        "expected": "",
        "reasoning": "test bundle",
        "status": status,
    }
    if inspired_by is not None:
        hypothesis["inspired_by"] = inspired_by
    exp.record(
        iteration=iteration,
        name=name,
        design_src_path=run_dir / "no_such_module.py",
        design=DesignSpec(params=dict(params)),
        result=result,
        hypothesis=hypothesis,
    )


# --------------------------------------------------------------------------- #
# param_diff — shallow top-level diff of DesignSpec.params (design.md §2)
# --------------------------------------------------------------------------- #
def test_param_diff_reports_changed_added_removed() -> None:
    # Grounded in the water-cooling domain: a straight-fin parent mutated into
    # a pin-fin child legitimately changes, gains, and loses knobs.
    parent = {
        "fin_type": "straight",
        "fin_height_mm": 10.0,
        "n_fins": 20,
        "material": "copper",
    }
    child = {
        "fin_type": "pin",
        "fin_height_mm": 10.0,
        "arrangement": "inline",
        "material": "copper",
    }
    diff = param_diff(parent, child)
    assert diff == {
        "changed": {"fin_type": ["straight", "pin"]},
        "added": {"arrangement": "inline"},
        "removed": {"n_fins": 20},
    }


def test_param_diff_identical_params_is_empty() -> None:
    params = {"fin_type": "straight", "n_fins": 20}
    assert param_diff(params, dict(params)) == {
        "changed": {},
        "added": {},
        "removed": {},
    }


def test_param_diff_values_kept_verbatim_and_non_scalars_opaque() -> None:
    # Non-scalar values compare as opaque wholes — no deep diffing (out of scope v1).
    parent = {"fin_rows": [2, 4, 6], "envelope": {"L": 40, "W": 40}}
    child = {"fin_rows": [2, 4, 8], "envelope": {"L": 40, "W": 40}}
    diff = param_diff(parent, child)
    assert diff["changed"] == {"fin_rows": [[2, 4, 6], [2, 4, 8]]}
    assert diff["added"] == {}
    assert diff["removed"] == {}


# --------------------------------------------------------------------------- #
# score_delta — per-objective child − parent (design.md §2)
# --------------------------------------------------------------------------- #
def test_score_delta_is_child_minus_parent() -> None:
    child = {"thermal_resistance": 0.042, "pressure_drop": 820.0}
    parent = {"thermal_resistance": 0.061, "pressure_drop": 410.0}
    delta = score_delta(child, parent)
    assert delta["thermal_resistance"] == pytest.approx(0.042 - 0.061)
    assert delta["pressure_drop"] == pytest.approx(410.0)


def test_score_delta_null_where_parent_lacks_score() -> None:
    # A crashed parent recorded no scores: the delta is a fact we cannot state.
    child = {"thermal_resistance": 0.042, "pressure_drop": 820.0}
    assert score_delta(child, {}) == {
        "thermal_resistance": None,
        "pressure_drop": None,
    }


# --------------------------------------------------------------------------- #
# build_kg — nodes (store everything; design.md §3)
# --------------------------------------------------------------------------- #
def test_build_kg_yields_one_node_per_bundle_including_failures(
    tmp_path: Path,
) -> None:
    _record_bundle(
        tmp_path,
        0,
        "straight_fins",
        params={"fin_type": "straight", "n_fins": 20},
        scores={"thermal_resistance": 0.061, "pressure_drop": 410.0},
        status="frontier",
    )
    _record_bundle(tmp_path, 1, "bad_fins", params={}, error="boom")
    _record_bundle(
        tmp_path,
        2,
        "too_thin",
        params={"fin_type": "straight", "fin_thickness_mm": 0.1},
        scores={"thermal_resistance": 0.05, "pressure_drop": 900.0},
        feasible=False,
    )

    graph = build_kg(tmp_path)

    assert [n["id"] for n in graph["nodes"]] == [
        "000_straight_fins",
        "001_bad_fins",
        "002_too_thin",
    ]
    baseline = graph["nodes"][0]
    assert baseline == {
        "id": "000_straight_fins",
        "iteration": 0,
        "name": "straight_fins",
        "status": "frontier",
        "scores": {"thermal_resistance": 0.061, "pressure_drop": 410.0},
        "bundle": "experience/000_straight_fins",
    }
    statuses = {n["id"]: n["status"] for n in graph["nodes"]}
    assert statuses["001_bad_fins"] == "crash"
    assert statuses["002_too_thin"] == "infeasible"


def test_build_kg_empty_run_dir_is_empty_graph(tmp_path: Path) -> None:
    assert build_kg(tmp_path) == {"nodes": [], "edges": [], "warnings": []}


# --------------------------------------------------------------------------- #
# build_kg — mutated-from edges (facts only; design.md §3)
# --------------------------------------------------------------------------- #
def test_mutated_from_edge_carries_param_diff_and_score_delta(
    tmp_path: Path,
) -> None:
    _record_bundle(
        tmp_path,
        0,
        "pin_fins",
        params={"fin_type": "pin", "arrangement": "inline", "pitch_mm": 4.0},
        scores={"thermal_resistance": 0.061, "pressure_drop": 590.0},
        status="frontier",
    )
    _record_bundle(
        tmp_path,
        1,
        "staggered_pin_v1",
        params={"fin_type": "pin", "arrangement": "staggered", "pitch_mm": 4.0},
        scores={"thermal_resistance": 0.042, "pressure_drop": 820.0},
        status="frontier",
        parent="pin_fins",
        axis="flow_arrangement",
    )

    graph = build_kg(tmp_path)

    assert graph["edges"] == [
        {
            "kind": "mutated-from",
            "src": "000_pin_fins",
            "dst": "001_staggered_pin_v1",
            "axis": "flow_arrangement",
            "param_diff": {
                "changed": {"arrangement": ["inline", "staggered"]},
                "added": {},
                "removed": {},
            },
            "score_delta": {
                "thermal_resistance": pytest.approx(0.042 - 0.061),
                "pressure_drop": pytest.approx(230.0),
            },
        }
    ]


def test_parent_resolves_to_latest_prior_evaluation(tmp_path: Path) -> None:
    # The same candidate name re-evaluated across iterations: a child naming it
    # links to the newest bundle *before* the child — never a later re-eval,
    # never a stale earlier one.
    base = {"fin_type": "pin", "arrangement": "inline"}
    scores = {"thermal_resistance": 0.06, "pressure_drop": 500.0}
    _record_bundle(tmp_path, 0, "pin_fins", params=base, scores=scores)
    _record_bundle(
        tmp_path, 1, "early_child", params=base, scores=scores, parent="pin_fins"
    )
    _record_bundle(tmp_path, 2, "pin_fins", params=base, scores=scores)
    _record_bundle(
        tmp_path, 3, "late_child", params=base, scores=scores, parent="pin_fins"
    )

    graph = build_kg(tmp_path)

    src_by_child = {e["dst"]: e["src"] for e in graph["edges"]}
    assert src_by_child["001_early_child"] == "000_pin_fins"
    assert src_by_child["003_late_child"] == "002_pin_fins"


def test_baselines_without_parent_have_no_mutated_from_edge(tmp_path: Path) -> None:
    _record_bundle(
        tmp_path,
        0,
        "straight_fins",
        params={"fin_type": "straight"},
        scores={"thermal_resistance": 0.061, "pressure_drop": 410.0},
        status="frontier",
        parent="",
    )
    graph = build_kg(tmp_path)
    assert graph["edges"] == []
    assert graph["warnings"] == []


# --------------------------------------------------------------------------- #
# build_kg — inspired-by edges (multi-parent lineage, pure pointers)
# --------------------------------------------------------------------------- #
def test_inspired_by_yields_pure_lineage_pointers(tmp_path: Path) -> None:
    scores = {"thermal_resistance": 0.06, "pressure_drop": 500.0}
    _record_bundle(tmp_path, 0, "straight_fins", params={"fin_type": "straight"}, scores=scores)
    _record_bundle(tmp_path, 1, "pin_fins", params={"fin_type": "pin"}, scores=scores)
    _record_bundle(
        tmp_path,
        2,
        "hybrid_fins",
        params={"fin_type": "pin"},
        scores=scores,
        parent="pin_fins",
        inspired_by=["straight_fins"],
    )

    graph = build_kg(tmp_path)

    inspired = [e for e in graph["edges"] if e["kind"] == "inspired-by"]
    # Pure pointer: src/dst/kind only — cross-design diffs are undefined (Q4).
    assert inspired == [
        {"kind": "inspired-by", "src": "000_straight_fins", "dst": "002_hybrid_fins"}
    ]
    # The primary mutated-from edge coexists with the inspiration pointers.
    assert [e["kind"] for e in graph["edges"]].count("mutated-from") == 1


# --------------------------------------------------------------------------- #
# build_kg — unresolvable lineage: loud warnings, never fatal, never silent
# --------------------------------------------------------------------------- #
def test_unresolvable_lineage_names_surface_as_warnings(tmp_path: Path) -> None:
    scores = {"thermal_resistance": 0.06, "pressure_drop": 500.0}
    _record_bundle(
        tmp_path,
        0,
        "typo_child",
        params={"fin_type": "pin"},
        scores=scores,
        parent="strait_fins",  # typo: matches no bundle
        inspired_by=["ghost_design"],
    )

    graph = build_kg(tmp_path)

    # The node is still produced; nothing is dropped silently.
    assert [n["id"] for n in graph["nodes"]] == ["000_typo_child"]
    assert graph["edges"] == []
    assert len(graph["warnings"]) == 2
    parent_warning = next(w for w in graph["warnings"] if "strait_fins" in w)
    assert "000_typo_child" in parent_warning
    ghost_warning = next(w for w in graph["warnings"] if "ghost_design" in w)
    assert "000_typo_child" in ghost_warning
