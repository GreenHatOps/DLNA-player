# Bulb Voice

A web app that streams YouTube, YouTube Music, internet radio, and audio files to any DLNA speaker on your network. Built for a Microchip CY920 audio module hiding inside a smart bulb, but works with any DLNA renderer (TVs, Xbox, receivers, etc).

## What it does

- **Search YouTube** right from the app — type a query, tap `+` to queue
- Paste any **YouTube/YouTube Music URL** (single video, playlist, or channel)
- Add **internet radio streams** or **direct audio file URLs**
- **Auto-discovers all DLNA speakers** on your network via SSDP — switch between them with one tap
- **Duplicate detection** — adding the same track or playlist twice is caught and skipped
- **Playlist download progress** — see each track's status (downloading/done/waiting) in a live card
- **Device keepalive** — prevents speakers from sleeping while the app is running
- **Log viewer** — collapsible panel showing server events in real time
- Mobile-friendly dark UI — designed for iPhone Safari, works as a home screen app
- Queue management with playback controls (play/pause/stop/skip/volume)
- Auto-advances to the next track when the current one finishes
- **Queue persists across restarts** — your playlist and device preference survive reboots

## How it works

```
iPhone/Browser  -->  FastAPI server  -->  Any DLNA speaker
                     (yt-dlp + ffmpeg)     (auto-discovered via SSDP)
```

1. You search or paste a URL in the web UI
2. The server downloads the audio and converts it to MP3 (YouTube uses DASH-fragmented containers that DLNA devices can't play directly)
3. The server tells the speaker to fetch the MP3 via DLNA `SetAVTransportURI`
4. The speaker streams the MP3 from the server and plays it

For playlists, all tracks are added to the queue instantly and downloaded one-by-one in the background.

## Setup

### Docker (recommended)

```bash
docker compose up -d
```

Or without compose:
```bash
docker build -t bulb-voice .
docker run -d --network host -v ./cache:/app/cache --restart unless-stopped bulb-voice
```

Host networking is required for SSDP multicast discovery and so DLNA devices can reach the audio stream.

### Manual

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Requirements

- Python 3.10+ (or Docker)
- DLNA device(s) on the same local network

The server starts on port 8000 and prints the LAN URL. Open it on your phone.
No system-level ffmpeg install needed — a bundled binary is included via `imageio-ffmpeg`.

## Usage

### Web UI

Open `http://<server-ip>:8000` on your phone or browser.

- **Search:** Type anything in the input and hit the search button — results appear instantly. Tap `+` to add to queue.
- **Paste URL:** Paste a YouTube/YouTube Music URL and hit search — it adds directly.
- **Playlists:** Paste a playlist or channel URL — all new tracks get queued with a live download progress card. Duplicates are automatically skipped.
- **Switch speakers:** Tap the speaker name in the header to see all discovered DLNA devices. Tap to switch. The app remembers your last device and reconnects automatically on restart.
- **Controls:** Play/pause, skip, stop, volume slider.
- **Seek:** Tap anywhere on the progress bar to jump to that position.
- **Play mode:** Tap the mode button (next to stop) to cycle: Sequential > Repeat All > Repeat One > Shuffle. Persisted across restarts.
- **Gapless playback:** The next track is pre-loaded so there's no gap between songs.
- **Logs:** Tap "Logs" at the bottom to see server events (downloads, errors, connections) in real time.

### Supported input types

| Input | What happens |
|-------|-------------|
| Text query (e.g. "lofi hip hop") | Searches YouTube, shows results |
| YouTube video URL | Downloads and queues immediately |
| YouTube Music URL | Downloads and queues immediately |
| YouTube playlist URL | Queues all tracks, downloads in background |
| YouTube channel URL | Queues all videos, downloads in background |
| Internet radio stream URL | Proxies stream to speaker |
| Direct audio file URL | Proxies to speaker |

### Compatible devices

Works with any DLNA/UPnP MediaRenderer on your local network. Tested with:

| Device | Type |
|--------|------|
| Sengled Pulse (CY920) | Smart bulb with speaker |
| LG webOS TV | Television |
| Xbox One | Game console |
| Any DLNA receiver | Speakers, soundbars, etc. |

On first run, the app discovers all DLNA speakers on your network. Tap the speaker button in the header to pick one. Your choice is remembered across restarts.

## API

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/api/search` | `{query, max_results?}` | Search YouTube |
| POST | `/api/queue/add` | `{url, type?}` | Add track or playlist |
| DELETE | `/api/queue/{id}` | — | Remove track from queue |
| GET | `/api/queue` | — | List all queued tracks |
| POST | `/api/play` | `{track_id?}` | Play specific track or resume |
| POST | `/api/pause` | — | Pause playback |
| POST | `/api/stop` | — | Stop playback |
| POST | `/api/next` | — | Skip to next track |
| POST | `/api/prev` | — | Go to previous track |
| POST | `/api/seek` | `{position}` | Seek to position (seconds) |
| POST | `/api/play-mode` | `{mode}` | Set play mode (NORMAL/REPEAT_ALL/REPEAT_ONE/SHUFFLE) |
| POST | `/api/volume` | `{level: 0-100}` | Set volume |
| GET | `/api/status` | — | Full player state |
| GET | `/api/devices` | — | List discovered DLNA speakers |
| POST | `/api/devices/select` | `{name}` | Switch to a speaker |
| POST | `/api/devices/refresh` | — | Re-scan network for speakers |
| GET | `/api/logs` | — | Last 100 server log entries |

## Persistence

The queue, current track position, volume, and last connected device are saved to `cache/state.json` automatically. On restart, the app restores your queue and reconnects to the same speaker. Only tracks whose downloaded MP3 files are still in `cache/` are loaded back. No database needed.

Removing a track from the queue deletes its cached MP3 file. Radio/URL streams have no local file.

## Testing

```bash
source venv/bin/activate
pytest test_app.py -v
```

52 unit tests covering parsers, URL detection, duplicate detection, state persistence, queue logic, and XML escaping. No DLNA device required to run tests.

## Design

The UI went through a design review focusing on mobile usability:

- All interactive elements meet the 44px minimum touch target (Apple HIG)
- Currently playing track is highlighted with accent color
- Volume slider has an enlarged thumb for easy grabbing
- Header layout won't break on narrow screens
- CSS/JS use `?v=N` query params for cache busting after updates

## Known quirks

**General (all DLNA devices):**
- Some devices reject `GetVolume` calls. The app handles this gracefully.
- CSS/JS changes require a browser refresh (not a server restart). Python code changes require a server restart.

**Sengled Pulse / CY920 specific:**
- Firmware can get stuck in `TRANSITIONING` state after rapid commands. Power cycle fixes it.
- Requires DIDL-Lite metadata with `protocolInfo` — empty metadata causes error 714.
- Goes into deep sleep when idle. The app sends a keepalive ping every 30s to prevent this.
- Friendly name may reset to "unknown" after power cycle. The app matches by URL as fallback.
- LED/dimming is NOT controllable locally — it's on a separate Zigbee chip (Sengled Home cloud app only).
