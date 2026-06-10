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
    DESIGN_SPEC_FILENAME,
    EXPERIENCE_DIRNAME,
    HYPOTHESIS_FILENAME,
    RESULT_FILENAME,
    _BUNDLE_RE,
    _read_hypothesis,
    _read_json,
    _write_json,
)

logger = logging.getLogger(__name__)

#: The derived knowledge-graph file, sibling of frontier.json / results.tsv.
KG_FILENAME = "kg.json"
#: Schema version stamped into the written file (DESIGN §7.6).
KG_VERSION = 1

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

    rows = _parse_bundles(experience_dir, warnings)

    for row in rows:
        nodes.append(
            {
                "id": row["id"],
                "iteration": row["iteration"],
                "name": row["name"],
                "status": row["status"],
                "scores": row["scores"],
                "bundle": f"{EXPERIENCE_DIRNAME}/{row['id']}",
            }
        )
        parent_name = row["parent"]
        if parent_name:
            parent = _resolve(parent_name, row["iteration"], rows)
            if parent is None:
                warnings.append(
                    f"{row['id']}: parent {parent_name!r} not found in prior bundles"
                )
            else:
                edges.append(
                    {
                        "kind": "mutated-from",
                        "src": parent["id"],
                        "dst": row["id"],
                        "axis": row["axis"],
                        "param_diff": param_diff(parent["params"], row["params"]),
                        "score_delta": score_delta(row["scores"], parent["scores"]),
                    }
                )
        for inspiration_name in row["inspired_by"]:
            inspiration = _resolve(inspiration_name, row["iteration"], rows)
            if inspiration is None:
                warnings.append(
                    f"{row['id']}: inspired_by {inspiration_name!r} not found in prior bundles"
                )
                continue
            # Pure lineage pointer: no diff/delta/axis — cross-design diff
            # semantics are undefined (locked decision Q4).
            edges.append(
                {"kind": "inspired-by", "src": inspiration["id"], "dst": row["id"]}
            )

    # Deterministic output: rebuilds of the same ledger are byte-identical, so
    # the committed kg.json diffs cleanly in git across iterations.
    nodes.sort(key=lambda n: (n["iteration"], n["name"]))
    edges.sort(key=lambda e: (e["dst"], e["kind"], e["src"]))
    return {"nodes": nodes, "edges": edges, "warnings": warnings}


def _parse_bundles(experience_dir: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """First pass: parse every bundle into a flat row (facts only).

    Malformed bundles append to ``warnings`` and are skipped — the KG mirrors
    :meth:`Experience.history`'s tolerance, never raising on ledger content.
    """
    rows: list[dict[str, Any]] = []
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
        spec_doc = _read_json(entry / DESIGN_SPEC_FILENAME) or {}
        front, _prose = _read_hypothesis(entry / HYPOTHESIS_FILENAME)
        rows.append(
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
                "params": dict(spec_doc.get("params") or {}),
                "parent": str(front.get("parent") or "").strip(),
                "inspired_by": _split_names(front.get("inspired_by")),
                "axis": str(front.get("axis") or ""),
            }
        )
    return rows


def _split_names(raw: Any) -> list[str]:
    """Parse the comma-separated ``inspired_by`` front-matter scalar (7.2)."""
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _resolve(
    name: str, before_iteration: int, rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve a lineage name to the **latest** prior bundle of that name.

    Re-evaluated names are common; the proposer's reference means "the version
    of <name> I could see when proposing" — i.e. the newest bundle strictly
    before the child's iteration. None when no prior bundle matches.
    """
    candidates = [
        row
        for row in rows
        if row["name"] == name and row["iteration"] < before_iteration
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row["iteration"])


def write_kg(run_dir: Path | str) -> Path:
    """Rebuild the graph from the ledger and persist ``run_dir/kg.json``.

    Self-healing by construction: the previous file's content is irrelevant —
    stale or corrupt state is fully replaced by the rebuild. Returns the path.
    """
    run_dir = Path(run_dir)
    path = run_dir / KG_FILENAME
    _write_json(path, {"version": KG_VERSION, **build_kg(run_dir)})
    return path


__all__ = ["param_diff", "score_delta", "build_kg", "write_kg", "KG_FILENAME", "KG_VERSION"]
