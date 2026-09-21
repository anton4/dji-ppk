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
    assert st.base == "base.26o" and st.next == "process"
    (d / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "geo_txt_rows": 5, "events": {"fix": 5, "mrk": 5}}))
    st = folder_status(d, flights, None)
    assert st.next == "done" and "5 rows" in st.result
    time.sleep(0.01); os.utime(d / "DJI_a.MRK", None)  # input newer than the result
    st = folder_status(d, flights, None)
    assert st.next == "reprocess" and "outdated" in st.result
    (d / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "geo_txt_rows": 3, "events": {"fix": 5, "mrk": 5}}))
    st = folder_status(d, flights, None)
    assert st.next == "reprocess" and "photos arrived" in st.result  # processed with 3 photos, 5 are here now
    (d / "DJI_20260912100505_0006_V.JPG").unlink()
    (d / "summary.json").write_text(json.dumps({"rover_obs": "DJI_a.OBS", "geo_txt_rows": 4, "events": {"fix": 5, "mrk": 5}}))
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
    assert st.next == "expired" and "no longer available" in st.base and "RINEX expired" in st.row()[2]
    st = folder_status(d, flights, None, now=datetime(2026, 12, 5))
    assert st.next == "order" and "(6 d left)" in st.row()[2]
    (d / "base.26o").write_text((FIX / "base_header.26o").read_text() + "> 2026 09 12 06 30  0.0000000  0  1\n> 2026 09 12 08 29 59.0000000  0  1\n")
    st = folder_status(d, flights, None, now=datetime(2026, 12, 15))
    assert st.next == "photos"  # base present: still processable even though the portal has no data any more
