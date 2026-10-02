"""Tests for the ppk package.

Fixtures are excerpts of real DJI / ESTPOS files. Coordinates are shifted by a few hundred
metres and the raw observations in sample.obs are perturbed, so they cannot be used for positioning.
"""
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ppk.compare import compare_events
from ppk.discover import index_images
from ppk.estpos import ceil_quarter, floor_quarter, gpst_to_utc, plan_order
from ppk.events import write_obs_with_events
from ppk.mrk import parse_mrk
from ppk.offsets import apply_ned_offset, ned_difference
from ppk.outputs import CameraEvent, match_events
from ppk.pos import read_pos
from ppk.rinex import ecef_to_llh, llh_to_ecef, read_header, scan_obs_span
from ppk.timeutil import format_rinex3_event, gps_to_datetime, parse_rinex3_epoch, datetime_to_gps

FIX = Path(__file__).parent / "fixtures"


def test_mrk_parse():
    ev = parse_mrk(FIX / "sample.MRK")
    assert [e.id for e in ev] == [1, 3, 4, 5, 6]
    e = ev[0]
    assert e.tow == pytest.approx(543905.155176)
    assert e.week == 2435
    assert (e.north_mm, e.east_mm, e.down_mm) == (58, -118, 149)
    assert e.lat == pytest.approx(58.40406298) and e.lon == pytest.approx(26.74558232)
    assert e.ellh == pytest.approx(126.645) and e.q == 50
    assert e.std_v == pytest.approx(0.006364)
    assert e.time == datetime(2026, 9, 12, 7, 5, 5, 155176)


def test_gps_time_roundtrip():
    t = gps_to_datetime(2435, 543905.155176)
    week, tow = datetime_to_gps(t)
    assert week == 2435 and tow == pytest.approx(543905.155176, abs=1e-6)


def test_rinex_epoch_parse_and_event_format():
    t, flag, n = parse_rinex3_epoch("> 2026  9 12  7  4 15.8000000  0 25                     ")
    assert t == datetime(2026, 9, 12, 7, 4, 15, 800000) and flag == 0 and n == 25
    line = format_rinex3_event(datetime(2026, 9, 12, 7, 5, 5, 155176))
    assert line == ">" + " 2026  9 12  7  5  5.1551760  5  0"
    t2, flag2, n2 = parse_rinex3_epoch(line)
    assert flag2 == 5 and n2 == 0 and t2 == datetime(2026, 9, 12, 7, 5, 5, 155176)


def test_rover_header_and_span():
    hdr = read_header(FIX / "sample.obs")
    assert hdr.version == pytest.approx(3.05)
    assert hdr.first_obs == datetime(2026, 9, 12, 7, 4, 15, 800000)
    assert "G" in hdr.obs_types and "C5I" in hdr.obs_types["G"]
    first, last, n = scan_obs_span(FIX / "sample.obs", hdr)
    assert n == 3 and first == hdr.first_obs and last == datetime(2026, 9, 12, 7, 4, 16, 200000)


def test_base_header_virtual_and_position():
    hdr = read_header(FIX / "base_header.26o")
    assert hdr.is_virtual
    assert hdr.interval == 1.0
    assert hdr.ant_type == "LEIAR25.R4      LEIT"
    lat, lon, h = hdr.approx_llh
    # LLH of the header XYZ (the fixture position is shifted from the real base, see module comment)
    assert lat == pytest.approx(58.403146800, abs=1e-9)
    assert lon == pytest.approx(26.741522299, abs=1e-9)
    assert h == pytest.approx(80.7127, abs=1e-4)
    x, y, z = llh_to_ecef(lat, lon, h)
    assert (x, y, z) == pytest.approx(hdr.approx_xyz, abs=1e-3)


def test_event_injection_order(tmp_path):
    events = [datetime(2026, 9, 12, 7, 4, 15, 900000), datetime(2026, 9, 12, 7, 4, 16, 0),
              datetime(2026, 9, 12, 7, 4, 10), datetime(2026, 9, 12, 7, 4, 17)]
    dst = tmp_path / "ev.obs"
    stats = write_obs_with_events(FIX / "sample.obs", dst, events)
    assert stats == {"inserted": 4, "before_first_epoch": 1, "after_last_epoch": 1, "epochs": 3}
    seq = [parse_rinex3_epoch(l) for l in dst.read_text().splitlines() if l.startswith(">")]
    times = [t for t, _, _ in seq]
    assert times == sorted(times)
    flags = [f for _, f, _ in seq]
    assert flags == [5, 0, 5, 5, 0, 0, 5]
    # observation lines untouched
    assert sum(1 for l in dst.read_text().splitlines() if l.startswith("G 1")) == 2


def test_offsets_match_emlid_convention():
    # Emlid camera position minus interpolated antenna position for MRK id 1 was
    # dN=+56.7 mm dE=-115.4 mm dU=-149.0 mm with MRK offsets N=58 E=-118 V=149.
    lat, lon, h = apply_ned_offset(58.4040648, 26.7455855, 126.95, 0.058, -0.118, 0.149)
    dn, de, du = ned_difference(58.4040648, 26.7455855, 126.95, lat, lon, h)
    assert dn == pytest.approx(0.058, abs=1e-5)
    assert de == pytest.approx(-0.118, abs=1e-5)
    assert du == pytest.approx(-0.149, abs=1e-6)


def test_pos_parse_and_event_matching():
    traj = read_pos(FIX / "sample.pos")
    assert len(traj.rows) == 12 and traj.rows[0].time == datetime(2026, 9, 12, 7, 4, 15, 800000)
    assert traj.rows[0].q in (1, 2) and traj.rows[0].ns > 0
    ev = read_pos(FIX / "sample_events.pos")
    mrk = parse_mrk(FIX / "sample.MRK")
    images = {1: Path("DJI_20260912100505_0001_V.JPG")}
    matched, unmatched = match_events(mrk, ev.rows, images)
    assert len(matched) == 5 and not unmatched  # Emlid times are truncated to ms; tolerance covers it
    assert matched[0].image == "DJI_20260912100505_0001_V.JPG" and matched[1].image == "#0003"
    # lever arm applied: camera is below the antenna by V
    assert matched[0].ant_h - matched[0].ellh == pytest.approx(0.149, abs=1e-6)


def test_compare_self_is_zero():
    ev = read_pos(FIX / "sample_events.pos")
    mrk = parse_mrk(FIX / "sample.MRK")
    matched, _ = match_events(mrk, ev.rows, {})
    res = compare_events(matched, ev.rows, use_antenna=True)
    assert res.n_matched == 5 and max(res.d3_mm) < 1e-6


def test_index_images(tmp_path):
    for n in ("DJI_20260912100505_0001_V.JPG", "DJI_20260912100505_0001_T.JPG", "DJI_20260912100506_0003_V.JPG", "x.txt"):
        (tmp_path / n).write_bytes(b"")
    idx = index_images(tmp_path)
    assert sorted(idx) == [1, 3] and idx[1].name.endswith("_V.JPG")


def test_quarter_rounding_and_leap_seconds():
    t = datetime(2026, 9, 12, 7, 4, 15, 800000)
    assert gpst_to_utc(t) == datetime(2026, 9, 12, 7, 3, 57, 800000)
    assert floor_quarter(datetime(2026, 9, 12, 6, 59, 30)) == datetime(2026, 9, 12, 6, 45)
    assert ceil_quarter(datetime(2026, 9, 12, 7, 29, 1)) == datetime(2026, 9, 12, 7, 30)
    assert ceil_quarter(datetime(2026, 9, 12, 7, 30, 1)) == datetime(2026, 9, 12, 7, 45)
    assert ceil_quarter(datetime(2026, 9, 12, 7, 30)) == datetime(2026, 9, 12, 7, 30)


def test_plan_order(tmp_path):
    from ppk.discover import Flight
    import shutil
    shutil.copy(FIX / "sample.obs", tmp_path / "DJI_x.OBS")
    shutil.copy(FIX / "sample.MRK", tmp_path / "DJI_x.MRK")
    (tmp_path / "DJI_x.NAV").write_text("")
    fl = Flight(tmp_path, tmp_path / "DJI_x.OBS", tmp_path / "DJI_x.NAV", tmp_path / "DJI_x.MRK", {})
    order = plan_order(fl, buffer_minutes=5)
    assert order.start_utc == datetime(2026, 9, 12, 6, 45)
    assert order.end_utc == datetime(2026, 9, 12, 7, 15)
    assert order.lat == pytest.approx(58.4041, abs=1e-3)
    assert order.height == pytest.approx(min(e.ellh for e in parse_mrk(fl.mrk)) - 50)
    assert order.start_local.hour == 9  # EEST = UTC+3


def test_out_dir_in_place(tmp_path):
    from ppk.cli import _out_dir_for
    from ppk.discover import Flight
    fl = Flight(tmp_path / "flight", tmp_path / "flight/DJI_x.OBS", tmp_path / "flight/DJI_x.NAV", tmp_path / "flight/DJI_x.MRK", {})
    assert _out_dir_for(Path("/data/out"), fl) == Path("/data/out/flight")
    assert _out_dir_for(Path("/data/out"), fl, in_place=True) == tmp_path / "flight"


def test_flight_path_fallback(tmp_path, monkeypatch):
    import ppk.cli as cli
    (tmp_path / "DJI_x").mkdir()
    monkeypatch.setattr(cli, "DEFAULT_FLIGHTS_DIR", str(tmp_path))
    assert cli._flight_path(str(tmp_path / "DJI_x")) == tmp_path / "DJI_x"
    assert cli._flight_path("../DJI_x") == tmp_path / "DJI_x"
    assert cli._flight_path("/somewhere/else/DJI_x") == tmp_path / "DJI_x"
    with pytest.raises(FileNotFoundError):
        cli._flight_path("DJI_missing")


NAV_SAMPLE = """     3.05           N: GNSS NAV DATA    M: Mixed            RINEX VERSION / TYPE
                                                            END OF HEADER
G15 2007  2  2 16  0  0  .445653684437E-03  .295585778076E-11  .000000000000E+00
      .600000000000E+02 -.760625000000E+02  .501378027264E-08  .102433957812E+01
E03 2026  9 18 14  0  0  .100000000000E-03  .000000000000E+00  .000000000000E+00
      .600000000000E+02 -.760625000000E+02  .501378027264E-08  .102433957812E+01
"""


def test_stale_nav_detection(tmp_path):
    from ppk.rinex import nav_epochs, stale_nav_systems
    nav = tmp_path / "DJI_x.NAV"
    nav.write_text(NAV_SAMPLE)
    ep = nav_epochs(nav)
    assert ep["G"] == [datetime(2007, 2, 2, 16)] and ep["E"] == [datetime(2026, 9, 18, 14)]
    stale = stale_nav_systems(nav, datetime(2026, 9, 18, 14, 18))
    assert list(stale) == ["G"] and stale["G"].year == 2007


def test_find_nav_files_zip_and_siblings(tmp_path):
    import zipfile
    from ppk.rinex import find_nav_files
    z = tmp_path / "virt261o00.rnx.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name in ("virt261o00.26o", "virt261o00.26n", "virt261o00.26g", "virt261o00.26l", "virt261o00.26f"):
            zf.writestr(name, "x")
    navs = find_nav_files(z, tmp_path / "work")
    assert [p.name for p in navs] == ["virt261o00.26f", "virt261o00.26g", "virt261o00.26l", "virt261o00.26n"]
    d = tmp_path / "plain"; d.mkdir()
    for name in ("virt255g30.26o", "virt255g30.26n", "virt255g30.26l", "other.26n", "virt255g30.26o.bak"):
        (d / name).write_text("x")
    assert [p.name for p in find_nav_files(d / "virt255g30.26o", tmp_path / "w2")] == ["virt255g30.26l", "virt255g30.26n"]


def test_solution_quality_report():
    from ppk.outputs import solution_quality, format_quality
    ev = read_pos(FIX / "sample_events.pos")
    mrk = parse_mrk(FIX / "sample.MRK")
    matched, _ = match_events(mrk, ev.rows, {})
    traj = read_pos(FIX / "sample.pos")
    q = solution_quality(matched, len(mrk), traj.rows)
    assert q["photos"]["fixed"] == 5 and q["photos"]["unsolved"] == 0
    assert q["trajectory"]["epochs"] == 12 and q["satellites"]["min"] >= 1
    assert q["std_mm"]["up"]["median"] > q["std_mm"]["north"]["median"] > 0
    text = format_quality(q, "flight", "base.26o", 0.13, None)
    assert "5/5 fixed (100.0 %)" in text and "baseline 0.13 km" in text and "none (no Emlid" in text


def test_rtk_vs_ppk_statistics():
    from ppk.outputs import rtk_vs_ppk, format_rtk_vs_ppk
    ev = read_pos(FIX / "sample_events.pos")
    mrk = parse_mrk(FIX / "sample.MRK")
    matched, _ = match_events(mrk, ev.rows, {})
    r = rtk_vs_ppk(matched)
    assert r["n"] == 5 and r["mrk_q"] == {"fixed": 5}
    assert abs(r["north"]["mean_mm"]) < 200 and r["north"]["std_mm"] < 50  # same flight, RTK and PPK agree to cm
    text = format_rtk_vs_ppk(r)
    assert "compared photos: 5" in text and "RTK base offset" in text


def test_in_short_table():
    from ppk.outputs import rtk_vs_ppk, format_in_short, solution_quality
    ev = read_pos(FIX / "sample_events.pos")
    mrk = parse_mrk(FIX / "sample.MRK")
    matched, _ = match_events(mrk, ev.rows, {})
    rtk = rtk_vs_ppk(matched)
    assert rtk["horizontal_error"]["rms_mm"] >= rtk["horizontal_error"]["p95_mm"] * 0 and rtk["vertical_error"]["max_mm"] >= 0
    text = format_in_short(rtk, solution_quality(matched, len(mrk), read_pos(FIX / "sample.pos").rows))
    assert "DJI on-board RTK (as flown)" in text and "after PPK with the RINEX base" in text and "cm" in text
    assert "of which a constant shift" in text and "and scatter around it" in text
    assert max(len(line) for line in text.splitlines()) <= 100
    real = {"north": {"mean_mm": -303.2}, "up": {"mean_mm": -154.0, "std_mm": 13.9},
            "horizontal_error": {"rms_mm": 403.4, "max_mm": 443.2}, "vertical_error": {"rms_mm": 154.6, "max_mm": 222.5},
            "offset_horizontal_mm": 366.0, "scatter_horizontal_mm": 169.5}
    text = format_in_short(real, {"std_mm": {"horizontal": {"median": 3.9}, "up": {"median": 5.9}}})
    shift = next(line for line in text.splitlines() if "constant shift" in line)
    scatter = next(line for line in text.splitlines() if "scatter around" in line)
    assert "37 cm" in shift and "-15 cm" in shift and "17 cm" in scatter and "1.4 cm" in scatter
    assert "-0 cm" not in format_in_short({**real, "up": {"mean_mm": -4.4, "std_mm": 2.5}}, {})


def test_results_layout_and_legacy_cleanup(tmp_path):
    from ppk.outputs import find_summary, remove_legacy_outputs, results_dir
    d = tmp_path / "DJI_f"; d.mkdir()
    assert find_summary(d) is None
    keep = ("geo.txt", "DJI_a.OBS", "DJI_a_events.pos", "notes.txt")  # geo.txt, inputs, an Emlid reference, user files stay
    old = ("summary.json", "accuracy.txt", "events.csv", "DJI_a_geo.txt", "DJI_a_accuracy.txt", "DJI_a_trajectory.pos",
           "DJI_a_trajectory_events.pos", "DONE")
    for n in keep + old:
        (d / n).write_text("x")
    assert find_summary(d) == d / "summary.json"
    assert sorted(remove_legacy_outputs(d, ["DJI_a"])) == sorted(old)
    assert sorted(p.name for p in d.iterdir()) == sorted(keep)
    results_dir(d).mkdir(); (results_dir(d) / "summary.json").write_text("{}")
    assert find_summary(d) == d / "ppk" / "summary.json"


def test_tee_log(tmp_path, monkeypatch, capsys):
    import logging, sys
    from ppk import ui
    monkeypatch.setenv("PPK_COLOR", "1")
    stdout = sys.stdout
    path = tmp_path / "ppk" / "processing.log"
    with pytest.raises(RuntimeError):
        with ui.tee_log(path):
            print(ui.c("hi", "green"))
            logging.getLogger("ppk").warning("w")
            raise RuntimeError("boom")
    text = path.read_text()
    assert "hi" in text and "WARNING w" in text and "RuntimeError: boom" in text and "\x1b" not in text
    assert sys.stdout is stdout and "\x1b[32mhi" in capsys.readouterr().out  # the terminal keeps its colors


def _untimed_mrk(text: str) -> str:
    """The fixture MRK as DJI writes it without exposure times: TOW -259200 in week [-522], lever arm 0/0/0."""
    import re
    return re.sub(r"^(\d+)\t[\d.]+\t\[\d+\]", r"\1\t-259200.000000\t[-522]", text, flags=re.M)


def test_mrk_without_exposure_times(tmp_path):
    import shutil
    from ppk.mrk import has_exposure_times
    from ppk.discover import load_flights
    from ppk.status import folder_status
    from ppk.cli import process_flight
    assert has_exposure_times(parse_mrk(FIX / "sample.MRK"))
    d = tmp_path / "DJI_untimed"; d.mkdir()
    shutil.copy(FIX / "sample.obs", d / "DJI_a.OBS"); (d / "DJI_a.NAV").write_text("")
    (d / "DJI_a.MRK").write_text(_untimed_mrk((FIX / "sample.MRK").read_text()))
    assert not has_exposure_times(parse_mrk(d / "DJI_a.MRK"))
    for i in (1, 3, 4, 5, 6):  # name times far from anything the MRK could say
        (d / f"DJI_20260808155508_{i:04d}_V.JPG").write_bytes(b"")
    flights = load_flights(d)
    assert len(flights[0].images) == 5  # counted, not dropped by a 1989 session window
    st = folder_status(d, flights, None)
    assert st.photos == 5 and st.next == "no-times" and "no exposure times (DJI wrote week -522)" in st.result
    with pytest.raises(RuntimeError, match="no exposure times"):
        process_flight(flights[0], d / "base.26o", d)
    from ppk.cli import main, NO_TIMES
    for cmd in ("estpos-order", "estpos-window", "process"):  # refused before Playwright or the portal is touched
        assert main([cmd, str(d)]) == NO_TIMES


def test_local_time_helpers(monkeypatch):
    from ppk.timeutil import gpst_to_local, span_local
    monkeypatch.setenv("PPK_TZ", "Europe/Tallinn")
    t = gpst_to_local(datetime(2026, 9, 18, 14, 18, 54))
    assert (t.hour, t.minute, t.second, t.tzname()) == (17, 18, 36, "EEST")  # UTC+3 and minus 18 leap seconds
    assert span_local(datetime(2026, 9, 18, 14, 18, 54), datetime(2026, 9, 18, 14, 38, 36)) == "2026-09-18 17:19 - 17:38 EEST"
    assert span_local(datetime(2026, 9, 18, 14, 0, 0), datetime(2026, 9, 18, 14, 59, 59)) == "2026-09-18 17:00 - 18:00 EEST"


def test_find_reference_events_skips_own_output(tmp_path):
    import shutil
    from ppk.compare import find_reference_events
    shutil.copy(FIX / "sample_events.pos", tmp_path / "DJI_x_events.pos")
    shutil.copy(FIX / "sample_events.pos", tmp_path / "DJI_x_trajectory_events.pos")
    assert [p.name for p in find_reference_events(tmp_path)] == ["DJI_x_events.pos"]


def test_estpos_dms_and_order_check():
    from datetime import timedelta
    from ppk.estpos import EstposOrder
    from ppk.estpos_web import dms, availability_params, order_mismatch
    digits, text = dms(59.4372403, 2)
    assert digits == "592614065" and text == "59° 26' 14.065\""
    digits, text = dms(24.7535747, 3)
    assert digits == "0244512869" and text == "024° 45' 12.869\""
    assert dms(59.9999999, 2)[0] == "600000000"  # carries into the next degree instead of 59° 60'
    order = EstposOrder(59.4372403, 24.7535747, 45.0, "x", datetime(2026, 9, 18, 14, 18), datetime(2026, 9, 18, 14, 38),
                        datetime(2026, 9, 18, 14, 0), datetime(2026, 9, 18, 14, 45))
    url = ("https://gnss-rtk.maaamet.ee/Xpos/API//vrinex/dataAvailability?startTime=2026-09-18T14%3A00%3A00.000Z"
           "&endTime=2026-09-18T14%3A45%3A00.000Z&latitude=59.4372403&longitude=24.7535747&name=flight1"
           "&markerName=Virtual%20RINEX&markerNumber=VRNX&sendNotifications=true&observationRate=1000&_=1")
    params = availability_params(url)
    assert params["start_utc"] == order.start_utc and params["rate_ms"] == 1000 and params["name"] == "flight1"
    assert order_mismatch(params, order, 1, "flight1") == []
    bad = order_mismatch(params, order, 5, "other")
    assert len(bad) == 2 and "rate" in bad[0] and "project" in bad[1]
    assert order.start_local.hour == 17  # EEST


def test_estpos_result_entry_parsing():
    from ppk.estpos_web import parse_result_entry, find_entry
    text = """2. Taotletud 2026-09-21 08:57:42, Projekt: demo week 9
Soovitatud algusaeg: 2026-09-18 17:00:00
Kestvus: 01:00 h
Laiuskraad: 59° 26' 14.065" N
Lae alla"""
    e = parse_result_entry(text, "vrinexFileDownloadButton42", True)
    assert e.requested == datetime(2026, 9, 21, 8, 57, 42) and e.project == "demo week 9"
    assert e.start_local == datetime(2026, 9, 18, 17, 0) and e.duration_h == 1.0 and e.ready
    empty = parse_result_entry("1. Taotletud 2026-09-18 11:42\nProjekt:\nTühi\nKestvus: 02:00 h", "b", False)
    assert empty.project == "" and empty.duration_h == 2.0
    assert find_entry([e, empty], "demo week 9", datetime(2026, 9, 21)) is e
    assert find_entry([e], "demo week 9", datetime(2026, 9, 22)) is None


def test_index_images_separates_sessions(tmp_path):
    from ppk.discover import index_images, image_timestamp
    for n in ("DJI_20260905113447_0001_V.JPG", "DJI_20260905114417_0663_V.JPG",
              "DJI_20260905115406_0001_V.JPG", "DJI_20260905120001_0002_V.JPG"):
        (tmp_path / n).write_bytes(b"")
    assert image_timestamp(tmp_path / "DJI_20260905113447_0001_V.JPG") == datetime(2026, 9, 5, 11, 34, 47)
    first = index_images(tmp_path, (datetime(2026, 9, 5, 11, 34, 50), datetime(2026, 9, 5, 11, 45, 0)))
    assert {i: p.name for i, p in first.items()} == {1: "DJI_20260905113447_0001_V.JPG", 663: "DJI_20260905114417_0663_V.JPG"}
    second = index_images(tmp_path, (datetime(2026, 9, 5, 11, 54, 10), datetime(2026, 9, 5, 12, 5, 0)))
    assert {i: p.name for i, p in second.items()} == {1: "DJI_20260905115406_0001_V.JPG", 2: "DJI_20260905120001_0002_V.JPG"}
    assert len(index_images(tmp_path)) == 3  # no window: later file wins for index 1


def test_load_flights_and_combined_order(tmp_path):
    import shutil
    from ppk.discover import load_flights, load_flight, group_by_folder
    for stem in ("DJI_20260905113447_0002_D", "DJI_20260905115406_0002_D"):
        shutil.copy(FIX / "sample.obs", tmp_path / f"{stem}.OBS")
        shutil.copy(FIX / "sample.MRK", tmp_path / f"{stem}.MRK")
        (tmp_path / f"{stem}.NAV").write_text("")
    flights = load_flights(tmp_path)
    assert [f.stem for f in flights] == ["DJI_20260905113447_0002_D", "DJI_20260905115406_0002_D"]
    assert load_flight(tmp_path / "DJI_20260905115406_0002_D.OBS").stem == "DJI_20260905115406_0002_D"
    with pytest.raises(ValueError):
        load_flight(tmp_path)
    assert list(group_by_folder(flights)) == [tmp_path]
    order = plan_order(flights, buffer_minutes=5)
    single = plan_order(flights[0], buffer_minutes=5)
    assert order.start_utc == single.start_utc and order.end_utc == single.end_utc  # identical sample spans
    from ppk.estpos import format_order
    assert "2 sessions" in format_order(order, flights)


def test_merge_summaries():
    from ppk.cli import merge_summaries
    s1 = {"rover_obs": "a.OBS", "events": {"mrk": 10, "solved": 10, "unsolved": 0, "fix": 9, "float": 1, "other": 0},
          "trajectory": {"fix": 90, "total": 100}, "images_missing": 0, "seconds": 5.0, "outputs": {"geo_txt": "a_geo.txt"}}
    s2 = {"rover_obs": "b.OBS", "events": {"mrk": 5, "solved": 4, "unsolved": 1, "fix": 4, "float": 0, "other": 0},
          "trajectory": {"fix": 50, "total": 50}, "images_missing": 1, "seconds": 3.0, "outputs": {"geo_txt": "b_geo.txt"}}
    m = merge_summaries("day", [s1, s2], 13)
    assert m["events"] == {"mrk": 15, "solved": 14, "unsolved": 1, "fix": 13, "float": 1, "other": 0}
    assert m["trajectory_fix_ratio"] == round(140 / 150, 4) and m["geo_txt_rows"] == 13 and m["sessions"] == ["a.OBS", "b.OBS"]


def test_plan_orders_splits_at_session_gaps(tmp_path, monkeypatch):
    import shutil
    from ppk import estpos
    from ppk.discover import load_flights
    for stem in ("DJI_20260905113447_0002_D", "DJI_20260905115406_0002_D", "DJI_20260905150000_0002_D"):
        shutil.copy(FIX / "sample.obs", tmp_path / f"{stem}.OBS")
        shutil.copy(FIX / "sample.MRK", tmp_path / f"{stem}.MRK")
        (tmp_path / f"{stem}.NAV").write_text("")
    flights = load_flights(tmp_path)
    # pretend the three sessions were observed 08:34-08:45, 08:54-09:05 and 12:00-12:20 GPST
    spans = {"DJI_20260905113447_0002_D": (datetime(2026, 9, 5, 8, 34), datetime(2026, 9, 5, 8, 45)),
             "DJI_20260905115406_0002_D": (datetime(2026, 9, 5, 8, 54), datetime(2026, 9, 5, 9, 5)),
             "DJI_20260905150000_0002_D": (datetime(2026, 9, 5, 12, 0), datetime(2026, 9, 5, 12, 20))}
    monkeypatch.setattr(estpos, "scan_obs_span", lambda path, header=None: (*spans[Path(path).stem], 1))
    orders = estpos.plan_orders(flights, buffer_minutes=5, max_hours=2.0)
    assert [len(f) for _, f in orders] == [2, 1]
    assert orders[0][0].start_utc == datetime(2026, 9, 5, 8, 15) and orders[0][0].end_utc == datetime(2026, 9, 5, 9, 15)
    assert orders[1][0].start_utc == datetime(2026, 9, 5, 11, 45) and orders[1][0].end_utc == datetime(2026, 9, 5, 12, 30)
    assert len(estpos.plan_orders(flights, buffer_minutes=5, max_hours=24)) == 1


def test_find_existing_order():
    from ppk.estpos import EstposOrder
    from ppk.estpos_web import ResultEntry, find_existing
    order = EstposOrder(59.4, 24.7, 45.0, "x", datetime(2026, 9, 5, 8, 34), datetime(2026, 9, 5, 9, 4),
                        datetime(2026, 9, 5, 8, 15), datetime(2026, 9, 5, 9, 15))
    same = ResultEntry(datetime(2026, 9, 21, 11, 34), "demo", datetime(2026, 9, 5, 11, 15), 1.0, True, "b1", "")
    shorter = ResultEntry(datetime(2026, 9, 21, 11, 24), "demo", datetime(2026, 9, 5, 11, 15), 0.75, True, "b2", "")
    other = ResultEntry(datetime(2026, 9, 21, 11, 24), "other", datetime(2026, 9, 5, 11, 15), 1.0, True, "b3", "")
    assert find_existing([shorter, other, same], "demo", order) is same
    assert find_existing([shorter, other], "demo", order) is None


def test_folder_status_next_step(tmp_path):
    import json, os, shutil, time
    from ppk.discover import load_flights
    from ppk.status import folder_status, format_status
    d = tmp_path / "DJI_x"; d.mkdir()
    shutil.copy(FIX / "sample.obs", d / "DJI_a.OBS"); shutil.copy(FIX / "sample.MRK", d / "DJI_a.MRK"); (d / "DJI_a.NAV").write_text("")
    for i in (1, 3, 4, 5, 6):  # the five MRK events, photo names inside the session's local time window (10:05 EEST)
        (d / f"DJI_20260912100505_{i:04d}_V.JPG").write_bytes(b"")
    flights = load_flights(d)
    st = folder_status(d, flights, None)
    assert st.next == "order" and st.base == "missing" and st.sessions == 1 and st.photos == 5
    # a base header that covers the sample span (07:04-07:24 GPST on 2026-09-12 -> the fixture base spans 06:30-08:29)
    base = d / "base.26o"
    hdr = (FIX / "base_header.26o").read_text()
    base.write_text(hdr + "> 2026 09 12 06 30  0.0000000  0  1\n> 2026 09 12 08 29 59.0000000  0  1\n")
    st = folder_status(d, flights, None)
    assert st.next == "order" and "no nav files" in st.base  # covers, but GPS would be unusable
    (d / "base.26n").write_text("")
    st = folder_status(d, flights, None)
    assert st.base == "base.26o" and st.next == "process"
    (d / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "geo_txt_rows": 5, "events": {"fix": 5, "mrk": 5}}))
    st = folder_status(d, flights, None)
    assert st.next == "reprocess" and "old layout" in st.result  # accuracy.txt next to the photos breaks WebODM
    (d / "summary.json").unlink()
    (d / "ppk").mkdir()
    (d / "ppk" / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "geo_txt_rows": 5, "events": {"fix": 5, "mrk": 5}}))
    st = folder_status(d, flights, None)
    assert st.next == "done" and "5 rows" in st.result
    time.sleep(0.01); os.utime(d / "DJI_a.MRK", None)  # input newer than the result
    st = folder_status(d, flights, None)
    assert st.next == "reprocess" and "outdated" in st.result
    (d / "ppk" / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "geo_txt_rows": 3, "events": {"fix": 5, "mrk": 5}}))
    st = folder_status(d, flights, None)
    assert st.next == "reprocess" and "photos arrived" in st.result  # processed with 3 photos, 5 are here now
    (d / "DJI_20260912100505_0006_V.JPG").unlink()
    (d / "ppk" / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "geo_txt_rows": 4, "events": {"fix": 5, "mrk": 5}}))
    st = folder_status(d, load_flights(d), None)
    assert st.next == "photos" and "1 of 5 photos not in the folder yet" in st.result
    assert "DJI_x" in format_status([st])


def test_find_flights_depth(tmp_path):
    import shutil
    from ppk.discover import find_flights
    for rel in ("DJI_top", "other/DJI_nested"):
        d = tmp_path / rel; d.mkdir(parents=True)
        shutil.copy(FIX / "sample.obs", d / "DJI_a.OBS"); shutil.copy(FIX / "sample.MRK", d / "DJI_a.MRK"); (d / "DJI_a.NAV").write_text("")
    (tmp_path / ".hidden").mkdir(); shutil.copy(FIX / "sample.obs", tmp_path / ".hidden" / "DJI_h.OBS")
    assert [f.directory.name for f in find_flights(tmp_path)] == ["DJI_top"]          # default depth 1
    assert find_flights(tmp_path, recursive=False) == []                                # root only
    assert sorted(f.directory.name for f in find_flights(tmp_path, max_depth=2)) == ["DJI_nested", "DJI_top"]


def test_rinex_retention(tmp_path):
    import shutil
    from ppk.estpos import rinex_days_left
    from ppk.discover import load_flights
    from ppk.status import folder_status
    first = datetime(2026, 9, 12, 7, 4, 15)  # GPST of the fixture flight
    assert rinex_days_left(first, now=datetime(2026, 9, 21)) == 81
    assert rinex_days_left(first, now=datetime(2026, 12, 11)) == 0
    assert rinex_days_left(first, now=datetime(2026, 12, 15)) == -4
    d = tmp_path / "DJI_old"; d.mkdir()
    shutil.copy(FIX / "sample.obs", d / "DJI_a.OBS"); shutil.copy(FIX / "sample.MRK", d / "DJI_a.MRK"); (d / "DJI_a.NAV").write_text("")
    flights = load_flights(d)
    st = folder_status(d, flights, None, now=datetime(2026, 12, 15))
    assert st.next == "expired" and st.base == "missing" and st.rinex_days_left < 0
    st = folder_status(d, flights, None, now=datetime(2026, 12, 5))
    assert st.next == "order" and "(6 d left)" in st.row()[2]
    (d / "base.26o").write_text((FIX / "base_header.26o").read_text() + "> 2026 09 12 06 30  0.0000000  0  1\n> 2026 09 12 08 29 59.0000000  0  1\n")
    (d / "base.26n").write_text("")
    st = folder_status(d, flights, None, now=datetime(2026, 12, 15))
    assert st.next == "photos"  # base present: still processable even though the portal has no data any more


def test_project_name_unique_and_legacy():
    from ppk.estpos_web import project_name, project_candidates
    a = "2026-07-26 mõisavärava pargi ehitus 1 4D"
    b = "2026-07-26 mõisavärava pargi ehitus 1 no elev optim 4D"
    assert len(project_name(a)) <= 30 and len(project_name(b)) <= 30 and project_name(a) != project_name(b)
    assert project_name("short name") == "short name"
    assert project_candidates(a)[1] == a[:30] and project_candidates("short name") == ["short name"]
    assert len(project_name(a, 28)) <= 28


def test_project_names_per_order():
    from ppk.cli import _project_names
    long = "2026-07-26 mõisavärava pargi ehitus 1 4D"
    one = _project_names(long, 1, None)
    assert len(one) == 1 and one[0][1] == long[:30] and "~" in one[0][0]
    two = _project_names(long, 2, None)
    assert [n[0][-2:] for n in two] == ["-1", "-2"] and two[1][1] == long[:28] + "-2"
    assert _project_names("DJI_x", 1, None) == [["DJI_x"]]
    assert _project_names("DJI_x", 1, "custom") == [["custom"]]


def test_portal_error_detection():
    from ppk.estpos_web import portal_error
    assert portal_error("X-posiga ei saanud ühendust, palun proovige uuesti värskendades lehte.(Unsuccessful HTTP response (XPOS_HTTP_404))")
    assert portal_error("Tulemused\nRINEX andmed\nLae alla") is None


def test_accuracy_text(tmp_path):
    import json
    from ppk.status import accuracy_text
    one = tmp_path / "s1.json"
    one.write_text(json.dumps({"rtk_vs_ppk": {"horizontal_error": {"rms_mm": 412.3}, "vertical_error": {"rms_mm": 209.0}},
                               "quality": {"std_mm": {"horizontal": {"median": 3.6}, "up": {"median": 6.0}}}}))
    assert accuracy_text(one) == "H 41 cm→0.4 cm  V 21 cm→0.6 cm"
    merged = tmp_path / "s2.json"
    merged.write_text(json.dumps({"session_summaries": [
        {"rtk_vs_ppk": {"horizontal_error": {"rms_mm": 170.0}, "vertical_error": {"rms_mm": 90.0}},
         "quality": {"std_mm": {"horizontal": {"median": 3.5}, "up": {"median": 5.6}}}},
        {"rtk_vs_ppk": {"horizontal_error": {"rms_mm": 510.0}, "vertical_error": {"rms_mm": 120.0}},
         "quality": {"std_mm": {"horizontal": {"median": 4.0}, "up": {"median": 7.4}}}}]}))
    assert accuracy_text(merged) == "H 51 cm→0.4 cm  V 12 cm→0.7 cm"
    (tmp_path / "s3.json").write_text(json.dumps({"events": {}}))
    assert accuracy_text(tmp_path / "s3.json") == ""


def test_has_nav_files(tmp_path):
    import zipfile
    from ppk.rinex import has_nav_files
    (tmp_path / "virt255g30.26o").write_text("x")
    assert not has_nav_files(tmp_path / "virt255g30.26o")
    (tmp_path / "virt255g30.26n").write_text("x")
    assert has_nav_files(tmp_path / "virt255g30.26o")
    with zipfile.ZipFile(tmp_path / "a.rnx.zip", "w") as zf:
        zf.writestr("virt261o00.26o", "x")
    assert not has_nav_files(tmp_path / "a.rnx.zip")
    with zipfile.ZipFile(tmp_path / "b.rnx.zip", "w") as zf:
        zf.writestr("virt261o00.26o", "x"); zf.writestr("virt261o00.26n", "x")
    assert has_nav_files(tmp_path / "b.rnx.zip")


def test_status_reports_float_photos(tmp_path):
    import json, shutil
    from ppk.discover import load_flights
    from ppk.status import folder_status
    d = tmp_path / "DJI_f"; d.mkdir()
    shutil.copy(FIX / "sample.obs", d / "DJI_a.OBS"); shutil.copy(FIX / "sample.MRK", d / "DJI_a.MRK"); (d / "DJI_a.NAV").write_text("")
    for i in (1, 3, 4, 5, 6):
        (d / f"DJI_20260912100505_{i:04d}_V.JPG").write_bytes(b"")
    (d / "base.26o").write_text((FIX / "base_header.26o").read_text() + "> 2026 09 12 06 30  0.0000000  0  1\n> 2026 09 12 08 29 59.0000000  0  1\n")
    (d / "base.26n").write_text("")
    (d / "ppk").mkdir(); (d / "ppk" / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "geo_txt_rows": 5, "events": {"fix": 3, "mrk": 5, "float": 2, "other": 0, "unsolved": 0}}))
    st = folder_status(d, load_flights(d), None)
    assert st.next == "done" and "3/5 fixed, 2 float (not cm-accurate)" in st.result


# ----------------------------------------------------------------------------- D-RTK 3 base point survey

STEM = "DRTK3_0041_20260912083000_8PHDN9B00AG8UN"
HELD = "58.40039533,Lat\t26.73652104,Lon\t75.865,Ellh"


def _epochs(first: datetime, last: datetime, step_s: int = 10) -> str:
    """RINEX 3 epoch lines from first to last (inclusive), enough of them for a surveyable session."""
    out, t = [], first
    while t < last:
        out.append(f"> {t:%Y %m %d %H %M} {t.second:2d}.0000000  0  1")
        t += timedelta(seconds=step_s)
    out.append(f"> {last:%Y %m %d %H %M} {last.second:2d}.0000000  0  1")
    return "\n".join(out) + "\n"


def _log_obs(text_header: str, epochs: str) -> str:
    """A D-RTK 3 session OBS: the Virtual RINEX fixture header (it has an APPROX POSITION) plus epoch records."""
    return text_header + epochs


def _basepoint_dir(tmp_path, name="site-basepoint", native=True, with_mrk=False):
    """A base point folder like the station's storage: DRTK3_<seq>_<time>_<serial>.OBS/.NAV (native RINEX), or a
    DAT-only session that needs convbin."""
    d = tmp_path / name; d.mkdir()
    epochs = _epochs(datetime(2026, 9, 12, 6, 30), datetime(2026, 9, 12, 8, 29, 59))
    if native:
        (d / f"{STEM}.OBS").write_text(_log_obs((FIX / "base_header.26o").read_text(), epochs))
        (d / f"{STEM}.NAV").write_text("     3.05           N: GNSS NAV DATA    M: Mixed            RINEX VERSION / TYPE\n"
                                       "                                                            END OF HEADER\n")
        (d / f"{STEM}.dat").write_bytes(b"\xd3\x00\x13" * 40)
    else:
        (d / f"{STEM}.dat").write_bytes(b"\xd3\x00\x13" * 40)
    if with_mrk:
        rows = []
        t = 22800.0  # 06:20 GPST on Saturday 2026-09-12 (week 2383, day 6)
        for i in range(1, 13):
            pos = "58.40038600,Lat\t26.73655463,Lon\t71.131,Ellh" if i < 3 else "58.40039536,Lat\t26.73655903,Lon\t76.605,Ellh" if i < 6 else HELD
            q = 16 if i < 3 else 1
            rows.append(f"{i}\t{t + 5 * (i - 1):.6f}\t[2383]\t     0,N\t     0,E\t     0,V\t{pos}\t0.3, 0.5, 0.8\t{q},Q")
        (d / "DRTK3_0039_20260912081500_8PHDN9B00AG8UN.MRK").write_text("\n".join(rows) + "\n")
    return d


def test_basepoint_discovery_and_orders(tmp_path):
    from ppk.basepoint import basepoint_at, find_basepoints
    from ppk.discover import find_flights
    from ppk.rinex import find_base_candidates
    from ppk.estpos import order_for_span, plan_basepoint_orders, format_order
    d = _basepoint_dir(tmp_path, with_mrk=True)
    bp = basepoint_at(d)
    assert bp and bp.name == "site-basepoint" and len(bp.logs) == 2 and len(bp.sessions()) == 1 and len(bp.converted()) == 1
    assert bp.sessions()[0].obs.name == f"{STEM}.OBS" and bp.nav_for(bp.sessions()[0]).name == f"{STEM}.NAV"
    assert [m.name for m in bp.calibration_logs()] == ["DRTK3_0039_20260912081500_8PHDN9B00AG8UN.MRK"]
    assert basepoint_at(tmp_path / "nothing") is None
    assert [b.name for b in find_basepoints(tmp_path)] == ["site-basepoint"]
    assert basepoint_at(d / f"{STEM}.OBS").sessions()[0].stem == STEM  # a session file works too
    # the station's files are neither a drone flight nor a base candidate, even with an OBS/NAV/MRK triplet
    (d / f"{STEM}.MRK").write_text((d / "DRTK3_0039_20260912081500_8PHDN9B00AG8UN.MRK").read_text())
    assert find_flights(tmp_path) == [] and find_base_candidates(d) == []
    assert (d / f"{STEM}.OBS").resolve() in bp.own_files()
    orders = plan_basepoint_orders(bp, buffer_minutes=5)
    assert len(orders) == 1
    order, logs = orders[0]
    assert logs == [d / f"{STEM}.OBS"]
    assert order.start_utc == datetime(2026, 9, 12, 6, 15) and order.end_utc == datetime(2026, 9, 12, 8, 45)
    assert order.lat == pytest.approx(58.40, abs=0.05) and order.height == pytest.approx(ecef_to_llh(2991651.1225, 1507361.2274, 5409467.2541)[2])
    assert "broadcast" in order.height_source
    o2 = order_for_span(datetime(2026, 9, 12, 6, 30), datetime(2026, 9, 12, 8, 29, 59), 58.4, 26.7, 100.0, "x", 5)
    assert (o2.start_utc, o2.end_utc) == (order.start_utc, order.end_utc)
    text = format_order(order, logs)
    assert "base point: site-basepoint" in text and "ppk base-survey" in text
    # a session longer than the portal limit is cut to its first hours
    (d / f"{STEM}.OBS").write_text(_log_obs((FIX / "base_header.26o").read_text(),
                                            _epochs(datetime(2026, 9, 12, 6, 30), datetime(2026, 9, 12, 18, 29, 59), 60)))
    order, _ = plan_basepoint_orders(bp, buffer_minutes=5, max_hours=6)[0]
    assert order.duration <= timedelta(hours=6)
    # a session shorter than 10 min is not surveyed and gets no order
    (d / f"{STEM}.OBS").write_text(_log_obs((FIX / "base_header.26o").read_text(),
                                            _epochs(datetime(2026, 9, 12, 6, 30), datetime(2026, 9, 12, 6, 32), 1)))
    assert bp.converted() and not bp.surveyable()
    with pytest.raises(FileNotFoundError):
        plan_basepoint_orders(bp)


def test_calibration_log_summary(tmp_path):
    from ppk.basepoint import basepoint_at, calibration_summary, format_calibration
    bp = basepoint_at(_basepoint_dir(tmp_path, with_mrk=True))
    cal = calibration_summary(bp.calibration_logs()[0])
    assert cal["rows"] == 12 and cal["q"] == {"1": 10, "16": 2}
    assert cal["final"]["h"] == 75.865 and cal["distinct_positions"] == 2 and cal["settle_minutes"] == round(25 / 60, 1)
    assert cal["drift_from_first_hold_mm"]["up"] == -740 and abs(cal["drift_from_first_hold_mm"]["east"]) > 2000
    text = "\n".join(format_calibration(cal))
    assert "held position: 58.40039533" in text and "inherited a worse position" in text


def test_dat_only_session_needs_conversion(tmp_path):
    from ppk.basepoint import basepoint_at
    from ppk.estpos import plan_basepoint_orders
    bp = basepoint_at(_basepoint_dir(tmp_path, native=False))
    assert len(bp.sessions()) == 1 and bp.converted() == []
    with pytest.raises(FileNotFoundError):
        plan_basepoint_orders(bp)


def test_convbin_invocation(tmp_path, monkeypatch):
    import subprocess
    from ppk import basepoint as bpm
    d = _basepoint_dir(tmp_path, native=False)
    bp = bpm.basepoint_at(d)
    slog = bp.sessions()[0]
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text("fake obs\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="1006 (12)  1075 (3600)\n")
    monkeypatch.setattr(bpm.subprocess, "run", fake_run)
    obs, ran = bpm.prepare_log(bp, slog)
    assert ran and obs.name == f"{STEM}.obs" and obs.read_text() == "fake obs\n"
    cmd = calls[0]
    assert cmd[1:5] == ["-r", "rtcm3", "-v", "3.04"] and "-tr" in cmd and cmd[-1].endswith(".dat")
    assert cmd[cmd.index("-hm") + 1] == "site-basepoint"
    assert "1006 (12)" in (d / f"{STEM}_convbin.log").read_text()
    obs2, ran2 = bpm.prepare_log(bp, slog)  # RINEX newer than the DAT: not converted again
    assert not ran2 and len(calls) == 1 and bp.converted() == [(slog, obs)]
    native = bpm.basepoint_at(_basepoint_dir(tmp_path, name="native"))
    assert bpm.prepare_log(native, native.sessions()[0]) == (native.sessions()[0].obs, False)  # the station's OBS is used as is

    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="error\n")
    monkeypatch.setattr(bpm.subprocess, "run", failing)
    with pytest.raises(RuntimeError):
        bpm.prepare_log(bp, slog, force=True)


def test_reduce_static_levels():
    from ppk.basepoint import reduce_static
    from ppk.pos import PosRow
    t0 = datetime(2026, 9, 12, 6, 30)
    lat, lon, h = 58.4, 26.7, 100.0

    def rows(n_fix, n_float):
        out = []
        for i in range(n_fix + n_float):
            q = 1 if i < n_fix else 2
            j = i - n_fix / 2
            out.append(PosRow(t0 + timedelta(seconds=i), lat + j * 1e-9, lon - j * 1e-9, h + (j % 3) * 0.001, q, 20, 0.002, 0.002, 0.005))
        return out
    r = reduce_static(rows(150, 5))
    assert r["level"] == "PASS" and r["fixed"] == 150 and r["epochs"] == 155
    assert r["lat"] == pytest.approx(lat, abs=1e-7) and r["h"] == pytest.approx(h, abs=0.002)
    assert r["spread_mm"]["north"] < 10 and r["spread_mm"]["up"] < 2 and r["mean_sd_mm"]["up"] == 5.0
    assert reduce_static(rows(60, 40))["level"] == "WARN"
    assert reduce_static(rows(40, 60))["level"] == "FAIL"
    assert reduce_static(rows(50, 0))["level"] == "WARN"  # all fixed, but too few epochs
    assert reduce_static([])["level"] == "FAIL" and reduce_static(rows(1, 3))["level"] == "FAIL"


def test_derive_point_arithmetic():
    from ppk.basepoint import derive_point, dms_text
    bc = (59.4, 24.7, 50.0)
    surveyed = apply_ned_offset(*bc, 0.3, -0.2, -0.1)  # the true phase centre is 30 cm north, 20 cm west, 10 cm higher
    shown = (59.4000001, 24.7000002, 48.06)
    p = derive_point(bc, surveyed, shown, 1.8, None)
    cm = p["correction_mm"]
    assert cm["north"] == pytest.approx(300, abs=1) and cm["east"] == pytest.approx(-200, abs=1) and cm["up"] == pytest.approx(100, abs=0.5)
    assert cm["horizontal"] == pytest.approx(360.6, abs=1)
    assert p["k_m"] == pytest.approx(0.14)
    g = p["ground"]
    dn, de, du = ned_difference(*shown, g["lat"], g["lon"], g["h"])
    assert (dn, de, du) == pytest.approx((0.3, -0.2, 0.1), abs=0.001) and g["pole_m"] == 1.8
    assert p["antenna"]["h"] == pytest.approx(surveyed[2], abs=1e-6)
    # no shown coordinates, but the phase-centre offset is known from an earlier survey
    p2 = derive_point(bc, surveyed, None, 1.8, 0.14)
    assert p2["ground"]["h"] == pytest.approx(surveyed[2] - 1.94) and p2["ground"]["lat"] == pytest.approx(surveyed[0], abs=1e-9)
    # nothing known: only the surveyed phase centre
    p3 = derive_point(None, surveyed, None, None, None)
    assert "ground" not in p3 and "correction_mm" not in p3
    # an explicit (drone-derived) correction replaces the survey's, the survey's is kept for the record
    fc = {"north": 300.0, "east": -200.0, "up": 10.0, "horizontal": 360.6}
    p4 = derive_point(bc, surveyed, shown, 1.8, None, correction=fc)
    assert p4["correction_mm"] == fc and p4["survey_correction_mm"]["up"] == pytest.approx(100, abs=0.5)
    assert p4["ground"]["h"] == pytest.approx(shown[2] + 0.010) and p4["antenna"]["h"] == pytest.approx(50.010)
    assert dms_text(59.5, -24.25).startswith("59° 30' 00.0000\" N   24° 15' 00.0000\" W")


def test_choose_correction():
    from ppk.basepoint import choose_correction, flight_correction
    survey = {"north": -336.0, "east": -162.0, "up": -133.0, "horizontal": 373.0}
    cc = [{"flight": "DJI_x", "north": -324.0, "east": -158.0, "up": -46.0}, {"flight": "DJI_x", "north": -320.0, "east": -160.0, "up": -50.0}]
    fl = flight_correction(cc)
    assert fl["north"] == -322.0 and fl["up"] == -48.0 and fl["flights"] == ["DJI_x"]
    corr, src, why = choose_correction(survey, fl)
    assert src == "flight" and corr is fl and "agree" in why  # horizontal parts agree: the drone's own number wins
    assert choose_correction(survey, fl, "survey")[1] == "survey"
    assert choose_correction(survey, None)[1] == "survey"
    assert choose_correction(None, fl)[1] == "none"
    far = dict(fl, north=-200.0)  # 12 cm off horizontally: another base position during the flight, not trusted
    corr, src, why = choose_correction(survey, far)
    assert src == "survey" and "not trusted" in why
    assert choose_correction(survey, far, "flight")[1] == "flight"
    assert flight_correction([]) is None


def test_basepoint_report_and_status(tmp_path):
    import json, os, time
    from ppk.basepoint import basepoint_at, format_report
    from ppk.status import basepoint_status, scan_status, format_status, status_json
    d = _basepoint_dir(tmp_path)
    bp = basepoint_at(d)
    st = basepoint_status(bp, None)
    assert st.kind == "basepoint" and st.next == "survey" and st.base == "missing" and st.sessions == 1
    assert "06:30" in st.flown or "09:30" in st.flown  # GPST or local
    assert st.count_text == "1 session" and "not surveyed" in st.result
    # the station's own RINEX must never count as its base; a real covering base does
    base = d / "vrnx.26o"
    base.write_text((FIX / "base_header.26o").read_text() + "> 2026 09 12 06 00  0.0000000  0  1\n> 2026 09 12 09 00  0.0000000  0  1\n")
    (d / "vrnx.26n").write_text("")
    st = basepoint_status(bp, None)
    assert st.base == "vrnx.26o" and st.base_ok and st.next == "survey"
    sessions = [{"stem": STEM, "obs": f"{STEM}.OBS", "base": "vrnx.26o", "span": "x",
                 "static": {"level": "PASS", "verdict": "3000 fixed epochs (100.0 %)", "lat": 58.4, "lon": 26.7, "h": 100.0,
                            "spread_mm": {"north": 1.0, "east": 1.2, "up": 2.5}, "fixed": 3000},
                 "broadcast": {"lat": 58.4, "lon": 26.7, "h": 100.3},
                 "correction_mm": {"north": 412.0, "east": -10.0, "up": -300.0, "horizontal": 412.1}}]
    point = {"surveyed": {"lat": 58.4, "lon": 26.7, "h": 100.0}, "correction_mm": sessions[0]["correction_mm"], "k_m": 0.141,
             "result_stem": STEM, "antenna": {"lat": 58.4, "lon": 26.7, "h": 100.0}, "correction_reason": "the survey (static solution)",
             "ground": {"lat": 58.4, "lon": 26.7, "h": 98.059, "pole_m": 1.8, "method": "shown coordinates + correction"}}
    settings = {"name": "yard", "pole_m": 1.8, "shown": {"lat": 58.4, "lon": 26.7, "h": 98.359}}
    cc = [{"flight": "DJI_x", "north": 400.0, "east": 0.0, "up": -290.0, "diff_mm": 18.0, "level": "PASS"}]
    text = format_report(bp, sessions, point, settings, cc)
    assert "Enter in DJI Pilot 2" in text and "Latitude            58.40000000" in text and "Pole height         1.800 m" in text
    assert "41.2 cm horizontal" in text and "Cross-check, flight DJI_x" in text and "0.141 m  (plausible)" in text
    settings["surveyed"] = {"at": "2026-09-13T10:00:00", "sessions": sessions, **point, "crosschecks": cc}
    bp.save_settings(settings)
    st = basepoint_status(bp, None)
    assert st.next == "done" and "correction 41.2 cm H / -30.0 cm up" in st.result and "Manual Calibration" in st.result
    time.sleep(0.01); os.utime(d / f"{STEM}.OBS", None)  # a newer session file: survey again
    st = basepoint_status(bp, None)
    assert st.next == "survey" and "outdated" in st.result
    # scan_status lists flights and base points side by side
    f = tmp_path / "DJI_flight"; f.mkdir()
    import shutil
    shutil.copy(FIX / "sample.obs", f / "DJI_a.OBS"); shutil.copy(FIX / "sample.MRK", f / "DJI_a.MRK"); (f / "DJI_a.NAV").write_text("")
    rows = scan_status(tmp_path, None)
    assert [(r.folder, r.kind) for r in rows] == [("DJI_flight", "flight"), ("site-basepoint", "basepoint")]
    table = format_status(rows)
    assert "1 session" in table and "'survey'" in table
    assert json.loads(status_json(rows))[1]["kind"] == "basepoint"


def test_check_base_against_log(tmp_path):
    from ppk.basepoint import basepoint_at
    from ppk.estpos import check_base
    d = _basepoint_dir(tmp_path)
    bp = basepoint_at(d)
    base = d / "vrnx.26o"
    base.write_text((FIX / "base_header.26o").read_text() + "> 2026 09 12 06 00  0.0000000  0  1\n> 2026 09 12 09 00  0.0000000  0  1\n")
    _, checks = check_base(base, None, rover_obs=bp.sessions()[0].obs)
    msgs = [c.message for c in checks]
    assert any("log span" in m and "covered" in m and "NOT" not in m for m in msgs)
    assert any(m.startswith("baseline to the station's broadcast position 0.0 m") for m in msgs)
    assert all(c.level != "FAIL" for c in checks)


def test_flight_crosschecks(tmp_path):
    import json, shutil
    from ppk.basepoint import flight_crosschecks
    f = tmp_path / "DJI_flight"; f.mkdir()
    shutil.copy(FIX / "sample.obs", f / "DJI_a.OBS"); shutil.copy(FIX / "sample.MRK", f / "DJI_a.MRK"); (f / "DJI_a.NAV").write_text("")
    (f / "ppk").mkdir(); (f / "ppk" / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "rtk_vs_ppk": {
        "north": {"mean_mm": 410.0}, "east": {"mean_mm": -5.0}, "up": {"mean_mm": -280.0}}}))
    corr = {"north": 412.0, "east": -10.0, "up": -300.0}
    cc = flight_crosschecks(tmp_path, datetime(2026, 9, 12, 6, 30), datetime(2026, 9, 12, 8, 30), corr)
    assert len(cc) == 1 and cc[0]["flight"] == "DJI_flight" and cc[0]["level"] == "PASS" and cc[0]["diff_mm"] == pytest.approx(20.7, abs=0.1)
    assert flight_crosschecks(tmp_path, datetime(2026, 9, 13, 6, 30), datetime(2026, 9, 13, 8, 30), corr) == []  # another day
    cc = flight_crosschecks(tmp_path, datetime(2026, 9, 12, 6, 30), datetime(2026, 9, 12, 8, 30), {"north": 0.0, "east": 0.0, "up": 0.0})
    assert cc[0]["level"] == "WARN"


def test_base_survey_cli_inspect(tmp_path, monkeypatch):
    import subprocess, json
    from ppk import basepoint as bpm
    from ppk.cli import main
    d = _basepoint_dir(tmp_path, native=False, with_mrk=True)
    hdr = (FIX / "base_header.26o").read_text()

    def fake_run(cmd, **kw):
        Path(cmd[cmd.index("-o") + 1]).write_text(_log_obs(hdr, _epochs(datetime(2026, 9, 12, 6, 30), datetime(2026, 9, 12, 8, 29, 59))))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n")
    monkeypatch.setattr(bpm.subprocess, "run", fake_run)
    rc = main(["base-survey", str(d), "--inspect", "--pole", "1.8", "--shown", "58.4", "26.7", "98.36", "--name", "yard"])
    assert rc == 0
    s = json.loads((d / "basepoint.json").read_text())
    assert s["pole_m"] == 1.8 and s["shown"]["h"] == 98.36 and s["name"] == "yard"
    assert (d / f"{STEM}.obs").exists()
    assert main(["base-survey", str(d)]) == 2  # no base file: the order instructions, exit 2
    (tmp_path / "empty").mkdir()
    assert main(["base-survey", str(tmp_path / "empty")]) == 1


def test_basepoint_inside_flight_folder(tmp_path):
    """A station folder kept inside its flight folder is found, labelled by its relative path, reuses the flight's
    Virtual RINEX when it covers the session, and its files never leak into the flight's own base search."""
    import shutil
    from ppk.basepoint import find_basepoints, basepoint_at
    from ppk.discover import find_flights
    from ppk.rinex import find_base_candidates
    from ppk.status import scan_status
    from ppk.cli import basepoint_bases
    f = tmp_path / "DJI_202609120900_040_yard"; f.mkdir()
    shutil.copy(FIX / "sample.obs", f / "DJI_a.OBS"); shutil.copy(FIX / "sample.MRK", f / "DJI_a.MRK"); (f / "DJI_a.NAV").write_text("")
    d = _basepoint_dir(f, name="d-rtk3")
    bps = find_basepoints(tmp_path)
    assert [b.label(tmp_path) for b in bps] == ["DJI_202609120900_040_yard/d-rtk3"]
    assert bps[0].parent_flight_dir() == f and basepoint_at(tmp_path / "x") is None
    assert [fl.directory for fl in find_flights(tmp_path)] == [f]  # the nested station folder is not a flight
    rows = scan_status(tmp_path, None)
    assert [(r.folder, r.kind, r.base) for r in rows] == [("DJI_202609120900_040_yard", "flight", "missing"),
                                                         ("DJI_202609120900_040_yard/d-rtk3", "basepoint", "missing")]
    # the flight's Virtual RINEX covers the station session: reused, nothing to order
    base = f / "virt255g15.26o"
    base.write_text((FIX / "base_header.26o").read_text() + "> 2026 09 12 06 00  0.0000000  0  1\n> 2026 09 12 09 00  0.0000000  0  1\n")
    (f / "virt255g15.26n").write_text("")
    rows = scan_status(tmp_path, None)
    assert rows[1].base == "virt255g15.26o" and rows[1].next == "survey"
    bp = bps[0]
    assert list(basepoint_bases(bp, None).values()) == [base]
    assert find_base_candidates(f) == [base]  # the station's OBS in the sub-folder is not seen by the flight
