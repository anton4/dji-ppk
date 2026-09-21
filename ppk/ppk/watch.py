"""Folder watcher: process new flight folders once a covering base file is available."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import traceback
from pathlib import Path

from .discover import find_flights, Flight
from .rinex import find_base_candidates, read_header, scan_obs_span, prepare_obs

log = logging.getLogger("ppk.watch")


def _signature(flight: Flight) -> tuple:
    return tuple((p.name, p.stat().st_size, int(p.stat().st_mtime)) for p in (flight.obs, flight.nav, flight.mrk))


def _span(path: Path) -> tuple | None:
    try:
        hdr = read_header(path)
        first, last, _ = scan_obs_span(path, hdr)
        return (first or hdr.first_obs, last or hdr.last_obs)
    except Exception:  # noqa: BLE001
        return None


def resolve_base(flight: Flight, base_dir: Path | None, workdir: Path | None = None) -> Path | None:
    """Base file inside the flight folder wins; otherwise the first file in base_dir covering the flight."""
    span = _span(flight.obs)
    candidates = find_base_candidates(flight.directory)
    if base_dir and base_dir.is_dir():
        candidates += find_base_candidates(base_dir)
    # Unpack compressed candidates into a temporary directory, not into the output folder, so a
    # flight without a covering base does not get an empty work/ directory next to its photos.
    scratch = tempfile.mkdtemp(prefix="ppk-basecheck-")
    try:
        for cand in candidates:
            try:
                plain = prepare_obs(cand, scratch)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot unpack %s: %s", cand, exc)
                continue
            bspan = _span(plain)
            if not span or not bspan or None in span or None in bspan:
                log.debug("%s: cannot determine observation span", cand.name)
                continue
            if bspan[0] <= span[0] and bspan[1] >= span[1]:
                return cand
            log.info("%s covers %s - %s, flight needs %s - %s: skipped", cand.name,
                     bspan[0], bspan[1], span[0], span[1])
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def watch(flights_dir: Path, base_dir: Path | None, out_dir: Path, process_fn, poll_seconds: int = 30,
          once: bool = False, settle_seconds: int = 120, in_place: bool = False) -> None:
    from .cli import _out_dir_for  # local import to avoid a cycle
    seen: dict[str, tuple] = {}
    log.info("watching %s (base dir %s, output %s, poll %ss)", flights_dir, base_dir,
             "in the flight folders" if in_place else out_dir, poll_seconds)
    while True:
        try:
            flights = find_flights(flights_dir)
        except Exception as exc:  # noqa: BLE001
            log.error("scan failed: %s", exc)
            flights = []
        for fl in flights:
            key = str(fl.obs)
            try:
                sig = _signature(fl)
            except OSError:
                continue
            target = _out_dir_for(out_dir, fl, in_place)
            done, failed = target / "DONE", target / "FAILED.log"
            if done.exists():
                continue
            if failed.exists() and seen.get(key) == sig:
                continue  # already failed with these inputs; wait for them to change
            if seen.get(key) != sig:
                seen[key] = sig
                newest = max(p.stat().st_mtime for p in (fl.obs, fl.nav, fl.mrk))
                if time.time() - newest < settle_seconds:
                    log.info("new/changed flight %s, waiting for files to settle", fl.name)
                    continue  # recently written: require a stable signature over two polls
            base = resolve_base(fl, base_dir, target / "work")
            if base is None:
                log.info("%s: no base file covering the flight yet", fl.name)
                continue
            log.info("processing %s with base %s", fl.name, base.name)
            try:
                process_fn(fl, base, target)
                done.write_text(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
                if failed.exists():
                    failed.unlink()
                log.info("%s: done", fl.name)
            except Exception:  # noqa: BLE001
                target.mkdir(parents=True, exist_ok=True)
                failed.write_text(traceback.format_exc())
                log.error("%s: FAILED, see %s", fl.name, failed)
        if once:
            return
        time.sleep(poll_seconds)
