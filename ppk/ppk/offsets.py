"""Apply antenna-to-camera lever arms in the local north/east/down frame."""
from __future__ import annotations

import math

from .rinex import WGS84_A, WGS84_E2


def radii(lat_deg: float) -> tuple[float, float]:
    """Meridional (M) and prime-vertical (N) radii of curvature."""
    s = math.sin(math.radians(lat_deg))
    w = math.sqrt(1 - WGS84_E2 * s * s)
    return WGS84_A * (1 - WGS84_E2) / w ** 3, WGS84_A / w


def apply_ned_offset(lat: float, lon: float, h: float, north_m: float, east_m: float, down_m: float) -> tuple[float, float, float]:
    m, n = radii(lat)
    dlat = math.degrees(north_m / (m + h))
    dlon = math.degrees(east_m / ((n + h) * math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon, h - down_m


def ned_difference(lat1: float, lon1: float, h1: float, lat2: float, lon2: float, h2: float) -> tuple[float, float, float]:
    """Vector (north, east, up) in metres from point 1 to point 2."""
    lat0 = (lat1 + lat2) / 2
    m, n = radii(lat0)
    h0 = (h1 + h2) / 2
    dn = math.radians(lat2 - lat1) * (m + h0)
    de = math.radians(lon2 - lon1) * (n + h0) * math.cos(math.radians(lat0))
    return dn, de, h2 - h1
