"""Tests for compute resources: probe parsing, resources.json validation, windows, and host checks (no network)."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from meta_research import cli, resources

NVIDIA = "NVIDIA RTX PRO 6000 Blackwell, 97887, 1024, 3, 580.95\nNVIDIA RTX PRO 6000 Blackwell, 97887, 0, 0, 580.95\n"
ROCM = json.dumps({
    "card0": {"Card Series": "AMD Instinct MI300X", "VRAM Total Memory (B)": str(192 * 2**30),
              "VRAM Total Used Memory (B)": str(2**30), "GPU use (%)": "100"},
    "system": {"Driver version": "6.8"},
})


def raw(nvidia: str = "", rocm: str = "", docker: str = "") -> str:
    return (
        "### hostname\nbox1\n### cpus\n64\n### mem_kb\n" + str(512 * 2**20) + "\n"
        "### disk\n/dev/nvme0 1000000000 1 524288000 1% /data\n"
        f"### nvidia\n{nvidia}### rocm\n{rocm}\n### docker\n{docker}\n"
        "### groups\nvideo:x:44:root\nrender:x:992:root\n### end\n"
    )


def test_probe_parses_nvidia() -> None:
    info = resources.parse_probe(raw(nvidia=NVIDIA))
    assert info["hostname"] == "box1" and info["cpus"] == 64 and info["ram_gb"] == 512.0
    assert len(info["accelerators"]) == 2
    g = info["accelerators"][0]
    assert g["vendor"] == "nvidia" and g["memory_gb"] == 95.6 and g["used_gb"] == 1.0 and g["driver"] == "580.95"
    assert info["disk"] == [{"mount": "/data", "free_gb": 500.0, "size_gb": 953.7}]
    assert info["group_gids"] == {"video": 44, "render": 992}


def test_probe_parses_rocm_and_ignores_non_cards() -> None:
    info = resources.parse_probe(raw(rocm=ROCM, docker="d3nano-rocm:latest"))
    assert info["accelerators"] == [{"vendor": "amd", "name": "AMD Instinct MI300X", "memory_gb": 192.0,
                                     "used_gb": 1.0, "util_pct": 100.0, "driver": None}]
    assert info["docker"] is True and info["docker_images"] == ["d3nano-rocm:latest"]


def test_probe_tolerates_missing_tools() -> None:
    info = resources.parse_probe(raw())
    assert info["accelerators"] == [] and info["docker"] is None and info["docker_images"] == []
    assert resources.parse_rocm("rocm-smi: not json") == []


def test_local_probe_runs() -> None:
    info = resources.parse_probe(resources.run_probe(None, ["/"]))
    assert info["cpus"] and info["disk"]


def good() -> dict:
    return {
        "version": 1,
        "hosts": [
            {"name": "local", "ssh": None, "windows": [{"days": "all", "start": "00:30", "end": "07:53"}],
             "deadlines": [{"time": "08:00", "what": "vLLM back", "stop_at": "07:53"}],
             "required_paths": [], "executables": [], "min_free_gb": 10},
            {"name": "mi300x", "ssh": "root@10.0.0.5", "windows": [], "deadlines": []},
        ],
        "services": [{"name": "teacher", "url": "http://localhost:8000", "max_concurrency": 4}],
        "confirm_before": ["uploads"],
    }


def test_validate_good() -> None:
    assert resources.validate(good()) == []


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r.update(version=2), "version must be 1"),
    (lambda r: r.update(hosts=[]), "hosts must be a non-empty list"),
    (lambda r: r["hosts"].append({"name": "local"}), "duplicate name"),
    (lambda r: r["hosts"][0]["windows"][0].update(start="25:00"), "is not HH:MM"),
    (lambda r: r["hosts"][0]["windows"][0].update(days=["monday"]), "days must be"),
    (lambda r: r["hosts"][0]["deadlines"][0].update(stop_at="7.53"), "stop_at"),
    (lambda r: r["services"][0].update(max_concurrency=0), "max_concurrency"),
    (lambda r: r["hosts"][1].update(ssh=5), "ssh must be"),
])
def test_validate_bad(mutate, message) -> None:
    r = good()
    mutate(r)
    assert any(message in e for e in resources.validate(r)), resources.validate(r)


def at(day: int, hhmm: str) -> dt.datetime:
    """2026-11-02 is a Monday; day 0 = mon."""
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(2026, 11, 2 + day, h, m)


def test_window_same_day() -> None:
    w = {"days": ["mon"], "start": "09:00", "end": "17:00"}
    assert resources.in_window(w, at(0, "09:00")) and not resources.in_window(w, at(0, "17:00"))
    assert not resources.in_window(w, at(1, "10:00"))


def test_window_crossing_midnight_belongs_to_its_start_day() -> None:
    w = {"days": ["fri"], "start": "22:00", "end": "07:53"}
    assert resources.in_window(w, at(4, "23:00"))  # Friday night
    assert resources.in_window(w, at(5, "07:00"))  # Saturday morning, still Friday's window
    assert not resources.in_window(w, at(5, "08:00"))
    assert not resources.in_window(w, at(4, "07:00"))  # Friday morning belongs to Thursday's window


def test_available_and_next_stop() -> None:
    host = good()["hosts"][0]
    assert resources.available(host, at(2, "03:00")) and not resources.available(host, at(2, "12:00"))
    assert resources.available(good()["hosts"][1], at(2, "12:00"))  # no windows: always
    assert resources.next_stop(host, at(2, "03:00")) == ("07:53", "vLLM back")
    assert resources.next_stop(good()["hosts"][1], at(2, "03:00")) is None


def test_check_finds_missing_path_and_lost_exec_bit(tmp_path: Path) -> None:
    ok, script = tmp_path / "model.bin", tmp_path / "run.sh"
    ok.write_text("x")
    script.write_text("#!/bin/sh\n")
    os.chmod(script, 0o644)
    host = {"name": "local", "ssh": None, "required_paths": [str(ok), str(tmp_path / "missing")],
            "executables": [str(script)], "min_free_gb": 0}
    rows = {(kind, item): (good_, detail) for kind, item, good_, detail in resources.run_check(host)}
    assert rows[("path", str(ok))][0] is True
    assert rows[("path", str(tmp_path / "missing"))] == (False, "missing")
    assert rows[("executable", str(script))] == (False, "not executable")
    assert any(k == "free disk" and v[0] for (k, _), v in rows.items())
    os.chmod(script, 0o755)
    assert all(g for _, _, g, _ in resources.run_check(dict(host, required_paths=[str(ok)])))


def test_check_disk_threshold() -> None:
    rows = resources.parse_check("disk / 1048576\n### end\n", {"min_free_gb": 2})
    assert rows == [("free disk", "/", False, "1.0 GB free (need 2)")]


def test_cli_show_and_check(tmp_path: Path, capsys) -> None:
    res = good()
    res["hosts"] = [res["hosts"][0]]
    (tmp_path / "resources.json").write_text(json.dumps(res))
    assert cli.main(["resources", "show", "--run-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "local" in out and "teacher" in out and "ask the user before: uploads" in out
    assert cli.main(["resources", "check", "--run-dir", str(tmp_path)]) == 0
    assert cli.main(["resources", "show", "--run-dir", str(tmp_path / "nowhere")]) == 2
    (tmp_path / "resources.json").write_text(json.dumps({"version": 1, "hosts": []}))
    assert cli.main(["resources", "show", "--run-dir", str(tmp_path)]) == 2
    assert "hosts must be a non-empty list" in capsys.readouterr().err


def test_cli_help_lists_resources(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["resources", "--help"])
    assert "probe" in capsys.readouterr().out
