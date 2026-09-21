"""Inject camera exposure events (RINEX epoch flag 5) into a copy of the rover observation file.

RTKLIB (postpos.c) then interpolates the solution to each event time and writes them to
<outfile>_events.pos, which is exactly how Emlid Studio handles DJI .MRK timestamps.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .timeutil import format_rinex3_event, parse_rinex3_epoch
from .rinex import read_header


def write_obs_with_events(src: str | Path, dst: str | Path, event_times: list[datetime]) -> dict:
    """Stream-copy `src` to `dst` inserting an event record before the first epoch later than the event.

    Returns statistics: inserted, before_first_epoch, after_last_epoch, epochs.
    """
    header = read_header(src)
    if header.version < 3:
        raise ValueError("only RINEX 3 rover files are supported for event injection")
    pending = sorted(event_times)
    idx = 0
    stats = {"inserted": 0, "before_first_epoch": 0, "after_last_epoch": 0, "epochs": 0}
    first_epoch = True
    with open(src, "r", errors="replace") as fi, open(dst, "w") as fo:
        for _ in range(header.header_lines):
            fo.write(next(fi))
        for line in fi:
            if line.startswith(">"):
                try:
                    t, flag, _ = parse_rinex3_epoch(line)
                except ValueError:
                    fo.write(line)
                    continue
                if flag <= 1:
                    stats["epochs"] += 1
                    while idx < len(pending) and pending[idx] <= t:
                        fo.write(format_rinex3_event(pending[idx]) + "\n")
                        stats["inserted"] += 1
                        if first_epoch:
                            stats["before_first_epoch"] += 1
                        idx += 1
                    first_epoch = False
            fo.write(line)
        while idx < len(pending):
            fo.write(format_rinex3_event(pending[idx]) + "\n")
            stats["inserted"] += 1
            stats["after_last_epoch"] += 1
            idx += 1
    return stats
