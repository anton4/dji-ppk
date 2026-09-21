"""RINEX observation file helpers: header parsing, observation span, unpacking, ECEF/LLH."""
from __future__ import annotations

import gzip
import math
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .timeutil import parse_rinex3_epoch

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)

OBS_NAME_RE = re.compile(r"(\.\d{2}[oO]$|\.rnx$|\.obs$|\.crx$|\.\d{2}[dD]$|\.gz$|\.Z$|\.zip$)", re.I)


@dataclass
class RinexHeader:
    version: float = 0.0
    file_type: str = ""
    program: str = ""
    marker_name: str = ""
    marker_number: str = ""
    marker_type: str = ""
    rec_type: str = ""
    ant_type: str = ""
    approx_xyz: tuple[float, float, float] | None = None
    ant_delta_hen: tuple[float, float, float] | None = None
    interval: float | None = None
    first_obs: datetime | None = None
    last_obs: datetime | None = None
    obs_types: dict[str, list[str]] = field(default_factory=dict)
    comments: list[str] = field(default_factory=list)
    header_lines: int = 0

    @property
    def is_virtual(self) -> bool:
        blob = " ".join(self.comments + [self.marker_name, self.marker_number]).lower()
        return "virtual" in blob or "vrnx" in blob

    @property
    def approx_llh(self) -> tuple[float, float, float] | None:
        if not self.approx_xyz or all(v == 0 for v in self.approx_xyz):
            return None
        return ecef_to_llh(*self.approx_xyz)


def _parse_time_of_obs(text: str) -> datetime | None:
    t = text.split()
    if len(t) < 6:
        return None
    y, mo, d, h, mi = (int(float(x)) for x in t[:5])
    sec = float(t[5])
    return datetime(y, mo, d, h, mi) + timedelta(seconds=sec)


def read_header(path: str | Path) -> RinexHeader:
    hdr = RinexHeader()
    cur_sys = None
    with open(path, "r", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            label = line[60:].strip()
            body = line[:60]
            if "RINEX VERSION / TYPE" in label:
                hdr.version = float(body[:9] or 0)
                hdr.file_type = body[20:21]
            elif "PGM / RUN BY / DATE" in label:
                hdr.program = body[:20].strip()
            elif label == "MARKER NAME":
                hdr.marker_name = body.strip()
            elif label == "MARKER NUMBER":
                hdr.marker_number = body.strip()
            elif label == "MARKER TYPE":
                hdr.marker_type = body.strip()
            elif "REC # / TYPE / VERS" in label:
                hdr.rec_type = body[20:40].strip()
            elif label.startswith("ANT # / TYPE"):
                hdr.ant_type = body[20:40].rstrip()
            elif "APPROX POSITION XYZ" in label:
                try:
                    hdr.approx_xyz = tuple(float(body[i:i + 14]) for i in (0, 14, 28))  # type: ignore[assignment]
                except ValueError:
                    hdr.approx_xyz = None
            elif "ANTENNA: DELTA H/E/N" in label:
                try:
                    hdr.ant_delta_hen = tuple(float(body[i:i + 14]) for i in (0, 14, 28))  # type: ignore[assignment]
                except ValueError:
                    pass
            elif label == "INTERVAL":
                try:
                    hdr.interval = float(body.strip())
                except ValueError:
                    pass
            elif "TIME OF FIRST OBS" in label:
                hdr.first_obs = _parse_time_of_obs(body[:43])
            elif "TIME OF LAST OBS" in label:
                hdr.last_obs = _parse_time_of_obs(body[:43])
            elif "SYS / # / OBS TYPES" in label:
                if body[0] != " ":
                    cur_sys = body[0]
                    hdr.obs_types[cur_sys] = body[7:].split()
                elif cur_sys:
                    hdr.obs_types[cur_sys].extend(body[7:].split())
            elif label == "COMMENT":
                hdr.comments.append(body.rstrip())
            elif "END OF HEADER" in label:
                hdr.header_lines = n
                break
    if hdr.header_lines == 0:
        raise ValueError(f"{path}: END OF HEADER not found")
    return hdr


def scan_obs_span(path: str | Path, header: RinexHeader | None = None) -> tuple[datetime | None, datetime | None, int]:
    """Return (first_epoch, last_epoch, epoch_count) from the RINEX 3 epoch records."""
    header = header or read_header(path)
    if header.version < 3:
        return header.first_obs, header.last_obs, 0
    first = last = None
    count = 0
    with open(path, "r", errors="replace") as fh:
        for _ in range(header.header_lines):
            next(fh)
        for line in fh:
            if line.startswith(">"):
                try:
                    t, flag, _ = parse_rinex3_epoch(line)
                except ValueError:
                    continue
                if flag > 1:
                    continue
                count += 1
                if first is None:
                    first = t
                last = t
    return first, last, count


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    h = 0.0
    for _ in range(10):
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def llh_to_ecef(lat: float, lon: float, h: float) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(la) ** 2)
    return ((n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
            (n * (1 - WGS84_E2) + h) * math.sin(la))


def _is_hatanaka(path: Path) -> bool:
    with open(path, "r", errors="replace") as fh:
        first = fh.readline()
    return "COMPACT RINEX FORMAT" in first


def prepare_obs(path: str | Path, workdir: str | Path) -> Path:
    """Return a plain-text RINEX observation file, unpacking .zip/.gz/.Z/Hatanaka into workdir if needed."""
    src = Path(path)
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    cur = src
    if cur.suffix.lower() == ".zip":
        with zipfile.ZipFile(cur) as zf:
            names = [n for n in zf.namelist() if re.search(r"(\.\d{2}[oOdD]|\.rnx|\.crx|\.obs)(\.gz)?$", n, re.I)]
            if not names:
                raise ValueError(f"{cur}: no RINEX observation file inside the zip")
            names.sort(key=lambda n: (not re.search(r"(\.\d{2}[oO]|\.rnx|\.obs)$", n, re.I), n))
            cur = Path(zf.extract(names[0], work))
    if cur.suffix.lower() == ".gz":
        dst = work / cur.with_suffix("").name
        with gzip.open(cur, "rb") as fi, open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        cur = dst
    elif cur.suffix == ".Z":
        dst = work / cur.with_suffix("").name
        with open(dst, "wb") as fo:
            subprocess.run(["gzip", "-dc", str(cur)], stdout=fo, check=True)
        cur = dst
    if cur.suffix.lower() == ".crx" or re.search(r"\.\d{2}[dD]$", cur.name) or _is_hatanaka(cur):
        dst = work / re.sub(r"\.crx$|\.(\d{2})[dD]$", lambda m: ".rnx" if m.group(0).lower() == ".crx" else f".{m.group(1)}o", cur.name, flags=re.I)
        if dst == cur:
            dst = work / (cur.stem + ".rnx")
        with open(dst, "wb") as fo:
            subprocess.run(["crx2rnx", "-", ], stdin=open(cur, "rb"), stdout=fo, check=True)
        cur = dst
    return cur


def find_base_candidates(directory: str | Path) -> list[Path]:
    """RINEX-looking observation files in a directory that are not DJI rover logs."""
    out = []
    for p in sorted(Path(directory).iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.name.upper().startswith("DJI_"):
            continue
        if OBS_NAME_RE.search(p.name):
            out.append(p)
    return out
