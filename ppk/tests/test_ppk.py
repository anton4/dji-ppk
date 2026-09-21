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
