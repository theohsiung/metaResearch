"""Derived knowledge graph over the experience ledger (DESIGN §7.6).

The KG is a **navigation index**, not a second store: it is rebuilt
deterministically from the experience bundles (the single source of truth) and
contains *facts only* — candidate nodes, parameter diffs, score deltas, and
lineage pointers. Interpretation (reasoning prose, expectations) stays in each
bundle's ``hypothesis.md``, which the agent reads by following the node's
``bundle`` pointer. Per the Meta-Harness discipline, the KG must never replace
reading the raw experience; it only answers "where should I look?".

Like :mod:`meta_research.frontier`, every rebuild recomputes the full graph
from the ledger — derived views are recomputed, only bundles are appended.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from meta_research.experience import (
    EXPERIENCE_DIRNAME,
    HYPOTHESIS_FILENAME,
    RESULT_FILENAME,
    _BUNDLE_RE,
    _read_hypothesis,
    _read_json,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Fact extraction helpers
# ----------------------------------------------------------------------------


def param_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Shallow top-level diff between two ``DesignSpec.params`` dicts.

    Returns ``{"changed": {key: [old, new]}, "added": {key: new},
    "removed": {key: old}}``. Values are kept verbatim; non-scalar values
    (lists, dicts) are compared as opaque wholes — no deep diffing.
    """
    changed: dict[str, Any] = {}
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    for key, new_value in new.items():
        if key not in old:
            added[key] = new_value
        elif old[key] != new_value:
            changed[key] = [old[key], new_value]
    for key, old_value in old.items():
        if key not in new:
            removed[key] = old_value
    return {"changed": changed, "added": added, "removed": removed}


def score_delta(
    child: dict[str, float], parent: dict[str, float]
) -> dict[str, float | None]:
    """Per-objective ``child − parent`` over the child's score keys.

    ``None`` where the parent lacks the key (e.g. a crashed parent recorded no
    scores). A raw fact — optimization direction lives in ``Objective``, not here.
    """
    return {
        key: (value - parent[key]) if key in parent else None
        for key, value in child.items()
    }


# ----------------------------------------------------------------------------
# Building the graph from the ledger
# ----------------------------------------------------------------------------


def build_kg(run_dir: Path | str) -> dict[str, Any]:
    """Derive the knowledge graph from the experience bundles under ``run_dir``.

    Pure read: one node per parseable bundle (kept, dominated, infeasible, or
    crashed — store everything). Malformed bundles are skipped with a
    ``warnings`` entry; an absent/empty ``experience/`` dir yields an empty
    graph. Never raises for ledger content problems.
    """
    run_dir = Path(run_dir)
    experience_dir = run_dir / EXPERIENCE_DIRNAME

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    warnings: list[str] = []

    if not experience_dir.is_dir():
        return {"nodes": nodes, "edges": edges, "warnings": warnings}

    for entry in sorted(experience_dir.iterdir()):
        if not entry.is_dir():
            continue
        match = _BUNDLE_RE.match(entry.name)
        if match is None:
            continue
        result_doc = _read_json(entry / RESULT_FILENAME)
        if result_doc is None:
            warnings.append(f"{entry.name}: missing/invalid {RESULT_FILENAME}; skipped")
            continue
        front, _prose = _read_hypothesis(entry / HYPOTHESIS_FILENAME)
        nodes.append(
            {
                "id": entry.name,
                "iteration": int(match.group("iter")),
                "name": match.group("name"),
                "status": str(result_doc.get("status") or front.get("status") or ""),
                "scores": {
                    k: float(v)
                    for k, v in (result_doc.get("scores") or {}).items()
                    if isinstance(v, (int, float))
                },
                "bundle": f"{EXPERIENCE_DIRNAME}/{entry.name}",
            }
        )

    return {"nodes": nodes, "edges": edges, "warnings": warnings}


__all__ = ["param_diff", "score_delta", "build_kg"]
