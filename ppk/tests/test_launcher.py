"""The host launcher (../../dji-ppk, a script without .py): the upgrade check, without network."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "dji-ppk"
pytestmark = pytest.mark.skipif(not LAUNCHER.is_file(), reason="the launcher is not in the image")


@pytest.fixture(scope="module")
def launcher():
    loader = importlib.machinery.SourceFileLoader("dji_ppk_launcher", str(LAUNCHER))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


def test_current_versions_from_the_pins(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "docker_available", lambda: False)
    cur = launcher.current_versions({})
    assert cur["rtklib"].startswith("v") and launcher._vtuple(cur["playwright"]) and launcher._vtuple(cur["rnxcmp"])
    assert launcher._vtuple(cur["python"]) and cur["debian"].endswith(")") and "antex" not in cur
    assert launcher.current_versions({"RTKLIB_REF": "main"})["rtklib"] == "main"  # .env wins over the Dockerfile


def test_upgrades(launcher):
    cur = {"rtklib": "v2.5.1", "playwright": "1.63.0", "rnxcmp": "4.2.0", "python": "3.12", "debian": "bookworm (12)",
           "antex": "2026-09-20"}
    assert launcher.upgrades(cur, dict(cur)) == []
    new = {"rtklib": "v2.10.0", "playwright": "1.63.0", "rnxcmp": "4.2.0", "python": "3.14", "debian": "trixie (13)",
           "antex": "2026-09-28"}
    found = {name: (old, latest, how) for name, old, latest, how in launcher.upgrades(cur, new)}
    assert found["RTKLIB-EX"][:2] == ("v2.5.1", "v2.10.0") and "RTKLIB_REF=v2.10.0" in found["RTKLIB-EX"][2]  # numeric, not text order
    assert "Playwright" not in found and "RNXCMP (crx2rnx)" not in found
    assert "python:3.14-slim" in found["Python (image)"][2] and "debian:trixie-slim" in found["Debian (builder)"][2]
    assert found["IGS ANTEX igs20.atx"][:2] == ("image 2026-09-20", "updated 2026-09-28")
    assert launcher.upgrades({**cur, "rtklib": "main"}, new)[0][0] != "RTKLIB-EX"  # a branch is not compared
    assert launcher.upgrades(cur, {}) == []  # offline: nothing to report


def test_latest_versions_uses_the_daily_cache(launcher, tmp_path, monkeypatch):
    import json, time
    monkeypatch.setattr(launcher, "VERSION_CACHE", tmp_path / "cache.json")
    calls = []
    monkeypatch.setattr(launcher, "fetch_latest", lambda: calls.append(1) or {"python": "3.14"})
    assert launcher.latest_versions() == {"python": "3.14"} and launcher.latest_versions() == {"python": "3.14"}
    assert len(calls) == 1
    (tmp_path / "cache.json").write_text(json.dumps({"checked": time.time() - 25 * 3600, "latest": {}}))
    launcher.latest_versions()
    assert len(calls) == 2


def test_unprocessable_folders_sort_last(launcher):
    rows = [{"folder": "a", "next": "no-times"}, {"folder": "b", "next": "done"}, {"folder": "c", "next": "order"}]
    assert [r["folder"] for r in launcher.sort_rows(rows)] == ["b", "c", "a"]


def test_unprocessable_folders_cannot_be_ticked(launcher):
    rows = [{"folder": "ok", "next": "order", "sessions": 1, "photos": 50, "flown": "f", "base": "missing", "result": ""},
            {"folder": "bad", "next": "no-times", "sessions": 1, "photos": 4, "flown": "f", "base": "missing",
             "result": "MRK has no exposure times (DJI wrote week -522)"}]
    assert launcher.selectable(rows[0]) and not launcher.selectable(rows[1])
    ticked = [False, False]
    assert launcher.toggle(rows, ticked, [0, 1]) == ["bad"] and ticked == [True, False]
    lines = launcher._table_lines(rows, ticked, -1, 200)
    assert "[-]  bad" in lines[2] and "skipped" not in lines[2] and "[x]" in lines[1]
