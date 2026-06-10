"""meta-research — a general-purpose autonomous design-research framework.

This package provides the **deterministic tools** layer (DESIGN §1.1.6): evaluate a
design, store it as append-only experience, recompute the Pareto frontier, and
optionally commit. The *intelligence* (propose, diagnose, decide) is the agent
following the ``meta-research`` skill in-session — there is no subprocess
orchestrator here.

Public API (re-exported below):

- Contracts (``interfaces``): :class:`Objective`, :class:`DesignSpec`,
  :class:`EvalResult`, :class:`Candidate`, :class:`Evaluator`.
- Pareto frontier (``frontier``): :func:`pareto_front`, :func:`dominates`,
  :func:`update_frontier`.
- Experience store (``experience``): :class:`Experience`.
- Deterministic step (``runner``): :func:`evaluate_and_record`, :func:`seed_baselines`.
- Evaluator adapters (``evaluators``): :class:`NumericalEvaluator`,
  :class:`SurrogateEvaluator`, :class:`ApiSolverEvaluator` — imported defensively so
  that a missing optional dependency does not break ``import meta_research``.
"""

from __future__ import annotations

from .interfaces import (
    Candidate,
    DesignSpec,
    EvalResult,
    Evaluator,
    Objective,
)
from .frontier import dominates, pareto_front, update_frontier
from .experience import Experience
from .kg import build_kg, write_kg
from .runner import evaluate_and_record, seed_baselines

__version__ = "0.1.0"

__all__ = [
    # interfaces
    "Objective",
    "DesignSpec",
    "EvalResult",
    "Candidate",
    "Evaluator",
    # frontier
    "pareto_front",
    "dominates",
    "update_frontier",
    # experience
    "Experience",
    # knowledge graph
    "build_kg",
    "write_kg",
    # runner
    "evaluate_and_record",
    "seed_baselines",
]

# Evaluator adapters may pull optional deps (joblib, onnxruntime, requests, ...) at
# import time. Importing meta_research must never fail because one is absent, so each
# adapter is imported defensively and only added to the public API when available.
try:
    from .evaluators import NumericalEvaluator  # noqa: F401
except Exception:  # pragma: no cover - optional/under-construction
    NumericalEvaluator = None  # type: ignore[assignment]
else:
    __all__.append("NumericalEvaluator")

try:
    from .evaluators import SurrogateEvaluator  # noqa: F401
except Exception:  # pragma: no cover - optional/under-construction
    SurrogateEvaluator = None  # type: ignore[assignment]
else:
    __all__.append("SurrogateEvaluator")

try:
    from .evaluators import ApiSolverEvaluator  # noqa: F401
except Exception:  # pragma: no cover - optional/under-construction
    ApiSolverEvaluator = None  # type: ignore[assignment]
else:
    __all__.append("ApiSolverEvaluator")
