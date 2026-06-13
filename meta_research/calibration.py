"""Derived hypothesis hit-rate over the knowledge-graph edges (DESIGN §6.5/§7.6).

Pure ledger read: aggregates the per-objective ``prediction`` verdicts that
:func:`meta_research.kg.build_kg` derives on each ``mutated-from`` edge into a
run-level calibration report — overall, per-objective, and per-axis. Like the
Pareto frontier and the KG it is **recomputed from the bundles on demand and
persists nothing**; the bundles stay the single source of truth.

``hit_rate = confirmed / (confirmed + refuted)`` — ``inconclusive`` verdicts
(parent lacked the score, or the change was within the dead-band) are excluded
from the denominator, so a hit-rate of ``None`` means "no decided predictions
yet", never "0% correct".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meta_research.kg import build_kg

#: Schema version stamped into the report.
CALIBRATION_VERSION = 1

#: The verdict vocabulary recorded on a prediction entry (DESIGN §7.6).
_VERDICTS = ("confirmed", "refuted", "inconclusive")


def _empty_tally() -> dict[str, int]:
    return {"confirmed": 0, "refuted": 0, "inconclusive": 0}


def _hit_rate(tally: dict[str, int]) -> float | None:
    """``confirmed / (confirmed + refuted)``; ``None`` when nothing is decided."""
    decided = tally["confirmed"] + tally["refuted"]
    if decided == 0:
        return None
    return tally["confirmed"] / decided


def _summarize(tally: dict[str, int]) -> dict[str, Any]:
    """A tally plus its derived ``predictions`` total and ``hit_rate``."""
    return {
        **tally,
        "predictions": sum(tally.values()),
        "hit_rate": _hit_rate(tally),
    }


def build_calibration(run_dir: Path | str) -> dict[str, Any]:
    """Aggregate edge prediction verdicts into a calibration report.

    Pure read: rebuilds the KG from the ledger and tallies every ``prediction``
    entry on its ``mutated-from`` edges. Edges without a prediction (legacy
    free-text ``expected``, or baselines) contribute nothing. Never raises for
    ledger-content problems — :func:`build_kg` already tolerates malformed bundles.
    """
    graph = build_kg(run_dir)
    overall = _empty_tally()
    by_objective: dict[str, dict[str, int]] = {}
    by_axis: dict[str, dict[str, int]] = {}

    for edge in graph.get("edges", []):
        prediction = edge.get("prediction")
        if not prediction:
            continue
        axis = str(edge.get("axis") or "")
        for objective, entry in prediction.items():
            verdict = entry.get("verdict")
            if verdict not in _VERDICTS:
                continue
            overall[verdict] += 1
            by_objective.setdefault(objective, _empty_tally())[verdict] += 1
            by_axis.setdefault(axis, _empty_tally())[verdict] += 1

    return {
        "version": CALIBRATION_VERSION,
        "totals": {**overall, "predictions": sum(overall.values())},
        "hit_rate": _hit_rate(overall),
        "by_objective": {k: _summarize(v) for k, v in by_objective.items()},
        "by_axis": {k: _summarize(v) for k, v in by_axis.items()},
    }


__all__ = ["build_calibration", "CALIBRATION_VERSION"]
