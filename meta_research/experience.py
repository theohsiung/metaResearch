"""Experience store + git append-only ledger.

This module implements the *store* half of the deterministic plumbing the
research agent calls (the other halves being :mod:`meta_research.frontier` and
:mod:`meta_research.runner`). It is responsible for writing the full
**experience bundle** for every evaluated candidate -- kept, dominated,
infeasible, or crashed -- and for the **append-only git ledger** that versions
those bundles.

Design contract: ``DESIGN.md`` sections 6.3 (store responsibilities), 7.1
(bundle layout), 7.2 (``hypothesis.md`` front-matter), 7.4 (frontier) and the
commit-message format pinned in 6.3.

Append-only discipline (fusion rule 1.1.2): there is **no** history-rewriting or
discarding operation anywhere in this file -- no resetting, no
``git checkout -- <path>``, no ``git revert``. Discarding would delete exactly
the diagnostic traces Meta-Harness depends on. The only git mutations are
``checkout -b`` / ``checkout`` (branch movement, never resetting working state)
and ``add -A`` + ``commit``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meta_research.interfaces import DesignSpec, EvalResult, Objective

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

#: Directory (relative to ``run_dir``) that holds every experience bundle.
EXPERIENCE_DIRNAME = "experience"
#: Flat tab-separated ledger of every evaluated candidate.
RESULTS_FILENAME = "results.tsv"
#: Current Pareto frontier (schema 7.4).
FRONTIER_FILENAME = "frontier.json"

#: File names inside a single bundle (schema 7.1).
DESIGN_SRC_FILENAME = "design.py"
DESIGN_SPEC_FILENAME = "design_spec.json"
HYPOTHESIS_FILENAME = "hypothesis.md"
RESULT_FILENAME = "result.json"
TRACE_DIRNAME = "trace"
BREAKDOWN_FILENAME = "breakdown.json"

#: Branch namespace for the append-only ledger.
BRANCH_PREFIX = "meta-research/"

#: Status vocabulary (mirrors ``ResultsLog`` in logfmt.py / schema 7.2).
STATUS_FRONTIER = "frontier"
STATUS_DOMINATED = "dominated"
STATUS_INFEASIBLE = "infeasible"
STATUS_CRASH = "crash"
_VALID_STATUSES = (STATUS_FRONTIER, STATUS_DOMINATED, STATUS_INFEASIBLE, STATUS_CRASH)

#: Matches a bundle directory name ``<iter:03d>_<name>`` and captures both parts.
_BUNDLE_RE = re.compile(r"^(?P<iter>\d+)_(?P<name>.+)$")

#: Used to extract the YAML-ish front-matter block from ``hypothesis.md``.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n?(?P<prose>.*)$", re.DOTALL)


# ----------------------------------------------------------------------------
# Bundle record (return value of record(); parsed rows for history())
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleRef:
    """A lightweight, immutable reference to a written bundle."""

    iteration: int
    name: str
    path: Path


# ----------------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------------


class Experience:
    """Experience-bundle store and git append-only ledger rooted at ``run_dir``.

    Args:
        run_dir: The experiment run directory. ``experience/``, ``results.tsv``
            and ``frontier.json`` live directly underneath it, and every git
            command runs with ``cwd=run_dir``.
        objectives: The ordered objective list (drives commit-message scoring
            and is kept for callers that want it alongside the store).
    """

    def __init__(self, run_dir: Path | str, objectives: list[Objective]) -> None:
        self.run_dir = Path(run_dir)
        self.objectives = list(objectives)

    # -- paths ---------------------------------------------------------------

    @property
    def experience_dir(self) -> Path:
        return self.run_dir / EXPERIENCE_DIRNAME

    @property
    def results_path(self) -> Path:
        return self.run_dir / RESULTS_FILENAME

    @property
    def frontier_path(self) -> Path:
        return self.run_dir / FRONTIER_FILENAME

    def bundle_dir(self, iteration: int, name: str) -> Path:
        """Return ``experience/<iteration:03d>_<name>/`` (not created here)."""
        return self.experience_dir / f"{int(iteration):03d}_{_safe_name(name)}"

    # -- writing a bundle ----------------------------------------------------

    def record(
        self,
        iteration: int,
        name: str,
        design_src_path: Path | str,
        design: DesignSpec,
        result: EvalResult,
        hypothesis: dict[str, Any],
    ) -> Path:
        """Write the full experience bundle for one evaluated candidate.

        Writes, per schema 7.1::

            <iter:03d>_<name>/
              design.py          # verbatim copy of design_src_path
              design_spec.json   # DesignSpec.to_json()
              hypothesis.md       # YAML front-matter (7.2) + prose reasoning
              result.json        # EvalResult.to_json() + a "status" field
              trace/
                breakdown.json   # = result.metadata
                <artifacts...>   # each result.artifacts file copied in

        The candidate's ``status`` is derived from the result (``crash`` when an
        error is present, ``infeasible`` when ``feasible`` is False) and
        otherwise taken from ``hypothesis["status"]`` if the caller (the runner,
        after classifying against the frontier) supplied it -- defaulting to
        ``dominated``. This keeps the bundle internally consistent regardless of
        ordering.

        Args:
            iteration: The bundle iteration index (see :meth:`next_iteration`).
            name: Candidate name (used for the directory and front-matter).
            design_src_path: Path to the ``designs/<name>.py`` module to copy.
            design: The built :class:`DesignSpec`.
            result: The :class:`EvalResult` from the evaluator.
            hypothesis: The agent's reasoning dict (schema 7.3): keys ``axis``,
                ``parent``, ``change`` (the one-line "what was changed"),
                ``expected``, ``reasoning`` (and an optional ``status`` folded
                in by the runner).

        Returns:
            The bundle directory :class:`~pathlib.Path`.
        """
        hyp = dict(hypothesis or {})
        status = self._derive_status(result, hyp.get("status"))

        bundle = self.bundle_dir(iteration, name)
        trace = bundle / TRACE_DIRNAME
        try:
            bundle.mkdir(parents=True, exist_ok=True)
            trace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("failed to create bundle dir %s: %s", bundle, exc)
            raise

        # 1) verbatim copy of the candidate source.
        self._copy_design_source(design_src_path, bundle / DESIGN_SRC_FILENAME)

        # 2) design_spec.json
        _write_json(bundle / DESIGN_SPEC_FILENAME, design.to_json())

        # 3) hypothesis.md (front-matter + prose) -- the reasoning trace.
        (bundle / HYPOTHESIS_FILENAME).write_text(
            _render_hypothesis_md(name=name, iteration=iteration, status=status, hypothesis=hyp),
            encoding="utf-8",
        )

        # 4) result.json (EvalResult.to_json + status)
        result_doc = result.to_json()
        result_doc["status"] = status
        _write_json(bundle / RESULT_FILENAME, result_doc)

        # 5) trace/breakdown.json = result.metadata
        _write_json(trace / BREAKDOWN_FILENAME, dict(result.metadata))

        # 6) copy each artifact into trace/ (evaluator wrote them under its
        #    out_dir; artifacts may be relative to that dir or absolute).
        self._copy_artifacts(result, bundle, trace)

        logger.info("recorded bundle %s [%s]", bundle.name, status)
        return bundle

    # -- reading the ledger back --------------------------------------------

    def history(self) -> list[dict[str, Any]]:
        """Parse every bundle into rows, sorted by ``(iteration, name)``.

        Each row has keys ``iteration``, ``name``, ``status``, ``scores``,
        ``metadata``, ``hypothesis`` (the prose), ``axis``, ``parent``.
        Malformed or partial bundles are skipped with a warning -- never raises.
        """
        rows: list[dict[str, Any]] = []
        if not self.experience_dir.is_dir():
            return rows

        for entry in sorted(self.experience_dir.iterdir()):
            if not entry.is_dir():
                continue
            match = _BUNDLE_RE.match(entry.name)
            if match is None:
                continue
            row = self._parse_bundle(entry, match)
            if row is not None:
                rows.append(row)

        rows.sort(key=lambda r: (r["iteration"], r["name"]))
        return rows

    def next_iteration(self) -> int:
        """Return ``max(existing bundle iteration) + 1`` (0 when none exist)."""
        highest = -1
        if self.experience_dir.is_dir():
            for entry in self.experience_dir.iterdir():
                if not entry.is_dir():
                    continue
                match = _BUNDLE_RE.match(entry.name)
                if match is None:
                    continue
                try:
                    highest = max(highest, int(match.group("iter")))
                except ValueError:  # pragma: no cover - guarded by the regex
                    continue
        return highest + 1

    # -- git helpers (append-only) ------------------------------------------

    def _verify_repo_root(self) -> None:
        """Refuse git mutations unless ``run_dir`` is its own repository root.

        ``git add -A`` stages the *whole* working tree (git >= 2.0) and
        ``git checkout -b`` switches the branch of the *whole* checkout. If
        ``run_dir`` were nested inside a larger host repository, a commit here
        would sweep in unrelated host files and branch-switch the host checkout.
        ``meta-research init`` gives each experiment its own repo; this guard
        enforces that precondition.
        """
        completed = self._git("rev-parse", "--show-toplevel")
        toplevel = completed.stdout.strip()
        if completed.returncode != 0 or not toplevel:
            raise RuntimeError(
                f"refusing git operation: {self.run_dir} is not inside a git repository; "
                "scaffold with `meta-research init` or `git init` the run dir first"
            )
        if Path(toplevel).resolve() != self.run_dir.resolve():
            raise RuntimeError(
                f"refusing git operation: run_dir {self.run_dir} is not the repository root "
                f"(the repository root is {toplevel}); committing from here would stage "
                "unrelated host files and switch the host branch — give the run dir its "
                "own repository (`meta-research init` does this)"
            )

    def ensure_branch(self, tag: str) -> str:
        """Ensure the run is on branch ``meta-research/<tag>``; never resets.

        Creates the branch with ``git checkout -b`` if absent, otherwise just
        checks it out. Returns the full branch name. Working-tree state is never
        discarded (no reset / no ``--force``). Refuses to run unless ``run_dir``
        is its own repository root (see :meth:`_verify_repo_root`).
        """
        self._verify_repo_root()
        branch = _branch_name(tag)
        existing = self._git("branch", "--list", branch).stdout.strip()
        if existing:
            self._git("checkout", branch, check=True)
        else:
            self._git("checkout", "-b", branch, check=True)
        return branch

    def commit(self, message: str) -> str:
        """Stage everything and commit. Append-only -- no reset, ever.

        Runs ``git add -A -- .`` then ``git commit -m <message>``. If there is
        nothing to commit, that is logged and the existing HEAD is returned
        rather than raising (re-recording an identical bundle is benign).
        Refuses to run unless ``run_dir`` is its own repository root (see
        :meth:`_verify_repo_root`); the ``.`` pathspec is defence in depth
        against staging anything outside ``run_dir``.

        Returns the resulting (or unchanged) HEAD short SHA, or ``""`` if it
        could not be resolved.
        """
        self._verify_repo_root()
        self._git("add", "-A", "--", ".", check=True)
        completed = self._git("commit", "-m", message)
        if completed.returncode != 0:
            out = (completed.stdout + completed.stderr).lower()
            if "nothing to commit" in out or "no changes added" in out:
                logger.info("commit skipped (nothing to commit): %s", message)
            else:
                logger.error(
                    "git commit failed (rc=%s): %s", completed.returncode, completed.stderr.strip()
                )
        return self._head_sha()

    def annotate_commit(self, bundle: Path | str, sha: str) -> None:
        """Write the ledger commit SHA into the bundle's ``result.json``.

        The SHA is the join key between a bundle and the full-run snapshot that
        contains it (autoresearch records the same key in its results.tsv). The
        annotation lands *after* the commit, so the git-tracked copy of
        ``result.json`` lags one commit behind the filesystem copy — the SHA
        names the commit that contains this bundle, which cannot contain
        itself. Best-effort: failures are logged, never raised.
        """
        if not sha:
            return
        path = Path(bundle) / RESULT_FILENAME
        doc = _read_json(path)
        if doc is None:
            logger.warning("cannot annotate commit sha: %s missing or invalid", path)
            return
        annotated = {**doc, "commit": sha}
        try:
            _write_json(path, annotated)
        except (OSError, TypeError, ValueError):
            logger.warning("failed to annotate commit sha into %s", path)

    def git_log_oneline(self) -> str:
        """Return ``git log --oneline`` (empty string on any failure)."""
        return self._git("log", "--oneline").stdout

    def git_show(self, ref: str) -> str:
        """Return ``git show <ref>`` (e.g. ``<sha>:experience/012_foo/design.py``)."""
        return self._git("show", ref).stdout

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _derive_status(self, result: EvalResult, hint: Any) -> str:
        """Resolve a candidate's status from its result and an optional hint."""
        if result.error is not None or not result.ok:
            return STATUS_CRASH
        if not result.feasible:
            return STATUS_INFEASIBLE
        if isinstance(hint, str) and hint in (STATUS_FRONTIER, STATUS_DOMINATED):
            return hint
        return STATUS_DOMINATED

    def _copy_design_source(self, src: Path | str, dst: Path) -> None:
        """Copy the candidate module verbatim; tolerate a missing source."""
        src_path = Path(src)
        try:
            if src_path.is_file():
                shutil.copyfile(src_path, dst)
            else:
                logger.warning("design source not found: %s", src_path)
                dst.write_text(
                    f"# source module not found at record time: {src_path}\n",
                    encoding="utf-8",
                )
        except OSError as exc:
            logger.error("failed to copy design source %s -> %s: %s", src_path, dst, exc)
            dst.write_text(f"# failed to copy source {src_path}: {exc}\n", encoding="utf-8")

    def _copy_artifacts(self, result: EvalResult, bundle: Path, trace: Path) -> None:
        """Copy every ``result.artifacts`` file into ``trace/`` by basename.

        Artifact paths may be absolute, relative to ``run_dir``, or relative to
        the bundle (the evaluator typically wrote into ``bundle/trace``). The
        first existing candidate wins; a copy onto itself is a no-op.
        """
        for key, raw in (result.artifacts or {}).items():
            source = self._resolve_artifact(raw, bundle, trace)
            if source is None:
                logger.warning("artifact %r not found (path=%s)", key, raw)
                continue
            dest = trace / source.name
            try:
                if source.resolve() == dest.resolve():
                    continue  # already in place
                shutil.copyfile(source, dest)
            except OSError as exc:
                logger.error("failed to copy artifact %r (%s): %s", key, source, exc)

    def _resolve_artifact(self, raw: str, bundle: Path, trace: Path) -> Path | None:
        """Find an artifact file given a possibly-relative path string."""
        if not raw:
            return None
        raw_path = Path(raw)
        candidates = (
            raw_path,
            trace / raw_path.name,
            bundle / raw_path,
            self.run_dir / raw_path,
        )
        for cand in candidates:
            try:
                if cand.is_file():
                    return cand
            except OSError:
                continue
        return None

    def _parse_bundle(self, bundle: Path, match: re.Match[str]) -> dict[str, Any] | None:
        """Parse a single bundle directory into a history row (None on failure)."""
        try:
            iteration = int(match.group("iter"))
        except ValueError:  # pragma: no cover - guarded by the regex
            return None
        name = match.group("name")

        result_doc = _read_json(bundle / RESULT_FILENAME)
        if result_doc is None:
            logger.warning("bundle %s missing/invalid %s; skipped", bundle.name, RESULT_FILENAME)
            return None

        scores = {
            k: float(v)
            for k, v in (result_doc.get("scores") or {}).items()
            if isinstance(v, (int, float))
        }
        metadata = dict(result_doc.get("metadata") or {})
        status = str(result_doc.get("status") or "")

        front, prose = _read_hypothesis(bundle / HYPOTHESIS_FILENAME)
        if not status:
            status = str(front.get("status") or "")

        return {
            "iteration": iteration,
            "name": str(front.get("name") or name),
            "status": status,
            "scores": scores,
            "metadata": metadata,
            "hypothesis": prose,
            "axis": str(front.get("axis") or ""),
            "parent": str(front.get("parent") or ""),
        }

    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """Run a git command with ``cwd=run_dir``; capture text output.

        Never raises for ordinary failures unless ``check`` is set; the few
        callers that require success (branch movement, ``add``) pass
        ``check=True``. No history-rewriting subcommand is ever issued here.
        """
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(self.run_dir),
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, ValueError) as exc:
            logger.error("git %s failed to launch: %s", " ".join(args), exc)
            if check:
                raise RuntimeError(f"git {' '.join(args)} failed to launch: {exc}") from exc
            return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr=str(exc))
        if check and completed.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed (rc={completed.returncode}): {completed.stderr.strip()}"
            )
        return completed

    def _head_sha(self) -> str:
        return self._git("rev-parse", "--short", "HEAD").stdout.strip()


# ----------------------------------------------------------------------------
# commit-message formatting (schema 6.3)
# ----------------------------------------------------------------------------


def commit_message(
    iteration: int,
    name: str,
    result: EvalResult,
    objectives: list[Objective],
    status: str,
    summary: str,
) -> str:
    """Format the append-only commit subject line per DESIGN 6.3::

        iter<NN> <name>: <obj1>=<v1> <obj2>=<v2> [<status>] — <one-line summary>

    Missing scores are rendered as ``n/a`` (e.g. for a crashed candidate).
    """
    parts = []
    for obj in objectives:
        value = result.scores.get(obj.name)
        rendered = _format_score(value)
        parts.append(f"{obj.name}={rendered}")
    score_str = " ".join(parts)
    summary_line = (summary or "").strip().splitlines()[0] if summary else ""
    return f"iter{int(iteration):02d} {name}: {score_str} [{status}] — {summary_line}".rstrip(" —")


# ----------------------------------------------------------------------------
# module-level helpers
# ----------------------------------------------------------------------------


def _branch_name(tag: str) -> str:
    """Return the full ledger branch name for ``tag``; idempotent on the prefix."""
    clean = (tag or "").strip()
    if clean.startswith(BRANCH_PREFIX):
        return clean
    return f"{BRANCH_PREFIX}{clean}"


def _safe_name(name: str) -> str:
    """Sanitize a candidate name for use as a directory component."""
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", str(name).strip())
    return cleaned.strip("._-") or "unnamed"


def _format_score(value: Any) -> str:
    """Render a score value compactly for the commit subject."""
    if value is None:
        return "n/a"
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if fv != fv or abs(fv) == float("inf"):
        return "n/a"
    if fv == 0:
        return "0"
    if 1e-3 <= abs(fv) < 1e4:
        return f"{fv:.4g}"
    return f"{fv:.3e}"


def _render_hypothesis_md(
    name: str,
    iteration: int,
    status: str,
    hypothesis: dict[str, Any],
) -> str:
    """Render ``hypothesis.md`` (front-matter per 7.2 + prose reasoning).

    Front-matter keys, in fixed order: ``name``, ``iteration``, ``axis``,
    ``parent``, ``change``, ``expected``, ``status``. The prose body is the
    agent's ``reasoning`` (which prior heatmaps/results were inspected and what
    failure mode this design targets).
    """
    axis = _scalar(hypothesis.get("axis"))
    parent = _scalar(hypothesis.get("parent"))
    change = _scalar(hypothesis.get("change"))
    expected = _scalar(hypothesis.get("expected"))
    reasoning = str(hypothesis.get("reasoning") or "").strip()

    lines = [
        "---",
        f"name: {_yaml_scalar(name)}",
        f"iteration: {int(iteration)}",
        f"axis: {_yaml_scalar(axis)}",
        f"parent: {_yaml_scalar(parent)}",
        f"change: {_yaml_scalar(change)}",
        f"expected: {_yaml_scalar(expected)}",
        f"status: {_yaml_scalar(status)}",
        "---",
        "",
        reasoning if reasoning else "(no reasoning recorded)",
        "",
    ]
    return "\n".join(lines)


def _scalar(value: Any) -> str:
    return "" if value is None else str(value)


def _yaml_scalar(value: str) -> str:
    """Quote a scalar for the front-matter block when it could confuse a parser."""
    text = str(value)
    if text == "":
        return '""'
    if _needs_quoting(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _needs_quoting(text: str) -> bool:
    if any(ch in text for ch in ":#\n\"'") or text != text.strip():
        return True
    # Leading indicators that YAML treats specially.
    return text[0] in "!&*?|>%@`,[]{}-"


def _write_json(path: Path, data: Any) -> None:
    """Write ``data`` as pretty JSON (deterministic key order via sort_keys)."""
    try:
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True, default=_json_default) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        logger.error("failed to write json %s: %s", path, exc)
        raise


def _json_default(obj: Any) -> Any:
    """Best-effort fallback for non-JSON-native metadata values."""
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "tolist"):  # numpy scalars / arrays
        try:
            return obj.tolist()
        except Exception:  # pragma: no cover - defensive
            return str(obj)
    if hasattr(obj, "item"):  # numpy scalar
        try:
            return obj.item()
        except Exception:  # pragma: no cover - defensive
            return str(obj)
    return str(obj)


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object file; return None on any error (never raises)."""
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("failed to read json %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _read_hypothesis(path: Path) -> tuple[dict[str, str], str]:
    """Parse ``hypothesis.md`` into (front-matter dict, prose). Never raises."""
    front: dict[str, str] = {}
    prose = ""
    try:
        if not path.is_file():
            return front, prose
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("failed to read %s: %s", path, exc)
        return front, prose

    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return front, text.strip()

    for line in match.group("body").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = _unquote(value.strip())
    prose = match.group("prose").strip()
    return front, prose


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return value


__all__ = [
    "Experience",
    "BundleRef",
    "commit_message",
    "EXPERIENCE_DIRNAME",
    "RESULTS_FILENAME",
    "FRONTIER_FILENAME",
    "STATUS_FRONTIER",
    "STATUS_DOMINATED",
    "STATUS_INFEASIBLE",
    "STATUS_CRASH",
]
