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

import json
import math
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


#: Relative dead-band below which a score change is too small to credit a
#: predicted direction (DESIGN §7.6) — ``|Δ|/|parent| < this`` is ``inconclusive``.
PREDICTION_DEAD_BAND_REL = 1e-3


def _parse_expected(raw: Any) -> dict[str, Any]:
    """Parse the front-matter ``expected`` scalar (schema 7.3) into a dict.

    The structured prediction is stored as a JSON string; a legacy free-text
    value (or absent/garbage) yields an empty dict — no verdict. Never raises.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _verdict(direction: str, delta: float | None, parent_value: Any) -> str:
    """Confirmed / refuted / inconclusive for one predicted objective (DESIGN §7.6).

    ``inconclusive`` when the parent lacked that score (``delta is None``) or the
    relative change is within the dead-band; otherwise ``confirmed`` iff the sign
    of ``delta`` matches the predicted ``direction`` (``down`` = value decreases),
    ``refuted`` if it is opposite. A derived fact — the agent never self-grades.

    When the parent score is zero or non-numeric the relative dead-band is
    undefined; only an exactly-zero ``delta`` is then ``inconclusive`` (a nonzero
    ``delta`` still receives a directional verdict). In practice ``parent_value``
    always arrives as a finite float (the scores are pre-filtered upstream).
    """
    if delta is None:
        return "inconclusive"
    if (
        isinstance(parent_value, (int, float))
        and math.isfinite(parent_value)
        and parent_value != 0
    ):
        if abs(delta) / abs(parent_value) < PREDICTION_DEAD_BAND_REL:
            return "inconclusive"
    elif delta == 0:
        return "inconclusive"
    if direction == "down":
        return "confirmed" if delta < 0 else "refuted"
    return "confirmed" if delta > 0 else "refuted"


def _actual_rel(delta: float | None, parent_value: Any) -> float | None:
    """Realized relative magnitude ``|delta|/|parent|`` when computable, else None."""
    if (
        delta is None
        or not isinstance(parent_value, (int, float))
        or not math.isfinite(parent_value)
        or parent_value == 0
    ):
        return None
    return abs(delta) / abs(parent_value)


def build_prediction(
    expected: dict[str, Any],
    parent_scores: dict[str, float],
    deltas: dict[str, float | None],
) -> dict[str, Any]:
    """Derive the per-objective verdict block for a mutated-from edge (DESIGN §7.6).

    Only objectives the child predicted with a valid ``direction`` get an entry.
    Magnitude facts (``predicted_rel`` / ``actual_rel`` / ``rel_error``) are
    attached only when the child predicted a ``rel`` and the realized magnitude is
    computable. Returns an empty dict when there is no valid prediction (the caller
    then omits the ``prediction`` key entirely).
    """
    prediction: dict[str, Any] = {}
    for obj, spec in expected.items():
        if not isinstance(spec, dict):
            continue
        direction = spec.get("direction")
        if direction not in ("down", "up"):
            continue
        delta = deltas.get(obj)
        parent_value = parent_scores.get(obj)
        entry: dict[str, Any] = {
            "predicted_direction": direction,
            "verdict": _verdict(direction, delta, parent_value),
        }
        predicted_rel = spec.get("rel")
        if (
            isinstance(predicted_rel, (int, float))
            and not isinstance(predicted_rel, bool)  # bool is an int subclass
            and math.isfinite(predicted_rel)
        ):
            entry["predicted_rel"] = float(predicted_rel)
            actual_rel = _actual_rel(delta, parent_value)
            if actual_rel is not None:
                entry["actual_rel"] = actual_rel
                entry["rel_error"] = actual_rel - float(predicted_rel)
        prediction[obj] = entry
    return prediction


# ----------------------------------------------------------------------------
# Building the graph from the ledger
# ----------------------------------------------------------------------------


def build_kg(run_dir: Path | str) -> dict[str, Any]:
    """Derive the knowledge graph from the experience bundles under ``run_dir``.

    Pure read: one node per parseable bundle (kept, dominated, infeasible, or
    crashed — store everything). Malformed bundles are skipped with a
    ``warnings`` entry; an absent/empty ``experience/`` dir yields an empty
    graph. Never raises for ledger content problems found while parsing bundles;
    edge-building runs on already-normalized rows (finite scores, dict
    ``expected``) and any residual fault is caught by the runner's KG-rebuild
    guard (§6.4).
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
                deltas = score_delta(row["scores"], parent["scores"])
                edge = {
                    "kind": "mutated-from",
                    "src": parent["id"],
                    "dst": row["id"],
                    "axis": row["axis"],
                    "param_diff": param_diff(parent["params"], row["params"]),
                    "score_delta": deltas,
                }
                # Derived verdict: did the child's structured prediction (§7.3) pan
                # out? Present only when the child gave a structured `expected`.
                prediction = build_prediction(row["expected"], parent["scores"], deltas)
                if prediction:
                    edge["prediction"] = prediction
                edges.append(edge)
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
        try:
            rows.append(
                {
                    "id": entry.name,
                    "iteration": int(match.group("iter")),
                    "name": match.group("name"),
                    "status": str(result_doc.get("status") or front.get("status") or ""),
                    "scores": _finite_scores(result_doc.get("scores")),
                    "params": dict(spec_doc.get("params") or {}),
                    "parent": str(front.get("parent") or "").strip(),
                    "inspired_by": _split_names(front.get("inspired_by")),
                    "axis": str(front.get("axis") or ""),
                    "expected": _parse_expected(front.get("expected")),
                }
            )
        except (AttributeError, TypeError, ValueError) as exc:
            # One type-confused bundle must not abort the whole rebuild — keep
            # every healthy bundle and stay loud about the broken one.
            warnings.append(f"{entry.name}: malformed bundle content; skipped ({exc})")
    return rows


def _finite_scores(raw: Any) -> dict[str, float]:
    """Extract finite numeric scores; NaN/Infinity are absent facts, not values.

    ``kg.json`` must stay strict RFC-8259 JSON (no ``NaN``/``Infinity``
    literals), and ``score_delta`` arithmetic over non-finite inputs would
    manufacture meaningless deltas.
    """
    return {
        k: float(v)
        for k, v in dict(raw or {}).items()
        if isinstance(v, (int, float)) and math.isfinite(v)
    }


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


__all__ = [
    "param_diff",
    "score_delta",
    "build_prediction",
    "build_kg",
    "write_kg",
    "KG_FILENAME",
    "KG_VERSION",
]
