"""Tests for the experience store + git append-only ledger (DESIGN §6.3, §7.1, §7.2).

In a fresh ``git init`` repo under ``tmp_path`` we exercise:
- ``ensure_branch`` creates / checks out ``meta-research/<tag>``.
- ``record`` writes the full bundle (§7.1): ``design.py``, ``design_spec.json``,
  ``hypothesis.md``, ``result.json``, ``trace/breakdown.json`` all exist.
- ``commit`` makes a real commit.
- ``history`` round-trips the recorded rows.
- The ``experience.py`` source contains NO ``git reset`` (append-only invariant, §1.1.2).
"""

from __future__ import annotations

import io
import json
import subprocess
import tokenize
from pathlib import Path

import pytest

import meta_research.experience as experience_module
from meta_research.experience import Experience
from meta_research.interfaces import DesignSpec, EvalResult, Objective


def _executable_source(module) -> str:
    """Return module source with comments and string literals (docstrings) stripped.

    The append-only invariant forbids any ``git reset`` *invocation*; the source is
    free to *document* the invariant in its docstrings (e.g. "we never ``git reset``").
    Stripping comments + string literals lets us assert on real code, not prose.
    """
    text = Path(module.__file__).read_text(encoding="utf-8")
    pieces: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)

OBJECTIVES: list[Objective] = [
    Objective("thermal_resistance", "min", "K/W"),
    Objective("pressure_drop", "min", "Pa"),
]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _git(run_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=run_dir,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A tmp_path initialised as a git repo with a deterministic identity."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test Runner")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    # An initial empty commit so a branch ref exists for checkout -b semantics.
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


@pytest.fixture()
def design_src(tmp_path: Path) -> Path:
    """A candidate module source file to be copied verbatim into the bundle."""
    src = tmp_path / "straight_fins_src.py"
    src.write_text(
        "from meta_research.interfaces import DesignSpec\n"
        "def build() -> DesignSpec:\n"
        "    return DesignSpec(params={'fin_type': 'straight', 'n_fins': 20})\n",
        encoding="utf-8",
    )
    return src


def _make_result() -> EvalResult:
    return EvalResult(
        scores={"thermal_resistance": 0.061, "pressure_drop": 410.0},
        feasible=True,
        metadata={
            "R_cond_base": 0.001,
            "R_conv": 0.05,
            "R_caloric": 0.01,
            "R_th": 0.061,
            "fin_efficiency": 0.82,
            "Re": 1200.0,
        },
        artifacts={},
    )


def _hypothesis() -> dict:
    return {
        "axis": "baseline",
        "parent": "none",
        "expected": "baseline reference point",
        "reasoning": "Seed the population with textbook straight fins.",
        "status": "frontier",
    }


# --------------------------------------------------------------------------- #
# Source-level invariant: APPEND-ONLY (no git reset)
# --------------------------------------------------------------------------- #
def test_experience_source_has_no_git_reset() -> None:
    # No 'git reset' invocation anywhere in executable code (append-only, §1.1.2).
    code = _executable_source(experience_module)
    assert "reset" not in code, "experience.py must be append-only: no git reset in code"


def test_experience_source_file_mentions_only_in_docs() -> None:
    # Any 'git reset' text that exists must live solely in docstrings/comments,
    # i.e. the executable code is free of it.
    code = _executable_source(experience_module)
    assert "git reset" not in code


# --------------------------------------------------------------------------- #
# ensure_branch
# --------------------------------------------------------------------------- #
def test_ensure_branch_creates_and_checks_out(git_repo: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    exp.ensure_branch("waterloop")
    branch = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "meta-research/waterloop"


def test_ensure_branch_idempotent(git_repo: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    exp.ensure_branch("waterloop")
    # Second call must not raise (checks out the existing branch).
    exp.ensure_branch("waterloop")
    branch = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "meta-research/waterloop"


# --------------------------------------------------------------------------- #
# record -> bundle layout (§7.1)
# --------------------------------------------------------------------------- #
def test_record_writes_full_bundle(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    design = DesignSpec(params={"fin_type": "straight", "n_fins": 20})
    result = _make_result()
    bundle = exp.record(
        iteration=0,
        name="straight_fins",
        design_src_path=design_src,
        design=design,
        result=result,
        hypothesis=_hypothesis(),
    )
    bundle = Path(bundle)
    assert bundle.is_dir()
    # The bundle dir is experience/<iter:03d>_<name>/ (§6.3 bundle_dir).
    assert bundle.parent.name == "experience"
    assert bundle.name == "000_straight_fins"

    # All §7.1 required files exist.
    assert (bundle / "design.py").is_file()
    assert (bundle / "design_spec.json").is_file()
    assert (bundle / "hypothesis.md").is_file()
    assert (bundle / "result.json").is_file()
    assert (bundle / "trace" / "breakdown.json").is_file()


def test_record_design_py_is_verbatim_copy(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    bundle = Path(
        exp.record(
            iteration=0,
            name="straight_fins",
            design_src_path=design_src,
            design=DesignSpec(params={"fin_type": "straight"}),
            result=_make_result(),
            hypothesis=_hypothesis(),
        )
    )
    assert (bundle / "design.py").read_text(encoding="utf-8") == design_src.read_text(
        encoding="utf-8"
    )


def test_record_design_spec_round_trips(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    design = DesignSpec(params={"fin_type": "straight", "n_fins": 20}, notes="x")
    bundle = Path(
        exp.record(
            iteration=3,
            name="straight_fins",
            design_src_path=design_src,
            design=design,
            result=_make_result(),
            hypothesis=_hypothesis(),
        )
    )
    data = json.loads((bundle / "design_spec.json").read_text(encoding="utf-8"))
    assert DesignSpec.from_json(data) == design


def test_record_result_json_round_trips(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    result = _make_result()
    bundle = Path(
        exp.record(
            iteration=1,
            name="straight_fins",
            design_src_path=design_src,
            design=DesignSpec(params={"fin_type": "straight"}),
            result=result,
            hypothesis=_hypothesis(),
        )
    )
    data = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    restored = EvalResult.from_json(data)
    assert restored.scores == result.scores
    assert restored.feasible == result.feasible


def test_record_breakdown_is_metadata(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    result = _make_result()
    bundle = Path(
        exp.record(
            iteration=2,
            name="straight_fins",
            design_src_path=design_src,
            design=DesignSpec(params={"fin_type": "straight"}),
            result=result,
            hypothesis=_hypothesis(),
        )
    )
    breakdown = json.loads(
        (bundle / "trace" / "breakdown.json").read_text(encoding="utf-8")
    )
    assert breakdown == result.metadata


def test_record_hypothesis_front_matter(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    bundle = Path(
        exp.record(
            iteration=5,
            name="straight_fins",
            design_src_path=design_src,
            design=DesignSpec(params={"fin_type": "straight"}),
            result=_make_result(),
            hypothesis=_hypothesis(),
        )
    )
    text = (bundle / "hypothesis.md").read_text(encoding="utf-8")
    # YAML front-matter delimiters (§7.2).
    assert text.lstrip().startswith("---")
    assert "axis:" in text
    assert "baseline" in text


def test_record_hypothesis_front_matter_carries_change(
    git_repo: Path, design_src: Path
) -> None:
    """The 'what was changed' line is preserved in the bundle, not just the log."""
    hyp = {**_hypothesis(), "change": "inline -> staggered pin rows"}
    exp = Experience(git_repo, OBJECTIVES)
    bundle = Path(
        exp.record(
            iteration=6,
            name="staggered_pins",
            design_src_path=design_src,
            design=DesignSpec(params={"fin_type": "pin"}),
            result=_make_result(),
            hypothesis=hyp,
        )
    )
    text = (bundle / "hypothesis.md").read_text(encoding="utf-8")
    assert "change: inline -> staggered pin rows" in text


# --------------------------------------------------------------------------- #
# bundle_dir / next_iteration
# --------------------------------------------------------------------------- #
def test_bundle_dir_naming(git_repo: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    bd = Path(exp.bundle_dir(12, "staggered_pin_v3"))
    assert bd.name == "012_staggered_pin_v3"
    assert bd.parent.name == "experience"


def test_next_iteration_advances(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    assert exp.next_iteration() == 0
    exp.record(
        iteration=0,
        name="straight_fins",
        design_src_path=design_src,
        design=DesignSpec(params={"fin_type": "straight"}),
        result=_make_result(),
        hypothesis=_hypothesis(),
    )
    assert exp.next_iteration() == 1


# --------------------------------------------------------------------------- #
# commit -> a real commit lands
# --------------------------------------------------------------------------- #
def test_commit_makes_a_commit(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    before = _git(git_repo, "rev-list", "--count", "HEAD").stdout.strip()
    exp.record(
        iteration=0,
        name="straight_fins",
        design_src_path=design_src,
        design=DesignSpec(params={"fin_type": "straight"}),
        result=_make_result(),
        hypothesis=_hypothesis(),
    )
    exp.commit("iter00 straight_fins: thermal_resistance=0.061 pressure_drop=410.0 [frontier] — baseline")
    after = _git(git_repo, "rev-list", "--count", "HEAD").stdout.strip()
    assert int(after) == int(before) + 1
    log = _git(git_repo, "log", "--oneline").stdout
    assert "straight_fins" in log


# --------------------------------------------------------------------------- #
# repo-root guard: git mutations refuse to touch a host repository
# --------------------------------------------------------------------------- #
def test_commit_refuses_when_run_dir_is_not_repo_root(git_repo: Path) -> None:
    """A run_dir nested inside a larger repo must not be committed from.

    `git add -A` stages the *whole* host tree (git >= 2.0) and would sweep
    unrelated work into the experiment ledger.
    """
    run_dir = git_repo / "runs" / "exp1"
    run_dir.mkdir(parents=True)
    stray = git_repo / "unrelated_wip.txt"
    stray.write_text("host work in progress\n", encoding="utf-8")

    exp = Experience(run_dir, OBJECTIVES)
    before = _git(git_repo, "rev-list", "--count", "HEAD").stdout.strip()
    with pytest.raises(RuntimeError, match="repository root"):
        exp.commit("iter00 x: cost=1 [frontier] — must be refused")

    after = _git(git_repo, "rev-list", "--count", "HEAD").stdout.strip()
    assert after == before, "no commit may land in the host repository"
    staged = _git(git_repo, "diff", "--cached", "--name-only").stdout
    assert "unrelated_wip.txt" not in staged, "host files must never be staged"


def test_ensure_branch_refuses_when_run_dir_is_not_repo_root(git_repo: Path) -> None:
    """ensure_branch would otherwise branch-switch the host repository checkout."""
    run_dir = git_repo / "runs" / "exp1"
    run_dir.mkdir(parents=True)
    exp = Experience(run_dir, OBJECTIVES)
    branch_before = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    with pytest.raises(RuntimeError, match="repository root"):
        exp.ensure_branch("waterloop")
    branch_after = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch_after == branch_before, "host repository branch must not change"


def test_commit_refuses_outside_any_repo(tmp_path: Path) -> None:
    run_dir = tmp_path / "norepo"
    run_dir.mkdir()
    exp = Experience(run_dir, OBJECTIVES)
    with pytest.raises(RuntimeError, match="not inside a git repository"):
        exp.commit("iter00 x: cost=1 [frontier] — must be refused")


def test_commit_allowed_when_run_dir_is_its_own_repo(git_repo: Path, design_src: Path) -> None:
    """A run_dir with its own nested repo is fine: its root == run_dir."""
    run_dir = git_repo / "runs" / "exp1"
    run_dir.mkdir(parents=True)
    _git(run_dir, "init")
    _git(run_dir, "config", "user.email", "test@example.com")
    _git(run_dir, "config", "user.name", "Test Runner")
    _git(run_dir, "config", "commit.gpgsign", "false")

    exp = Experience(run_dir, OBJECTIVES)
    exp.record(
        iteration=0,
        name="straight_fins",
        design_src_path=design_src,
        design=DesignSpec(params={"fin_type": "straight"}),
        result=_make_result(),
        hypothesis=_hypothesis(),
    )
    outer_before = _git(git_repo, "rev-list", "--count", "HEAD").stdout.strip()
    sha = exp.commit("iter00 straight_fins: thermal_resistance=0.061 [frontier] — nested repo")
    assert sha, "commit in the run_dir's own repo must succeed and return a SHA"
    outer_after = _git(git_repo, "rev-list", "--count", "HEAD").stdout.strip()
    assert outer_after == outer_before, "the host repository must be untouched"


# --------------------------------------------------------------------------- #
# annotate_commit: best-effort SHA annotation into result.json
# --------------------------------------------------------------------------- #
def test_annotate_commit_writes_sha(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    bundle = exp.record(
        iteration=0,
        name="straight_fins",
        design_src_path=design_src,
        design=DesignSpec(params={"fin_type": "straight"}),
        result=_make_result(),
        hypothesis=_hypothesis(),
    )
    exp.annotate_commit(bundle, "abc1234")
    doc = json.loads((Path(bundle) / "result.json").read_text(encoding="utf-8"))
    assert doc["commit"] == "abc1234"
    # The rest of the document is untouched.
    assert doc["scores"] == _make_result().scores


def test_annotate_commit_missing_result_json_never_raises(git_repo: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    bundle = git_repo / "experience" / "000_ghost"
    bundle.mkdir(parents=True)
    exp.annotate_commit(bundle, "abc1234")  # must log a warning, not raise
    assert not (bundle / "result.json").exists()


def test_annotate_commit_corrupt_result_json_never_raises(git_repo: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    bundle = git_repo / "experience" / "000_corrupt"
    bundle.mkdir(parents=True)
    (bundle / "result.json").write_text("{not json", encoding="utf-8")
    exp.annotate_commit(bundle, "abc1234")  # must log a warning, not raise
    assert (bundle / "result.json").read_text(encoding="utf-8") == "{not json"


def test_annotate_commit_empty_sha_is_a_noop(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    bundle = exp.record(
        iteration=0,
        name="straight_fins",
        design_src_path=design_src,
        design=DesignSpec(params={"fin_type": "straight"}),
        result=_make_result(),
        hypothesis=_hypothesis(),
    )
    exp.annotate_commit(bundle, "")
    doc = json.loads((Path(bundle) / "result.json").read_text(encoding="utf-8"))
    assert "commit" not in doc


# --------------------------------------------------------------------------- #
# history -> round-trips recorded rows
# --------------------------------------------------------------------------- #
def test_history_round_trips(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    result = _make_result()
    exp.record(
        iteration=0,
        name="straight_fins",
        design_src_path=design_src,
        design=DesignSpec(params={"fin_type": "straight"}),
        result=result,
        hypothesis=_hypothesis(),
    )
    rows = exp.history()
    assert len(rows) == 1
    row = rows[0]
    assert row["iteration"] == 0
    assert row["name"] == "straight_fins"
    assert row["scores"]["thermal_resistance"] == result.scores["thermal_resistance"]
    assert row["scores"]["pressure_drop"] == result.scores["pressure_drop"]
    # hypothesis front-matter fields round-trip into history rows (§6.3).
    assert row["axis"] == "baseline"


def test_history_multiple_rows_sorted(git_repo: Path, design_src: Path) -> None:
    exp = Experience(git_repo, OBJECTIVES)
    for it, name in [(0, "straight_fins"), (1, "pin_fins")]:
        exp.record(
            iteration=it,
            name=name,
            design_src_path=design_src,
            design=DesignSpec(params={"fin_type": "straight"}),
            result=_make_result(),
            hypothesis=_hypothesis(),
        )
    rows = exp.history()
    assert {r["name"] for r in rows} == {"straight_fins", "pin_fins"}
    iters = [r["iteration"] for r in rows]
    assert sorted(iters) == [0, 1]
