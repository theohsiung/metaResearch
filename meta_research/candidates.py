"""Load, validate and build candidate design modules (DESIGN.md 5).

A *candidate* is a single Python module under ``designs/`` that exposes a
module-level ``build() -> DesignSpec`` and, optionally, a module-level ``NAME``
string (defaulting to the module filename stem). This module turns such a module
into a usable design:

- :func:`load_candidate` resolves a name or path to ``(name, build_callable)``.
- :func:`validate_candidate` checks interface compliance only (import + build +
  type check); **no exception escapes** it.
- :func:`build_design` validates then returns the built :class:`DesignSpec`.

Validation is *interface compliance only*. The actual scoring is the Evaluator's
job (DESIGN.md 5). Importing arbitrary candidate modules executes their
top-level code, so the framework only ever loads modules from a trusted
experiment directory the human controls.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Callable

from meta_research.interfaces import DesignSpec

# Type alias for the zero-arg build callable a candidate exposes.
BuildFn = Callable[[], DesignSpec]


class CandidateError(Exception):
    """Raised by :func:`load_candidate`/:func:`build_design` for load failures.

    :func:`validate_candidate` catches everything (including this) and reports a
    ``(False, message)`` tuple instead of raising.
    """


def _module_stem(name_or_path: str) -> str:
    """Best-effort candidate name from a module name or a file path."""
    text = str(name_or_path)
    if text.endswith(".py"):
        return Path(text).stem
    # Allow dotted module paths like ``designs.straight_fins``.
    return text.rsplit(".", 1)[-1]


def _load_module_from_path(path: Path):
    """Import a module from an explicit ``.py`` file path.

    Uses a unique synthetic module name so repeated loads of edited candidates do
    not collide in ``sys.modules``.
    """
    stem = path.stem
    mod_name = f"_meta_research_candidate_{stem}"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise CandidateError(f"cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses / relative lookups behave.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 -- re-wrapped for the caller
        sys.modules.pop(mod_name, None)
        raise CandidateError(f"failed to import {path}: {exc}") from exc
    return module


def _resolve_module(name_or_path: str, designs_dir: str | Path | None):
    """Import the candidate module given a name/path and an optional designs dir.

    Resolution order:
      1. If ``name_or_path`` points at an existing ``.py`` file, import that file.
      2. If ``designs_dir`` is given, look for ``<designs_dir>/<stem>.py``.
      3. Otherwise import it as a dotted module name via :func:`importlib.import_module`.
    """
    text = str(name_or_path)

    # 1. explicit file path
    as_path = Path(text)
    if text.endswith(".py") and as_path.is_file():
        return _load_module_from_path(as_path)

    stem = _module_stem(text)

    # 2. designs_dir/<stem>.py
    if designs_dir is not None:
        candidate_file = Path(designs_dir) / f"{stem}.py"
        if candidate_file.is_file():
            return _load_module_from_path(candidate_file)

    # 3. dotted module import (caller must have it on sys.path)
    try:
        return importlib.import_module(text)
    except Exception as exc:  # noqa: BLE001
        raise CandidateError(
            f"could not resolve candidate {text!r} "
            f"(designs_dir={designs_dir!r}): {exc}"
        ) from exc


def load_candidate(
    path_or_module: str,
    designs_dir: str | Path | None = None,
) -> tuple[str, BuildFn]:
    """Resolve a candidate to ``(name, build_callable)`` (DESIGN.md 5).

    Args:
        path_or_module: A module name (``"straight_fins"``), a dotted path, or a
            ``.py`` file path.
        designs_dir: Optional directory to look in for ``<name>.py``.

    Returns:
        ``(name, build)`` where ``name`` is the module-level ``NAME`` if present
        else the filename stem, and ``build`` is the module's ``build`` callable.

    Raises:
        CandidateError: if the module cannot be imported or has no callable
            ``build``. (Used internally by :func:`validate_candidate`, which
            converts this into a status tuple.)
    """
    module = _resolve_module(path_or_module, designs_dir)

    build = getattr(module, "build", None)
    if build is None or not callable(build):
        raise CandidateError(
            f"candidate {path_or_module!r} has no module-level callable 'build()'"
        )

    raw_name = getattr(module, "NAME", None)
    name = str(raw_name) if isinstance(raw_name, str) and raw_name.strip() else _module_stem(path_or_module)
    return name, build


def validate_candidate(name: str, designs_dir: str | Path) -> tuple[bool, str]:
    """Validate interface compliance of a candidate. **Never raises.**

    Imports ``<designs_dir>/<name>.py``, calls ``build()`` and checks the result
    is a :class:`DesignSpec` with a non-empty ``params`` dict (DESIGN.md 5).

    Returns:
        ``(ok, message)``. ``ok`` is True on success with a short OK message;
        on any failure ``ok`` is False and ``message`` carries a human-readable
        explanation (including the traceback summary for unexpected errors).
    """
    try:
        resolved_name, build = load_candidate(str(name), designs_dir)
    except CandidateError as exc:
        return False, f"load failed: {exc}"
    except Exception as exc:  # noqa: BLE001 -- defensive: never raise
        return False, f"unexpected load error: {exc}\n{traceback.format_exc(limit=3)}"

    try:
        design = build()
    except Exception as exc:  # noqa: BLE001 -- candidate code is untrusted
        return False, f"build() raised: {exc}\n{traceback.format_exc(limit=3)}"

    if not isinstance(design, DesignSpec):
        return (
            False,
            f"build() returned {type(design).__name__}, expected DesignSpec",
        )

    params = getattr(design, "params", None)
    if not isinstance(params, dict):
        return False, "DesignSpec.params must be a dict"
    if len(params) == 0:
        return False, "DesignSpec.params is empty; a design must define parameters"

    return True, f"ok: {resolved_name} ({len(params)} params)"


def build_design(name: str, designs_dir: str | Path) -> DesignSpec:
    """Validate then build the named candidate, returning its :class:`DesignSpec`.

    Args:
        name: Candidate module name (looked up under ``designs_dir``).
        designs_dir: Directory containing the candidate module.

    Returns:
        The :class:`DesignSpec` produced by the candidate's ``build()``.

    Raises:
        CandidateError: if validation fails. (Validation runs first so callers
            get a single, clear failure mode; the message mirrors
            :func:`validate_candidate`.)
    """
    ok, message = validate_candidate(name, designs_dir)
    if not ok:
        raise CandidateError(f"invalid candidate {name!r}: {message}")

    # Validation passed, so loading + build will succeed here too.
    _, build = load_candidate(str(name), designs_dir)
    design = build()
    if not isinstance(design, DesignSpec):  # pragma: no cover -- guarded above
        raise CandidateError(
            f"candidate {name!r} build() returned non-DesignSpec after validation"
        )
    return design


__all__ = [
    "BuildFn",
    "CandidateError",
    "load_candidate",
    "validate_candidate",
    "build_design",
]
