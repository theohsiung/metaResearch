"""``NumericalEvaluator`` -- wrap a pure-python ``simulate`` callable (DESIGN §8.1).

This is the runnable default evaluator. The caller supplies a pure-python
``simulate(params, out_dir) -> (scores, metadata, artifacts)`` function (the
domain physics) and a list of objectives. The evaluator is responsible only for
the *deterministic plumbing* around that callable:

  * call ``simulate`` inside a ``try/except`` so a buggy model can never crash the
    research loop -- any exception becomes ``EvalResult.crashed(...)``;
  * verify the returned ``scores`` cover every declared objective and are finite;
  * assemble and return an :class:`EvalResult`.

It implements the :class:`meta_research.interfaces.Evaluator` protocol and, per
the framework contract, **never raises**.

The water-cooling example composes this class: ``objective.py`` defines a
``simulate`` function and constructs ``NumericalEvaluator(OBJECTIVES, simulate)``.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any, Callable

from meta_research.interfaces import DesignSpec, EvalResult, Objective

logger = logging.getLogger(__name__)

# A pure-python model: (params, out_dir) -> (scores, metadata, artifacts).
SimulateFn = Callable[
    [dict[str, Any], Path],
    "tuple[dict[str, float], dict[str, Any], dict[str, str]]",
]


def _is_finite_number(value: Any) -> bool:
    """True iff ``value`` is a real, finite number (not NaN / inf / bool-but-ok)."""
    if isinstance(value, bool):
        # bool is a subclass of int; treat it as a (degenerate) number so a model
        # that returns 0/1 flags still passes -- but it must still be finite.
        return True
    if not isinstance(value, (int, float)):
        return False
    return value == value and abs(value) != float("inf")


class NumericalEvaluator:
    """Evaluate a design with an in-process pure-python ``simulate`` callable.

    Args:
        objectives: Ordered objectives this evaluator scores. ``evaluate`` verifies
            the returned scores cover every name here.
        simulate: ``simulate(params, out_dir) -> (scores, metadata, artifacts)``.
            ``scores`` maps objective name -> float; ``metadata`` is free-form
            secondary metrics (becomes ``result.metadata``); ``artifacts`` maps a
            name -> path (absolute or relative to ``out_dir``) for diagnostics such
            as ``heatmap.png``. The callable should write any files into ``out_dir``.
    """

    def __init__(self, objectives: list[Objective], simulate: SimulateFn) -> None:
        if not objectives:
            raise ValueError("NumericalEvaluator requires at least one objective")
        if not callable(simulate):
            raise TypeError("simulate must be callable")
        self.objectives: list[Objective] = list(objectives)
        self._simulate: SimulateFn = simulate

    def evaluate(self, design: DesignSpec, out_dir: Path) -> EvalResult:
        """Score ``design`` by calling ``simulate``. Never raises.

        Failures (exceptions, missing objectives, non-finite scores) are returned
        as ``EvalResult.crashed(...)`` / infeasible results so the research loop can
        record the experience and continue.
        """
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - filesystem edge case
            logger.exception("NumericalEvaluator: could not create out_dir %s", out_dir)
            return EvalResult.crashed(f"could not create out_dir {out_dir!r}: {exc}")

        params = dict(getattr(design, "params", {}) or {})

        try:
            raw = self._simulate(params, out_dir)
        except Exception as exc:  # noqa: BLE001 - we deliberately swallow everything
            logger.exception("NumericalEvaluator: simulate() raised")
            tb = traceback.format_exc(limit=8)
            return EvalResult.crashed(f"simulate() raised {type(exc).__name__}: {exc}\n{tb}")

        unpacked = self._unpack(raw)
        if isinstance(unpacked, EvalResult):  # an error result from unpacking
            return unpacked
        scores, metadata, artifacts = unpacked

        # Verify every declared objective is present and finite.
        missing = [obj.name for obj in self.objectives if obj.name not in scores]
        if missing:
            return EvalResult(
                scores={k: float(v) for k, v in scores.items() if _is_finite_number(v)},
                feasible=False,
                metadata=metadata,
                artifacts=artifacts,
                error=f"simulate() omitted objective(s): {', '.join(missing)}",
            )

        bad = [name for name in (obj.name for obj in self.objectives) if not _is_finite_number(scores[name])]
        if bad:
            return EvalResult(
                scores={name: self._coerce(scores[name]) for name in scores},
                feasible=False,
                metadata=metadata,
                artifacts=artifacts,
                error=f"simulate() returned non-finite score(s): {', '.join(bad)}",
            )

        feasible = bool(metadata.get("feasible", True))
        return EvalResult(
            scores={obj.name: float(scores[obj.name]) for obj in self.objectives},
            feasible=feasible,
            metadata=metadata,
            artifacts=artifacts,
            error=None,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _coerce(value: Any) -> float:
        """Best-effort float coercion that never raises (NaN sentinel on failure)."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    def _unpack(
        self, raw: Any
    ) -> "tuple[dict[str, float], dict[str, Any], dict[str, str]] | EvalResult":
        """Validate the shape of ``simulate``'s return value. Never raises."""
        if not isinstance(raw, (tuple, list)) or len(raw) != 3:
            return EvalResult.crashed(
                "simulate() must return a 3-tuple (scores, metadata, artifacts); "
                f"got {type(raw).__name__}"
            )
        scores_raw, metadata_raw, artifacts_raw = raw
        if not isinstance(scores_raw, dict):
            return EvalResult.crashed("simulate() scores must be a dict")
        metadata: dict[str, Any] = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        artifacts: dict[str, str] = (
            {str(k): str(v) for k, v in artifacts_raw.items()}
            if isinstance(artifacts_raw, dict)
            else {}
        )
        scores = {str(k): v for k, v in scores_raw.items()}
        return scores, metadata, artifacts


__all__ = ["NumericalEvaluator", "SimulateFn"]
