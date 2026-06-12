"""Best-effort capture of the proposer's thinking trace into the bundle.

Meta-Harness (DESIGN §1, "store everything") asks for the proposer's *thinking
blocks*, not just the curated ``hypothesis.md`` hand-off. How that thinking
can be recovered is **runtime-specific**: each agent runtime persists (or
redacts) its reasoning differently, so each gets its own
:class:`~meta_research.thinking.base.ThinkingSource` adapter — Claude Code
today; a Codex adapter would be one more module registered in
:data:`SOURCES`, with no change here (the same pluggable pattern as
``meta_research.evaluators``).

:func:`capture_thinking` tries each registered source in order and writes the
first non-empty harvest to ``<bundle>/trace/proposer_thinking.md``, appending
a pointer line to ``hypothesis.md``. Everything is best-effort by contract:
no source yields text (missing transcript, redacted thinking, foreign
session) -> ``None``, never an exception. The runner calls this between
``record()`` and the git commit, so the captured trace lands in the same
commit as its bundle.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import ThinkingSource
from .claude_code import (
    TRANSCRIPT_ENV,
    ClaudeCodeSource,
    find_transcript,
    harvest_window,
)

logger = logging.getLogger(__name__)

#: File name inside the bundle's trace/ directory.
THINKING_FILENAME = "proposer_thinking.md"

#: Cap the captured text so a pathological session cannot bloat the ledger.
_MAX_CHARS = 500_000

#: Registered runtime adapters, tried in order; first non-empty harvest wins.
SOURCES: tuple[ThinkingSource, ...] = (ClaudeCodeSource(),)


def capture_thinking(
    run_dir: Path | str, bundle: Path | str, *, name: str = ""
) -> Path | None:
    """Write the current iteration's thinking into ``<bundle>/trace/``.

    Returns the written path, or ``None`` when no registered source yields
    text (or on any error -- logged, never raised). A pointer line is appended
    to the bundle's ``hypothesis.md`` so readers of the curated hand-off can
    find the raw trace.
    """
    try:
        source_name, text = _first_harvest(run_dir, name)
        if text is None:
            return None
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "\n\n[truncated: thinking exceeded size cap]"

        trace_dir = Path(bundle) / "trace"
        trace_dir.mkdir(parents=True, exist_ok=True)
        dest = trace_dir / THINKING_FILENAME
        header = (
            f"# Proposer thinking — {name or Path(bundle).name}\n\n"
            f"Raw thinking harvested at eval time from the `{source_name}` runtime\n"
            "(best-effort; includes options considered and rejected). The curated\n"
            "hand-off remains `hypothesis.md`.\n\n---\n\n"
        )
        dest.write_text(header + text + "\n", encoding="utf-8")
        _append_pointer(Path(bundle) / "hypothesis.md")
        return dest
    except Exception as exc:  # noqa: BLE001 -- best-effort by contract
        logger.warning("thinking capture failed: %s", exc)
        return None


def _first_harvest(run_dir: Path | str, name: str) -> tuple[str, str | None]:
    """Try each registered source; return (source name, text) or ("", None)."""
    for source in SOURCES:
        try:
            text = source.harvest(run_dir, name)
        except Exception as exc:  # noqa: BLE001 -- a broken adapter must not stop the rest
            logger.warning("thinking source %s failed: %s", source.name, exc)
            continue
        if text and text.strip():
            return source.name, text
    return "", None


def _append_pointer(hypothesis_path: Path) -> None:
    """Append a one-line pointer to the raw trace; idempotent, best-effort."""
    try:
        if not hypothesis_path.is_file():
            return
        text = hypothesis_path.read_text(encoding="utf-8")
        if THINKING_FILENAME in text:
            return
        pointer = f"\nFull thinking trace: `trace/{THINKING_FILENAME}`\n"
        hypothesis_path.write_text(text.rstrip("\n") + "\n" + pointer, encoding="utf-8")
    except OSError as exc:
        logger.warning("could not append thinking pointer: %s", exc)


__all__ = [
    "capture_thinking",
    "find_transcript",
    "harvest_window",
    "ClaudeCodeSource",
    "ThinkingSource",
    "SOURCES",
    "THINKING_FILENAME",
    "TRANSCRIPT_ENV",
]
