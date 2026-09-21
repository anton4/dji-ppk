"""Parser for RTKLIB .pos solution files (llh format, hms time)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .timeutil import parse_pos_time, datetime_to_tow

Q_LABEL = {0: "none", 1: "fix", 2: "float", 3: "sbas", 4: "dgps", 5: "single", 6: "ppp"}


@dataclass
class PosRow:
    time: datetime
    lat: float
    lon: float
    h: float
    q: int
    ns: int
    sdn: float = 0.0
    sde: float = 0.0
    sdu: float = 0.0
    sdne: float = 0.0
    sdeu: float = 0.0
    sdun: float = 0.0
    age: float = 0.0
    ratio: float = 0.0

    @property
    def tow(self) -> float:
        return datetime_to_tow(self.time)


@dataclass
class PosFile:
    path: Path
    header: list[str]
    rows: list[PosRow]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            k = Q_LABEL.get(r.q, str(r.q))
            out[k] = out.get(k, 0) + 1
        out["total"] = len(self.rows)
        return out

    @property
    def fix_ratio(self) -> float:
        return sum(1 for r in self.rows if r.q == 1) / len(self.rows) if self.rows else 0.0


def read_pos(path: str | Path) -> PosFile:
    header: list[str] = []
    rows: list[PosRow] = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if line.startswith("%"):
                header.append(line.rstrip("\n"))
                continue
            t = line.split()
            if len(t) < 6:
                continue
            try:
                time = parse_pos_time(t[0], t[1])
                nums = [float(x) for x in t[2:]]
            except ValueError:
                continue
            lat, lon, h, q, ns = nums[0], nums[1], nums[2], int(nums[3]), int(nums[4])
            extra = nums[5:] + [0.0] * (8 - len(nums[5:]))
            rows.append(PosRow(time, lat, lon, h, q, ns, *extra[:8]))
    return PosFile(Path(path), header, rows)
