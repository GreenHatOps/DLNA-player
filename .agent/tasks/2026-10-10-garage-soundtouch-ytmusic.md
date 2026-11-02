# Task: Garage SoundTouch controller + YouTube Music in bulb_voice

Owner request (2026-10-10): an iPhone "software SoundTouch controller" for the
garage Bose SA-4 (the official SoundTouch app is unusable since the Bose cloud
shutdown), with YouTube Music: list / search, and suggestions by genre.
Decisions: extend bulb_voice (not a new app); YouTube Music signed in with the
owner's account; all phases.

## Observed (repo / runtime)
- Single active DLNA device (globals `av_transport`, `rendering_control`,
  `device`); devices only from SSDP `MediaRenderer:1` search. OBSERVED.
- Live `/api/devices` lists Volumio + 2 LG TVs, **not Bose_out**, although HA's
  dlna_dmr found its renderer at `http://192.168.1.37:8091/XD/BO5EBO5E-F00D-F00D-FEED-000C8AAC574C.xml`. OBSERVED.
- `/stream/{id}` serves cached MP3s (yt-dlp → mp3) or proxies radio URLs over
  plain http from `BASE_URL` — the SoundTouch renderer accepts only http. OBSERVED.
- Volume logic is built for the CY920 (resets to 100 after reboot →
  re-push saved level; any other mismatch is adopted as an external change). OBSERVED.
- `auto_advance_loop` advances the queue on any PLAYING → STOPPED/NO_MEDIA
  transition, regardless of who was playing. OBSERVED.
- Cache dir has no size cap: 2.5 GB in the running container. OBSERVED.
- Baseline: `venv/bin/python -m pytest -q` → 109 passed. VERIFIED.

## Bose SA-4 facts (from bose_out project, VERIFIED there by ear/logs)
- Two volumes: SoundTouch (port 8090 `/volume`) and UPnP renderer. Renderer
  volume resets to **0** in standby; a stream started at renderer volume 0 is
  silent. While a stream plays, SoundTouch volume changes mirror to the renderer.
- Wake from standby: `/key POWER` (press+release) only when `/now_playing`
  source is STANDBY (toggle!). Ready to accept a URI ~3-4 s later.
- Mute is a toggle key; state in `/volume` `muteenabled`.
- Home Assistant also plays on this renderer (radio presets, TTS announcements).

## Changes
1. **Static devices** — env `BV_STATIC_DEVICES` (comma-separated description
   URLs) probed in `discover_and_connect` alongside SSDP results.
2. **SoundTouch helper** (`soundtouch.py`, httpx, no new dependency):
   detect (`GET :8090/info` has `deviceID`), `wake()`, `unmute()`,
   `get/set_volume()`, `standby()`, `source()`. Hooks when the connected device
   is a SoundTouch:
   - `_play_current`: wake + unmute + set SoundTouch volume before SetAVTransportURI;
     re-push renderer volume after Play and again ~3 s later.
   - `api_volume`: set SoundTouch volume too.
   - `_poll_device_volume`: read SoundTouch volume (renderer 0 in standby is
     not an external change); skip while in standby.
3. **Foreign-playback guard** (all devices): auto-advance only when the
   renderer's last PLAYING URI was one of ours (`BASE_URL/stream/`). Prevents
   bulb_voice starting its queue after an HA preset/announcement ends — same
   protection applies to anything else casting to the bulb/TVs.
4. **YouTube Music** (`ytmusic.py`, new dep `ytmusicapi`): signed in with a
   browser-headers file at `cache/ytmusic_browser.json` (owner exports once;
   never committed; cache/ is git-ignored). Endpoints under `/api/ytm/`:
   `status`, `search`, `moods`, `moods/{params}`, `playlist/{id}`, `home`,
   `library`, `radio/{videoId}` + `POST /api/ytm/enqueue` (videoIds → existing
   playlist batch download path). Anonymous fallback when no auth file.
5. **Presets** — `cache/garage_presets.json` (6 radio stations, http/https;
   proxied through `/stream`, so https works too). `POST /api/garage/preset/{n}`
   plays it now (replaces queue head? → inserts as radio track and plays).
   `GET /api/garage/state` (SoundTouch source/volume/standby, preset playing
   detected from the renderer URI incl. HA-started ones).
6. **iPhone app** — `static/garage.html|js|css`: round controller (rotary
   volume ring, 6 presets with hover preview, power) + tabs Search / Genres /
   For you / Library / Queue. Home-screen web app (apple-mobile-web-app meta).
7. **Cache cap** — env `BV_CACHE_MAX_MB` (default 3000): evict oldest MP3s not
   in the queue after each download.

## Risks / unknowns
- ytmusicapi + yt-dlp are unofficial; YouTube changes break them. UNKNOWN when.
- SetVolume on the SA-4 renderer before a URI is set: UNKNOWN (existing
  `_volume_push_pending` retry covers it).
- Concurrency with HA on the same renderer: mitigated by (3); HA scripts still
  win when they start a stream.
- iOS home-screen web app over http on LAN: no service worker / lock-screen
  controls. ACCEPTED.

## Tests
New pytest cases (mocked network): static device probing, foreign-URI guard,
SoundTouch volume path, cache eviction, ytm endpoints with a fake YTMusic,
preset play. Full suite must stay green.

## Outcome (2026-10-10) — branch `garage-soundtouch`, not committed
- Implemented items 1-7 plus review changes: presets play the RAW station URL
  (HA/Morpheus see the same URI); no auto-advance when the SoundTouch went to
  standby; static devices listed at startup; lenient UPnP parsing
  (`non_strict=True` — the SA-4's Tencent QPlay SCPD has a non-standard root,
  strict parsing dropped the whole device: OBSERVED "Invalid document root").
- ytmusicapi's typed `get_mood_playlists` crashed on the real Blues page
  (video among playlists) → pages parsed by walking raw responses (ytmusic.py).
- Tests: 146 passed (109 baseline + 37 new), `-W error::RuntimeWarning`. VERIFIED.
- Deployed. VERIFIED by owner use: a YouTube track from bulb_voice played on
  the SA-4 (Europe — The Final Countdown), auto-advance to the next queued track.
- /garage page VERIFIED headless at 390x844 against the live backend: state
  display, For you shelves, 36 genre/mood chips, Blues (162 items), search.
  No playback actions were triggered from the headless run.
- NOT VERIFIED: signed-in YouTube Music (needs the owner's headers file);
  presets/power/volume ring from the /garage page on a real iPhone.
- Incident: one container rebuild happened while the owner was playing music
  (pre-deploy check printed PLAYING but did not gate). Playback survived.
  Later deploys gate on transport != PLAYING.
- Follow-up fix: static registration no longer connects (raced connect_loop's
  fast reconnect → "Connected to" twice). Deploy gated on idle.
