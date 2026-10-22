"""Sealed holdout scores — Hidden Consistent Evaluation (AIRA_2, arXiv:2603.26499).

When an evaluator can score a candidate on two disjoint splits, the loop must only
ever *see* one of them. AIRA_2's ablation is the strongest evidence in the
literature for an automated research loop: scoring the search on one fixed split
and choosing the final candidate on another, never-searched split adds 13-18
points, and removes the late-run degradation that self-reported, re-split scores
show (noise and lucky splits get selected, not real gains).

Contract (no change to the frozen interfaces):

- The evaluator puts the holdout scores in ``result.metadata["sealed_scores"]``
  (``{name: float}``). ``result.scores`` stays the search-split scores the
  objectives, frontier, KG and ``results.tsv`` use.
- The runner calls :func:`split_sealed` *before* recording, so the sealed scores
  never reach ``result.json``, ``trace/breakdown.json``, ``results.tsv``,
  ``frontier.json`` or ``kg.json``; :func:`store_sealed` writes them to
  ``<run_dir>/.sealed/<bundle>.json``.
- The proposer never reads ``.sealed/``. :func:`finalize` is the only reader: it
  takes the top-k candidates by a *search* objective, reveals their sealed scores,
  picks the best on a sealed key, and appends the reveal to ``finalize.jsonl``
  (append-only, like the rest of the ledger).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .frontier import _read_bundle_row
from .interfaces import EvalResult, Objective, replace

SEALED_KEY = "sealed_scores"
SEALED_DIRNAME = ".sealed"
FINALIZE_LOG = "finalize.jsonl"


def split_sealed(result: EvalResult) -> tuple[EvalResult, dict[str, float] | None, str | None]:
    """(result without the sealed scores, the sealed scores, an error message).

    Non-numeric or non-finite sealed values are dropped and reported, never stored
    into the visible result.
    """
    if SEALED_KEY not in result.metadata:
        return result, None, None
    metadata = dict(result.metadata)
    raw = metadata.pop(SEALED_KEY)
    visible = replace(result, metadata=metadata)
    if not isinstance(raw, dict):
        return visible, None, f"{SEALED_KEY} must be a dict, got {type(raw).__name__}"
    sealed, bad = {}, []
    for key, value in raw.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            sealed[str(key)] = float(value)
        else:
            bad.append(str(key))
    error = f"non-numeric sealed score(s) dropped: {', '.join(bad)}" if bad else None
    return visible, sealed, error


def sealed_dir(run_dir: Path | str) -> Path:
    return Path(run_dir) / SEALED_DIRNAME


def store_sealed(run_dir: Path | str, bundle_name: str, sealed: dict[str, float]) -> Path:
    """Write ``.sealed/<bundle>.json``; one file per bundle, never rewritten."""
    directory = sealed_dir(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{bundle_name}.json"
    if path.exists():
        raise FileExistsError(f"sealed scores for {bundle_name} already stored")
    path.write_text(json.dumps({"bundle": bundle_name, "scores": sealed}, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def _load_sealed(run_dir: Path, bundle_name: str) -> dict[str, float] | None:
    path = sealed_dir(run_dir) / f"{bundle_name}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("scores")


def finalize(
    run_dir: Path | str,
    objectives: list[Objective],
    *,
    top: int = 3,
    by: str | None = None,
    pick: str | None = None,
    pick_direction: str | None = None,
    weights: dict[str, float] | None = None,
    exclude: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Reveal the sealed scores of the top-``top`` candidates and pick one.

    Args:
        run_dir: Experiment directory.
        objectives: The experiment's objectives (search split).
        top: How many candidates to reveal.
        by: Search objective that ranks the candidates (default: the first objective).
        pick: Sealed key the final choice maximizes/minimizes (default: ``by``).
        pick_direction: ``"max"``/``"min"`` for ``pick``; defaults to the direction of the
            objective of the same name, else ``"max"``.
        weights: Rank by a weighted sum of search objectives instead of ``by``
            (e.g. ``{"s": 0.6, "o": 0.4}``); each term is signed by its objective's
            direction, so the sum is always maximized. ``pick`` is then required.
        exclude: Design names that cannot be the final pick (reference points).

    Returns:
        The reveal record (also appended to ``finalize.jsonl``).
    """
    run_dir = Path(run_dir)
    if top < 1:
        raise ValueError("top must be >= 1")
    if weights:
        directions = {o.name: o.direction for o in objectives}
        unknown = sorted(set(weights) - set(directions))
        if unknown:
            raise ValueError(f"unknown search objective(s) in weights: {unknown}")
        if not pick:
            raise ValueError("pick is required with weights")
        rank_names = sorted(weights)

        def rank_key(scores: dict[str, float]) -> float:
            return sum(weights[n] * scores[n] * (1 if directions[n] == "max" else -1) for n in rank_names)

        by_label, descending = "weighted", True
    else:
        by_obj = next((o for o in objectives if o.name == (by or objectives[0].name)), None)
        if by_obj is None:
            raise ValueError(f"unknown search objective {by!r}")
        rank_names = [by_obj.name]

        def rank_key(scores: dict[str, float]) -> float:
            return scores[by_obj.name]

        by_label, descending = by_obj.name, by_obj.direction == "max"
    pick = pick or by_label
    if pick_direction is None:
        pick_direction = next((o.direction for o in objectives if o.name == pick), "max")
    if pick_direction not in ("max", "min"):
        raise ValueError("pick_direction must be 'max' or 'min'")

    rows = []
    experience = run_dir / "experience"
    for child in sorted(experience.iterdir()) if experience.is_dir() else []:
        row = _read_bundle_row(child) if child.is_dir() else None
        if (row and row["feasible"] and not row.get("error") and row.get("status") != "crash"
                and all(n in row["scores"] for n in rank_names) and row["name"] not in set(exclude)):
            rows.append({**row, "bundle": child.name})
    rows.sort(key=lambda r: rank_key(r["scores"]), reverse=descending)
    revealed = [{"bundle": r["bundle"], "name": r["name"], "search": r["scores"],
                 "sealed": _load_sealed(run_dir, r["bundle"])} for r in rows[:top]]
    scored = [c for c in revealed if c["sealed"] and pick in c["sealed"]]
    chosen = None
    if scored:
        best = (max if pick_direction == "max" else min)(scored, key=lambda c: c["sealed"][pick])
        chosen = best["bundle"]
    record = {"top": top, "by": by_label, "pick": pick, "pick_direction": pick_direction,
              "candidates": revealed, "chosen": chosen}
    if weights:
        record["weights"] = dict(weights)
    with (run_dir / FINALIZE_LOG).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


__all__ = ["SEALED_KEY", "SEALED_DIRNAME", "FINALIZE_LOG", "split_sealed", "store_sealed", "sealed_dir", "finalize"]
