"""``ApiSolverEvaluator`` -- call an external solver (DESIGN §8.3).

Two modes, both stdlib-only (``requests`` is an *optional* speed-up for REST):

  * **REST mode** -- POST ``design.params`` as a JSON body to ``endpoint`` and hand
    the decoded JSON response to ``parse_fn(response_json) -> (scores, metadata,
    artifacts)``.
  * **CLI mode** -- render ``command_template`` (a ``str.format`` template that may
    reference ``out_dir``, ``params_json`` (a written input file), and any param
    key), run it under ``subprocess`` with a ``timeout``, capture stdout to
    ``solver.log``, then call ``parse_fn(out_dir) -> (scores, metadata, artifacts)``
    to read whatever files the solver produced.

The adapter is **not runnable without a real endpoint / solver binary**; it ships a
clear stub + the example below. ``evaluate`` never raises -- timeouts, non-zero exit
codes, connection errors, and bad responses all become ``EvalResult.crashed(...)``.

REST example (CFD micro-service)::

    from meta_research.interfaces import Objective
    from meta_research.evaluators import ApiSolverEvaluator

    objectives = [Objective("thermal_resistance", "min"),
                  Objective("pressure_drop", "min")]

    def parse_rest(resp: dict):
        scores = {"thermal_resistance": resp["R_th"], "pressure_drop": resp["dP"]}
        metadata = {"solver_iters": resp.get("iters")}
        artifacts = {}  # could download a heatmap URL into out_dir
        return scores, metadata, artifacts

    ev = ApiSolverEvaluator.rest(
        objectives, endpoint="http://solver.local/score", parse_fn=parse_rest,
        timeout_s=120)

CLI example (OpenFOAM-style binary)::

    def parse_cli(out_dir):
        import json
        data = json.loads((out_dir / "result.json").read_text())
        return ({"thermal_resistance": data["R_th"], "pressure_drop": data["dP"]},
                {"mesh_cells": data.get("cells")},
                {"heatmap": "T_field.png"})

    ev = ApiSolverEvaluator.cli(
        objectives,
        command_template="solve --in {params_json} --out {out_dir}",
        parse_fn=parse_cli, timeout_s=600)
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess  # noqa: S404 - command template is operator-controlled, not user input
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from meta_research.interfaces import DesignSpec, EvalResult, Objective

logger = logging.getLogger(__name__)

# parse_fn(payload) -> (scores, metadata, artifacts). payload is the decoded JSON
# response (REST) or the out_dir Path (CLI).
ParseFn = Callable[[Any], "tuple[dict[str, float], dict[str, Any], dict[str, str]]"]

_MODE_REST = "rest"
_MODE_CLI = "cli"
_DEFAULT_TIMEOUT_S = 300


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if not isinstance(value, (int, float)):
        return False
    return value == value and abs(value) != float("inf")


class ApiSolverEvaluator:
    """Score a design via an external REST endpoint or CLI solver.

    Prefer the :meth:`rest` / :meth:`cli` classmethods over the raw constructor.

    Args:
        objectives: Ordered objectives this evaluator scores.
        mode: ``"rest"`` or ``"cli"``.
        parse_fn: Maps the solver payload to ``(scores, metadata, artifacts)``.
        endpoint: REST URL (REST mode only).
        headers: Extra HTTP headers (REST mode only).
        command_template: ``str.format`` command template (CLI mode only).
        timeout_s: Per-call timeout in seconds.
        extra_payload: Static fields merged into the REST JSON body.
    """

    def __init__(
        self,
        objectives: list[Objective],
        *,
        mode: str,
        parse_fn: ParseFn,
        endpoint: str | None = None,
        headers: dict[str, str] | None = None,
        command_template: str | None = None,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        if not objectives:
            raise ValueError("ApiSolverEvaluator requires at least one objective")
        if mode not in (_MODE_REST, _MODE_CLI):
            raise ValueError(f"mode must be {_MODE_REST!r} or {_MODE_CLI!r}, got {mode!r}")
        if not callable(parse_fn):
            raise TypeError("parse_fn must be callable")
        if mode == _MODE_REST and not endpoint:
            raise ValueError("REST mode requires an endpoint URL")
        if mode == _MODE_CLI and not command_template:
            raise ValueError("CLI mode requires a command_template")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

        self.objectives: list[Objective] = list(objectives)
        self._mode = mode
        self._parse_fn = parse_fn
        self._endpoint = endpoint
        self._headers = dict(headers or {})
        self._command_template = command_template
        self._timeout_s = int(timeout_s)
        self._extra_payload = dict(extra_payload or {})

    # ----------------------------------------------------------- constructors

    @classmethod
    def rest(
        cls,
        objectives: list[Objective],
        *,
        endpoint: str,
        parse_fn: ParseFn,
        headers: dict[str, str] | None = None,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        extra_payload: dict[str, Any] | None = None,
    ) -> "ApiSolverEvaluator":
        """Build a REST-mode evaluator that POSTs params as JSON to ``endpoint``."""
        return cls(
            objectives,
            mode=_MODE_REST,
            parse_fn=parse_fn,
            endpoint=endpoint,
            headers=headers,
            timeout_s=timeout_s,
            extra_payload=extra_payload,
        )

    @classmethod
    def cli(
        cls,
        objectives: list[Objective],
        *,
        command_template: str,
        parse_fn: ParseFn,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> "ApiSolverEvaluator":
        """Build a CLI-mode evaluator that runs ``command_template`` via subprocess.

        The template is rendered with ``str.format`` and may reference:
          * ``{out_dir}``     -- the per-candidate output directory,
          * ``{params_json}`` -- path to a JSON file holding the params,
          * any ``{param_key}`` from ``design.params``.
        """
        return cls(
            objectives,
            mode=_MODE_CLI,
            parse_fn=parse_fn,
            command_template=command_template,
            timeout_s=timeout_s,
        )

    # --------------------------------------------------------------- scoring

    def evaluate(self, design: DesignSpec, out_dir: Path) -> EvalResult:
        """Run the external solver and assemble an :class:`EvalResult`. Never raises."""
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - filesystem edge case
            logger.exception("ApiSolverEvaluator: could not create out_dir %s", out_dir)
            return EvalResult.crashed(f"could not create out_dir {out_dir!r}: {exc}")

        params = dict(getattr(design, "params", {}) or {})

        try:
            if self._mode == _MODE_REST:
                payload = self._run_rest(params, out_dir)
            else:
                payload = self._run_cli(params, out_dir)
        except Exception as exc:  # noqa: BLE001 - solver failures are recorded, not raised
            logger.exception("ApiSolverEvaluator: solver invocation failed")
            tb = traceback.format_exc(limit=8)
            return EvalResult.crashed(
                f"solver invocation failed ({type(exc).__name__}): {exc}\n{tb}"
            )

        try:
            parsed = self._parse_fn(payload)
        except Exception as exc:  # noqa: BLE001 - parse_fn is user code
            logger.exception("ApiSolverEvaluator: parse_fn raised")
            tb = traceback.format_exc(limit=8)
            return EvalResult.crashed(
                f"parse_fn raised {type(exc).__name__}: {exc}\n{tb}"
            )

        unpacked = self._unpack(parsed)
        if isinstance(unpacked, EvalResult):
            return unpacked
        scores, metadata, artifacts = unpacked

        missing = [obj.name for obj in self.objectives if obj.name not in scores]
        if missing:
            return EvalResult(
                scores={k: float(v) for k, v in scores.items() if _is_finite_number(v)},
                feasible=False,
                metadata=metadata,
                artifacts=artifacts,
                error=f"parse_fn omitted objective(s): {', '.join(missing)}",
            )

        bad = [obj.name for obj in self.objectives if not _is_finite_number(scores[obj.name])]
        if bad:
            return EvalResult(
                scores={obj.name: self._coerce(scores[obj.name]) for obj in self.objectives},
                feasible=False,
                metadata=metadata,
                artifacts=artifacts,
                error=f"parse_fn returned non-finite score(s): {', '.join(bad)}",
            )

        feasible = bool(metadata.get("feasible", True))
        return EvalResult(
            scores={obj.name: float(scores[obj.name]) for obj in self.objectives},
            feasible=feasible,
            metadata=metadata,
            artifacts=artifacts,
            error=None,
        )

    # ------------------------------------------------------------- REST mode

    def _run_rest(self, params: dict[str, Any], out_dir: Path) -> Any:
        """POST params as JSON and return the decoded JSON response. May raise."""
        body = json.dumps({**self._extra_payload, "params": params}).encode("utf-8")
        headers = {"Content-Type": "application/json", **self._headers}

        raw_text = self._post(self._endpoint or "", body, headers)
        self._write_log(out_dir, raw_text)

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"solver response was not valid JSON: {exc}") from exc

    def _post(self, url: str, body: bytes, headers: dict[str, str]) -> str:
        """POST and return the response text. Uses requests if available, else urllib."""
        try:
            import requests  # type: ignore
        except ImportError:
            requests = None  # type: ignore

        if requests is not None:
            resp = requests.post(url, data=body, headers=headers, timeout=self._timeout_s)
            resp.raise_for_status()
            return resp.text

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as resp:  # noqa: S310 - operator-configured URL
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise ValueError(f"solver returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"could not reach solver endpoint {url!r}: {exc.reason}") from exc

    # -------------------------------------------------------------- CLI mode

    def _run_cli(self, params: dict[str, Any], out_dir: Path) -> Path:
        """Write params, render + run the command, capture stdout; return out_dir."""
        params_json = out_dir / "input_params.json"
        params_json.write_text(json.dumps({"params": params}, indent=2), encoding="utf-8")

        try:
            rendered = (self._command_template or "").format(
                out_dir=str(out_dir),
                params_json=str(params_json),
                **params,
            )
        except (KeyError, IndexError) as exc:
            raise ValueError(
                f"command_template references an unknown field {exc}; "
                "available: {out_dir}, {params_json}, and design param keys"
            ) from exc

        argv = shlex.split(rendered)
        if not argv:
            raise ValueError("command_template rendered to an empty command")

        try:
            completed = subprocess.run(  # noqa: S603 - argv is operator-controlled
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(out_dir),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._write_log(out_dir, (exc.stdout or "") + "\n[TIMEOUT]")
            raise TimeoutError(
                f"solver exceeded timeout of {self._timeout_s}s: {rendered}"
            ) from exc
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"solver binary not found: {argv[0]!r}") from exc

        log = (completed.stdout or "") + (
            ("\n[STDERR]\n" + completed.stderr) if completed.stderr else ""
        )
        self._write_log(out_dir, log)

        if completed.returncode != 0:
            raise RuntimeError(
                f"solver exited with code {completed.returncode}: {rendered}\n"
                f"{(completed.stderr or '')[:2000]}"
            )
        return out_dir

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _write_log(out_dir: Path, text: str) -> None:
        """Persist solver stdout/stderr to ``solver.log``; non-fatal on failure."""
        try:
            (out_dir / "solver.log").write_text(text or "", encoding="utf-8")
        except Exception:  # pragma: no cover - non-fatal diagnostic write
            logger.warning("ApiSolverEvaluator: could not write solver.log", exc_info=True)

    @staticmethod
    def _coerce(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    def _unpack(
        self, parsed: Any
    ) -> "tuple[dict[str, float], dict[str, Any], dict[str, str]] | EvalResult":
        """Validate the parse_fn return shape. Never raises."""
        if not isinstance(parsed, (tuple, list)) or len(parsed) != 3:
            return EvalResult.crashed(
                "parse_fn must return a 3-tuple (scores, metadata, artifacts); "
                f"got {type(parsed).__name__}"
            )
        scores_raw, metadata_raw, artifacts_raw = parsed
        if not isinstance(scores_raw, dict):
            return EvalResult.crashed("parse_fn scores must be a dict")
        metadata: dict[str, Any] = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        artifacts: dict[str, str] = (
            {str(k): str(v) for k, v in artifacts_raw.items()}
            if isinstance(artifacts_raw, dict)
            else {}
        )
        # Always surface the solver log as an artifact for the loop to read back.
        artifacts.setdefault("solver_log", "solver.log")
        scores = {str(k): v for k, v in scores_raw.items()}
        return scores, metadata, artifacts


__all__ = ["ApiSolverEvaluator", "ParseFn"]
