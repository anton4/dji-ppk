"""GPS time helpers. All datetimes are naive and represent GPST unless stated otherwise."""
from __future__ import annotations

from datetime import datetime, timedelta

GPS_EPOCH = datetime(1980, 1, 6)
WEEK_SECONDS = 7 * 86400


def gps_to_datetime(week: int, tow: float) -> datetime:
    return GPS_EPOCH + timedelta(weeks=week, seconds=tow)


def datetime_to_gps(dt: datetime) -> tuple[int, float]:
    delta = dt - GPS_EPOCH
    total = delta.days * 86400 + delta.seconds + delta.microseconds / 1e6
    week = int(total // WEEK_SECONDS)
    return week, total - week * WEEK_SECONDS


def datetime_to_tow(dt: datetime) -> float:
    return datetime_to_gps(dt)[1]


def parse_rinex3_epoch(line: str) -> tuple[datetime, int, int]:
    """Parse a RINEX 3 epoch record '> yyyy mm dd hh mm ss.sssssss  f nnn'.

    Returns (time, epoch_flag, num_sats). Raises ValueError on malformed lines.
    """
    if not line.startswith(">"):
        raise ValueError(f"not an epoch line: {line!r}")
    t = line[1:29].split()
    if len(t) != 6:
        raise ValueError(f"bad epoch time: {line!r}")
    year, month, day, hour, minute = (int(x) for x in t[:5])
    sec = float(t[5])
    whole = int(sec)
    micro = int(round((sec - whole) * 1e6))
    if micro >= 1_000_000:
        whole += 1
        micro -= 1_000_000
    base = datetime(year, month, day, hour, minute) + timedelta(seconds=whole, microseconds=micro)
    flag = int(line[31:32] or 0)
    nsat_txt = line[32:35].strip()
    nsat = int(nsat_txt) if nsat_txt else 0
    return base, flag, nsat


def format_rinex3_event(time: datetime) -> str:
    """RINEX 3 external-event record (epoch flag 5, no special records), same layout RTKLIB writes."""
    sec = time.second + time.microsecond / 1e6
    return "> %4d %2d %2d %2d %2d%11.7f  %d%3d" % (
        time.year, time.month, time.day, time.hour, time.minute, sec, 5, 0)


def parse_pos_time(date_str: str, time_str: str) -> datetime:
    """Parse RTKLIB .pos 'yyyy/mm/dd hh:mm:ss.ffffff' (any number of decimals)."""
    if "." in time_str:
        hms, frac = time_str.split(".", 1)
        frac = (frac + "000000")[:6]
    else:
        hms, frac = time_str, "000000"
    return datetime.strptime(f"{date_str} {hms}.{frac}", "%Y/%m/%d %H:%M:%S.%f")


def fmt(dt: datetime, decimals: int = 3) -> str:
    s = dt.strftime("%Y/%m/%d %H:%M:%S.%f")
    return s[: len(s) - (6 - decimals)] if decimals < 6 else s


import os as _os
from datetime import timezone as _timezone
from zoneinfo import ZoneInfo as _ZoneInfo

GPS_UTC_LEAP_SECONDS = 18  # valid since 2017-01-01


def local_tz():
    """Time zone for human readable output: $PPK_TZ, else $TZ, else Europe/Tallinn."""
    name = _os.environ.get("PPK_TZ") or _os.environ.get("TZ") or "Europe/Tallinn"
    try:
        return _ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return _ZoneInfo("Europe/Tallinn")


def gpst_to_local(t: datetime) -> datetime:
    """Naive GPST -> aware local time (GPST is UTC + 18 s)."""
    return (t - timedelta(seconds=GPS_UTC_LEAP_SECONDS)).replace(tzinfo=_timezone.utc).astimezone(local_tz())


def span_local(first: datetime, last: datetime, seconds: bool = False) -> str:
    """'2026-09-18 17:18 - 17:38 EEST' from two naive GPST datetimes."""
    a, b = gpst_to_local(first), gpst_to_local(last)
    if not seconds:  # round to the nearest minute so 13:59:42 UTC reads as 17:00, not 16:59
        a = (a + timedelta(seconds=30)).replace(second=0, microsecond=0)
        b = (b + timedelta(seconds=30)).replace(second=0, microsecond=0)
    f = "%H:%M:%S" if seconds else "%H:%M"
    day = "" if a.date() == b.date() else f"{b:%Y-%m-%d} "
    return f"{a:%Y-%m-%d} {a:{f}} - {day}{b:{f}} {a.tzname()}"
