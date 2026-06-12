"""Tests for the proposer-thinking harvest (transcript -> bundle trace).

The harvest reads a Claude-Code-style session transcript (JSONL; assistant
messages whose ``message.content[]`` contains ``{"type": "thinking"}`` blocks)
and captures the thinking produced for the CURRENT iteration: the blocks since
the previous ``meta-research eval``/``seed`` invocation. Captured text lands in
``<bundle>/trace/proposer_thinking.md`` with a pointer line appended to
``hypothesis.md``. Everything is best-effort: no transcript -> no file, never
an exception.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meta_research.experience import Experience
from meta_research.interfaces import DesignSpec, EvalResult, Objective
from meta_research.thinking import capture_thinking, harvest_window

OBJECTIVES = [Objective("cost", "min", "")]


# --------------------------------------------------------------------------- #
# transcript fixture helpers
# --------------------------------------------------------------------------- #
def _thinking_entry(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": text}]}}
    )


def _eval_marker(command: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": command}}
                ]
            },
        }
    )


def _write_transcript(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# harvest_window: which thinking blocks belong to the current iteration
# --------------------------------------------------------------------------- #
def test_no_marker_returns_all_thinking(tmp_path: Path) -> None:
    t = _write_transcript(
        tmp_path / "t.jsonl",
        [_thinking_entry("first thought"), _thinking_entry("second thought")],
    )
    text = harvest_window(t)
    assert "first thought" in text and "second thought" in text


def test_one_marker_takes_blocks_before_it(tmp_path: Path) -> None:
    # The current eval's own tool_use is already in the transcript when the
    # runner executes: the window is everything before that marker.
    t = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _thinking_entry("reasoning for this design"),
            _eval_marker("meta-research eval d1 --commit"),
        ],
    )
    text = harvest_window(t)
    assert "reasoning for this design" in text


def test_two_markers_take_the_window_between_them(tmp_path: Path) -> None:
    t = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _thinking_entry("stale: belongs to iteration 1"),
            _eval_marker("python3 -m meta_research.cli eval d1 --commit"),
            _thinking_entry("fresh: reasoning for iteration 2"),
            _eval_marker("python3 -m meta_research.cli eval d2 --commit"),
        ],
    )
    text = harvest_window(t)
    assert "fresh: reasoning for iteration 2" in text
    assert "stale" not in text


def test_seed_counts_as_a_boundary(tmp_path: Path) -> None:
    t = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _thinking_entry("setup thoughts before seeding"),
            _eval_marker("meta-research seed --commit"),
            _thinking_entry("post-seed reasoning"),
            _eval_marker("meta-research eval d1 --commit"),
        ],
    )
    text = harvest_window(t)
    assert "post-seed reasoning" in text
    assert "setup thoughts" not in text


def test_non_boundary_commands_are_ignored(tmp_path: Path) -> None:
    # frontier/progress/kg inspection calls happen mid-iteration and must NOT
    # truncate the window.
    t = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _eval_marker("meta-research eval d1 --commit"),
            _thinking_entry("looked at the frontier"),
            _eval_marker("meta-research frontier"),
            _thinking_entry("then formed the hypothesis"),
            _eval_marker("meta-research eval d2 --commit"),
        ],
    )
    text = harvest_window(t)
    assert "looked at the frontier" in text
    assert "then formed the hypothesis" in text


# --------------------------------------------------------------------------- #
# capture_thinking: transcript -> bundle trace file (best-effort)
# --------------------------------------------------------------------------- #
def _record_bundle(run_dir: Path) -> Path:
    src = run_dir / "d1_src.py"
    src.write_text("def build(): ...\n", encoding="utf-8")
    exp = Experience(run_dir, OBJECTIVES)
    return Path(
        exp.record(
            iteration=0,
            name="d1",
            design_src_path=src,
            design=DesignSpec(params={"x": 1.0}),
            result=EvalResult(scores={"cost": 1.0}, feasible=True, metadata={}, artifacts={}),
            hypothesis={"axis": "a", "parent": "p", "reasoning": "curated summary"},
        )
    )


def test_capture_writes_trace_and_hypothesis_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_transcript(
        tmp_path / "t.jsonl", [_thinking_entry("the full internal reasoning")]
    )
    monkeypatch.setenv("META_RESEARCH_TRANSCRIPT", str(transcript))
    bundle = _record_bundle(tmp_path)

    dest = capture_thinking(tmp_path, bundle, name="d1")

    assert dest is not None and dest.is_file()
    body = dest.read_text(encoding="utf-8")
    assert "the full internal reasoning" in body
    hyp = (bundle / "hypothesis.md").read_text(encoding="utf-8")
    assert "trace/proposer_thinking.md" in hyp


def test_capture_without_transcript_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("META_RESEARCH_TRANSCRIPT", str(tmp_path / "missing.jsonl"))
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.claude/projects fallback
    bundle = _record_bundle(tmp_path)

    dest = capture_thinking(tmp_path, bundle, name="d1")

    assert dest is None
    assert not (bundle / "trace" / "proposer_thinking.md").exists()


def test_capture_with_empty_window_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_transcript(
        tmp_path / "t.jsonl",
        [_thinking_entry("old"), _eval_marker("meta-research eval d0 --commit"),
         _eval_marker("meta-research eval d1 --commit")],
    )
    monkeypatch.setenv("META_RESEARCH_TRANSCRIPT", str(transcript))
    bundle = _record_bundle(tmp_path)

    dest = capture_thinking(tmp_path, bundle, name="d1")

    assert dest is None


def test_capture_never_raises_on_corrupt_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "t.jsonl"
    bad.write_text("{not json at all\n\x00\x01", encoding="utf-8")
    monkeypatch.setenv("META_RESEARCH_TRANSCRIPT", str(bad))
    bundle = _record_bundle(tmp_path)

    dest = capture_thinking(tmp_path, bundle, name="d1")  # must not raise

    assert dest is None


# --------------------------------------------------------------------------- #
# source registry: runtime adapters are pluggable (Claude Code today; a Codex
# adapter would be one more entry, never a change to capture_thinking)
# --------------------------------------------------------------------------- #
def test_capture_uses_first_source_that_yields_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meta_research.thinking as thinking_pkg

    class _Empty:
        name = "empty-runtime"

        def harvest(self, run_dir, candidate):
            return None

    class _Fake:
        name = "fake-runtime"

        def harvest(self, run_dir, candidate):
            return f"fake thinking for {candidate}"

    monkeypatch.setattr(thinking_pkg, "SOURCES", (_Empty(), _Fake()))
    monkeypatch.delenv("META_RESEARCH_TRANSCRIPT", raising=False)
    bundle = _record_bundle(tmp_path)

    dest = capture_thinking(tmp_path, bundle, name="d1")

    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    assert "fake thinking for d1" in body
    assert "fake-runtime" in body, "the trace header should name its source"


def test_capture_survives_a_crashing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meta_research.thinking as thinking_pkg

    class _Broken:
        name = "broken-runtime"

        def harvest(self, run_dir, candidate):
            raise RuntimeError("adapter exploded")

    class _Fake:
        name = "fake-runtime"

        def harvest(self, run_dir, candidate):
            return "recovered thinking"

    monkeypatch.setattr(thinking_pkg, "SOURCES", (_Broken(), _Fake()))
    bundle = _record_bundle(tmp_path)

    dest = capture_thinking(tmp_path, bundle, name="d1")

    assert dest is not None and "recovered thinking" in dest.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# affinity guard: auto-discovered transcripts must belong to THIS loop
# --------------------------------------------------------------------------- #
def _fake_claude_home(tmp_path: Path, lines: list[str]) -> Path:
    """Build a fake ~/.claude/projects with one transcript; return new HOME."""
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-some-other-project"
    proj.mkdir(parents=True)
    _write_transcript(proj / "session.jsonl", lines)
    return home


def test_autodiscovered_transcript_from_foreign_session_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The newest transcript belongs to an unrelated session (its last boundary
    # references a different design, or none at all): capture must skip rather
    # than pollute the ledger with someone else's thinking.
    home = _fake_claude_home(
        tmp_path,
        [_thinking_entry("unrelated session thoughts"),
         _eval_marker("meta-research eval other_design --commit")],
    )
    monkeypatch.delenv("META_RESEARCH_TRANSCRIPT", raising=False)
    monkeypatch.setenv("HOME", str(home))
    bundle = _record_bundle(tmp_path)

    assert capture_thinking(tmp_path, bundle, name="d1") is None


def test_affinity_guard_rejects_name_as_substring_of_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "d1" must NOT match a session whose last eval was for "d10".
    home = _fake_claude_home(
        tmp_path,
        [_thinking_entry("thinking that belongs to d10"),
         _eval_marker("meta-research eval d10 --commit")],
    )
    monkeypatch.delenv("META_RESEARCH_TRANSCRIPT", raising=False)
    monkeypatch.setenv("HOME", str(home))
    bundle = _record_bundle(tmp_path)

    assert capture_thinking(tmp_path, bundle, name="d1") is None


def test_multiline_command_mentioning_both_words_is_not_a_boundary(
    tmp_path: Path
) -> None:
    # A heredoc whose first line mentions meta-research and a LATER line says
    # "eval" must not count as an iteration boundary (no cross-line matching).
    t = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _thinking_entry("early thought"),
            _eval_marker("echo 'docs about meta-research frontier'\npython3 -c 'eval(\"1\")'"),
            _thinking_entry("late thought"),
        ],
    )
    text = harvest_window(t)
    assert "early thought" in text and "late thought" in text


def test_pointer_append_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_transcript(
        tmp_path / "t.jsonl", [_thinking_entry("the reasoning")]
    )
    monkeypatch.setenv("META_RESEARCH_TRANSCRIPT", str(transcript))
    bundle = _record_bundle(tmp_path)

    capture_thinking(tmp_path, bundle, name="d1")
    capture_thinking(tmp_path, bundle, name="d1")

    hyp = (bundle / "hypothesis.md").read_text(encoding="utf-8")
    assert hyp.count("trace/proposer_thinking.md") == 1


def test_autodiscovered_transcript_with_matching_marker_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_claude_home(
        tmp_path,
        [_thinking_entry("reasoning that led to d1"),
         _eval_marker("meta-research eval d1 --commit")],
    )
    monkeypatch.delenv("META_RESEARCH_TRANSCRIPT", raising=False)
    monkeypatch.setenv("HOME", str(home))
    bundle = _record_bundle(tmp_path)

    dest = capture_thinking(tmp_path, bundle, name="d1")

    assert dest is not None
    assert "reasoning that led to d1" in dest.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# runner integration: eval captures the thinking automatically
# --------------------------------------------------------------------------- #
def test_evaluate_and_record_captures_thinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meta_research.evaluators import NumericalEvaluator
    from meta_research.runner import evaluate_and_record

    transcript = _write_transcript(
        tmp_path / "t.jsonl", [_thinking_entry("why I chose this design")]
    )
    monkeypatch.setenv("META_RESEARCH_TRANSCRIPT", str(transcript))

    designs = tmp_path / "designs"
    designs.mkdir()
    (designs / "__init__.py").write_text("", encoding="utf-8")
    (designs / "d0.py").write_text(
        "from meta_research.interfaces import DesignSpec\n"
        "def build():\n    return DesignSpec(params={'x': 2.0})\n",
        encoding="utf-8",
    )

    class _Experiment:
        OBJECTIVES = OBJECTIVES
        BASELINES = ["d0"]
        DESIGNS_DIR = str(designs)

        @staticmethod
        def make_evaluator() -> NumericalEvaluator:
            return NumericalEvaluator(
                OBJECTIVES, lambda params, out: ({"cost": 1.0}, {}, {})
            )

    result = evaluate_and_record("d0", _Experiment(), tmp_path, commit=False)

    assert result.ok
    trace = tmp_path / "experience" / "000_d0" / "trace" / "proposer_thinking.md"
    assert trace.is_file()
    assert "why I chose this design" in trace.read_text(encoding="utf-8")
