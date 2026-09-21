"""Write per-flight results: events.csv, geo.txt (WebODM), summary.json."""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .mrk import MrkEvent
from .offsets import apply_ned_offset
from .pos import PosRow
from .timeutil import fmt

MATCH_TOLERANCE_S = 0.0015


@dataclass
class CameraEvent:
    id: int
    image: str
    time: datetime
    tow: float
    week: int
    lat: float
    lon: float
    ellh: float
    q: int
    ns: int
    sdn: float
    sde: float
    sdu: float
    ratio: float
    ant_lat: float
    ant_lon: float
    ant_h: float
    off_n_mm: float
    off_e_mm: float
    off_v_mm: float
    mrk_lat: float
    mrk_lon: float
    mrk_ellh: float
    mrk_q: int


def match_events(mrk: list[MrkEvent], rows: list[PosRow], images: dict[int, Path]) -> tuple[list[CameraEvent], list[MrkEvent]]:
    """Pair MRK events with RTKLIB event solutions by time and apply the camera lever arm."""
    rows = sorted(rows, key=lambda r: r.time)
    times = [r.time for r in rows]
    matched: list[CameraEvent] = []
    unmatched: list[MrkEvent] = []
    import bisect
    for ev in mrk:
        t = ev.time
        i = bisect.bisect_left(times, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(rows):
                dt = abs((rows[j].time - t).total_seconds())
                if dt <= MATCH_TOLERANCE_S and (best is None or dt < best[0]):
                    best = (dt, rows[j])
        if best is None or best[1].q == 0:
            unmatched.append(ev)
            continue
        r = best[1]
        lat, lon, h = apply_ned_offset(r.lat, r.lon, r.h, ev.north_mm / 1000, ev.east_mm / 1000, ev.down_mm / 1000)
        img = images.get(ev.id)
        matched.append(CameraEvent(
            id=ev.id, image=img.name if img else f"#{ev.id:04d}", time=t, tow=ev.tow, week=ev.week,
            lat=lat, lon=lon, ellh=h, q=r.q, ns=r.ns, sdn=r.sdn, sde=r.sde, sdu=r.sdu, ratio=r.ratio,
            ant_lat=r.lat, ant_lon=r.lon, ant_h=r.h,
            off_n_mm=ev.north_mm, off_e_mm=ev.east_mm, off_v_mm=ev.down_mm,
            mrk_lat=ev.lat, mrk_lon=ev.lon, mrk_ellh=ev.ellh, mrk_q=ev.q,
        ))
    return matched, unmatched


def write_events_csv(path: Path, events: list[CameraEvent]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "image", "gpst", "tow", "week", "lat", "lon", "ellh", "q", "ns", "sdn", "sde", "sdu", "ratio",
                    "ant_lat", "ant_lon", "ant_h", "off_n_mm", "off_e_mm", "off_v_mm", "mrk_lat", "mrk_lon", "mrk_ellh", "mrk_q"])
        for e in events:
            w.writerow([e.id, e.image, fmt(e.time, 6), f"{e.tow:.6f}", e.week,
                        f"{e.lat:.10f}", f"{e.lon:.10f}", f"{e.ellh:.4f}", e.q, e.ns,
                        f"{e.sdn:.4f}", f"{e.sde:.4f}", f"{e.sdu:.4f}", f"{e.ratio:.1f}",
                        f"{e.ant_lat:.10f}", f"{e.ant_lon:.10f}", f"{e.ant_h:.4f}",
                        f"{e.off_n_mm:g}", f"{e.off_e_mm:g}", f"{e.off_v_mm:g}",
                        f"{e.mrk_lat:.8f}", f"{e.mrk_lon:.8f}", f"{e.mrk_ellh:.3f}", e.mrk_q])


def read_events_csv(path: Path) -> list[CameraEvent]:
    out: list[CameraEvent] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(CameraEvent(
                id=int(row["id"]), image=row["image"],
                time=datetime.strptime(row["gpst"], "%Y/%m/%d %H:%M:%S.%f"), tow=float(row["tow"]), week=int(row["week"]),
                lat=float(row["lat"]), lon=float(row["lon"]), ellh=float(row["ellh"]), q=int(row["q"]), ns=int(row["ns"]),
                sdn=float(row["sdn"]), sde=float(row["sde"]), sdu=float(row["sdu"]), ratio=float(row["ratio"]),
                ant_lat=float(row["ant_lat"]), ant_lon=float(row["ant_lon"]), ant_h=float(row["ant_h"]),
                off_n_mm=float(row["off_n_mm"]), off_e_mm=float(row["off_e_mm"]), off_v_mm=float(row["off_v_mm"]),
                mrk_lat=float(row["mrk_lat"]), mrk_lon=float(row["mrk_lon"]), mrk_ellh=float(row["mrk_ellh"]), mrk_q=int(row["mrk_q"]),
            ))
    return out


def write_geo_txt(path: Path, events: list[CameraEvent], with_accuracy: bool = False, fixed_only: bool = False) -> int:
    """WebODM/ODM geo.txt: 'EPSG:4326' then '<image> <lon> <lat> <ellh> [horiz_acc vert_acc]' per image."""
    n = 0
    with open(path, "w") as fh:
        fh.write("EPSG:4326\n")
        for e in events:
            if fixed_only and e.q != 1:
                continue
            if not e.image or e.image.startswith("#"):
                continue
            line = f"{e.image} {e.lon:.9f} {e.lat:.9f} {e.ellh:.4f}"
            if with_accuracy:
                line += f" {math.hypot(e.sdn, e.sde):.3f} {e.sdu:.3f}"
            fh.write(line + "\n")
            n += 1
    return n


def write_summary(path: Path, summary: dict) -> None:
    def default(o):
        if isinstance(o, datetime):
            return fmt(o, 3)
        if isinstance(o, Path):
            return str(o)
        return str(o)
    path.write_text(json.dumps(summary, indent=2, default=default) + "\n")
