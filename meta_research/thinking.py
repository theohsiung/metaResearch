"""Best-effort capture of the proposer's thinking trace into the bundle.

Meta-Harness (DESIGN §1, "store everything") asks for the proposer's *thinking
blocks*, not just the curated ``hypothesis.md`` hand-off. When the research
loop runs inside Claude Code, the session transcript (a JSONL file under
``~/.claude/projects/<slug>/``) already contains every thinking block -- but
transcripts are NOT durable: context compaction discards old blocks. The only
reliable copy is the one harvested into the ledger at eval time, which is what
this module does.

How the window is chosen: the blocks that produced the *current* candidate are
exactly those since the previous ``meta-research eval``/``seed`` invocation
(inspection commands like ``frontier``/``progress``/``kg`` are not boundaries).
The current eval's own tool_use is already in the transcript when the runner
executes, so the window is "between the last two eval markers" (or everything
before the only marker / the whole file when none exist).

Everything here is best-effort by contract: a missing transcript, a foreign
agent runtime, or a corrupt file yields ``None`` -- never an exception. The
runner calls :func:`capture_thinking` between ``record()`` and the git commit,
so the captured trace lands in the same commit as its bundle.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Environment override for the transcript location (also used by tests).
TRANSCRIPT_ENV = "META_RESEARCH_TRANSCRIPT"

#: File name inside the bundle's trace/ directory.
THINKING_FILENAME = "proposer_thinking.md"

#: A command is an iteration boundary iff it invokes the meta-research
#: evaluation plumbing (CLI script name or python -m module form). Same-line
#: only: real invocations never wrap, and cross-line matching would turn
#: heredocs that merely mention both words into false boundaries.
_BOUNDARY_RE = re.compile(r"meta[-_]research(\.cli)?\b[^\n]*\b(eval|seed)\b")

#: Cap the captured text so a pathological session cannot bloat the ledger.
_MAX_CHARS = 500_000


# --------------------------------------------------------------------------- #
# transcript discovery
# --------------------------------------------------------------------------- #
def find_transcript(run_dir: Path | str) -> Path | None:
    """Locate the live session transcript, newest-first. None when absent.

    Order: the :data:`TRANSCRIPT_ENV` override; the Claude Code project dir
    derived from ``run_dir``; any project's most recently modified transcript
    (the active session file is appended continuously, so the globally newest
    ``.jsonl`` is almost always the current session -- the session's project
    slug is its *original* cwd, which need not be ``run_dir``).
    """
    override = os.environ.get(TRANSCRIPT_ENV)
    if override:
        path = Path(override)
        return path if path.is_file() else None

    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None
    slug = re.sub(r"[^A-Za-z0-9-]", "-", str(Path(run_dir).resolve()))
    candidates = sorted(
        (projects / slug).glob("*.jsonl") if (projects / slug).is_dir() else [],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    everywhere = sorted(
        projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return everywhere[0] if everywhere else None


# --------------------------------------------------------------------------- #
# harvesting
# --------------------------------------------------------------------------- #
def harvest_window(transcript: Path) -> str:
    """Return the thinking text for the current iteration (may be empty).

    Parses the JSONL transcript into an ordered stream of thinking blocks and
    boundary markers, then keeps the blocks between the last two boundaries
    (see module docstring for why that is the current iteration's window).
    Unparseable lines are skipped; never raises for content problems.
    """
    blocks, boundaries = _scan(transcript)
    return _window_text(blocks, boundaries)


def _scan(transcript: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Parse the transcript into ((index, thinking), (index, boundary command))."""
    blocks: list[tuple[int, str]] = []
    boundaries: list[tuple[int, str]] = []
    index = 0
    with open(transcript, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            entry = _parse_json(line)
            if entry is None:
                continue
            for item in _content_blocks(entry):
                kind = item.get("type")
                if kind == "thinking":
                    text = str(item.get("thinking") or "")
                    if text.strip():
                        blocks.append((index, text))
                elif kind == "tool_use":
                    command = _boundary_command(item)
                    if command is not None:
                        boundaries.append((index, command))
                index += 1
    return blocks, boundaries


def _window_text(
    blocks: list[tuple[int, str]], boundaries: list[tuple[int, str]]
) -> str:
    lo = boundaries[-2][0] if len(boundaries) >= 2 else -1
    hi = boundaries[-1][0] if boundaries else float("inf")
    return "\n\n---\n\n".join(text for i, text in blocks if lo < i < hi)


def capture_thinking(
    run_dir: Path | str, bundle: Path | str, *, name: str = ""
) -> Path | None:
    """Write the current iteration's thinking into ``<bundle>/trace/``.

    Returns the written path, or ``None`` when there is nothing to capture
    (no transcript found, empty window, or any error -- logged, never raised).
    A pointer line is appended to the bundle's ``hypothesis.md`` so readers of
    the curated hand-off can find the raw trace.
    """
    try:
        trusted = bool(os.environ.get(TRANSCRIPT_ENV))
        transcript = find_transcript(run_dir)
        if transcript is None:
            logger.debug("no session transcript found; skipping thinking capture")
            return None
        blocks, boundaries = _scan(transcript)
        if not trusted and not _is_this_loops_session(boundaries, name):
            logger.debug(
                "transcript %s does not reference this eval; skipping capture",
                transcript,
            )
            return None
        text = _window_text(blocks, boundaries)
        if not text.strip():
            return None
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "\n\n[truncated: thinking exceeded size cap]"

        trace_dir = Path(bundle) / "trace"
        trace_dir.mkdir(parents=True, exist_ok=True)
        dest = trace_dir / THINKING_FILENAME
        header = (
            f"# Proposer thinking — {name or Path(bundle).name}\n\n"
            "Raw thinking blocks harvested from the session transcript at eval\n"
            "time (best-effort; includes options considered and rejected).\n"
            "The curated hand-off remains `hypothesis.md`.\n\n---\n\n"
        )
        dest.write_text(header + text + "\n", encoding="utf-8")
        _append_pointer(Path(bundle) / "hypothesis.md")
        return dest
    except Exception as exc:  # noqa: BLE001 -- best-effort by contract
        logger.warning("thinking capture failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse_json(line: str) -> dict[str, Any] | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _boundary_command(tool_use: dict[str, Any]) -> str | None:
    payload = tool_use.get("input")
    command = payload.get("command") if isinstance(payload, dict) else None
    if isinstance(command, str) and _BOUNDARY_RE.search(command):
        return command
    return None


def _is_this_loops_session(boundaries: list[tuple[int, str]], name: str) -> bool:
    """Auto-discovered transcripts must reference THIS eval to be harvested.

    When the loop runs inside the live session, the current eval's tool_use is
    the transcript's last boundary and its command names the design. A foreign
    session's transcript (wrong project, another agent) fails this check, so
    its thinking can never pollute the ledger. ``seed`` commands carry no
    design name and are accepted as-is.
    """
    if not boundaries:
        return False
    last = boundaries[-1][1]
    if re.search(r"\bseed\b", last):
        return True
    # Whole-word match so e.g. name "d1" never matches an eval of "d10".
    return bool(name) and re.search(rf"\b{re.escape(name)}\b", last) is not None


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
    "THINKING_FILENAME",
    "TRANSCRIPT_ENV",
]
