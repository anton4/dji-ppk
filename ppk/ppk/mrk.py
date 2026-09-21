"""Parser for DJI Timestamp .MRK files.

Row layout (tab separated):
  id  tow  [week]  N,N  E,E  V,V  lat,Lat  lon,Lon  h,Ellh  sdN, sdE, sdV  Q,Q

N/E/V are the offsets in millimetres from the GNSS antenna phase centre to the camera
CMOS centre: positive N = camera north of antenna, positive E = east, positive V = camera
BELOW the antenna. Camera position = antenna position + N, + E, height - V.
lat/lon/ellh are the drone's real-time (RTK) positions of the camera at exposure.
Q: 50 = RTK fixed, 16 = single/float (as reported by DJI), 34/35 = float variants.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .timeutil import gps_to_datetime

_RE_TAG = {
    "north_mm": re.compile(r"(-?\d+(?:\.\d+)?),N\b"),
    "east_mm": re.compile(r"(-?\d+(?:\.\d+)?),E\b"),
    "down_mm": re.compile(r"(-?\d+(?:\.\d+)?),V\b"),
    "lat": re.compile(r"(-?\d+\.\d+),Lat\b"),
    "lon": re.compile(r"(-?\d+\.\d+),Lon\b"),
    "ellh": re.compile(r"(-?\d+\.\d+),Ellh\b"),
    "q": re.compile(r"(\d+),Q\b"),
}
_RE_STD = re.compile(r"(\d+\.\d+),\s*(\d+\.\d+),\s*(\d+\.\d+)\s+\d+,Q")


@dataclass
class MrkEvent:
    id: int
    tow: float
    week: int
    north_mm: float
    east_mm: float
    down_mm: float
    lat: float
    lon: float
    ellh: float
    std_n: float
    std_e: float
    std_v: float
    q: int

    @property
    def time(self) -> datetime:
        return gps_to_datetime(self.week, self.tow)


def parse_mrk(path: str | Path) -> list[MrkEvent]:
    events: list[MrkEvent] = []
    with open(path, "r", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            tok = line.split()
            if len(tok) < 3 or not tok[0].isdigit():
                continue
            try:
                vals = {k: float(rx.search(line).group(1)) for k, rx in _RE_TAG.items()}
            except AttributeError as exc:
                raise ValueError(f"{path}:{lineno}: cannot parse MRK row: {line!r}") from exc
            m = _RE_STD.search(line)
            std = tuple(float(x) for x in m.groups()) if m else (float("nan"),) * 3
            events.append(MrkEvent(
                id=int(tok[0]), tow=float(tok[1]), week=int(tok[2].strip("[]")),
                north_mm=vals["north_mm"], east_mm=vals["east_mm"], down_mm=vals["down_mm"],
                lat=vals["lat"], lon=vals["lon"], ellh=vals["ellh"],
                std_n=std[0], std_e=std[1], std_v=std[2], q=int(vals["q"]),
            ))
    if not events:
        raise ValueError(f"{path}: no events found")
    return events
