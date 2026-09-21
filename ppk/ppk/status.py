"""Per-folder overview: sessions, photos, base coverage, results, and what to do next."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .discover import Flight, find_flights, group_by_folder
from .rinex import read_header, scan_obs_span
from .timeutil import span_local
from .watch import resolve_base

NEXT_ORDER, NEXT_PROCESS, NEXT_REPROCESS, NEXT_DONE = "order", "process", "reprocess", "done"


@dataclass
class FolderStatus:
    folder: str
    path: str
    sessions: int
    photos: int
    flown: str
    base: str
    base_ok: bool
    result: str
    next: str

    def row(self) -> list[str]:
        return [self.folder, f"{self.sessions}/{self.photos}", self.flown, self.base, self.result, self.next]


def _span(flights: list[Flight]) -> tuple[datetime | None, datetime | None]:
    first = last = None
    for fl in flights:
        hdr = read_header(fl.obs)
        f, l, _ = scan_obs_span(fl.obs, hdr)
        f, l = f or hdr.first_obs, l or hdr.last_obs
        if f and (first is None or f < first):
            first = f
        if l and (last is None or l > last):
            last = l
    return first, last


def folder_status(directory: Path, flights: list[Flight], base_dir: Path | None) -> FolderStatus:
    flights = sorted(flights, key=lambda f: f.stem)
    photos = sum(len(f.images) for f in flights)
    first, last = _span(flights)
    flown = span_local(first, last) if first and last else "?"

    bases = [resolve_base(f, base_dir) for f in flights]
    if all(bases):
        names = sorted({b.name for b in bases})
        base, base_ok = ", ".join(names), True
    elif any(bases):
        missing = [f.stem for f, b in zip(flights, bases) if b is None]
        base, base_ok = "partial, missing for " + ", ".join(missing), False
    else:
        base, base_ok = "missing", False

    inputs = [p for f in flights for p in (f.obs, f.nav, f.mrk)] + [b for b in bases if b]
    newest_input = max(p.stat().st_mtime for p in inputs)
    summary_path = directory / "summary.json"
    result, nxt = "not processed", NEXT_PROCESS
    if summary_path.exists():
        try:
            s = json.loads(summary_path.read_text())
            ev = s.get("events", {})
            result = f"{s.get('geo_txt_rows', '?')} rows in geo.txt, {ev.get('fix', '?')}/{ev.get('mrk', '?')} fixed"
            sessions_done = len(s.get("sessions", [s.get("rover_obs")]))
            if summary_path.stat().st_mtime < newest_input or sessions_done != len(flights):
                result += " (outdated)"
                nxt = NEXT_REPROCESS
            else:
                nxt = NEXT_DONE
        except (OSError, ValueError):
            result, nxt = "summary.json unreadable", NEXT_REPROCESS
    if not base_ok:
        nxt = NEXT_ORDER
    return FolderStatus(directory.name, str(directory), len(flights), photos, flown, base, base_ok, result, nxt)


def scan_status(root: Path, base_dir: Path | None) -> list[FolderStatus]:
    print(f"scanning {root} ...", file=sys.stderr, flush=True)
    groups = group_by_folder(find_flights(root))
    return [folder_status(d, fls, base_dir) for d, fls in sorted(groups.items())]


def format_status(rows: list[FolderStatus]) -> str:
    if not rows:
        return "no flight folders (OBS/NAV/MRK triplets) found"
    head = ["folder", "sess/photos", "flown", "base", "result", "next"]
    table = [head] + [r.row() for r in rows]
    widths = [max(len(str(row[i])) for row in table) for i in range(len(head))]
    out = []
    for n, row in enumerate(table):
        out.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def status_json(rows: list[FolderStatus]) -> str:
    return json.dumps([asdict(r) for r in rows], indent=1)
