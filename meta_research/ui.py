"""Research browser — one self-contained HTML page with the search trajectory and the idea tournament.

``meta-research ui`` gathers what ``trajectory``, ``ideas rank`` and the ledger already know into one JSON
document and embeds it in a static page (no server, no network): an **Evolution** view (every candidate over time
with its lineage, per objective; click a point for its scores, change and hypothesis) and a **Tournament** view
(ideas by Elo; click one for its statement, reviews, matches with the judges' rationales, parents and the runs
that tested it). The two views link to each other. Read-only and deterministic apart from the timestamp.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from . import ideas as ib
from .experience import EXPERIENCE_DIRNAME, HYPOTHESIS_FILENAME, _read_hypothesis
from .interfaces import Objective
from .kg import build_kg
from .trajectory import trajectory_data

UI_FILENAME = "research.html"
MAX_PROSE = 6000


def ui_data(run_dir: Path | str, objectives: list[Objective], *, refs: dict[str, float] | None = None,
            scores: dict[str, dict[str, float]] | None = None, weights: dict[str, float] | None = None,
            title: str | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir)
    kg = build_kg(run_dir)
    board = ib.Board(run_dir)
    board = board if board.ideas else None
    links = board.links() if board else {}
    tested_by = {b: i for i, bundles in links.items() for b in bundles}

    runs = {}
    for n in kg["nodes"]:
        front, prose = _read_hypothesis(run_dir / EXPERIENCE_DIRNAME / n["id"] / HYPOTHESIS_FILENAME)
        if len(prose) > MAX_PROSE:
            prose = prose[:MAX_PROSE] + "\n…"
        runs[n["id"]] = {
            "id": n["id"], "iteration": n["iteration"], "name": n["name"], "status": n["status"],
            "operator": n.get("operator") or front.get("operator", ""),
            "scores": {**n["scores"], **(scores or {}).get(n["id"], {})},
            "change": front.get("change", ""), "idea": front.get("idea") or tested_by.get(n["id"]),
            "parents": [e["src"] for e in kg["edges"] if e["dst"] == n["id"]],
            "children": [e["dst"] for e in kg["edges"] if e["src"] == n["id"]],
            "hypothesis": prose,
        }
    views = [trajectory_data(run_dir, o, refs=refs, scores=scores) for o in objectives]

    tournament = None
    if board:
        rows = ib.rank(board, weights=weights, scores=scores)
        ideas = board.ideas
        starts = board.starts()
        for row in rows:
            idea = ideas[row["id"]]
            tested_by_bundle = sorted({*links.get(row["id"], []), *(rid for rid, r in runs.items() if r["idea"] == row["id"])})
            row.update(statement=idea.get("statement", ""), change=idea.get("change", ""),
                       tags=idea.get("tags", []), at=idea.get("at"), runs=tested_by_bundle,
                       started=[{k: s.get(k) for k in ("run", "where", "note", "at")} for s in starts.get(row["id"], [])],
                       reviews=[{k: r.get(k) for k in ("kind", "verdict", "scores", "critique", "at")}
                                for r in board.reviews(row["id"])])
        cards = {c: {"title": v.get("title"), "source": v.get("source"), "technique": v.get("technique")}
                 for c, v in board.cards.items()}
        matches = [{k: m.get(k) for k in ("a", "b", "winner", "format", "rationale", "at")}
                   for m in board.matches()]
        meta = [m.get("text", "") for m in board.of("metareview")]
        tournament = {"ideas": rows, "matches": matches, "cards": cards, "metareview": meta[-1] if meta else ""}

    return {"title": title or run_dir.name, "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "runs": runs, "views": views, "tournament": tournament}


def render_page(data: dict[str, Any], live: bool = False) -> str:
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.replace("__TITLE__", _esc(f"{data['title']} research")).replace("__DATA__", blob)
    return page.replace("__LIVE__", "true" if live else "false")


def render_ui(data: dict[str, Any], out: Path | str) -> Path:
    out = Path(out)
    out.write_text(render_page(data), encoding="utf-8")
    return out


def ledger_stamp(run_dir: Path | str, extra: list[Path] | None = None) -> str:
    """Changes whenever a result, bundle or idea-board event lands: the newest mtime of the ledger files."""
    run_dir = Path(run_dir)
    paths = [run_dir / "results.tsv", run_dir / "frontier.json", run_dir / ib.BOARD_DIRNAME / ib.BOARD_FILENAME,
             run_dir / EXPERIENCE_DIRNAME, *(extra or [])]
    if (run_dir / EXPERIENCE_DIRNAME).is_dir():
        paths += list((run_dir / EXPERIENCE_DIRNAME).iterdir())
    stamps = [p.stat().st_mtime_ns for p in paths if p.exists()]
    return str(max(stamps, default=0))


def serve(build: Any, run_dir: Path | str, *, host: str = "127.0.0.1", port: int = 8765,
          extra: list[Path] | None = None, on_ready: Any = None) -> None:
    """Serve the page on http://host:port/, rebuilt from the ledger on every load. ``build()`` returns ui_data.
    ``/stamp`` answers the ledger stamp; the page polls it and reloads itself when the ledger changes."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            path = self.path.split("?")[0]
            if path == "/stamp":
                body, kind = ledger_stamp(run_dir, extra).encode(), "text/plain"
            elif path in ("/", "/index.html"):
                try:
                    body, kind = render_page(build(), live=True).encode("utf-8"), "text/html; charset=utf-8"
                except Exception as exc:  # noqa: BLE001 - show the error in the browser, keep serving
                    self.send_error(500, f"could not build the page: {exc}")
                    return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # quiet: the page polls every few seconds
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    if on_ready:
        on_ready(server)  # e.g. to learn the port when port=0, or to shut it down from a test
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


TEMPLATE = r"""<title>__TITLE__</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500;6..72,600&family=Public+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: a lab notebook — sticky tab bar, a chart or table on the left, the selected item's record on the right. */
:root {
  --bg: #f6f7f5; --panel: #ffffff; --ink: #1d2421; --muted: #5d6a64; --line: #d9dfdb; --soft: #eef1ee;
  --accent: #2f6f73; --accent-soft: #dcebea; --win: #2e7d4f; --loss: #b2452f; --warn: #a66a00; --cross: #b0562c;
  --pt-front: #2f6f73; --pt-dom: #8a96a8; --pt-bad: #c07a3a; --pt-crash: #b2452f;
  --display: "Newsreader", Georgia, serif; --body: "Public Sans", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #121615; --panel: #1a201e; --ink: #e3e9e5; --muted: #98a59f; --line: #2d3532; --soft: #222a27;
  --accent: #6fb7b6; --accent-soft: #23393a; --win: #6cc08d; --loss: #e58a74; --warn: #e0a84a; --cross: #e08a5a;
  --pt-front: #6fb7b6; --pt-dom: #75819a; --pt-bad: #d99a5e; --pt-crash: #e58a74; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #121615; --panel: #1a201e; --ink: #e3e9e5; --muted: #98a59f; --line: #2d3532; --soft: #222a27;
  --accent: #6fb7b6; --accent-soft: #23393a; --win: #6cc08d; --loss: #e58a74; --warn: #e0a84a; --cross: #e08a5a;
  --pt-front: #6fb7b6; --pt-dom: #75819a; --pt-bad: #d99a5e; --pt-crash: #e58a74; color-scheme: dark }
* { box-sizing: border-box }
body { background: var(--bg); color: var(--ink); font: 14px/1.5 var(--body); }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--panel) }
.wrap { padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); max-width: 1440px; margin: 0 auto }
header { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--panel); border-bottom: 1px solid var(--line) }
header .wrap { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 20px; padding-block: 12px 0 }
h1 { font: 600 22px/1.2 var(--display); margin: 0; text-wrap: balance }
.meta { color: var(--muted); font-size: 12px }
nav { display: flex; gap: 4px; width: 100% }
nav a { padding: 8px 14px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent; font-weight: 500 }
nav a[aria-current="page"] { color: var(--ink); border-color: var(--accent) }
nav a:focus-visible, button:focus-visible, select:focus-visible, tr:focus-visible, circle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }
main .wrap { padding-block: 16px 40px }
.grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 420px); gap: 16px; align-items: start }
@media (max-width: 900px) { .grid { grid-template-columns: minmax(0, 1fr) } }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px; min-width: 0 }
.detail { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 96px); max-height: calc(100vh - 110px); overflow: auto }
@media (max-width: 900px) { .detail { position: static; max-height: none } }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin-bottom: 8px; color: var(--muted); font-size: 12px }
select, button { font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 4px 8px }
button { cursor: pointer }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; vertical-align: -1px }
.chart { overflow-x: auto }
svg text { fill: var(--muted); font: 11px var(--mono) }
svg .grid line { stroke: var(--line) }
svg .edge { fill: none; stroke: var(--muted); stroke-opacity: .45 }
svg .edge.crossover { stroke: var(--cross); stroke-dasharray: 5 3; stroke-opacity: .8 }
svg .edge.inspiration { stroke-dasharray: 2 3; stroke-opacity: .35 }
svg .edge.hot { stroke: var(--accent); stroke-opacity: 1; stroke-width: 2 }
svg .best { fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-opacity: .6 }
svg .ref line { stroke: var(--warn); stroke-dasharray: 6 4 }
svg .ref text { fill: var(--warn) }
svg circle.pt { cursor: pointer; stroke: var(--panel); stroke-width: 1.5 }
svg circle.pt.sel { stroke: var(--ink); stroke-width: 2.5 }
h2 { font: 600 18px/1.25 var(--display); margin: 0 0 4px; text-wrap: balance }
h3 { font: 600 11px/1.3 var(--body); text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 16px 0 6px }
.mono { font-family: var(--mono); font-size: 12px }
.kv { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 2px 12px; font-variant-numeric: tabular-nums }
.kv dt { color: var(--muted) } .kv dd { margin: 0; font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere }
.prose { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; max-width: 65ch }
.chip { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; background: var(--soft); color: var(--muted) }
.chip.tested, .chip.pass { background: var(--accent-soft); color: var(--accent) }
.chip.active { background: var(--soft); color: var(--ink) }
.chip.running { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn) }
.sections { display: flex; flex-direction: column; gap: 22px }
.sections h2 { display: flex; align-items: baseline; gap: 8px }
.sections .count { font: 600 12px var(--mono); color: var(--muted); background: var(--soft); border-radius: 10px; padding: 1px 8px }
.sections section > .meta { margin: 0 0 6px }
.sections details summary { font-weight: 500 }
.pos { color: var(--win); font-weight: 600 } .neg { color: var(--loss); font-weight: 600 }
.chip.fail, .chip.rejected, .chip.failed { background: color-mix(in srgb, var(--loss) 15%, transparent); color: var(--loss) }
.chip.revise { color: var(--warn); background: color-mix(in srgb, var(--warn) 14%, transparent) }
.link { color: var(--accent); cursor: pointer; text-decoration: underline; text-underline-offset: 2px; background: none; border: 0; padding: 0; font: inherit }
.tablewrap { overflow-x: auto }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums }
th { text-align: left; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); padding: 6px 8px; border-bottom: 1px solid var(--line); white-space: nowrap }
td { padding: 6px 8px; border-bottom: 1px solid var(--soft); vertical-align: top }
tbody tr { cursor: pointer } tbody tr:hover { background: var(--soft) } tbody tr.sel { background: var(--accent-soft) }
td.num { font-family: var(--mono); font-size: 12px; text-align: right; white-space: nowrap }
.bar { height: 4px; background: var(--accent); border-radius: 2px; opacity: .55; margin-top: 3px }
.match { border-top: 1px solid var(--soft); padding: 8px 0 }
.match .who { font-weight: 600 } .match .w { color: var(--win) } .match .l { color: var(--loss) }
.review { border-left: 3px solid var(--line); padding: 2px 0 2px 10px; margin: 8px 0 }
.review.pass { border-color: var(--accent) } .review.fail { border-color: var(--loss) } .review.revise { border-color: var(--warn) }
.empty { color: var(--muted); padding: 24px 0 }
details summary { cursor: pointer; color: var(--muted) }
@media (prefers-reduced-motion: no-preference) { tbody tr, circle.pt { transition: background .12s, r .12s } }
</style>

<header><div class="wrap">
  <h1 id="title"></h1><span class="meta" id="gen"></span>
  <nav><a href="#evolution" id="tab-evolution">Evolution</a><a href="#tournament" id="tab-tournament">Tournament</a></nav>
</div></header>
<main><div class="wrap">
  <section id="v-evolution" class="grid">
    <div class="card">
      <div class="toolbar">
        <label>Objective <select id="obj"></select></label>
        <span class="legend" id="legend"></span>
      </div>
      <div class="chart" id="chart"></div>
    </div>
    <aside class="card detail" id="run-detail"><p class="empty">Click a point to see that candidate: its scores, the change it made, its lineage and its hypothesis.</p></aside>
  </section>
  <section id="v-tournament" class="grid" hidden>
    <div class="card sections">
      <section>
        <h2>Running <span class="count" id="n-running"></span></h2>
        <p class="meta">Launched and waiting for results. They have left the tournament: their real outcome will rate them.</p>
        <div class="tablewrap"><table><thead><tr><th>Idea</th><th>Run</th><th>Where</th><th>Started</th><th>Elo</th></tr></thead><tbody id="rows-running"></tbody></table></div>
      </section>
      <section>
        <h2>Up next <span class="count" id="n-next"></span></h2>
        <p class="meta">Not started yet, best tournament rating first. Reviewed ones passed; unreviewed and directed ones are marked.</p>
        <div class="tablewrap"><table><thead><tr><th>#</th><th>Idea</th><th>Elo</th><th>W–L</th><th>Reviews</th></tr></thead><tbody id="rows-next"></tbody></table></div>
      </section>
      <section>
        <h2>Done <span class="count" id="n-done"></span></h2>
        <p class="meta">Tested. The gain is the realized change of the tested run over its parent.</p>
        <div class="tablewrap"><table><thead><tr><th>Idea</th><th>Tested by</th><th>Gain</th><th>Elo</th><th>W–L</th></tr></thead><tbody id="rows-done"></tbody></table></div>
      </section>
      <details><summary>Needs revision <span id="n-revise"></span></summary>
        <div class="tablewrap"><table><thead><tr><th>Idea</th><th>Elo</th><th>What the reviewers asked</th></tr></thead><tbody id="rows-revise"></tbody></table></div></details>
      <details><summary>Rejected <span id="n-rejected"></span></summary>
        <div class="tablewrap"><table><thead><tr><th>Idea</th><th>Elo</th><th>Why</th></tr></thead><tbody id="rows-rejected"></tbody></table></div></details>
      <details><summary>Latest meta-review</summary><div class="prose" id="metareview"></div></details>
    </div>
    <aside class="card detail" id="idea-detail"><p class="empty">Click an idea to see its statement, reviews, the matches it played with the judges' reasons, and the runs that tested it.</p></aside>
  </section>
</div></main>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const fmt = (v) => v == null ? "–" : (Math.abs(v) >= 100 ? v.toFixed(1) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(4));
const store = { get(k) { try { return localStorage.getItem(k) } catch (e) { return null } }, set(k, v) { try { localStorage.setItem(k, v) } catch (e) {} } };
$("#title").textContent = D.title;
$("#gen").textContent = `${Object.keys(D.runs).length} runs · ${D.tournament ? D.tournament.ideas.length + " ideas · " + D.tournament.matches.length + " matches · " : ""}generated ${D.generated}`;

/* ---------- routing ---------- */
let selRun = null, selIdea = null;
function route() {
  const h = location.hash.slice(1);
  const view = h === "tournament" ? "tournament" : "evolution";
  for (const v of ["evolution", "tournament"]) {
    $("#v-" + v).hidden = v !== view;
    if (v === view) $("#tab-" + v).setAttribute("aria-current", "page"); else $("#tab-" + v).removeAttribute("aria-current");
  }
}
addEventListener("hashchange", route);

/* ---------- evolution ---------- */
const COLORS = { frontier: "var(--pt-front)", dominated: "var(--pt-dom)", infeasible: "var(--pt-bad)", crash: "var(--pt-crash)" };
const LABELS = { frontier: "on the frontier", dominated: "dominated", infeasible: "infeasible", crash: "crashed" };
$("#legend").innerHTML = Object.keys(COLORS).map((k) => `<span><i style="background:${COLORS[k]}"></i>${LABELS[k]}</span>`).join("")
  + `<span>— lineage &nbsp;<span style="color:var(--cross)">- - crossover</span></span>`;
const objSel = $("#obj");
D.views.forEach((v, i) => objSel.add(new Option(`${v.objective} (${v.direction})`, i)));
objSel.value = Math.min(+(store.get("mr-obj") || 0), D.views.length - 1) || 0;
objSel.addEventListener("change", () => { store.set("mr-obj", objSel.value); drawChart() });

function drawChart() {
  const v = D.views[+objSel.value || 0];
  if (!v) { $("#chart").innerHTML = '<p class="empty">No objectives yet. Run meta-research seed.</p>'; return }
  const pts = v.points, valued = pts.filter((p) => p.value != null);
  const W = Math.max(640, Math.min(1100, pts.length * 22 + 120)), H = 420, m = { l: 56, r: 16, t: 14, b: 34 };
  const vals = valued.map((p) => p.value).concat(Object.values(v.refs || {}));
  let lo = Math.min(...vals), hi = Math.max(...vals); if (!isFinite(lo)) { lo = 0; hi = 1 } if (lo === hi) { lo -= 1; hi += 1 }
  const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
  const iters = pts.map((p) => p.iteration), x0 = Math.min(...iters, 0), x1 = Math.max(...iters, 1);
  const X = (i) => m.l + (i - x0) / (x1 - x0 || 1) * (W - m.l - m.r), Y = (val) => m.t + (hi - val) / (hi - lo) * (H - m.t - m.b);
  const ticks = 5, step = (hi - lo) / ticks;
  let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(v.objective)} for every candidate over iterations"><g class="grid">`;
  for (let k = 0; k <= ticks; k++) { const val = lo + k * step; s += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(val)}" y2="${Y(val)}"/><text x="${m.l - 6}" y="${Y(val) + 4}" text-anchor="end">${fmt(val)}</text>` }
  const xt = Math.max(1, Math.ceil((x1 - x0) / 10));
  for (let i = Math.ceil(x0 / xt) * xt; i <= x1; i += xt) s += `<text x="${X(i)}" y="${H - 12}" text-anchor="middle">${i}</text>`;
  s += `<text x="${W - m.r}" y="${H - 1}" text-anchor="end">iteration</text></g>`;
  for (const [label, val] of Object.entries(v.refs || {})) s += `<g class="ref"><line x1="${m.l}" x2="${W - m.r}" y1="${Y(val)}" y2="${Y(val)}"/><text x="${W - m.r - 4}" y="${Y(val) - 4}" text-anchor="end">${esc(label)} ${fmt(val)}</text></g>`;
  const byId = Object.fromEntries(pts.map((p) => [p.id, p]));
  for (const e of v.edges) { const a = byId[e.src], b = byId[e.dst]; if (!a || !b || a.value == null || b.value == null) continue;
    s += `<line class="edge ${e.style}" data-src="${a.id}" data-dst="${b.id}" x1="${X(a.iteration)}" y1="${Y(a.value)}" x2="${X(b.iteration)}" y2="${Y(b.value)}"/>` }
  if (v.best_so_far.length) { let d = ""; v.best_so_far.forEach(([i, b], k) => { d += (k ? `H${X(i)}V${Y(b)}` : `M${X(i)},${Y(b)}`) }); d += `H${X(x1)}`; s += `<path class="best" d="${d}"/>` }
  for (const p of valued) s += `<circle class="pt${p.id === selRun ? " sel" : ""}" tabindex="0" data-id="${p.id}" cx="${X(p.iteration)}" cy="${Y(p.value)}" r="${p.id === selRun ? 7 : 5}" fill="${COLORS[p.now] || COLORS.dominated}"><title>${esc(p.iteration + " " + p.name + ": " + fmt(p.value))}</title></circle>`;
  s += "</svg>";
  const missing = pts.length - valued.length;
  $("#chart").innerHTML = s + (missing ? `<p class="meta">${missing} candidate(s) have no ${esc(v.objective)} score (crashed, infeasible or not scored) and are listed only in their parents' and children's records.</p>` : "");
  $("#chart").querySelectorAll("circle.pt").forEach((c) => { c.onclick = () => selectRun(c.dataset.id); c.onkeydown = (ev) => { if (ev.key === "Enter") selectRun(c.dataset.id) } });
  highlight();
}
function highlight() { document.querySelectorAll("svg .edge").forEach((e) => e.classList.toggle("hot", e.dataset.src === selRun || e.dataset.dst === selRun)) }
function runLink(id) { const r = D.runs[id]; return r ? `<button class="link" data-run="${id}">${esc(r.iteration + " " + r.name)}</button>` : esc(id) }
function ideaLink(id) { const t = D.tournament && D.tournament.ideas.find((x) => x.id === id); return t ? `<button class="link" data-idea="${id}">${esc(id)}</button> ${esc(t.title)}` : esc(id) }
function selectRun(id, nav = true) {
  selRun = id; const r = D.runs[id]; if (!r) return;
  if (nav && location.hash !== "#evolution") location.hash = "evolution";
  const scores = Object.entries(r.scores).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${typeof v === "number" ? fmt(v) : esc(v)}</dd>`).join("");
  $("#run-detail").innerHTML = `<div class="meta mono">${esc(r.id)}</div><h2>${esc(r.name)}</h2>
    <div><span class="chip ${r.status === "crash" ? "failed" : ""}">${esc(r.status)}</span> ${r.operator ? `<span class="chip">${esc(r.operator)}</span>` : ""}</div>
    ${r.change ? `<h3>Change</h3><p>${esc(r.change)}</p>` : ""}
    ${r.idea ? `<h3>Idea tested</h3><p>${ideaLink(r.idea)}</p>` : ""}
    <h3>Scores</h3><dl class="kv">${scores || "<dt>none</dt><dd></dd>"}</dl>
    <h3>Lineage</h3><dl class="kv"><dt>parents</dt><dd>${r.parents.map(runLink).join(", ") || "–"}</dd><dt>children</dt><dd>${r.children.map(runLink).join(", ") || "–"}</dd></dl>
    ${r.hypothesis ? `<h3>Hypothesis</h3><div class="prose">${esc(r.hypothesis)}</div>` : ""}`;
  wire($("#run-detail")); drawChart();
}

/* ---------- tournament ---------- */
const T = D.tournament;
function statusChip(s, failed) { return `<span class="chip ${failed ? "failed" : esc(s)}">${failed ? "failed" : esc(s)}</span>` }
const GROUP = { running: "running", active: "next", directed: "next", new: "next", tested: "done", revise: "revise", rejected: "rejected" };
function drawTable() {
  if (!T) { $("#rows-next").innerHTML = '<tr><td colspan="5" class="empty">No idea board yet. Run an ideation round (meta-research ideas).</td></tr>'; return }
  const maxE = Math.max(...T.ideas.map((r) => r.elo)), minE = Math.min(...T.ideas.map((r) => r.elo));
  const groups = { running: [], next: [], done: [], revise: [], rejected: [] };
  for (const r of T.ideas) (groups[GROUP[r.status]] || groups.next).push(r);
  groups.done.sort((a, b) => (b.gain ?? -1e9) - (a.gain ?? -1e9));
  const tr = (r, cells) => `<tr tabindex="0" data-idea="${r.id}" class="${r.id === selIdea ? "sel" : ""}">${cells}</tr>`;
  const name = (r) => `<td style="min-width:220px"><span class="mono">${esc(r.id)}</span> ${esc(r.title)}<div class="meta">${esc(r.origin)}${r.strategy ? ":" + esc(r.strategy) : ""}</div></td>`;
  const elo = (r) => `<td class="num">${r.elo.toFixed(1)}<div class="bar" style="width:${Math.max(4, (r.elo - minE) / ((maxE - minE) || 1) * 64)}px;margin-left:auto"></div></td>`;
  const wl = (r) => `<td class="num">${r.wins}–${r.losses}</td>`;
  const gain = (r) => r.failed ? '<span class="chip failed">failed</span>' : r.gain == null ? "–" : `<span class="${r.gain >= 0 ? "pos" : "neg"}">${(r.gain >= 0 ? "+" : "") + (r.gain * 100).toFixed(1)}%</span>`;
  const lastReview = (r) => { const v = r.reviews[r.reviews.length - 1]; return v ? esc((v.critique || "").slice(0, 220)) + ((v.critique || "").length > 220 ? "…" : "") : "" };
  const empty = (n, text) => `<tr><td colspan="${n}" class="meta">${text}</td></tr>`;
  $("#rows-running").innerHTML = groups.running.map((r) => { const s = r.started[r.started.length - 1] || {};
    return tr(r, `${name(r)}<td class="mono">${esc(s.run || "")}</td><td>${esc(s.where || "")}</td><td class="num">${esc((s.at || "").replace("T", " ").slice(0, 16))}</td>${elo(r)}`) }).join("")
    || empty(5, "Nothing running.");
  $("#rows-next").innerHTML = groups.next.map((r, k) => tr(r, `<td class="num">${k + 1}</td>${name(r)}${elo(r)}${wl(r)}<td>${
    r.status === "active" ? `<span class="chip pass">passed</span>` : r.status === "directed" ? `<span class="chip">directed</span>` : `<span class="chip">not reviewed</span>`}</td>`)).join("")
    || empty(5, "No candidates: run an ideation round.");
  $("#rows-done").innerHTML = groups.done.map((r) => tr(r, `${name(r)}<td>${r.runs.map((b) => D.runs[b] ? esc(D.runs[b].iteration + " " + D.runs[b].name) : esc(b)).join("<br>") || "–"}</td><td class="num">${gain(r)}</td>${elo(r)}${wl(r)}`)).join("")
    || empty(5, "Nothing tested yet.");
  $("#rows-revise").innerHTML = groups.revise.map((r) => tr(r, `${name(r)}${elo(r)}<td class="meta">${lastReview(r)}</td>`)).join("") || empty(3, "None.");
  $("#rows-rejected").innerHTML = groups.rejected.map((r) => tr(r, `${name(r)}${elo(r)}<td class="meta">${lastReview(r)}</td>`)).join("") || empty(3, "None.");
  for (const k of ["running", "next", "done"]) $("#n-" + k).textContent = groups[k].length;
  $("#n-revise").textContent = `(${groups.revise.length})`; $("#n-rejected").textContent = `(${groups.rejected.length})`;
  document.querySelectorAll("#v-tournament tbody tr[data-idea]").forEach((row) => { row.onclick = () => selectIdea(row.dataset.idea); row.onkeydown = (ev) => { if (ev.key === "Enter") selectIdea(row.dataset.idea) } });
}
function selectIdea(id, nav = true) {
  selIdea = id; const r = T && T.ideas.find((x) => x.id === id); if (!r) return;
  if (nav && location.hash !== "#tournament") location.hash = "tournament";
  const ms = T.matches.filter((m) => m.a === id || m.b === id).slice().reverse();
  const matches = ms.map((m) => { const opp = m.a === id ? m.b : m.a, won = m.winner === id;
    return `<div class="match"><span class="who ${won ? "w" : "l"}">${won ? "Won" : "Lost"}</span> vs ${ideaLink(opp)} <span class="chip">${esc(m.format || "")}</span>
      ${m.rationale ? `<div class="prose meta" style="margin-top:4px">${esc(m.rationale)}</div>` : ""}</div>` }).join("");
  const reviews = r.reviews.map((v) => `<div class="review ${esc(v.verdict)}"><span class="chip ${esc(v.verdict)}">${esc(v.verdict)}</span> <span class="meta">${esc(v.kind)} review${v.scores ? " · " + Object.entries(v.scores).map(([k, s]) => esc(k) + " " + s).join(", ") : ""}</span>
      ${v.critique ? `<div class="prose" style="margin-top:4px">${esc(v.critique)}</div>` : ""}</div>`).join("");
  const cites = (r.cites || []).map((c) => T.cards[c] ? `<li><span class="mono">${esc(c)}</span> ${esc(T.cards[c].title)} <span class="meta">${esc(T.cards[c].source || "")}</span></li>` : D.runs[c] ? `<li>${runLink(c)}</li>` : `<li class="mono">${esc(c)}</li>`).join("");
  const children = T.ideas.filter((x) => (x.parents || []).includes(id)).map((x) => ideaLink(x.id)).join("<br>");
  $("#idea-detail").innerHTML = `<div class="meta mono">${esc(r.id)} · ${esc(r.origin)}${r.strategy ? ":" + esc(r.strategy) : ""}</div><h2>${esc(r.title)}</h2>
    <div>${statusChip(r.status, r.failed)} <span class="chip">Elo ${r.elo.toFixed(1)}</span> <span class="chip">${r.wins}–${r.losses}</span>${r.gain != null ? ` <span class="chip tested">realized ${(r.gain >= 0 ? "+" : "") + (r.gain * 100).toFixed(1)}%</span>` : ""}</div>
    <h3>Statement</h3><div class="prose">${esc(r.statement)}</div>
    ${r.change ? `<h3>Change</h3><p>${esc(r.change)}</p>` : ""}
    ${(r.started || []).length ? `<h3>Started</h3><p>${r.started.map((s) => `<span class="mono">${esc(s.run)}</span>${s.where ? " on " + esc(s.where) : ""} <span class="meta">${esc((s.at || "").replace("T", " ").slice(0, 16))}</span>`).join("<br>")}</p>` : ""}
    ${r.runs.length ? `<h3>Tested by</h3><p>${r.runs.map(runLink).join("<br>")}</p>` : ""}
    ${(r.parents || []).length || children ? `<h3>Idea lineage</h3><dl class="kv"><dt>parents</dt><dd>${(r.parents || []).map(ideaLink).join("<br>") || "–"}</dd><dt>children</dt><dd>${children || "–"}</dd></dl>` : ""}
    <h3>Reviews (${r.reviews.length})</h3>${reviews || '<p class="meta">none yet</p>'}
    <h3>Matches (${ms.length})</h3>${matches || '<p class="meta">none yet</p>'}
    ${cites ? `<h3>Cites</h3><ul style="padding-left:18px;margin:0">${cites}</ul>` : ""}`;
  wire($("#idea-detail")); drawTable();
}
function wire(el) {
  el.querySelectorAll("[data-run]").forEach((b) => b.onclick = () => selectRun(b.dataset.run));
  el.querySelectorAll("button[data-idea]").forEach((b) => b.onclick = () => selectIdea(b.dataset.idea));
}
if (T) $("#metareview").textContent = T.metareview || "No meta-review recorded yet.";
else { $("#tab-tournament").hidden = true }

route(); drawChart(); drawTable();
const kept = (k) => { try { return sessionStorage.getItem(k) } catch (e) { return null } };
const keep = (k, v) => { try { sessionStorage.setItem(k, v) } catch (e) {} };
const ideaPick = T && (T.ideas.find((x) => x.id === kept("mr-idea")) || T.ideas.find((x) => x.status === "active") || T.ideas[0]);
if (ideaPick) selectIdea(ideaPick.id, false);
const last = D.views[0] && D.views[0].points.filter((p) => p.value != null).slice(-1)[0];
const runPick = D.runs[kept("mr-run")] ? kept("mr-run") : last && last.id;
if (runPick) selectRun(runPick, false);
const _selRun = selectRun, _selIdea = selectIdea;
selectRun = (id, nav) => { keep("mr-run", id); _selRun(id, nav) };
selectIdea = (id, nav) => { keep("mr-idea", id); _selIdea(id, nav) };
const LIVE = __LIVE__;
if (LIVE) {
  let stamp = null;
  const poll = async () => {
    try {
      const s = await (await fetch("/stamp", { cache: "no-store" })).text();
      if (stamp !== null && s !== stamp) location.reload();
      stamp = s;
    } catch (e) {}
  };
  poll(); setInterval(poll, 10000);
  $("#gen").textContent += " · live";
}
</script>
"""
