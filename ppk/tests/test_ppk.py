"""Tests for the ppk package.

Fixtures are excerpts of real DJI / ESTPOS files. Coordinates are shifted by a few hundred
metres and the raw observations in sample.obs are perturbed, so they cannot be used for positioning.
"""
import math
from datetime import datetime
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
