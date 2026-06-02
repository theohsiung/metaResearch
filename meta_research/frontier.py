"""Pareto-frontier computation over multiple objectives.

In meta-research, autoresearch's keep/discard decision is replaced by Pareto
membership (DESIGN.md 1.1.3): a candidate that is non-dominated enters
``frontier.json``; a dominated candidate is still committed as experience but
flagged ``dominated``. Only **feasible** candidates with usable finite scores can
be on the frontier.

This module is pure/deterministic (DESIGN.md 10): no ``datetime.now()`` / random
calls. ``update_frontier`` does touch the filesystem (it reads the experience
ledger and writes ``frontier.json``) but performs no nondeterministic work.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from meta_research.interfaces import Objective


# ---------------------------------------------------------------------------
# core dominance logic
# ---------------------------------------------------------------------------

def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _covers(scores: dict[str, float], objectives: list[Objective]) -> bool:
    """True if ``scores`` has a finite value for every objective."""
    for obj in objectives:
        if obj.name not in scores or not _is_finite_number(scores[obj.name]):
            return False
    return True


def dominates(
    a: dict[str, float],
    b: dict[str, float],
    objectives: list[Objective],
) -> bool:
    """Return True if score-set ``a`` Pareto-dominates ``b``.

    ``a`` dominates ``b`` iff ``a`` is *no worse* on every objective and
    *strictly better* on at least one (DESIGN.md 6.2). Direction is taken from
    each :class:`Objective`. If either score-set fails to cover all objectives
    with finite values, ``a`` cannot dominate (returns False) -- incomparable.
    """
    if not _covers(a, objectives) or not _covers(b, objectives):
        return False

    strictly_better_somewhere = False
    for obj in objectives:
        av = float(a[obj.name])
        bv = float(b[obj.name])
        if obj.is_better(bv, av):
            # b is strictly better than a on this objective => a is worse here
            return False
        if obj.is_better(av, bv):
            strictly_better_somewhere = True
    return strictly_better_somewhere


def pareto_front(rows: list[dict], objectives: list[Objective]) -> list[dict]:
    """Return the non-dominated, feasible rows from ``rows``.

    Each row is a dict ``{"name", "iter", "scores", "metadata", ...}`` (extra
    keys are preserved on the returned rows). A row is eligible only if it is
    feasible (``row.get("feasible", True)`` truthy), and its ``scores`` cover all
    objectives with finite values. Among eligible rows, a row is kept iff no
    other eligible row dominates it.

    The returned list preserves input order and the original row objects.
    """
    eligible: list[dict] = []
    for row in rows:
        if not row.get("feasible", True):
            continue
        scores = row.get("scores") or {}
        if not _covers(scores, objectives):
            continue
        eligible.append(row)

    front: list[dict] = []
    for i, row in enumerate(eligible):
        scores_i = row["scores"]
        dominated = False
        for j, other in enumerate(eligible):
            if i == j:
                continue
            if dominates(other["scores"], scores_i, objectives):
                dominated = True
                break
        if not dominated:
            front.append(row)
    return front


def classify(
    scores: dict[str, float],
    frontier_rows: list[dict],
    objectives: list[Objective],
) -> str:
    """Label a freshly-evaluated candidate as ``"frontier"`` or ``"dominated"``.

    A candidate is ``"frontier"`` unless some existing frontier row dominates it
    (DESIGN.md 6.2). Feasibility/crash handling is the caller's responsibility:
    this helper only compares scores. If ``scores`` does not cover all objectives
    with finite values it cannot enter the frontier and is reported
    ``"dominated"``.
    """
    if not _covers(scores, objectives):
        return "dominated"
    for row in frontier_rows:
        other = row.get("scores") or {}
        if dominates(other, scores, objectives):
            return "dominated"
    return "frontier"


# ---------------------------------------------------------------------------
# experience-ledger driven frontier file
# ---------------------------------------------------------------------------

def _objective_json(objectives: list[Objective]) -> list[dict]:
    return [
        {"name": obj.name, "direction": obj.direction, "unit": obj.unit}
        for obj in objectives
    ]


def _read_bundle_row(bundle_dir: Path) -> dict | None:
    """Parse one experience bundle directory into a frontier-row dict.

    Reads ``result.json`` (EvalResult.to_json plus a runner-added ``status``)
    from ``bundle_dir``. The candidate name and iteration are recovered from the
    directory name ``<iter:03d>_<name>``. Returns ``None`` if the bundle is
    unreadable or missing scores -- never raises.
    """
    result_path = bundle_dir / "result.json"
    if not result_path.is_file():
        return None
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    raw_scores = data.get("scores") or {}
    scores: dict[str, float] = {}
    for key, value in raw_scores.items():
        try:
            scores[str(key)] = float(value)
        except (TypeError, ValueError):
            continue

    feasible = bool(data.get("feasible", True))
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    name, iteration = _parse_bundle_name(bundle_dir.name)
    return {
        "name": name,
        "iter": iteration,
        "scores": scores,
        "metadata": metadata,
        "feasible": feasible,
        "status": data.get("status"),
        "error": data.get("error"),
    }


def _parse_bundle_name(dirname: str) -> tuple[str, int | None]:
    """Split ``<iter:03d>_<name>`` into ``(name, iter)``.

    Falls back gracefully if the prefix is not a number or there is no
    underscore.
    """
    prefix, sep, rest = dirname.partition("_")
    if sep == "":
        return dirname, None
    try:
        iteration: int | None = int(prefix)
    except (TypeError, ValueError):
        return dirname, None
    return rest, iteration


def _collect_rows(run_dir: Path) -> list[dict]:
    """Read every experience bundle under ``run_dir/experience`` into rows."""
    exp_dir = run_dir / "experience"
    if not exp_dir.is_dir():
        return []
    rows: list[dict] = []
    for child in sorted(exp_dir.iterdir()):
        if not child.is_dir():
            continue
        row = _read_bundle_row(child)
        if row is not None:
            rows.append(row)
    return rows


def _best_per_objective(rows: list[dict], objectives: list[Objective]) -> dict[str, dict]:
    """Best feasible value seen per objective across all eligible rows."""
    best: dict[str, dict] = {}
    for obj in objectives:
        best_row: dict | None = None
        best_val: float | None = None
        for row in rows:
            if not row.get("feasible", True):
                continue
            scores = row.get("scores") or {}
            if obj.name not in scores or not _is_finite_number(scores[obj.name]):
                continue
            val = float(scores[obj.name])
            if best_val is None or obj.is_better(val, best_val):
                best_val = val
                best_row = row
        if best_row is not None and best_val is not None:
            best[obj.name] = {"name": best_row["name"], "value": best_val}
    return best


def _frontier_dict(rows: list[dict], objectives: list[Objective]) -> dict:
    """Build the ``frontier.json`` payload (schema DESIGN.md 7.4)."""
    front = pareto_front(rows, objectives)
    pareto_entries: list[dict] = []
    for row in front:
        pareto_entries.append(
            {
                "name": row.get("name"),
                "iteration": row.get("iter"),
                "scores": {obj.name: float(row["scores"][obj.name]) for obj in objectives},
                "metadata": row.get("metadata", {}),
            }
        )
    return {
        "objectives": _objective_json(objectives),
        "pareto": pareto_entries,
        "best_per_objective": _best_per_objective(rows, objectives),
    }


def update_frontier(run_dir: Path | str, objectives: list[Objective]) -> dict:
    """Recompute the Pareto frontier from the experience ledger and persist it.

    Reads every bundle under ``run_dir/experience/`` (DESIGN.md 7.2), recomputes
    the non-dominated set and per-objective bests, writes ``run_dir/frontier.json``
    (schema 7.4) and returns the payload.

    The recompute is full (not incremental) so the frontier always reflects the
    complete append-only ledger -- consistent with "never reset" (DESIGN.md 1.1.2).
    """
    run_path = Path(run_dir)
    rows = _collect_rows(run_path)
    payload = _frontier_dict(rows, objectives)

    out_path = run_path / "frontier.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def load_frontier(run_dir: Path | str) -> dict:
    """Read ``frontier.json`` from ``run_dir`` (empty dict if missing/corrupt)."""
    path = Path(run_dir) / "frontier.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "dominates",
    "pareto_front",
    "classify",
    "update_frontier",
    "load_frontier",
]
