"""Core contracts for meta-research.

Everything in the framework depends on the small set of types defined here, so
this module deliberately has **no third-party imports** and must stay stable.

The two pluggable contracts are:

- ``Candidate`` -- the artifact the research agent edits. By default a single
  Python module exposing ``build() -> DesignSpec`` (the analog of autoresearch's
  ``train.py``). It is *parametric* by default but ``DesignSpec`` can carry paths
  to free geometry (STEP / mesh) so high-fidelity evaluators work too.

- ``Evaluator`` -- scores a design and emits diagnostic artifacts. This is the
  generality hinge: the concrete evaluator may call a real-simulation API, load a
  user-provided surrogate model, or run a simple in-process numerical model. All
  three live behind this one interface and are selected from config.

Both ``Candidate`` and ``Evaluator`` are :class:`typing.Protocol` so domain code
never has to import the framework to satisfy them -- duck typing is enough.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# Allowed optimization directions for an objective.
DIRECTIONS = ("min", "max")


@dataclass(frozen=True)
class Objective:
    """A single optimization objective.

    Attributes:
        name: Key used in ``EvalResult.scores`` and in the experience log.
        direction: ``"min"`` or ``"max"`` -- which way is better.
        unit: Human-facing unit string (e.g. ``"K/W"``, ``"Pa"``). Display only.
        weight: Optional weight for scalarization / tie-breaking. Pareto ranking
            does not use it; it is only consulted when a single scalar is needed.
    """

    name: str
    direction: str = "min"
    unit: str = ""
    weight: float = 1.0

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise ValueError(
                f"Objective {self.name!r} has direction {self.direction!r}; "
                f"expected one of {DIRECTIONS}"
            )

    def is_better(self, a: float, b: float) -> bool:
        """Return True if score ``a`` is strictly better than ``b`` for this objective."""
        return a < b if self.direction == "min" else a > b


@dataclass(frozen=True)
class DesignSpec:
    """A concrete candidate design produced by a :class:`Candidate`.

    Attributes:
        params: JSON-serializable knobs that fully describe the design. The
            evaluator consumes these. Keep everything here serializable so the
            design can be snapshotted into the experience filesystem.
        artifacts: Optional ``name -> path`` map pointing at heavier inputs the
            evaluator may need (a generated STEP file, a mesh, a boundary-condition
            file). Empty for purely parametric designs.
        notes: Free-text rationale from the proposer (what idea this encodes).
    """

    params: dict[str, Any]
    artifacts: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"params": self.params, "artifacts": self.artifacts, "notes": self.notes}

    @staticmethod
    def from_json(data: dict[str, Any]) -> "DesignSpec":
        return DesignSpec(
            params=dict(data.get("params", {})),
            artifacts=dict(data.get("artifacts", {})),
            notes=str(data.get("notes", "")),
        )


@dataclass(frozen=True)
class EvalResult:
    """The outcome of evaluating one design.

    Attributes:
        scores: ``objective_name -> value`` for every objective. The keys MUST
            cover every :class:`Objective` the evaluator declares.
        feasible: False if the design violates a hard constraint (still recorded,
            but excluded from the Pareto frontier).
        metadata: Secondary metrics and solver bookkeeping (Reynolds number,
            mesh size, wall-clock, fidelity level, ...). Display / analysis only.
        artifacts: ``name -> path`` for diagnostics the proposer should be able to
            read back next iteration -- e.g. a temperature ``heatmap.png``. This is
            the closing of the loop: the agent literally re-reads these.
        error: Populated when evaluation crashed; ``feasible`` should then be False.
    """

    scores: dict[str, float]
    feasible: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True if the evaluation produced usable, finite scores."""
        return self.error is None and all(
            isinstance(v, (int, float)) and v == v and abs(v) != float("inf")
            for v in self.scores.values()
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "scores": self.scores,
            "feasible": self.feasible,
            "metadata": self.metadata,
            "artifacts": self.artifacts,
            "error": self.error,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "EvalResult":
        return EvalResult(
            scores={k: float(v) for k, v in (data.get("scores") or {}).items()},
            feasible=bool(data.get("feasible", True)),
            metadata=dict(data.get("metadata", {})),
            artifacts=dict(data.get("artifacts", {})),
            error=data.get("error"),
        )

    @staticmethod
    def crashed(error: str) -> "EvalResult":
        """Construct a failed result for a candidate that could not be evaluated."""
        return EvalResult(scores={}, feasible=False, metadata={}, artifacts={}, error=error)


@runtime_checkable
class Candidate(Protocol):
    """The artifact the agent edits.

    A candidate module satisfies this by exposing a module-level ``build()``
    returning a :class:`DesignSpec`, or by defining a class with ``name`` and
    ``build``. Loading/validation lives in ``meta_research.experience``.
    """

    name: str

    def build(self) -> DesignSpec:
        """Construct the concrete design. Must succeed with no prior state (cold start)."""
        ...


@runtime_checkable
class Evaluator(Protocol):
    """Scores a design. The pluggable generality hinge.

    Concrete implementations (see ``meta_research.evaluators``):
      * ``NumericalEvaluator``  -- a simple in-process model (runnable default).
      * ``SurrogateEvaluator``  -- load and call a user-provided model artifact.
      * ``ApiSolverEvaluator``  -- POST to a REST endpoint or shell out to a CLI.

    Contract:
      * ``objectives`` declares what ``evaluate`` will score, in order.
      * ``evaluate`` is given a writable ``out_dir`` for this candidate and MUST
        return an :class:`EvalResult` whose ``scores`` cover every objective.
        Diagnostic files (heatmaps, fields, logs) go under ``out_dir`` and are
        referenced by relative path in ``EvalResult.artifacts``.
      * Never raise: wrap failures in ``EvalResult.crashed(...)``.
    """

    objectives: list[Objective]

    def evaluate(self, design: DesignSpec, out_dir: Path) -> EvalResult:
        ...


__all__ = [
    "DIRECTIONS",
    "Objective",
    "DesignSpec",
    "EvalResult",
    "Candidate",
    "Evaluator",
    "replace",
]
