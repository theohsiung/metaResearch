"""Experience recall — find earlier candidates that tried a similar idea (MLEvolve, arXiv:2606.06473).

MLEvolve's retrospective memory (retrieving past plans, outcomes and metrics before
proposing) was one of its largest ablations. This is the dependency-free version for
the ledger: BM25 over each bundle's hypothesis (name, axis, operator, change and the
full reasoning prose), returning the bundle with its status, scores and the verdicts
of its structured predictions, so a proposer can check whether an idea was already
tried, and how it went, before spending an evaluation on it.

It is a search index over the bundles, like ``kg.json``: read the bundles it points to.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .kg import EXPERIENCE_DIRNAME, HYPOTHESIS_FILENAME, KG_FILENAME, _parse_bundles, _read_hypothesis

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)  # split on underscores too: candidate names are snake_case


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def _documents(run_dir: Path) -> list[dict[str, Any]]:
    experience = run_dir / EXPERIENCE_DIRNAME
    if not experience.is_dir():
        return []
    rows = _parse_bundles(experience, [])
    verdicts: dict[str, dict[str, str]] = {}
    kg_path = run_dir / KG_FILENAME
    if kg_path.is_file():
        for edge in json.loads(kg_path.read_text(encoding="utf-8")).get("edges", []):
            if edge.get("prediction"):
                verdicts[edge["dst"]] = {k: v.get("verdict", "") for k, v in edge["prediction"].items()}
    docs = []
    for row in rows:
        front, prose = _read_hypothesis(experience / row["id"] / HYPOTHESIS_FILENAME)
        text = " ".join(str(x) for x in (row["name"], row["axis"], row["operator"], front.get("change", ""), prose))
        docs.append({"id": row["id"], "status": row["status"], "scores": row["scores"],
                     "change": str(front.get("change") or ""), "verdicts": verdicts.get(row["id"], {}),
                     "tokens": tokenize(text)})
    return docs


def bm25(documents: list[list[str]], terms: list[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """BM25 score of each tokenized document for the query ``terms`` (0.0 when no term occurs)."""
    if not documents or not terms:
        return [0.0] * len(documents)
    avg_len = sum(len(d) for d in documents) / len(documents) or 1.0
    df = Counter(t for d in documents for t in set(d))
    n = len(documents)
    scores = []
    for tokens in documents:
        tf = Counter(tokens)
        score = 0.0
        for t in set(terms):
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(tokens) / avg_len))
        scores.append(score)
    return scores


def recall(run_dir: Path | str, query: str, *, k: int = 5, k1: float = 1.5, b: float = 0.75) -> list[dict[str, Any]]:
    """Top-``k`` bundles for ``query`` by BM25, best first; bundles with no query term are left out."""
    docs = _documents(Path(run_dir))
    scores = bm25([d["tokens"] for d in docs], tokenize(query), k1=k1, b=b)
    hits = [{**{key: d[key] for key in ("id", "status", "scores", "change", "verdicts")}, "score": round(score, 4)}
            for d, score in zip(docs, scores) if score > 0]
    hits.sort(key=lambda h: (-h["score"], h["id"]))
    return hits[:k]


__all__ = ["bm25", "recall", "tokenize"]
