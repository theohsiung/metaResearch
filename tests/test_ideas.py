"""Idea board (meta_research.ideas): Co-Scientist's cards, reviews, Elo tournament and reality matches."""

from __future__ import annotations

import json

import pytest

from meta_research.cli import main as cli_main
from meta_research.ideas import (
    INITIAL_ELO,
    Board,
    BoardError,
    concordance,
    elo,
    expected_score,
    outcomes,
    proximity,
    rank,
    schedule,
    search_cards,
    stats,
    status,
    sync,
)

from test_suggest import lab  # noqa: F401  (fixture: a synthetic two-objective experiment)


def _idea(board: Board, title: str, statement: str = "", **kw) -> str:
    return board.add_idea(title=title, statement=statement or title, **kw)["id"]


def test_cards_and_ideas_get_sequential_ids_and_persist(tmp_path):
    board = Board(tmp_path)
    card = board.add_card(title="mammoth-1 report", source="https://example.org", technique="synthetic decision data",
                          claim="+20 index points from 16x data", tags=["data"])
    assert card["id"] == "L001"
    first = _idea(board, "synthetic causal rows", cites=["L001"])
    second = _idea(board, "program-labelled logic puzzles")
    assert (first, second) == ("i001", "i002")
    again = Board(tmp_path)  # replayed from ideas/board.jsonl
    assert list(again.ideas) == ["i001", "i002"] and again.cards["L001"]["claim"].startswith("+20")
    assert [e["seq"] for e in again.events] == [1, 2, 3]


def test_invalid_events_are_refused(tmp_path):
    board = Board(tmp_path)
    a = _idea(board, "a")
    with pytest.raises(BoardError):
        _idea(board, "cites a missing card", cites=["L009"])
    with pytest.raises(BoardError):
        _idea(board, "evolved from nothing", origin="evolution")
    with pytest.raises(BoardError):
        _idea(board, "unknown parent", origin="evolution", parents=["i042"])
    with pytest.raises(BoardError):
        board.add_review(a, kind="initial", verdict="maybe")
    with pytest.raises(BoardError):
        board.add_match(a, a, winner=a)
    b = _idea(board, "b")
    with pytest.raises(BoardError):
        board.add_match(a, b, winner="i099")
    with pytest.raises(BoardError):
        board.add_match(a, b, winner=a, format="reality")  # reality matches only come from sync
    assert len(board.of("match")) == 0


def test_elo_replays_matches_from_1200():
    assert expected_score(1200, 1200) == 0.5
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        board = Board(d)
        a, b, c = (_idea(board, t) for t in "abc")
        board.add_match(a, b, winner=a)
        board.add_match(a, c, winner=a, format="debate")
        table = elo(board)
        assert table[a]["elo"] > INITIAL_ELO > table[b]["elo"]
        assert table[a]["wins"] == 2 and table[b]["losses"] == 1 and table[c]["matches"] == 1
        assert abs(sum(r["elo"] for r in table.values()) - 3 * INITIAL_ELO) < 1e-9  # zero-sum
        assert table[c]["elo"] > table[b]["elo"]  # c lost to a stronger a than b did, so it loses less


def test_status_follows_the_latest_review(tmp_path):
    board = Board(tmp_path)
    a = _idea(board, "a")
    assert status(board, a) == "new"
    board.add_review(a, kind="initial", verdict="revise", critique="no falsifiable prediction")
    assert status(board, a) == "revise"
    board.add_review(a, kind="full", verdict="pass")
    assert status(board, a) == "active"
    board.add_review(a, kind="deep", verdict="fail", critique="needs a GPU we do not have")
    assert status(board, a) == "rejected"
    assert status(board, a, tested={a}) == "tested"


def test_a_started_idea_is_running_until_tested_and_leaves_the_tournament(tmp_path, capsys):
    board = Board(tmp_path)
    a, b, c = (_idea(board, x) for x in ("alpha mechanism", "beta mechanism", "gamma mechanism"))
    for i in (a, b, c):
        board.add_review(i, kind="initial", verdict="pass")
    assert {m["a"] for m in schedule(board, n=6)} | {m["b"] for m in schedule(board, n=6)} >= {a}
    board.start(a, "d11_minimal_pairs", where="mi300x-2")
    assert status(board, a) == "running" and board.starts()[a][0]["where"] == "mi300x-2"
    assert all(a not in (m["a"], m["b"]) for m in schedule(board, n=6))
    assert status(board, a, tested={a}) == "tested"
    with pytest.raises(BoardError):
        board.start("i999", "x")
    with pytest.raises(BoardError):
        board.start(b, " ")
    assert cli_main(["ideas", "start", b, "--run", "d12_slots_k8", "--where", "r2", "--run-dir", str(tmp_path)]) == 0
    assert "running: d12_slots_k8 on r2" in capsys.readouterr().out
    assert status(Board(tmp_path), b) == "running"


def test_proximity_clusters_similar_ideas_and_flags_duplicates(tmp_path):
    board = Board(tmp_path)
    a = _idea(board, "synthetic causal probability questions", "generate cladder style causal questions")
    b = _idea(board, "synthetic causal probability questions", "generate cladder style causal questions again")
    c = _idea(board, "lower learning rate", "halve the peak lr of the readout")
    graph = proximity(board)
    assert [a, b] in graph["clusters"] and [c] in graph["clusters"]
    assert graph["duplicates"][0]["a"] == a and graph["duplicates"][0]["b"] == b


def test_schedule_prefers_new_ideas_similar_opponents_and_debates_at_the_top(tmp_path):
    board = Board(tmp_path)
    ids = [_idea(board, t, s) for t, s in [
        ("causal data", "synthetic causal questions with exact labels"),
        ("chess data", "chess positions labelled by an engine"),
        ("causal data v2", "synthetic causal questions with exact labels and counterfactuals"),
        ("lr sweep", "halve the learning rate"),
        ("unreviewed", "never reviewed"),
    ]]
    for i in ids[:4]:
        board.add_review(i, kind="initial", verdict="pass")
    board.add_match(ids[0], ids[1], winner=ids[0])
    plan = schedule(board, n=2)
    assert all(ids[4] not in (m["a"], m["b"]) for m in plan)  # unreviewed ideas do not play
    assert ids[3] in (plan[0]["a"], plan[0]["b"])  # the newest unplayed idea plays first
    assert {plan[1]["a"], plan[1]["b"]} == {ids[2], ids[0]}  # the next one meets its nearest neighbour
    assert {m["format"] for m in plan} <= {"single", "debate"}
    top_pair = schedule(board, n=1, top_fraction=1.0)[0]
    assert top_pair["format"] == "debate"
    assert schedule(Board(tmp_path / "empty"), n=3) == []


def test_reality_outcomes_sync_and_concordance(lab, capsys):  # noqa: F811
    run, add = lab
    board = Board(run)
    good = _idea(board, "more same-skill data")
    bad = _idea(board, "bigger batch")
    untested = _idea(board, "something else")
    for i in (good, bad, untested):
        board.add_review(i, kind="initial", verdict="pass")
    board.add_match(good, bad, winner=bad, rationale="the judge liked batch size")  # wrong, as it turns out
    add("base", {"acc": 0.80, "cost": 5.0})
    add("data", {"acc": 0.84, "cost": 5.0}, parent="base", idea=good)
    bb = add("bigbatch", {"acc": 0.78, "cost": 4.0}, parent="base")
    board.link(bad, bb)
    with pytest.raises(BoardError):
        board.link(bad, bb)
    real = outcomes(board)
    assert real[good]["gain"] == pytest.approx(0.05) and real[bad]["gain"] == pytest.approx(-0.025)
    assert untested not in real
    weighted = outcomes(board, weights={"acc": 0.5, "cost": 0.5})  # cost is minimized: 5 -> 4 is a gain
    assert weighted[bad]["gain"] == pytest.approx(0.5 * -0.025 + 0.5 * 0.2)
    base_id = real[good]["parent"]
    rescored = {base_id: {"pooled": 50.0}, real[good]["bundle"]: {"pooled": 49.0}, bb: {"pooled": 51.0}}
    flipped = outcomes(board, scores=rescored)  # a later, consistent metric reverses the order
    assert flipped[good]["gain"] == pytest.approx(-0.02) and flipped[bad]["gain"] == pytest.approx(0.02)
    with pytest.raises(BoardError):
        outcomes(board, scores={base_id: {"x": 1.0}, bb: {"y": 2.0}})  # no shared name: say which
    conc = concordance(board)
    assert conc == {"agree": 0, "n": 1, "rate": 0.0, "by_format": {"single": {"agree": 0, "n": 1}}}
    added = sync(board)
    assert len(added) == 1 and added[0]["winner"] == good and added[0]["format"] == "reality"
    assert sync(board) == []  # idempotent
    ranked = rank(board)
    assert ranked[0]["id"] == good and ranked[0]["status"] == "tested"
    assert {r["id"]: r["status"] for r in ranked}[untested] == "active"
    summary = stats(board)
    assert summary["matches"] == {"reality": 1, "single": 1}
    assert summary["by_method"]["generation"]["tested"] == 2 and summary["by_method"]["generation"]["improved"] == 1
    # untested ideas are scheduled against tested anchors
    plan = schedule(board, n=1, tested=set(real))
    assert untested in (plan[0]["a"], plan[0]["b"])


def test_failed_runs_lose_reality_matches(lab):  # noqa: F811
    run, add = lab
    board = Board(run)
    ok = _idea(board, "ok")
    boom = _idea(board, "crashes")
    add("base", {"acc": 0.8, "cost": 5.0})
    add("fine", {"acc": 0.8, "cost": 5.0}, parent="base", idea=ok)
    add("broken", {"acc": 0.8, "cost": 5.0, "crash": True}, parent="base", idea=boom)
    real = outcomes(board)
    assert real[boom]["failed"] and real[ok]["gain"] == 0.0
    assert sync(board)[0]["winner"] == ok


def test_search_cards_and_digest(tmp_path):
    board = Board(tmp_path)
    board.add_card(title="Co-Scientist", source="arXiv:2502.18864", technique="Elo tournament of hypotheses")
    board.add_card(title="mammoth-1", source="baseten", technique="6M synthetic decision examples")
    assert [c["id"] for c in search_cards(board, "synthetic")] == ["L002"]
    assert len(search_cards(board, "")) == 2
    a, b = _idea(board, "a"), _idea(board, "b")
    board.add_review(a, kind="full", verdict="revise", critique="ignores licence of the teacher")
    board.add_match(a, b, winner=b, format="debate", rationale="b is testable in one run")
    board.add_metareview("Reviews keep missing licence checks.")
    from meta_research.ideas import digest

    text = digest(board)
    assert "licence of the teacher" in text and "testable in one run" in text and "Previous meta-review" in text


def test_cli_round_trip(tmp_path, capsys):
    run = str(tmp_path)
    spec = tmp_path / "idea.json"
    spec.write_text(json.dumps({"title": "program-labelled SATA rows", "statement": "select-all questions",
                                "strategy": "literature", "tags": "data,sata"}), encoding="utf-8")
    assert cli_main(["ideas", "card", "--title", "t", "--technique", "x", "--source", "s", "--run-dir", run]) == 0
    assert cli_main(["ideas", "add", "--file", str(spec), "--cites", "L001", "--run-dir", run]) == 0
    assert cli_main(["ideas", "add", "--title", "program-labelled SATA rows", "--statement", "select-all questions",
                     "--run-dir", run]) == 0
    assert "near-duplicate of i001" in capsys.readouterr().out
    assert cli_main(["ideas", "add", "--title", "evolved", "--statement", "s", "--origin", "evolution",
                     "--parents", "i001", "--strategy", "simplification", "--run-dir", run]) == 0
    for i in ("i001", "i002", "i003"):
        assert cli_main(["ideas", "review", i, "--kind", "initial", "--verdict", "pass",
                         "--scores", "novelty=3,feasibility=5", "--run-dir", run]) == 0
    capsys.readouterr()
    assert cli_main(["ideas", "schedule", "--json", "--run-dir", run]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan and all(m["a"] != m["b"] for m in plan)
    assert cli_main(["ideas", "match", "i001", "i003", "--winner", "i003", "--format", "debate",
                     "--rationale", "simpler to test", "--run-dir", run]) == 0
    assert cli_main(["ideas", "match", "i001", "i003", "--winner", "i009", "--run-dir", run]) == 2
    capsys.readouterr()
    assert cli_main(["ideas", "rank", "--json", "--run-dir", run]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "i003"
    assert cli_main(["ideas", "metareview", "--text", "check licences", "--run-dir", run]) == 0
    assert cli_main(["ideas", "stats", "--run-dir", run]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])["ideas"] == 3
    for action in (["proximity"], ["digest"], ["cards", "t"], ["sync"]):
        assert cli_main(["ideas", *action, "--run-dir", run]) == 0
    assert (tmp_path / "ideas" / "board.jsonl").is_file()


def test_directed_ideas_wait_for_evaluation_and_play_reality_matches(lab):  # noqa: F811
    run, add = lab
    board = Board(run)
    judged = _idea(board, "more same-skill data")
    board.add_review(judged, kind="initial", verdict="pass")
    directed = _idea(board, "drop the generator rows that teach the wrong label", origin="directed", strategy="audit")
    assert status(board, directed) == "directed"
    assert all(directed not in (m["a"], m["b"]) for m in schedule(board, n=4))  # never judged
    add("base", {"acc": 0.80, "cost": 5.0})
    add("data", {"acc": 0.82, "cost": 5.0}, parent="base", idea=judged)
    add("fix", {"acc": 0.86, "cost": 5.0}, parent="base", idea=directed)
    board = Board(run)
    played = sync(board)
    assert [(m["a"], m["b"], m["winner"]) for m in played] == [(judged, directed, directed)]
    assert elo(Board(run))[directed]["elo"] > INITIAL_ELO


def test_a_standin_scores_a_parent_whose_own_bundle_is_missing(lab, capsys):  # noqa: F811
    run, add = lab
    board = Board(run)
    i = _idea(board, "class balancing", origin="directed", strategy="user")
    add("trained_score", {"acc": 0.80, "cost": 5.0})  # the score-only run of "trained", whose bundle never landed
    add("balanced", {"acc": 0.84, "cost": 5.0}, parent="trained", idea=i)
    assert outcomes(Board(run))[i]["gain"] is None
    scored = max(p.name for p in (run / "experience").glob("*_trained_score"))
    assert cli_main(["ideas", "standin", "trained", scored, "--run-dir", str(run)]) == 0
    out = outcomes(Board(run))[i]
    assert out["gain"] == pytest.approx(0.05) and out["parent"].endswith("_trained_score")


def _gated_lab(lab, minutes):  # noqa: F811
    run, _ = lab
    with open(run / "prepare.py", "a", encoding="utf-8") as fh:
        fh.write(f"\nEVAL_MINUTES = {minutes}\n")
    (run / "designs" / "cand.py").write_text(
        "from meta_research import DesignSpec\n\n\ndef build():\n"
        "    return DesignSpec(params={'acc': 0.81, 'cost': 5.0})\n", encoding="utf-8")
    return run


def _hyp(run, **kw):
    path = run / "hyp.json"
    path.write_text(json.dumps({"axis": "test", "change": "c", "reasoning": "why", **kw}), encoding="utf-8")
    return str(path)


def test_expensive_evaluations_must_name_a_board_idea(lab, capsys):  # noqa: F811
    run = _gated_lab(lab, 120)
    assert cli_main(["eval", "cand", "--hypothesis", _hyp(run), "--run-dir", str(run)]) == 2
    assert "EVAL_MINUTES" in capsys.readouterr().err
    assert cli_main(["eval", "cand", "--hypothesis", _hyp(run, idea="i007"), "--run-dir", str(run)]) == 2
    assert "not on ideas/board.jsonl" in capsys.readouterr().err
    i = Board(run).add_idea(title="directed fix", statement="s", origin="directed", strategy="user")["id"]
    assert cli_main(["eval", "cand", "--hypothesis", _hyp(run, idea=i), "--run-dir", str(run)]) == 0
    assert cli_main(["eval", "cand", "--hypothesis", _hyp(run), "--no-idea", "rescore only",
                     "--run-dir", str(run)]) == 0
    bundles = sorted((run / "experience").glob("*_cand/hypothesis.md"))
    assert f"idea: {i}" in bundles[0].read_text() and "(no idea: rescore only)" in bundles[-1].read_text()


def test_cheap_evaluations_need_no_idea(lab):  # noqa: F811
    run = _gated_lab(lab, 5)
    assert cli_main(["eval", "cand", "--hypothesis", _hyp(run), "--run-dir", str(run)]) == 0
