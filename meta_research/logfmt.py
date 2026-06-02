"""ANSI color helpers and the ``results.tsv`` ledger reader/writer.

The color helpers honor ``sys.stdout.isatty()``: when output is not a TTY (a
pipe, a file, a CI log) they return the string unchanged so the ledger and
captured logs stay clean. They are intended for user-facing progress printed by
the CLI/loop; library internals should prefer :mod:`logging`.

:class:`ResultsLog` reads and writes the flat ``results.tsv`` ledger described in
DESIGN.md 7.5. The schema is::

    iter  name  status  feasible  <one column per objective>  hypothesis

with ``status`` in ``{"frontier", "dominated", "infeasible", "crash"}``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from meta_research.interfaces import EvalResult, Objective

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

# Status values allowed in the ledger (DESIGN.md 6.1 / 7.5).
STATUSES = ("frontier", "dominated", "infeasible", "crash")

_RESET = "\033[0m"
_CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


def _supports_color() -> bool:
    """Return True only when stdout is an interactive terminal.

    Any error probing the stream (e.g. a stub stdout without ``isatty``) is
    treated as "no color" so the helpers never raise.
    """
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _wrap(text: str, code_name: str) -> str:
    """Wrap ``text`` in the named ANSI code when color is supported."""
    s = str(text)
    if not _supports_color():
        return s
    code = _CODES.get(code_name)
    if code is None:
        return s
    return f"{code}{s}{_RESET}"


def bold(s: str) -> str:
    """Render ``s`` bold (no-op when stdout is not a TTY)."""
    return _wrap(s, "bold")


def dim(s: str) -> str:
    """Render ``s`` dim (no-op when stdout is not a TTY)."""
    return _wrap(s, "dim")


def green(s: str) -> str:
    """Render ``s`` green (no-op when stdout is not a TTY)."""
    return _wrap(s, "green")


def red(s: str) -> str:
    """Render ``s`` red (no-op when stdout is not a TTY)."""
    return _wrap(s, "red")


def yellow(s: str) -> str:
    """Render ``s`` yellow (no-op when stdout is not a TTY)."""
    return _wrap(s, "yellow")


def cyan(s: str) -> str:
    """Render ``s`` cyan (no-op when stdout is not a TTY)."""
    return _wrap(s, "cyan")


def ts() -> str:
    """Return a short wall-clock timestamp ``[HH:MM:SS]`` for progress lines.

    This is user-facing logging only -- never used inside pure functions or the
    deterministic evaluation/frontier code paths (DESIGN.md 10).
    """
    import time

    return time.strftime("[%H:%M:%S]")


def elapsed(seconds: float) -> str:
    """Format a duration in seconds as a compact human string.

    Examples: ``0.42s``, ``12.3s``, ``2m05s``, ``1h03m``.
    """
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if total != total or total < 0:  # NaN or negative
        return "?"
    if total < 60:
        return f"{total:.2f}s" if total < 10 else f"{total:.1f}s"
    minutes, secs = divmod(int(round(total)), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# ---------------------------------------------------------------------------
# results.tsv ledger
# ---------------------------------------------------------------------------

_SEP = "\t"
_FIXED_LEADING = ("iter", "name", "status", "feasible")
_FIXED_TRAILING = ("hypothesis",)


def _clean_cell(value: Any) -> str:
    """Make a value safe for a single tab-separated cell.

    Tabs and newlines would corrupt the row geometry, so they are collapsed to
    single spaces. ``None`` becomes the empty string.
    """
    if value is None:
        return ""
    text = str(value)
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _format_score(value: Any) -> str:
    """Render a numeric score for the ledger, tolerating missing/odd values."""
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return _clean_cell(value)
    if num != num:  # NaN
        return "nan"
    if num == float("inf"):
        return "inf"
    if num == float("-inf"):
        return "-inf"
    return repr(num)


class ResultsLog:
    """Reader/writer for the ``results.tsv`` flat ledger (DESIGN.md 7.5).

    Columns are ``iter name status feasible <objective columns...> hypothesis``,
    where the objective columns come from ``objectives`` in declared order.

    The ledger is append-only in spirit (it mirrors the git append-only store),
    but :class:`ResultsLog` itself only provides ``write_header``/``append``/
    ``rows`` -- it never rewrites or truncates existing rows.
    """

    def __init__(self, path: Path | str, objectives: list[Objective]) -> None:
        self.path = Path(path)
        self.objectives = list(objectives)

    # -- schema -----------------------------------------------------------
    @property
    def objective_names(self) -> list[str]:
        return [obj.name for obj in self.objectives]

    @property
    def columns(self) -> list[str]:
        """Full ordered column list for the header row."""
        return [
            *_FIXED_LEADING,
            *self.objective_names,
            *_FIXED_TRAILING,
        ]

    # -- writing ----------------------------------------------------------
    def write_header(self) -> None:
        """Write (or overwrite) the file with just the header row.

        Call this once when initializing a fresh ledger. It creates parent
        directories as needed.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = _SEP.join(self.columns) + "\n"
        self.path.write_text(header, encoding="utf-8")

    def ensure_header(self) -> None:
        """Write the header only if the file does not already exist/has content."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            self.write_header()

    def append(
        self,
        iteration: int,
        name: str,
        status: str,
        result: EvalResult,
        hypothesis: str,
    ) -> None:
        """Append one ledger row for an evaluated (or skipped) candidate.

        Args:
            iteration: Iteration index this candidate belongs to.
            name: Candidate module name.
            status: One of :data:`STATUSES`.
            result: The :class:`EvalResult`; its ``scores`` fill the objective
                columns and ``feasible`` fills the feasible column.
            hypothesis: A one-line summary of the reasoning (already-flattened).

        The header is created automatically if the file does not yet exist so a
        bare ``append`` cannot lose a row.
        """
        if status not in STATUSES:
            raise ValueError(
                f"status {status!r} not in {STATUSES}"
            )
        self.ensure_header()

        scores = dict(result.scores or {})
        score_cells = [_format_score(scores.get(name_)) for name_ in self.objective_names]
        row = [
            _clean_cell(iteration),
            _clean_cell(name),
            _clean_cell(status),
            "true" if result.feasible else "false",
            *score_cells,
            _clean_cell(hypothesis),
        ]
        line = _SEP.join(row) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    # -- reading ----------------------------------------------------------
    def rows(self) -> list[dict]:
        """Parse the ledger into a list of row dicts.

        Each row dict has keys ``iter`` (int when parseable), ``name``,
        ``status``, ``feasible`` (bool), ``scores`` (dict[str, float]) and
        ``hypothesis``. Missing/corrupt files yield an empty list; malformed
        individual rows are skipped rather than raising.
        """
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []

        lines = [ln for ln in text.splitlines() if ln.strip() != ""]
        if not lines:
            return []

        header = lines[0].split(_SEP)
        # Determine which header columns are objective columns.
        out: list[dict] = []
        n_lead = len(_FIXED_LEADING)
        n_trail = len(_FIXED_TRAILING)
        for raw in lines[1:]:
            cells = raw.split(_SEP)
            if len(cells) < n_lead + n_trail:
                continue
            iter_raw = cells[0]
            try:
                iter_val: Any = int(iter_raw)
            except (TypeError, ValueError):
                iter_val = iter_raw
            name = cells[1]
            status = cells[2]
            feasible = cells[3].strip().lower() == "true"
            hypothesis = cells[-1]
            score_cells = cells[n_lead : len(cells) - n_trail]
            # Map score cells back to the objective names from the header.
            obj_header = header[n_lead : len(header) - n_trail] if len(header) >= n_lead + n_trail else []
            scores: dict[str, float] = {}
            for col_name, cell in zip(obj_header, score_cells):
                if cell == "":
                    continue
                try:
                    scores[col_name] = float(cell)
                except (TypeError, ValueError):
                    continue
            out.append(
                {
                    "iter": iter_val,
                    "name": name,
                    "status": status,
                    "feasible": feasible,
                    "scores": scores,
                    "hypothesis": hypothesis,
                }
            )
        return out


__all__ = [
    "STATUSES",
    "bold",
    "dim",
    "green",
    "red",
    "yellow",
    "cyan",
    "ts",
    "elapsed",
    "ResultsLog",
]
