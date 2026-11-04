"""Research browser page (meta_research.ui) over a synthetic ledger and idea board."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from meta_research import ideas as ib
from meta_research.cli import main as cli_main
from meta_research.interfaces import Objective
from meta_research.ui import render_ui, ui_data

from test_suggest import lab  # noqa: F401  (fixture: a synthetic two-objective experiment)
from test_trajectory import _ledger

ACC = Objective("acc", direction="max")


def _board(run):
    b = ib.Board(run)
    b.add_idea(title="wider fins", statement="Wider fins move more heat.", change="fin width +20%")
    b.add_idea(title="merge the two", statement="Combine base and other.", origin="evolution", parents=["i001"])
    b.add_review("i001", kind="initial", verdict="pass", critique="clear </script> mechanism")
    b.add_match("i001", "i002", winner="i002", rationale="the merge keeps both gains")
    b.link("i001", "002_wider")
    return b


def _embedded(path):
    html = path.read_text(encoding="utf-8")
    blob = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    return html, json.loads(blob.replace("<\\/", "</"))


def test_data_joins_runs_views_and_tournament(lab):  # noqa: F811
    run, add = lab
    _ledger(add)
    _board(run)
    d = ui_data(run, [ACC], scores={"005_merge": {"acc": 0.9}}, title="lab")
    assert d["title"] == "lab" and len(d["runs"]) == 6
    merge = d["runs"]["005_merge"]
    assert merge["scores"]["acc"] == 0.9 and set(merge["parents"]) == {"002_wider", "001_other"}
    assert d["runs"]["002_wider"]["idea"] == "i001" and "005_merge" in d["runs"]["002_wider"]["children"]
    assert d["views"][0]["objective"] == "acc"
    t = d["tournament"]
    rows = {r["id"]: r for r in t["ideas"]}
    assert rows["i002"]["wins"] == 1 and rows["i001"]["runs"] == ["002_wider"]
    assert rows["i001"]["reviews"][0]["verdict"] == "pass" and rows["i002"]["parents"] == ["i001"]
    assert t["matches"][0]["rationale"] == "the merge keeps both gains"


def test_render_is_self_contained_and_escapes_script_tags(lab, tmp_path):  # noqa: F811
    run, add = lab
    _ledger(add)
    _board(run)
    out = render_ui(ui_data(run, [ACC]), tmp_path / "r.html")
    html, data = _embedded(out)
    assert html.startswith("<title>") and "<!doctype" not in html.lower()
    assert data["tournament"]["ideas"][0]["reviews"] or data["tournament"]["ideas"][1]["reviews"]
    assert "clear </script> mechanism" in json.dumps(data)  # survives the round trip, closed tag escaped in HTML
    assert html.count("</script>") == 2  # the data block and the page script, nothing from the data
    for src in re.findall(r'<script[^>]*\ssrc="([^"]+)"', html):
        pytest.fail(f"unexpected external script {src}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_page_script_parses(lab, tmp_path):  # noqa: F811
    run, add = lab
    _ledger(add)
    out = render_ui(ui_data(run, [ACC]), tmp_path / "r.html")
    js = re.findall(r"<script>(.*?)</script>", out.read_text(encoding="utf-8"), re.S)[-1]
    (tmp_path / "page.js").write_text(js, encoding="utf-8")
    assert subprocess.run(["node", "--check", str(tmp_path / "page.js")], capture_output=True).returncode == 0


def test_without_board(lab):  # noqa: F811
    run, add = lab
    _ledger(add)
    assert ui_data(run, [ACC])["tournament"] is None


def test_cli(lab, capsys):  # noqa: F811
    run, add = lab
    _ledger(add)
    _board(run)
    (run / "rescored.json").write_text(json.dumps({"005_merge": {"pooled": 1.0}}))
    assert cli_main(["ui", "--rescored", "rescored.json", "--objective", "acc"]) == 0
    out = capsys.readouterr().out
    assert "6 runs" in out and "2 ideas, 1 matches" in out
    _, data = _embedded(run / "research.html")
    assert [v["objective"] for v in data["views"]][:2] == ["acc", "pooled"]
    assert cli_main(["ui", "--objective", "nope"]) == 2
