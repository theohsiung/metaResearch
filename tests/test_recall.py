"""Experience recall (meta_research.recall): BM25 over the ledger's hypotheses."""

from __future__ import annotations

import json

from meta_research.cli import main as cli_main
from meta_research.recall import recall, tokenize

from test_suggest import lab  # noqa: F401  (fixture: a synthetic two-objective experiment)


def test_tokenize_lowercases_words():
    assert tokenize("Option-ORDER shuffle_more, v2!") == ["option", "order", "shuffle", "more", "v2"]


def test_recall_ranks_matching_ideas_and_reports_outcomes(lab, capsys):  # noqa: F811
    run, add = lab
    add("base", {"acc": 0.8, "cost": 5.0})
    add("shuffle", {"acc": 0.82, "cost": 5.0}, parent="base", operator="improve")
    add("bigbatch", {"acc": 0.7, "cost": 4.0}, parent="base", operator="improve")
    add("shuffle_more", {"acc": 0.79, "cost": 5.0}, parent="shuffle", operator="improve")
    hits = recall(run, "shuffle")
    assert [h["id"] for h in hits] == ["001_shuffle", "003_shuffle_more"]
    assert hits[0]["status"] in ("frontier", "dominated") and hits[0]["scores"]["acc"] == 0.82
    assert recall(run, "nothing like this") == []
    assert recall(run, "") == []
    assert cli_main(["recall", "bigbatch", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "002_bigbatch"
    assert cli_main(["recall", "unmatched-term"]) == 0
    assert "no earlier candidate" in capsys.readouterr().out
