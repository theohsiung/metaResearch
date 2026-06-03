"""Progress plots over the experience ledger — the autoresearch ``progress.png`` analog.

autoresearch ships a single ``progress.png`` showing its scalar metric (``val_bpb``)
improving over experiments. meta-research is multi-objective, so this produces:

* ``progress_<objective>.png`` — one per objective: every evaluated candidate scattered
  by experiment index, with the **best-so-far** step line (the direct ``val_bpb``-curve
  analog, respecting each objective's min/max direction).
* ``progress_pareto[...].png`` — the **Pareto frontier** in objective space, candidates
  coloured by experiment order so you can see the front advance (one scatter per pair of
  objectives when there are more than two).
* ``progress.png`` — a headline montage of the per-objective best-so-far curves, so the
  filename matches autoresearch's.

This module only *reads* the append-only ledger (``experience/`` via
:class:`meta_research.experience.Experience`) and writes PNGs; it never mutates state.
It is deterministic (DESIGN.md 10): no ``datetime.now()`` / random.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless: never try to open a display
import matplotlib.pyplot as plt  # noqa: E402  (must follow use("Agg"))

from meta_research.experience import Experience
from meta_research.frontier import pareto_front
from meta_research.interfaces import Objective

logger = logging.getLogger(__name__)

#: Statuses whose candidates count as feasible, finite contributions to a curve.
_FEASIBLE_STATUSES = ("frontier", "dominated")


def _is_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def gather_rows(run_dir: Path | str, objectives: list[Objective]) -> list[dict[str, Any]]:
    """Read the ledger into experiment rows in evaluation order.

    Each row: ``{"idx", "iteration", "name", "scores", "feasible"}`` where ``idx``
    is the 1-based experiment counter (the x-axis, like autoresearch's experiment
    number) and ``feasible`` means the candidate has finite scores and was not
    infeasible/crashed.
    """
    history = Experience(run_dir, objectives).history()
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(history, start=1):
        scores = row.get("scores") or {}
        finite = all(_is_finite(scores.get(o.name)) for o in objectives)
        feasible = finite and row.get("status") in _FEASIBLE_STATUSES
        rows.append(
            {
                "idx": idx,
                "iteration": row.get("iteration"),
                "name": row.get("name", "?"),
                "scores": scores,
                "feasible": feasible,
            }
        )
    return rows


def _best_so_far(rows: list[dict[str, Any]], obj: Objective) -> tuple[list[int], list[float]]:
    """Cumulative best value of ``obj`` over feasible rows, in experiment order."""
    xs: list[int] = []
    ys: list[float] = []
    best: float | None = None
    for row in rows:
        if not row["feasible"]:
            continue
        val = float(row["scores"][obj.name])
        if best is None or obj.is_better(val, best):
            best = val
        xs.append(row["idx"])
        ys.append(best)
    return xs, ys


def _plot_objective(rows: list[dict[str, Any]], obj: Objective, ax: "plt.Axes") -> None:
    """Draw one objective's scatter + best-so-far curve onto ``ax``."""
    feas = [r for r in rows if r["feasible"]]
    infeas = [r for r in rows if not r["feasible"] and _is_finite(r["scores"].get(obj.name))]

    if feas:
        ax.scatter(
            [r["idx"] for r in feas],
            [r["scores"][obj.name] for r in feas],
            s=28, alpha=0.5, color="tab:blue", label="evaluated",
        )
    if infeas:
        ax.scatter(
            [r["idx"] for r in infeas],
            [r["scores"][obj.name] for r in infeas],
            s=28, marker="x", color="0.6", label="infeasible/crash",
        )

    bx, by = _best_so_far(rows, obj)
    if bx:
        ax.step(bx, by, where="post", color="tab:red", linewidth=2.0, label="best so far")
        arrow = "↓" if obj.direction == "min" else "↑"
        ax.set_title(f"{obj.name} ({arrow})   best={by[-1]:.4g}{(' ' + obj.unit) if obj.unit else ''}")
    else:
        ax.set_title(f"{obj.name} — no feasible candidates yet")

    ax.set_xlabel("experiment")
    ax.set_ylabel(f"{obj.name}" + (f" [{obj.unit}]" if obj.unit else ""))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize="small")


def _plot_pareto_pair(
    rows: list[dict[str, Any]],
    ox: Objective,
    oy: Objective,
    objectives: list[Objective],
    out_path: Path,
) -> Path | None:
    """Scatter all feasible candidates in (ox, oy) space; highlight the Pareto front."""
    feas = [r for r in rows if r["feasible"]]
    if not feas:
        return None

    fig, ax = plt.subplots(figsize=(6.4, 5.2))
    xs = [r["scores"][ox.name] for r in feas]
    ys = [r["scores"][oy.name] for r in feas]
    cs = [r["idx"] for r in feas]
    sc = ax.scatter(xs, ys, c=cs, cmap="viridis", s=40, alpha=0.85)
    cbar = fig.colorbar(sc, ax=ax)
    cbar.set_label("experiment order")

    # Current Pareto front (over ALL objectives) drawn as a connected line in this plane.
    front = pareto_front(
        [{"name": r["name"], "scores": r["scores"], "feasible": True} for r in feas],
        objectives,
    )
    if front:
        pts = sorted(((f["scores"][ox.name], f["scores"][oy.name]) for f in front))
        ax.plot(
            [p[0] for p in pts], [p[1] for p in pts],
            "-o", color="tab:red", linewidth=1.8, markersize=6, label="Pareto frontier",
        )
        ax.legend(loc="best", fontsize="small")

    ax.set_xlabel(f"{ox.name}" + (f" [{ox.unit}] " if ox.unit else " ") + ("↓" if ox.direction == "min" else "↑"))
    ax.set_ylabel(f"{oy.name}" + (f" [{oy.unit}] " if oy.unit else " ") + ("↓" if oy.direction == "min" else "↑"))
    ax.set_title("Pareto frontier (lower-left is better for min objectives)")
    ax.grid(True, alpha=0.3)
    _save(fig, out_path)
    return out_path


def _save(fig: "plt.Figure", path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_progress(
    run_dir: Path | str,
    objectives: list[Objective],
    out_dir: Path | str | None = None,
) -> list[Path]:
    """Render all progress plots for ``run_dir``; return the PNG paths written.

    Produces ``progress_<obj>.png`` per objective, ``progress_pareto*.png`` for each
    objective pair (when there are >=2 objectives), and a headline ``progress.png``
    montage. Returns ``[]`` (with a warning) when the ledger has no candidates yet.
    Never raises for ordinary data/IO problems.
    """
    run_path = Path(run_dir)
    out_path = Path(out_dir) if out_dir is not None else run_path
    objectives = list(objectives)

    rows = gather_rows(run_path, objectives)
    if not rows:
        logger.warning("no experience bundles under %s — nothing to plot", run_path / "experience")
        return []

    written: list[Path] = []

    # 1) Per-objective best-so-far curves (the val_bpb-curve analog).
    for obj in objectives:
        fig, ax = plt.subplots(figsize=(7.0, 4.2))
        _plot_objective(rows, obj, ax)
        p = out_path / f"progress_{_slug(obj.name)}.png"
        _save(fig, p)
        written.append(p)

    # 2) Pareto-frontier scatter(s) — one per objective pair.
    if len(objectives) >= 2:
        pairs = [
            (objectives[i], objectives[j])
            for i in range(len(objectives))
            for j in range(i + 1, len(objectives))
        ]
        for ox, oy in pairs:
            suffix = "" if len(pairs) == 1 else f"_{_slug(ox.name)}_vs_{_slug(oy.name)}"
            p = out_path / f"progress_pareto{suffix}.png"
            if _plot_pareto_pair(rows, ox, oy, objectives, p) is not None:
                written.append(p)

    # 3) Headline montage named progress.png (parity with autoresearch).
    montage = _plot_montage(rows, objectives, out_path / "progress.png")
    if montage is not None:
        written.append(montage)

    logger.info("wrote %d progress plot(s) to %s", len(written), out_path)
    return written


def _plot_montage(
    rows: list[dict[str, Any]],
    objectives: list[Objective],
    out_path: Path,
) -> Path | None:
    """One figure with every objective's best-so-far curve side by side."""
    n = len(objectives)
    if n == 0:
        return None
    fig, axes = plt.subplots(1, n, figsize=(6.5 * n, 4.2), squeeze=False)
    for ax, obj in zip(axes[0], objectives):
        _plot_objective(rows, obj, ax)
    fig.suptitle("meta-research progress", fontweight="bold")
    _save(fig, out_path)
    return out_path


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(name)).strip("_") or "objective"


__all__ = ["plot_progress", "gather_rows"]
