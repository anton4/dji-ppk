"""ppk command line interface."""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .compare import compare_events, format_report, find_reference_events
from .discover import Flight, load_flight
from .estpos import check_base, format_order, plan_order
from .events import write_obs_with_events
from .mrk import parse_mrk
from .outputs import match_events, read_events_csv, write_events_csv, write_geo_txt, write_summary, solution_quality, format_quality, rtk_vs_ppk, format_rtk_vs_ppk, format_in_short
from .pos import read_pos
from .rinex import prepare_obs, read_header, scan_obs_span, find_base_candidates, find_nav_files, stale_nav_systems
from .rtklib import rtklib_version, run_rnx2rtkp, write_conf
from .timeutil import span_local

log = logging.getLogger("ppk")

DEFAULT_CONF = os.environ.get("PPK_CONF", "/app/config/dji_m4e.conf")
DEFAULT_OUT = os.environ.get("PPK_OUT_DIR", "/data/out")
DEFAULT_BASE_DIR = os.environ.get("PPK_BASE_DIR", "/data/base")
DEFAULT_IN_PLACE = os.environ.get("PPK_IN_PLACE", "").lower() in ("1", "true", "on", "yes")


def _out_dir_for(out_root: Path, flight: Flight, in_place: bool = False) -> Path:
    """Output directory: the flight folder itself with in_place, else <out_root>/<flight name>."""
    return flight.directory if in_place else out_root / flight.name


DEFAULT_FLIGHTS_DIR = os.environ.get("PPK_FLIGHTS_DIR", "/data/flights")


def _flight_path(arg: str) -> Path:
    """Resolve a flight argument: as given, or by its last path component under /data/flights.

    Lets users pass a bare folder name or a host path (e.g. ../DJI_xxx) from inside the container.
    """
    p = Path(arg)
    if p.exists():
        return p
    alt = Path(DEFAULT_FLIGHTS_DIR) / p.name
    if alt.exists():
        log.info("%s not found, using %s", arg, alt)
        return alt
    raise FileNotFoundError(f"{arg}: not found (flight folders are mounted under {DEFAULT_FLIGHTS_DIR}, "
                            f"try {DEFAULT_FLIGHTS_DIR}/{p.name})")


def _parse_overrides(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--set expects key=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ----------------------------------------------------------------------------- process

def process_flight(flight: Flight, base: Path, out_dir: Path, conf: Path = Path(DEFAULT_CONF),
                   overrides: dict[str, str] | None = None, geo_accuracy: bool = False,
                   fixed_only: bool = False, keep_work: bool = False, extra_nav: list[Path] | None = None) -> dict:
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    for stale in (out_dir / "DONE", out_dir / "FAILED.log"):
        if stale.exists():
            stale.unlink()

    mrk = parse_mrk(flight.mrk)
    log.info("%s: %d camera events in %s, %d images", flight.name, len(mrk), flight.mrk.name, len(flight.images))
    missing_images = [e.id for e in mrk if e.id not in flight.images]
    if missing_images:
        log.warning("%d MRK events have no matching image file (first: %s)", len(missing_images), missing_images[:5])

    base_plain = prepare_obs(base, work)
    base_hdr = read_header(base_plain)
    if base_plain.resolve() != Path(base).resolve():
        log.info("base unpacked to %s", base_plain.name)
    base_navs = find_nav_files(base, work)
    if base_navs:
        log.info("base navigation files: %s", ", ".join(p.name for p in base_navs))
    first_obs = read_header(flight.obs).first_obs or mrk[0].time
    stale = stale_nav_systems(flight.nav, first_obs)
    if stale:
        names = {"G": "GPS", "R": "GLONASS", "E": "Galileo", "C": "BeiDou", "J": "QZSS"}
        desc = ", ".join(f"{names.get(k, k)} (newest {v:%Y-%m-%d})" for k, v in sorted(stale.items()))
        if base_navs:
            log.warning("rover NAV has unusable ephemerides for %s (DJI GPS week rollover bug); "
                        "using the base navigation files instead", desc)
        else:
            log.warning("rover NAV has unusable ephemerides for %s (DJI GPS week rollover bug) and the base "
                        "comes without navigation files: those satellites cannot be used. Download the base "
                        "with its navigation files (ESTPOS zip) for a better solution", desc)
    hdr, checks = check_base(base_plain, flight, Path(os.environ.get("PPK_ANTEX", "")) or None)
    for c in checks:
        (log.error if c.level == "FAIL" else log.warning if c.level == "WARN" else log.info)("base: %s", c.message)
    if any(c.level == "FAIL" for c in checks):
        raise RuntimeError("base file failed validation, see log")

    rover_events = work / (flight.stem + "_events.obs")
    stats = write_obs_with_events(flight.obs, rover_events, [e.time for e in mrk])
    log.info("injected %d event epochs into %s (%d obs epochs)", stats["inserted"], rover_events.name, stats["epochs"])
    if stats["before_first_epoch"] or stats["after_last_epoch"]:
        log.warning("%d events before first / %d after last observation epoch",
                    stats["before_first_epoch"], stats["after_last_epoch"])

    if (overrides or {}).get("misc-timeinterp", "off").lower() in ("on", "1"):
        log.warning("misc-timeinterp=on suppresses RTKLIB's *_events.pos output; expect no camera solutions")
    conf_used = write_conf(conf, out_dir / "rtklib_used.conf", overrides)
    out_pos = out_dir / f"{flight.stem}_trajectory.pos"
    navs = [flight.nav] + list(extra_nav or []) + base_navs
    log.info("running %s ...", rtklib_version())
    res = run_rnx2rtkp(conf_used, out_pos, rover_events, base_plain, navs, out_dir / "rtklib.log")
    log.info("rnx2rtkp finished in %.1f s (exit %d)", res.seconds, res.returncode)
    if not res.events_path.exists() or not res.pos_path.exists():
        raise RuntimeError(f"rnx2rtkp produced no solution, see {res.log_path}")

    traj = read_pos(res.pos_path)
    ev_pos = read_pos(res.events_path)
    matched, unmatched = match_events(mrk, ev_pos.rows, flight.images)
    if unmatched:
        log.warning("%d MRK events have no RTKLIB solution (ids %s ...)", len(unmatched), [e.id for e in unmatched][:5])

    csv_path = out_dir / "events.csv"
    write_events_csv(csv_path, matched)
    n_geo = write_geo_txt(out_dir / "geo.txt", matched, with_accuracy=geo_accuracy, fixed_only=fixed_only)
    summary = {
        "flight": flight.name, "rover_obs": flight.obs.name, "mrk": flight.mrk.name, "base": str(base),
        "base_marker": base_hdr.marker_name, "base_virtual": base_hdr.is_virtual, "base_antenna": base_hdr.ant_type,
        "base_position_llh": base_hdr.approx_llh, "rtklib": rtklib_version(), "conf": str(conf), "overrides": overrides or {},
        "trajectory": traj.counts(), "trajectory_fix_ratio": round(traj.fix_ratio, 4),
        "events": {"mrk": len(mrk), "solved": len(matched), "unsolved": len(unmatched),
                   "fix": sum(1 for e in matched if e.q == 1), "float": sum(1 for e in matched if e.q == 2),
                   "other": sum(1 for e in matched if e.q not in (1, 2))},
        "geo_txt_rows": n_geo, "images_missing": len(missing_images), "seconds": round(time.time() - t0, 1),
        "base_nav_files": [p.name for p in base_navs], "rover_nav_stale": sorted(stale),
        "outputs": {"events_csv": csv_path.name, "geo_txt": "geo.txt", "trajectory_pos": res.pos_path.name,
                    "events_pos": res.events_path.name, "rtklib_log": "rtklib.log", "conf": conf_used.name},
    }
    write_summary(out_dir / "summary.json", summary)
    ev = summary["events"]
    log.info("events: %d fixed, %d float, %d other, %d unsolved of %d; trajectory fix ratio %.1f%%",
             ev["fix"], ev["float"], ev["other"], ev["unsolved"], len(mrk), 100 * traj.fix_ratio)

    quality = solution_quality(matched, len(mrk), traj.rows)
    summary["quality"] = quality
    baseline_km = None
    for c in checks:
        m = re.search(r"baseline to flight area ([0-9.]+) km", c.message)
        if m:
            baseline_km = float(m.group(1))
    refs = find_reference_events(flight.directory, exclude=res.events_path)
    print()
    print(format_quality(quality, flight.name, Path(base).name, baseline_km, refs[-1].name if refs else None))
    rtk = rtk_vs_ppk(matched)
    summary["rtk_vs_ppk"] = rtk
    print(format_rtk_vs_ppk(rtk))
    in_short = format_in_short(rtk, quality)
    (out_dir / "accuracy.txt").write_text(in_short + "\n")
    if refs:
        ref = read_pos(refs[-1])
        cmp_res = compare_events(matched, ref.rows)
        report = format_report(cmp_res, f"ours vs reference {refs[-1].name}")
        (out_dir / "compare_report.txt").write_text(report + "\n")
        summary["compare"] = {"reference": refs[-1].name, **cmp_res.as_dict()}
        print(report)
    print(in_short)
    write_summary(out_dir / "summary.json", summary)
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return summary


def _no_base_help(flight: Flight, base_dir: str | None) -> str:
    """Human readable instructions when no base RINEX covers the flight."""
    first, last, _ = scan_obs_span(flight.obs)
    lines = [
        "",
        "=" * 72,
        f" No base RINEX covers this flight: {flight.name}",
        "=" * 72,
        f" Flight observed:   {span_local(first, last)}  ({first:%H:%M} - {last:%H:%M} GPST)",
        f" Looked in:         {flight.directory}",
    ]
    if base_dir:
        lines.append(f"                    {base_dir}")
    lines += [
        " Rejected files are listed above with the time span they cover.",
        "",
        " What to do:",
        "   1. Order a Virtual RINEX for the parameters below (ESTPOS portal, Post Processing -> RINEX Data,",
        "      tick 'Virtual RINEX'). The form uses Estonian time, like the DJI folder name.",
        f"   2. Copy the downloaded .??o / .rnx / .zip into {flight.directory}",
        "   3. Run this command again.",
        "",
    ]
    try:
        lines.append(format_order(plan_order(flight), flight))
    except Exception as exc:  # noqa: BLE001
        lines.append(f" (could not compute the order parameters: {exc})")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- commands

def cmd_estpos_window(a: argparse.Namespace) -> int:
    flight = load_flight(_flight_path(a.flight))
    order = plan_order(flight, a.buffer, a.height)
    print(format_order(order, flight))
    return 0


def cmd_check_base(a: argparse.Namespace) -> int:
    flight = load_flight(_flight_path(a.flight)) if a.flight else None
    work = Path(a.work or "/tmp/ppk-check")
    plain = prepare_obs(Path(a.base), work)
    hdr, checks = check_base(plain, flight, Path(a.antex) if a.antex else None)
    print(f"Base file: {a.base}")
    for c in checks:
        print(f"  [{c.level:>4}] {c.message}")
    worst = "FAIL" if any(c.level == "FAIL" for c in checks) else "WARN" if any(c.level == "WARN" for c in checks) else "PASS"
    print(f"Overall: {worst}")
    return 1 if worst == "FAIL" else 0


def cmd_process(a: argparse.Namespace) -> int:
    flight = load_flight(_flight_path(a.flight))
    if a.base:
        base = Path(a.base)
    else:
        from .watch import resolve_base
        base = resolve_base(flight, Path(a.base_dir) if a.base_dir else None,
                            _out_dir_for(Path(a.out_dir), flight, a.in_place) / "work")
        if base is None:
            log.error("no base file covering the flight in %s or %s", flight.directory, a.base_dir)
            print(_no_base_help(flight, a.base_dir))
            return 2
        log.info("auto-selected base %s", base)
    out_dir = _out_dir_for(Path(a.out_dir), flight, a.in_place)
    if a.name and not a.in_place:
        out_dir = Path(a.out_dir) / a.name
    summary = process_flight(flight, base, out_dir, Path(a.conf), _parse_overrides(a.set), a.geo_accuracy,
                             a.fixed_only, a.keep_work, [Path(n) for n in (a.nav or [])])
    print(f"\nResults in {out_dir}: geo.txt ({summary['geo_txt_rows']} rows), events.csv, summary.json, accuracy.txt")
    return 0


def cmd_compare(a: argparse.Namespace) -> int:
    ours_path = Path(a.ours)
    if ours_path.is_dir():
        ours_path = ours_path / "events.csv"
    ours = read_events_csv(ours_path)
    ref = read_pos(Path(a.reference))
    res = compare_events(ours, ref.rows, use_antenna=a.antenna, fixed_only=a.fixed_only)
    title = f"{'antenna' if a.antenna else 'camera'} positions from {ours_path} vs {Path(a.reference).name}"
    print(format_report(res, title))
    return 0 if res.passed() else 1


def cmd_watch(a: argparse.Namespace) -> int:
    from .watch import watch
    overrides = _parse_overrides(a.set)

    def run(flight: Flight, base: Path, target: Path) -> None:
        process_flight(flight, base, target, Path(a.conf), overrides, a.geo_accuracy, a.fixed_only)

    watch(Path(a.flights_dir), Path(a.base_dir) if a.base_dir else None, Path(a.out_dir), run,
          int(a.poll or os.environ.get("PPK_POLL_SECONDS", 30)), once=a.once, in_place=a.in_place)
    return 0


def cmd_version(a: argparse.Namespace) -> int:
    print(f"ppk {__version__}")
    print(f"rnx2rtkp: {rtklib_version()}")
    commit = Path("/app/rtklib_commit.txt")
    if commit.exists():
        print(f"RTKLIB source: {commit.read_text().strip()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ppk", description="DJI drone PPK processing with RTKLIB-EX and ESTPOS base data")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("estpos-window", help="print the ESTPOS Virtual RINEX order parameters for a flight")
    s.add_argument("flight", help="flight folder (or .OBS file)")
    s.add_argument("--buffer", type=int, default=5, help="minutes of margin before/after the flight (default 5)")
    s.add_argument("--height", type=float, help="override the virtual point ellipsoidal height (m)")
    s.set_defaults(func=cmd_estpos_window)

    s = sub.add_parser("check-base", help="validate a downloaded base RINEX file, optionally against a flight")
    s.add_argument("base")
    s.add_argument("--flight")
    s.add_argument("--antex", default=os.environ.get("PPK_ANTEX"))
    s.add_argument("--work", help="scratch dir for unpacking compressed files")
    s.set_defaults(func=cmd_check_base)

    s = sub.add_parser("process", help="run PPK for one flight")
    s.add_argument("flight", help="flight folder (or .OBS file)")
    s.add_argument("--base", help="base RINEX file (.??o/.rnx/.crx/.gz/.zip); default: auto-detect")
    s.add_argument("--base-dir", default=DEFAULT_BASE_DIR, help="directory searched for a covering base file")
    s.add_argument("--nav", action="append", help="additional navigation file(s)")
    s.add_argument("--out-dir", default=DEFAULT_OUT)
    s.add_argument("--in-place", action=argparse.BooleanOptionalAction, default=DEFAULT_IN_PLACE,
                   help="write results into the flight folder itself instead of <out-dir>/<flight> (env PPK_IN_PLACE)")
    s.add_argument("--name", help="output sub-directory name (default: flight folder name; ignored with --in-place)")
    s.add_argument("--conf", default=DEFAULT_CONF, help="RTKLIB configuration file")
    s.add_argument("--set", action="append", metavar="KEY=VALUE", help="override an RTKLIB option (repeatable)")
    s.add_argument("--geo-accuracy", action="store_true", help="add horizontal/vertical accuracy columns to geo.txt")
    s.add_argument("--fixed-only", action="store_true", help="only write Q=1 photos to geo.txt")
    s.add_argument("--keep-work", action="store_true", help="keep the work directory (event-injected RINEX etc.)")
    s.set_defaults(func=cmd_process)

    s = sub.add_parser("compare", help="compare events.csv with a reference _events.pos (e.g. Emlid Studio)")
    s.add_argument("ours", help="output directory or events.csv")
    s.add_argument("reference", help="reference _events.pos")
    s.add_argument("--antenna", action="store_true", help="compare antenna positions (no lever arm) instead of camera")
    s.add_argument("--fixed-only", action="store_true")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("watch", help="process flight folders as they appear")
    s.add_argument("flights_dir")
    s.add_argument("--base-dir", default=DEFAULT_BASE_DIR)
    s.add_argument("--out-dir", default=DEFAULT_OUT)
    s.add_argument("--in-place", action=argparse.BooleanOptionalAction, default=DEFAULT_IN_PLACE,
                   help="write results into each flight folder instead of <out-dir>/<flight> (env PPK_IN_PLACE)")
    s.add_argument("--conf", default=DEFAULT_CONF)
    s.add_argument("--set", action="append", metavar="KEY=VALUE")
    s.add_argument("--poll", type=int)
    s.add_argument("--geo-accuracy", action="store_true")
    s.add_argument("--fixed-only", action="store_true")
    s.add_argument("--once", action="store_true", help="scan once and exit (for tests)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("version", help="show ppk and RTKLIB versions")
    s.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
