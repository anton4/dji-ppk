"""Per-folder overview: sessions, photos, base coverage, results, and what to do next."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .discover import Flight, find_flights, group_by_folder
from .mrk import parse_mrk
from .estpos import rinex_days_left, RINEX_RETENTION_DAYS
from .rinex import read_header, scan_obs_span
from .timeutil import span_local
from .watch import resolve_base

NEXT_ORDER, NEXT_PHOTOS, NEXT_PROCESS, NEXT_REPROCESS, NEXT_DONE, NEXT_EXPIRED = "order", "photos", "process", "reprocess", "done", "expired"
RETENTION_WARN_DAYS = 14


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
    rinex_days_left: int | None = None
    accuracy: str = ""  # "H 41 cm→0.4 cm  V 21 cm→0.6 cm": typical photo error as flown (on-board RTK) → after PPK (RTKLIB estimate)

    def row(self) -> list[str]:
        flown = self.flown
        if self.rinex_days_left is not None and 0 <= self.rinex_days_left <= RETENTION_WARN_DAYS:
            flown += f" ({self.rinex_days_left} d left)"
        return [self.folder, f"{self.sessions}/{self.photos}", flown, self.base, self.result, self.accuracy, self.next]


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


def folder_status(directory: Path, flights: list[Flight], base_dir: Path | None, now: datetime | None = None) -> FolderStatus:
    flights = sorted(flights, key=lambda f: f.stem)
    photos = sum(len(f.images) for f in flights)
    try:
        expected = sum(len(parse_mrk(f.mrk)) for f in flights)  # one camera event per photo
    except Exception:  # noqa: BLE001
        expected = photos
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
            rows_then = s.get("geo_txt_rows") or 0
            if summary_path.stat().st_mtime < newest_input or sessions_done != len(flights):
                result += " (outdated)"
                nxt = NEXT_REPROCESS
            elif photos > rows_then:
                result += f" (photos arrived since: {photos} present)"
                nxt = NEXT_REPROCESS
            else:
                nxt = NEXT_DONE
        except (OSError, ValueError):
            result, nxt = "summary.json unreadable", NEXT_REPROCESS
    if photos < expected:
        result += f"; {expected - photos} of {expected} photos not in the folder yet"
        if nxt in (NEXT_DONE, NEXT_PROCESS):
            nxt = NEXT_PHOTOS  # copy still running? processing now would give a short geo.txt
    days_left = rinex_days_left(first, now) if first else None
    if not base_ok:
        nxt = NEXT_ORDER
        if days_left is not None and days_left < 0:
            nxt = NEXT_EXPIRED  # the reason is in the next column; keep the base column short
    accuracy = accuracy_text(summary_path) if summary_path.exists() else ""
    return FolderStatus(directory.name, str(directory), len(flights), photos, flown, base, base_ok, result, nxt, days_left, accuracy)


def _cm(mm: float | None) -> str:
    if mm is None:
        return "?"
    cm = mm / 10
    return f"{cm:.0f} cm" if cm >= 10 else f"{cm:.1f} cm"


def accuracy_text(summary_path: Path) -> str:
    """'H 41 cm→0.4 cm  V 21 cm→0.6 cm': typical horizontal and vertical error of the photo positions as flown
    (on-board RTK vs PPK, rms) and RTKLIB's estimated 1-sigma after PPK. For several sessions the worst session is shown."""
    try:
        s = json.loads(summary_path.read_text())
    except (OSError, ValueError):
        return ""
    sessions = s.get("session_summaries") or [s]
    flown_h, flown_v, ppk_h, ppk_v = [], [], [], []
    for ss in sessions:
        rtk = ss.get("rtk_vs_ppk") or {}
        if rtk.get("horizontal_error", {}).get("rms_mm") is not None:
            flown_h.append(rtk["horizontal_error"]["rms_mm"])
        if rtk.get("vertical_error", {}).get("rms_mm") is not None:
            flown_v.append(rtk["vertical_error"]["rms_mm"])
        sd = (ss.get("quality") or {}).get("std_mm", {})
        if sd.get("horizontal", {}).get("median") is not None:
            ppk_h.append(sd["horizontal"]["median"])
        if sd.get("up", {}).get("median") is not None:
            ppk_v.append(sd["up"]["median"])
    if not ppk_h:
        return ""
    h = (_cm(max(flown_h)) if flown_h else "?") + "→" + _cm(max(ppk_h))
    v = ((_cm(max(flown_v)) if flown_v else "?") + "→" + _cm(max(ppk_v))) if ppk_v else "?"
    return f"H {h}  V {v}"


def scan_status(root: Path, base_dir: Path | None) -> list[FolderStatus]:
    print(f"scanning {root} ...", file=sys.stderr, flush=True)
    groups = group_by_folder(find_flights(root))
    return [folder_status(d, fls, base_dir) for d, fls in sorted(groups.items())]


def format_status(rows: list[FolderStatus]) -> str:
    if not rows:
        return "no flight folders (OBS/NAV/MRK triplets) found"
    head = ["folder", "sess/photos", "flown", "base", "result", "accuracy flown→PPK", "next"]
    table = [head] + [r.row() for r in rows]
    widths = [max(len(str(row[i])) for row in table) for i in range(len(head))]
    out = []
    for n, row in enumerate(table):
        out.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    out.append("")
    out.append(f"ESTPOS provides RINEX for the last {RINEX_RETENTION_DAYS} days only: folders marked 'expired' cannot get a base file "
               f"any more (unless one is already in the folder); flights within {RETENTION_WARN_DAYS} days of the limit show the days left.")
    return "\n".join(out)


def status_json(rows: list[FolderStatus]) -> str:
    return json.dumps([asdict(r) for r in rows], indent=1)
