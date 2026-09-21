"""Write per-flight results: events.csv, geo.txt (WebODM), summary.json."""
from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .mrk import MrkEvent
from .offsets import apply_ned_offset, ned_difference
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


def _stats_mm(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    v = sorted(values)
    return {"median": round(1000 * statistics.median(v), 1), "p95": round(1000 * v[min(len(v) - 1, int(0.95 * len(v)))], 1),
            "max": round(1000 * v[-1], 1)}


def solution_quality(matched: list[CameraEvent], n_mrk: int, traj_rows: list) -> dict:
    """Quality figures of a solution without any external reference: fix counts, satellites, RTKLIB's own
    standard deviations of the photo positions and the ambiguity ratio test."""
    fixed = [e for e in matched if e.q == 1]
    ns = sorted(e.ns for e in matched)
    ratios = sorted(e.ratio for e in fixed if e.ratio > 0)
    duration = (traj_rows[-1].time - traj_rows[0].time).total_seconds() if len(traj_rows) > 1 else 0.0
    q = {
        "photos": {"mrk": n_mrk, "solved": len(matched), "fixed": len(fixed),
                   "float": sum(1 for e in matched if e.q == 2), "other": sum(1 for e in matched if e.q not in (1, 2)),
                   "unsolved": n_mrk - len(matched)},
        "trajectory": {"epochs": len(traj_rows), "fixed": sum(1 for r in traj_rows if r.q == 1),
                       "float": sum(1 for r in traj_rows if r.q == 2), "seconds": round(duration)},
        "satellites": {"min": ns[0], "median": statistics.median(ns), "max": ns[-1]} if ns else {},
        "std_mm": {"north": _stats_mm([e.sdn for e in fixed]), "east": _stats_mm([e.sde for e in fixed]),
                   "up": _stats_mm([e.sdu for e in fixed]),
                   "horizontal": _stats_mm([math.hypot(e.sdn, e.sde) for e in fixed])},
        "ar_ratio": {"min": round(ratios[0], 1), "median": round(statistics.median(ratios), 1)} if ratios else {},
    }
    return q


def format_quality(q: dict, flight_name: str, base_name: str, baseline_km: float | None, reference: str | None) -> str:
    ph, tr, sat, sd, ar = q["photos"], q["trajectory"], q.get("satellites", {}), q["std_mm"], q.get("ar_ratio", {})
    fix_pct = 100 * ph["fixed"] / ph["mrk"] if ph["mrk"] else 0.0
    traj_pct = 100 * tr["fixed"] / tr["epochs"] if tr["epochs"] else 0.0

    def sd_row(name: str, st: dict[str, float]) -> str:
        return f" {name:<12} median {st['median']:6.1f}   p95 {st['p95']:6.1f}   max {st['max']:6.1f}" if st else f" {name:<12} (no fixed photos)"

    lines = [
        "=" * 78,
        f" Solution quality: {flight_name}",
        "=" * 78,
        f" base:         {base_name}" + (f"   baseline {baseline_km:.2f} km" if baseline_km is not None else ""),
        f" photos:       {ph['fixed']}/{ph['mrk']} fixed ({fix_pct:.1f} %), {ph['float']} float, {ph['other']} other, {ph['unsolved']} unsolved",
        f" trajectory:   {tr['fixed']}/{tr['epochs']} epochs fixed ({traj_pct:.1f} %), {tr['float']} float, {tr['seconds'] // 60:.0f} min {tr['seconds'] % 60:.0f} s",
    ]
    if sat:
        lines.append(f" satellites:   {sat['min']} - {sat['max']} per photo (median {sat['median']:.0f})")
    if ar:
        lines.append(f" AR ratio:     median {ar['median']}, min {ar['min']}  (RTKLIB fix validation, >= 3 required)")
    lines += ["-" * 78, " RTKLIB estimated standard deviation of fixed photo positions, in millimetres:",
              sd_row("north", sd["north"]), sd_row("east", sd["east"]), sd_row("up", sd["up"]), sd_row("horizontal", sd["horizontal"]),
              "-" * 78]
    if reference:
        lines.append(f" reference:    compared with {reference}, see compare_report.txt")
    else:
        lines.append(" reference:    none (no Emlid Studio *_events.pos in the flight folder, so no comparison)")
    lines.append("=" * 78)
    return "\n".join(lines)


DJI_Q_LABEL = {50: "fixed", 16: "float", 34: "float", 1: "single", 0: "none"}


def rtk_vs_ppk(matched: list[CameraEvent]) -> dict:
    """Compare the drone's on-board RTK antenna positions from the .MRK (D-RTK 3 or NTRIP) with the PPK
    antenna positions. The mean is the base position error of the on-board RTK (e.g. a PPP-surveyed D-RTK 3),
    the standard deviation is the RTK noise."""
    rows = [e for e in matched if e.q == 1 and e.mrk_q == 50 and e.mrk_lat and e.mrk_lon]
    if len(rows) < 2:
        return {"n": len(rows), "mrk_q": _count_q(matched)}
    dn, de, du = zip(*(ned_difference(e.mrk_lat, e.mrk_lon, e.mrk_ellh, e.ant_lat, e.ant_lon, e.ant_h) for e in rows))

    def st(v):
        return {"mean_mm": round(1000 * statistics.fmean(v), 1), "std_mm": round(1000 * statistics.pstdev(v), 1),
                "min_mm": round(1000 * min(v), 1), "max_mm": round(1000 * max(v), 1)}
    mn, me, mu = statistics.fmean(dn), statistics.fmean(de), statistics.fmean(du)
    return {"n": len(rows), "mrk_q": _count_q(matched), "north": st(dn), "east": st(de), "up": st(du),
            "offset_horizontal_mm": round(1000 * math.hypot(mn, me), 1), "offset_3d_mm": round(1000 * math.sqrt(mn * mn + me * me + mu * mu), 1),
            "scatter_horizontal_mm": round(1000 * math.hypot(statistics.pstdev(dn), statistics.pstdev(de)), 1)}


def _count_q(matched: list[CameraEvent]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in matched:
        k = DJI_Q_LABEL.get(e.mrk_q, str(e.mrk_q))
        out[k] = out.get(k, 0) + 1
    return out


def format_rtk_vs_ppk(r: dict) -> str:
    lines = ["=" * 78, " Drone on-board RTK (.MRK, D-RTK 3 / NTRIP base) vs PPK, antenna positions", "=" * 78]
    qs = ", ".join(f"{v} {k}" for k, v in sorted(r.get("mrk_q", {}).items(), key=lambda kv: -kv[1]))
    lines.append(f" MRK RTK status:  {qs}")
    if "north" not in r:
        lines += [" (not enough photos with both RTK fixed and PPK fixed for statistics)", "=" * 78]
        return "\n".join(lines)
    lines.append(f" compared photos: {r['n']} (RTK fixed and PPK fixed)")
    lines.append("-" * 78)
    lines.append(" PPK - RTK, in millimetres:      mean (= RTK base position error)   std (= RTK noise)")
    for k in ("north", "east", "up"):
        s = r[k]
        lines.append(f" {k:<10} {s['mean_mm']:+29.1f} {s['std_mm']:22.1f}   (range {s['min_mm']:+.0f} .. {s['max_mm']:+.0f})")
    lines.append("-" * 78)
    lines.append(f" RTK base offset: {r['offset_horizontal_mm'] / 10:.1f} cm horizontal, {r['up']['mean_mm'] / 10:+.1f} cm up, "
                 f"{r['offset_3d_mm'] / 10:.1f} cm 3D  -> this is how far the on-board RTK base position was off")
    lines.append(f" RTK scatter:     {r['scatter_horizontal_mm']:.1f} mm horizontal, {r['up']['std_mm']:.1f} mm up (1 sigma)")
    lines.append(" The mean offset is the correction PPK applies to the whole flight (PPP-surveyed D-RTK 3 bases are")
    lines.append(" typically off by decimetres). Horizontal scatter mostly reflects the drone's motion between the")
    lines.append(" on-board RTK epoch and the exposure instant, so it grows with flight speed; up scatter is the RTK noise.")
    lines.append("=" * 78)
    return "\n".join(lines)
