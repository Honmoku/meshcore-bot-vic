#!/usr/bin/env python3
"""
VicEmergency command - current warnings, incidents and fire danger for Victoria, Australia.

Data: the public VicEmergency feed behind https://emergency.vic.gov.au (no API key).

Usage:
    vic                 warnings and incidents near you (companion, config, bot location, else Melbourne GPO)
    vic <town>          ... near a Victorian town or suburb (e.g. "vic ballarat")
    vic <postcode>      ... near a Victorian postcode (e.g. "vic 3350")
    vic <lat,lon>       ... near coordinates
    vic all             statewide warning summary
    vic fire [district] today's Fire Danger Ratings and Total Fire Bans
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from datetime import date
from typing import Optional

from ..clients.vic_emergency_client import (
    FIRE_DISTRICTS,
    LEVEL_ADVICE,
    LEVEL_COMMUNITY,
    LEVEL_EMERGENCY,
    LEVEL_WATCH_AND_ACT,
    MELBOURNE_GPO,
    MELBOURNE_GPO_LABEL,
    VicFeed,
    fetch_feed,
    filter_events,
    format_event_short,
    level_from_config,
    postcode_lat_lon,
)
from ..location import get_bot_lat_lon, get_companion_lat_lon, get_config_default_lat_lon
from ..models import MeshMessage
from ..utils import geocode_city_sync
from .base_command import BaseCommand

SECTION = "VicEmergency_Command"
FIRE_WORDS = {"fire", "fires", "fdr", "tfb", "ban", "bans", "firedanger"}
ALL_WORDS = {"all", "state", "statewide", "vic", "victoria"}
DISTRICT_SHORT = {
    "Northern Country": "N Country",
    "North Central": "N Central",
    "North East": "NE",
    "West and South Gippsland": "W&S Gipps",
    "East Gippsland": "E Gipps",
    "South West": "SW",
}


class VicEmergencyCommand(BaseCommand):
    """Look up VicEmergency warnings, incidents and fire danger ratings."""

    render_safe = True
    name = "vicemergency"
    honors_skip_channel_check = False
    enabled_attr = "vicemergency_enabled"
    keywords = ["vic", "vicemergency", "emv"]
    description = "VicEmergency warnings, incidents and fire danger (Victoria, Australia)"
    category = "weather"
    requires_internet = True
    cooldown_seconds = 5

    short_description = "VicEmergency warnings, incidents and fire danger near you"
    usage = "vic [town|postcode|lat,lon|all|fire [district]]"
    examples = ["vic", "vic ballarat", "vic 3350", "vic -37.81,144.96", "vic all", "vic fire", "vic fire central"]
    parameters = [
        {"name": "location", "description": "Optional: Victorian town, lat,lon, 'all' for statewide, "
                                            "or 'fire' for fire danger ratings and Total Fire Bans."},
    ]

    settings_schema = [
        {"key": "radius_km", "label": "Search radius", "type": "float", "min": 1, "default": 50, "unit": "km",
         "help": "How far from the location to list warnings and incidents."},
        {"key": "max_results", "label": "Max results", "type": "int", "min": 1, "max": 10, "default": 4,
         "help": "Most warnings/incidents listed in one reply."},
        {"key": "min_warning_level", "label": "Minimum warning level", "type": "str", "default": "community",
         "help": "community, advice, watch_and_act or emergency."},
        {"key": "include_incidents", "label": "Include incidents", "type": "bool", "default": True,
         "help": "List incidents (fires, floods, rescues...) as well as warnings."},
        {"key": "default_lat", "label": "Default latitude", "type": "float", "min": -90, "max": 90, "default": "",
         "help": "Used when the sender has no position (then bot location, then Melbourne GPO)."},
        {"key": "default_lon", "label": "Default longitude", "type": "float", "min": -180, "max": 180,
         "default": "", "help": "Used when the sender has no position (then bot location, then Melbourne GPO)."},
    ]

    # Feed responses are shared between users for a short time to spare the server.
    CACHE_SECONDS = 60

    def __init__(self, bot):
        super().__init__(bot)
        self.vicemergency_enabled = self.get_config_value(SECTION, "enabled", fallback=True, value_type="bool")
        self.radius_km = self.get_config_value(SECTION, "radius_km", fallback=50.0, value_type="float")
        self.max_results = self.get_config_value(SECTION, "max_results", fallback=4, value_type="int")
        self.min_level = level_from_config(
            self.get_config_value(SECTION, "min_warning_level", fallback="community"), LEVEL_COMMUNITY
        )
        self.include_incidents = self.get_config_value(SECTION, "include_incidents", fallback=True,
                                                       value_type="bool")
        self._cache: Optional[tuple[float, VicFeed]] = None

    # ------------------------------------------------------------------ data

    async def _get_feed(self) -> VicFeed:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < self.CACHE_SECONDS:
            return self._cache[1]
        loop = asyncio.get_running_loop()
        feed = await loop.run_in_executor(None, lambda: fetch_feed(timeout=15))
        self._cache = (now, feed)
        return feed

    def _resolve_location(self, message: MeshMessage, text: str
                          ) -> tuple[Optional[float], Optional[float], str, Optional[str]]:
        """(lat, lon, label, error). Blocking (geocoding); run in a thread."""
        text = text.strip()
        if not text:
            for loc in (get_companion_lat_lon(self.bot, message, self.logger),
                        get_config_default_lat_lon(self.bot, SECTION),
                        get_bot_lat_lon(self.bot, self.logger)):
                if loc:
                    return loc[0], loc[1], "you", None
            return MELBOURNE_GPO[0], MELBOURNE_GPO[1], MELBOURNE_GPO_LABEL, None
        if re.fullmatch(r"\d{4}", text):
            pc = postcode_lat_lon(text)
            if pc is None:
                return None, None, "", f"Unknown VIC postcode {text}"
            return pc[0], pc[1], text, None
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", text)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon, f"{lat:.2f},{lon:.2f}", None
            return None, None, "", "Invalid coordinates"
        glat, glon, _ = geocode_city_sync(self.bot, text, default_state="VIC", default_country="AU",
                                          include_address_info=False, timeout=10)
        if glat is None or glon is None:
            return None, None, "", f"Couldn't find '{text}' in VIC"
        return glat, glon, text.title(), None

    # ------------------------------------------------------------------ replies

    def _pack(self, lines: list[str], max_bytes: int, max_messages: int = 3) -> list[str]:
        """Join lines into as few messages as fit the byte budget."""
        out: list[str] = []
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate.encode("utf-8")) <= max_bytes:
                current = candidate
                continue
            if current:
                out.append(current)
            current = line
            while len(current.encode("utf-8")) > max_bytes:
                cut = current.encode("utf-8")[: max_bytes - 3].decode("utf-8", "ignore")
                out.append(cut + "...")
                current = ""
        if current:
            out.append(current)
        return out[:max_messages]

    def nearby_lines(self, feed: VicFeed, lat: float, lon: float, label: str) -> list[str]:
        matches = filter_events(
            feed.events, lat=lat, lon=lon, radius_km=self.radius_km, min_level=self.min_level,
            include_incidents=self.include_incidents,
        )
        n_warn = sum(1 for ev, _ in matches if ev.is_warning)
        n_inc = len(matches) - n_warn
        where = "you" if label == "you" else label
        if not matches:
            return [f"VicEmergency: nothing within {self.radius_km:g}km of {where}"]
        head = f"VicEmergency {self.radius_km:g}km of {where}: {n_warn} warning{'s' if n_warn != 1 else ''}"
        if self.include_incidents:
            head += f", {n_inc} incident{'s' if n_inc != 1 else ''}"
        lines = [head]
        lines += [format_event_short(ev, dist) for ev, dist in matches[: self.max_results]]
        if len(matches) > self.max_results:
            lines.append(f"+{len(matches) - self.max_results} more: emergency.vic.gov.au")
        return lines

    def statewide_lines(self, feed: VicFeed) -> list[str]:
        warnings = feed.warnings
        counts = Counter(ev.level for ev in warnings)
        parts = []
        for level, label in ((LEVEL_EMERGENCY, "Emergency"), (LEVEL_WATCH_AND_ACT, "Watch&Act"),
                             (LEVEL_ADVICE, "Advice"), (LEVEL_COMMUNITY, "Info")):
            if counts.get(level):
                parts.append(f"{counts[level]} {label}")
        head = f"VIC: {len(warnings)} warnings"
        if parts:
            head += f" ({', '.join(parts)})"
        head += f", {len(feed.incidents)} incidents"
        lines = [head]
        top = filter_events(warnings, min_level=max(self.min_level, LEVEL_ADVICE), include_incidents=False)
        lines += [format_event_short(ev) for ev, _ in top[: self.max_results]]
        return lines

    def fire_lines(self, feed: VicFeed, district_filter: str = "") -> list[str]:
        if not feed.fire:
            return ["No fire danger ratings in the VicEmergency feed right now"]
        day = feed.fire[0]
        try:
            day_label = date.fromisoformat(day.date).strftime("%a %d %b") if day.date else "Today"
        except ValueError:
            day_label = day.date
        districts = list(dict.fromkeys(list(day.ratings) + list(day.total_fire_ban))) or FIRE_DISTRICTS
        if district_filter:
            f = district_filter.lower()
            districts = [d for d in districts if f in d.lower() or f in DISTRICT_SHORT.get(d, "").lower()]
            if not districts:
                return [f"Unknown fire district '{district_filter}'. Try: " + ", ".join(FIRE_DISTRICTS)]
        by_rating: dict[str, list[str]] = {}
        for d in districts:
            rating = (day.ratings.get(d) or "UNKNOWN").title()
            if rating.upper() == "NO FORECAST":
                rating = "No forecast"
            by_rating.setdefault(rating, []).append(DISTRICT_SHORT.get(d, d) if len(districts) > 2 else d)
        bans = [DISTRICT_SHORT.get(d, d) for d in districts if day.total_fire_ban.get(d)]
        lines = [f"VIC fire danger {day_label}"]
        for rating, ds in by_rating.items():
            lines.append(f"{rating}: {', '.join(ds)}")
        lines.append(f"Total Fire Ban: {', '.join(bans) if bans else 'none'}")
        return lines

    async def execute(self, message: MeshMessage) -> bool:
        _, args = self.split_trigger_and_args(message.content.strip())
        args = (args or "").strip()
        words = args.split()
        first = words[0].lower() if words else ""

        try:
            self.record_execution(message.sender_id)
            if first in FIRE_WORDS:
                feed = await self._get_feed()
                lines = self.fire_lines(feed, " ".join(words[1:]))
            elif first in ALL_WORDS and len(words) == 1:
                feed = await self._get_feed()
                lines = self.statewide_lines(feed)
            else:
                lat, lon, label, err = await asyncio.to_thread(self._resolve_location, message, args)
                if err or lat is None or lon is None:
                    await self.send_response(message, err or "Location not found")
                    return True
                feed = await self._get_feed()
                lines = self.nearby_lines(feed, lat, lon, label)
        except Exception as e:
            self.logger.error(f"VicEmergency lookup failed: {e}")
            await self.send_response(message, "VicEmergency is unavailable right now, try again soon")
            return True

        chunks = self._pack(lines, self.get_max_message_length(message))
        if len(chunks) == 1:
            await self.send_response(message, chunks[0])
        else:
            await self.send_response_chunked(message, chunks)
        return True
