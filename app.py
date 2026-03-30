from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import imageio_ffmpeg
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from typing import TYPE_CHECKING

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpDevice, UpnpService

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SERVER_PORT = 8000
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
STATE_FILE = Path(__file__).parent / "cache" / "state.json"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


SERVER_IP = get_lan_ip()
BASE_URL = f"http://{SERVER_IP}:{SERVER_PORT}"

log = logging.getLogger("bulb_voice")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Ring buffer for UI log viewer
from collections import deque
_log_buffer: deque[dict] = deque(maxlen=100)


class _UILogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        _log_buffer.append({
            "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "msg": record.getMessage(),
        })


log.addHandler(_UILogHandler())

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10),
    follow_redirects=True,
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class Track:
    id: str
    title: str
    artist: str
    source_type: str  # "youtube", "radio", "url"
    source_url: str
    duration: int = 0
    local_path: str = ""  # path to downloaded file (youtube)
    direct_url: str = ""  # for radio/url streams
    content_type: str = "audio/mpeg"
    content_length: int = 0


@dataclass
class DownloadProgress:
    total: int = 0
    done: int = 0
    current_title: str = ""
    track_ids: list[str] = field(default_factory=list)  # IDs of tracks in this batch
    started_at: float = 0.0  # time.time() when download batch started


@dataclass
class PlayerState:
    queue: list[Track] = field(default_factory=list)
    current_index: int = -1
    volume: int = 30
    play_mode: str = "NORMAL"  # NORMAL, REPEAT_ALL, REPEAT_ONE, SHUFFLE
    download: DownloadProgress = field(default_factory=DownloadProgress)
    last_device: str = ""  # friendly name of last connected device
    last_device_url: str = ""  # description URL of last connected device


def _save_state():
    """Persist queue and playback position to disk."""
    data = {
        "current_index": state.current_index,
        "volume": state.volume,
        "play_mode": state.play_mode,
        "last_device": state.last_device,
        "last_device_url": state.last_device_url,
        "queue": [
            {
                "id": t.id, "title": t.title, "artist": t.artist,
                "source_type": t.source_type, "source_url": t.source_url,
                "duration": t.duration, "local_path": t.local_path,
                "direct_url": t.direct_url, "content_type": t.content_type,
                "content_length": t.content_length,
            }
            for t in state.queue
        ],
    }
    STATE_FILE.write_text(json.dumps(data))


def _load_state():
    """Restore queue from disk if available."""
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
        for t in data.get("queue", []):
            # Skip tracks whose cached file is missing
            if t.get("local_path") and not Path(t["local_path"]).exists():
                continue
            state.queue.append(Track(**t))
        state.current_index = data.get("current_index", -1)
        state.volume = data.get("volume", 30)
        state.play_mode = data.get("play_mode", "NORMAL")
        state.last_device = data.get("last_device", "")
        state.last_device_url = data.get("last_device_url", "")
        # Clamp index to valid range
        if state.current_index >= len(state.queue):
            state.current_index = len(state.queue) - 1 if state.queue else -1
        log.info("Restored %d tracks from saved state (last device: %s)",
                 len(state.queue), state.last_device or "none")
    except Exception as e:
        log.warning("Failed to load state: %s", e)


class AddRequest(BaseModel):
    url: str
    type: str = "auto"


class VolumeRequest(BaseModel):
    level: int


class PlayRequest(BaseModel):
    track_id: str | None = None


class SearchRequest(BaseModel):
    query: str
    max_results: int = 10


class DeviceSelectRequest(BaseModel):
    name: str


class SeekRequest(BaseModel):
    position: int  # seconds


class PlayModeRequest(BaseModel):
    mode: str  # NORMAL, REPEAT_ALL, REPEAT_ONE, SHUFFLE


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_state()
    # Immediately nudge last device to prevent deep sleep during restart gap
    if state.last_device_url:
        asyncio.create_task(_nudge_device(state.last_device_url))
    asyncio.create_task(connect_loop())
    asyncio.create_task(auto_advance_loop())
    asyncio.create_task(keepalive_loop())
    log.info("Server at %s", BASE_URL)
    log.info("Open on your phone: %s", BASE_URL)
    yield
    # Ping device repeatedly before shutdown to keep it awake for restart
    if state.last_device_url:
        for _ in range(3):
            await _nudge_device(state.last_device_url)
            await asyncio.sleep(1)
    _save_state()


async def _nudge_device(url: str):
    """Send a lightweight HTTP GET to keep the device's WiFi stack awake."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.get(url)
    except Exception:
        pass


async def connect_loop():
    """Discover DLNA renderers via SSDP and connect to the first one found."""
    # On first run, try direct connect to last device (faster than SSDP)
    if av_transport is None and state.last_device_url:
        try:
            log.info("Fast reconnect to %s", state.last_device_url)
            await connect_to_device(state.last_device_url)
        except Exception as e:
            log.debug("Fast reconnect failed, falling back to SSDP: %s", e)
    while True:
        try:
            if av_transport is None:
                await discover_and_connect()
        except Exception as e:
            log.warning("Device discovery failed, retrying in 10s: %s", e)
        await asyncio.sleep(10)

app = FastAPI(lifespan=lifespan)
state = PlayerState()


@app.middleware("http")
async def save_state_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.method in ("POST", "DELETE") and request.url.path.startswith("/api/"):
        _save_state()
    return response

av_transport: UpnpService | None = None
rendering_control: UpnpService | None = None
device: UpnpDevice | None = None
discovered_devices: dict[str, dict] = {}  # {friendly_name: {url, name, model}}

# ---------------------------------------------------------------------------
# DLNA Discovery & Control
# ---------------------------------------------------------------------------

async def discover_and_connect():
    """Use SSDP to find DLNA MediaRenderers on the network."""
    global av_transport, rendering_control, device

    from async_upnp_client.search import async_search

    found = {}
    async def on_response(headers: dict):
        location = headers.get("location", headers.get("LOCATION", ""))
        if location:
            found[location] = headers

    # Search for MediaRenderer devices
    await async_search(
        search_target="urn:schemas-upnp-org:device:MediaRenderer:1",
        timeout=5,
        async_callback=on_response,
    )

    if not found:
        log.debug("No DLNA renderers found on network")
        return

    requester = AiohttpRequester()
    factory = UpnpFactory(requester)

    last_known = None
    for location in found:
        try:
            dev = await factory.async_create_device(location)
            avt = dev.service("urn:schemas-upnp-org:service:AVTransport:1")
            rc = dev.service("urn:schemas-upnp-org:service:RenderingControl:1")
            if avt and rc:
                discovered_devices[dev.friendly_name] = {
                    "url": location,
                    "name": dev.friendly_name,
                    "model": dev.model_name or "",
                }
                # Auto-connect to last known device by name or URL
                if state.last_device and dev.friendly_name == state.last_device:
                    last_known = (dev, avt, rc, location)
                elif state.last_device_url and location == state.last_device_url:
                    last_known = (dev, avt, rc, location)
        except Exception as e:
            log.debug("Device probe failed for %s: %s", location, e)

    # If name/URL match didn't work (device half-awake), try connecting by URL directly
    if not last_known and av_transport is None and state.last_device_url:
        # Match by IP, not exact URL (SSDP may return different paths)
        last_ip = state.last_device_url.split("//")[1].split("/")[0].split(":")[0]
        matching_url = next((loc for loc in found if last_ip in loc), None)
        if matching_url:
            try:
                log.info("Retrying direct connect to %s (matched via IP)", matching_url)
                await connect_to_device(matching_url)
                return
            except Exception as e:
                log.debug("Direct reconnect failed: %s", e)

    if last_known and av_transport is None:
        dev, avt, rc, location = last_known
        device = dev
        av_transport = avt
        rendering_control = rc
        try:
            state.volume = await dlna_get_volume()
        except Exception as e:
            log.debug("GetVolume not supported on %s: %s", dev.friendly_name, e)
        log.info("Reconnected to last device: %s (%s)", dev.friendly_name, location)
    elif av_transport is None and discovered_devices:
        names = ", ".join(discovered_devices.keys())
        log.info("Found %d device(s): %s. Select one from the UI.", len(discovered_devices), names)


async def connect_to_device(description_url: str):
    """Connect to a specific device by its description URL."""
    global av_transport, rendering_control, device
    requester = AiohttpRequester()
    factory = UpnpFactory(requester)
    device = await factory.async_create_device(description_url)
    av_transport = device.service("urn:schemas-upnp-org:service:AVTransport:1")
    rendering_control = device.service("urn:schemas-upnp-org:service:RenderingControl:1")
    try:
        state.volume = await dlna_get_volume()
    except Exception as e:
        log.debug("GetVolume not supported on %s: %s", device.friendly_name, e)
    state.last_device = device.friendly_name
    state.last_device_url = description_url
    _save_state()
    log.info("Connected to: %s", device.friendly_name)


async def dlna_set_uri(track: Track):
    stream_url = f"{BASE_URL}/stream/{track.id}"
    profile = _dlna_profile(track.content_type)
    size_attr = f' size="{track.content_length}"' if track.content_length else ''
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f'<dc:title>{_xml_escape(track.title)}</dc:title>'
        f'<dc:creator>{_xml_escape(track.artist)}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{track.content_type}:{profile}"{size_attr}>'
        f'{stream_url}</res>'
        '</item></DIDL-Lite>'
    )
    action = av_transport.action("SetAVTransportURI")
    await action.async_call(InstanceID=0, CurrentURI=stream_url, CurrentURIMetaData=didl)


async def dlna_play():
    await av_transport.action("Play").async_call(InstanceID=0, Speed="1")


async def dlna_pause():
    await av_transport.action("Pause").async_call(InstanceID=0)


async def dlna_stop():
    await av_transport.action("Stop").async_call(InstanceID=0)


async def dlna_set_volume(level: int):
    await rendering_control.action("SetVolume").async_call(
        InstanceID=0, Channel="Master", DesiredVolume=level
    )


async def dlna_get_volume() -> int:
    r = await rendering_control.action("GetVolume").async_call(InstanceID=0, Channel="Master")
    return r.get("CurrentVolume", 0)


async def dlna_get_position() -> dict:
    r = await av_transport.action("GetPositionInfo").async_call(InstanceID=0)
    return {
        "position": _parse_duration(r.get("RelTime", "0:00:00")),
        "duration": _parse_duration(r.get("TrackDuration", "0:00:00")),
    }


async def dlna_get_transport_state() -> str:
    r = await av_transport.action("GetTransportInfo").async_call(InstanceID=0)
    return r.get("CurrentTransportState", "UNKNOWN")


async def dlna_seek(position_secs: int):
    h = position_secs // 3600
    m = (position_secs % 3600) // 60
    s = position_secs % 60
    target = f"{h}:{m:02d}:{s:02d}"
    await av_transport.action("Seek").async_call(
        InstanceID=0, Unit="REL_TIME", Target=target
    )


async def dlna_set_next_uri(track: Track):
    """Pre-load next track for gapless playback."""
    stream_url = f"{BASE_URL}/stream/{track.id}"
    profile = _dlna_profile(track.content_type)
    size_attr = f' size="{track.content_length}"' if track.content_length else ''
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f'<dc:title>{_xml_escape(track.title)}</dc:title>'
        f'<dc:creator>{_xml_escape(track.artist)}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{track.content_type}:{profile}"{size_attr}>'
        f'{stream_url}</res>'
        '</item></DIDL-Lite>'
    )
    await av_transport.action("SetNextAVTransportURI").async_call(
        InstanceID=0, NextURI=stream_url, NextURIMetaData=didl
    )


async def dlna_set_play_mode(mode: str):
    await av_transport.action("SetPlayMode").async_call(
        InstanceID=0, NewPlayMode=mode
    )


def _parse_duration(s: str) -> int:
    if not s or s == "NOT_IMPLEMENTED":
        return 0
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
    return 0


def _dlna_profile(content_type: str) -> str:
    return {
        "audio/mpeg": "DLNA.ORG_PN=MP3",
        "audio/mp4": "DLNA.ORG_PN=AAC_ISO",
        "audio/flac": "*",
        "audio/wav": "*",
        "audio/x-wav": "*",
    }.get(content_type, "*")


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# yt-dlp Integration — download to local file
# ---------------------------------------------------------------------------

async def extract_playlist(url: str) -> list[dict]:
    """Extract metadata for all entries in a playlist/channel/single video."""
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--flat-playlist", "--dump-json", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"yt-dlp error: {stderr.decode()[:300]}")

    entries = []
    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue
        info = json.loads(line)
        video_url = info.get("url") or info.get("webpage_url") or info.get("original_url", "")
        # flat-playlist gives minimal ids; build full URL if needed
        if video_url and not video_url.startswith("http"):
            video_url = f"https://www.youtube.com/watch?v={video_url}"
        entries.append({
            "title": info.get("title", "Unknown"),
            "artist": info.get("uploader", info.get("channel", "Unknown")),
            "duration": int(info.get("duration") or 0),
            "url": video_url,
        })
    return entries


async def download_youtube(url: str, track_id: str) -> dict:
    """Download a single YouTube video's audio and convert to MP3."""
    output_template = str(CACHE_DIR / f"{track_id}.%(ext)s")
    mp3_path = CACHE_DIR / f"{track_id}.mp3"

    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "--ffmpeg-location", FFMPEG,
        "-f", "bestaudio",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "2",
        "--no-playlist",
        "--print-json",
        "-o", output_template,
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"yt-dlp error: {stderr.decode()[:300]}")

    info = json.loads(stdout)

    if not mp3_path.exists():
        matches = list(CACHE_DIR.glob(f"{track_id}*.mp3"))
        if matches:
            mp3_path = matches[0]
        else:
            raise HTTPException(status_code=500, detail="MP3 conversion failed")

    return {
        "title": info.get("title", "Unknown"),
        "artist": info.get("uploader", info.get("channel", "Unknown")),
        "duration": int(info.get("duration", 0)),
        "local_path": str(mp3_path),
        "content_type": "audio/mpeg",
        "content_length": mp3_path.stat().st_size,
    }


async def background_download(track: Track):
    """Download a track's audio in the background."""
    try:
        info = await download_youtube(track.source_url, track.id)
        track.local_path = info["local_path"]
        track.content_type = info["content_type"]
        track.content_length = info["content_length"]
        track.title = info["title"]  # update with full title
        track.artist = info["artist"]
        track.duration = info["duration"]
        # If this is the current track and nothing is playing, start it
        if _device_ready():
            idx = next((i for i, t in enumerate(state.queue) if t.id == track.id), -1)
            if idx == state.current_index:
                ts = await dlna_get_transport_state()
                if ts in ("STOPPED", "NO_MEDIA_PRESENT"):
                    await _play_current()
    except Exception as e:
        log.error("Background download failed for %s (%s): %s", track.id, track.source_url, e)


# ---------------------------------------------------------------------------
# Stream Endpoint (called by DLNA device)
# ---------------------------------------------------------------------------

@app.head("/stream/{track_id}")
@app.get("/stream/{track_id}")
async def stream_audio(track_id: str, request: Request):
    track = next((t for t in state.queue if t.id == track_id), None)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.local_path and Path(track.local_path).exists():
        # Serve local file with Range support
        return FileResponse(
            track.local_path,
            media_type=track.content_type,
            headers={
                "Accept-Ranges": "bytes",
                "transferMode.dlna.org": "Streaming",
                "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000",
            },
        )
    elif track.direct_url:
        # Proxy stream for radio/url sources
        return await _proxy_stream(track, request)
    else:
        raise HTTPException(status_code=404, detail="No audio source")


async def _proxy_stream(track: Track, request: Request):
    url = track.direct_url or track.source_url
    headers = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    if request.method == "HEAD":
        return Response(status_code=200, headers={
            "Content-Type": track.content_type,
            "Accept-Ranges": "bytes",
            "transferMode.dlna.org": "Streaming",
        })

    upstream = await http_client.send(
        http_client.build_request("GET", url, headers=headers),
        stream=True,
    )

    response_headers = {
        "Content-Type": track.content_type,
        "Accept-Ranges": "bytes",
        "transferMode.dlna.org": "Streaming",
    }
    if "content-length" in upstream.headers:
        response_headers["Content-Length"] = upstream.headers["content-length"]
    if "content-range" in upstream.headers:
        response_headers["Content-Range"] = upstream.headers["content-range"]

    async def generate():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(generate(), status_code=upstream.status_code, headers=response_headers)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

def _detect_type(url: str) -> str:
    lower = url.lower()
    if any(d in lower for d in ["youtube.com", "youtu.be", "music.youtube.com"]):
        return "youtube"
    if any(lower.endswith(ext) for ext in [".mp3", ".flac", ".aac", ".wav", ".m4a", ".ogg"]):
        return "url"
    return "radio"


@app.post("/api/search")
async def api_search(req: SearchRequest):
    """Search YouTube via yt-dlp and return results."""
    query = f"ytsearch{req.max_results}:{req.query}"
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--flat-playlist", "--dump-json", query,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"Search failed: {stderr.decode()[:200]}")

    results = []
    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue
        info = json.loads(line)
        vid = info.get("id", "")
        results.append({
            "id": vid,
            "title": info.get("title", "Unknown"),
            "artist": info.get("uploader", info.get("channel", "")),
            "duration": int(info.get("duration") or 0),
            "url": f"https://www.youtube.com/watch?v={vid}" if vid else "",
            "thumbnail": info.get("thumbnail", ""),
        })
    return {"results": results}


@app.get("/api/devices")
async def api_devices():
    """List discovered DLNA renderers."""
    current_name = device.friendly_name if device else None
    return {
        "devices": list(discovered_devices.values()),
        "current": current_name,
    }


@app.post("/api/devices/select")
async def api_device_select(req: DeviceSelectRequest):
    """Switch to a different DLNA device by name."""
    name = req.name
    if name not in discovered_devices:
        raise HTTPException(status_code=404, detail="Device not found")
    global av_transport, rendering_control
    av_transport = None
    rendering_control = None
    await connect_to_device(discovered_devices[name]["url"])
    return {"ok": True, "device": name}


@app.post("/api/devices/refresh")
async def api_devices_refresh():
    """Re-scan the network for DLNA devices."""
    global av_transport, rendering_control
    av_transport = None
    rendering_control = None
    discovered_devices.clear()
    await discover_and_connect()
    return {
        "devices": list(discovered_devices.values()),
        "current": device.friendly_name if device else None,
    }


def _is_playlist_url(url: str) -> bool:
    lower = url.lower()
    return "list=" in lower or "/playlist" in lower or "/@" in lower or "/channel/" in lower


def _normalize_yt_url(url: str) -> str:
    """Extract video ID to compare YouTube URLs regardless of format."""
    import re
    m = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else url


def _is_duplicate(url: str) -> bool:
    """Check if a URL is already in the queue."""
    existing = {t.source_url for t in state.queue}
    if url in existing:
        return True
    # Also compare by YouTube video ID (handles youtu.be vs youtube.com)
    norm = _normalize_yt_url(url)
    return any(_normalize_yt_url(t.source_url) == norm for t in state.queue)


@app.post("/api/queue/add")
async def queue_add(req: AddRequest):
    source_type = req.type if req.type != "auto" else _detect_type(req.url)

    if source_type == "youtube" and _is_playlist_url(req.url):
        entries = await extract_playlist(req.url)
        if not entries:
            raise HTTPException(status_code=400, detail="No entries found in playlist")

        tracks = []
        skipped = 0
        for entry in entries:
            if not entry["url"]:
                continue
            if _is_duplicate(entry["url"]):
                skipped += 1
                continue
            track_id = str(uuid.uuid4())[:8]
            track = Track(
                id=track_id,
                title=entry["title"],
                artist=entry["artist"],
                source_type="youtube",
                source_url=entry["url"],
                duration=entry["duration"],
            )
            state.queue.append(track)
            tracks.append(track)

        if not tracks and skipped:
            return {"ok": True, "count": 0, "skipped": skipped, "tracks": []}

        was_empty = state.current_index == -1
        if was_empty and tracks:
            state.current_index = len(state.queue) - len(tracks)

        asyncio.create_task(_download_playlist_tracks(tracks, play_first=was_empty))

        return {
            "ok": True,
            "count": len(tracks),
            "skipped": skipped,
            "tracks": [_track_dict(t) for t in tracks[:10]],
        }

    # Single track duplicate check
    if _is_duplicate(req.url):
        return {"ok": True, "duplicate": True, "detail": "Already in queue"}

    if source_type == "youtube":
        track_id = str(uuid.uuid4())[:8]
        state.download = DownloadProgress(
            total=1, done=0, current_title="Downloading...",
            track_ids=[track_id], started_at=time.time(),
        )
        info = await download_youtube(req.url, track_id)
        state.download = DownloadProgress()  # clear
        track = Track(
            id=track_id,
            title=info["title"],
            artist=info["artist"],
            source_type="youtube",
            source_url=req.url,
            duration=info["duration"],
            local_path=info["local_path"],
            content_type=info["content_type"],
            content_length=info["content_length"],
        )
    else:
        track_id = str(uuid.uuid4())[:8]
        track = Track(
            id=track_id,
            title=req.url.split("/")[-1] or req.url,
            artist="",
            source_type=source_type,
            source_url=req.url,
            direct_url=req.url,
        )

    state.queue.append(track)

    if state.current_index == -1:
        state.current_index = len(state.queue) - 1
        try:
            await _play_current()
        except Exception as e:
            log.warning("Auto-play failed for track %s: %s", track.id, e)

    return {"ok": True, "track": _track_dict(track)}


async def _download_playlist_tracks(tracks: list[Track], play_first: bool = False):
    """Download playlist tracks sequentially in the background."""
    state.download = DownloadProgress(
        total=len(tracks), done=0, current_title="",
        track_ids=[t.id for t in tracks], started_at=time.time(),
    )
    for i, track in enumerate(tracks):
        state.download.current_title = track.title
        try:
            info = await download_youtube(track.source_url, track.id)
            track.local_path = info["local_path"]
            track.content_type = info["content_type"]
            track.content_length = info["content_length"]
            track.title = info["title"]
            track.artist = info["artist"]
            track.duration = info["duration"]

            if i == 0 and play_first and _device_ready():
                try:
                    await _play_current()
                except Exception as e:
                    log.warning("Auto-play first playlist track failed: %s", e)

            state.download.done = i + 1
            log.info("Downloaded [%d/%d]: %s", i + 1, len(tracks), track.title)
            _save_state()
        except Exception as e:
            state.download.done = i + 1
            log.error("Failed to download %s: %s", track.source_url, e)
    state.download = DownloadProgress()  # clear when done


@app.delete("/api/queue/{track_id}")
async def queue_remove(track_id: str):
    idx = next((i for i, t in enumerate(state.queue) if t.id == track_id), None)
    if idx is None:
        raise HTTPException(status_code=404)
    track = state.queue.pop(idx)
    # Clean up cache file
    if track.local_path and Path(track.local_path).exists():
        Path(track.local_path).unlink(missing_ok=True)
    if idx < state.current_index:
        state.current_index -= 1
    elif idx == state.current_index:
        await dlna_stop()
        if state.current_index >= len(state.queue):
            state.current_index = -1
    return {"ok": True}


@app.get("/api/queue")
async def queue_list():
    return [_track_dict(t) for t in state.queue]


@app.post("/api/play")
async def api_play(req: PlayRequest = PlayRequest()):
    if req.track_id:
        idx = next((i for i, t in enumerate(state.queue) if t.id == req.track_id), None)
        if idx is None:
            raise HTTPException(status_code=404)
        state.current_index = idx
        await _play_current()
    else:
        transport = await dlna_get_transport_state()
        if transport in ("PAUSED_PLAYBACK", "PAUSED"):
            await dlna_play()
        elif state.current_index >= 0:
            await _play_current()
    return {"ok": True}


@app.post("/api/pause")
async def api_pause():
    await dlna_pause()
    return {"ok": True}


@app.post("/api/stop")
async def api_stop():
    await dlna_stop()
    return {"ok": True}


@app.post("/api/next")
async def api_next():
    if state.current_index < len(state.queue) - 1:
        state.current_index += 1
        await _play_current()
        return {"ok": True}
    return {"ok": False, "reason": "end of queue"}


@app.post("/api/prev")
async def api_prev():
    if state.current_index > 0:
        state.current_index -= 1
        await _play_current()
        return {"ok": True}
    return {"ok": False, "reason": "start of queue"}


@app.post("/api/volume")
async def api_volume(req: VolumeRequest):
    level = max(0, min(100, req.level))
    await dlna_set_volume(level)
    state.volume = level
    return {"ok": True}


@app.post("/api/seek")
async def api_seek(req: SeekRequest):
    if not _device_ready():
        raise HTTPException(status_code=503, detail="Device not connected")
    await dlna_seek(max(0, req.position))
    return {"ok": True}


@app.post("/api/play-mode")
async def api_play_mode(req: PlayModeRequest):
    mode = req.mode.upper()
    if mode not in ("NORMAL", "REPEAT_ALL", "REPEAT_ONE", "SHUFFLE"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    state.play_mode = mode
    if _device_ready():
        try:
            await dlna_set_play_mode(mode)
        except Exception as e:
            log.warning("SetPlayMode failed (device may not support it): %s", e)
    return {"ok": True, "mode": mode}


@app.get("/api/status")
async def api_status():
    transport_state = "STOPPED"
    position = {"position": 0, "duration": 0}

    try:
        transport_state = await dlna_get_transport_state()
        position = await dlna_get_position()
    except Exception as e:
        log.debug("Status poll failed: %s", e)
    # Volume is cached, updated only on connect and after volume API calls

    current = None
    if 0 <= state.current_index < len(state.queue):
        current = _track_dict(state.queue[state.current_index])

    dl = state.download
    download = None
    if dl.total > 0:
        dl_tracks = []
        for tid in dl.track_ids:
            t = next((t for t in state.queue if t.id == tid), None)
            if t:
                dl_tracks.append({
                    "id": t.id,
                    "title": t.title,
                    "ready": bool(t.local_path),
                    "downloading": t.id == next(
                        (tr.id for tr in state.queue
                         if tr.id in dl.track_ids and not tr.local_path),
                        None
                    ),
                })
        elapsed = time.time() - dl.started_at if dl.started_at else 0
        download = {
            "total": dl.total, "done": dl.done,
            "current": dl.current_title, "tracks": dl_tracks,
            "elapsed": round(elapsed),
        }

    return {
        "current_track": current,
        "current_index": state.current_index,
        "transport_state": transport_state,
        "position": position["position"],
        "duration": position["duration"],
        "volume": state.volume,
        "device_connected": _device_ready(),
        "device_name": device.friendly_name if device else None,
        "play_mode": state.play_mode,
        "download": download,
        "queue": [_track_dict(t) for t in state.queue],
    }


@app.get("/api/logs")
async def api_logs():
    return {"logs": list(_log_buffer)}


# ---------------------------------------------------------------------------
# Auto-advance
# ---------------------------------------------------------------------------

_last_transport_state = "STOPPED"


async def auto_advance_loop():
    global _last_transport_state
    while True:
        await asyncio.sleep(3)
        if not _device_ready():
            continue
        try:
            ts = await dlna_get_transport_state()
            if _last_transport_state == "PLAYING" and ts in ("STOPPED", "NO_MEDIA_PRESENT"):
                next_idx = _get_next_index()
                if next_idx is not None:
                    state.current_index = next_idx
                    log.info("Auto-advancing to track %d (%s)", state.current_index, state.play_mode)
                    try:
                        await _play_current()
                    except Exception as e:
                        log.error("Auto-advance play failed: %s. Waiting for device...", e)
                        # Wait for TRANSITIONING to clear before retry
                        for _ in range(5):
                            await asyncio.sleep(2)
                            try:
                                ts2 = await dlna_get_transport_state()
                                if ts2 != "TRANSITIONING":
                                    break
                            except Exception:
                                pass
                        try:
                            await _play_current()
                        except Exception as e2:
                            log.error("Auto-advance retry failed: %s", e2)
                    _save_state()
            _last_transport_state = ts
        except Exception as e:
            log.debug("Auto-advance poll error: %s", e)


async def keepalive_loop():
    """Ping the connected device every 15s to prevent it from sleeping.
    Retries up to 3 times before marking disconnected.  When disconnected,
    nudges the device with a raw HTTP GET to its description URL so it stays
    on Wi-Fi long enough for SSDP rediscovery."""
    global av_transport, rendering_control
    fail_count = 0
    max_failures = 3
    while True:
        await asyncio.sleep(15)
        if _device_ready():
            try:
                await dlna_get_transport_state()
                fail_count = 0
            except Exception as e:
                fail_count += 1
                log.warning("Device keepalive failed (%d/%d): %s",
                            fail_count, max_failures, e)
                if fail_count >= max_failures:
                    log.warning("Device unresponsive after %d pings, "
                                "marking disconnected", max_failures)
                    av_transport = None
                    rendering_control = None
                    fail_count = 0
        elif state.last_device_url:
            # Device disconnected — nudge it with a lightweight HTTP request
            # to prevent it from going into deep sleep before SSDP finds it.
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.get(state.last_device_url)
            except Exception:
                pass  # best-effort; device may already be asleep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _device_ready():
    return av_transport is not None and rendering_control is not None


async def _play_current():
    if state.current_index < 0 or state.current_index >= len(state.queue):
        return
    if not _device_ready():
        raise HTTPException(status_code=503, detail="Device not connected")
    track = state.queue[state.current_index]
    await dlna_set_uri(track)
    await dlna_play()
    # Pre-load next track for gapless playback
    await _preload_next()


async def _preload_next():
    """Set the next track URI for gapless playback."""
    if not _device_ready():
        return
    next_idx = _get_next_index()
    if next_idx is None:
        return
    next_track = state.queue[next_idx]
    if not next_track.local_path and next_track.source_type == "youtube":
        return  # Not downloaded yet
    try:
        await dlna_set_next_uri(next_track)
        log.debug("Pre-loaded next track: %s", next_track.title)
    except Exception as e:
        log.debug("SetNextAVTransportURI not supported or failed: %s", e)


def _get_next_index() -> int | None:
    """Get the next track index based on play mode."""
    if not state.queue:
        return None
    if state.play_mode == "REPEAT_ONE":
        return state.current_index
    if state.play_mode == "SHUFFLE":
        import random
        candidates = [i for i in range(len(state.queue)) if i != state.current_index]
        return random.choice(candidates) if candidates else None
    # NORMAL or REPEAT_ALL
    next_idx = state.current_index + 1
    if next_idx >= len(state.queue):
        if state.play_mode == "REPEAT_ALL":
            return 0
        return None
    return next_idx


def _track_dict(t: Track) -> dict:
    ready = bool(t.local_path or t.direct_url or t.source_type != "youtube")
    return {
        "id": t.id,
        "title": t.title,
        "artist": t.artist,
        "source_type": t.source_type,
        "duration": t.duration,
        "ready": ready,
    }


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR))


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
