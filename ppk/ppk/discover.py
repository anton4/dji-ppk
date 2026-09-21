"""Locate DJI flight folders (OBS + NAV + MRK triplets) and their images."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

IMAGE_RE = re.compile(r"_(\d{4})(?:_([A-Z]))?\.(?:JPG|JPEG|DNG)$", re.I)
IMAGE_TS_RE = re.compile(r"^DJI_(\d{14})_\d{4}", re.I)  # capture time (local) in the file name
PREFERRED_CAMERA = "V"  # DJI suffixes: V visible, W wide, Z zoom, T thermal
SESSION_MARGIN = timedelta(minutes=2)


@dataclass
class Flight:
    directory: Path
    obs: Path
    nav: Path
    mrk: Path
    images: dict[int, Path] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.directory.name

    @property
    def stem(self) -> str:
        return self.obs.stem


def _sibling(base: Path, ext: str) -> Path | None:
    for cand in (base.with_suffix(ext.upper()), base.with_suffix(ext.lower())):
        if cand.exists():
            return cand
    return None


def image_timestamp(path: Path) -> datetime | None:
    """Local capture time encoded in a DJI file name, e.g. DJI_20260905113447_0001_V.JPG."""
    m = IMAGE_TS_RE.match(path.name)
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S") if m else None
    except ValueError:
        return None


def index_images(directory: Path, window: tuple[datetime, datetime] | None = None) -> dict[int, Path]:
    """Photo index -> file. `window` (local times) keeps only photos taken in that span, which separates two
    flight sessions stored in one folder: DJI restarts the index at 0001 for every session."""
    found: dict[int, dict[str, Path]] = {}
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        m = IMAGE_RE.search(p.name)
        if not m:
            continue
        if window:
            ts = image_timestamp(p)
            if ts is not None and not (window[0] - SESSION_MARGIN <= ts <= window[1] + SESSION_MARGIN):
                continue
        found.setdefault(int(m.group(1)), {})[(m.group(2) or "").upper()] = p
    out: dict[int, Path] = {}
    for idx, cams in found.items():
        if PREFERRED_CAMERA in cams:
            out[idx] = cams[PREFERRED_CAMERA]
        else:
            out[idx] = sorted(cams.values())[0]
    return out


def find_flights(root: str | Path, recursive: bool = True) -> list[Flight]:
    root = Path(root)
    flights: list[Flight] = []
    it = root.rglob("*") if recursive else root.iterdir()
    for obs in sorted(p for p in it if p.is_file() and p.suffix.lower() == ".obs"):
        base = obs.with_suffix("")
        nav = _sibling(base, ".nav")
        mrk = _sibling(base, ".mrk")
        if nav and mrk:
            flights.append(Flight(obs.parent, obs, nav, mrk, index_images(obs.parent, session_window(mrk))))
    return flights


def session_window(mrk_path: Path) -> tuple[datetime, datetime] | None:
    """Local time span of the camera events in a .MRK, for picking this session's photos."""
    try:
        from .mrk import parse_mrk
        from .timeutil import gpst_to_local
        events = parse_mrk(mrk_path)
        if not events:
            return None
        first = gpst_to_local(events[0].time).replace(tzinfo=None)
        last = gpst_to_local(events[-1].time).replace(tzinfo=None)
        return first, last
    except Exception:  # noqa: BLE001 - unreadable MRK: fall back to all photos
        return None


def load_flights(path: str | Path) -> list[Flight]:
    """All flight sessions (OBS/NAV/MRK triplets) of a folder, oldest first, or the one session of a .OBS path.

    A folder holds several sessions when the pilot swapped batteries or restarted after rain: each restart
    gives a new triplet and a photo index starting at 0001 again. They belong to one flight day and share one
    base file and one geo.txt.
    """
    p = Path(path)
    if p.is_file():
        flights = find_flights(p.parent, recursive=False)
        flights = [f for f in flights if f.obs.resolve() == p.resolve()]
    else:
        flights = find_flights(p, recursive=False)
    if not flights:
        raise FileNotFoundError(f"{path}: no OBS/NAV/MRK triplet found")
    return sorted(flights, key=lambda f: f.stem)


def load_flight(path: str | Path) -> Flight:
    """Exactly one session: a .OBS path, or a folder with a single triplet."""
    flights = load_flights(path)
    if len(flights) > 1:
        raise ValueError(f"{path}: {len(flights)} sessions found, pass the .OBS file explicitly")
    return flights[0]


def group_by_folder(flights: list[Flight]) -> dict[Path, list[Flight]]:
    groups: dict[Path, list[Flight]] = {}
    for f in flights:
        groups.setdefault(f.directory, []).append(f)
    return {k: sorted(v, key=lambda f: f.stem) for k, v in groups.items()}
