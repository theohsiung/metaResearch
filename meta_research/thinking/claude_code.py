"""Claude Code thinking source: harvest from the session transcript JSONL.

Claude Code persists each session as a JSONL file under
``~/.claude/projects/<slug>/``; assistant messages carry ``message.content[]``
items of ``{"type": "thinking", "thinking": ...}``. Two hard caveats shape
this adapter:

* Transcripts are not durable -- context compaction discards old blocks, so
  only an at-eval-time harvest can be reliable.
* Some configurations redact thinking text entirely (blocks persist with an
  empty string plus a signature). Harvesting then yields nothing, by design:
  ``hypothesis.md`` remains the reasoning trace of record.

Window selection: the blocks that produced the *current* candidate are those
since the previous ``meta-research eval``/``seed`` invocation (inspection
commands like ``frontier``/``progress``/``kg`` are not boundaries). The
current eval's own tool_use is already in the transcript when the runner
executes, so the window is "between the last two eval markers" (or everything
before the only marker / the whole file when none exist).
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

#: A command is an iteration boundary iff it invokes the meta-research
#: evaluation plumbing (CLI script name or python -m module form). Same-line
#: only: real invocations never wrap, and cross-line matching would turn
#: heredocs that merely mention both words into false boundaries.
_BOUNDARY_RE = re.compile(r"meta[-_]research(\.cli)?\b[^\n]*\b(eval|seed)\b")


class ClaudeCodeSource:
    """Recover the proposer's thinking from a Claude Code session transcript."""

    name = "claude-code"

    def harvest(self, run_dir: Path | str, candidate: str) -> str | None:
        """Return this iteration's thinking text, or ``None`` when unavailable.

        Auto-discovered transcripts pass an affinity guard (the last eval/seed
        marker must reference ``candidate``) so a foreign session can never
        pollute the ledger; a transcript named by :data:`TRANSCRIPT_ENV` is
        trusted unconditionally.
        """
        trusted = bool(os.environ.get(TRANSCRIPT_ENV))
        transcript = find_transcript(run_dir)
        if transcript is None:
            logger.debug("no Claude Code transcript found; nothing to harvest")
            return None
        blocks, boundaries = _scan(transcript)
        if not trusted and not _is_this_loops_session(boundaries, candidate):
            logger.debug(
                "transcript %s does not reference this eval; skipping", transcript
            )
            return None
        text = _window_text(blocks, boundaries)
        return text if text.strip() else None


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


__all__ = ["ClaudeCodeSource", "find_transcript", "harvest_window", "TRANSCRIPT_ENV"]
