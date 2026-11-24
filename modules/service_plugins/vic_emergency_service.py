#!/usr/bin/env python3
"""
VicEmergency Service for MeshCore Bot.

Polls the public VicEmergency feed (https://emergency.vic.gov.au, Victoria,
Australia) and posts to a mesh channel when:

* a warning (Emergency Warning, Watch and Act, Advice, Community Update, BoM
  flood/weather warning) is issued near the configured location, or its level
  changes;
* optionally, a new incident (fire, flood, tree down, rescue, ...) appears nearby;
* optionally, a watched fire district gets a Total Fire Ban or a Fire Danger
  Rating at or above a threshold.

No API key required. What has been posted is kept in the ``bot_metadata``
table so a restart does not repost the same warnings.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any, Optional

import requests

from ..clients.vic_emergency_client import (
    FIRE_DANGER_LEVELS,
    LEVEL_ADVICE,
    LEVEL_ICONS,
    LEVEL_NAMES,
    MELBOURNE_GPO,
    FireConditions,
    VicEvent,
    VicFeed,
    fetch_feed,
    filter_events,
    fire_danger_index,
    format_incident_alert,
    format_warning_alert,
    level_from_config,
)
from .base_service import BaseServicePlugin

METADATA_KEY_SEEN = "vic_emergency_seen"
METADATA_KEY_FIRE = "vic_emergency_fire_posted"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class VicEmergencyService(BaseServicePlugin):
    """Posts VicEmergency warnings, incidents and fire bans for Victoria, Australia."""

    config_section = "VicEmergency_Service"
    description = "VicEmergency warnings, incidents and fire bans (Victoria, Australia)"

    # Web-viewer settings schema (see modules/settings_schema.py)
    settings_schema = [
        {"key": "channel", "label": "Channel", "type": "str", "default": "emergency", "required": True,
         "help": "Channel to post alerts to (e.g. #emergency)."},
        {"key": "poll_interval", "label": "Poll interval", "type": "int", "min": 30000, "default": 120000,
         "unit": "ms", "help": "How often to check VicEmergency, in milliseconds."},
        {"key": "min_warning_level", "label": "Minimum warning level", "type": "str", "default": "advice",
         "help": "community, advice, watch_and_act or emergency."},
        {"key": "latitude", "label": "Centre latitude", "type": "float", "min": -90, "max": 90, "default": "",
         "help": "Centre of the watched area. Empty uses [Bot] bot_latitude, else Melbourne GPO."},
        {"key": "longitude", "label": "Centre longitude", "type": "float", "min": -180, "max": 180,
         "default": "", "help": "Centre of the watched area. Empty uses [Bot] bot_longitude, else Melbourne GPO."},
        {"key": "radius_km", "label": "Radius", "type": "float", "min": 0, "default": 50, "unit": "km",
         "help": "Only post events within this distance. 0 = all of Victoria."},
        {"key": "keywords", "label": "Extra places", "type": "str", "default": "",
         "help": "Comma-separated town names that always match, wherever they are."},
        {"key": "include_incidents", "label": "Post incidents", "type": "bool", "default": False,
         "help": "Also post new incidents (fires, floods, rescues...), not just warnings."},
        {"key": "incident_categories", "label": "Incident categories", "type": "str", "default": "",
         "help": "Comma-separated, e.g. Fire, Flooding. Empty = all."},
        {"key": "post_level_changes", "label": "Post level changes", "type": "bool", "default": True,
         "help": "Repost a warning when its level goes up or down."},
        {"key": "send_link", "label": "Include link", "type": "bool", "default": True,
         "help": "Follow a warning with its emergency.vic.gov.au link."},
        {"key": "max_length", "label": "Max alert length", "type": "int", "min": 60, "default": 280,
         "unit": "chars", "help": "Longer alerts are cut; over ~140 chars sends as several messages."},
        {"key": "fire_districts", "label": "Fire districts", "type": "str", "default": "",
         "help": "Comma-separated fire districts to watch for bans/ratings (e.g. Central). Empty = off."},
        {"key": "min_fire_danger", "label": "Minimum fire danger", "type": "str", "default": "extreme",
         "help": "moderate, high, extreme or catastrophic."},
        {"key": "announce_total_fire_ban", "label": "Announce Total Fire Bans", "type": "bool",
         "default": True, "help": "Post when a watched district has a Total Fire Ban."},
        {"key": "flood_scope", "label": "Flood scope", "type": "str", "default": "",
         "help": "Optional regional TC_FLOOD scope for mesh posts (e.g. #vic)."},
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        section = self.config_section
        cfg = self.bot.config
        self.channel = cfg.get(section, "channel", fallback="emergency")
        self.poll_interval_seconds = max(30.0, cfg.getint(section, "poll_interval", fallback=120000) / 1000.0)
        self.min_level = level_from_config(cfg.get(section, "min_warning_level", fallback="advice"), LEVEL_ADVICE)
        self.radius_km = cfg.getfloat(section, "radius_km", fallback=50.0)
        self.keywords = _csv(cfg.get(section, "keywords", fallback=""))
        self.include_incidents = cfg.getboolean(section, "include_incidents", fallback=False)
        self.incident_categories = _csv(cfg.get(section, "incident_categories", fallback=""))
        self.post_level_changes = cfg.getboolean(section, "post_level_changes", fallback=True)
        self.send_link = cfg.getboolean(section, "send_link", fallback=True)
        self.max_length = cfg.getint(section, "max_length", fallback=280)
        self.fire_districts = _csv(cfg.get(section, "fire_districts", fallback=""))
        self.min_fire_danger = fire_danger_index(cfg.get(section, "min_fire_danger", fallback="extreme")) or \
            FIRE_DANGER_LEVELS.index("EXTREME")
        self.announce_tfb = cfg.getboolean(section, "announce_total_fire_ban", fallback=True)

        self.lat, self.lon = self._resolve_centre()

        self._poll_task: Optional[asyncio.Task] = None
        self._session = requests.Session()
        # id -> last posted level ("i" for incidents)
        self._seen: dict[str, int] = self._load_json(METADATA_KEY_SEEN)
        # date -> {district: "RATING|TFB"} already announced
        self._fire_posted: dict[str, dict[str, str]] = self._load_json(METADATA_KEY_FIRE)

        area = (f"{self.radius_km:g} km of {self.lat:.3f},{self.lon:.3f}"
                if self.lat is not None and self.radius_km > 0 else "all of Victoria")
        self.logger.info(
            "VicEmergency service initialized: channel=%s, area=%s, min level=%s, incidents=%s",
            self.channel, area, LEVEL_NAMES.get(self.min_level), self.include_incidents,
        )

    # ------------------------------------------------------------------ setup

    def _resolve_centre(self) -> tuple[Optional[float], Optional[float]]:
        cfg = self.bot.config
        section = self.config_section
        lat = cfg.getfloat(section, "latitude", fallback=None) if cfg.has_option(section, "latitude") and \
            cfg.get(section, "latitude").strip() else None
        lon = cfg.getfloat(section, "longitude", fallback=None) if cfg.has_option(section, "longitude") and \
            cfg.get(section, "longitude").strip() else None
        if lat is None or lon is None:
            try:
                from ..location import get_bot_lat_lon

                loc = get_bot_lat_lon(self.bot, self.logger)
            except Exception:
                loc = None
            if loc:
                lat, lon = loc
        if lat is None or lon is None:
            lat, lon = MELBOURNE_GPO
            if self.radius_km > 0:
                self.logger.info(
                    "VicEmergency: no latitude/longitude configured (service or [Bot]); "
                    "using Melbourne GPO (%.4f, %.4f)", lat, lon,
                )
        return lat, lon

    def _load_json(self, key: str) -> dict:
        db = getattr(self.bot, "db_manager", None)
        if not db:
            return {}
        try:
            raw = db.get_metadata(key)
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_json(self, key: str, value: dict) -> None:
        db = getattr(self.bot, "db_manager", None)
        if not db:
            return
        try:
            db.set_metadata(key, json.dumps(value, separators=(",", ":")))
        except Exception as e:
            self.logger.warning("VicEmergency: could not save state: %s", e)

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("VicEmergency service is disabled, not starting")
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info("VicEmergency service started")

    async def stop(self) -> None:
        self._running = False
        await self._cancel_tasks(self._poll_task)
        self._poll_task = None
        self._session.close()
        self.logger.info("VicEmergency service stopped")

    async def _poll_loop(self) -> None:
        await self.run_periodic(self._check, lambda: self.poll_interval_seconds, "Error in VicEmergency poll loop")

    # ------------------------------------------------------------------ polling

    async def _check(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            feed = await loop.run_in_executor(None, lambda: fetch_feed(self._session, timeout=20))
        except requests.exceptions.RequestException as e:
            self.logger.warning("VicEmergency request failed: %s", e)
            return
        except ValueError as e:
            self.logger.warning("VicEmergency response parse error: %s", e)
            return
        await self.process_feed(feed)

    async def process_feed(self, feed: VicFeed) -> int:
        """Post anything new in ``feed``. Returns the number of alerts posted."""
        posted = 0
        for message, link in self.pending_event_alerts(feed):
            if await self._post(message, link):
                posted += 1
        fire_msg = self.pending_fire_alert(feed.fire)
        if fire_msg and await self._post(fire_msg, None):
            posted += 1
        self._save_json(METADATA_KEY_SEEN, self._seen)
        self._save_json(METADATA_KEY_FIRE, self._fire_posted)
        return posted

    def pending_event_alerts(self, feed: VicFeed) -> list[tuple[str, Optional[str]]]:
        """Alerts for new/changed events, updating the seen state. Pure apart from state."""
        radius = self.radius_km if self.lat is not None and self.lon is not None else 0.0
        matches = filter_events(
            feed.events,
            lat=self.lat,
            lon=self.lon,
            radius_km=radius,
            min_level=self.min_level,
            include_incidents=self.include_incidents,
            incident_categories=self.incident_categories,
            keywords=self.keywords,
        )
        alerts: list[tuple[str, Optional[str]]] = []
        # Post lower levels first so the most severe warning is the newest message.
        for ev, dist in reversed(matches):
            alert = self._alert_for(ev, dist)
            if alert:
                alerts.append(alert)

        # Forget events no longer in the feed so the state stays small,
        # and so a re-issued warning with the same id is posted again.
        live_ids = {e.id for e in feed.events}
        self._seen = {k: v for k, v in self._seen.items() if k in live_ids}
        return alerts

    def _alert_for(self, ev: VicEvent, dist: Optional[float]) -> Optional[tuple[str, Optional[str]]]:
        key = ev.id
        if ev.is_warning:
            level = ev.level
            prev = self._seen.get(key)
            if prev is not None and (prev == level or not self.post_level_changes):
                return None
            self._seen[key] = level
            text = format_warning_alert(ev, dist, self.max_length)
            if prev is not None:
                change = "UPGRADED" if level > prev else "DOWNGRADED"
                text = f"{LEVEL_ICONS.get(level, '')} {change}: " + text.split(" ", 1)[-1]
            return text, (ev.link if self.send_link else None)
        if key in self._seen:
            return None
        self._seen[key] = 0
        return format_incident_alert(ev, dist, self.max_length), None

    def pending_fire_alert(self, days: list[FireConditions]) -> Optional[str]:
        """One message covering watched districts with a ban or high rating not yet announced."""
        if not self.fire_districts or not days:
            return None
        parts: list[str] = []
        live_dates = set()
        for day in days[:2]:  # today and tomorrow, when bans are usually declared
            if not day.date:
                continue
            live_dates.add(day.date)
            announced = self._fire_posted.setdefault(day.date, {})
            entries = []
            for district in day.districts_matching(self.fire_districts):
                rating = day.ratings.get(district, "")
                tfb = day.total_fire_ban.get(district, False)
                notable = (tfb and self.announce_tfb) or fire_danger_index(rating) >= self.min_fire_danger
                if not notable:
                    continue
                state = f"{rating}|{int(tfb)}"
                if announced.get(district) == state:
                    continue
                announced[district] = state
                label = rating.title() if rating else ""
                entries.append(f"{district} {label}{' TFB' if tfb else ''}".strip())
            if entries:
                parts.append(f"{self._day_label(day.date)}: " + ", ".join(entries))
        self._fire_posted = {d: v for d, v in self._fire_posted.items() if d in live_dates}
        if not parts:
            return None
        return "🔥 VIC fire danger | " + " | ".join(parts)

    @staticmethod
    def _day_label(iso: str) -> str:
        try:
            return date.fromisoformat(iso).strftime("%a %d %b")
        except ValueError:
            return iso

    async def _post(self, text: str, link: Optional[str]) -> bool:
        try:
            ok = await self.bot.command_manager.send_channel_message(
                self.channel, text, scope=self.get_mesh_flood_scope()
            )
            if ok and link:
                await self.bot.command_manager.send_channel_message(
                    self.channel, link, scope=self.get_mesh_flood_scope(), skip_user_rate_limit=True
                )
            try:
                await self.send_external_notifications(text + (f"\n{link}" if link else ""))
            except Exception as e:  # external notify is best-effort
                self.logger.debug("VicEmergency external notify failed: %s", e)
            return bool(ok)
        except Exception as e:
            self.logger.error("Error sending VicEmergency alert: %s", e)
            return False
