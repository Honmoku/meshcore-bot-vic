# VicEmergency (Victoria, Australia)

Warnings, incidents, Fire Danger Ratings and Total Fire Bans for Victoria, Australia, from the public feed behind [emergency.vic.gov.au](https://emergency.vic.gov.au). No API key is needed.

There are two parts. You can use either one on its own:

- **`vic` command**: anyone on the mesh can ask what's happening near them.
- **VicEmergency Service**: posts new warnings near you to a channel by itself.

!!! warning "Not an official warning channel"
    Mesh delivery is best-effort. In an emergency, use official sources: **emergency.vic.gov.au**, the **VicEmergency app**, the **VicEmergency Hotline (1800 226 226)**, ABC local radio, and call **000** if life or property is threatened.

---

## Quick start

```ini
[Bot]
# Used as the centre for both the command and the service
# Melbourne GPO, 350 Bourke St (also the default when unset)
bot_latitude = -37.8136
bot_longitude = 144.9633

[VicEmergency_Command]
enabled = true

[VicEmergency_Service]
enabled = true
channel = #emergency
radius_km = 50
min_warning_level = advice
fire_districts = Central
```

Restart the bot. Within a couple of minutes the service posts every current warning within 50 km. After that it posts only new warnings and warnings whose level changes.

---

## `vic` command

| Usage | Reply |
|-------|-------|
| `vic` | Warnings and incidents within `radius_km` of you. Uses your advertised position, then `default_lat`/`default_lon`, then the bot's location, then Melbourne GPO |
| `vic <town>` | The same, around a Victorian town or suburb (e.g. `vic ballarat`) |
| `vic <lat>,<lon>` | The same, around coordinates |
| `vic all` | Statewide count of warnings by level, plus the most serious ones |
| `vic fire [district]` | Today's Fire Danger Rating per fire district and any Total Fire Bans |

Keywords: `vic`, `vicemergency`, `emv`.

**Examples**

```
> vic ballarat
VicEmergency 50km of Ballarat: 1 warning, 2 incidents
🟠 Watch and Act: Bushfire - Buninyong (8.2km)
🔥 Bushfire - Mt Buninyong Rd | Going 12 Ha. (9.0km)
🌳 Tree Down - Wendouree | Request For Assistance (3.1km)

> vic fire
VIC fire danger Tue 24 Nov
Extreme: Mallee, Wimmera
High: N Country, N Central, Central
Moderate: NE, W&S Gipps, E Gipps, SW
Total Fire Ban: Mallee, Wimmera
```

Warning levels are shown as 🔴 Emergency Warning, 🟠 Watch and Act, 🟡 Advice (including Bureau of Meteorology flood/weather warnings), and 🔵 Community Update/Information. Distances are measured to the nearest point of the warning area. "in area" means the location is inside it.

| `[VicEmergency_Command]` option | Description | Default |
|--------|-------------|---------|
| `enabled` | Turn the command on or off | `true` |
| `radius_km` | Search radius for `vic` and `vic <town>` | `50` |
| `max_results` | Most warnings/incidents listed in one reply | `4` |
| `min_warning_level` | `community`, `advice`, `watch_and_act` or `emergency` | `community` |
| `include_incidents` | List incidents as well as warnings | `true` |
| `default_lat`, `default_lon` | Location when the sender has no position | bot location, else Melbourne GPO |

The feed is cached for 60 seconds, so several people asking at once make one request.

---

## VicEmergency Service

Checks the feed every `poll_interval` and posts to `channel`:

1. **Warnings** at or above `min_warning_level` within `radius_km` of the centre, or whose warning area contains the centre. Each warning is followed by its emergency.vic.gov.au link when `send_link = true`.
2. **Level changes**: a warning that goes from Advice to Watch and Act (or back down) is posted again, marked `UPGRADED` or `DOWNGRADED`.
3. **Incidents** (optional, `include_incidents = true`), filtered by `incident_categories`.
4. **Fire danger** (optional, `fire_districts`): one message when a watched district has a Total Fire Ban or a rating at or above `min_fire_danger`, for today or tomorrow.

When several alerts arrive at once, the most severe is posted last, so it's the newest message on the channel.

**Example posts**

```
🟠 WATCH AND ACT - BUSHFIRE - Prepare to leave
Mt Buninyong Rd, Buninyong | For: Buninyong, Scotsburn, Durham Lead +2 (in area)
There is a bushfire at Mt Buninyong Rd that is not yet under control.
https://emergency.vic.gov.au/respond/#!/warning/43701/moreinfo

🔥 VIC fire danger | Wed 25 Nov: Central Extreme TFB
```

| `[VicEmergency_Service]` option | Description | Default |
|--------|-------------|---------|
| `enabled` | Turn the service on or off | `false` |
| `channel` | Channel to post to | `#emergency` |
| `poll_interval` | Milliseconds between checks (minimum 30000) | `120000` |
| `min_warning_level` | `community`, `advice`, `watch_and_act` or `emergency` | `advice` |
| `latitude`, `longitude` | Centre of the watched area | `[Bot]` location, else Melbourne GPO (-37.8136, 144.9633) |
| `radius_km` | Distance from the centre; `0` = all of Victoria | `50` |
| `keywords` | Town names that always match, wherever they are | empty |
| `include_incidents` | Also post new incidents | `false` |
| `incident_categories` | e.g. `Fire, Flooding`; empty = all | empty |
| `post_level_changes` | Repost when a warning's level changes | `true` |
| `send_link` | Follow warnings with their link | `true` |
| `max_length` | Cut alerts to this many characters | `280` |
| `fire_districts` | Districts to watch for bans/ratings; empty = off | empty |
| `min_fire_danger` | `moderate`, `high`, `extreme` or `catastrophic` | `extreme` |
| `announce_total_fire_ban` | Announce Total Fire Ban days | `true` |
| `flood_scope` | Optional regional flood scope for posts | — |
| `discord_webhook_urls`, `telegram_chat_ids` | Also send alerts off-mesh | — |

Fire districts: Mallee, Wimmera, South West, Northern Country, North Central, Central, North East, West and South Gippsland, East Gippsland.

**Deduplication.** Posted warnings, and the levels they were posted at, are stored in the `bot_metadata` table (`vic_emergency_seen`, `vic_emergency_fire_posted`). A restart does not repost them. When a warning leaves the feed it is forgotten, so if it is issued again it is posted again.

---

## Data source

`https://emergency.vic.gov.au/public/events-geojson.json`: the public GeoJSON feed used by the VicEmergency website and app. It is published by Emergency Management Victoria and combines warnings and incidents from CFA, FRV, VICSES, DEECA, the Bureau of Meteorology and other agencies. Check [emergency.vic.gov.au](https://emergency.vic.gov.au) for the terms that apply to this data.
