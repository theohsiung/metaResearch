"""Idea board — Co-Scientist style hypothesis generation, review and tournament ranking.

Co-Scientist (Gottweis et al., arXiv:2502.18864) proposes research hypotheses with a team of
agents: *Generation* (literature exploration, simulated debates), *Reflection* (initial, full,
deep-verification, observation and simulation reviews), *Ranking* (an Elo tournament of pairwise
comparisons, multi-turn debates for the top hypotheses and single-turn ones below, matches
preferring similar, newer and top-ranked hypotheses), *Proximity* (a similarity graph for
clustering and de-duplication), *Evolution* (new hypotheses from the top ones: grounding,
feasibility, inspiration, combination, simplification, out-of-box; never edits an existing one)
and *Meta-review* (recurring critiques fed back into every agent's next prompt).

Here the agent in the session plays those roles; this module is the deterministic half, as the
rest of the CLI is: an append-only event log (``ideas/board.jsonl``) of literature cards, ideas,
reviews, matches, meta-reviews and links to evaluated bundles, replayed into Elo ratings, a
match schedule, a proximity graph and statistics. It adds one thing Co-Scientist does not have:
ideas that were evaluated have a *realized* outcome (gain over their parent in the ledger), so
``sync`` plays reality matches between tested ideas and ``stats`` reports how often the judged
matches agreed with reality, i.e. how far to trust the tournament before spending an evaluation.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .recall import bm25, tokenize

BOARD_DIRNAME = "ideas"
BOARD_FILENAME = "board.jsonl"

#: Co-Scientist starts every new hypothesis at Elo 1200.
INITIAL_ELO = 1200.0
#: Elo step for a judged match, and for a reality match (a realized outcome is stronger evidence).
K_JUDGED = 32.0
K_REALITY = 64.0

REVIEW_KINDS = ("initial", "full", "deep", "observation", "simulation", "tournament")
VERDICTS = ("pass", "revise", "fail")
ORIGINS = ("generation", "evolution")
#: Generation and Evolution strategies named in the paper (free text is allowed too).
STRATEGIES = ("literature", "debate", "assumptions", "expansion",
              "grounding", "feasibility", "inspiration", "combination", "simplification", "out-of-box")
JUDGED_FORMATS = ("single", "debate")
FAILED_STATUSES = ("infeasible", "crash")

_CARD_ID = re.compile(r"^L\d{3,}$")


class BoardError(ValueError):
    """An event that would make the board inconsistent (unknown id, bad verdict, ...)."""


# --------------------------------------------------------------------------- #
# the event log
# --------------------------------------------------------------------------- #
class Board:
    """Append-only idea board under ``<run_dir>/ideas/board.jsonl``."""

    def __init__(self, run_dir: Path | str):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / BOARD_DIRNAME / BOARD_FILENAME
        self.events: list[dict[str, Any]] = []
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.events.append(json.loads(line))

    # -- views ---------------------------------------------------------------
    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == kind]

    @property
    def cards(self) -> dict[str, dict[str, Any]]:
        return {e["id"]: e for e in self.of("card")}

    @property
    def ideas(self) -> dict[str, dict[str, Any]]:
        return {e["id"]: e for e in self.of("idea")}

    def reviews(self, idea: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.of("review") if idea is None or e["idea"] == idea]

    def matches(self) -> list[dict[str, Any]]:
        return self.of("match")

    def links(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in self.of("link"):
            out.setdefault(e["idea"], []).append(e["bundle"])
        return out

    # -- writers -------------------------------------------------------------
    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        event = {**event, "seq": len(self.events) + 1,
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
        self.events.append(event)
        return event

    def _next_id(self, kind: str, prefix: str) -> str:
        return f"{prefix}{len(self.of(kind)) + 1:03d}"

    def add_card(self, *, title: str, source: str, technique: str, claim: str = "",
                 conditions: str = "", cost: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        """A literature card: one technique from one paper / model card / report (Generation's grounding)."""
        if not title.strip() or not technique.strip():
            raise BoardError("a card needs a title and the technique it describes")
        return self._append({"type": "card", "id": self._next_id("card", "L"), "title": title.strip(),
                             "source": source.strip(), "technique": technique.strip(), "claim": claim.strip(),
                             "conditions": conditions.strip(), "cost": cost.strip(), "tags": list(tags or [])})

    def add_idea(self, *, title: str, statement: str, change: str = "", origin: str = "generation",
                 strategy: str = "", parents: list[str] | None = None, cites: list[str] | None = None,
                 tags: list[str] | None = None) -> dict[str, Any]:
        """A new hypothesis. Evolution adds a new idea with ``parents``; it never edits one."""
        if not title.strip() or not statement.strip():
            raise BoardError("an idea needs a title and a statement")
        if origin not in ORIGINS:
            raise BoardError(f"origin must be one of {ORIGINS}, got {origin!r}")
        parents = list(parents or [])
        unknown = [p for p in parents if p not in self.ideas]
        if unknown:
            raise BoardError(f"unknown parent idea(s): {', '.join(unknown)}")
        if origin == "evolution" and not parents:
            raise BoardError("an evolved idea names the idea(s) it evolves from (parents)")
        cites = list(cites or [])
        missing = [c for c in cites if _CARD_ID.match(c) and c not in self.cards]
        if missing:
            raise BoardError(f"unknown literature card(s): {', '.join(missing)}")
        return self._append({"type": "idea", "id": self._next_id("idea", "i"), "title": title.strip(),
                             "statement": statement.strip(), "change": change.strip(), "origin": origin,
                             "strategy": strategy.strip(), "parents": parents, "cites": cites,
                             "tags": list(tags or [])})

    def add_review(self, idea: str, *, kind: str, verdict: str, scores: dict[str, float] | None = None,
                   critique: str = "") -> dict[str, Any]:
        self._require(idea)
        if kind not in REVIEW_KINDS:
            raise BoardError(f"review kind must be one of {REVIEW_KINDS}, got {kind!r}")
        if verdict not in VERDICTS:
            raise BoardError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
        return self._append({"type": "review", "idea": idea, "kind": kind, "verdict": verdict,
                             "scores": {k: float(v) for k, v in (scores or {}).items()},
                             "critique": critique.strip()})

    def add_match(self, a: str, b: str, *, winner: str, format: str = "single",  # noqa: A002 -- the paper's word
                  rationale: str = "") -> dict[str, Any]:
        """A judged pairwise comparison (``single`` turn or multi-turn ``debate``)."""
        if format not in JUDGED_FORMATS:
            raise BoardError(f"format must be one of {JUDGED_FORMATS} (reality matches come from sync)")
        return self._match(a, b, winner=winner, format=format, rationale=rationale)

    def _match(self, a: str, b: str, *, winner: str, format: str, rationale: str) -> dict[str, Any]:  # noqa: A002
        self._require(a)
        self._require(b)
        if a == b:
            raise BoardError("a match needs two different ideas")
        if winner not in (a, b):
            raise BoardError(f"winner must be {a} or {b}, got {winner!r}")
        return self._append({"type": "match", "a": a, "b": b, "winner": winner, "format": format,
                             "rationale": rationale.strip()})

    def add_metareview(self, text: str) -> dict[str, Any]:
        if not text.strip():
            raise BoardError("empty meta-review")
        return self._append({"type": "metareview", "text": text.strip()})

    def link(self, idea: str, bundle: str) -> dict[str, Any]:
        """Record that ``bundle`` (an ``experience/`` id) tested ``idea`` (for runs made without ``idea``)."""
        self._require(idea)
        if bundle in self.links().get(idea, []):
            raise BoardError(f"{bundle} is already linked to {idea}")
        return self._append({"type": "link", "idea": idea, "bundle": bundle})

    def _require(self, idea: str) -> None:
        if idea not in self.ideas:
            raise BoardError(f"unknown idea {idea!r}")


# --------------------------------------------------------------------------- #
# derived state
# --------------------------------------------------------------------------- #
def expected_score(rating: float, other: float) -> float:
    return 1.0 / (1.0 + 10 ** ((other - rating) / 400.0))


def elo(board: Board) -> dict[str, dict[str, Any]]:
    """Replay every match in order: rating, wins, losses and judged/reality match counts per idea."""
    table = {i: {"elo": INITIAL_ELO, "wins": 0, "losses": 0, "matches": 0} for i in board.ideas}
    for m in board.matches():
        a, b = m["a"], m["b"]
        k = K_REALITY if m["format"] == "reality" else K_JUDGED
        ea = expected_score(table[a]["elo"], table[b]["elo"])
        sa = 1.0 if m["winner"] == a else 0.0
        table[a]["elo"] += k * (sa - ea)
        table[b]["elo"] -= k * (sa - ea)
        loser = b if m["winner"] == a else a
        table[m["winner"]]["wins"] += 1
        table[loser]["losses"] += 1
        table[a]["matches"] += 1
        table[b]["matches"] += 1
    return table


def status(board: Board, idea: str, tested: set[str] | None = None) -> str:
    """``tested`` once evaluated, else the latest review verdict (``new`` before any review)."""
    if tested is not None and idea in tested:
        return "tested"
    reviews = board.reviews(idea)
    if not reviews:
        return "new"
    return {"pass": "active", "revise": "revise", "fail": "rejected"}[reviews[-1]["verdict"]]


def _idea_text(idea: dict[str, Any]) -> str:
    return " ".join([idea["title"], idea["statement"], idea.get("change", ""), " ".join(idea.get("tags", []))])


def similarity(board: Board) -> dict[tuple[str, str], float]:
    """TF-IDF cosine between every pair of ideas (keys sorted, ``(a, b)`` with ``a < b``)."""
    ideas = board.ideas
    tokens = {i: Counter(tokenize(_idea_text(idea))) for i, idea in ideas.items()}
    n = len(tokens)
    df = Counter(t for c in tokens.values() for t in c)
    vectors = {}
    for i, c in tokens.items():
        v = {t: tf * math.log(1 + n / df[t]) for t, tf in c.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vectors[i] = {t: x / norm for t, x in v.items()}
    ids = sorted(vectors)
    return {(a, b): sum(x * vectors[b].get(t, 0.0) for t, x in vectors[a].items())
            for k, a in enumerate(ids) for b in ids[k + 1:]}


def proximity(board: Board, *, threshold: float = 0.3, duplicate: float = 0.75) -> dict[str, Any]:
    """Proximity graph: edges at or above ``threshold``, clusters (connected components), near-duplicates."""
    sims = similarity(board)
    parent = {i: i for i in board.ideas}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = []
    for (a, b), s in sorted(sims.items()):
        if s >= threshold:
            edges.append({"a": a, "b": b, "similarity": round(s, 4)})
            parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for i in sorted(board.ideas):
        groups.setdefault(find(i), []).append(i)
    clusters = sorted(groups.values(), key=lambda g: (-len(g), g[0]))
    return {"edges": edges, "clusters": clusters,
            "duplicates": [e for e in edges if e["similarity"] >= duplicate]}


def schedule(board: Board, *, n: int = 4, top_fraction: float = 0.25,
             tested: set[str] | None = None) -> list[dict[str, Any]]:
    """The next ``n`` tournament matches, Co-Scientist's priorities.

    Every focal idea is an untested ``active`` one, newest and least-played first, then the
    top-rated. Its opponent is an eligible idea (active or tested, the latter acting as anchors
    whose rating reality has moved) it has met least often, most similar to it, then closest in
    rating. Matches between two top-ranked ideas are multi-turn ``debate`` s, the rest ``single``.
    """
    tested = tested or set()
    ratings = elo(board)
    eligible = [i for i in board.ideas if status(board, i, tested) in ("active", "tested")]
    if len(eligible) < 2:
        return []
    ranked = sorted(eligible, key=lambda i: (-ratings[i]["elo"], i))
    top = set(ranked[:max(2, math.ceil(top_fraction * len(ranked)))])
    met = Counter(tuple(sorted((m["a"], m["b"]))) for m in board.matches())
    sims = similarity(board)
    focal = [i for i in eligible if i not in tested]
    focal.sort(key=lambda i: (ratings[i]["matches"], -int(i[1:]), ranked.index(i)))
    out: list[dict[str, Any]] = []
    planned: Counter = Counter()
    for i in focal * max(1, math.ceil(n / max(1, len(focal)))):
        if len(out) >= n:
            break
        options = [j for j in eligible if j != i]
        if not options:
            break

        def key(j: str, i: str = i) -> tuple:
            pair = tuple(sorted((i, j)))
            return (met[pair] + planned[pair], -sims.get(pair, 0.0), abs(ratings[i]["elo"] - ratings[j]["elo"]), j)

        j = min(options, key=key)
        pair = tuple(sorted((i, j)))
        planned[pair] += 1
        a, b = (i, j) if len(out) % 2 == 0 else (j, i)  # alternate sides: judges favour a position
        out.append({"a": a, "b": b, "format": "debate" if {i, j} <= top else "single",
                    "similarity": round(sims.get(pair, 0.0), 4), "met": met[pair]})
    return out


# --------------------------------------------------------------------------- #
# reality: outcomes of evaluated ideas
# --------------------------------------------------------------------------- #
def outcomes(board: Board, *, weights: dict[str, float] | None = None,
             scores: dict[str, dict[str, float]] | None = None) -> dict[str, dict[str, Any]]:
    """Realized outcome of every idea with an evaluated bundle.

    A bundle tests an idea when its ``hypothesis.md`` front-matter names it (``idea`` in hyp.json)
    or a ``link`` event says so. Its gain is the weighted relative improvement over its parent
    (``weights`` maps objective -> weight, default the first objective in ``frontier.json``),
    signed so that positive is better. An infeasible or crashed bundle (including an early-stopped
    run) failed. An idea keeps its best bundle.

    ``scores`` overrides the ledger's scores per bundle id (``{bundle: {name: value}}``), for a run
    whose metric changed after some bundles were recorded: the ledger stays append-only and the
    rescored values live beside it. A name only in ``scores`` is maximized; with ``scores`` and no
    ``weights``, the name its entries share is used.
    """
    from .suggest import _parent_of, load_rows, objectives_from_frontier

    objectives = {o.name: o for o in objectives_from_frontier(board.run_dir)}
    if not weights and scores:
        shared = set.intersection(*(set(v) for v in scores.values())) if scores else set()
        if len(shared) != 1:
            raise BoardError("with --scores, name the objective(s) to use with --weights")
        weights = {shared.pop(): 1.0}
    if not weights:
        weights = {next(iter(objectives)): 1.0} if objectives else {}
    rows = load_rows(board.run_dir)
    if scores:
        rows = [{**r, "scores": {**r["scores"], **scores.get(r["id"], {})}} for r in rows]
    by_id = {r["id"]: r for r in rows}
    tested: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("idea") in board.ideas:
            tested.setdefault(r["idea"], []).append(r)
    for idea, bundles in board.links().items():
        for b in bundles:
            if b in by_id and by_id[b] not in tested.get(idea, []):
                tested.setdefault(idea, []).append(by_id[b])
    out = {}
    for idea, bundles in tested.items():
        results = [_outcome(r, _parent_of(r, rows), weights, objectives) for r in bundles]
        scored = [x for x in results if x["gain"] is not None]
        failed = [x for x in results if x["failed"]]
        best = max(scored, key=lambda x: x["gain"]) if scored else (failed[0] if failed else results[0])
        out[idea] = {**best, "bundles": [r["id"] for r in bundles]}
    return out


def _outcome(row: dict[str, Any], parent: dict[str, Any] | None, weights: dict[str, float],
             objectives: dict[str, Any]) -> dict[str, Any]:
    if row["status"] in FAILED_STATUSES:
        return {"bundle": row["id"], "gain": None, "failed": True}
    if parent is None or parent["status"] in FAILED_STATUSES:
        return {"bundle": row["id"], "gain": None, "failed": False}
    gain = 0.0
    for name, w in weights.items():
        child, base = row["scores"].get(name), parent["scores"].get(name)
        if child is None or base is None:
            return {"bundle": row["id"], "gain": None, "failed": False}
        rel = (child - base) / abs(base) if base else child - base
        sign = -1.0 if objectives.get(name) is not None and objectives[name].direction == "min" else 1.0
        gain += w * sign * rel
    return {"bundle": row["id"], "gain": gain, "failed": False, "parent": parent["id"]}


def _realized_order(x: dict[str, Any], y: dict[str, Any], deadband: float) -> int:
    """+1 if ``x`` did better than ``y``, -1 if worse, 0 if a tie or not comparable."""
    if x["failed"] and y["failed"]:
        return 0
    if x["failed"] or y["failed"]:
        return -1 if x["failed"] else 1
    if x["gain"] is None or y["gain"] is None or abs(x["gain"] - y["gain"]) <= deadband:
        return 0
    return 1 if x["gain"] > y["gain"] else -1


def sync(board: Board, *, weights: dict[str, float] | None = None, deadband: float = 0.002,
         scores: dict[str, dict[str, float]] | None = None) -> list[dict[str, Any]]:
    """Play one reality match per pair of tested ideas whose outcomes differ by more than ``deadband``.

    Idempotent: a pair that already has a reality match is not replayed (its first outcome stands).
    """
    real = outcomes(board, weights=weights, scores=scores)
    played = {tuple(sorted((m["a"], m["b"]))) for m in board.matches() if m["format"] == "reality"}
    added = []
    ids = sorted(real)
    for k, a in enumerate(ids):
        for b in ids[k + 1:]:
            if (a, b) in played:
                continue
            order = _realized_order(real[a], real[b], deadband)
            if order == 0:
                continue
            winner, loser = (a, b) if order > 0 else (b, a)
            added.append(board._match(a, b, winner=winner, format="reality",
                                      rationale=f"realized: {_describe(real[winner])} vs {_describe(real[loser])}"))
    return added


def _describe(outcome: dict[str, Any]) -> str:
    if outcome["failed"]:
        return f"{outcome['bundle']} failed"
    return f"{outcome['bundle']} gain {outcome['gain']:+.4f}"


def concordance(board: Board, *, weights: dict[str, float] | None = None, deadband: float = 0.002,
                scores: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """How often a judged match picked the idea that later did better (both tested, outcomes apart)."""
    real = outcomes(board, weights=weights, scores=scores)
    per_format: dict[str, list[int]] = {}
    for m in board.matches():
        if m["format"] == "reality" or m["a"] not in real or m["b"] not in real:
            continue
        order = _realized_order(real[m["a"]], real[m["b"]], deadband)
        if order == 0:
            continue
        better = m["a"] if order > 0 else m["b"]
        per_format.setdefault(m["format"], []).append(int(m["winner"] == better))
    every = [x for xs in per_format.values() for x in xs]
    return {"agree": sum(every), "n": len(every),
            "rate": round(sum(every) / len(every), 4) if every else None,
            "by_format": {f: {"agree": sum(xs), "n": len(xs)} for f, xs in sorted(per_format.items())}}


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def rank(board: Board, *, weights: dict[str, float] | None = None,
         scores: dict[str, dict[str, float]] | None = None) -> list[dict[str, Any]]:
    """Every idea with rating, record, status and realized gain, best rating first."""
    real = outcomes(board, weights=weights, scores=scores)
    ratings = elo(board)
    rows = []
    for i, idea in board.ideas.items():
        r = ratings[i]
        rows.append({"id": i, "title": idea["title"], "elo": round(r["elo"], 1), "wins": r["wins"],
                     "losses": r["losses"], "status": status(board, i, set(real)), "origin": idea["origin"],
                     "strategy": idea["strategy"], "parents": idea["parents"], "cites": idea["cites"],
                     "gain": None if i not in real or real[i]["gain"] is None else round(real[i]["gain"], 4),
                     "failed": bool(real.get(i, {}).get("failed"))})
    rows.sort(key=lambda x: (-x["elo"], x["id"]))
    return rows


def stats(board: Board, *, weights: dict[str, float] | None = None, deadband: float = 0.002,
          scores: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """The Supervisor's view: what each generation method yields, in rating and in reality."""
    rows = rank(board, weights=weights, scores=scores)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["origin"], []).append(row)
        if row["strategy"]:
            groups.setdefault(f"{row['origin']}:{row['strategy']}", []).append(row)
    by_method = {}
    for name, members in sorted(groups.items()):
        tested = [m for m in members if m["status"] == "tested"]
        hits = [m for m in tested if m["gain"] is not None and m["gain"] > deadband]
        by_method[name] = {"ideas": len(members), "mean_elo": round(sum(m["elo"] for m in members) / len(members), 1),
                           "tested": len(tested), "improved": len(hits)}
    return {"ideas": len(rows), "cards": len(board.cards),
            "status": dict(sorted(Counter(r["status"] for r in rows).items())),
            "matches": dict(sorted(Counter(m["format"] for m in board.matches()).items())),
            "reviews": dict(sorted(Counter(f"{r['kind']}:{r['verdict']}" for r in board.reviews()).items())),
            "by_method": by_method,
            "concordance": concordance(board, weights=weights, deadband=deadband, scores=scores)}


def search_cards(board: Board, query: str, *, k: int = 10) -> list[dict[str, Any]]:
    """Literature cards for ``query`` (BM25), or every card when the query is empty."""
    cards = list(board.cards.values())
    if not query.strip():
        return cards
    docs = [tokenize(" ".join([c["title"], c["technique"], c["claim"], c["conditions"], " ".join(c["tags"])]))
            for c in cards]
    scored = [(s, c) for s, c in zip(bm25(docs, tokenize(query)), cards) if s > 0]
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    return [c for _, c in scored[:k]]


def digest(board: Board, *, last: int = 40) -> str:
    """Material for the Meta-review: the latest reviews and match rationales, and the previous meta-review."""
    ideas = board.ideas
    lines = []
    metas = board.of("metareview")
    if metas:
        lines += ["## Previous meta-review", "", metas[-1]["text"], ""]
    reviews = [r for r in board.reviews() if r["critique"]][-last:]
    if reviews:
        lines += ["## Reviews", ""]
        for r in reviews:
            lines.append(f"- {r['idea']} ({ideas[r['idea']]['title']}) {r['kind']} -> {r['verdict']}: {r['critique']}")
        lines.append("")
    matches = [m for m in board.matches() if m["rationale"]][-last:]
    if matches:
        lines += ["## Matches", ""]
        for m in matches:
            lines.append(f"- {m['a']} vs {m['b']} [{m['format']}] -> {m['winner']}: {m['rationale']}")
        lines.append("")
    return "\n".join(lines) if lines else "(nothing reviewed or matched yet)"


__all__ = [
    "Board", "BoardError", "INITIAL_ELO", "K_JUDGED", "K_REALITY", "REVIEW_KINDS", "STRATEGIES",
    "concordance", "digest", "elo", "expected_score", "outcomes", "proximity", "rank", "schedule",
    "search_cards", "similarity", "stats", "status", "sync",
]
