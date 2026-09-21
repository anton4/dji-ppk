"""Locate DJI flight folders (OBS + NAV + MRK triplets) and their images."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_RE = re.compile(r"_(\d{4})(?:_([A-Z]))?\.(?:JPG|JPEG|DNG)$", re.I)
PREFERRED_CAMERA = "V"  # DJI suffixes: V visible, W wide, Z zoom, T thermal


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


def index_images(directory: Path) -> dict[int, Path]:
    found: dict[int, dict[str, Path]] = {}
    for p in directory.iterdir():
        if not p.is_file():
            continue
        m = IMAGE_RE.search(p.name)
        if not m:
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
            flights.append(Flight(obs.parent, obs, nav, mrk, index_images(obs.parent)))
    return flights


def load_flight(path: str | Path) -> Flight:
    """A flight folder (or a direct .OBS path) must contain exactly one OBS/NAV/MRK triplet."""
    p = Path(path)
    if p.is_file():
        flights = find_flights(p.parent, recursive=False)
        flights = [f for f in flights if f.obs.resolve() == p.resolve()]
    else:
        flights = find_flights(p, recursive=False)
    if not flights:
        raise FileNotFoundError(f"{path}: no OBS/NAV/MRK triplet found")
    if len(flights) > 1:
        raise ValueError(f"{path}: {len(flights)} triplets found, pass the .OBS file explicitly")
    return flights[0]
