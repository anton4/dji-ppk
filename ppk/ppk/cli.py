"""ppk command line interface."""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

from . import __version__
from .compare import compare_events, format_report, find_reference_events
from .discover import Flight, load_flight, load_flights
from .estpos import check_base, format_order, plan_order, plan_orders, rinex_days_left, RINEX_RETENTION_DAYS
from .events import write_obs_with_events
from .mrk import parse_mrk
from .outputs import match_events, read_events_csv, write_events_csv, write_geo_txt, write_summary, solution_quality, format_quality, rtk_vs_ppk, format_rtk_vs_ppk, format_in_short
from .pos import read_pos
from .rinex import prepare_obs, read_header, scan_obs_span, find_base_candidates, find_nav_files, stale_nav_systems
from .rtklib import rtklib_version, run_rnx2rtkp, write_conf
from .timeutil import span_local
from . import ui

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
                   fixed_only: bool = False, keep_work: bool = False, extra_nav: list[Path] | None = None,
                   prefix: str = "", matched_out: list | None = None) -> dict:
    """Process one session. `prefix` (e.g. '<stem>_') names the outputs when a folder holds several sessions;
    `matched_out`, if given, receives the CameraEvents so the caller can merge sessions into one geo.txt."""
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    for stale in (out_dir / "DONE", out_dir / "FAILED.log"):
        if stale.exists():
            stale.unlink()

    mrk = parse_mrk(flight.mrk)
    log.info(ui.step(1, 5, "%s: %d camera events in %s, %d images"), flight.name, len(mrk), flight.mrk.name, len(flight.images))
    missing_images = [e.id for e in mrk if e.id not in flight.images]
    if missing_images:
        log.warning("%d MRK events have no matching image file (first: %s)", len(missing_images), missing_images[:5])

    log.info(ui.step(2, 5, "base %s: unpacking and checking"), Path(base).name)
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

    log.info(ui.step(3, 5, "injecting %d exposure events into the rover observations"), len(mrk))
    rover_events = work / (flight.stem + "_events.obs")
    stats = write_obs_with_events(flight.obs, rover_events, [e.time for e in mrk])
    log.info("injected %d event epochs into %s (%d obs epochs)", stats["inserted"], rover_events.name, stats["epochs"])
    if stats["before_first_epoch"] or stats["after_last_epoch"]:
        log.warning("%d events before first / %d after last observation epoch",
                    stats["before_first_epoch"], stats["after_last_epoch"])

    if (overrides or {}).get("misc-timeinterp", "off").lower() in ("on", "1"):
        log.warning("misc-timeinterp=on suppresses RTKLIB's *_events.pos output; expect no camera solutions")
    conf_used = write_conf(conf, out_dir / f"{prefix}rtklib_used.conf", overrides)
    out_pos = out_dir / f"{flight.stem}_trajectory.pos"
    navs = [flight.nav] + list(extra_nav or []) + base_navs
    log.info(ui.step(4, 5, "running %s ..."), rtklib_version())
    res = run_rnx2rtkp(conf_used, out_pos, rover_events, base_plain, navs, out_dir / f"{prefix}rtklib.log")
    log.info("rnx2rtkp finished in %.1f s (exit %d)", res.seconds, res.returncode)
    if not res.events_path.exists() or not res.pos_path.exists():
        raise RuntimeError(f"rnx2rtkp produced no solution, see {res.log_path}")

    traj = read_pos(res.pos_path)
    ev_pos = read_pos(res.events_path)
    matched, unmatched = match_events(mrk, ev_pos.rows, flight.images)
    if unmatched:
        log.warning("%d MRK events have no RTKLIB solution (ids %s ...)", len(unmatched), [e.id for e in unmatched][:5])

    log.info(ui.step(5, 5, "writing outputs"))
    csv_path = out_dir / f"{prefix}events.csv"
    write_events_csv(csv_path, matched)
    n_geo = write_geo_txt(out_dir / f"{prefix}geo.txt", matched, with_accuracy=geo_accuracy, fixed_only=fixed_only)
    if matched_out is not None:
        matched_out.extend(matched)
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
        "outputs": {"events_csv": csv_path.name, "geo_txt": f"{prefix}geo.txt", "trajectory_pos": res.pos_path.name,
                    "events_pos": res.events_path.name, "rtklib_log": f"{prefix}rtklib.log", "conf": conf_used.name},
    }
    write_summary(out_dir / f"{prefix}summary.json", summary)
    ev = summary["events"]
    fixed_pct = 100 * ev["fix"] / len(mrk) if mrk else 0.0
    verdict = f"{ev['fix']}/{len(mrk)} photos fixed ({ui.pct(fixed_pct)}), trajectory {ui.pct(100 * traj.fix_ratio)} fixed"
    if ev["float"] or ev["other"] or ev["unsolved"]:
        verdict += f"; {ev['float']} float, {ev['other']} other, {ev['unsolved']} unsolved"
    log.info("%s", ui.ok(verdict) if fixed_pct >= 99 else ui.warn(verdict) if fixed_pct >= 95 else ui.bad(verdict))

    quality = solution_quality(matched, len(mrk), traj.rows)
    summary["quality"] = quality
    baseline_km = None
    for c in checks:
        m = re.search(r"baseline to flight area ([0-9.]+) km", c.message)
        if m:
            baseline_km = float(m.group(1))
    refs = find_reference_events(flight.directory, exclude=res.events_path)
    own = [r for r in refs if r.name.startswith(flight.stem)]
    refs = own or ([] if prefix else refs)  # several sessions: only a reference named after this session counts
    print()
    print(ui.colorize_report(format_quality(quality, flight.name, Path(base).name, baseline_km, refs[-1].name if refs else None)))
    rtk = rtk_vs_ppk(matched)
    summary["rtk_vs_ppk"] = rtk
    print(ui.colorize_report(format_rtk_vs_ppk(rtk)))
    in_short = format_in_short(rtk, quality)
    (out_dir / f"{prefix}accuracy.txt").write_text(in_short + "\n")
    if refs:
        ref = read_pos(refs[-1])
        cmp_res = compare_events(matched, ref.rows)
        report = format_report(cmp_res, f"ours vs reference {refs[-1].name}")
        (out_dir / f"{prefix}compare_report.txt").write_text(report + "\n")
        summary["compare"] = {"reference": refs[-1].name, **cmp_res.as_dict()}
        print(ui.colorize_report(report))
    print(ui.colorize_report(in_short))
    write_summary(out_dir / f"{prefix}summary.json", summary)
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return summary


def _no_base_help(flights, base_dir: str | None) -> str:
    """Human readable instructions when no base RINEX covers the flight (all sessions of the folder)."""
    flights = list(flights) if isinstance(flights, (list, tuple)) else [flights]
    flight = flights[0]
    spans = [scan_obs_span(fl.obs)[:2] for fl in flights]
    first = min(sp[0] for sp in spans)
    last = max(sp[1] for sp in spans)
    lines = [
        "",
        "=" * 72,
        f" No base RINEX covers this flight: {flight.name}" + ("" if len(flights) == 1 else f" ({len(flights)} sessions)"),
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
        for order, fls in plan_orders(flights):
            lines.append(format_order(order, fls))
    except Exception as exc:  # noqa: BLE001
        lines.append(f" (could not compute the order parameters: {exc})")
    return "\n".join(lines)


def process_sessions(flights: list[Flight], bases: list[Path], out_dir: Path, conf: Path = Path(DEFAULT_CONF),
                     overrides: dict[str, str] | None = None, geo_accuracy: bool = False, fixed_only: bool = False,
                     keep_work: bool = False, extra_nav: list[Path] | None = None) -> dict:
    """Process every session of a flight day and, with more than one, merge them into one geo.txt / events.csv /
    summary.json / accuracy.txt (per-session files keep the session stem as prefix)."""
    if len(flights) == 1:
        return process_flight(flights[0], bases[0], out_dir, conf, overrides, geo_accuracy, fixed_only, keep_work, extra_nav)
    all_matched: list = []
    summaries = []
    for i, (fl, base) in enumerate(zip(flights, bases), 1):
        log.info("%s", ui.c(f"── session {i}/{len(flights)}: {fl.stem} ──", "bold", "magenta"))
        summaries.append(process_flight(fl, base, out_dir, conf, overrides, geo_accuracy, fixed_only, keep_work,
                                        extra_nav, prefix=f"{fl.stem}_", matched_out=all_matched))
    all_matched.sort(key=lambda e: e.time)
    write_events_csv(out_dir / "events.csv", all_matched)
    n_geo = write_geo_txt(out_dir / "geo.txt", all_matched, with_accuracy=geo_accuracy, fixed_only=fixed_only)
    merged = merge_summaries(flights[0].name, summaries, n_geo)
    write_summary(out_dir / "summary.json", merged)
    parts = []
    for fl in flights:
        acc = out_dir / f"{fl.stem}_accuracy.txt"
        if acc.exists():
            parts.append(f"### session {fl.stem}\n" + acc.read_text())
    (out_dir / "accuracy.txt").write_text("\n".join(parts))
    ev = merged["events"]
    log.info("all sessions: %d photos, %d fixed, %d unsolved; geo.txt has %d rows", ev["mrk"], ev["fix"], ev["unsolved"], n_geo)
    return merged


def merge_summaries(name: str, summaries: list[dict], n_geo: int) -> dict:
    keys = ("mrk", "solved", "unsolved", "fix", "float", "other")
    events = {k: sum(s["events"][k] for s in summaries) for k in keys}
    traj_total = sum(s["trajectory"].get("total", 0) for s in summaries)
    traj_fix = sum(s["trajectory"].get("fix", 0) for s in summaries)
    return {
        "flight": name, "sessions": [s["rover_obs"] for s in summaries], "session_summaries": summaries,
        "events": events, "geo_txt_rows": n_geo,
        "trajectory": {"fix": traj_fix, "total": traj_total},
        "trajectory_fix_ratio": round(traj_fix / traj_total, 4) if traj_total else 0.0,
        "images_missing": sum(s["images_missing"] for s in summaries),
        "seconds": round(sum(s["seconds"] for s in summaries), 1),
        "outputs": {"events_csv": "events.csv", "geo_txt": "geo.txt", "per_session": [s["outputs"] for s in summaries]},
    }


# ----------------------------------------------------------------------------- commands

def _print_orders(orders) -> None:
    if len(orders) > 1:
        print(f"\n {len(orders)} orders needed: the sessions span more than the portal's limit, split at the gaps between sessions")
    for order, fls in orders:
        print(format_order(order, fls))


def cmd_estpos_window(a: argparse.Namespace) -> int:
    flights = load_flights(_flight_path(a.flight))
    _print_orders(plan_orders(flights, a.buffer, a.height, a.max_hours))
    return 0


def _estpos_credentials() -> tuple[str, str]:
    user, pw = os.environ.get("ESTPOS_USER", ""), os.environ.get("ESTPOS_PASSWORD", "")
    if not user or not pw:
        raise SystemExit("ESTPOS_USER and ESTPOS_PASSWORD are not set. Put them into .env (it is gitignored) and rerun.")
    return user, pw


def cmd_estpos_order(a: argparse.Namespace) -> int:
    """Order a Virtual RINEX for a flight on the ESTPOS portal and download it into the flight folder."""
    try:
        from . import estpos_web
    except ImportError as exc:
        raise SystemExit(f"playwright is not installed ({exc}); use the ppk-estpos compose service: "
                         "docker compose --profile cli run --rm ppk-estpos estpos-order <flight>")
    flights = load_flights(_flight_path(a.flight))
    flight = flights[0]
    if not a.force:
        have = resolve_bases(flights, None, DEFAULT_BASE_DIR)
        if all(have):
            names = sorted({b.name for b in have})
            print(f"{flight.name}: base file{'s' if len(names) > 1 else ''} {', '.join(names)} already cover"
                  f"{'s' if len(names) == 1 else ''} all {len(flights)} session{'s' if len(flights) > 1 else ''}; nothing to order.")
            print(f"Run: ppk process {flight.directory}   (use --force to order anyway)")
            return 0
    orders = plan_orders(flights, a.buffer, a.height, a.max_hours)
    _print_orders(orders)
    left = min(rinex_days_left(o.flight_first_gpst) for o, _f in orders)
    if left < 0:
        log.error("this flight is %d days past ESTPOS's %d-day RINEX retention: no Virtual RINEX can be ordered for it any more",
                  -left, RINEX_RETENTION_DAYS)
        return 2
    if left <= 7:
        log.warning("only %d day(s) left before ESTPOS drops the RINEX data for this flight", left)
    base_project = (a.project or flight.name)
    if len(orders) > 1:
        base_project = base_project[:28]
        projects = [f"{base_project}-{i}" for i in range(1, len(orders) + 1)]
    else:
        projects = [base_project]
    for order, _fls in orders:
        if order.duration > timedelta(hours=a.max_hours):
            log.warning("order of %.2f h exceeds the %.1f h limit (one session is that long); the portal may refuse it",
                        order.duration.total_seconds() / 3600, a.max_hours)
    user, pw = _estpos_credentials()
    dest = flight.directory
    shot = dest / "estpos_order_form.png" if (a.dry_run or a.screenshot) else None
    height = orders[0][0].height if a.send_height else None
    try:
        paths = estpos_web.order_and_download_many([(o, p) for (o, _f), p in zip(orders, projects)], dest, user, pw,
                                                   rate_s=a.rate, height=height, wait=not a.no_wait, timeout_min=a.timeout,
                                                   headed=a.headed, dry_run=a.dry_run, screenshot=shot)
    except Exception as exc:  # noqa: BLE001
        log.error("%s", exc)
        return 1
    rc = 0
    for path in paths:
        rc = max(rc, _report_downloaded_base(path, flight, flights))
    return rc


def cmd_estpos_download(a: argparse.Namespace) -> int:
    """Download an already ordered Virtual RINEX (by project name) into the flight folder."""
    try:
        from . import estpos_web
    except ImportError as exc:
        raise SystemExit(f"playwright is not installed ({exc}); use the ppk-estpos compose service")
    flights = load_flights(_flight_path(a.flight))
    flight = flights[0]
    user, pw = _estpos_credentials()
    if not a.force:
        have = resolve_bases(flights, None, DEFAULT_BASE_DIR)
        if all(have):
            print(f"{flight.name}: base file {', '.join(sorted({b.name for b in have}))} already covers every session; nothing to download.")
            return 0
    try:
        if a.project:
            paths = [estpos_web.download_existing(a.project, flight.directory, user, pw, timeout_min=a.timeout, headed=a.headed)]
        else:
            # same planning as estpos-order, so the project names and spans match what was ordered before
            orders = plan_orders(flights, a.buffer, None, a.max_hours)
            base_project = flight.name if len(orders) == 1 else flight.name[:28]
            projects = [base_project] if len(orders) == 1 else [f"{base_project}-{i}" for i in range(1, len(orders) + 1)]
            paths = estpos_web.download_for_orders([(o, p) for (o, _f), p in zip(orders, projects)], flight.directory, user, pw,
                                                   timeout_min=a.timeout, headed=a.headed)
    except Exception as exc:  # noqa: BLE001
        log.error("%s", exc)
        return 1
    rc = 0
    for path in paths:
        rc = max(rc, _report_downloaded_base(path, flight, flights))
    return rc


def _report_downloaded_base(path: Path, flight: Flight, flights: list[Flight] | None = None) -> int:
    """Validate a downloaded base against every session of the folder and print the checks."""
    import tempfile
    sessions = flights or [flight]
    checks = []
    with tempfile.TemporaryDirectory(prefix="ppk-basecheck-") as tmp:
        plain = prepare_obs(path, tmp)
        for i, fl in enumerate(sessions):
            _, cs = check_base(plain, fl, Path(os.environ.get("PPK_ANTEX", "")) or None)
            # base-only checks are identical for every session: keep them once, flight checks per session
            checks += [c for c in cs if i == 0 or "flight" in c.message or "baseline" in c.message]
    print(f"\nBase file: {path}")
    for c in checks:
        if "ANTEX" in c.message and not os.environ.get("PPK_ANTEX"):
            continue  # the portal image has no ANTEX file; `ppk process` checks the antenna in the processing image
        print(ui.colorize_report(f"  [{c.level:>4}] {c.message}"))
    worst = "FAIL" if any(c.level == "FAIL" for c in checks) else "PASS"
    print(ui.colorize_report(f"Overall: {worst}"))
    print(f"\nNext: ppk process {flight.directory}   (or wait for the watcher)")
    return 1 if worst == "FAIL" else 0


def cmd_check_base(a: argparse.Namespace) -> int:
    flight = load_flight(_flight_path(a.flight)) if a.flight else None
    work = Path(a.work or "/tmp/ppk-check")
    plain = prepare_obs(Path(a.base), work)
    hdr, checks = check_base(plain, flight, Path(a.antex) if a.antex else None)
    print(f"Base file: {a.base}")
    for c in checks:
        print(ui.colorize_report(f"  [{c.level:>4}] {c.message}"))
    worst = "FAIL" if any(c.level == "FAIL" for c in checks) else "WARN" if any(c.level == "WARN" for c in checks) else "PASS"
    print(ui.colorize_report(f"Overall: {worst}"))
    return 1 if worst == "FAIL" else 0


def resolve_bases(flights: list[Flight], base_arg: str | None, base_dir: str | None) -> list[Path | None]:
    """Base file per session: the --base argument for all, else auto-detection per session."""
    if base_arg:
        return [Path(base_arg)] * len(flights)
    from .watch import resolve_base
    return [resolve_base(fl, Path(base_dir) if base_dir else None) for fl in flights]


def cmd_process(a: argparse.Namespace) -> int:
    flights = load_flights(_flight_path(a.flight))
    if len(flights) > 1:
        log.info("%s: %d sessions (%s), results are merged into one geo.txt", flights[0].name, len(flights),
                 ", ".join(f.stem for f in flights))
    bases = resolve_bases(flights, a.base, a.base_dir)
    missing = [fl for fl, b in zip(flights, bases) if b is None]
    if missing:
        log.error("no base file covering %s in %s or %s", ", ".join(f.stem for f in missing), flights[0].directory, a.base_dir)
        print(_no_base_help(flights, a.base_dir))
        return 2
    for fl, b in zip(flights, bases):
        log.info("%s: base %s", fl.stem, b.name)
    out_dir = _out_dir_for(Path(a.out_dir), flights[0], a.in_place)
    if a.name and not a.in_place:
        out_dir = Path(a.out_dir) / a.name
    summary = process_sessions(flights, bases, out_dir, Path(a.conf), _parse_overrides(a.set), a.geo_accuracy,
                               a.fixed_only, a.keep_work, [Path(n) for n in (a.nav or [])])
    ev = summary["events"]
    print()
    print(ui.ok(f"{flights[0].name}: {ev['fix']}/{ev['mrk']} photos fixed, geo.txt has {summary['geo_txt_rows']} rows")
          if ev["fix"] == ev["mrk"] else ui.warn(f"{flights[0].name}: {ev['fix']}/{ev['mrk']} photos fixed, {ev['unsolved']} unsolved, "
                                                 f"geo.txt has {summary['geo_txt_rows']} rows"))
    print(ui.c(f"  in {out_dir}: geo.txt, events.csv, summary.json, accuracy.txt", "dim"))
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

    def run(flights: list[Flight], bases: list[Path], target: Path) -> None:
        process_sessions(flights, bases, target, Path(a.conf), overrides, a.geo_accuracy, a.fixed_only)

    watch(Path(a.flights_dir), Path(a.base_dir) if a.base_dir else None, Path(a.out_dir), run,
          int(a.poll or os.environ.get("PPK_POLL_SECONDS", 30)), once=a.once, in_place=a.in_place)
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    from .status import scan_status, format_status, status_json
    logging.getLogger("ppk.watch").setLevel(logging.WARNING)  # keep "candidate skipped" chatter out of the table
    rows = scan_status(Path(a.root), Path(a.base_dir) if a.base_dir else None)
    if a.json:
        print(status_json(rows))
    else:
        text = format_status(rows)
        for key in ui.NEXT_COLORS:
            text = re.sub(rf"(\S)(\s+)({key})$", lambda m: m.group(1) + m.group(2) + ui.colorize_next(m.group(3)), text, flags=re.M)
        print(text)
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
    s.add_argument("--max-hours", type=float, default=6.0, help="max length of one Virtual RINEX order; a longer flight day is split into several orders at the gaps between sessions (each order only spans its sessions plus the buffer)")
    s.set_defaults(func=cmd_estpos_window)

    s = sub.add_parser("estpos-order", help="order the Virtual RINEX for a flight on the ESTPOS portal and download it (Playwright)")
    s.add_argument("flight", help="flight folder (or .OBS file)")
    s.add_argument("--project", help="project name shown on the portal (default: flight folder name)")
    s.add_argument("--buffer", type=int, default=5, help="minutes of margin before/after the flight (default 5)")
    s.add_argument("--height", type=float, help="override the virtual point ellipsoidal height (m)")
    s.add_argument("--send-height", action=argparse.BooleanOptionalAction, default=True,
                   help="fill the height field (default) or leave it to the portal's automatic value")
    s.add_argument("--rate", type=int, default=1, choices=(1, 5, 10, 15, 20, 30, 60), help="observation rate in seconds")
    s.add_argument("--max-hours", type=float, default=6.0, help="max length of one Virtual RINEX order; a longer flight day is split into several orders at the gaps between sessions (each order only spans its sessions plus the buffer)")
    s.add_argument("--no-wait", action="store_true", help="submit only; download later with estpos-download")
    s.add_argument("--timeout", type=float, default=60, help="minutes to wait for the portal to prepare the file")
    s.add_argument("--dry-run", action="store_true", help="fill and verify the form, save a screenshot, do not submit")
    s.add_argument("--force", action="store_true", help="order even if a base file in the folder already covers every session")
    s.add_argument("--screenshot", action="store_true", help="save estpos_order_form.png next to the flight before submitting")
    s.add_argument("--headed", action="store_true", help="show the browser (host only)")
    s.set_defaults(func=cmd_estpos_order)

    s = sub.add_parser("estpos-download", help="download an already ordered Virtual RINEX into the flight folder; never orders")
    s.add_argument("flight", help="flight folder (or .OBS file)")
    s.add_argument("--project", help="project name used when ordering (default: match by the flight's own span and folder name)")
    s.add_argument("--buffer", type=int, default=5)
    s.add_argument("--max-hours", type=float, default=6.0)
    s.add_argument("--timeout", type=float, default=0, help="minutes to keep polling if it is still processing (default: no wait)")
    s.add_argument("--force", action="store_true", help="download even if a base file in the folder already covers every session")
    s.add_argument("--headed", action="store_true")
    s.set_defaults(func=cmd_estpos_download)

    s = sub.add_parser("check-base", help="validate a downloaded base RINEX file, optionally against a flight")
    s.add_argument("base")
    s.add_argument("--flight")
    s.add_argument("--antex", default=os.environ.get("PPK_ANTEX"))
    s.add_argument("--work", help="scratch dir for unpacking compressed files")
    s.set_defaults(func=cmd_check_base)

    s = sub.add_parser("process", help="run PPK for one flight")
    s.add_argument("flight", help="flight folder (all sessions, merged geo.txt) or one .OBS file")
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

    s = sub.add_parser("status", help="one line per flight folder: sessions, photos, base coverage, result, next step")
    s.add_argument("root", nargs="?", default=DEFAULT_FLIGHTS_DIR)
    s.add_argument("--base-dir", default=DEFAULT_BASE_DIR)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("version", help="show ppk and RTKLIB versions")
    s.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = logging.StreamHandler()
    handler.setFormatter(ui.ColorFormatter())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=[handler])
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
