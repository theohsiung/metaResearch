"""Search-trajectory plot (meta_research.trajectory) over a synthetic ledger."""

from __future__ import annotations

import json

from meta_research.cli import main as cli_main
from meta_research.interfaces import Objective
from meta_research.trajectory import render_trajectory, trajectory_data

from test_suggest import lab  # noqa: F401  (fixture: a synthetic two-objective experiment)

ACC = Objective("acc", direction="max")


def _ledger(add):
    add("base", {"acc": 0.70, "cost": 5.0}, operator="draft")
    add("other", {"acc": 0.65, "cost": 2.0}, operator="draft")
    add("wider", {"acc": 0.78, "cost": 5.5}, parent="base", operator="improve")
    add("oops", {"acc": 0, "cost": 0, "crash": True}, parent="wider", operator="improve")
    add("fixed", {"acc": 0.76, "cost": 5.4}, parent="oops", operator="debug")
    add("merge", {"acc": 0.81, "cost": 4.0}, parent="wider", operator="crossover", inspired_by="other")


def test_points_edges_and_best_so_far(lab):  # noqa: F811
    run, add = lab
    _ledger(add)
    data = trajectory_data(run, ACC, refs={"released": 0.72})
    by_id = {p["id"]: p for p in data["points"]}
    assert by_id["003_oops"]["value"] is None and by_id["003_oops"]["now"] == "crash"
    # colours follow the frontier as it stands now, not the status recorded at eval time
    assert by_id["002_wider"]["status"] == "frontier" and by_id["002_wider"]["now"] == "dominated"
    assert by_id["005_merge"]["now"] == "frontier"
    styles = {(e["src"], e["dst"]): e["style"] for e in data["edges"]}
    assert styles[("000_base", "002_wider")] == "mutation"
    assert styles[("003_oops", "004_fixed")] == "mutation"  # debug keeps a solid line
    assert styles[("002_wider", "005_merge")] == "crossover"
    assert styles[("001_other", "005_merge")] == "crossover"  # the second parent, via inspired_by
    assert data["best_so_far"] == [(0, 0.70), (1, 0.70), (2, 0.78), (4, 0.78), (5, 0.81)]
    labels = {a["id"]: a for a in data["annotations"]}
    assert labels["002_wider"]["why"] == "best-so-far" and labels["002_wider"]["label"] == "2: wider"
    assert "005_merge" in labels and "001_other" in labels  # frontier member (cheapest)
    assert "004_fixed" not in labels and data["refs"] == {"released": 0.72}


def test_render_writes_a_png(lab, tmp_path):  # noqa: F811
    run, add = lab
    _ledger(add)
    out = render_trajectory(trajectory_data(run, ACC, refs={"released": 0.72}), tmp_path / "t.png")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and out.stat().st_size > 10_000


def test_cli(lab, capsys):  # noqa: F811
    run, add = lab
    _ledger(add)
    assert cli_main(["trajectory", "--ref", "released=0.72", "--ref", "turbo=0.8"]) == 0
    assert (run / "trajectory.png").is_file()
    assert "6 candidates" in capsys.readouterr().out
    assert cli_main(["trajectory", "--objective", "cost", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["direction"] == "min"
    assert cli_main(["trajectory", "--objective", "nope"]) == 2
    assert cli_main(["trajectory", "--ref", "bad"]) == 2
