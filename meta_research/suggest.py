"""Operator + parent suggestions — evolutionary selection evidence from AIRA_2 / MLEvolve / AIRA-dojo.

The proposer still decides what to try (DESIGN §1.1.6); this module is a
deterministic tool that turns the ledger into a recommendation the proposer may
follow or overrule (with a stated reason):

- **Operators** (AIRA-dojo, arXiv:2507.02554): ``draft`` a new design, ``improve`` one
  parent, ``debug`` a crashed candidate, ``crossover`` two parents.
- **Parent selection** (AIRA_2, arXiv:2603.26499): temperature-scaled rank selection
  (T=0.2) over feasible candidates ranked by Pareto layer, then the first objective;
  crossover about 15% of the time, between parents from different lineages. AIRA_2's
  "no evolution" ablation lost 8-11 points.
- **Stagnation** (MLEvolve, arXiv:2606.06473): a parent whose last ``stagnation``
  children all failed to improve on it is not offered for ``improve``; when every
  candidate is stagnant, crossover or a fresh draft is suggested instead.
- **Debug first**: when the latest candidate crashed, fix it (at most ``max_debug``
  consecutive debug attempts on one chain) before spending evaluations elsewhere.

Suggestions are a pure function of the ledger and ``seed``: rerunning gives the same
answer, so a suggestion can be cited in ``hyp.json``.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

from .frontier import dominates
from .interfaces import Objective
from .kg import EXPERIENCE_DIRNAME, _parse_bundles

OPERATORS = ("draft", "improve", "debug", "crossover")
FEASIBLE = ("frontier", "dominated")


def load_rows(run_dir: Path | str) -> list[dict[str, Any]]:
    """Ledger rows (one per bundle) with lineage fields and the recorded operator."""
    warnings: list[str] = []
    experience = Path(run_dir) / EXPERIENCE_DIRNAME
    if not experience.is_dir():
        return []
    return _parse_bundles(experience, warnings)


def objectives_from_frontier(run_dir: Path | str) -> list[Objective]:
    path = Path(run_dir) / "frontier.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Objective(o["name"], direction=o.get("direction", "min")) for o in data.get("objectives", [])]


def _parent_of(row: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not row["parent"]:
        return None
    prior = [r for r in rows if r["name"] == row["parent"] and r["iteration"] < row["iteration"]]
    return max(prior, key=lambda r: r["iteration"]) if prior else None


def _root(row: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    seen = set()
    while row is not None and row["id"] not in seen:
        seen.add(row["id"])
        parent = _parent_of(row, rows)
        if parent is None:
            return row["id"]
        row = parent
    return row["id"] if row else ""


def _children(row: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((r for r in rows if (p := _parent_of(r, rows)) is not None and p["id"] == row["id"]),
                  key=lambda r: r["iteration"])


def _better(a: dict[str, float], b: dict[str, float], objectives: list[Objective]) -> bool:
    """``a`` improves on ``b``: dominates it, or is strictly better on the first objective."""
    if dominates(a, b, objectives):
        return True
    first = objectives[0]
    if first.name in a and first.name in b:
        return first.is_better(a[first.name], b[first.name])
    return False


def stagnant_run(row: dict[str, Any], rows: list[dict[str, Any]], objectives: list[Objective]) -> int:
    """Trailing children of ``row`` (latest first) that did not improve on it."""
    run = 0
    for child in reversed(_children(row, rows)):
        if child["status"] in FEASIBLE and _better(child["scores"], row["scores"], objectives):
            break
        run += 1
    return run


def pareto_layers(rows: list[dict[str, Any]], objectives: list[Objective]) -> list[dict[str, Any]]:
    """Feasible rows ranked by non-dominated layer, then by the first objective."""
    pool = [r for r in rows if r["status"] in FEASIBLE and all(o.name in r["scores"] for o in objectives)]
    ranked, layer = [], 0
    while pool:
        front = [r for r in pool if not any(dominates(o["scores"], r["scores"], objectives) for o in pool if o is not r)]
        first = objectives[0]
        front.sort(key=lambda r: (r["scores"][first.name] * (-1 if first.direction == "max" else 1), r["iteration"]))
        ranked.extend({**r, "layer": layer} for r in front)
        pool = [r for r in pool if all(r is not f for f in front)]
        layer += 1
    return ranked


def rank_weights(n: int, temperature: float) -> list[float]:
    """AIRA_2-style temperature-scaled rank selection: w_i ∝ exp(-(i / (n-1)) / T)."""
    if n == 1:
        return [1.0]
    raw = [math.exp(-(i / (n - 1)) / temperature) for i in range(n)]
    total = sum(raw)
    return [w / total for w in raw]


def _debug_depth(row: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    depth = 0
    while row is not None and row["operator"] == "debug":
        depth += 1
        row = _parent_of(row, rows)
    return depth


def suggest(
    run_dir: Path | str,
    objectives: list[Objective] | None = None,
    *,
    seed: int = 0,
    temperature: float = 0.2,
    crossover_rate: float = 0.15,
    draft_rate: float = 0.1,
    stagnation: int = 3,
    max_debug: int = 2,
    references: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """One recommendation ``{"operator", "parents", "facts", ...}`` from the ledger.

    ``references``: design names that are reference points (e.g. an untrained model, or one built
    outside the space the search may change): they keep their place in the Pareto layering, so they
    still dominate what they beat, but are never suggested as parents."""
    rows = load_rows(run_dir)
    objectives = objectives or objectives_from_frontier(run_dir)
    rng = random.Random(f"{seed}:{len(rows)}")
    facts: list[str] = []
    out = {"seed": seed, "temperature": temperature, "operator": "draft", "parents": [], "facts": facts}
    if not rows or not objectives:
        facts.append("empty ledger or no objectives: start from a draft (or seed the baselines)")
        return out

    latest = max(rows, key=lambda r: r["iteration"])
    if latest["status"] == "crash":
        depth = _debug_depth(latest, rows)
        if depth < max_debug:
            facts.append(f"latest candidate {latest['id']} crashed (debug depth {depth} < {max_debug})")
            return {**out, "operator": "debug", "parents": [latest["id"]]}
        facts.append(f"{latest['id']} crashed after {depth} debug attempts: leave that chain")

    ranked = pareto_layers(rows, objectives)
    if not ranked:
        facts.append("no feasible candidate yet: draft a new design")
        return out
    skipped = [r["id"] for r in ranked if r["name"] in set(references)]
    if skipped:
        ranked = [r for r in ranked if r["name"] not in set(references)]
        facts.append(f"reference points, never parents: {', '.join(skipped)}")
        if not ranked:
            facts.append("only reference points are feasible: draft a new design")
            return out
    stalled = {r["id"]: stagnant_run(r, rows, objectives) for r in ranked}
    open_ = [r for r in ranked if stalled[r["id"]] < stagnation]
    for r in ranked:
        if stalled[r["id"]] >= stagnation:
            facts.append(f"{r['id']} stagnant: last {stalled[r['id']]} children did not improve on it")

    roll = rng.random()
    roots = {r["id"]: _root(r, rows) for r in ranked}
    if roll < draft_rate:
        facts.append(f"exploration draw ({roll:.3f} < draft_rate {draft_rate})")
        return out
    if roll < draft_rate + crossover_rate or not open_:
        if not open_:
            facts.append("every candidate is stagnant: crossover across lineages, else draft")
        else:
            facts.append(f"crossover draw ({roll:.3f} < {draft_rate + crossover_rate:.2f})")
        weights = rank_weights(len(ranked), temperature)
        first = rng.choices(ranked, weights=weights)[0]
        others = [(r, w) for r, w in zip(ranked, weights) if roots[r["id"]] != roots[first["id"]]]
        if others:
            second = rng.choices([r for r, _ in others], weights=[w for _, w in others])[0]
            facts.append(f"lineages {roots[first['id']]} x {roots[second['id']]}")
            return {**out, "operator": "crossover", "parents": [first["id"], second["id"]]}
        facts.append("only one lineage exists: no crossover partner")
        if not open_:
            return out
    weights = rank_weights(len(open_), temperature)
    parent = rng.choices(open_, weights=weights)[0]
    facts.append(f"parent {parent['id']} (Pareto layer {parent['layer']}, rank "
                 f"{open_.index(parent) + 1}/{len(open_)}, p={weights[open_.index(parent)]:.2f})")
    return {**out, "operator": "improve", "parents": [parent["id"]],
            "weights": {r["id"]: round(w, 4) for r, w in list(zip(open_, weights))[:5]}}


def context(run_dir: Path | str, operator: str, parents: list[str], *, recent: int = 3) -> list[tuple[str, str]]:
    """Bundles the proposer should read for ``operator`` (scoped memory, AIRA-dojo), as (bundle, why)."""
    if operator not in OPERATORS:
        raise ValueError(f"operator must be one of {OPERATORS}")
    rows = load_rows(run_dir)
    by_id = {r["id"]: r for r in rows}
    missing = [p for p in parents if p not in by_id]
    if missing:
        raise ValueError(f"unknown bundle(s): {', '.join(missing)} (use bundle ids like 012_name)")
    picks: list[tuple[str, str]] = []

    def add(row_id: str, why: str) -> None:
        if all(row_id != p for p, _ in picks):
            picks.append((row_id, why))

    frontier_path = Path(run_dir) / "frontier.json"
    frontier = json.loads(frontier_path.read_text(encoding="utf-8")).get("pareto", []) if frontier_path.is_file() else []
    if operator == "debug":
        row = by_id[parents[0]]
        while row is not None:
            add(row["id"], "crashed candidate: read result.json error + trace/" if row["status"] == "crash"
                else "last working ancestor of the debug chain")
            if row["status"] != "crash":
                break
            row = _parent_of(row, rows)
    else:
        for p in parents:
            add(p, "parent: design.py, hypothesis.md, trace/")
            for child in _children(by_id[p], rows):
                add(child["id"], f"earlier child of {p} ({child['status']}): what was already tried")
        if operator in ("draft", "crossover") or not parents:
            for f in frontier:
                fid = f"{f['iteration']:03d}_{f['name']}" if f.get("iteration") is not None else f["name"]
                if fid in by_id:
                    add(fid, "Pareto frontier member")
            for r in sorted(rows, key=lambda r: r["iteration"])[-recent:]:
                add(r["id"], f"recent candidate ({r['status']})")
    return picks


__all__ = ["OPERATORS", "suggest", "context", "pareto_layers", "rank_weights", "stagnant_run", "load_rows"]
