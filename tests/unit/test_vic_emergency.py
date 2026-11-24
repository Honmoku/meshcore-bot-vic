"""Tests for VicEmergency (Victoria, Australia) feed parsing, the vic command and the service."""

import configparser
import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.clients.vic_emergency_client import (
    LEVEL_ADVICE,
    LEVEL_COMMUNITY,
    LEVEL_EMERGENCY,
    LEVEL_WATCH_AND_ACT,
    MELBOURNE_GPO,
    filter_events,
    fire_danger_index,
    format_incident_short,
    format_warning_alert,
    format_warning_short,
    level_from_config,
    parse_feed,
    parse_level,
    shorten_places,
)
from modules.commands.vicemergency_command import VicEmergencyCommand
from modules.service_plugins.vic_emergency_service import VicEmergencyService

KANIVA = (-36.38, 141.24)
MELBOURNE = (-37.81, 144.96)


def _warning(id_, category1, lat, lon, location="Kaniva, Lillimur, Serviceton, Telopea Downs",
             event="Grass Fire", text=None, polygon=None):
    geoms = [{"type": "Point", "coordinates": [lon, lat]}]
    if polygon:
        geoms.append({"type": "Polygon", "coordinates": [polygon]})
    if text is None:
        text = (f"{category1.upper()} - {event.upper()} - Stay Informed\n"
                f"Incident Location: Murrawong Road, Kaniva\n"
                f"Issue Date: Tuesday 24 November 2026, 07:12 pm\n\n"
                f"This {category1} message is being issued for {location}.\n"
                f"- There is a grassfire along Murrawong Road, Kaniva that is not yet under control.\n"
                f"- Second bullet.\n")
    return {
        "type": "Feature",
        "geometry": {"type": "GeometryCollection", "geometries": geoms},
        "properties": {
            "feedType": "warning", "id": id_, "sourceId": id_, "sourceOrg": "EMV",
            "category1": category1, "category2": "Fire", "name": category1, "status": "Minor",
            "action": "Stay Informed", "location": location, "text": text,
            "created": "2026-11-24T19:12:39+11:00", "updated": "2026-11-24T19:12:41+11:00",
            "cap": {"event": event, "severity": "Minor"},
        },
    }


def _incident(id_, category1, category2, lat, lon, location, status="Responding", size="Small"):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "feedType": "incident", "id": id_, "sourceOrg": "VIC/CFA", "category1": category1,
            "category2": category2, "status": status, "location": location, "sizeFmt": size,
            "resources": 2, "created": "2026-11-24T07:25:00.000Z", "updated": "2026-11-24T07:25:00.000Z",
        },
    }


def _feed_dict(features, fdr=None, tfb=None):
    fdr = fdr or {"Central": "HIGH", "Mallee": "EXTREME", "Wimmera": "MODERATE"}
    tfb = tfb or {"Central": "NO - RESTRICTIONS MAY APPLY", "Mallee": "YES - TOTAL FIRE BAN IN FORCE",
                  "Wimmera": "NO - RESTRICTIONS MAY APPLY"}
    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "lastUpdated": "2026-11-24T08:23:08.019Z",
            "conditions": {
                "fdr": fdr,
                "tfb": tfb,
                "forecasts": [
                    {"date": "2026-11-23T13:00:00.000Z", "fdr": fdr, "tfb": tfb},
                    {"date": "2026-11-24T13:00:00.000Z", "fdr": {"Central": "CATASTROPHIC"},
                     "tfb": {"Central": "YES - TOTAL FIRE BAN IN FORCE"}},
                ],
            },
        },
    }


def _sample_feed():
    square = [[141.0, -36.5], [141.5, -36.5], [141.5, -36.2], [141.0, -36.2], [141.0, -36.5]]
    return parse_feed(_feed_dict([
        _warning("100", "Advice", *KANIVA, polygon=square),
        _warning("101", "Watch and Act", -37.56, 143.85, location="Ballarat, Buninyong", event="Bushfire"),
        _warning("102", "Community Update", -37.7, 145.0, location="Preston", event="Haystack Fire"),
        _incident("ESTA:1", "Tree Down", "Tree Down", -37.70, 145.00, "Preston", "Request For Assistance"),
        _incident("ESTA:2", "Fire", "Bushfire", -36.40, 141.30, "3.9Km Ne Of Kaniva", "Under Control", "954 Ha."),
        {"type": "Feature", "geometry": None,
         "properties": {"feedType": "burn-area", "id": "x", "category1": "Burn Area"}},
    ]))


class TestLevels:
    @pytest.mark.parametrize("title,level", [
        ("Emergency Warning", LEVEL_EMERGENCY),
        ("Evacuate Immediately", LEVEL_EMERGENCY),
        ("Watch and Act", LEVEL_WATCH_AND_ACT),
        ("Watch & Act", LEVEL_WATCH_AND_ACT),
        ("Advice", LEVEL_ADVICE),
        ("Minor Flood Warning", LEVEL_ADVICE),
        ("Community Update", LEVEL_COMMUNITY),
        ("", LEVEL_COMMUNITY),
    ])
    def test_parse_level(self, title, level):
        assert parse_level(title) == level

    @pytest.mark.parametrize("value,level", [
        ("advice", LEVEL_ADVICE), ("watch_and_act", LEVEL_WATCH_AND_ACT), ("Watch and Act", LEVEL_WATCH_AND_ACT),
        ("emergency", LEVEL_EMERGENCY), ("community", LEVEL_COMMUNITY), ("3", LEVEL_WATCH_AND_ACT),
        ("", LEVEL_ADVICE), (None, LEVEL_ADVICE),
    ])
    def test_level_from_config(self, value, level):
        assert level_from_config(value) == level

    def test_fire_danger_index(self):
        assert fire_danger_index("catastrophic") > fire_danger_index("EXTREME") > fire_danger_index("High")
        assert fire_danger_index("NO FORECAST") == 0


class TestParseFeed:
    def test_warnings_and_incidents_split_and_other_types_dropped(self):
        feed = _sample_feed()
        assert [w.id for w in feed.warnings] == ["100", "101", "102"]
        assert [i.id for i in feed.incidents] == ["ESTA:1", "ESTA:2"]

    def test_warning_fields(self):
        w = _sample_feed().warnings[0]
        assert w.level == LEVEL_ADVICE
        assert w.headline == "ADVICE - GRASS FIRE - Stay Informed"
        assert w.incident_location == "Murrawong Road, Kaniva"
        assert w.summary.startswith("There is a grassfire")
        assert w.link == "https://emergency.vic.gov.au/respond/#!/warning/100/moreinfo"
        assert w.points and w.polygons

    def test_inside_warning_polygon_is_distance_zero(self):
        w = _sample_feed().warnings[0]
        assert w.distance_km(-36.3, 141.2) == 0.0
        assert w.distance_km(*MELBOURNE) > 300

    def test_fire_conditions(self):
        fire = _sample_feed().fire
        assert fire[0].date == "2026-11-24"
        assert fire[0].ratings["Mallee"] == "EXTREME"
        assert fire[0].total_fire_ban["Mallee"] is True
        assert fire[0].total_fire_ban["Central"] is False
        assert fire[1].date == "2026-11-25"
        assert fire[1].ratings["Central"] == "CATASTROPHIC"

    def test_garbage_is_tolerated(self):
        feed = parse_feed({"features": [None, {}, {"properties": {"feedType": "warning"}}, "x"]})
        assert feed.events == [] and feed.fire == []


class TestFilterAndFormat:
    def test_radius_filter_and_order(self):
        feed = _sample_feed()
        near_melb = filter_events(feed.events, lat=MELBOURNE[0], lon=MELBOURNE[1], radius_km=50)
        assert [e.id for e, _ in near_melb] == ["102", "ESTA:1"]

    def test_statewide_sorted_by_level(self):
        feed = _sample_feed()
        ids = [e.id for e, _ in filter_events(feed.events, include_incidents=False)]
        assert ids == ["101", "100", "102"]

    def test_min_level_and_incident_categories(self):
        feed = _sample_feed()
        res = filter_events(feed.events, min_level=LEVEL_ADVICE, incident_categories=["Fire"])
        assert {e.id for e, _ in res} == {"100", "101", "ESTA:2"}

    def test_keywords_match_outside_radius(self):
        feed = _sample_feed()
        res = filter_events(feed.events, lat=MELBOURNE[0], lon=MELBOURNE[1], radius_km=20,
                            include_incidents=False, keywords=["ballarat"])
        assert {e.id for e, _ in res} == {"101", "102"}

    def test_format_short_lines(self):
        feed = _sample_feed()
        assert format_warning_short(feed.warnings[1], 12.3) == "🟠 Watch and Act: Bushfire - Ballarat, Buninyong (12km)"
        assert format_incident_short(feed.incidents[1], 0.0) == \
            "🔥 Bushfire - 3.9Km Ne Of Kaniva | Under Control 954 Ha. (in area)"

    def test_format_alert_respects_max_length(self):
        w = _sample_feed().warnings[0]
        text = format_warning_alert(w, None, 280)
        assert text.startswith("🟡 ADVICE - GRASS FIRE")
        assert "For: Kaniva, Lillimur, Serviceton, Telopea Downs" in text
        assert len(format_warning_alert(w, None, 60)) <= 60

    def test_shorten_places(self):
        assert shorten_places("a, b, c, d, e", 3) == "a, b, c +2"
        assert shorten_places("a, b", 3) == "a, b"


# ---------------------------------------------------------------- service

def _service_bot(extra=None, metadata=None):
    bot = MagicMock()
    bot.logger = Mock()
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    cfg.add_section("VicEmergency_Service")
    for k, v in {"enabled": "true", "channel": "#emergency", "latitude": str(KANIVA[0]),
                 "longitude": str(KANIVA[1]), "radius_km": "50", **(extra or {})}.items():
        cfg.set("VicEmergency_Service", k, v)
    bot.config = cfg
    store = dict(metadata or {})
    bot.db_manager.get_metadata = Mock(side_effect=lambda k: store.get(k))
    bot.db_manager.set_metadata = Mock(side_effect=lambda k, v: store.__setitem__(k, v))
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    bot._store = store
    return bot


class TestService:
    def test_posts_nearby_warning_once_then_on_level_change(self):
        svc = VicEmergencyService(_service_bot())
        alerts = svc.pending_event_alerts(_sample_feed())
        assert len(alerts) == 1
        text, link = alerts[0]
        assert text.startswith("🟡 ADVICE") and link.endswith("/warning/100/moreinfo")
        assert svc.pending_event_alerts(_sample_feed()) == []

        upgraded = parse_feed(_feed_dict([_warning("100", "Watch and Act", *KANIVA)]))
        alerts = svc.pending_event_alerts(upgraded)
        assert len(alerts) == 1 and alerts[0][0].startswith("🟠 UPGRADED: WATCH AND ACT")

    def test_statewide_when_radius_zero_and_incidents_optional(self):
        svc = VicEmergencyService(_service_bot({"radius_km": "0", "include_incidents": "true",
                                                "incident_categories": "Fire"}))
        texts = [t for t, _ in svc.pending_event_alerts(_sample_feed())]
        # Community Update is below the default 'advice' level; Tree Down is not a Fire.
        assert len(texts) == 3
        assert texts[-1].startswith("🟠")  # most severe posted last
        assert any(t.startswith("🔥 Bushfire") for t in texts)

    def test_state_survives_restart(self):
        bot = _service_bot()
        svc = VicEmergencyService(bot)
        svc.pending_event_alerts(_sample_feed())
        svc._save_json("vic_emergency_seen", svc._seen)
        again = VicEmergencyService(_service_bot(metadata=bot._store))
        assert again.pending_event_alerts(_sample_feed()) == []

    def test_fire_alert_for_watched_districts_once(self):
        svc = VicEmergencyService(_service_bot({"fire_districts": "Mallee, Central"}))
        msg = svc.pending_fire_alert(_sample_feed().fire)
        assert msg is not None
        assert "Mallee Extreme TFB" in msg
        assert "Central Catastrophic TFB" in msg  # tomorrow's forecast
        assert "Central High" not in msg  # below min_fire_danger=extreme, no ban
        assert svc.pending_fire_alert(_sample_feed().fire) is None

    def test_fire_alert_off_without_districts(self):
        svc = VicEmergencyService(_service_bot())
        assert svc.pending_fire_alert(_sample_feed().fire) is None

    async def test_process_feed_sends_alert_and_link(self):
        bot = _service_bot()
        svc = VicEmergencyService(bot)
        posted = await svc.process_feed(_sample_feed())
        assert posted == 1
        sent = [c.args[1] for c in bot.command_manager.send_channel_message.call_args_list]
        assert sent[0].startswith("🟡 ADVICE") and sent[1].startswith("https://emergency.vic.gov.au/")
        assert json.loads(bot._store["vic_emergency_seen"]) == {"100": LEVEL_ADVICE}


# ---------------------------------------------------------------- command

def _command_bot():
    bot = MagicMock()
    bot.logger = Mock()
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    cfg.set("Bot", "bot_name", "TestBot")
    cfg.add_section("Channels")
    cfg.set("Channels", "monitor_channels", "general")
    cfg.add_section("Keywords")
    cfg.add_section("VicEmergency_Command")
    cfg.set("VicEmergency_Command", "enabled", "true")
    bot.config = cfg
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    return bot


class TestCommand:
    def setup_method(self):
        self.cmd = VicEmergencyCommand(_command_bot())
        self.feed = _sample_feed()

    def test_keywords_and_section(self):
        assert "vic" in self.cmd.keywords
        assert self.cmd._derive_config_section_name() == "VicEmergency_Command"

    def test_nearby(self):
        lines = self.cmd.nearby_lines(self.feed, *MELBOURNE, "you")
        assert lines[0] == "VicEmergency 50km of you: 1 warning, 1 incident"
        assert lines[1].startswith("🔵 Community Update")
        assert lines[2].startswith("🌳 Tree Down")

    def test_nearby_nothing(self):
        lines = self.cmd.nearby_lines(self.feed, -38.4, 142.5, "Warrnambool")
        assert lines == ["VicEmergency: nothing within 50km of Warrnambool"]

    def test_statewide(self):
        lines = self.cmd.statewide_lines(self.feed)
        assert lines[0] == "VIC: 3 warnings (1 Watch&Act, 1 Advice, 1 Info), 2 incidents"
        assert lines[1].startswith("🟠")

    def test_fire(self):
        lines = self.cmd.fire_lines(self.feed)
        assert lines[0] == "VIC fire danger Tue 24 Nov"
        assert "Extreme: Mallee" in lines
        assert lines[-1] == "Total Fire Ban: Mallee"
        assert self.cmd.fire_lines(self.feed, "central")[1] == "High: Central"
        assert self.cmd.fire_lines(self.feed, "atlantis")[0].startswith("Unknown fire district")

    def test_pack_respects_budget(self):
        chunks = self.cmd._pack(["a" * 60, "b" * 60, "c" * 60], 130)
        assert chunks == ["a" * 60 + "\n" + "b" * 60, "c" * 60]

    def test_resolve_coordinates(self):
        lat, lon, label, err = self.cmd._resolve_location(MagicMock(), "-37.81,144.96")
        assert (lat, lon, err) == (-37.81, 144.96, None)


class TestMelbourneGpoDefault:
    def test_service_defaults_to_melbourne_gpo(self):
        bot = _service_bot()
        bot.config.remove_option("VicEmergency_Service", "latitude")
        bot.config.remove_option("VicEmergency_Service", "longitude")
        svc = VicEmergencyService(bot)
        assert (svc.lat, svc.lon) == MELBOURNE_GPO == (-37.8136, 144.9633)
        # Preston community update is within 50km of the GPO but below 'advice'.
        assert svc.pending_event_alerts(_sample_feed()) == []

    def test_service_prefers_bot_location(self):
        bot = _service_bot()
        bot.config.remove_option("VicEmergency_Service", "latitude")
        bot.config.remove_option("VicEmergency_Service", "longitude")
        bot.config.set("Bot", "bot_latitude", "-37.56")
        bot.config.set("Bot", "bot_longitude", "143.85")
        svc = VicEmergencyService(bot)
        assert (svc.lat, svc.lon) == (-37.56, 143.85)

    def test_command_defaults_to_melbourne_gpo(self, monkeypatch):
        import modules.commands.vicemergency_command as mod

        for name in ("get_companion_lat_lon", "get_config_default_lat_lon", "get_bot_lat_lon"):
            monkeypatch.setattr(mod, name, lambda *a, **k: None)
        cmd = VicEmergencyCommand(_command_bot())
        lat, lon, label, err = cmd._resolve_location(MagicMock(), "")
        assert (lat, lon, label, err) == (-37.8136, 144.9633, "Melbourne GPO", None)
        assert cmd.nearby_lines(_sample_feed(), lat, lon, label)[0] == \
            "VicEmergency 50km of Melbourne GPO: 1 warning, 1 incident"


class TestPostcodes:
    def test_known_postcodes(self):
        from modules.clients.vic_emergency_client import postcode_lat_lon

        assert postcode_lat_lon("3000") == (-37.81444, 144.98258)
        assert postcode_lat_lon(" 3350 ") == (-37.57194, 143.86451)
        assert postcode_lat_lon("2000") is None  # Sydney: not in the VIC table
        assert postcode_lat_lon("") is None

    def test_command_resolves_postcode(self):
        cmd = VicEmergencyCommand(_command_bot())
        assert cmd._resolve_location(MagicMock(), "3350") == (-37.57194, 143.86451, "3350", None)
        lat, lon, label, err = cmd._resolve_location(MagicMock(), "9999")
        assert lat is None and err == "Unknown VIC postcode 9999"

    def test_service_uses_postcode_when_no_coordinates(self):
        bot = _service_bot({"postcode": "3350"})
        bot.config.remove_option("VicEmergency_Service", "latitude")
        bot.config.remove_option("VicEmergency_Service", "longitude")
        svc = VicEmergencyService(bot)
        assert (svc.lat, svc.lon) == (-37.57194, 143.86451)
        # Ballarat Watch and Act warning is within 50km of 3350.
        assert [t for t, _ in svc.pending_event_alerts(_sample_feed())][0].startswith("🟠 WATCH AND ACT")
