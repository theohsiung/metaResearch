"""Pluggable :class:`~meta_research.interfaces.Evaluator` adapters (DESIGN §8).

Three adapters live behind the single ``Evaluator`` protocol; ``prepare.py`` picks
one in ``make_evaluator()`` (configuration-as-code):

  * :class:`NumericalEvaluator` -- wrap a pure-python ``simulate`` callable (the
    runnable default; the water-cooling example composes it).
  * :class:`SurrogateEvaluator` -- load and call a user model artifact
    (``module:attr`` callable / joblib / pickle / onnxruntime).
  * :class:`ApiSolverEvaluator` -- POST to a REST endpoint or shell out to a CLI.

``NumericalEvaluator`` has no third-party requirements and always imports.
``SurrogateEvaluator`` and ``ApiSolverEvaluator`` only *soft*-import their optional
backends (``joblib``/``onnxruntime``/``requests``) at call time, so importing this
package never hard-fails. As belt-and-suspenders, the surrogate/api imports below
are still guarded: if either module fails to import for any reason, its name is
bound to ``None`` and the failure is recorded in ``IMPORT_ERRORS`` rather than
raising at ``import meta_research.evaluators`` time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meta_research.evaluators.numerical import NumericalEvaluator

# Records why an optional adapter could not be imported (name -> error string).
IMPORT_ERRORS: dict[str, str] = {}

try:
    from meta_research.evaluators.surrogate import SurrogateEvaluator
except Exception as exc:  # noqa: BLE001 - never hard-fail the package import
    SurrogateEvaluator = None  # type: ignore[assignment,misc]
    IMPORT_ERRORS["SurrogateEvaluator"] = f"{type(exc).__name__}: {exc}"

try:
    from meta_research.evaluators.api_solver import ApiSolverEvaluator
except Exception as exc:  # noqa: BLE001 - never hard-fail the package import
    ApiSolverEvaluator = None  # type: ignore[assignment,misc]
    IMPORT_ERRORS["ApiSolverEvaluator"] = f"{type(exc).__name__}: {exc}"

if TYPE_CHECKING:  # give type checkers the concrete types regardless of guards
    from meta_research.evaluators.api_solver import ApiSolverEvaluator
    from meta_research.evaluators.surrogate import SurrogateEvaluator

__all__ = [
    "NumericalEvaluator",
    "SurrogateEvaluator",
    "ApiSolverEvaluator",
    "IMPORT_ERRORS",
]
