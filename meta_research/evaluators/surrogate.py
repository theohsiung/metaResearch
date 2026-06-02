"""``SurrogateEvaluator`` -- load and call a user-provided surrogate model (DESIGN §8.2).

A surrogate replaces an expensive simulation with a cheap learned/analytic model.
This adapter is deliberately storage-agnostic: it accepts an already-loaded model
object plus two callables, or loads a model from a path via :meth:`from_path`.

  * ``feature_fn(params) -> features`` turns a :class:`DesignSpec`'s params into
    whatever the model consumes (a numpy array, a dict, a DataFrame row, ...).
  * ``predict_fn(model, params) -> scores`` runs the model and returns a
    ``objective_name -> float`` dict. If omitted, a default tries common shapes
    (``model.predict(features)`` returning a sequence aligned to ``objectives`` or a
    dict) so scikit-learn-style and callable models work out of the box.

Optional dependencies (``joblib``, ``onnxruntime``, ``torch``) are **soft imports**:
they are only imported inside the loader branch that needs them, so importing this
module never fails. ``evaluate`` never raises.

Example::

    from meta_research.interfaces import Objective
    from meta_research.evaluators import SurrogateEvaluator
    import numpy as np

    objectives = [Objective("thermal_resistance", "min"),
                  Objective("pressure_drop", "min")]

    def feature_fn(params):
        return np.array([[params["fin_height_mm"], params["n_fins"]]])

    ev = SurrogateEvaluator.from_path(
        objectives, "models/cold_plate.joblib", feature_fn=feature_fn)
    result = ev.evaluate(design, out_dir)
"""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import Any, Callable

from meta_research.interfaces import DesignSpec, EvalResult, Objective

logger = logging.getLogger(__name__)

FeatureFn = Callable[[dict[str, Any]], Any]
PredictFn = Callable[[Any, dict[str, Any]], "dict[str, float]"]


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if not isinstance(value, (int, float)):
        return False
    return value == value and abs(value) != float("inf")


class SurrogateEvaluator:
    """Score a design by calling a loaded surrogate model.

    Args:
        objectives: Ordered objectives this evaluator scores.
        model: An already-loaded model object (callable, sklearn estimator,
            onnxruntime session, torch module, ...).
        feature_fn: ``params -> features`` adapter feeding the model.
        predict_fn: Optional ``(model, params) -> {objective: value}``. When None a
            default attempts ``model.predict(feature_fn(params))`` and aligns the
            output to ``objectives`` (sequence) or uses it directly (dict).
    """

    def __init__(
        self,
        objectives: list[Objective],
        model: Any,
        feature_fn: FeatureFn,
        predict_fn: PredictFn | None = None,
    ) -> None:
        if not objectives:
            raise ValueError("SurrogateEvaluator requires at least one objective")
        if model is None:
            raise ValueError("SurrogateEvaluator requires a non-None model")
        if not callable(feature_fn):
            raise TypeError("feature_fn must be callable")
        if predict_fn is not None and not callable(predict_fn):
            raise TypeError("predict_fn must be callable or None")
        self.objectives: list[Objective] = list(objectives)
        self._model = model
        self._feature_fn = feature_fn
        self._predict_fn = predict_fn

    # --------------------------------------------------------------- loaders

    @classmethod
    def from_path(
        cls,
        objectives: list[Objective],
        model_path: str,
        feature_fn: FeatureFn,
        predict_fn: PredictFn | None = None,
    ) -> "SurrogateEvaluator":
        """Construct by loading a model artifact from ``model_path``.

        Load order:
          1. ``"module:attr"`` -- import ``module`` and use its ``attr`` as a python
             callable / object (no file needed; for analytic surrogates).
          2. ``*.joblib`` -- ``joblib.load`` (soft import).
          3. ``*.onnx`` -- wrap an ``onnxruntime.InferenceSession`` (soft import).
          4. anything else -- ``pickle.load``.

        Raises ``ImportError`` / ``FileNotFoundError`` only here (construction time),
        never inside :meth:`evaluate`.
        """
        model = cls._load_model(model_path)
        return cls(objectives, model, feature_fn, predict_fn)

    @staticmethod
    def _load_model(model_path: str) -> Any:
        """Resolve ``model_path`` to a model object. May raise at construction time."""
        # 1. "module:attr" python callable / object.
        if ":" in model_path and not Path(model_path).exists():
            module_name, _, attr = model_path.partition(":")
            if not module_name or not attr:
                raise ValueError(
                    f"invalid 'module:attr' spec {model_path!r}: both sides required"
                )
            import importlib

            module = importlib.import_module(module_name)
            try:
                return getattr(module, attr)
            except AttributeError as exc:
                raise AttributeError(
                    f"module {module_name!r} has no attribute {attr!r}"
                ) from exc

        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"surrogate model artifact not found: {model_path}")

        suffix = path.suffix.lower()

        # 2. joblib (soft import).
        if suffix == ".joblib":
            try:
                import joblib  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dep
                raise ImportError(
                    "loading a .joblib surrogate requires the optional 'joblib' "
                    "package (pip install joblib)"
                ) from exc
            return joblib.load(path)

        # 3. ONNX (soft import) -- wrap an InferenceSession so predict_fn can run it.
        if suffix == ".onnx":
            try:
                import onnxruntime  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dep
                raise ImportError(
                    "loading a .onnx surrogate requires the optional 'onnxruntime' "
                    "package (pip install onnxruntime)"
                ) from exc
            return onnxruntime.InferenceSession(str(path))

        # 4. Fallback: pickle.
        import pickle  # noqa: S403 - user-supplied trusted artifact

        with path.open("rb") as fh:
            return pickle.load(fh)  # noqa: S301 - trusted local artifact

    # --------------------------------------------------------------- scoring

    def evaluate(self, design: DesignSpec, out_dir: Path) -> EvalResult:
        """Predict scores for ``design``. Never raises.

        Writes ``prediction.json`` (the params + raw predicted scores) into
        ``out_dir`` as the diagnostic artifact; no heatmap is produced.
        """
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - filesystem edge case
            logger.exception("SurrogateEvaluator: could not create out_dir %s", out_dir)
            return EvalResult.crashed(f"could not create out_dir {out_dir!r}: {exc}")

        params = dict(getattr(design, "params", {}) or {})

        try:
            scores_raw = self._predict(params)
        except Exception as exc:  # noqa: BLE001 - never let the model crash the loop
            logger.exception("SurrogateEvaluator: prediction raised")
            tb = traceback.format_exc(limit=8)
            return EvalResult.crashed(
                f"surrogate prediction raised {type(exc).__name__}: {exc}\n{tb}"
            )

        if not isinstance(scores_raw, dict):
            return EvalResult.crashed(
                "surrogate predict must yield a dict {objective: value}; "
                f"got {type(scores_raw).__name__}"
            )
        scores = {str(k): v for k, v in scores_raw.items()}

        artifacts = self._write_prediction(out_dir, params, scores)

        missing = [obj.name for obj in self.objectives if obj.name not in scores]
        if missing:
            return EvalResult(
                scores={k: float(v) for k, v in scores.items() if _is_finite_number(v)},
                feasible=False,
                metadata={},
                artifacts=artifacts,
                error=f"surrogate omitted objective(s): {', '.join(missing)}",
            )

        bad = [obj.name for obj in self.objectives if not _is_finite_number(scores[obj.name])]
        if bad:
            return EvalResult(
                scores={
                    obj.name: self._coerce(scores[obj.name]) for obj in self.objectives
                },
                feasible=False,
                metadata={},
                artifacts=artifacts,
                error=f"surrogate returned non-finite score(s): {', '.join(bad)}",
            )

        return EvalResult(
            scores={obj.name: float(scores[obj.name]) for obj in self.objectives},
            feasible=True,
            metadata={"surrogate": True},
            artifacts=artifacts,
            error=None,
        )

    # ------------------------------------------------------------------ helpers

    def _predict(self, params: dict[str, Any]) -> Any:
        """Run the user predict_fn, or a sensible default. May raise (caught above)."""
        if self._predict_fn is not None:
            return self._predict_fn(self._model, params)

        features = self._feature_fn(params)
        # Callable model (e.g. a lambda or a torch module's __call__).
        if not hasattr(self._model, "predict") and callable(self._model):
            raw = self._model(features)
        elif hasattr(self._model, "predict"):
            raw = self._model.predict(features)
        else:
            raise TypeError(
                "model is neither callable nor has a .predict method; "
                "supply predict_fn explicitly"
            )
        return self._align(raw)

    def _align(self, raw: Any) -> dict[str, float]:
        """Map a raw model output onto the objective names. May raise (caught above)."""
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items()}

        # Flatten common numpy/sequence shapes like [[a, b]] -> [a, b].
        values = self._flatten(raw)
        if len(values) != len(self.objectives):
            raise ValueError(
                f"model returned {len(values)} value(s) but there are "
                f"{len(self.objectives)} objective(s); supply predict_fn to map them"
            )
        return {obj.name: values[i] for i, obj in enumerate(self.objectives)}

    @staticmethod
    def _flatten(raw: Any) -> list[Any]:
        """Flatten a (possibly nested / numpy) sequence to a flat python list."""
        # numpy array -> ravel via tolist without importing numpy.
        if hasattr(raw, "ravel") and hasattr(raw, "tolist"):
            flat = raw.ravel().tolist()
            return list(flat) if isinstance(flat, (list, tuple)) else [flat]
        if isinstance(raw, (list, tuple)):
            out: list[Any] = []
            for item in raw:
                if isinstance(item, (list, tuple)) or (
                    hasattr(item, "tolist") and hasattr(item, "ravel")
                ):
                    out.extend(SurrogateEvaluator._flatten(item))
                else:
                    out.append(item)
            return out
        return [raw]

    @staticmethod
    def _coerce(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    @staticmethod
    def _write_prediction(
        out_dir: Path, params: dict[str, Any], scores: dict[str, Any]
    ) -> dict[str, str]:
        """Write ``prediction.json``; failure to write is non-fatal."""
        payload = {
            "params": params,
            "scores": {k: SurrogateEvaluator._coerce(v) for k, v in scores.items()},
        }
        try:
            path = out_dir / "prediction.json"
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            return {"prediction": "prediction.json"}
        except Exception:  # pragma: no cover - non-fatal diagnostic write
            logger.warning("SurrogateEvaluator: could not write prediction.json", exc_info=True)
            return {}


__all__ = ["SurrogateEvaluator", "FeatureFn", "PredictFn"]
