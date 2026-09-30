"""Known base point for the D-RTK 3: survey its position once against ESTPOS from its own logs.

In base mode the D-RTK 3 keeps, on its internal storage, per session `DRTK3_<seq>_<yyyymmddhhmmss>_<serial>.*`:
  .OBS / .NAV   its own observations as RINEX 3.05 (1 Hz, GPS/GLONASS/Galileo/QZSS/BeiDou) and broadcast ephemerides;
                the header's APPROX POSITION XYZ is the position the station broadcast to the drone (it equals the
                RTCM 1006 position in the .dat to 0.3 mm): DJI's own antenna phase-centre coordinate for that session,
  .dat          the same data as RTCM 3.2 (1006, MSM5, ephemerides), what the drone received; only needed when a
                session has no OBS: then convbin converts it,
  .MRK          the station's position log during calibration (one row per 5 s, same layout as the drone's MRK):
                shows the PPP convergence and which position each later session inherited.
A static RTKLIB solution of a session's OBS against an ESTPOS Virtual RINEX generated at the broadcast position gives
the true phase centre in EUREF-EST97. The difference is the calibration error of that session; adding it to the
coordinates DJI Pilot 2 displayed gives the corrected ground point for Manual Calibration (same pole height), to be
saved as a Frequent Coordinate. Afterwards the drone's on-board RTK is in the ESTPOS frame at centimetre level and
RINEX + PPK are no longer needed for that site.

A base point folder is a folder under the flights directory holding the station's files (no drone flight). Outputs
land next to them: <stem>_static.pos, <stem>_rtklib.log, <stem>_rtklib_used.conf, basepoint.json, basepoint.txt.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .offsets import apply_ned_offset, ned_difference
from .pos import PosRow
from .rinex import RinexHeader, read_header, scan_obs_span
from .timeutil import span_local

STATION_RE = re.compile(r"^DRTK3?_", re.I)  # DJI's file prefix for the station's own logs
STATION_EXT = {".obs", ".nav", ".dat", ".mrk"}
JSON_NAME = "basepoint.json"
REPORT_NAME = "basepoint.txt"
MIN_FIXED_EPOCHS = 100
MIN_SESSION_EPOCHS = 600  # 10 min at 1 Hz: shorter sessions (the 2 min log written during the calibration) are not surveyed
GOOD_FIX_RATIO = 0.9     # below: WARN
USABLE_FIX_RATIO = 0.5   # below: FAIL
CROSSCHECK_WARN_MM = 30  # drone-derived base offset vs this survey
MULTI_LOG_WARN_MM = 20   # several sessions of the same point disagreeing
CALIBRATED_Q = 1         # Q in the station's MRK once it holds a calibrated position (16 = single, 34 = float)


def is_station_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in STATION_EXT and (STATION_RE.match(p.name) is not None or p.suffix.lower() == ".dat")


@dataclass
class StationLog:
    """One session of the station: the files sharing a stem."""
    stem: str
    obs: Path | None = None
    nav: Path | None = None
    dat: Path | None = None
    mrk: Path | None = None

    @property
    def is_session(self) -> bool:
        return self.obs is not None or self.dat is not None


@dataclass
class BasePoint:
    directory: Path
    logs: list[StationLog] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.directory.name

    @property
    def json_path(self) -> Path:
        return self.directory / JSON_NAME

    def sessions(self) -> list[StationLog]:
        return [l for l in self.logs if l.is_session]

    def calibration_logs(self) -> list[Path]:
        return [l.mrk for l in self.logs if l.mrk is not None]

    def obs_for(self, log: StationLog) -> Path:
        """The session's RINEX: the station's own OBS, else the file convbin writes from the DAT."""
        return log.obs if log.obs is not None else self.directory / (log.stem + ".obs")

    def nav_for(self, log: StationLog) -> Path | None:
        if log.nav is not None:
            return log.nav
        p = self.directory / (log.stem + ".nav")
        return p if p.exists() and p.stat().st_size > 0 else None

    def converted(self) -> list[tuple[StationLog, Path]]:
        """(log, obs) for every session whose RINEX is available (native, or converted and not older than the DAT)."""
        out = []
        for log in self.sessions():
            obs = self.obs_for(log)
            if not obs.exists() or obs.stat().st_size == 0:
                continue
            if log.obs is None and log.dat is not None and obs.stat().st_mtime < log.dat.stat().st_mtime:
                continue
            out.append((log, obs))
        return out

    def surveyable(self) -> list[tuple[StationLog, Path]]:
        """Converted sessions long enough for a static survey; the 2 min log the station writes while it is being
        calibrated is not (it also carries the unsettled position)."""
        out = []
        for log, obs in self.converted():
            try:
                _f, _l, n = obs_span(obs)
            except (OSError, ValueError):
                continue
            if n >= MIN_SESSION_EPOCHS:
                out.append((log, obs))
        return out

    def own_files(self) -> set[Path]:
        """Files of the station itself: never base candidates."""
        out = set()
        for log in self.logs:
            for p in (log.obs, log.nav, log.dat, log.mrk, self.obs_for(log), self.directory / (log.stem + ".nav")):
                if p is not None:
                    out.add(p.resolve())
        return out

    def parent_flight_dir(self) -> Path | None:
        """The flight folder this base point sits in (`<flight>/d-rtk3/`), whose Virtual RINEX may cover the session."""
        parent = self.directory.parent
        try:
            has_flight = any(p.is_file() and p.name.upper().startswith("DJI_") and p.suffix.lower() == ".obs" for p in parent.iterdir())
        except OSError:
            return None
        return parent if has_flight else None

    def base_search_dirs(self) -> tuple[Path, ...]:
        parent = self.parent_flight_dir()
        return (parent,) if parent else ()

    def label(self, root: Path | None) -> str:
        """The folder as the status table names it: relative to the flights directory when nested."""
        if root is not None:
            try:
                return self.directory.resolve().relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                pass
        return self.name

    def settings(self) -> dict:
        try:
            return json.loads(self.json_path.read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self, data: dict) -> None:
        self.json_path.write_text(json.dumps(data, indent=1, default=str) + "\n")


def station_logs(directory: Path) -> list[StationLog]:
    logs: dict[str, StationLog] = {}
    try:
        files = sorted(p for p in directory.iterdir() if is_station_file(p))
    except OSError:
        return []
    for p in files:
        log = logs.setdefault(p.stem, StationLog(p.stem))
        setattr(log, p.suffix.lower()[1:], p)
    return [logs[k] for k in sorted(logs)]


def basepoint_at(path: str | Path) -> BasePoint | None:
    """The base point in `path` (a folder, or one of the station's files), or None when the folder holds none."""
    p = Path(path)
    directory = p.parent if p.is_file() else p
    logs = station_logs(directory)
    if p.is_file():
        logs = [l for l in logs if l.stem == p.stem]
    return BasePoint(directory, logs) if any(l.is_session for l in logs) else None


def find_basepoints(root: str | Path, max_depth: int = 2) -> list[BasePoint]:
    """Base point folders under root (root itself included), up to max_depth levels down; dot-folders skipped.
    Depth 2 reaches a station folder kept inside its flight folder (`<flight>/d-rtk3/`)."""
    root = Path(root)
    out: list[BasePoint] = []

    def walk(d: Path, depth: int) -> None:
        bp = basepoint_at(d)
        if bp:
            out.append(bp)
        if depth <= 0:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                walk(Path(e.path), depth - 1)

    walk(root, max_depth)
    return sorted(out, key=lambda b: b.name)


# ----------------------------------------------------------------------------- the station's own files

def convbin_binary() -> str:
    return os.environ.get("PPK_CONVBIN", "convbin")


def convbin_command(dat: Path, obs: Path, nav: Path, marker: str, approx_time: datetime) -> list[str]:
    """convbin arguments for a D-RTK 3 DAT (RTCM 3.2). `-tr` resolves the GPS week of RTCM epochs (they carry the
    time of week only) from an approximate time: the DAT's modification time, i.e. the end of the log."""
    binary = shutil.which(convbin_binary()) or convbin_binary()
    return [binary, "-r", "rtcm3", "-v", "3.04", "-od", "-os", "-oi", "-ot", "-tr", approx_time.strftime("%Y/%m/%d %H:%M:%S"),
            "-hm", marker[:60], "-o", str(obs), "-n", str(nav), str(dat)]


def prepare_log(bp: BasePoint, log: StationLog, force: bool = False) -> tuple[Path, bool]:
    """The session's RINEX. The station's own OBS is used as is; a DAT-only session is converted with convbin next to
    it (again only when the DAT is newer, or `force`). Returns (obs, converted_now). convbin's message statistics go
    to <stem>_convbin.log."""
    if log.obs is not None:
        return log.obs, False
    if log.dat is None:
        raise FileNotFoundError(f"{log.stem}: no OBS and no DAT")
    obs, nav = bp.obs_for(log), bp.directory / (log.stem + ".nav")
    if not force and obs.exists() and obs.stat().st_size > 0 and obs.stat().st_mtime >= log.dat.stat().st_mtime:
        return obs, False
    mtime = datetime.fromtimestamp(log.dat.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
    cmd = convbin_command(log.dat, obs, nav, bp.name, mtime)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    log_path = bp.directory / f"{log.stem}_convbin.log"
    log_path.write_text("$ " + " ".join(cmd) + f"\n\nexit code: {proc.returncode}\n\n" + proc.stdout)
    if proc.returncode != 0 or not obs.exists() or obs.stat().st_size == 0:
        raise RuntimeError(f"convbin could not convert {log.dat.name} (exit {proc.returncode}), see {log_path.name}")
    os.utime(obs, None)  # newer than the DAT even on file systems with coarse time stamps
    return obs, True


def broadcast_position(obs: Path) -> tuple[RinexHeader, tuple[float, float, float] | None]:
    """The position the D-RTK 3 broadcast (RTCM 1006 = the OBS header's APPROX POSITION XYZ), or None."""
    hdr = read_header(obs)
    return hdr, hdr.approx_llh


def obs_span(obs: Path) -> tuple[datetime, datetime, int]:
    hdr = read_header(obs)
    first, last, n = scan_obs_span(obs, hdr)
    first, last = first or hdr.first_obs, last or hdr.last_obs
    if not first or not last:
        raise ValueError(f"{obs.name}: cannot determine the observation span")
    return first, last, n


def calibration_summary(mrk: Path) -> dict:
    """The station's calibration log: how the position converged and where it ended up. Rows with Q=1 hold a
    calibrated position; the last distinct one is what later sessions broadcast."""
    from .mrk import parse_mrk
    rows = parse_mrk(mrk)
    out = {"file": mrk.name, "rows": len(rows), "first": rows[0].time, "last": rows[-1].time,
           "q": {str(k): v for k, v in sorted(__import__("collections").Counter(r.q for r in rows).items())}}
    held = [r for r in rows if r.q == CALIBRATED_Q]
    if held:
        final = held[-1]
        out["final"] = {"lat": final.lat, "lon": final.lon, "h": final.ellh, "std": (final.std_n, final.std_e, final.std_v)}
        # when the final position was first reached (the convergence time)
        for r in held:
            if (r.lat, r.lon, r.ellh) == (final.lat, final.lon, final.ellh):
                out["settled"] = r.time
                out["settle_minutes"] = round((r.time - rows[0].time).total_seconds() / 60, 1)
                break
        distinct = {(r.lat, r.lon, r.ellh) for r in held}
        out["distinct_positions"] = len(distinct)
        if len(distinct) > 1:
            first_held = held[0]
            dn, de, du = ned_difference(first_held.lat, first_held.lon, first_held.ellh, final.lat, final.lon, final.ellh)
            out["drift_from_first_hold_mm"] = {"north": round(1000 * dn), "east": round(1000 * de), "up": round(1000 * du)}
    return out


# ----------------------------------------------------------------------------- static solution

def reduce_static(rows: list[PosRow]) -> dict:
    """Mean of the fixed epochs of a static solution with its spread, and a verdict on how much to trust it."""
    fixed = [r for r in rows if r.q == 1]
    out = {"epochs": len(rows), "fixed": len(fixed), "fix_ratio": round(len(fixed) / len(rows), 4) if rows else 0.0}
    if rows:
        out["first"], out["last"] = rows[0].time, rows[-1].time
    if len(fixed) < 2:
        out["level"] = "FAIL"
        out["verdict"] = "no fixed solution" if not fixed else "a single fixed epoch"
        return out
    lat = statistics.fmean(r.lat for r in fixed)
    lon = statistics.fmean(r.lon for r in fixed)
    h = statistics.fmean(r.h for r in fixed)
    dn, de, du = zip(*(ned_difference(lat, lon, h, r.lat, r.lon, r.h) for r in fixed))
    out.update({"lat": lat, "lon": lon, "h": h,
                "spread_mm": {"north": round(1000 * statistics.pstdev(dn), 1), "east": round(1000 * statistics.pstdev(de), 1),
                              "up": round(1000 * statistics.pstdev(du), 1)},
                "last_epoch": {"lat": fixed[-1].lat, "lon": fixed[-1].lon, "h": fixed[-1].h},
                "mean_sd_mm": {"north": round(1000 * statistics.fmean(r.sdn for r in fixed), 1),
                               "east": round(1000 * statistics.fmean(r.sde for r in fixed), 1),
                               "up": round(1000 * statistics.fmean(r.sdu for r in fixed), 1)}})
    ratio = out["fix_ratio"]
    if ratio < USABLE_FIX_RATIO:
        out["level"], out["verdict"] = "FAIL", f"only {100 * ratio:.0f} % of the epochs fixed: not usable"
    elif len(fixed) < MIN_FIXED_EPOCHS or ratio < GOOD_FIX_RATIO:
        out["level"] = "WARN"
        out["verdict"] = (f"{len(fixed)} fixed epochs ({100 * ratio:.0f} %): usable, a longer or cleaner log would be better"
                          if len(fixed) < MIN_FIXED_EPOCHS else f"{100 * ratio:.0f} % of the epochs fixed: check the sky view")
    else:
        out["level"], out["verdict"] = "PASS", f"{len(fixed)} fixed epochs ({100 * ratio:.1f} %)"
    return out


# ----------------------------------------------------------------------------- the point

def correction_mm(broadcast: tuple[float, float, float], surveyed: tuple[float, float, float]) -> dict:
    dn, de, du = ned_difference(*broadcast, *surveyed)
    return {"north": round(1000 * dn, 1), "east": round(1000 * de, 1), "up": round(1000 * du, 1),
            "horizontal": round(1000 * math.hypot(dn, de), 1)}


def flight_correction(crosschecks: list[dict]) -> dict | None:
    """The base error as the drone saw it: mean of the on-board-RTK-vs-PPK offsets of the overlapping flights."""
    rows = [cc for cc in crosschecks if "north" in cc]
    if not rows:
        return None
    n = statistics.fmean(cc["north"] for cc in rows)
    e = statistics.fmean(cc["east"] for cc in rows)
    u = statistics.fmean(cc["up"] for cc in rows)
    return {"north": round(n, 1), "east": round(e, 1), "up": round(u, 1), "horizontal": round(math.hypot(n, e), 1),
            "flights": sorted({cc["flight"] for cc in rows})}


def choose_correction(survey: dict | None, flight: dict | None, mode: str = "auto") -> tuple[dict | None, str, str]:
    """Which correction to apply to the broadcast position: (correction, source, reason).

    The survey correction puts the broadcast on the true phase centre as RTKLIB sees the station's own antenna. The
    flight correction is what makes the drone's on-board RTK agree with the PPK result: it absorbs the antenna
    modelling differences between DJI's RTK chain and RTKLIB (they showed up as ~9 cm in height on the first real
    survey while the horizontal parts agreed to 1 cm). Since the point exists to make the drone's RTK right, the flight
    correction wins when there is one and its horizontal part agrees with the survey within CROSSCHECK_WARN_MM."""
    if mode == "survey" or flight is None or survey is None:
        if survey is None:
            return None, "none", "no broadcast position in the session: the correction cannot be measured"
        why = "the survey" if flight is None or mode == "survey" else "the survey"
        return survey, "survey", f"{why} (static solution of the station's own log)" + ("" if flight is None or mode != "survey" else ", as requested")
    dh = math.hypot(flight["north"] - survey["north"], flight["east"] - survey["east"])
    if mode == "flight" or dh <= CROSSCHECK_WARN_MM:
        return flight, "flight", (f"the drone's on-board RTK vs PPK of flight {', '.join(flight['flights'])}: it makes the drone agree with the "
                                  f"PPK result; its horizontal part agrees with the survey within {dh / 10:.1f} cm")
    return survey, "survey", (f"the survey; the flight-derived correction differs horizontally by {dh / 10:.1f} cm, more than "
                              f"{CROSSCHECK_WARN_MM / 10:.0f} cm, so it is not trusted (different base position during the flight?)")


def derive_point(broadcast: tuple[float, float, float] | None, surveyed: tuple[float, float, float],
                 shown: tuple[float, float, float] | None, pole: float | None, k: float | None,
                 correction: dict | None = None) -> dict:
    """Correction and Manual Calibration coordinates.

    broadcast: what the D-RTK 3 transmitted (phase centre, RTCM 1006) in the session the shown coordinates belong to.
    surveyed: the static solution (phase centre).  shown: the ground point DJI Pilot 2 displayed after the calibration
    of that session.  pole: pole height then.  k: phase-centre height above the pole tip, once known from an earlier
    survey (h_broadcast - h_shown - pole).  correction: the north/east/up shift (mm) to apply to the broadcast
    position; default surveyed - broadcast.

    antenna = broadcast + correction.  ground = shown + correction, or antenna - (pole + k) up when the shown
    coordinates are unknown but k is.  The sign convention matches outputs.rtk_vs_ppk (true minus DJI).
    """
    out: dict = {"surveyed": {"lat": surveyed[0], "lon": surveyed[1], "h": surveyed[2]}}
    if broadcast:
        out["broadcast"] = {"lat": broadcast[0], "lon": broadcast[1], "h": broadcast[2]}
        out["survey_correction_mm"] = correction_mm(broadcast, surveyed)
        corr = correction or out["survey_correction_mm"]
        out["correction_mm"] = corr
        lat, lon, h = apply_ned_offset(*broadcast, corr["north"] / 1000, corr["east"] / 1000, -corr["up"] / 1000)
        out["antenna"] = {"lat": lat, "lon": lon, "h": h}
        if shown and pole is not None:
            out["k_m"] = round(broadcast[2] - shown[2] - pole, 4)
        if shown:
            lat, lon, h = apply_ned_offset(*shown, corr["north"] / 1000, corr["east"] / 1000, -corr["up"] / 1000)
            out["ground"] = {"lat": lat, "lon": lon, "h": h, "pole_m": pole, "method": "shown coordinates + correction"}
            return out
    antenna = out.get("antenna") or out["surveyed"]
    if pole is not None and k is not None:
        out["ground"] = {"lat": antenna["lat"], "lon": antenna["lon"], "h": antenna["h"] - pole - k, "pole_m": pole,
                         "method": f"corrected antenna phase centre - pole {pole:.3f} m - phase-centre offset {k:.3f} m"}
    return out


def dms_text(lat: float, lon: float) -> str:
    def one(v: float, pos: str, neg: str) -> str:
        total = round(abs(v) * 3600 * 10000)
        deg, rem = divmod(total, 3600 * 10000)
        minutes, rem = divmod(rem, 60 * 10000)
        return f"{deg}° {minutes:02d}' {rem / 10000:07.4f}\" {pos if v >= 0 else neg}"
    return one(lat, "N", "S") + "   " + one(lon, "E", "W")


def format_calibration(cal: dict) -> list[str]:
    lines = [f" Calibration log {cal['file']}: {span_local(cal['first'], cal['last'])}, {cal['rows']} rows, Q " +
             ", ".join(f"{v}x{k}" for k, v in cal["q"].items())]
    if "final" in cal:
        f = cal["final"]
        lines.append(f"   held position: {f['lat']:.8f}  {f['lon']:.8f}  {f['h']:.3f} m"
                     + (f", reached after {cal['settle_minutes']:.0f} min" if cal.get("settle_minutes") is not None else "")
                     + (f", {cal['distinct_positions']} positions held in all" if cal.get("distinct_positions", 1) > 1 else ""))
        d = cal.get("drift_from_first_hold_mm")
        if d:
            lines.append(f"   the first held position was {d['north']:+d} mm N, {d['east']:+d} mm E, {d['up']:+d} mm up from the final one:"
                         " a session recorded before the log settled inherited a worse position")
    return lines


def format_report(bp: BasePoint, sessions: list[dict], point: dict, settings: dict, crosschecks: list[dict],
                  calibrations: list[dict] | None = None) -> str:
    """The human report; the same numbers go to basepoint.json."""
    w = 78
    lines = ["=" * w, f" D-RTK 3 base point survey: {bp.name}" + (f"  ({settings['name']})" if settings.get("name") else ""), "=" * w]
    for cal in calibrations or []:
        lines += format_calibration(cal)
    for s in sessions:
        r = s["static"]
        lines.append(f" Session {s['stem']}: {s['span']}, base {s['base']}")
        if "lat" not in r:
            lines.append(f"   [{r['level']:>4}] {r['verdict']}")
            continue
        sp = r["spread_mm"]
        lines.append(f"   [{r['level']:>4}] {r['verdict']}, spread {sp['north']:.1f} / {sp['east']:.1f} / {sp['up']:.1f} mm N/E/U (1 sigma)")
        if s.get("broadcast"):
            b = s["broadcast"]
            lines.append(f"   broadcast (RTCM 1006, what the station used):  {b['lat']:.8f}  {b['lon']:.8f}  {b['h']:.3f} m")
        else:
            lines.append("   [WARN] the log carries no base position (RTCM 1006 / APPROX POSITION): correction not measurable from it")
        lines.append(f"   surveyed  (static vs ESTPOS Virtual RINEX):     {r['lat']:.8f}  {r['lon']:.8f}  {r['h']:.3f} m")
        if s.get("correction_mm"):
            cm = s["correction_mm"]
            lines.append(f"   correction surveyed - broadcast: {cm['north']:+.0f} mm N, {cm['east']:+.0f} mm E, {cm['up']:+.0f} mm up"
                         f"  = {cm['horizontal'] / 10:.1f} cm horizontal, {cm['up'] / 10:+.1f} cm up")
    if len(sessions) > 1 and point.get("disagreement_mm") is not None:
        d = point["disagreement_mm"]
        lvl = "WARN" if d > MULTI_LOG_WARN_MM else "PASS"
        lines.append(f" [{lvl}] the surveyed positions of the sessions differ by up to {d / 10:.1f} cm (3D); the result is the session "
                     f"with the most fixed epochs ({point.get('result_stem', '?')})")
    for cc in crosschecks:
        lines.append(f" Cross-check, flight {cc['flight']} (drone on-board RTK vs PPK): {cc['north']:+.0f} mm N, {cc['east']:+.0f} mm E, "
                     f"{cc['up']:+.0f} mm up -> [{cc['level']}] differs by {cc['diff_mm'] / 10:.1f} cm")
    if point.get("correction_mm"):
        cm = point["correction_mm"]
        lines.append(f" Correction applied: {cm['north']:+.0f} mm N, {cm['east']:+.0f} mm E, {cm['up']:+.0f} mm up, from {point.get('correction_reason', '?')}")
        a = point["antenna"]
        lines.append(f" Corrected antenna phase centre (EUREF-EST97):  {a['lat']:.8f}  {a['lon']:.8f}  {a['h']:.3f} m")
    lines.append("-" * w)
    shown = settings.get("shown")
    pole = settings.get("pole_m")
    if shown:
        lines.append(f" Shown by DJI Pilot 2 after that calibration:      {shown['lat']:.8f}  {shown['lon']:.8f}  {shown['h']:.3f} m"
                     + (f", pole {pole:.3f} m" if pole is not None else ""))
    if point.get("k_m") is not None:
        k = point["k_m"]
        note = "plausible" if 0.05 <= k <= 0.30 else "the app shows the antenna, not the ground?" if abs(k + (pole or 0)) < 0.05 else "unexpected, check pole height and the shown height"
        lines.append(f" Phase centre above the pole tip implied by the data (h_broadcast - h_shown - pole): {k:.3f} m  ({note})")
    g = point.get("ground")
    if g:
        lines += ["", " Enter in DJI Pilot 2 -> RTK -> D-RTK 3 -> Advanced Settings -> Adjust Coordinates (Manual Calibration),",
                  " then save it as a Frequent Coordinate:",
                  f"   Latitude            {g['lat']:.8f}",
                  f"   Longitude           {g['lon']:.8f}",
                  f"   Ellipsoidal height  {g['h']:.3f} m",
                  f"   Pole height         {g['pole_m']:.3f} m" if g.get("pole_m") is not None else "   Pole height         (the one used when it was calibrated)",
                  f"   ({dms_text(g['lat'], g['lon'])})",
                  f"   derived as: {g['method']}"]
    else:
        s = point.get("antenna") or point["surveyed"]
        lines += ["", " Corrected antenna phase centre (EUREF-EST97, ellipsoidal height):",
                  f"   {s['lat']:.8f}  {s['lon']:.8f}  {s['h']:.3f} m   ({dms_text(s['lat'], s['lon'])})",
                  " The ground point for Manual Calibration also needs the pole height and the coordinates DJI Pilot 2 showed after",
                  " a calibration on this marker (any session in this folder): ppk base-survey <folder> --shown <lat> <lon> <h> --pole <m>",
                  " Once the phase-centre offset k is known (from any such session) the shown coordinates are not needed again."]
    lines.append("=" * w)
    return "\n".join(lines)


# ----------------------------------------------------------------------------- cross-check with flights

def flight_crosschecks(root: Path, first: datetime, last: datetime, correction_mm: dict | None,
                       exclude: Path | None = None) -> list[dict]:
    """Processed flights that overlap the session span: their on-board-RTK-vs-PPK mean is the same base error seen
    from the drone, so it must agree with this survey's correction."""
    from .discover import find_flights, group_by_folder
    out = []
    try:
        groups = group_by_folder(find_flights(root))
    except Exception:  # noqa: BLE001
        return out
    for directory, flights in sorted(groups.items()):
        if exclude and directory.resolve() == exclude.resolve():
            continue
        summary = directory / "summary.json"
        if not summary.exists():
            continue
        try:
            overlaps = False
            for fl in flights:
                f, l, _ = scan_obs_span(fl.obs)
                if f and l and f <= last and l >= first:
                    overlaps = True
            if not overlaps:
                continue
            s = json.loads(summary.read_text())
        except (OSError, ValueError):
            continue
        for ss in s.get("session_summaries") or [s]:
            rtk = ss.get("rtk_vs_ppk") or {}
            if "north" not in rtk:
                continue
            n, e, u = rtk["north"]["mean_mm"], rtk["east"]["mean_mm"], rtk["up"]["mean_mm"]
            entry = {"flight": directory.name, "session": ss.get("rover_obs"), "north": n, "east": e, "up": u}
            if correction_mm:
                d = math.sqrt((n - correction_mm["north"]) ** 2 + (e - correction_mm["east"]) ** 2 + (u - correction_mm["up"]) ** 2)
                entry["diff_mm"] = round(d, 1)
                entry["level"] = "PASS" if d <= CROSSCHECK_WARN_MM else "WARN"
            else:
                entry["diff_mm"], entry["level"] = float("nan"), "INFO"
            out.append(entry)
    return out


def rinex_days_left_for(first_gpst: datetime) -> int:
    from .estpos import rinex_days_left
    return rinex_days_left(first_gpst)
