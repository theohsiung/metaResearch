"""Compute resources: probe a machine, validate ``resources.json``, check hosts, and say what is usable now.

The agent decides where and when to run; this module only gathers facts and checks them. A probe is one shell
script, run locally or over a single ``ssh`` call, that prints ``### section`` blocks. A missing tool leaves its
section empty (``None`` after parsing): a probe never fails because ``nvidia-smi`` or ``docker`` is absent.

``resources.json`` (version 1) is written by the meta-research-setup skill after it has probed the machines and
asked the user what a probe cannot see (availability windows, deadlines of shared services, booking limits)::

    {"version": 1,
     "hosts": [{"name": "local", "ssh": null, "accelerators": [...], "container": {"image": "...", "run": "..."},
                "windows": [{"days": "all", "start": "00:30", "end": "07:53", "note": "GPU shared by day"}],
                "deadlines": [{"time": "08:00", "what": "vLLM teacher back up", "stop_at": "07:53"}],
                "required_paths": ["/data/models/x"], "executables": ["/root/run.sh"], "min_free_gb": 100,
                "notes": "..."}],
     "services": [{"name": "teacher", "url": "http://localhost:8000", "max_concurrency": 4,
                   "windows": [...], "notes": "..."}],
     "confirm_before": ["uploads", "scheduling on the shared GPU"]}
"""

from __future__ import annotations

import datetime as dt
import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

VERSION = 1
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

PROBE_SCRIPT = r"""
echo '### hostname'; hostname 2>/dev/null
echo '### cpus'; nproc 2>/dev/null
echo '### mem_kb'; awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null
echo '### disk'; df -Pk {paths} 2>/dev/null | tail -n +2
echo '### nvidia'; command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu,driver_version --format=csv,noheader,nounits 2>/dev/null
echo '### rocm'; command -v rocm-smi >/dev/null 2>&1 && rocm-smi --showproductname --showmeminfo vram --showuse --json 2>/dev/null
echo '### docker'; command -v docker >/dev/null 2>&1 && docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | head -20
echo '### groups'; getent group video render 2>/dev/null
echo '### end'
"""


# --- probe -------------------------------------------------------------------------------------------------------


def probe_script(paths: list[str] | None = None) -> str:
    return PROBE_SCRIPT.replace("{paths}", " ".join(shlex.quote(p) for p in (paths or ["/"])))


def run_probe(host: str | None = None, paths: list[str] | None = None, timeout: int = 60, tries: int = 4,
              wait: float = 5.0) -> str:
    """Run the probe locally (``host`` None) or over one ``ssh -o BatchMode=yes`` call (a refused connection is
    retried); return its raw output."""
    script = probe_script(paths)
    if host:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "bash -s"]
    else:
        cmd = ["bash", "-s"]
    out = _run(cmd, script, timeout, tries if host else 1, wait)
    if "### end" not in out.stdout:
        raise RuntimeError(f"probe of {host or 'local'} failed: {out.stderr.strip() or 'no output'}")
    return out.stdout


def sections(raw: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in raw.splitlines():
        if line.startswith("### "):
            cur = line[4:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _num(s: str) -> float | None:
    try:
        return float(s.strip().split()[0])
    except (ValueError, IndexError):
        return None


def parse_nvidia(text: str) -> list[dict[str, Any]]:
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or not parts[0]:
            continue
        gpus.append({"vendor": "nvidia", "name": parts[0], "memory_gb": _gb_from_mib(parts[1]),
                     "used_gb": _gb_from_mib(parts[2]), "util_pct": _num(parts[3]),
                     "driver": parts[4] if len(parts) > 4 else None})
    return gpus


def _gb_from_mib(s: str) -> float | None:
    v = _num(s)
    return None if v is None else round(v / 1024, 1)


def parse_rocm(text: str) -> list[dict[str, Any]]:
    """rocm-smi --json: {"card0": {"Card Series": ..., "VRAM Total Memory (B)": ..., "GPU use (%)": ...}, ...}."""
    if not text.strip():
        return []
    try:
        data = json.loads(text[text.index("{"):])
    except ValueError:
        return []
    gpus = []
    for card, info in sorted(data.items()):
        if not card.startswith("card") or not isinstance(info, dict):
            continue
        name = info.get("Card Series") or info.get("Card series") or info.get("Card Model") or info.get("Device Name")
        total = _num(str(info.get("VRAM Total Memory (B)", "")))
        used = _num(str(info.get("VRAM Total Used Memory (B)", "")))
        gpus.append({"vendor": "amd", "name": name, "memory_gb": None if total is None else round(total / 2**30, 1),
                     "used_gb": None if used is None else round(used / 2**30, 1),
                     "util_pct": _num(str(info.get("GPU use (%)", ""))), "driver": None})
    return gpus


def parse_probe(raw: str) -> dict[str, Any]:
    s = sections(raw)
    mem = _num(s.get("mem_kb", ""))
    disk = []
    for line in s.get("disk", "").splitlines():
        p = line.split()
        if len(p) >= 6:
            disk.append({"mount": p[5], "free_gb": round(int(p[3]) / 2**20, 1), "size_gb": round(int(p[1]) / 2**20, 1)})
    groups = {}
    for line in s.get("groups", "").splitlines():
        p = line.split(":")
        if len(p) >= 3 and p[2].isdigit():
            groups[p[0]] = int(p[2])
    images = [x for x in s.get("docker", "").splitlines() if x.strip()]
    return {
        "hostname": s.get("hostname") or None,
        "cpus": int(_num(s.get("cpus", "")) or 0) or None,
        "ram_gb": None if mem is None else round(mem / 2**20, 1),
        "disk": disk,
        "accelerators": parse_nvidia(s.get("nvidia", "")) + parse_rocm(s.get("rocm", "")),
        "docker": bool(images) or None,
        "docker_images": images,
        "group_gids": groups,
    }


def endpoint_up(url: str, timeout: float = 5.0) -> dict[str, Any]:
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v1/models", timeout=timeout) as r:
            body = json.loads(r.read().decode())
        return {"url": url, "up": True, "models": [m.get("id") for m in body.get("data", [])]}
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable", recorded as data
        return {"url": url, "up": False, "error": str(exc)[:200]}


# --- resources.json ----------------------------------------------------------------------------------------------


def _hhmm(s: Any, where: str, errors: list[str]) -> None:
    try:
        h, m = str(s).split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError
    except ValueError:
        errors.append(f"{where}: {s!r} is not HH:MM")


def _windows(ws: Any, where: str, errors: list[str]) -> None:
    if not isinstance(ws, list):
        errors.append(f"{where}.windows must be a list")
        return
    for i, w in enumerate(ws):
        at = f"{where}.windows[{i}]"
        if not isinstance(w, dict):
            errors.append(f"{at} must be an object")
            continue
        for k in ("start", "end"):
            _hhmm(w.get(k), f"{at}.{k}", errors)
        days = w.get("days", "all")
        if days != "all" and not (isinstance(days, list) and all(d in DAYS for d in days)):
            errors.append(f"{at}.days must be 'all' or a list of {', '.join(DAYS)}")


def validate(res: Any) -> list[str]:
    """Every problem in a resources.json object, as readable messages (empty list = valid)."""
    errors: list[str] = []
    if not isinstance(res, dict):
        return ["resources.json must hold a JSON object"]
    if res.get("version") != VERSION:
        errors.append(f"version must be {VERSION}")
    hosts = res.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        errors.append("hosts must be a non-empty list")
        hosts = []
    names = set()
    for i, h in enumerate(hosts):
        at = f"hosts[{i}]"
        if not isinstance(h, dict) or not h.get("name"):
            errors.append(f"{at} needs a name")
            continue
        at = f"host {h['name']}"
        if h["name"] in names:
            errors.append(f"{at}: duplicate name")
        names.add(h["name"])
        if h.get("ssh") is not None and not isinstance(h["ssh"], str):
            errors.append(f"{at}.ssh must be null (local) or 'user@host'")
        _windows(h.get("windows", []), at, errors)
        for j, d in enumerate(h.get("deadlines", [])):
            if not isinstance(d, dict) or not d.get("what"):
                errors.append(f"{at}.deadlines[{j}] needs time, what and stop_at")
                continue
            _hhmm(d.get("time"), f"{at}.deadlines[{j}].time", errors)
            _hhmm(d.get("stop_at", d.get("time")), f"{at}.deadlines[{j}].stop_at", errors)
        for key in ("required_paths", "executables"):
            if not isinstance(h.get(key, []), list):
                errors.append(f"{at}.{key} must be a list of paths")
        mf = h.get("min_free_gb")
        if mf is not None and not isinstance(mf, (int, float)):
            errors.append(f"{at}.min_free_gb must be a number")
    for i, s in enumerate(res.get("services", [])):
        at = f"services[{i}]"
        if not isinstance(s, dict) or not s.get("name") or not s.get("url"):
            errors.append(f"{at} needs name and url")
            continue
        mc = s.get("max_concurrency")
        if mc is not None and (not isinstance(mc, int) or mc < 1):
            errors.append(f"service {s['name']}.max_concurrency must be a positive integer")
        _windows(s.get("windows", []), f"service {s['name']}", errors)
    if not isinstance(res.get("confirm_before", []), list):
        errors.append("confirm_before must be a list of strings")
    return errors


def load(path: Path) -> dict[str, Any]:
    res = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate(res)
    if errors:
        raise ValueError("invalid resources.json:\n  " + "\n  ".join(errors))
    return res


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(w: dict[str, Any], now: dt.datetime) -> bool:
    """Whether ``now`` falls in window ``w``. A window whose end is before its start crosses midnight; its ``days``
    name the day it starts on."""
    start, end, t = _minutes(w["start"]), _minutes(w["end"]), now.hour * 60 + now.minute
    days = w.get("days", "all")
    today, yesterday = DAYS[now.weekday()], DAYS[(now.weekday() - 1) % 7]
    ok = (lambda d: days == "all" or d in days)
    if start == end:
        return ok(today)
    if start < end:
        return ok(today) and start <= t < end
    return (t >= start and ok(today)) or (t < end and ok(yesterday))


def available(entry: dict[str, Any], now: dt.datetime) -> bool:
    """No windows means always available."""
    ws = entry.get("windows") or []
    return not ws or any(in_window(w, now) for w in ws)


def next_stop(host: dict[str, Any], now: dt.datetime) -> tuple[str, str] | None:
    """The soonest deadline stop time after ``now``: (stop_at, what)."""
    best = None
    t = now.hour * 60 + now.minute
    for d in host.get("deadlines", []):
        stop = d.get("stop_at") or d["time"]
        wait = (_minutes(stop) - t) % (24 * 60)
        if best is None or wait < best[0]:
            best = (wait, stop, d["what"])
    return None if best is None else (best[1], best[2])


# --- check -------------------------------------------------------------------------------------------------------

CHECK_SCRIPT = r"""
for p in {paths}; do [ -e "$p" ] && echo "path ok $p" || echo "path missing $p"; done
for p in {execs}; do if [ -x "$p" ]; then echo "exec ok $p"; elif [ -e "$p" ]; then echo "exec notexec $p"; else echo "exec missing $p"; fi; done
for p in {disk}; do df -Pk "$p" 2>/dev/null | tail -n +2 | awk -v p="$p" '{print "disk", p, $4}'; done
echo '### end'
"""


def check_script(host: dict[str, Any]) -> str:
    q = lambda xs: " ".join(shlex.quote(x) for x in xs) or ""  # noqa: E731
    disk = sorted({str(Path(p)) for p in host.get("required_paths", [])[:1]}) or ["/"]
    return (CHECK_SCRIPT.replace("{paths}", q(host.get("required_paths", [])))
            .replace("{execs}", q(host.get("executables", []))).replace("{disk}", q(disk)))


def parse_check(raw: str, host: dict[str, Any]) -> list[tuple[str, str, bool, str]]:
    """Rows of (kind, item, ok, detail)."""
    rows = []
    for line in raw.splitlines():
        p = line.split(" ", 2)
        if len(p) < 3:
            continue
        kind, state, item = p
        if kind == "path":
            rows.append(("path", item, state == "ok", state))
        elif kind == "exec":
            rows.append(("executable", item, state == "ok", {"ok": "ok", "notexec": "not executable"}.get(state, state)))
        elif kind == "disk":
            free = round(int(item) / 2**20, 1) if item.isdigit() else None
            need = host.get("min_free_gb")
            ok = need is None or (free is not None and free >= need)
            rows.append(("free disk", state, ok, f"{free} GB free" + (f" (need {need})" if need is not None else "")))
    return rows


def _run(cmd: list[str], script: str, timeout: int, tries: int, wait: float) -> subprocess.CompletedProcess[str]:
    """Run a probe script; retry a failed connection (an sshd under a scan refuses new connections for a while)."""
    for attempt in range(tries):
        out = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout, check=False)
        if "### end" in out.stdout or out.returncode != 255 or attempt == tries - 1:
            return out
        time.sleep(wait * (attempt + 1))
    return out


def run_check(host: dict[str, Any], timeout: int = 60, tries: int = 4,
              wait: float = 5.0) -> list[tuple[str, str, bool, str]]:
    script = check_script(host)
    cmd = (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host["ssh"], "bash -s"]
           if host.get("ssh") else ["bash", "-s"])
    try:
        out = _run(cmd, script, timeout, tries if host.get("ssh") else 1, wait)
    except subprocess.TimeoutExpired:
        return [("reach", host.get("ssh") or "local", False, "timed out")]
    if "### end" not in out.stdout:
        return [("reach", host.get("ssh") or "local", False, out.stderr.strip()[:200] or "no output")]
    return parse_check(out.stdout, host)
