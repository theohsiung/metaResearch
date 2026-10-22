"""Search-trajectory plot — every candidate over time with its lineage (AIRA_2, arXiv:2603.26499, Fig. 7).

AIRA_2 plots each solution's score in chronological order, joins parents to children
(solid for mutations, dashed for crossovers), draws reference levels as horizontal
lines, and annotates the steps that moved the search. ``progress.png`` shows *that* the
best score improved; this shows *which lineage and which hypothesis* improved it, and
where the search went into dead ends.

Everything comes from the ledger: points and lineage from :func:`meta_research.kg.build_kg`
(``mutated-from`` / ``inspired-by`` edges, the ``operator`` on each node), and each
annotation's text from the bundle's ``change`` line. Read-only and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .experience import EXPERIENCE_DIRNAME, HYPOTHESIS_FILENAME, _read_hypothesis
from .interfaces import Objective
from .kg import build_kg

FEASIBLE = ("frontier", "dominated")
TRAJECTORY_FILENAME = "trajectory.png"


def _change(run_dir: Path, bundle_id: str) -> str:
    front, _ = _read_hypothesis(run_dir / EXPERIENCE_DIRNAME / bundle_id / HYPOTHESIS_FILENAME)
    return str(front.get("change") or "").strip()


def trajectory_data(
    run_dir: Path | str,
    objective: Objective,
    *,
    refs: dict[str, float] | None = None,
    max_label: int = 60,
) -> dict[str, Any]:
    """Points, lineage edges, best-so-far steps and annotations for one objective.

    Edge styles: ``mutation`` (child's operator is not crossover: improve, debug, or
    unlabeled), ``crossover`` (both parents of a crossover child), ``inspiration``
    (an ``inspired_by`` pointer of a non-crossover child).
    """
    run_dir = Path(run_dir)
    kg = build_kg(run_dir)
    nodes = {n["id"]: n for n in kg["nodes"]}
    points = []
    for n in sorted(kg["nodes"], key=lambda n: (n["iteration"], n["name"])):
        value = n["scores"].get(objective.name)
        points.append({"id": n["id"], "iteration": n["iteration"], "name": n["name"], "status": n["status"],
                       "operator": n.get("operator", ""),
                       "value": value if n["status"] in FEASIBLE else None})
    edges = []
    for e in kg["edges"]:
        if e["src"] not in nodes or e["dst"] not in nodes:
            continue
        crossover = nodes[e["dst"]].get("operator") == "crossover"
        if e["kind"] == "mutated-from":
            style = "crossover" if crossover else "mutation"
        else:
            style = "crossover" if crossover else "inspiration"
        edges.append({"src": e["src"], "dst": e["dst"], "style": style})

    best, steps, jumps = None, [], []
    for p in points:
        if p["value"] is None:
            continue
        if best is None or objective.is_better(p["value"], best):
            best = p["value"]
            jumps.append(p["id"])
        steps.append((p["iteration"], best))

    frontier_ids = set()
    frontier_path = run_dir / "frontier.json"
    if frontier_path.is_file():
        for f in json.loads(frontier_path.read_text(encoding="utf-8")).get("pareto", []):
            if f.get("iteration") is not None:
                frontier_ids.add(f"{f['iteration']:03d}_{f['name']}")
    for p in points:
        # Colour by the frontier as it stands now; a bundle's recorded status is its status at eval time.
        if p["status"] in FEASIBLE:
            p["now"] = "frontier" if p["id"] in frontier_ids else "dominated"
        else:
            p["now"] = p["status"]
    annotated = [p for p in points if p["id"] in jumps or p["id"] in frontier_ids]
    annotations = []
    for p in annotated:
        text = _change(run_dir, p["id"]) or p["name"]
        if len(text) > max_label:
            text = text[: max_label - 1] + "…"
        why = "best-so-far" if p["id"] in jumps else "frontier"
        annotations.append({"id": p["id"], "label": f"{p['iteration']}: {text}", "why": why})
    return {"objective": objective.name, "direction": objective.direction, "points": points, "edges": edges,
            "best_so_far": steps, "annotations": annotations, "refs": dict(refs or {})}


_STATUS_STYLE = {
    "frontier": {"color": "#1b9e77", "marker": "o", "label": "on the Pareto frontier now"},
    "dominated": {"color": "#7570b3", "marker": "o", "label": "dominated now"},
    "infeasible": {"color": "#d95f02", "marker": "s", "label": "infeasible"},
    "crash": {"color": "#e7298a", "marker": "x", "label": "crash"},
}
_EDGE_STYLE = {
    "mutation": {"linestyle": "-", "color": "#555555", "alpha": 0.6},
    "crossover": {"linestyle": "--", "color": "#d95f02", "alpha": 0.8},
    "inspiration": {"linestyle": ":", "color": "#999999", "alpha": 0.5},
}


def render_trajectory(data: dict[str, Any], out: Path | str) -> Path:
    """Draw ``data`` (from :func:`trajectory_data`) to a PNG and return its path."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = Path(out)
    points = {p["id"]: p for p in data["points"]}
    values = [p["value"] for p in data["points"] if p["value"] is not None] + list(data["refs"].values())
    low = min(values) if values else 0.0
    high = max(values) if values else 1.0
    span = (high - low) or 1.0
    floor = low - 0.08 * span  # where candidates without a score (crash, infeasible) are drawn

    def y(p: dict[str, Any]) -> float:
        return p["value"] if p["value"] is not None else floor

    fig, ax = plt.subplots(figsize=(12, 6.5))
    for e in data["edges"]:
        a, b = points[e["src"]], points[e["dst"]]
        ax.plot([a["iteration"], b["iteration"]], [y(a), y(b)], zorder=1, linewidth=1.2, **_EDGE_STYLE[e["style"]])
    for status, style in _STATUS_STYLE.items():
        sel = [p for p in data["points"] if p["now"] == status]
        if sel:
            ax.scatter([p["iteration"] for p in sel], [y(p) for p in sel], s=46, zorder=3,
                       label=style["label"], color=style["color"], marker=style["marker"])
    if any(p["value"] is None for p in data["points"]):
        ax.axhline(floor, color="#e7298a", linewidth=0.6, alpha=0.4, zorder=0)
        ax.annotate("no score (crash / infeasible)", xy=(0.0, floor), xycoords=("axes fraction", "data"),
                    xytext=(4, 4), textcoords="offset points", fontsize=7.5, color="#e7298a")
    if data["best_so_far"]:
        xs = [s[0] for s in data["best_so_far"]]
        ys = [s[1] for s in data["best_so_far"]]
        ax.step(xs, ys, where="post", color="#1b9e77", linewidth=2, alpha=0.35, zorder=2, label="best so far")
    for i, (label, value) in enumerate(sorted(data["refs"].items(), key=lambda kv: kv[1])):
        ax.axhline(value, linestyle="--", linewidth=1, color="#888888", zorder=0)
        ax.annotate(f"{label} {value:g}", xy=(1.0, value), xycoords=("axes fraction", "data"), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8, color="#555555")
    for k, note in enumerate(data["annotations"]):
        p = points[note["id"]]
        ax.annotate(note["label"], xy=(p["iteration"], y(p)), xytext=(8, 10 + 11 * (k % 3)), textcoords="offset points",
                    fontsize=7.5, arrowprops={"arrowstyle": "-", "color": "#bbbbbb", "lw": 0.6},
                    bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "#dddddd", "alpha": 0.9})
    for style, spec in _EDGE_STYLE.items():
        if any(e["style"] == style for e in data["edges"]):
            ax.plot([], [], label=style, linewidth=1.2, **spec)
    ax.set_xlabel("iteration")
    arrow = "↑" if data["direction"] == "max" else "↓"
    ax.set_ylabel(f"{data['objective']} ({arrow} better)")
    ax.set_title(f"Search trajectory — {data['objective']}")
    ax.legend(loc="best", fontsize=8, ncol=2)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


__all__ = ["trajectory_data", "render_trajectory", "TRAJECTORY_FILENAME"]
