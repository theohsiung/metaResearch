"""Protocol for runtime-specific thinking sources.

Each agent runtime (Claude Code, Codex, ...) persists -- or redacts -- the
proposer's internal reasoning differently, so recovering it is runtime-specific
by nature. A :class:`ThinkingSource` encapsulates one runtime's recovery
strategy behind a single method; the orchestrator in
:mod:`meta_research.thinking` tries each registered source in order and keeps
the first non-empty harvest. Adding support for a new runtime is one new
module implementing this protocol plus a registry entry -- the same pluggable
pattern as ``meta_research.evaluators``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ThinkingSource(Protocol):
    """One agent runtime's way of recovering the proposer's thinking."""

    #: Short identifier recorded in the trace header (e.g. ``"claude-code"``).
    name: str

    def harvest(self, run_dir: Path | str, candidate: str) -> str | None:
        """Return the current iteration's thinking text for ``candidate``.

        Implementations must be best-effort and side-effect free: return
        ``None`` when the runtime's records are absent, redacted, or do not
        belong to this loop's session. Raising is tolerated (the orchestrator
        catches and moves on) but discouraged.
        """
        ...


__all__ = ["ThinkingSource"]
