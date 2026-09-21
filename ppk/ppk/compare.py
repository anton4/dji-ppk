"""Compare our camera events with a reference _events.pos (e.g. Emlid Studio)."""
from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .offsets import ned_difference
from .outputs import CameraEvent
from .pos import PosRow, read_pos

MATCH_TOL_S = 0.0015


@dataclass
class CompareResult:
    n_ours: int
    n_ref: int
    n_matched: int
    fix_ours: int
    fix_ref: int
    dn_mm: list[float] = field(default_factory=list)
    de_mm: list[float] = field(default_factory=list)
    du_mm: list[float] = field(default_factory=list)
    d3_mm: list[float] = field(default_factory=list)
    mismatched_q: int = 0

    def stats(self, arr: list[float]) -> dict[str, float]:
        if not arr:
            return {}
        s = sorted(arr)
        return {
            "mean": statistics.fmean(arr),
            "median": statistics.median(arr),
            "p95": s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))],
            "max": max(arr, key=abs),
            "rms": math.sqrt(sum(v * v for v in arr) / len(arr)),
        }

    def passed(self, median_3d_mm: float = 10.0, fix_slack: int = 1) -> bool:
        return (self.n_matched > 0 and self.fix_ours >= self.fix_ref - fix_slack
                and statistics.median(self.d3_mm) < median_3d_mm)

    def as_dict(self) -> dict:
        return {
            "n_ours": self.n_ours, "n_ref": self.n_ref, "n_matched": self.n_matched,
            "fix_ours": self.fix_ours, "fix_ref": self.fix_ref, "mismatched_q": self.mismatched_q,
            "north_mm": self.stats(self.dn_mm), "east_mm": self.stats(self.de_mm),
            "up_mm": self.stats(self.du_mm), "d3_mm": self.stats(self.d3_mm),
            "passed": self.passed(),
        }


def compare_events(ours: list[CameraEvent], ref: list[PosRow], use_antenna: bool = False,
                   fixed_only: bool = False) -> CompareResult:
    ref = sorted(ref, key=lambda r: r.time)
    times = [r.time for r in ref]
    res = CompareResult(len(ours), len(ref), 0, sum(1 for e in ours if e.q == 1), sum(1 for r in ref if r.q == 1))
    for e in ours:
        i = bisect.bisect_left(times, e.time)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ref):
                dt = abs((ref[j].time - e.time).total_seconds())
                if dt <= MATCH_TOL_S and (best is None or dt < best[0]):
                    best = (dt, ref[j])
        if best is None:
            continue
        r = best[1]
        if fixed_only and (e.q != 1 or r.q != 1):
            continue
        if e.q != r.q:
            res.mismatched_q += 1
        lat, lon, h = (e.ant_lat, e.ant_lon, e.ant_h) if use_antenna else (e.lat, e.lon, e.ellh)
        dn, de, du = ned_difference(r.lat, r.lon, r.h, lat, lon, h)
        res.n_matched += 1
        res.dn_mm.append(dn * 1000)
        res.de_mm.append(de * 1000)
        res.du_mm.append(du * 1000)
        res.d3_mm.append(math.hypot(dn, de, du) * 1000)
    return res


def format_report(res: CompareResult, title: str) -> str:
    def row(name: str, st: dict[str, float]) -> str:
        if not st:
            return f" {name:<10} (no data)"
        return (f" {name:<10} mean {st['mean']:8.1f}  median {st['median']:8.1f}  rms {st['rms']:8.1f}"
                f"  p95 {st['p95']:8.1f}  max {st['max']:9.1f}")
    lines = [
        "=" * 78, f" {title}", "=" * 78,
        f" events: ours {res.n_ours}  reference {res.n_ref}  matched (±1.5 ms) {res.n_matched}",
        f" fixed (Q=1): ours {res.fix_ours}  reference {res.fix_ref}  differing Q flags {res.mismatched_q}",
        "-" * 78, " differences ours - reference, in millimetres:",
        row("north", res.stats(res.dn_mm)), row("east", res.stats(res.de_mm)),
        row("up", res.stats(res.du_mm)), row("3D", res.stats(res.d3_mm)),
        "-" * 78,
        " RESULT: " + ("PASS (median 3D < 10 mm and fix count within 1 of reference)" if res.passed()
                       else "NOTICE: does not meet the parity target (median 3D < 10 mm, fix count within 1)"),
        "=" * 78,
    ]
    return "\n".join(lines)


def find_reference_events(directory: Path, exclude: Path | None = None) -> list[Path]:
    """Reference *_events*.pos files in a flight folder, best (most fixed rows) last."""
    out = []
    for p in sorted(directory.glob("*_events*.pos")):
        if exclude and p.resolve() == exclude.resolve():
            continue
        if p.name.endswith("_trajectory_events.pos"):
            continue  # our own output (antenna positions), never a reference
        try:
            out.append((sum(1 for r in read_pos(p).rows if r.q == 1), p))
        except OSError:
            continue
    return [p for _, p in sorted(out, key=lambda x: (x[0], x[1].name))]
