#!/usr/bin/env python3
"""
VicEmergency (Victoria, Australia) public feed client.

Fetches and parses the public GeoJSON feed behind https://emergency.vic.gov.au,
which carries:

* warnings (Emergency Warning, Watch and Act, Advice, Community Update, and
  Bureau of Meteorology flood/weather warnings) issued by Emergency Management
  Victoria, CFA, FRV, SES and partner agencies;
* incidents (fires, floods, tree down, rescues, hazmat, planned burns, ...);
* the day's Fire Danger Ratings and Total Fire Ban status per fire district.

No API key is needed. Parsing is kept free of network and bot dependencies so
the command, the service plugin and tests can share it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import requests

VIC_EVENTS_URL = "https://emergency.vic.gov.au/public/events-geojson.json"
VIC_WARNING_URL = "https://emergency.vic.gov.au/respond/#!/warning/{id}/moreinfo"
VIC_RESPOND_URL = "https://emergency.vic.gov.au/respond/"
USER_AGENT = "meshcore-bot (VicEmergency client)"

# Warning levels, lowest to highest. The Australian Warning System uses three
# levels (Advice, Watch and Act, Emergency Warning); VicEmergency also issues
# "Community Update"/"Community Information" notices below Advice.
LEVEL_COMMUNITY = 1
LEVEL_ADVICE = 2
LEVEL_WATCH_AND_ACT = 3
LEVEL_EMERGENCY = 4

LEVEL_NAMES = {
    LEVEL_COMMUNITY: "community",
    LEVEL_ADVICE: "advice",
    LEVEL_WATCH_AND_ACT: "watch_and_act",
    LEVEL_EMERGENCY: "emergency",
}
LEVEL_ICONS = {
    LEVEL_COMMUNITY: "🔵",
    LEVEL_ADVICE: "🟡",
    LEVEL_WATCH_AND_ACT: "🟠",
    LEVEL_EMERGENCY: "🔴",
}

# Fire Danger Ratings (Australian Fire Danger Rating System), lowest to highest.
FIRE_DANGER_LEVELS = ["NO RATING", "MODERATE", "HIGH", "EXTREME", "CATASTROPHIC"]

# The nine Victorian fire districts used for ratings and Total Fire Bans.
FIRE_DISTRICTS = [
    "Mallee",
    "Wimmera",
    "South West",
    "Northern Country",
    "North Central",
    "Central",
    "North East",
    "West and South Gippsland",
    "East Gippsland",
]

INCIDENT_ICONS = {
    "fire": "🔥",
    "planned burn": "🔥",
    "flooding": "🌊",
    "flood": "🌊",
    "tree down": "🌳",
    "building damage": "🏚",
    "hazardous material": "☣",
    "accident / rescue": "🚑",
    "rescue": "🚑",
    "medical": "🚑",
    "storm": "⛈",
    "earthquake": "🌏",
}


def parse_level(text: Optional[str]) -> int:
    """Map a warning title ("Watch and Act", "Advice", ...) to a level (1-4)."""
    t = (text or "").strip().lower().replace("&", "and")
    if not t:
        return LEVEL_COMMUNITY
    if "emergency" in t or "evacuat" in t:
        return LEVEL_EMERGENCY
    if "watch and act" in t:
        return LEVEL_WATCH_AND_ACT
    if "advice" in t or "warning" in t or "watch" in t:
        return LEVEL_ADVICE
    return LEVEL_COMMUNITY


def level_from_config(value: Optional[str], default: int = LEVEL_ADVICE) -> int:
    """Parse a config value such as ``advice``, ``watch_and_act`` or ``3``."""
    if value is None:
        return default
    v = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not v:
        return default
    if v.isdigit():
        return max(LEVEL_COMMUNITY, min(LEVEL_EMERGENCY, int(v)))
    for level, name in LEVEL_NAMES.items():
        if v == name:
            return level
    aliases = {"community_update": 1, "info": 1, "information": 1, "watch": 3,
               "watchandact": 3, "emergency_warning": 4, "all": 1}
    if v in aliases:
        return aliases[v]
    return parse_level(v.replace("_", " "))


def fire_danger_index(rating: Optional[str]) -> int:
    """Index of a Fire Danger Rating in FIRE_DANGER_LEVELS (0 when unknown)."""
    r = (rating or "").strip().upper()
    if r in FIRE_DANGER_LEVELS:
        return FIRE_DANGER_LEVELS.index(r)
    return 0


@dataclass
class VicEvent:
    """One warning or incident from the VicEmergency feed."""

    id: str
    feed_type: str  # "warning" or "incident"
    category1: str
    category2: str
    name: str
    status: str
    location: str
    created: str
    updated: str
    source_org: str = ""
    action: str = ""
    event: str = ""
    text: str = ""
    size: str = ""
    resources: Optional[int] = None
    url: str = ""
    points: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)
    polygons: list[list[tuple[float, float]]] = field(default_factory=list)  # rings of (lat, lon)

    @property
    def is_warning(self) -> bool:
        return self.feed_type == "warning"

    @property
    def level(self) -> int:
        """Warning level (1-4). Incidents are always LEVEL_COMMUNITY."""
        if not self.is_warning:
            return LEVEL_COMMUNITY
        return parse_level(self.category1 or self.name)

    @property
    def link(self) -> str:
        if self.is_warning:
            return VIC_WARNING_URL.format(id=self.id)
        return self.url or VIC_RESPOND_URL

    @property
    def representative_point(self) -> Optional[tuple[float, float]]:
        if self.points:
            return self.points[0]
        for ring in self.polygons:
            if ring:
                lat = sum(p[0] for p in ring) / len(ring)
                lon = sum(p[1] for p in ring) / len(ring)
                return (lat, lon)
        return None

    def distance_km(self, lat: float, lon: float) -> Optional[float]:
        """Distance from (lat, lon) to the event; 0 when inside a warning area."""
        for ring in self.polygons:
            if _point_in_ring(lat, lon, ring):
                return 0.0
        best: Optional[float] = None
        for plat, plon in self.points:
            d = haversine_km(lat, lon, plat, plon)
            best = d if best is None else min(best, d)
        for ring in self.polygons:
            for plat, plon in ring:
                d = haversine_km(lat, lon, plat, plon)
                best = d if best is None else min(best, d)
        return best

    @property
    def headline(self) -> str:
        """First line of the warning text, e.g. 'ADVICE - GRASS FIRE - Stay Informed'."""
        if self.text:
            first = self.text.strip().splitlines()[0].strip()
            if first:
                return first
        parts = [self.category1 or self.name]
        if self.event:
            parts.append(self.event)
        if self.action:
            parts.append(self.action)
        return " - ".join(p for p in parts if p)

    @property
    def incident_location(self) -> str:
        m = re.search(r"^Incident Location:\s*(.+)$", self.text or "", re.MULTILINE)
        return m.group(1).strip() if m else ""

    @property
    def summary(self) -> str:
        """First bullet point of the warning body, which usually says what is happening."""
        for line in (self.text or "").splitlines():
            s = line.strip()
            if s.startswith("- ") and len(s) > 3:
                return s[2:].strip()
        return ""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _point_in_ring(lat: float, lon: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _collect_geometry(geom: Optional[dict], points: list, polygons: list) -> None:
    if not isinstance(geom, dict):
        return
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    try:
        if gtype == "Point" and coords and len(coords) >= 2:
            points.append((float(coords[1]), float(coords[0])))
        elif gtype == "MultiPoint" and coords:
            for c in coords:
                points.append((float(c[1]), float(c[0])))
        elif gtype == "Polygon" and coords:
            polygons.append([(float(c[1]), float(c[0])) for c in coords[0]])
        elif gtype == "MultiPolygon" and coords:
            for poly in coords:
                if poly:
                    polygons.append([(float(c[1]), float(c[0])) for c in poly[0]])
        elif gtype == "GeometryCollection":
            for g in geom.get("geometries") or []:
                _collect_geometry(g, points, polygons)
    except (TypeError, ValueError, IndexError):
        return


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_feature(feature: dict) -> Optional[VicEvent]:
    """Build a VicEvent from one GeoJSON feature, or None if it is not a warning/incident."""
    props = feature.get("properties") or {}
    feed_type = _s(props.get("feedType")).lower()
    if feed_type not in ("warning", "incident"):
        return None
    event_id = _s(props.get("id") or props.get("sourceId"))
    if not event_id:
        return None
    cap_raw = props.get("cap")
    cap: dict = cap_raw if isinstance(cap_raw, dict) else {}
    points: list[tuple[float, float]] = []
    polygons: list[list[tuple[float, float]]] = []
    _collect_geometry(feature.get("geometry"), points, polygons)
    resources = props.get("resources")
    try:
        resources = int(resources) if resources is not None else None
    except (TypeError, ValueError):
        resources = None
    location = _s(props.get("location"))
    if not location:
        location = _s(props.get("sourceTitle"))
    return VicEvent(
        id=event_id,
        feed_type=feed_type,
        category1=_s(props.get("category1")),
        category2=_s(props.get("category2")),
        name=_s(props.get("name") or props.get("sourceTitle")),
        status=_s(props.get("status")),
        location=location,
        created=_s(props.get("created")),
        updated=_s(props.get("updated")),
        source_org=_s(props.get("sourceOrg")),
        action=_s(props.get("action")),
        event=_s(cap.get("event")),
        text=_s(props.get("text")),
        size=_s(props.get("sizeFmt")),
        resources=resources,
        url=_s(props.get("url")),
        points=points,
        polygons=polygons,
    )


@dataclass
class FireConditions:
    """Fire Danger Rating and Total Fire Ban status per district for one day."""

    date: str  # ISO date (YYYY-MM-DD) in Victorian local time, "" when unknown
    ratings: dict[str, str]
    total_fire_ban: dict[str, bool]

    def districts_matching(self, names: list[str]) -> list[str]:
        """Districts from the feed matching the configured names (case-insensitive)."""
        if not names:
            return list(self.ratings) or list(self.total_fire_ban)
        wanted = {n.strip().lower() for n in names if n.strip()}
        known = list(dict.fromkeys(list(self.ratings) + list(self.total_fire_ban)))
        return [d for d in known if d.lower() in wanted]


def _tfb_flag(value: Any) -> bool:
    v = _s(value).upper()
    return v.startswith("YES")


def _local_date_from_utc(iso: str) -> str:
    """The feed dates forecasts at local midnight expressed in UTC (13:00Z/14:00Z the day before)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    # Victoria is UTC+10 or UTC+11; adding 11h lands on the local calendar date either way.
    from datetime import timedelta

    return (dt + timedelta(hours=11)).date().isoformat()


def parse_fire_conditions(data: dict) -> list[FireConditions]:
    """Fire Danger Ratings / Total Fire Bans for today and the forecast days.

    The first entry is the current day (``conditions.fdr``/``conditions.tfb``);
    following entries come from ``conditions.forecasts`` with dates after today.
    """
    props = data.get("properties") or {}
    cond = props.get("conditions") or {}
    out: list[FireConditions] = []
    forecasts = [f for f in cond.get("forecasts") or [] if isinstance(f, dict)]
    today_date = _local_date_from_utc(_s(forecasts[0].get("date"))) if forecasts else ""
    fdr = cond.get("fdr") or {}
    tfb = cond.get("tfb") or {}
    if fdr or tfb:
        out.append(FireConditions(
            date=today_date,
            ratings={k: _s(v).upper() for k, v in fdr.items()},
            total_fire_ban={k: _tfb_flag(v) for k, v in tfb.items()},
        ))
    for f in forecasts[1:]:
        out.append(FireConditions(
            date=_local_date_from_utc(_s(f.get("date"))),
            ratings={k: _s(v).upper() for k, v in (f.get("fdr") or {}).items()},
            total_fire_ban={k: _tfb_flag(v) for k, v in (f.get("tfb") or {}).items()},
        ))
    return out


@dataclass
class VicFeed:
    events: list[VicEvent]
    fire: list[FireConditions]
    last_updated: str = ""

    @property
    def warnings(self) -> list[VicEvent]:
        return [e for e in self.events if e.is_warning]

    @property
    def incidents(self) -> list[VicEvent]:
        return [e for e in self.events if not e.is_warning]


def parse_feed(data: dict) -> VicFeed:
    events = []
    for feature in data.get("features") or []:
        if isinstance(feature, dict):
            ev = parse_feature(feature)
            if ev is not None:
                events.append(ev)
    props = data.get("properties") or {}
    return VicFeed(events=events, fire=parse_fire_conditions(data), last_updated=_s(props.get("lastUpdated")))


def fetch_feed(session: Optional[requests.Session] = None, timeout: float = 15.0,
               url: str = VIC_EVENTS_URL) -> VicFeed:
    """Download and parse the feed (blocking; run it in an executor)."""
    getter = session.get if session is not None else requests.get
    resp = getter(url, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    resp.raise_for_status()
    return parse_feed(resp.json())


# --------------------------------------------------------------------------
# Filtering and formatting helpers
# --------------------------------------------------------------------------

def filter_events(
    events: list[VicEvent],
    *,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: float = 0.0,
    min_level: int = LEVEL_COMMUNITY,
    include_warnings: bool = True,
    include_incidents: bool = True,
    incident_categories: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
) -> list[tuple[VicEvent, Optional[float]]]:
    """Events passing the filters, with their distance (km) when a centre is given.

    Sorted by warning level (highest first), then distance, then most recently updated.
    """
    cats = [c.strip().lower() for c in (incident_categories or []) if c.strip()]
    kws = [k.strip().lower() for k in (keywords or []) if k.strip()]
    out: list[tuple[VicEvent, Optional[float]]] = []
    for ev in events:
        if ev.is_warning:
            if not include_warnings or ev.level < min_level:
                continue
        else:
            if not include_incidents:
                continue
            if cats and ev.category1.lower() not in cats and ev.category2.lower() not in cats:
                continue
        dist: Optional[float] = None
        if lat is not None and lon is not None:
            dist = ev.distance_km(lat, lon)
        if kws:
            hay = f"{ev.location} {ev.text} {ev.name}".lower()
            kw_hit = any(k in hay for k in kws)
        else:
            kw_hit = False
        if radius_km and radius_km > 0 and lat is not None and lon is not None:
            in_radius = dist is not None and dist <= radius_km
            if not in_radius and not kw_hit:
                continue
        elif kws and not kw_hit:
            continue
        out.append((ev, dist))
    out.sort(key=lambda t: (-t[0].level if t[0].is_warning else 0, not t[0].is_warning,
                            t[1] if t[1] is not None else 1e9))
    return out


def shorten_places(location: str, max_names: int = 3) -> str:
    names = [n.strip() for n in location.split(",") if n.strip()]
    if len(names) <= max_names:
        return ", ".join(names)
    return ", ".join(names[:max_names]) + f" +{len(names) - max_names}"


def incident_icon(ev: VicEvent) -> str:
    for key in (ev.category1.lower(), ev.category2.lower()):
        if key in INCIDENT_ICONS:
            return INCIDENT_ICONS[key]
    for key, icon in INCIDENT_ICONS.items():
        if key in ev.category1.lower() or key in ev.category2.lower():
            return icon
    return "🚨"


def _fmt_dist(dist: Optional[float]) -> str:
    if dist is None:
        return ""
    if dist <= 0.05:
        return "in area"
    return f"{dist:.0f}km" if dist >= 10 else f"{dist:.1f}km"


def format_warning_short(ev: VicEvent, dist: Optional[float] = None) -> str:
    """One line for listings, e.g. '🟠 Watch and Act: Bushfire - Kaniva +2 (12km)'."""
    title = ev.category1 or ev.name or "Warning"
    what = ev.event or ev.category2
    where = shorten_places(ev.location, 2) or ev.incident_location
    line = f"{LEVEL_ICONS.get(ev.level, '')} {title}"
    if what:
        line += f": {what}"
    if where:
        line += f" - {where}"
    d = _fmt_dist(dist)
    if d:
        line += f" ({d})"
    return line.strip()


def format_incident_short(ev: VicEvent, dist: Optional[float] = None) -> str:
    """One line for listings, e.g. '🔥 Bushfire - 3.9Km Ne Of Nandaly | Under Control 954 Ha.'."""
    what = ev.category2 if ev.category2 and ev.category2 != ev.category1 else ev.category1
    line = f"{incident_icon(ev)} {what or 'Incident'}"
    if ev.location:
        line += f" - {ev.location}"
    extra = " ".join(p for p in (ev.status, ev.size if ev.size and ev.size != "Small" else "") if p)
    if extra:
        line += f" | {extra}"
    d = _fmt_dist(dist)
    if d:
        line += f" ({d})"
    return line


def format_event_short(ev: VicEvent, dist: Optional[float] = None) -> str:
    return format_warning_short(ev, dist) if ev.is_warning else format_incident_short(ev, dist)


def format_warning_alert(ev: VicEvent, dist: Optional[float] = None, max_chars: int = 280) -> str:
    """Full alert text for a channel post (level, headline, areas, first bullet)."""
    icon = LEVEL_ICONS.get(ev.level, "")
    lines = [f"{icon} {ev.headline}".strip()]
    where = ev.incident_location
    areas = shorten_places(ev.location, 4)
    loc_line = where or areas
    if where and areas and where.lower() not in areas.lower():
        loc_line = f"{where} | For: {areas}"
    d = _fmt_dist(dist)
    if d:
        loc_line = f"{loc_line} ({d})" if loc_line else d
    if loc_line:
        lines.append(loc_line)
    if ev.summary:
        lines.append(ev.summary)
    return truncate("\n".join(lines), max_chars)


def format_incident_alert(ev: VicEvent, dist: Optional[float] = None, max_chars: int = 200) -> str:
    text = format_incident_short(ev, dist)
    if ev.source_org:
        org = ev.source_org.split("/")[-1]
        text += f" [{org}]"
    return truncate(text, max_chars)


def format_fire_line(district: str, rating: str, tfb: bool) -> str:
    r = (rating or "").title() or "Unknown"
    if r.upper() == "NO FORECAST":
        r = "No forecast"
    return f"{district}: {r}{' TFB' if tfb else ''}"


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"
