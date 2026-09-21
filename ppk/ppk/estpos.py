"""ESTPOS (Maa-amet, Leica Spider Business Center) helpers.

There is no API: Virtual RINEX is ordered in the browser at https://gnss-rtk.maaamet.ee/sbc
(Post Processing -> RINEX Data -> tick "Virtual RINEX", enter lat/lon/ellipsoidal height,
date, start time on a quarter hour, and file length). This module computes those inputs from a
flight and validates a downloaded base file against the flight.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .discover import Flight
from .mrk import parse_mrk
from .rinex import RinexHeader, read_header, scan_obs_span
from .timeutil import span_local, local_tz

PORTAL_URL = "https://gnss-rtk.maaamet.ee/sbc"
LOCAL_TZ = local_tz()  # Europe/Tallinn unless TZ / PPK_TZ says otherwise
GPS_UTC_LEAP_SECONDS = 18  # valid since 2017-01-01
QUARTER = timedelta(minutes=15)

_XMP_ABS = re.compile(rb'drone-dji:AbsoluteAltitude="([+-]?\d+(?:\.\d+)?)"')
_XMP_REL = re.compile(rb'drone-dji:RelativeAltitude="([+-]?\d+(?:\.\d+)?)"')


@dataclass
class EstposOrder:
    lat: float
    lon: float
    height: float
    height_source: str
    flight_first_gpst: datetime
    flight_last_gpst: datetime
    start_utc: datetime
    end_utc: datetime

    @property
    def duration(self) -> timedelta:
        return self.end_utc - self.start_utc

    @property
    def start_local(self) -> datetime:
        return self.start_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)

    @property
    def end_local(self) -> datetime:
        return self.end_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def gpst_to_utc(t: datetime) -> datetime:
    return t - timedelta(seconds=GPS_UTC_LEAP_SECONDS)


def floor_quarter(t: datetime) -> datetime:
    return t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)


def ceil_quarter(t: datetime) -> datetime:
    f = floor_quarter(t)
    return f if f == t else f + QUARTER


def ground_height_from_image(path: Path) -> float | None:
    """DJI XMP: ground ellipsoidal height ~= AbsoluteAltitude - RelativeAltitude (both in metres)."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read(256 * 1024)
    except OSError:
        return None
    a, r = _XMP_ABS.search(blob), _XMP_REL.search(blob)
    if a and r:
        return float(a.group(1)) - float(r.group(1))
    return None


def _as_list(flights) -> list[Flight]:
    return list(flights) if isinstance(flights, (list, tuple)) else [flights]


def plan_order(flights, buffer_minutes: int = 5, height_override: float | None = None) -> EstposOrder:
    """One Virtual RINEX order covering one session or every session of a flight day (list of Flight)."""
    flights = _as_list(flights)
    first = last = None
    mrk = []
    for fl in flights:
        header = read_header(fl.obs)
        f, l, _ = scan_obs_span(fl.obs, header)
        f = f or header.first_obs
        l = l or header.last_obs
        if not f or not l:
            raise ValueError(f"{fl.obs}: cannot determine observation span")
        first = f if first is None or f < first else first
        last = l if last is None or l > last else last
        mrk += parse_mrk(fl.mrk)
    lat = sum(e.lat for e in mrk) / len(mrk)
    lon = sum(e.lon for e in mrk) / len(mrk)
    if height_override is not None:
        height, src = height_override, "override"
    else:
        height = None
        for fl in flights:
            for idx in sorted(fl.images):
                height = ground_height_from_image(fl.images[idx])
                if height is not None:
                    src = f"AbsoluteAltitude - RelativeAltitude from {fl.images[idx].name}"
                    break
            if height is not None:
                break
        if height is None:
            height = min(e.ellh for e in mrk) - 50.0
            src = "min photo ellipsoidal height - 50 m (no XMP altitude found)"
    buf = timedelta(minutes=buffer_minutes)
    start = floor_quarter(gpst_to_utc(first) - buf)
    end = ceil_quarter(gpst_to_utc(last) + buf)
    return EstposOrder(lat, lon, height, src, first, last, start, end)


def plan_orders(flights, buffer_minutes: int = 5, height_override: float | None = None,
                max_hours: float = 6.0) -> list[tuple[EstposOrder, list[Flight]]]:
    """Orders for a flight day. All sessions share one order when it fits into `max_hours` (the portal limit
    for Virtual RINEX); otherwise sessions are clustered at their gaps so every order stays within the limit.
    A single session longer than the limit still gets one (too long) order and a warning is left to the caller."""
    flights = sorted(_as_list(flights), key=lambda f: f.stem)
    spans = []
    for fl in flights:
        header = read_header(fl.obs)
        f, l, _ = scan_obs_span(fl.obs, header)
        spans.append((f or header.first_obs, l or header.last_obs))
    buf = timedelta(minutes=buffer_minutes)
    clusters: list[list[int]] = []
    for i, (f, l) in enumerate(spans):
        if clusters:
            first = spans[clusters[-1][0]][0]
            start = floor_quarter(gpst_to_utc(first) - buf)
            end = ceil_quarter(gpst_to_utc(l) + buf)
            if (end - start) <= timedelta(hours=max_hours):
                clusters[-1].append(i)
                continue
        clusters.append([i])
    return [(plan_order([flights[i] for i in idx], buffer_minutes, height_override), [flights[i] for i in idx]) for idx in clusters]


def format_order(order: EstposOrder, flights) -> str:
    flights = _as_list(flights)
    flight = flights[0]
    dur_min = int(order.duration.total_seconds() // 60)
    tz = order.start_local.tzname()
    sessions = "" if len(flights) == 1 else f"  ({len(flights)} sessions: " + ", ".join(f.stem for f in flights) + ")"
    lines = [
        "=" * 72,
        " ESTPOS Virtual RINEX order for flight: " + flight.name + sessions,
        "=" * 72,
        f" Portal:            {PORTAL_URL}  (Post Processing -> RINEX Data -> [x] Virtual RINEX)",
        f" Latitude (deg):    {order.lat:.7f}",
        f" Longitude (deg):   {order.lon:.7f}",
        f" Ellips. height:    {order.height:.2f} m   ({order.height_source})",
        f" Date:              {order.start_local:%Y-%m-%d}  ({tz} = Estonian time, what the form and the DJI folder use)",
        f" Start time:        {order.start_local:%H:%M} {tz}   quarter = {order.start_local:%M}",
        f" Length:            {dur_min} min  ({dur_min / 60:.2f} h)  -> pick the next available length in the form",
        f" End time:          {order.end_local:%H:%M} {tz}",
        f" Interval:          1 s (keep default)",
        "-" * 72,
        f" Same in UTC:       {order.start_utc:%Y-%m-%d %H:%M} - {order.end_utc:%H:%M} UTC",
        f" Flight:            {span_local(order.flight_first_gpst, order.flight_last_gpst, seconds=True)}"
        f"  ({order.flight_first_gpst:%H:%M:%S} - {order.flight_last_gpst:%H:%M:%S} GPST)",
        f" Photos:            {sum(len(f.images) for f in flights)}",
        "=" * 72,
        f" After download, copy the .??o/.rnx (or the zip) into {flight.directory} and run:",
        f"   ppk process {flight.directory}",
        " (or keep it in /data/base; it is picked up automatically when it covers the flight)",
    ]
    return "\n".join(lines)


@dataclass
class Check:
    ok: bool
    level: str  # PASS / WARN / FAIL
    message: str


def check_base(base_obs: Path, flight: Flight | None, antex: Path | None = None) -> tuple[RinexHeader, list[Check]]:
    checks: list[Check] = []
    hdr = read_header(base_obs)
    first, last, n = scan_obs_span(base_obs, hdr)
    first = first or hdr.first_obs
    last = last or hdr.last_obs
    checks.append(Check(hdr.version >= 3, "PASS" if hdr.version >= 3 else "WARN",
                        f"RINEX version {hdr.version:.2f} ({hdr.program or 'unknown program'})"))
    if hdr.interval is None and first and last and n > 1:
        hdr.interval = (last - first).total_seconds() / (n - 1)
    if hdr.interval is not None:
        ok = hdr.interval <= 1.0 + 1e-6
        checks.append(Check(ok, "PASS" if ok else "WARN",
                            f"observation interval {hdr.interval:g} s" + ("" if ok else " (1 s recommended for a 5 Hz rover)")))
    checks.append(Check(True, "INFO", f"{'Virtual RINEX' if hdr.is_virtual else 'physical station'}: marker "
                        f"'{hdr.marker_name or '-'}' receiver '{hdr.rec_type or '-'}' antenna '{hdr.ant_type or '-'}'"))
    llh = hdr.approx_llh
    if llh:
        checks.append(Check(True, "PASS", f"base position (header) lat {llh[0]:.7f} lon {llh[1]:.7f} h {llh[2]:.3f} m"))
    else:
        checks.append(Check(False, "FAIL", "APPROX POSITION XYZ missing or zero: set ant2-postype/pos manually"))
    if first and last:
        checks.append(Check(True, "INFO", f"base span {span_local(first, last)} ({first:%H:%M:%S} - {last:%H:%M:%S} GPST), {n} epochs"))
    if flight is not None and first and last:
        fh = read_header(flight.obs)
        f_first, f_last, _ = scan_obs_span(flight.obs, fh)
        f_first = f_first or fh.first_obs
        f_last = f_last or fh.last_obs
        if f_first and f_last:
            covers = first <= f_first and last >= f_last
            checks.append(Check(covers, "PASS" if covers else "FAIL",
                                f"flight span {span_local(f_first, f_last)} ({f_first:%H:%M:%S} - {f_last:%H:%M:%S} GPST) is "
                                f"{'covered' if covers else 'NOT covered'} by the base file"))
            if llh:
                mrk = parse_mrk(flight.mrk)
                lat = sum(e.lat for e in mrk) / len(mrk)
                lon = sum(e.lon for e in mrk) / len(mrk)
                dn = math.radians(lat - llh[0]) * 6371000
                de = math.radians(lon - llh[1]) * 6371000 * math.cos(math.radians(lat))
                dist = math.hypot(dn, de)
                lvl = "PASS" if dist < 15000 else "WARN"
                checks.append(Check(lvl == "PASS", lvl, f"baseline to flight area {dist / 1000:.2f} km"))
    if antex and hdr.ant_type:
        try:
            present = hdr.ant_type[:20].rstrip() in Path(antex).read_text(errors="replace")
        except OSError:
            present = False
        checks.append(Check(present, "PASS" if present else "WARN",
                            f"antenna '{hdr.ant_type}' {'found' if present else 'NOT found'} in ANTEX {Path(antex).name}"))
    return hdr, checks
