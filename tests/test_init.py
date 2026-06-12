"""Tests for `meta-research init` scaffolding (git-init + skill copy + --from example)."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from meta_research import cli


def _init(tmp_path: Path, name: str, from_example: str | None = None) -> int:
    args = argparse.Namespace(name=name, run_dir=str(tmp_path), from_example=from_example)
    return cli.cmd_init(args)


def _has_commit(repo: Path) -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    return r.returncode == 0


def test_init_stub_scaffolds_files_skill_and_git(tmp_path: Path) -> None:
    assert _init(tmp_path, "exp1") == 0
    target = tmp_path / "exp1"
    # stub experiment files
    assert (target / "prepare.py").is_file()
    assert (target / "program.md").is_file()
    assert (target / "designs" / "baseline.py").is_file()
    # skill copied so Claude Code loads it in-dir
    assert (target / ".claude" / "skills" / "meta-research" / "SKILL.md").is_file()
    assert (target / ".claude" / "skills" / "meta-research" / "REFERENCE.md").is_file()
    # git initialized with an initial commit (append-only ledger live immediately)
    assert (target / ".git").is_dir()
    assert _has_commit(target)


def test_init_from_water_cooling_copies_example_without_artifacts(tmp_path: Path) -> None:
    assert _init(tmp_path, "exp2", from_example="water_cooling") == 0
    target = tmp_path / "exp2"
    # real example content present
    assert (target / "objective.py").is_file()
    assert (target / "designs" / "straight_fins.py").is_file()
    assert (target / "designs" / "pin_fins.py").is_file()
    assert (target / ".claude" / "skills" / "meta-research" / "SKILL.md").is_file()
    # generated/scratch artifacts are NOT copied
    assert not (target / "experience").exists()
    assert not (target / "results.tsv").exists()
    assert not (target / "frontier.json").exists()
    assert not list(target.glob("*.png"))
    assert _has_commit(target)


def test_init_from_example_excludes_generated_kg(
    tmp_path: Path, monkeypatch
) -> None:
    # A user who actually ran the example leaves generated artifacts behind
    # (kg.json like frontier.json/results.tsv). Scaffolding must not copy them.
    import shutil

    examples_root = tmp_path / "examples"
    src = examples_root / "water_cooling"
    shutil.copytree(cli._examples_root() / "water_cooling", src)
    (src / "kg.json").write_text('{"version": 1, "nodes": []}', encoding="utf-8")
    (src / "frontier.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "_examples_root", lambda: examples_root)

    assert _init(tmp_path, "exp_kg", from_example="water_cooling") == 0
    target = tmp_path / "exp_kg"
    assert (target / "prepare.py").is_file()
    assert not (target / "kg.json").exists()
    assert not (target / "frontier.json").exists()


def test_init_unknown_example_errors(tmp_path: Path) -> None:
    assert _init(tmp_path, "exp3", from_example="does_not_exist") == 2
    assert not (tmp_path / "exp3").exists()


def test_init_refuses_existing_target(tmp_path: Path) -> None:
    (tmp_path / "exp4").mkdir()
    assert _init(tmp_path, "exp4") == 2
