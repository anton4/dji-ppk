"""Per-folder overview: sessions, photos, base coverage, results, and what to do next."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .discover import Flight, find_flights, group_by_folder
from .mrk import has_exposure_times, parse_mrk
from .outputs import RESULTS_DIR, find_summary, results_dir
from .estpos import rinex_days_left, RINEX_RETENTION_DAYS
from .rinex import read_header, scan_obs_span, has_nav_files
from .timeutil import span_local
from .watch import resolve_base

NEXT_ORDER, NEXT_PHOTOS, NEXT_PROCESS, NEXT_REPROCESS, NEXT_DONE, NEXT_EXPIRED = "order", "photos", "process", "reprocess", "done", "expired"
NEXT_NO_TIMES = "no-times"  # the MRK has no exposure times: PPK is impossible, ordering RINEX would be wasted
NEXT_SURVEY = "survey"  # a D-RTK 3 base point: convert the log, order the Virtual RINEX, static survey
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
    kind: str = "flight"  # "flight" (OBS/NAV/MRK sessions) or "basepoint" (D-RTK 3 raw logs to survey)

    @property
    def count_text(self) -> str:
        if self.kind == "basepoint":
            return f"{self.sessions} session{'s' if self.sessions != 1 else ''}"
        return f"{self.sessions}/{self.photos}"

    def row(self) -> list[str]:
        flown = self.flown
        if self.rinex_days_left is not None and 0 <= self.rinex_days_left <= RETENTION_WARN_DAYS:
            flown += f" ({self.rinex_days_left} d left)"
        return [self.folder, self.count_text, flown, self.base, self.result, self.accuracy, self.next]


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
        if all(has_nav_files(b) for b in bases):
            base, base_ok = ", ".join(names), True
        else:
            base, base_ok = ", ".join(names) + " (no nav files)", False  # GPS unusable with the DJI NAV: order the zip
    elif any(bases):
        missing = [f.stem for f, b in zip(flights, bases) if b is None]
        base, base_ok = "partial, missing for " + ", ".join(missing), False
    else:
        base, base_ok = "missing", False

    inputs = [p for f in flights for p in (f.obs, f.nav, f.mrk)] + [b for b in bases if b]
    newest_input = max(p.stat().st_mtime for p in inputs)
    summary_path = find_summary(directory) or results_dir(directory) / "summary.json"
    result, nxt = "not processed", NEXT_PROCESS
    if summary_path.exists():
        try:
            s = json.loads(summary_path.read_text())
            ev = s.get("events", {})
            result = f"{s.get('geo_txt_rows', '?')} rows in geo.txt, {ev.get('fix', '?')}/{ev.get('mrk', '?')} fixed"
            weak = (ev.get("float") or 0) + (ev.get("other") or 0) + (ev.get("unsolved") or 0)
            if weak:
                parts = [f"{ev['float']} float" if ev.get("float") else "", f"{ev['other']} other" if ev.get("other") else "",
                         f"{ev['unsolved']} unsolved" if ev.get("unsolved") else ""]
                result += ", " + ", ".join(p for p in parts if p) + " (not cm-accurate)"
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
            if summary_path.parent == directory:  # old layout: accuracy.txt & co next to the photos break WebODM
                result += f" (old layout: reports not in {RESULTS_DIR}/)"
                nxt = NEXT_REPROCESS
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
    untimed = untimed_mrk(flights)
    if untimed:
        result = f"MRK has no exposure times ({untimed}): PPK impossible, use the on-board RTK positions"
        nxt = NEXT_NO_TIMES
    accuracy = accuracy_text(summary_path) if summary_path.exists() else ""
    return FolderStatus(directory.name, str(directory), len(flights), photos, flown, base, base_ok, result, nxt, days_left, accuracy)


def untimed_mrk(flights: list[Flight]) -> str:
    """'DJI wrote week -522' for the first session whose MRK has no exposure times, else ''."""
    for f in flights:
        try:
            events = parse_mrk(f.mrk)
        except Exception:  # noqa: BLE001
            continue
        if not has_exposure_times(events):
            return f"DJI wrote week {events[0].week}" + ("" if len(flights) == 1 else f" in {f.mrk.name}")
    return ""


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


def basepoint_status(bp, base_dir: Path | None, now: datetime | None = None, root: Path | None = None) -> FolderStatus:
    """A D-RTK 3 base point folder: logs, their span, the covering Virtual RINEX, the survey result, next step."""
    from .basepoint import obs_span, JSON_NAME
    from .watch import resolve_base_for_obs
    converted = bp.converted()
    n_sessions = len(bp.sessions())
    first = last = None
    for _log, obs in converted:
        try:
            f, l, _ = obs_span(obs)
        except (OSError, ValueError):
            continue
        first = f if first is None or f < first else first
        last = l if last is None or l > last else last
    flown = span_local(first, last) if first and last else ("DAT not converted yet" if not converted else "?")
    surveyable = bp.surveyable()  # the 2 min log written during the calibration is not surveyed, so it needs no base
    bases = {obs: resolve_base_for_obs(obs, bp.directory, base_dir, bp.own_files(), bp.base_search_dirs()) for _l, obs in surveyable}
    good = {obs: b for obs, b in bases.items() if b is not None and has_nav_files(b)}
    if bases and len(good) == len(bases):
        base, base_ok = ", ".join(sorted({b.name for b in good.values()})), True
    elif good:
        base, base_ok = ", ".join(sorted({b.name for b in good.values()})) + f" ({len(good)} of {len(bases)} sessions)", True
    elif any(bases.values()):
        base, base_ok = ", ".join(sorted({b.name for b in bases.values() if b})) + " (no nav files)", False
    elif converted and not surveyable:
        base, base_ok = "missing (no session long enough)", False
    else:
        base, base_ok = "missing", False
    newest_input = max(p.stat().st_mtime for l in bp.logs for p in (l.obs, l.nav, l.dat, l.mrk) if p is not None)
    json_path = bp.directory / JSON_NAME
    result, nxt = "not surveyed", NEXT_SURVEY
    settings = bp.settings()
    sv = settings.get("surveyed") or {}
    if sv.get("at"):
        cm = sv.get("correction_mm")
        if cm:
            result = f"surveyed: correction {cm['horizontal'] / 10:.1f} cm H / {cm['up'] / 10:+.1f} cm up"
        else:
            result = "surveyed (no broadcast position in the log)"
        if sv.get("ground"):
            result += ", Manual Calibration coordinates in basepoint.txt"
        st = sv.get("sessions", [{}])[-1].get("static", {}) if sv.get("sessions") else {}
        if st.get("level") == "WARN":
            result += " (weak fix)"
        nxt = NEXT_DONE
        if (json_path.exists() and json_path.stat().st_mtime < newest_input) or len(sv.get("sessions", [])) != n_sessions:
            result += " (outdated: new session)"
            nxt = NEXT_SURVEY
    days_left = rinex_days_left(first, now) if first else None
    if not base_ok and nxt != NEXT_DONE and days_left is not None and days_left < 0:
        nxt = NEXT_EXPIRED
    return FolderStatus(bp.label(root), str(bp.directory), n_sessions, 0, flown, base, base_ok, result, nxt, days_left, "", "basepoint")


def scan_status(root: Path, base_dir: Path | None) -> list[FolderStatus]:
    from .basepoint import find_basepoints
    print(f"scanning {root} ...", file=sys.stderr, flush=True)
    groups = group_by_folder(find_flights(root))
    rows = [folder_status(d, fls, base_dir) for d, fls in sorted(groups.items())]
    flight_dirs = {d.resolve() for d in groups}
    rows += [basepoint_status(bp, base_dir, root=root) for bp in find_basepoints(root) if bp.directory.resolve() not in flight_dirs]
    return sorted(rows, key=lambda r: r.folder)


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
    if any(r.kind == "basepoint" for r in rows):
        out.append("'survey' = a D-RTK 3 base point folder (the station's own DRTK3_* logs): order the Virtual RINEX, static survey; "
                   "the corrected coordinates for Manual Calibration end up in basepoint.txt.")
    return "\n".join(out)


def status_json(rows: list[FolderStatus]) -> str:
    return json.dumps([asdict(r) for r in rows], indent=1)
