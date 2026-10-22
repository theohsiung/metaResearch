"""Operator + parent suggestions (meta_research.suggest) over a synthetic two-objective ledger."""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from meta_research.cli import main as cli_main
from meta_research.runner import evaluate_and_record
from meta_research.suggest import (
    context,
    load_rows,
    objectives_from_frontier,
    pareto_layers,
    rank_weights,
    suggest,
)

PREPARE = '''
from meta_research import Objective
from meta_research.evaluators import NumericalEvaluator

OBJECTIVES = [Objective("acc", direction="max"), Objective("cost", direction="min")]
BUDGET = {}
OPERATING = {}
BASELINES = []
DESIGNS_DIR = "designs"


def _simulate(params, out_dir):
    if params.get("crash"):
        raise RuntimeError("boom")
    return {"acc": params["acc"], "cost": params["cost"]}, {}, {}


def make_evaluator():
    return NumericalEvaluator(OBJECTIVES, _simulate)
'''


@pytest.fixture()
def lab(tmp_path: Path):
    run = tmp_path / "run"
    (run / "designs").mkdir(parents=True)
    (run / "prepare.py").write_text(textwrap.dedent(PREPARE), encoding="utf-8")
    (run / "designs" / "__init__.py").write_text("", encoding="utf-8")
    for args in (["init"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", *args], cwd=run, check=True, capture_output=True)
    prev = Path.cwd()
    os.chdir(run)
    sys.path.insert(0, str(run))
    import importlib.util

    spec = importlib.util.spec_from_file_location("suggest_prepare", run / "prepare.py")
    experiment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(experiment)

    def add(name: str, params: dict, parent: str = "", operator: str = "", inspired_by=None) -> str:
        (run / "designs" / f"{name}.py").write_text(
            "from meta_research import DesignSpec\n\n\ndef build():\n"
            f"    return DesignSpec(params={params!r})\n", encoding="utf-8")
        for mod in [m for m in sys.modules if m.startswith("designs")]:
            del sys.modules[mod]
        hyp = {"axis": "test", "parent": parent, "operator": operator, "change": name}
        if inspired_by:
            hyp["inspired_by"] = inspired_by
        evaluate_and_record(name, experiment, run, hypothesis=hyp)
        return max(r["id"] for r in load_rows(run))

    try:
        yield run, add
    finally:
        os.chdir(prev)
        sys.path.remove(str(run))
        for mod in [m for m in sys.modules if m.startswith("designs")]:
            del sys.modules[mod]


def test_rank_weights_decrease_and_sum_to_one():
    w = rank_weights(5, 0.2)
    assert abs(sum(w) - 1) < 1e-12 and all(a > b for a, b in zip(w, w[1:]))
    assert rank_weights(5, 2.0)[0] < w[0]  # a hotter temperature flattens the selection
    assert rank_weights(1, 0.2) == [1.0]


def test_pareto_layers_and_operator_recorded(lab):
    run, add = lab
    a = add("a", {"acc": 0.9, "cost": 5.0}, operator="draft")
    b = add("b", {"acc": 0.7, "cost": 1.0}, operator="draft")
    c = add("c", {"acc": 0.6, "cost": 6.0}, parent="a", operator="improve")  # dominated by a
    ranked = pareto_layers(load_rows(run), objectives_from_frontier(run))
    assert [(r["id"], r["layer"]) for r in ranked] == [(a, 0), (b, 0), (c, 1)]
    assert "operator: improve" in (run / "experience" / c / "hypothesis.md").read_text()
    node = next(n for n in json.loads((run / "kg.json").read_text())["nodes"] if n["id"] == c)
    assert node["operator"] == "improve"


def test_debug_first_then_give_up_after_two_attempts(lab):
    run, add = lab
    add("a", {"acc": 0.9, "cost": 5.0})
    x = add("x", {"acc": 0, "cost": 0, "crash": True}, parent="a", operator="improve")
    rec = suggest(run)
    assert rec["operator"] == "debug" and rec["parents"] == [x]
    x1 = add("x1", {"acc": 0, "cost": 0, "crash": True}, parent="x", operator="debug")
    assert suggest(run)["parents"] == [x1]
    add("x2", {"acc": 0, "cost": 0, "crash": True}, parent="x1", operator="debug")
    assert suggest(run)["operator"] != "debug"


def test_stagnant_parent_is_not_offered_for_improve(lab):
    run, add = lab
    a = add("a", {"acc": 0.9, "cost": 5.0})
    b = add("b", {"acc": 0.5, "cost": 1.0})
    for i in range(3):
        add(f"a{i}", {"acc": 0.8, "cost": 6.0}, parent="a", operator="improve")
    picks = collections.Counter()
    for seed in range(200):
        rec = suggest(run, seed=seed)
        if rec["operator"] == "improve":
            picks[rec["parents"][0]] += 1
    assert picks and a not in picks and b in picks
    assert any("stagnant" in f for f in suggest(run, seed=1)["facts"])


def test_deterministic_mix_and_crossover_across_lineages(lab):
    run, add = lab
    add("a", {"acc": 0.9, "cost": 5.0})
    add("b", {"acc": 0.7, "cost": 1.0})
    add("a1", {"acc": 0.92, "cost": 5.5}, parent="a", operator="improve")
    rows = {r["id"]: r for r in load_rows(run)}
    assert suggest(run, seed=7) == suggest(run, seed=7)
    ops = collections.Counter()
    for seed in range(400):
        rec = suggest(run, seed=seed)
        ops[rec["operator"]] += 1
        assert all(p in rows and rows[p]["status"] in ("frontier", "dominated") for p in rec["parents"])
        if rec["operator"] == "crossover":
            roots = {"a" if rows[p]["name"].startswith("a") else "b" for p in rec["parents"]}
            assert roots == {"a", "b"}
    assert 0.05 < ops["crossover"] / 400 < 0.3 and 0.03 < ops["draft"] / 400 < 0.2
    assert ops["improve"] > ops["crossover"] + ops["draft"]


def test_context_scopes_the_experience(lab, capsys):
    run, add = lab
    a = add("a", {"acc": 0.9, "cost": 5.0})
    a1 = add("a1", {"acc": 0.8, "cost": 6.0}, parent="a", operator="improve")
    x = add("x", {"acc": 0, "cost": 0, "crash": True}, parent="a1", operator="improve")
    improve = dict(context(run, "improve", [a]))
    assert list(improve) == [a, a1]  # the parent and its own children; x is a grandchild
    debug = [b for b, _ in context(run, "debug", [x])]
    assert debug == [x, a1]
    with pytest.raises(ValueError):
        context(run, "improve", ["999_nope"])
    assert cli_main(["context", "--operator", "draft"]) == 0
    assert "Pareto frontier member" in capsys.readouterr().out
    assert cli_main(["suggest", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["operator"] == "debug"
