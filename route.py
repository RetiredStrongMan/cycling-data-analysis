"""GPX route file parser + course analysis.

What this gives you, given a GPX (or TCX) file:
  - per-point lat/lon/elevation with cumulative distance and smoothed gradient
  - total distance / elevation gain / loss
  - segmentation into fixed-length chunks (default 500 m) with average grade
  - climb detection (continuous sections above a grade threshold)

The math is deliberately simple and dependency-free:
  * Haversine for inter-point distance
  * 50-meter moving window to smooth elevation before computing grade — raw
    GPX elevation is noisy and produces unusable per-point gradients.
  * Climb categorization roughly follows UCI / Strava conventions
    (HC > Cat1 > Cat2 > Cat3 > Cat4) using length_km × avg_grade_pct as points.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable


# Earth radius in meters.
_R_EARTH = 6371008.8

# GPX 1.0/1.1 default namespace; we'll strip namespaces while parsing to keep
# the code simple.
def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _haversine(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in meters."""
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 2 * _R_EARTH * math.asin(math.sqrt(a))


@dataclass
class RoutePoint:
    lat: float
    lon: float
    ele: float            # meters above sea level
    distance_m: float     # cumulative from start
    grade: float = 0.0    # decimal (0.05 = 5%)


@dataclass
class RouteSegment:
    start_km: float
    end_km: float
    distance_m: float
    elev_start: float
    elev_end: float
    elev_gain: float
    elev_loss: float
    grade_pct: float       # average over the segment


@dataclass
class Climb:
    start_km: float
    end_km: float
    length_m: float
    elev_gain: float
    avg_grade_pct: float
    max_grade_pct: float
    category: str          # HC / Cat1..Cat4


@dataclass
class Course:
    name: str
    points: list[RoutePoint] = field(default_factory=list)
    total_distance_m: float = 0.0
    total_elev_gain: float = 0.0
    total_elev_loss: float = 0.0
    max_ele: float = 0.0
    min_ele: float = 0.0

    @property
    def total_km(self) -> float:
        return self.total_distance_m / 1000.0


def parse_gpx(content: bytes | str, name_fallback: str = "Route") -> Course:
    """Parse GPX 1.0 or 1.1 into a Course. Also tolerates TCX-ish files by
    looking for Trackpoint/Position/LatitudeDegrees structures.
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="ignore")
    else:
        text = content
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"无法解析路书文件:{e}") from e

    course = Course(name=name_fallback)

    # Extract a name from <gpx><trk><name>...</name></trk></gpx> if present.
    for el in root.iter():
        if _strip_ns(el.tag) == "name" and el.text and el.text.strip():
            course.name = el.text.strip()
            break

    # Collect trackpoints. Handles GPX <trkpt> and TCX <Trackpoint>.
    raw: list[tuple[float, float, float]] = []
    for el in root.iter():
        tag = _strip_ns(el.tag)
        if tag == "trkpt" or tag == "rtept":
            try:
                lat = float(el.attrib["lat"])
                lon = float(el.attrib["lon"])
            except (KeyError, ValueError):
                continue
            ele = 0.0
            for child in el:
                if _strip_ns(child.tag) == "ele" and child.text:
                    try: ele = float(child.text); break
                    except ValueError: pass
            raw.append((lat, lon, ele))
        elif tag == "Trackpoint":
            lat = lon = None
            ele = 0.0
            for child in el.iter():
                t = _strip_ns(child.tag)
                if t == "LatitudeDegrees" and child.text:
                    try: lat = float(child.text)
                    except ValueError: pass
                elif t == "LongitudeDegrees" and child.text:
                    try: lon = float(child.text)
                    except ValueError: pass
                elif t == "AltitudeMeters" and child.text:
                    try: ele = float(child.text)
                    except ValueError: pass
            if lat is not None and lon is not None:
                raw.append((lat, lon, ele))

    if len(raw) < 2:
        raise ValueError("路书文件未找到足够的轨迹点。")

    # Compute cumulative distance, drop duplicate consecutive points.
    points: list[RoutePoint] = []
    cum = 0.0
    prev = None
    for lat, lon, ele in raw:
        if prev is not None:
            d = _haversine(prev[0], prev[1], lat, lon)
            if d < 0.5:  # <0.5m apart, skip
                continue
            cum += d
        points.append(RoutePoint(lat=lat, lon=lon, ele=ele, distance_m=cum))
        prev = (lat, lon, ele)

    if not points:
        raise ValueError("路书文件未包含可用的距离数据。")

    # Smooth elevation with a 50-m moving window to compute grade.
    _compute_grades(points, window_m=50.0)

    course.points = points
    course.total_distance_m = points[-1].distance_m
    course.max_ele = max(p.ele for p in points)
    course.min_ele = min(p.ele for p in points)

    gain, loss = 0.0, 0.0
    for a, b in zip(points, points[1:]):
        dh = b.ele - a.ele
        if dh > 0: gain += dh
        else: loss -= dh
    course.total_elev_gain = gain
    course.total_elev_loss = loss
    return course


def _compute_grades(points: list[RoutePoint], window_m: float = 50.0) -> None:
    """In-place: set p.grade for each point using a centered ±window_m moving
    average of elevation, then a finite-difference grade. Smoothing absorbs the
    well-known noise in consumer GPS elevation."""
    n = len(points)
    # First pass: smooth elevations into a parallel array.
    smoothed = [0.0] * n
    j_lo = 0
    for i, p in enumerate(points):
        # advance window bounds
        while j_lo < n and points[j_lo].distance_m < p.distance_m - window_m:
            j_lo += 1
        j_hi = i
        while j_hi + 1 < n and points[j_hi + 1].distance_m <= p.distance_m + window_m:
            j_hi += 1
        s = 0.0
        cnt = 0
        for k in range(j_lo, j_hi + 1):
            s += points[k].ele
            cnt += 1
        smoothed[i] = s / cnt if cnt else p.ele

    # Second pass: per-point grade from smoothed elevation deltas over ~window_m.
    for i, p in enumerate(points):
        target = p.distance_m + window_m
        j = i
        while j + 1 < n and points[j + 1].distance_m <= target:
            j += 1
        if j == i and i > 0:
            j = i
            i_back = i - 1
            d = p.distance_m - points[i_back].distance_m
            if d > 0:
                p.grade = (smoothed[i] - smoothed[i_back]) / d
            continue
        d = points[j].distance_m - p.distance_m
        if d > 0:
            p.grade = (smoothed[j] - smoothed[i]) / d
        else:
            p.grade = 0.0


def segment_course(course: Course, segment_m: float = 500.0) -> list[RouteSegment]:
    """Cut the course into roughly fixed-length chunks for tabular display.

    Single forward pass: open a new segment whenever the accumulated distance
    since the previous segment boundary reaches `segment_m`. Each segment uses
    its endpoint elevation delta for grade, and sums up/down deltas between
    interior points for total gain/loss.
    """
    pts = course.points
    if len(pts) < 2:
        return []

    segs: list[RouteSegment] = []
    start_idx = 0
    n = len(pts)
    for i in range(1, n):
        elapsed = pts[i].distance_m - pts[start_idx].distance_m
        is_last = (i == n - 1)
        if elapsed >= segment_m or is_last:
            seg_pts = pts[start_idx:i + 1]
            gain, loss = 0.0, 0.0
            for a, b in zip(seg_pts, seg_pts[1:]):
                dh = b.ele - a.ele
                if dh > 0: gain += dh
                else: loss -= dh
            dist = seg_pts[-1].distance_m - seg_pts[0].distance_m
            if dist <= 0:
                start_idx = i
                continue
            grade = (seg_pts[-1].ele - seg_pts[0].ele) / dist * 100.0
            segs.append(RouteSegment(
                start_km=seg_pts[0].distance_m / 1000.0,
                end_km=seg_pts[-1].distance_m / 1000.0,
                distance_m=dist,
                elev_start=seg_pts[0].ele,
                elev_end=seg_pts[-1].ele,
                elev_gain=gain,
                elev_loss=loss,
                grade_pct=grade,
            ))
            start_idx = i
    return segs


def find_climbs(course: Course, min_length_m: float = 500.0,
                min_grade: float = 0.03) -> list[Climb]:
    """Detect continuous climbing sections above `min_grade` for at least `min_length_m`.

    A climb starts when smoothed grade rises above `min_grade` and ends when it
    drops below half of `min_grade` for a sustained distance.
    """
    climbs: list[Climb] = []
    pts = course.points
    n = len(pts)
    if n < 2:
        return climbs

    in_climb = False
    start_i = 0
    end_i = 0
    exit_threshold = min_grade * 0.5  # hysteresis
    exit_buffer_m = 200.0             # need to be below exit_threshold for this far to end

    i = 0
    while i < n:
        p = pts[i]
        if not in_climb and p.grade >= min_grade:
            in_climb = True
            start_i = i
            end_i = i
        elif in_climb:
            if p.grade >= exit_threshold:
                end_i = i
            else:
                # check if we've truly dropped off — look ahead exit_buffer_m
                lookahead = p.distance_m + exit_buffer_m
                k = i
                still_low = True
                while k < n and pts[k].distance_m <= lookahead:
                    if pts[k].grade >= min_grade:
                        still_low = False
                        break
                    k += 1
                if still_low:
                    # finalize climb
                    s, e = pts[start_i], pts[end_i]
                    length = e.distance_m - s.distance_m
                    if length >= min_length_m and e.ele > s.ele:
                        climb_pts = pts[start_i:end_i + 1]
                        max_g = max((cp.grade for cp in climb_pts), default=0)
                        avg_g = (e.ele - s.ele) / length * 100
                        cat = _climb_category(length, avg_g)
                        climbs.append(Climb(
                            start_km=s.distance_m / 1000.0,
                            end_km=e.distance_m / 1000.0,
                            length_m=length,
                            elev_gain=e.ele - s.ele,
                            avg_grade_pct=avg_g,
                            max_grade_pct=max_g * 100,
                            category=cat,
                        ))
                    in_climb = False
        i += 1

    # Edge case: route ends mid-climb
    if in_climb and end_i > start_i:
        s, e = pts[start_i], pts[end_i]
        length = e.distance_m - s.distance_m
        if length >= min_length_m and e.ele > s.ele:
            climb_pts = pts[start_i:end_i + 1]
            max_g = max((cp.grade for cp in climb_pts), default=0)
            avg_g = (e.ele - s.ele) / length * 100
            climbs.append(Climb(
                start_km=s.distance_m / 1000.0,
                end_km=e.distance_m / 1000.0,
                length_m=length,
                elev_gain=e.ele - s.ele,
                avg_grade_pct=avg_g,
                max_grade_pct=max_g * 100,
                category=_climb_category(length, avg_g),
            ))
    return climbs


def _climb_category(length_m: float, avg_grade_pct: float) -> str:
    """Score = length_km × avg_grade_pct. Boundaries roughly match UCI/Strava."""
    score = (length_m / 1000.0) * avg_grade_pct
    if score >= 80:  return "HC"
    if score >= 40:  return "Cat 1"
    if score >= 20:  return "Cat 2"
    if score >= 10:  return "Cat 3"
    if score >= 4:   return "Cat 4"
    return "—"
