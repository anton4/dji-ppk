"""Write per-flight results: events.csv, geo.txt (WebODM), summary.json."""
from __future__ import annotations

import csv
import json
import math
import statistics
import textwrap
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .mrk import MrkEvent
from .offsets import apply_ned_offset, ned_difference
from .pos import PosRow
from .timeutil import fmt

MATCH_TOLERANCE_S = 0.0015

# Only geo.txt stays next to the photos; every other output goes into this subfolder. WebODM/NodeODM takes any
# uploaded .txt that is not geo.txt or image_groups.txt for a GCP file, so accuracy.txt & co must not be there.
RESULTS_DIR = "ppk"
# Outputs earlier versions wrote next to the photos (with "<session stem>_" in front for several sessions).
LEGACY_OUTPUTS = ("events.csv", "summary.json", "accuracy.txt", "compare_report.txt", "rtklib.log", "rtklib_used.conf",
                  "DONE", "FAILED.log")
LEGACY_SESSION_OUTPUTS = LEGACY_OUTPUTS[:6] + ("geo.txt", "trajectory.pos", "trajectory_events.pos")


def results_dir(folder: Path) -> Path:
    return folder / RESULTS_DIR


def find_summary(folder: Path) -> Path | None:
    """summary.json of a processed folder: in the results subfolder, or next to the photos (old layout)."""
    for p in (results_dir(folder) / "summary.json", folder / "summary.json"):
        if p.exists():
            return p
    return None


def remove_legacy_outputs(folder: Path, stems: list[str]) -> list[str]:
    """Delete the outputs an earlier version wrote next to the photos (they are rewritten in RESULTS_DIR)."""
    names = list(LEGACY_OUTPUTS) + [f"{s}_{n}" for s in stems for n in LEGACY_SESSION_OUTPUTS]
    removed = []
    for n in names:
        p = folder / n
        if p.is_file():
            p.unlink()
            removed.append(n)
    return removed


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
    horiz = sorted(math.hypot(a, b) for a, b in zip(dn, de))
    vert = sorted(abs(u) for u in du)

    def err(v):
        return {"rms_mm": round(1000 * math.sqrt(statistics.fmean(x * x for x in v)), 1),
                "p95_mm": round(1000 * v[min(len(v) - 1, int(0.95 * len(v)))], 1), "max_mm": round(1000 * v[-1], 1)}
    out = {"n": len(rows), "mrk_q": _count_q(matched), "north": st(dn), "east": st(de), "up": st(du),
           "horizontal_error": err(horiz), "vertical_error": err(vert),
           "offset_horizontal_mm": round(1000 * math.hypot(mn, me), 1), "offset_3d_mm": round(1000 * math.sqrt(mn * mn + me * me + mu * mu), 1),
           "scatter_horizontal_mm": round(1000 * math.hypot(statistics.pstdev(dn), statistics.pstdev(de)), 1)}
    track = track_analysis(rows, [a - mn for a in dn], [b - me for b in de])
    if track:
        out["track"] = track
    return out


TRACK_MAX_GAP_S = 3.0  # neighbouring photos further apart than this give no velocity (turns, session breaks)
TRACK_MIN_PHOTOS = 10


def _rms(v: list[float]) -> float:
    return math.sqrt(statistics.fmean(x * x for x in v))


def track_analysis(rows: list[CameraEvent], rn: list[float], re_: list[float]) -> dict | None:
    """Explain the horizontal scatter of on-board RTK minus PPK (rn/re_: north/east residuals after the mean, m).

    The scatter is split into the part along the flight direction and across it (rms of the residuals: a constant
    lag gives the same along-track error on every line, in the flight direction), and fitted as
        residual = -lag * velocity - lever * camera_offset
    lag: how much later (s) DJI's MRK position is than the exposure; lever: 1 when the MRK holds the camera position
    rather than the antenna's (the camera offset turns with the drone's heading, so it scatters on alternate lines).
    The velocity comes from the PPK antenna positions of the neighbouring photos. None without enough motion."""
    order = sorted(range(len(rows)), key=lambda i: rows[i].time)
    samples = []  # (vn, ve, on, oe, rn, re) per photo with a velocity
    for k in range(1, len(order) - 1):
        a, i, b = rows[order[k - 1]], rows[order[k]], rows[order[k + 1]]
        dt = (b.time - a.time).total_seconds()
        if not 0 < dt <= 2 * TRACK_MAX_GAP_S or (i.time - a.time).total_seconds() > TRACK_MAX_GAP_S \
                or (b.time - i.time).total_seconds() > TRACK_MAX_GAP_S:
            continue
        n, e, _ = ned_difference(a.ant_lat, a.ant_lon, a.ant_h, b.ant_lat, b.ant_lon, b.ant_h)
        vn, ve = n / dt, e / dt
        if math.hypot(vn, ve) < 1.0:  # hovering or turning on the spot: no direction
            continue
        samples.append((vn, ve, i.off_n_mm / 1000, i.off_e_mm / 1000, rn[order[k]], re_[order[k]]))
    if len(samples) < TRACK_MIN_PHOTOS:
        return None
    along, cross = [], []
    for vn, ve, _on, _oe, r1, r2 in samples:
        s = math.hypot(vn, ve)
        along.append((r1 * vn + r2 * ve) / s)
        cross.append((-r1 * ve + r2 * vn) / s)
    # least squares for x = (lag, lever) in residual = -lag * v - lever * o, both components stacked
    sxx = sum(vn * vn + ve * ve for vn, ve, *_ in samples)
    soo = sum(on * on + oe * oe for _, _, on, oe, *_ in samples)
    sxo = sum(vn * on + ve * oe for vn, ve, on, oe, *_ in samples)
    sxr = sum(-(vn * r1 + ve * r2) for vn, ve, _, _, r1, r2 in samples)
    sor = sum(-(on * r1 + oe * r2) for _, _, on, oe, r1, r2 in samples)
    det = sxx * soo - sxo * sxo
    if soo > 1e-6 and det > 1e-9 * sxx * soo:
        lag, lever = (sxr * soo - sor * sxo) / det, (sor * sxx - sxr * sxo) / det
    else:  # no camera offset in the MRK, or it never turns: fit the lag alone
        lag, lever = sxr / sxx, None
    left = [(r1 + lag * vn + (lever or 0) * on, r2 + lag * ve + (lever or 0) * oe) for vn, ve, on, oe, r1, r2 in samples]
    after = math.hypot(statistics.pstdev(x for x, _ in left), statistics.pstdev(y for _, y in left))
    return {"n": len(samples), "median_speed_mps": round(statistics.median(math.hypot(s[0], s[1]) for s in samples), 1),
            "along_rms_mm": round(1000 * _rms(along), 1), "cross_rms_mm": round(1000 * _rms(cross), 1),
            "lag_ms": round(1000 * lag, 1), "lever": None if lever is None else round(lever, 2),
            "scatter_after_mm": round(1000 * after, 1)}


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
    lines.append(" typically off by decimetres); up scatter is the RTK noise.")
    t = r.get("track")
    if t:
        lines.append("-" * 78)
        lines.append(f" along / across the flight direction: {t['along_rms_mm']:.0f} / {t['cross_rms_mm']:.0f} mm (rms), "
                     f"{t['n']} photos at {t['median_speed_mps']:.1f} m/s")
        fit = f" fitted: MRK timing lag {round(t['lag_ms']):+d} ms"
        if t.get("lever") is not None:
            fit += f", camera offset in the MRK position x{t['lever'] + 0.0:.2f}"
        lines.append(fit + f"; horizontal scatter left: {t['scatter_after_mm']:.0f} mm")
        lines += textwrap.wrap(scatter_verdict(r), 96, initial_indent=" ", subsequent_indent=" ")
    else:
        lines.append(" Horizontal scatter: not enough straight flight to split it into timing and RTK noise.")
    lines.append("=" * 78)
    return "\n".join(lines)


def scatter_verdict(r: dict) -> str:
    """What the on-board RTK scatter means for flying without PPK, from rtk_vs_ppk()'s 'track' fit."""
    t = r["track"]
    before, after = r["scatter_horizontal_mm"], t["scatter_after_mm"]
    lag = abs(t["lag_ms"]) * t["median_speed_mps"] >= 20  # >= 2 cm along track at the typical speed
    lever = t.get("lever") is not None and abs(t["lever"]) >= 0.5
    if not (lag or lever) or after > 0.5 * before:
        return f"No clear timing or offset pattern: the on-board RTK itself scatters about {after / 10:.0f} cm horizontally."
    parts = []
    if lag:
        parts.append(f"a {abs(t['lag_ms']):.0f} ms timing lag of the MRK positions, a real photo error that a known base "
                     "point does not remove (only PPK does)")
    if lever:
        parts.append("the camera offset: the MRK holds the camera position, not the antenna's, so it is no photo error")
    return "The scatter is mostly " + ", plus ".join(parts) + f". RTK noise alone: about {after / 10:.0f} cm."


def format_in_short(rtk: dict, quality: dict) -> str:
    """One table a non-specialist can read: typical photo position error before and after PPK."""
    sd = quality.get("std_mm", {})
    ppk_h = sd.get("horizontal", {}).get("median")
    ppk_v = sd.get("up", {}).get("median")
    lines = ["=" * 78, " In short: how accurate are the photo positions of this flight?", "=" * 78,
             f" {'':34}{'horizontal':<16}{'vertical':<16}",
             "-" * 78]
    if "horizontal_error" in rtk:
        h, v = rtk["horizontal_error"], rtk["vertical_error"]
        lines.append(f" {'DJI on-board RTK (as flown)':<34}{'about ' + f'{h['rms_mm'] / 10:.0f} cm':<16}"
                     f"{'about ' + f'{v['rms_mm'] / 10:.0f} cm':<16}typical error (rms)")
        lines.append(f" {'':34}{'worst ' + f'{h['max_mm'] / 10:.0f} cm':<16}"
                     f"{'worst ' + f'{v['max_mm'] / 10:.0f} cm':<16}worst photo")
        if "north" in rtk:
            def cm(mm: float, sign: str = "") -> str:  # one decimal below 10 cm, so a small value never reads "-0 cm"
                return f"{mm / 10:{sign}.{0 if abs(mm) >= 100 else 1}f} cm"
            lines.append(f" {'  of which a constant shift':<34}{cm(rtk['offset_horizontal_mm']):<16}"
                         f"{cm(rtk['up']['mean_mm'], '+'):<16}RTK base position error")
            lines.append(f" {'  and scatter around it':<34}{cm(rtk['scatter_horizontal_mm']):<16}"
                         f"{cm(rtk['up']['std_mm']):<16}1 sigma")
            if rtk.get("track"):
                t = rtk["track"]
                lines.append(f" {'  of it RTK noise alone':<34}{cm(t['scatter_after_mm']):<16}{'':<16}"
                             f"rest: {round(t['lag_ms']):+d} ms timing lag" + (" + camera offset" if abs(t.get("lever") or 0) >= 0.5 else ""))
    else:
        lines.append(f" {'DJI on-board RTK (as flown)':<34}{'n/a':<16}{'n/a':<16}(no RTK fixed photos in the MRK)")
    if ppk_h is not None and ppk_v is not None:
        lines.append(f" {'after PPK with the RINEX base':<34}{'about ' + f'{ppk_h / 10:.1f} cm':<16}{'about ' + f'{ppk_v / 10:.1f} cm':<16}RTKLIB estimate, 1 sigma")
    else:
        lines.append(f" {'after PPK with the RINEX base':<34}{'n/a':<16}{'n/a':<16}(no fixed photos)")
    lines += ["-" * 78,
              " 'DJI on-board RTK' is the position the drone wrote into the photos during the flight, measured",
              " against the PPK result (EUREF-EST97). The constant shift is how far the D-RTK 3 base position was",
              " off; `ppk base-survey` removes it once for a site. The accuracy the D-RTK 3 app shows after PPP",
              " calibration is the solution's own precision estimate, not this offset. 'after PPK' is likewise",
              " RTKLIB's own estimate, not verified against ground control points; independent checks typically",
              " show 1-3 cm.",
              "=" * 78]
    return "\n".join(lines)
