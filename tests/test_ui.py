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
    ib.Board(run).start("i002", "merge_run", where="box2")
    rows = {r["id"]: r for r in ui_data(run, [ACC])["tournament"]["ideas"]}
    assert rows["i002"]["status"] == "running" and rows["i002"]["started"][0]["where"] == "box2"
    assert rows["i001"]["started"] == []


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


def test_serve_rebuilds_and_reloads_on_ledger_change(lab):  # noqa: F811
    import threading
    import time
    import urllib.request

    from meta_research import ui

    run, add = lab
    _ledger(add)
    b = _board(run)
    calls = []

    def build():
        calls.append(1)
        return ui_data(run, [ACC], title="lab")

    ready = threading.Event()
    box = {}

    def on_ready(server):
        box["server"] = server
        ready.set()

    threading.Thread(target=ui.serve, args=(build, run), kwargs={"port": 0, "on_ready": on_ready},
                     daemon=True).start()
    assert ready.wait(5)
    base = f"http://127.0.0.1:{box['server'].server_address[1]}"
    page = urllib.request.urlopen(base + "/").read().decode()
    assert "const LIVE = true" in page and "wider fins" in page and calls
    s1 = urllib.request.urlopen(base + "/stamp").read().decode()
    time.sleep(0.01)
    b.add_review("i002", kind="initial", verdict="revise", critique="later")
    s2 = urllib.request.urlopen(base + "/stamp").read().decode()
    assert s1 != s2
    assert urllib.request.urlopen(base + "/").read().decode().count("later") >= 1
    try:
        urllib.request.urlopen(base + "/nope")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    box["server"].shutdown()


def test_static_page_is_not_live(lab, tmp_path):  # noqa: F811
    run, add = lab
    _ledger(add)
    html = render_ui(ui_data(run, [ACC]), tmp_path / "r.html").read_text(encoding="utf-8")
    assert "const LIVE = false" in html


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
