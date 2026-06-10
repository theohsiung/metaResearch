"""Tests for the derived knowledge graph (design doc: docs/features/kg-graph/design.md).

The KG is a purely *derived* navigation index over the experience ledger:
nodes are evaluated candidates, edges are facts (param diffs, score deltas,
lineage pointers). It is rebuilt deterministically from bundles — the LLM
never writes it.
"""

from __future__ import annotations

import pytest

from meta_research.kg import param_diff


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
